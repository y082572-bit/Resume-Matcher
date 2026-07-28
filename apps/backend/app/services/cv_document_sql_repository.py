"""Explicit Provenance Stage P6-B1: the synchronous SQL document artifact
repository.

``CvDocumentArtifactSqlRepository`` implements the exact P6-A
``CvDocumentArtifactRepository`` Protocol (structurally and at runtime) over
a synchronous SQLAlchemy ``Session`` and a ``CvDocumentBlobStore``. It is
the production successor to ``InMemoryCvDocumentArtifactRepository`` -- the
P6-A Protocol itself is never modified, and every method here stays
synchronous ``def`` (no ``async def``, no ``AsyncSession``, no
``asyncio.run``, no nested event loop). A future async router is expected
to call every method on this class through a threadpool
(``anyio.to_thread.run_sync``/``run_in_executor``) -- that boundary is
described in ``docs/explicit-provenance-stage-p6b1.md`` and is never
implemented here (no FastAPI/Starlette import exists in this module).

Ordering discipline used by every mutating method:

1. Independently recompute the owner-key fingerprint, the artifact/snapshot
   fingerprint, and the raw bytes hash -- a stored fingerprint or a
   caller's own field is never trusted at face value.
2. Write (or reuse) the blob **before** opening any DB transaction, so a
   failed DB transaction can only ever leave an orphan blob on disk, never
   a DB row pointing at bytes that were never durably written.
3. Open one ``Session``/transaction per mutating call. A CAS mismatch
   detected up front returns ``STALE_REVISION`` without touching any table.
   A race between two concurrent first-writers is resolved by the unique
   constraints on ``cv_document_proposal_slots``/``cv_docx_proposal_artifacts``
   (and their PDF/snapshot counterparts): the loser's ``IntegrityError``
   is caught, its transaction is already rolled back by the context
   manager, and the loser is reported ``STALE_REVISION`` with the winner's
   now-current artifact.
4. A genuine data-integrity violation (a tampered fingerprint, an unknown
   owner, a dangling snapshot/proposal reference) is never silently
   converted into a Protocol status the caller could mistake for a normal
   business outcome -- it raises ``CvDocumentStorageError`` instead.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_artifact import (
    ConfirmedCvPdfArtifact,
    CvDocxProposalArtifact,
    DocumentProvenanceMode,
    DocxProposalProvenanceMode,
    DocxStructuralValidationResult,
    DocxStructuralValidationStatus,
    JobArtifactOwnerKey,
    ValidatedCvDocxSnapshot,
)
from app.schemas.cv_document_storage import (
    CvDocumentStorageErrorCode,
    CvDocumentStorageOperationResult,
    DocumentArtifactStorageStatus,
)
from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfLineageKind
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
    CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN,
)
from app.services.cv_document_models import (
    CvConfirmedPdfArtifactRow,
    CvConfirmedPdfCurrentAuthorityRow,
    CvDocumentArtifactOwner,
    CvDocumentPdfSlot,
    CvDocumentProposalSlot,
    CvDocxProposalArtifactRow,
    CvDocxValidatedSnapshotRow,
)
from app.services.cv_document_storage_shared import (
    ensure_blob_row as _shared_ensure_blob_row,
    ensure_owner as _shared_ensure_owner,
    require_owner_key as _shared_require_owner_key,
    row_to_owner_key as _shared_row_to_owner_key,
)
from app.services.cv_document_pdf_confirmation_builder import compute_pdf_artifact_fingerprint
from app.services.cv_document_proposal_builder import compute_proposal_artifact_fingerprint
from app.services.cv_document_repository_protocol import (
    CasReplaceStatus,
    CvDocumentPdfSlotCutoverStatus,
    PdfReplaceResult,
    ProposalReplaceResult,
    SnapshotSaveResult,
    SnapshotSaveStatus,
)
from app.services.cv_document_snapshot_builder import compute_snapshot_fingerprint


_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MEDIA_TYPE = "application/pdf"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ConcurrentSlotMutation(Exception):
    """Internal-only: a slot's optimistic ``slot_version`` no longer
    matched at UPDATE time. Always caught within the same method and
    converted into ``CasReplaceStatus.STALE_REVISION`` -- never surfaced to
    a caller."""


class _AuthorityBridgeConflict(Exception):
    """Internal-only: Explicit Provenance Stage P6-B3b-A legacy-authority
    write bridge -- the ``cv_confirmed_pdf_current_authority`` row was
    absent-or-LEGACY at pre-read time but no longer matched (either raced
    to ``FINAL_DOCX_SNAPSHOT``, or its ``slot_version`` moved) by the time
    the bridge UPDATE ran. Always caught within the same method, which
    rolls back the whole transaction (including the legacy slot CAS that
    already succeeded) and re-classifies from a fresh authority read --
    never surfaced to a caller."""


class CvDocumentArtifactSqlRepository:
    """Synchronous SQL + content-addressed-blob implementation of the P6-A
    ``CvDocumentArtifactRepository`` Protocol.

    Accepts an already-configured sync session factory (e.g. the same
    ``sessionmaker(sync_engine, expire_on_commit=False)`` ``app.database``
    builds for the ``api_keys`` table) and a ``CvDocumentBlobStore`` -- it
    never constructs its own engine, never runs a migration, and never
    creates a second SQLite database.
    """

    def __init__(
        self, session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store

    # -- owner upsert (section K) --------------------------------------------

    # Explicit Provenance Stage P6-B1 SQL storage addendum (H1): the actual
    # logic now lives in ``cv_document_storage_shared.py``, shared verbatim
    # with ``FinalDocxSnapshotSqlRepository`` -- these remain thin delegating
    # wrappers (same names, same signatures, same semantics) purely so
    # existing monkeypatch-based tests (e.g. patching ``repo._ensure_owner``)
    # keep working unmodified.

    def _ensure_owner(self, session: Session, owner_key: JobArtifactOwnerKey) -> None:
        _shared_ensure_owner(session, owner_key)

    def _row_to_owner_key(self, owner_row: CvDocumentArtifactOwner) -> JobArtifactOwnerKey:
        return _shared_row_to_owner_key(owner_row)

    def _require_owner_key(self, session: Session, owner_key_fingerprint: str) -> JobArtifactOwnerKey:
        return _shared_require_owner_key(session, owner_key_fingerprint)

    # -- blob metadata row (ensure, never overwrite) -------------------------

    def _ensure_blob_row(self, session: Session, *, blob_sha256: str, byte_size: int, media_type: str) -> None:
        _shared_ensure_blob_row(
            session, self._blob_store, blob_sha256=blob_sha256, byte_size=byte_size, media_type=media_type
        )

    # -- domain reconstruction -----------------------------------------------

    def _row_to_domain_proposal(
        self, row: CvDocxProposalArtifactRow, owner_key: JobArtifactOwnerKey
    ) -> CvDocxProposalArtifact:
        return CvDocxProposalArtifact(
            artifact_id=UUID(row.artifact_id),
            owner_key=owner_key,
            approved_cv_content_fingerprint=row.approved_cv_content_fingerprint,
            content_plan_fingerprint=row.content_plan_fingerprint,
            role_strategy_context_fingerprint=row.role_strategy_context_fingerprint,
            document_schema_version=row.document_schema_version,
            rendering_policy_version=row.rendering_policy_version,
            renderer_adapter_id=row.renderer_adapter_id,
            template_fingerprint=row.template_fingerprint,
            generation_input_fingerprint=row.generation_input_fingerprint,
            generated_docx_content_hash=row.generated_docx_content_hash,
            current_file_hash=row.current_file_hash or row.generated_docx_content_hash,
            validated_file_hash=row.validated_file_hash,
            proposal_revision=row.proposal_revision,
            superseded=(row.artifact_storage_status != DocumentArtifactStorageStatus.ACTIVE.value),
            artifact_fingerprint=row.artifact_fingerprint,
            provenance_mode=DocxProposalProvenanceMode(row.provenance_mode),
        )

    def _row_to_domain_snapshot(
        self, row: CvDocxValidatedSnapshotRow, owner_key: JobArtifactOwnerKey
    ) -> ValidatedCvDocxSnapshot:
        # ``structural_validation_result`` is never persisted as its own
        # columns: only a snapshot whose structural validation already
        # succeeded (VALID, no violations, validated_sha256 ==
        # exact_docx_sha256) is ever allowed to reach ``save_validated_snapshot``
        # (see cv_document_snapshot_builder.py) -- so it is always safe and
        # exact to rebuild that closed sub-result deterministically here.
        return ValidatedCvDocxSnapshot(
            owner_key=owner_key,
            proposal_artifact_fingerprint=row.proposal_artifact_fingerprint,
            proposal_revision=row.proposal_revision,
            exact_docx_sha256=row.exact_docx_sha256,
            approved_cv_content_fingerprint=row.approved_cv_content_fingerprint,
            content_plan_fingerprint=row.content_plan_fingerprint,
            template_fingerprint=row.template_fingerprint,
            validation_policy_version=row.validation_policy_version,
            structural_validation_result=DocxStructuralValidationResult(
                status=DocxStructuralValidationStatus.VALID,
                validated_sha256=row.exact_docx_sha256,
            ),
            manual_confirmation_fingerprint=row.manual_confirmation_fingerprint,
            provenance_mode=DocumentProvenanceMode(row.provenance_mode),
            snapshot_fingerprint=row.snapshot_fingerprint,
        )

    def _row_to_domain_pdf(
        self, row: CvConfirmedPdfArtifactRow, owner_key: JobArtifactOwnerKey
    ) -> ConfirmedCvPdfArtifact:
        return ConfirmedCvPdfArtifact(
            artifact_id=UUID(row.artifact_id),
            owner_key=owner_key,
            source_validated_docx_snapshot_fingerprint=row.source_validated_docx_snapshot_fingerprint,
            source_docx_sha256=row.source_docx_sha256,
            approved_cv_content_fingerprint=row.approved_cv_content_fingerprint,
            content_plan_fingerprint=row.content_plan_fingerprint,
            pdf_sha256=row.pdf_sha256,
            conversion_adapter_id=row.conversion_adapter_id,
            conversion_policy_version=row.conversion_policy_version,
            provenance_mode=DocumentProvenanceMode(row.provenance_mode),
            artifact_fingerprint=row.artifact_fingerprint,
            pdf_revision=row.pdf_revision,
            superseded=(row.artifact_storage_status != DocumentArtifactStorageStatus.ACTIVE.value),
        )

    # -- proposal slot (reads) ------------------------------------------------

    def get_current_proposal(self, owner_key: JobArtifactOwnerKey) -> CvDocxProposalArtifact | None:
        with self._session_factory() as session:
            slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
            if slot is None or slot.current_artifact_fingerprint is None:
                return None
            row = session.get(CvDocxProposalArtifactRow, slot.current_artifact_fingerprint)
            if row is None:
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT,
                    "proposal slot points at a missing artifact row",
                )
            domain_owner_key = self._require_owner_key(session, owner_key.owner_key_fingerprint)
            return self._row_to_domain_proposal(row, domain_owner_key)

    def read_current_proposal_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None:
        with self._session_factory() as session:
            slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
            if slot is None or slot.current_artifact_fingerprint is None:
                return None
            row = session.get(CvDocxProposalArtifactRow, slot.current_artifact_fingerprint)
            if row is None:
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT,
                    "proposal slot points at a missing artifact row",
                )
            read_result = self._blob_store.read_blob(row.blob_sha256)
            if not read_result.success:
                return None
            return read_result.data

    def _reread_current_proposal_domain(
        self, session: Session, owner_key: JobArtifactOwnerKey
    ) -> CvDocxProposalArtifact | None:
        slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
        if slot is None or slot.current_artifact_fingerprint is None:
            return None
        row = session.get(CvDocxProposalArtifactRow, slot.current_artifact_fingerprint)
        if row is None:
            return None
        domain_owner_key = self._require_owner_key(session, owner_key.owner_key_fingerprint)
        return self._row_to_domain_proposal(row, domain_owner_key)

    # -- proposal slot (write, section L) -------------------------------------

    def replace_current_proposal(
        self,
        owner_key: JobArtifactOwnerKey,
        artifact: CvDocxProposalArtifact,
        docx_bytes: bytes,
        expected_previous_revision: int | None,
    ) -> ProposalReplaceResult:
        raw = bytes(docx_bytes)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if raw_sha256 != artifact.generated_docx_content_hash:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.ARTIFACT_BYTES_HASH_MISMATCH,
                "a fresh hash of docx_bytes does not match artifact.generated_docx_content_hash",
            )
        if owner_key.owner_key_fingerprint != artifact.owner_key.owner_key_fingerprint:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.OWNER_IDENTITY_MISMATCH,
                "owner_key does not match artifact.owner_key",
            )
        recomputed_fp = compute_proposal_artifact_fingerprint(
            owner_key_fingerprint=artifact.owner_key.owner_key_fingerprint,
            generation_input_fingerprint=artifact.generation_input_fingerprint,
            generated_docx_content_hash=artifact.generated_docx_content_hash,
            proposal_revision=artifact.proposal_revision,
        )
        if recomputed_fp != artifact.artifact_fingerprint:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.ARTIFACT_FINGERPRINT_MISMATCH,
                "a recomputed artifact_fingerprint does not match artifact.artifact_fingerprint",
            )

        write_result = self._blob_store.write_blob(
            raw, expected_sha256=raw_sha256, media_type=_DOCX_MEDIA_TYPE
        )
        if not write_result.success:
            assert write_result.error_code is not None
            raise CvDocumentStorageError(
                CvDocumentStorageOperationResult(
                    success=False, error_code=write_result.error_code, diagnostics=write_result.diagnostics
                )
            )

        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        self._ensure_owner(session, owner_key)

                        slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
                        current_revision = slot.current_revision if slot is not None else None
                        if current_revision != expected_previous_revision:
                            current_artifact = self._reread_current_proposal_domain(session, owner_key)
                            return ProposalReplaceResult(
                                status=CasReplaceStatus.STALE_REVISION, current_artifact=current_artifact
                            )

                        expected_next_revision = (current_revision or 0) + 1
                        if artifact.proposal_revision != expected_next_revision:
                            raise CvDocumentStorageError.of(
                                CvDocumentStorageErrorCode.ARTIFACT_FINGERPRINT_MISMATCH,
                                "artifact.proposal_revision does not match the computed next revision",
                            )

                        self._ensure_blob_row(
                            session, blob_sha256=raw_sha256, byte_size=len(raw), media_type=_DOCX_MEDIA_TYPE
                        )

                        if slot is not None and slot.current_artifact_fingerprint is not None:
                            previous_row = session.get(
                                CvDocxProposalArtifactRow, slot.current_artifact_fingerprint
                            )
                            if previous_row is not None:
                                previous_row.artifact_storage_status = (
                                    DocumentArtifactStorageStatus.SUPERSEDED.value
                                )

                        session.add(
                            CvDocxProposalArtifactRow(
                                artifact_fingerprint=artifact.artifact_fingerprint,
                                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                artifact_id=str(artifact.artifact_id),
                                approved_cv_content_fingerprint=artifact.approved_cv_content_fingerprint,
                                content_plan_fingerprint=artifact.content_plan_fingerprint,
                                role_strategy_context_fingerprint=artifact.role_strategy_context_fingerprint,
                                document_schema_version=artifact.document_schema_version,
                                rendering_policy_version=artifact.rendering_policy_version,
                                renderer_adapter_id=artifact.renderer_adapter_id,
                                template_fingerprint=artifact.template_fingerprint,
                                generation_input_fingerprint=artifact.generation_input_fingerprint,
                                generated_docx_content_hash=artifact.generated_docx_content_hash,
                                current_file_hash=artifact.current_file_hash,
                                validated_file_hash=artifact.validated_file_hash,
                                proposal_revision=artifact.proposal_revision,
                                provenance_mode=artifact.provenance_mode.value,
                                artifact_storage_status=DocumentArtifactStorageStatus.ACTIVE.value,
                                blob_sha256=raw_sha256,
                            )
                        )
                        session.flush()

                        if slot is None:
                            session.add(
                                CvDocumentProposalSlot(
                                    owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                    current_artifact_fingerprint=artifact.artifact_fingerprint,
                                    current_revision=artifact.proposal_revision,
                                    slot_version=1,
                                    updated_at=_utcnow_iso(),
                                )
                            )
                            session.flush()
                        else:
                            result = session.execute(
                                update(CvDocumentProposalSlot)
                                .where(
                                    CvDocumentProposalSlot.owner_key_fingerprint
                                    == owner_key.owner_key_fingerprint,
                                    CvDocumentProposalSlot.slot_version == slot.slot_version,
                                )
                                .values(
                                    current_artifact_fingerprint=artifact.artifact_fingerprint,
                                    current_revision=artifact.proposal_revision,
                                    slot_version=slot.slot_version + 1,
                                    updated_at=_utcnow_iso(),
                                )
                            )
                            if result.rowcount != 1:
                                raise _ConcurrentSlotMutation()
                except (IntegrityError, _ConcurrentSlotMutation):
                    return ProposalReplaceResult(
                        status=CasReplaceStatus.STALE_REVISION,
                        current_artifact=self._reread_current_proposal_domain(session, owner_key),
                    )

                return ProposalReplaceResult(
                    status=CasReplaceStatus.REPLACED,
                    current_artifact=self._reread_current_proposal_domain(session, owner_key),
                )
        except OperationalError as exc:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.STORAGE_UNAVAILABLE, f"sqlite operational error: {exc}"
            ) from exc

    def mark_proposal_superseded(self, owner_key: JobArtifactOwnerKey, artifact_fingerprint: str) -> None:
        with self._session_factory() as session:
            with session.begin():
                slot = session.get(CvDocumentProposalSlot, owner_key.owner_key_fingerprint)
                if slot is None or slot.current_artifact_fingerprint != artifact_fingerprint:
                    return
                row = session.get(CvDocxProposalArtifactRow, artifact_fingerprint)
                if row is not None:
                    row.artifact_storage_status = DocumentArtifactStorageStatus.SUPERSEDED.value
                slot.current_artifact_fingerprint = None
                slot.current_revision = None
                slot.slot_version += 1
                slot.updated_at = _utcnow_iso()

    # -- PDF slot (reads) ------------------------------------------------------

    def get_current_pdf(self, owner_key: JobArtifactOwnerKey) -> ConfirmedCvPdfArtifact | None:
        """Explicit Provenance Stage P6-B3b-A cross-line current-authority
        bridge: reads exclusively from ``cv_confirmed_pdf_current_authority``
        -- never from ``cv_document_pdf_slots``, which is frozen historical
        data only. The returned ``ConfirmedCvPdfArtifact | None`` represents
        exclusively the *legacy* (``LEGACY_VALIDATED_DOCX``) lineage view:
        ``None`` is returned both when there is no current authority row at
        all and when the current authority is the newer
        ``FINAL_DOCX_SNAPSHOT`` lineage -- a caller must not treat this
        method's ``None`` as proof no PDF exists at all for the owner.
        """
        with self._session_factory() as session:
            authority = session.get(CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
            if authority is None:
                return None
            if authority.lineage_kind != ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX.value:
                return None
            row = session.get(CvConfirmedPdfArtifactRow, authority.current_artifact_fingerprint)
            if row is None:
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT,
                    "legacy current authority points at a missing artifact row",
                )
            domain_owner_key = self._require_owner_key(session, owner_key.owner_key_fingerprint)
            return self._row_to_domain_pdf(row, domain_owner_key)

    def read_current_pdf_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None:
        """Same authority-first semantics as :meth:`get_current_pdf` --
        exclusively the legacy lineage view. Returns ``None`` for no
        authority row, a ``FINAL_DOCX_SNAPSHOT`` current authority, or a
        legacy blob that fails to read back."""
        with self._session_factory() as session:
            authority = session.get(CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
            if authority is None:
                return None
            if authority.lineage_kind != ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX.value:
                return None
            row = session.get(CvConfirmedPdfArtifactRow, authority.current_artifact_fingerprint)
            if row is None:
                raise CvDocumentStorageError.of(
                    CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT,
                    "legacy current authority points at a missing artifact row",
                )
            read_result = self._blob_store.read_blob(row.blob_sha256)
            if not read_result.success:
                return None
            return read_result.data

    def _reread_current_pdf_domain(
        self, session: Session, owner_key: JobArtifactOwnerKey
    ) -> ConfirmedCvPdfArtifact | None:
        slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
        if slot is None or slot.current_artifact_fingerprint is None:
            return None
        row = session.get(CvConfirmedPdfArtifactRow, slot.current_artifact_fingerprint)
        if row is None:
            return None
        domain_owner_key = self._require_owner_key(session, owner_key.owner_key_fingerprint)
        return self._row_to_domain_pdf(row, domain_owner_key)

    # -- PDF slot (write, section M) -------------------------------------------

    def replace_current_pdf(
        self,
        owner_key: JobArtifactOwnerKey,
        artifact: ConfirmedCvPdfArtifact,
        pdf_bytes: bytes,
        expected_previous_revision: int | None,
    ) -> PdfReplaceResult:
        raw = bytes(pdf_bytes)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if raw_sha256 != artifact.pdf_sha256:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.ARTIFACT_BYTES_HASH_MISMATCH,
                "a fresh hash of pdf_bytes does not match artifact.pdf_sha256",
            )
        if owner_key.owner_key_fingerprint != artifact.owner_key.owner_key_fingerprint:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.OWNER_IDENTITY_MISMATCH,
                "owner_key does not match artifact.owner_key",
            )
        recomputed_fp = compute_pdf_artifact_fingerprint(
            owner_key_fingerprint=artifact.owner_key.owner_key_fingerprint,
            source_validated_docx_snapshot_fingerprint=artifact.source_validated_docx_snapshot_fingerprint,
            source_docx_sha256=artifact.source_docx_sha256,
            pdf_sha256=artifact.pdf_sha256,
            conversion_adapter_id=artifact.conversion_adapter_id,
            conversion_policy_version=artifact.conversion_policy_version,
            pdf_revision=artifact.pdf_revision,
        )
        if recomputed_fp != artifact.artifact_fingerprint:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.ARTIFACT_FINGERPRINT_MISMATCH,
                "a recomputed artifact_fingerprint does not match artifact.artifact_fingerprint",
            )

        write_result = self._blob_store.write_blob(
            raw, expected_sha256=raw_sha256, media_type=_PDF_MEDIA_TYPE
        )
        if not write_result.success:
            assert write_result.error_code is not None
            raise CvDocumentStorageError(
                CvDocumentStorageOperationResult(
                    success=False, error_code=write_result.error_code, diagnostics=write_result.diagnostics
                )
            )

        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        self._ensure_owner(session, owner_key)

                        # Explicit Provenance Stage P6-B3b-A legacy-authority
                        # write bridge, step 1-2: pre-read cross-line current
                        # authority before ever touching the legacy slot. A
                        # FINAL_DOCX_SNAPSHOT current authority permanently
                        # blocks this legacy write path -- no legacy slot
                        # mutation, no artifact insert, nothing to roll back.
                        #
                        # ``cv_confirmed_pdf_current_authority`` is only
                        # guaranteed to exist once the P6-B3b-A cutover
                        # installer has run (always true in production,
                        # where ``db_engine.init_models_sync`` runs it before
                        # any request is ever served). A database that
                        # predates that cutover (or a test fixture modeling
                        # exactly that pre-cutover state) may not have the
                        # table yet -- the bridge is then simply inactive,
                        # and this call behaves exactly as it did before
                        # this remediation.
                        authority_table_exists = (
                            session.execute(
                                text(
                                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='cv_confirmed_pdf_current_authority'"
                                )
                            ).first()
                            is not None
                        )
                        authority_row = None
                        captured_authority_slot_version = None
                        if authority_table_exists:
                            authority_row = session.get(
                                CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint
                            )
                            if (
                                authority_row is not None
                                and authority_row.lineage_kind == ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT.value
                            ):
                                return PdfReplaceResult(
                                    status=CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
                                )
                            captured_authority_slot_version = (
                                authority_row.slot_version if authority_row is not None else None
                            )

                        snapshot_row = session.get(
                            CvDocxValidatedSnapshotRow, artifact.source_validated_docx_snapshot_fingerprint
                        )
                        if snapshot_row is None or snapshot_row.exact_docx_sha256 != artifact.source_docx_sha256:
                            raise CvDocumentStorageError.of(
                                CvDocumentStorageErrorCode.SNAPSHOT_FINGERPRINT_MISMATCH,
                                "referenced validated snapshot not found or source_docx_sha256 mismatch",
                            )

                        slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
                        current_revision = slot.current_revision if slot is not None else None
                        if current_revision != expected_previous_revision:
                            current_artifact = self._reread_current_pdf_domain(session, owner_key)
                            return PdfReplaceResult(
                                status=CasReplaceStatus.STALE_REVISION, current_artifact=current_artifact
                            )

                        expected_next_revision = (current_revision or 0) + 1
                        if artifact.pdf_revision != expected_next_revision:
                            raise CvDocumentStorageError.of(
                                CvDocumentStorageErrorCode.ARTIFACT_FINGERPRINT_MISMATCH,
                                "artifact.pdf_revision does not match the computed next revision",
                            )

                        self._ensure_blob_row(
                            session, blob_sha256=raw_sha256, byte_size=len(raw), media_type=_PDF_MEDIA_TYPE
                        )

                        if slot is not None and slot.current_artifact_fingerprint is not None:
                            previous_row = session.get(
                                CvConfirmedPdfArtifactRow, slot.current_artifact_fingerprint
                            )
                            if previous_row is not None:
                                previous_row.artifact_storage_status = (
                                    DocumentArtifactStorageStatus.SUPERSEDED.value
                                )

                        session.add(
                            CvConfirmedPdfArtifactRow(
                                artifact_fingerprint=artifact.artifact_fingerprint,
                                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                artifact_id=str(artifact.artifact_id),
                                source_validated_docx_snapshot_fingerprint=(
                                    artifact.source_validated_docx_snapshot_fingerprint
                                ),
                                source_docx_sha256=artifact.source_docx_sha256,
                                approved_cv_content_fingerprint=artifact.approved_cv_content_fingerprint,
                                content_plan_fingerprint=artifact.content_plan_fingerprint,
                                pdf_sha256=artifact.pdf_sha256,
                                conversion_adapter_id=artifact.conversion_adapter_id,
                                conversion_policy_version=artifact.conversion_policy_version,
                                provenance_mode=artifact.provenance_mode.value,
                                pdf_revision=artifact.pdf_revision,
                                blob_sha256=raw_sha256,
                                artifact_storage_status=DocumentArtifactStorageStatus.ACTIVE.value,
                            )
                        )
                        session.flush()

                        if slot is None:
                            session.add(
                                CvDocumentPdfSlot(
                                    owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                    current_artifact_fingerprint=artifact.artifact_fingerprint,
                                    current_revision=artifact.pdf_revision,
                                    slot_version=1,
                                    updated_at=_utcnow_iso(),
                                )
                            )
                            session.flush()
                        else:
                            result = session.execute(
                                update(CvDocumentPdfSlot)
                                .where(
                                    CvDocumentPdfSlot.owner_key_fingerprint == owner_key.owner_key_fingerprint,
                                    CvDocumentPdfSlot.slot_version == slot.slot_version,
                                )
                                .values(
                                    current_artifact_fingerprint=artifact.artifact_fingerprint,
                                    current_revision=artifact.pdf_revision,
                                    slot_version=slot.slot_version + 1,
                                    updated_at=_utcnow_iso(),
                                )
                            )
                            if result.rowcount != 1:
                                raise _ConcurrentSlotMutation()

                        # Explicit Provenance Stage P6-B3b-A legacy-authority
                        # write bridge, step 4-5: the legacy slot CAS above
                        # has already succeeded (and, transitively, already
                        # tripped the freeze trigger via IntegrityError if
                        # cv_document_pdf_slots is frozen) -- now synchronize
                        # cv_confirmed_pdf_current_authority to the same
                        # target artifact, atomically in this same
                        # transaction. A no-op if the table does not exist
                        # yet (bridge inactive; see the pre-read comment
                        # above).
                        if authority_table_exists:
                            if authority_row is None:
                                session.add(
                                    CvConfirmedPdfCurrentAuthorityRow(
                                        owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                        lineage_kind=ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX.value,
                                        current_artifact_fingerprint=artifact.artifact_fingerprint,
                                        slot_version=1,
                                        updated_at=_utcnow_iso(),
                                    )
                                )
                                session.flush()
                            else:
                                authority_result = session.execute(
                                    update(CvConfirmedPdfCurrentAuthorityRow)
                                    .where(
                                        CvConfirmedPdfCurrentAuthorityRow.owner_key_fingerprint
                                        == owner_key.owner_key_fingerprint,
                                        CvConfirmedPdfCurrentAuthorityRow.lineage_kind
                                        == ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX.value,
                                        CvConfirmedPdfCurrentAuthorityRow.slot_version
                                        == captured_authority_slot_version,
                                    )
                                    .values(
                                        current_artifact_fingerprint=artifact.artifact_fingerprint,
                                        slot_version=captured_authority_slot_version + 1,
                                        updated_at=_utcnow_iso(),
                                    )
                                )
                                if authority_result.rowcount != 1:
                                    raise _AuthorityBridgeConflict()
                except _ConcurrentSlotMutation:
                    return PdfReplaceResult(
                        status=CasReplaceStatus.STALE_REVISION,
                        current_artifact=self._reread_current_pdf_domain(session, owner_key),
                    )
                except _AuthorityBridgeConflict:
                    # The whole transaction (legacy slot CAS + artifact
                    # insert + this bridge attempt) has already been rolled
                    # back by the `with session.begin():` context manager --
                    # a fresh read on this same session now reflects
                    # genuinely current committed state.
                    fresh_authority = session.get(
                        CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint
                    )
                    if (
                        fresh_authority is not None
                        and fresh_authority.lineage_kind == ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT.value
                    ):
                        return PdfReplaceResult(
                            status=CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN
                        )
                    return PdfReplaceResult(
                        status=CasReplaceStatus.STALE_REVISION,
                        current_artifact=self._reread_current_pdf_domain(session, owner_key),
                    )
                except IntegrityError as exc:
                    if CV_DOCUMENT_PDF_SLOT_FROZEN_CUTOVER_TOKEN in str(exc):
                        # Explicit Provenance Stage P6-B3b-A: the legacy
                        # cv_document_pdf_slots table has been frozen by the
                        # Final-Confirmed-PDF cutover -- a permanent terminal
                        # status, never STALE_REVISION, never resolved by a
                        # retry.
                        return PdfReplaceResult(status=CvDocumentPdfSlotCutoverStatus.LEGACY_CURRENT_PDF_CUTOVER_FROZEN)
                    return PdfReplaceResult(
                        status=CasReplaceStatus.STALE_REVISION,
                        current_artifact=self._reread_current_pdf_domain(session, owner_key),
                    )

                return PdfReplaceResult(
                    status=CasReplaceStatus.REPLACED,
                    current_artifact=self._reread_current_pdf_domain(session, owner_key),
                )
        except OperationalError as exc:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.STORAGE_UNAVAILABLE, f"sqlite operational error: {exc}"
            ) from exc

    def mark_pdf_superseded(self, owner_key: JobArtifactOwnerKey, artifact_fingerprint: str) -> None:
        with self._session_factory() as session:
            with session.begin():
                slot = session.get(CvDocumentPdfSlot, owner_key.owner_key_fingerprint)
                if slot is None or slot.current_artifact_fingerprint != artifact_fingerprint:
                    return
                row = session.get(CvConfirmedPdfArtifactRow, artifact_fingerprint)
                if row is not None:
                    row.artifact_storage_status = DocumentArtifactStorageStatus.SUPERSEDED.value
                slot.current_artifact_fingerprint = None
                slot.current_revision = None
                slot.slot_version += 1
                slot.updated_at = _utcnow_iso()

    # -- content-addressed snapshot storage (section N) ------------------------

    def save_validated_snapshot(
        self, snapshot: ValidatedCvDocxSnapshot, docx_bytes: bytes
    ) -> SnapshotSaveResult:
        raw = bytes(docx_bytes)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if raw_sha256 != snapshot.exact_docx_sha256:
            return SnapshotSaveResult(status=SnapshotSaveStatus.LOCATOR_CONTENT_MISMATCH)

        recomputed_fp = compute_snapshot_fingerprint(
            owner_key_fingerprint=snapshot.owner_key.owner_key_fingerprint,
            proposal_artifact_fingerprint=snapshot.proposal_artifact_fingerprint,
            proposal_revision=snapshot.proposal_revision,
            exact_docx_sha256=snapshot.exact_docx_sha256,
            approved_cv_content_fingerprint=snapshot.approved_cv_content_fingerprint,
            content_plan_fingerprint=snapshot.content_plan_fingerprint,
            template_fingerprint=snapshot.template_fingerprint,
            validation_policy_version=snapshot.validation_policy_version,
            structural_validated_sha256=snapshot.structural_validation_result.validated_sha256,
            manual_confirmation_fingerprint=snapshot.manual_confirmation_fingerprint,
            provenance_mode=snapshot.provenance_mode,
        )
        if recomputed_fp != snapshot.snapshot_fingerprint:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.SNAPSHOT_FINGERPRINT_MISMATCH,
                "a recomputed snapshot_fingerprint does not match snapshot.snapshot_fingerprint",
            )
        if snapshot.structural_validation_result.status != DocxStructuralValidationStatus.VALID:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.SNAPSHOT_FINGERPRINT_MISMATCH,
                "only a structurally VALID snapshot may ever be persisted",
            )

        write_result = self._blob_store.write_blob(
            raw, expected_sha256=raw_sha256, media_type=_DOCX_MEDIA_TYPE
        )
        if not write_result.success:
            assert write_result.error_code is not None
            raise CvDocumentStorageError(
                CvDocumentStorageOperationResult(
                    success=False, error_code=write_result.error_code, diagnostics=write_result.diagnostics
                )
            )

        with self._session_factory() as session:
            with session.begin():
                self._ensure_owner(session, snapshot.owner_key)

                proposal_row = session.get(
                    CvDocxProposalArtifactRow, snapshot.proposal_artifact_fingerprint
                )
                if (
                    proposal_row is None
                    or proposal_row.owner_key_fingerprint != snapshot.owner_key.owner_key_fingerprint
                ):
                    raise CvDocumentStorageError.of(
                        CvDocumentStorageErrorCode.ARTIFACT_FINGERPRINT_MISMATCH,
                        "referenced proposal_artifact_fingerprint not found or owner mismatch",
                    )

                self._ensure_blob_row(
                    session, blob_sha256=raw_sha256, byte_size=len(raw), media_type=_DOCX_MEDIA_TYPE
                )

                existing = session.get(CvDocxValidatedSnapshotRow, snapshot.snapshot_fingerprint)
                if existing is not None:
                    if (
                        existing.owner_key_fingerprint != snapshot.owner_key.owner_key_fingerprint
                        or existing.proposal_artifact_fingerprint != snapshot.proposal_artifact_fingerprint
                        or existing.proposal_revision != snapshot.proposal_revision
                        or existing.blob_sha256 != raw_sha256
                        or existing.exact_docx_sha256 != snapshot.exact_docx_sha256
                        or existing.approved_cv_content_fingerprint
                        != snapshot.approved_cv_content_fingerprint
                        or existing.content_plan_fingerprint != snapshot.content_plan_fingerprint
                        or existing.template_fingerprint != snapshot.template_fingerprint
                        or existing.validation_policy_version != snapshot.validation_policy_version
                        or existing.manual_confirmation_fingerprint
                        != snapshot.manual_confirmation_fingerprint
                        or existing.provenance_mode != snapshot.provenance_mode.value
                    ):
                        return SnapshotSaveResult(status=SnapshotSaveStatus.LOCATOR_CONTENT_MISMATCH)
                    return SnapshotSaveResult(status=SnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL)

                session.add(
                    CvDocxValidatedSnapshotRow(
                        snapshot_fingerprint=snapshot.snapshot_fingerprint,
                        owner_key_fingerprint=snapshot.owner_key.owner_key_fingerprint,
                        proposal_artifact_fingerprint=snapshot.proposal_artifact_fingerprint,
                        proposal_revision=snapshot.proposal_revision,
                        blob_sha256=raw_sha256,
                        exact_docx_sha256=snapshot.exact_docx_sha256,
                        approved_cv_content_fingerprint=snapshot.approved_cv_content_fingerprint,
                        content_plan_fingerprint=snapshot.content_plan_fingerprint,
                        template_fingerprint=snapshot.template_fingerprint,
                        validation_policy_version=snapshot.validation_policy_version,
                        manual_confirmation_fingerprint=snapshot.manual_confirmation_fingerprint,
                        provenance_mode=snapshot.provenance_mode.value,
                        artifact_storage_status=DocumentArtifactStorageStatus.ACTIVE.value,
                    )
                )
                session.flush()

        return SnapshotSaveResult(status=SnapshotSaveStatus.SAVED)

    def retrieve_snapshot(
        self, exact_docx_sha256: str
    ) -> tuple[ValidatedCvDocxSnapshot, bytes] | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(CvDocxValidatedSnapshotRow)
                    .where(CvDocxValidatedSnapshotRow.exact_docx_sha256 == exact_docx_sha256)
                    .order_by(
                        CvDocxValidatedSnapshotRow.created_at.desc(),
                        CvDocxValidatedSnapshotRow.snapshot_fingerprint.desc(),
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return None
            read_result = self._blob_store.read_blob(row.blob_sha256)
            if not read_result.success:
                return None
            owner_key = self._require_owner_key(session, row.owner_key_fingerprint)
            snapshot = self._row_to_domain_snapshot(row, owner_key)
            assert read_result.data is not None
            return snapshot, read_result.data
