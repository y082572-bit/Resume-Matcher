"""Explicit Provenance Stage P6-B3b-A: the synchronous SQL
``FinalConfirmedPdfRepository`` implementation.

Ordering discipline, mirroring ``cv_document_sql_repository.py`` and
``cv_document_final_snapshot_sql_repository.py``:

1. Independently recompute the owner-key fingerprint and the raw PDF bytes
   hash before ever trusting a caller-supplied field.
2. Write (or reuse) the PDF blob via the shared ``CvDocumentBlobStore``
   *before* opening the short artifact-insert transaction.
3. Open one ``Session``/transaction per mutating call. A race between two
   concurrent first-writers on the globally ``UNIQUE``
   ``conversion_request_fingerprint`` (or, less likely, on the
   content-addressed ``artifact_fingerprint`` primary key) is resolved by
   closing the losing transaction's ``Session`` and opening a brand-new one
   to re-read and report the exact winning row -- never by reusing the
   raised-on ``Session`` and never by retrying the write.
4. This repository never raises ``CvDocumentStorageError``,
   ``sqlalchemy.exc.SQLAlchemyError``, or a raw Pydantic
   ``ValidationError`` to its own caller -- every technical failure is
   mapped onto one of this module's closed result statuses instead.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    FinalConfirmedPdfArtifact,
    compute_canonical_runtime_identity_json,
)
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity
from app.schemas.cv_document_storage import CvDocumentStorageErrorCode
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadResult,
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityCasResult,
    FinalConfirmedPdfAuthorityCasStatus,
    FinalConfirmedPdfAuthorityReadResult,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfCanonicalReadResult,
    FinalConfirmedPdfCanonicalReadStatus,
    FinalConfirmedPdfRetrievalResult,
    FinalConfirmedPdfRetrievalStatus,
    FinalConfirmedPdfSaveResult,
    FinalConfirmedPdfSaveStatus,
)
from app.services.cv_document_models import (
    CvConfirmedPdfCurrentAuthorityRow,
    CvDocumentArtifactOwner,
    CvDocumentBlob,
    CvDocxFinalSnapshotRow,
    CvFinalConfirmedPdfArtifactRow,
)
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint
from app.services.cv_document_storage_shared import ensure_blob_row, row_to_owner_key


_PDF_MEDIA_TYPE = "application/pdf"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _MetadataConflict(Exception):
    """Internal-only: a stored row failed re-verification (a tampered
    ``runtime_identity_json``, a fingerprint that no longer recomputes).
    Always caught within the same method and mapped onto
    ``STORAGE_METADATA_CONFLICT`` -- never surfaced to a caller."""


class _ConcurrentSlotMutation(Exception):
    """Internal-only: the authority row's optimistic ``slot_version`` no
    longer matched at UPDATE time. Always caught within the same method and
    converted into ``FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION``."""


class FinalConfirmedPdfSqlRepository:
    """Synchronous SQL + content-addressed-blob implementation of the
    ``FinalConfirmedPdfRepository`` Protocol. Never installs its own tables
    or triggers -- see ``cv_document_final_confirmed_pdf_cutover_state.py``
    for that; this class assumes the cutover is already ``READY``."""

    def __init__(
        self, session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store

    # -- domain reconstruction (fail-closed) ----------------------------------

    def _row_to_domain_artifact(
        self, row: CvFinalConfirmedPdfArtifactRow, owner_key: JobArtifactOwnerKey
    ) -> FinalConfirmedPdfArtifact:
        try:
            runtime_identity = ConverterRuntimeIdentity.model_validate_json(row.runtime_identity_json)
        except ValidationError as exc:
            raise _MetadataConflict("stored runtime_identity_json failed to parse") from exc

        recanonicalized = compute_canonical_runtime_identity_json(runtime_identity)
        if recanonicalized != row.runtime_identity_json:
            raise _MetadataConflict(
                "stored runtime_identity_json is not the canonical serialization of its own parsed value"
            )
        if runtime_identity.runtime_identity_fingerprint != row.runtime_identity_fingerprint:
            raise _MetadataConflict(
                "reconstructed runtime_identity_fingerprint does not match the stored row"
            )

        try:
            return FinalConfirmedPdfArtifact(
                artifact_fingerprint=row.artifact_fingerprint,
                owner_key=owner_key,
                source_final_snapshot_fingerprint=row.source_final_snapshot_fingerprint,
                source_final_docx_sha256=row.source_final_docx_sha256,
                conversion_request_fingerprint=row.conversion_request_fingerprint,
                runtime_identity=runtime_identity,
                runtime_identity_fingerprint=row.runtime_identity_fingerprint,
                conversion_policy_version=row.conversion_policy_version,
                pdf_sha256=row.pdf_sha256,
                conversion_request_fingerprint_schema_version=(
                    row.conversion_request_fingerprint_schema_version
                ),
                artifact_fingerprint_schema_version=row.artifact_fingerprint_schema_version,
            )
        except ValidationError as exc:
            raise _MetadataConflict("stored artifact row failed domain reconstruction") from exc

    def _row_to_domain_observation(
        self, row: CvConfirmedPdfCurrentAuthorityRow
    ) -> ConfirmedPdfCurrentAuthorityObservation:
        try:
            return ConfirmedPdfCurrentAuthorityObservation(
                exists=True,
                lineage_kind=ConfirmedPdfLineageKind(row.lineage_kind),
                current_artifact_fingerprint=row.current_artifact_fingerprint,
                slot_version=row.slot_version,
            )
        except (ValueError, ValidationError) as exc:
            raise _MetadataConflict("stored authority row failed domain reconstruction") from exc

    def _require_owner_key(self, session: Session, owner_key_fingerprint: str) -> JobArtifactOwnerKey:
        owner_row = session.get(CvDocumentArtifactOwner, owner_key_fingerprint)
        if owner_row is None:
            raise _MetadataConflict(f"no owner row for owner_key_fingerprint={owner_key_fingerprint}")
        return row_to_owner_key(owner_row)

    # -- reads: current authority ----------------------------------------------

    def read_current_authority(
        self, owner_key: JobArtifactOwnerKey
    ) -> FinalConfirmedPdfAuthorityReadResult:
        try:
            with self._session_factory() as session:
                row = session.get(CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
                if row is None:
                    return FinalConfirmedPdfAuthorityReadResult(
                        status=FinalConfirmedPdfAuthorityReadStatus.ABSENT,
                        observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
                    )
                observation = self._row_to_domain_observation(row)
                return FinalConfirmedPdfAuthorityReadResult(
                    status=FinalConfirmedPdfAuthorityReadStatus.FOUND, observation=observation
                )
        except _MetadataConflict:
            return FinalConfirmedPdfAuthorityReadResult(
                status=FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT
            )
        except OperationalError:
            return FinalConfirmedPdfAuthorityReadResult(
                status=FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE
            )

    # -- reads: single artifact --------------------------------------------------

    def read_artifact(self, artifact_fingerprint: str) -> FinalConfirmedPdfArtifactReadResult:
        try:
            with self._session_factory() as session:
                row = session.get(CvFinalConfirmedPdfArtifactRow, artifact_fingerprint)
                if row is None:
                    return FinalConfirmedPdfArtifactReadResult(status=FinalConfirmedPdfArtifactReadStatus.NOT_FOUND)
                owner_key = self._require_owner_key(session, row.owner_key_fingerprint)
                artifact = self._row_to_domain_artifact(row, owner_key)
                return FinalConfirmedPdfArtifactReadResult(
                    status=FinalConfirmedPdfArtifactReadStatus.FOUND, artifact=artifact
                )
        except _MetadataConflict:
            return FinalConfirmedPdfArtifactReadResult(
                status=FinalConfirmedPdfArtifactReadStatus.STORAGE_METADATA_CONFLICT
            )
        except OperationalError:
            return FinalConfirmedPdfArtifactReadResult(status=FinalConfirmedPdfArtifactReadStatus.STORAGE_UNAVAILABLE)

    # -- reads: canonical by conversion_request_fingerprint -----------------------

    def read_canonical_artifact_by_conversion_request_fingerprint(
        self, conversion_request_fingerprint: str
    ) -> FinalConfirmedPdfCanonicalReadResult:
        try:
            with self._session_factory() as session:
                row = (
                    session.execute(
                        select(CvFinalConfirmedPdfArtifactRow).where(
                            CvFinalConfirmedPdfArtifactRow.conversion_request_fingerprint
                            == conversion_request_fingerprint
                        )
                    )
                    .scalars()
                    .first()
                )
                if row is None:
                    return FinalConfirmedPdfCanonicalReadResult(status=FinalConfirmedPdfCanonicalReadStatus.NOT_FOUND)
                owner_key = self._require_owner_key(session, row.owner_key_fingerprint)
                artifact = self._row_to_domain_artifact(row, owner_key)
                return FinalConfirmedPdfCanonicalReadResult(
                    status=FinalConfirmedPdfCanonicalReadStatus.FOUND, artifact=artifact
                )
        except _MetadataConflict:
            return FinalConfirmedPdfCanonicalReadResult(
                status=FinalConfirmedPdfCanonicalReadStatus.STORAGE_METADATA_CONFLICT
            )
        except OperationalError:
            return FinalConfirmedPdfCanonicalReadResult(status=FinalConfirmedPdfCanonicalReadStatus.STORAGE_UNAVAILABLE)

    # -- retrieve pdf bytes by artifact -------------------------------------------

    def retrieve_pdf_bytes_by_artifact(self, artifact_fingerprint: str) -> FinalConfirmedPdfRetrievalResult:
        try:
            with self._session_factory() as session:
                row = session.get(CvFinalConfirmedPdfArtifactRow, artifact_fingerprint)
                if row is None:
                    return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.ARTIFACT_NOT_FOUND)
                owner_key = self._require_owner_key(session, row.owner_key_fingerprint)
                artifact = self._row_to_domain_artifact(row, owner_key)

                blob_meta = session.get(CvDocumentBlob, row.blob_sha256)
                if blob_meta is None:
                    return FinalConfirmedPdfRetrievalResult(
                        status=FinalConfirmedPdfRetrievalStatus.STORAGE_METADATA_CONFLICT
                    )
                if blob_meta.media_type != _PDF_MEDIA_TYPE:
                    return FinalConfirmedPdfRetrievalResult(
                        status=FinalConfirmedPdfRetrievalStatus.BLOB_MEDIA_TYPE_MISMATCH
                    )

                read_result = self._blob_store.read_blob(row.blob_sha256)
                if not read_result.success:
                    if read_result.error_code == CvDocumentStorageErrorCode.FINAL_BLOB_MISSING:
                        return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.BLOB_MISSING)
                    if read_result.error_code == CvDocumentStorageErrorCode.FINAL_BLOB_HASH_MISMATCH:
                        return FinalConfirmedPdfRetrievalResult(
                            status=FinalConfirmedPdfRetrievalStatus.BLOB_HASH_MISMATCH
                        )
                    return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.STORAGE_UNAVAILABLE)

                assert read_result.data is not None
                if len(read_result.data) != blob_meta.byte_size:
                    return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.BLOB_SIZE_MISMATCH)

                computed_sha256 = hashlib.sha256(read_result.data).hexdigest()
                if computed_sha256 != row.pdf_sha256 or computed_sha256 != row.blob_sha256:
                    return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.BLOB_HASH_MISMATCH)

                return FinalConfirmedPdfRetrievalResult(
                    status=FinalConfirmedPdfRetrievalStatus.OK,
                    artifact=artifact,
                    pdf_bytes=bytes(read_result.data),
                )
        except _MetadataConflict:
            return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.STORAGE_METADATA_CONFLICT)
        except CvDocumentStorageError:
            return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.STORAGE_UNAVAILABLE)
        except OperationalError:
            return FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.STORAGE_UNAVAILABLE)

    # -- save ----------------------------------------------------------------------

    def _reread_after_race(self, artifact: FinalConfirmedPdfArtifact) -> FinalConfirmedPdfSaveResult:
        """Always opens a brand-new ``Session`` -- the one that raised
        ``IntegrityError`` is already fully exited by the time this runs."""

        with self._session_factory() as session:
            row = session.get(CvFinalConfirmedPdfArtifactRow, artifact.artifact_fingerprint)
            if row is not None:
                try:
                    owner_key = self._require_owner_key(session, row.owner_key_fingerprint)
                    winner = self._row_to_domain_artifact(row, owner_key)
                except _MetadataConflict:
                    return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT)
                return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL, artifact=winner)

            row = (
                session.execute(
                    select(CvFinalConfirmedPdfArtifactRow).where(
                        CvFinalConfirmedPdfArtifactRow.conversion_request_fingerprint
                        == artifact.conversion_request_fingerprint
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT)
            try:
                owner_key = self._require_owner_key(session, row.owner_key_fingerprint)
                winner = self._row_to_domain_artifact(row, owner_key)
            except _MetadataConflict:
                return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT)
            if winner.artifact_fingerprint == artifact.artifact_fingerprint:
                return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL, artifact=winner)
            return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL, artifact=winner)

    def save_artifact(
        self, artifact: FinalConfirmedPdfArtifact, pdf_bytes: bytes
    ) -> FinalConfirmedPdfSaveResult:
        raw = bytes(pdf_bytes)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if raw_sha256 != artifact.pdf_sha256:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.ARTIFACT_BYTES_HASH_MISMATCH,
                "a fresh hash of pdf_bytes does not match artifact.pdf_sha256",
            )

        recomputed_owner_fp = compute_owner_key_fingerprint(
            person_entity_id=artifact.owner_key.person_entity_id,
            owner_kind=artifact.owner_key.owner_kind,
            owner_reference_id=artifact.owner_key.owner_reference_id,
            owner_key_schema_version=artifact.owner_key.owner_key_schema_version,
        )
        if recomputed_owner_fp != artifact.owner_key.owner_key_fingerprint:
            raise CvDocumentStorageError.of(
                CvDocumentStorageErrorCode.OWNER_IDENTITY_MISMATCH,
                "artifact.owner_key.owner_key_fingerprint does not match a recomputed fingerprint",
            )

        write_result = self._blob_store.write_blob(raw, expected_sha256=raw_sha256, media_type=_PDF_MEDIA_TYPE)
        if not write_result.success:
            return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.STORAGE_UNAVAILABLE)

        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        owner_row = session.get(CvDocumentArtifactOwner, recomputed_owner_fp)
                        if owner_row is None:
                            return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND)

                        snapshot_row = session.get(
                            CvDocxFinalSnapshotRow, artifact.source_final_snapshot_fingerprint
                        )
                        if snapshot_row is None:
                            return FinalConfirmedPdfSaveResult(
                                status=FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND
                            )
                        if snapshot_row.owner_key_fingerprint != recomputed_owner_fp:
                            return FinalConfirmedPdfSaveResult(
                                status=FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH
                            )
                        if snapshot_row.final_docx_sha256 != artifact.source_final_docx_sha256:
                            return FinalConfirmedPdfSaveResult(
                                status=FinalConfirmedPdfSaveStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH
                            )

                        try:
                            ensure_blob_row(
                                session,
                                self._blob_store,
                                blob_sha256=raw_sha256,
                                byte_size=len(raw),
                                media_type=_PDF_MEDIA_TYPE,
                            )
                        except CvDocumentStorageError as exc:
                            if exc.result.error_code == CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT:
                                return FinalConfirmedPdfSaveResult(
                                    status=FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT
                                )
                            raise

                        existing_by_fp = session.get(CvFinalConfirmedPdfArtifactRow, artifact.artifact_fingerprint)
                        if existing_by_fp is not None:
                            owner_key = self._require_owner_key(session, existing_by_fp.owner_key_fingerprint)
                            existing_artifact = self._row_to_domain_artifact(existing_by_fp, owner_key)
                            return FinalConfirmedPdfSaveResult(
                                status=FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL,
                                artifact=existing_artifact,
                            )

                        existing_by_request = (
                            session.execute(
                                select(CvFinalConfirmedPdfArtifactRow).where(
                                    CvFinalConfirmedPdfArtifactRow.conversion_request_fingerprint
                                    == artifact.conversion_request_fingerprint
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if existing_by_request is not None:
                            owner_key = self._require_owner_key(
                                session, existing_by_request.owner_key_fingerprint
                            )
                            existing_artifact = self._row_to_domain_artifact(existing_by_request, owner_key)
                            return FinalConfirmedPdfSaveResult(
                                status=FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL,
                                artifact=existing_artifact,
                            )

                        runtime_identity_json = compute_canonical_runtime_identity_json(artifact.runtime_identity)
                        session.add(
                            CvFinalConfirmedPdfArtifactRow(
                                artifact_fingerprint=artifact.artifact_fingerprint,
                                owner_key_fingerprint=recomputed_owner_fp,
                                source_final_snapshot_fingerprint=artifact.source_final_snapshot_fingerprint,
                                source_final_docx_sha256=artifact.source_final_docx_sha256,
                                conversion_request_fingerprint=artifact.conversion_request_fingerprint,
                                runtime_identity_fingerprint=artifact.runtime_identity_fingerprint,
                                runtime_identity_json=runtime_identity_json,
                                conversion_policy_version=artifact.conversion_policy_version,
                                pdf_sha256=artifact.pdf_sha256,
                                blob_sha256=raw_sha256,
                                conversion_request_fingerprint_schema_version=(
                                    artifact.conversion_request_fingerprint_schema_version
                                ),
                                artifact_fingerprint_schema_version=artifact.artifact_fingerprint_schema_version,
                            )
                        )
                        session.flush()
                except (IntegrityError, _MetadataConflict):
                    return self._reread_after_race(artifact)

                return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.SAVED, artifact=artifact.model_copy())
        except OperationalError:
            return FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.STORAGE_UNAVAILABLE)

    # -- authority CAS ---------------------------------------------------------------

    def _reread_authority_after_race(self, owner_key: JobArtifactOwnerKey) -> ConfirmedPdfCurrentAuthorityObservation:
        with self._session_factory() as fresh_session:
            row = fresh_session.get(CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
            if row is None:
                return ConfirmedPdfCurrentAuthorityObservation(exists=False)
            try:
                return self._row_to_domain_observation(row)
            except _MetadataConflict:
                return ConfirmedPdfCurrentAuthorityObservation(exists=False)

    def promote_current_authority(
        self,
        owner_key: JobArtifactOwnerKey,
        target_artifact_fingerprint: str,
        expected_observation: ConfirmedPdfCurrentAuthorityObservation,
    ) -> FinalConfirmedPdfAuthorityCasResult:
        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        owner_row = session.get(CvDocumentArtifactOwner, owner_key.owner_key_fingerprint)
                        if owner_row is None:
                            return FinalConfirmedPdfAuthorityCasResult(
                                status=FinalConfirmedPdfAuthorityCasStatus.OWNER_NOT_FOUND
                            )

                        target_row = session.get(CvFinalConfirmedPdfArtifactRow, target_artifact_fingerprint)
                        if target_row is None:
                            return FinalConfirmedPdfAuthorityCasResult(
                                status=FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_NOT_FOUND
                            )
                        if target_row.owner_key_fingerprint != owner_key.owner_key_fingerprint:
                            return FinalConfirmedPdfAuthorityCasResult(
                                status=FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_OWNER_MISMATCH
                            )

                        current_row = session.get(CvConfirmedPdfCurrentAuthorityRow, owner_key.owner_key_fingerprint)
                        current_observation = (
                            ConfirmedPdfCurrentAuthorityObservation(exists=False)
                            if current_row is None
                            else self._row_to_domain_observation(current_row)
                        )

                        if current_observation != expected_observation:
                            return FinalConfirmedPdfAuthorityCasResult(
                                status=FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION,
                                current_observation=current_observation,
                            )

                        now = _utcnow_iso()
                        if current_row is None:
                            session.add(
                                CvConfirmedPdfCurrentAuthorityRow(
                                    owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                    lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT.value,
                                    current_artifact_fingerprint=target_artifact_fingerprint,
                                    slot_version=1,
                                    updated_at=now,
                                )
                            )
                            session.flush()
                            next_slot_version = 1
                        else:
                            # Captured *before* the UPDATE executes: SQLAlchemy's
                            # ORM-enabled bulk UPDATE synchronizes already-loaded
                            # matching objects' attributes in-place immediately
                            # (synchronize_session="auto") -- re-reading
                            # current_row.slot_version *after* session.execute()
                            # below would silently observe the row's *new* value,
                            # not the expected-previous one.
                            expected_slot_version = current_row.slot_version
                            result = session.execute(
                                update(CvConfirmedPdfCurrentAuthorityRow)
                                .where(
                                    CvConfirmedPdfCurrentAuthorityRow.owner_key_fingerprint
                                    == owner_key.owner_key_fingerprint,
                                    CvConfirmedPdfCurrentAuthorityRow.slot_version == expected_slot_version,
                                )
                                .values(
                                    lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT.value,
                                    current_artifact_fingerprint=target_artifact_fingerprint,
                                    slot_version=expected_slot_version + 1,
                                    updated_at=now,
                                )
                            )
                            if result.rowcount != 1:
                                raise _ConcurrentSlotMutation()
                            next_slot_version = expected_slot_version + 1
                except (IntegrityError, _ConcurrentSlotMutation, _MetadataConflict):
                    fresh_observation = self._reread_authority_after_race(owner_key)
                    return FinalConfirmedPdfAuthorityCasResult(
                        status=FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION,
                        current_observation=fresh_observation,
                    )

                promoted = ConfirmedPdfCurrentAuthorityObservation(
                    exists=True,
                    lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                    current_artifact_fingerprint=target_artifact_fingerprint,
                    slot_version=next_slot_version,
                )
                return FinalConfirmedPdfAuthorityCasResult(
                    status=FinalConfirmedPdfAuthorityCasStatus.PROMOTED, current_observation=promoted
                )
        except OperationalError:
            return FinalConfirmedPdfAuthorityCasResult(status=FinalConfirmedPdfAuthorityCasStatus.STORAGE_UNAVAILABLE)
