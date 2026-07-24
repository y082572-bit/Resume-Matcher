"""Explicit Provenance Stage P6-B1 SQL storage addendum: the synchronous
SQL ``FinalDocxSnapshotRepository`` implementation.

``FinalDocxSnapshotSqlRepository`` implements the exact P6-A
``FinalDocxSnapshotRepository`` Protocol (structurally and at runtime) over
a synchronous SQLAlchemy ``Session`` and the shared, global
``CvDocumentBlobStore`` -- the very same store ``CvDocumentArtifactSqlRepository``
already uses, so a final snapshot's ``final_docx_sha256`` blob can be
byte-identical to (and physically dedup with) a proposal's or a validated
snapshot's blob. It never constructs its own engine and never runs its own
migration.

Ordering discipline, mirroring ``cv_document_sql_repository.py``:

1. Independently recompute the owner-key fingerprint, ``final_docx_sha256``,
   ``edited_by_user``, and ``final_snapshot_fingerprint`` -- a caller's own
   ``FinalDocxSnapshot`` fields are never trusted at face value. Any
   mismatch is blocked *before* the blob is ever written to disk.
2. Publish (or reuse) the blob via ``CvDocumentBlobStore`` before opening
   any DB transaction.
3. Open one short ``Session``/transaction: ensure the owner row, verify the
   source proposal's lineage against the live ``cv_docx_proposal_artifacts``
   row, ensure the blob metadata row, check for an existing snapshot row,
   then insert.
4. A concurrent ``IntegrityError`` (two identical first-writers racing on
   the same ``final_snapshot_fingerprint`` primary key) is never resolved by
   reusing the transaction's own ``Session`` -- that ``Session`` is closed,
   and a literally new one is opened to re-read and compare.

This repository never raises ``CvDocumentStorageError``,
``sqlalchemy.exc.IntegrityError``, ``sqlalchemy.exc.OperationalError``,
``OSError``, or any other ``SQLAlchemyError`` to its caller -- every normal
SQL/storage failure is mapped onto a closed ``FinalSnapshotSaveStatus``
instead. A DB failure that happens strictly after the blob was durably
published may leave an orphan filesystem blob; this repository never
deletes it automatically (that is exclusively the reconciliation module's
read-only reporting concern).
"""

from __future__ import annotations

import hashlib

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot
from app.schemas.cv_document_storage import CvDocumentStorageErrorCode
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_final_snapshot_repository import (
    FinalSnapshotSaveResult,
    FinalSnapshotSaveStatus,
    compute_final_snapshot_fingerprint,
)
from app.services.cv_document_models import CvDocxFinalSnapshotRow, CvDocxProposalArtifactRow
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint
from app.services.cv_document_storage_shared import ensure_blob_row, ensure_owner, require_owner_key


_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FinalDocxSnapshotSqlRepository:
    """Synchronous SQL + content-addressed-blob implementation of the
    ``FinalDocxSnapshotRepository`` Protocol.

    Accepts an already-configured sync session factory and the shared
    ``CvDocumentBlobStore`` -- never constructs its own engine, never runs a
    migration, and shares the exact same blob store instance a caller also
    hands to ``CvDocumentArtifactSqlRepository``.
    """

    def __init__(
        self, session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore
    ) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store

    # -- save -----------------------------------------------------------------

    def save_final_docx_snapshot(
        self, snapshot: FinalDocxSnapshot, docx_bytes: bytes
    ) -> FinalSnapshotSaveResult:
        raw = bytes(docx_bytes)
        raw_final_docx_sha256 = hashlib.sha256(raw).hexdigest()
        if raw_final_docx_sha256 != snapshot.final_docx_sha256:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.DOCX_HASH_MISMATCH)

        recomputed_owner_key_fingerprint = compute_owner_key_fingerprint(
            person_entity_id=snapshot.owner_key.person_entity_id,
            owner_kind=snapshot.owner_key.owner_kind,
            owner_reference_id=snapshot.owner_key.owner_reference_id,
            owner_key_schema_version=snapshot.owner_key.owner_key_schema_version,
        )
        recomputed_edited_by_user = snapshot.generated_proposal_sha256 != raw_final_docx_sha256
        recomputed_fingerprint = compute_final_snapshot_fingerprint(
            owner_key_fingerprint=recomputed_owner_key_fingerprint,
            source_proposal_artifact_fingerprint=snapshot.source_proposal_artifact_fingerprint,
            source_proposal_revision=snapshot.source_proposal_revision,
            generated_proposal_sha256=snapshot.generated_proposal_sha256,
            final_docx_sha256=raw_final_docx_sha256,
            finalization_policy_version=snapshot.finalization_policy_version,
            snapshot_schema_version=snapshot.snapshot_schema_version,
        )
        # A mismatch here always covers a tampered/rebuilt owner_key too --
        # owner_key_fingerprint is one of compute_final_snapshot_fingerprint's
        # own inputs, so blocking on the *recomputed* value (never the
        # caller-supplied snapshot.owner_key.owner_key_fingerprint) blocks a
        # bad owner binding before the blob is ever written, satisfying the
        # "block before filesystem mutation" ordering rule.
        if (
            recomputed_fingerprint != snapshot.final_snapshot_fingerprint
            or recomputed_edited_by_user != snapshot.edited_by_user
        ):
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SNAPSHOT_FINGERPRINT_MISMATCH)

        write_result = self._blob_store.write_blob(
            raw, expected_sha256=raw_final_docx_sha256, media_type=_DOCX_MEDIA_TYPE
        )
        if not write_result.success:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.STORAGE_UNAVAILABLE)

        needs_reread = False
        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        result = self._insert_snapshot_transaction(
                            session,
                            snapshot,
                            final_docx_sha256=raw_final_docx_sha256,
                            byte_size=len(raw),
                            owner_key_fingerprint=recomputed_owner_key_fingerprint,
                        )
                    return result
                except IntegrityError:
                    needs_reread = True
                except CvDocumentStorageError as exc:
                    if exc.result.error_code == CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT:
                        return FinalSnapshotSaveResult(
                            status=FinalSnapshotSaveStatus.STORAGE_METADATA_CONFLICT
                        )
                    return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.STORAGE_UNAVAILABLE)
        except OperationalError:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.STORAGE_UNAVAILABLE)

        assert needs_reread
        # The Session that raised IntegrityError is already closed (the
        # `with self._session_factory() as session:` block above has fully
        # exited) -- the reread below always opens a brand-new one.
        return self._reread_after_integrity_error(snapshot, raw_final_docx_sha256)

    def _insert_snapshot_transaction(
        self,
        session: Session,
        snapshot: FinalDocxSnapshot,
        *,
        final_docx_sha256: str,
        byte_size: int,
        owner_key_fingerprint: str,
    ) -> FinalSnapshotSaveResult:
        ensure_owner(session, snapshot.owner_key)

        proposal_row = session.get(
            CvDocxProposalArtifactRow, snapshot.source_proposal_artifact_fingerprint
        )
        if proposal_row is None:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SOURCE_PROPOSAL_NOT_FOUND)
        if proposal_row.owner_key_fingerprint != owner_key_fingerprint:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SOURCE_PROPOSAL_OWNER_MISMATCH)
        if proposal_row.proposal_revision != snapshot.source_proposal_revision:
            return FinalSnapshotSaveResult(
                status=FinalSnapshotSaveStatus.SOURCE_PROPOSAL_REVISION_MISMATCH
            )
        if proposal_row.generated_docx_content_hash != snapshot.generated_proposal_sha256:
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SOURCE_PROPOSAL_HASH_MISMATCH)

        ensure_blob_row(
            session,
            self._blob_store,
            blob_sha256=final_docx_sha256,
            byte_size=byte_size,
            media_type=_DOCX_MEDIA_TYPE,
        )

        existing = session.get(CvDocxFinalSnapshotRow, snapshot.final_snapshot_fingerprint)
        if existing is not None:
            if self._row_matches_snapshot(existing, snapshot, final_docx_sha256):
                return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL)
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SNAPSHOT_METADATA_CONFLICT)

        session.add(
            CvDocxFinalSnapshotRow(
                final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
                owner_key_fingerprint=owner_key_fingerprint,
                source_proposal_artifact_fingerprint=snapshot.source_proposal_artifact_fingerprint,
                source_proposal_revision=snapshot.source_proposal_revision,
                generated_proposal_sha256=snapshot.generated_proposal_sha256,
                final_docx_sha256=final_docx_sha256,
                edited_by_user=1 if snapshot.edited_by_user else 0,
                finalization_policy_version=snapshot.finalization_policy_version,
                snapshot_schema_version=snapshot.snapshot_schema_version,
            )
        )
        session.flush()
        return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SAVED)

    def _row_matches_snapshot(
        self, row: CvDocxFinalSnapshotRow, snapshot: FinalDocxSnapshot, final_docx_sha256: str
    ) -> bool:
        return (
            row.owner_key_fingerprint == snapshot.owner_key.owner_key_fingerprint
            and row.source_proposal_artifact_fingerprint == snapshot.source_proposal_artifact_fingerprint
            and row.source_proposal_revision == snapshot.source_proposal_revision
            and row.generated_proposal_sha256 == snapshot.generated_proposal_sha256
            and row.final_docx_sha256 == final_docx_sha256
            and bool(row.edited_by_user) == snapshot.edited_by_user
            and row.finalization_policy_version == snapshot.finalization_policy_version
            and row.snapshot_schema_version == snapshot.snapshot_schema_version
        )

    def _reread_after_integrity_error(
        self, snapshot: FinalDocxSnapshot, final_docx_sha256: str
    ) -> FinalSnapshotSaveResult:
        with self._session_factory() as fresh_session:
            row = fresh_session.get(CvDocxFinalSnapshotRow, snapshot.final_snapshot_fingerprint)
            if row is None or not self._row_matches_snapshot(row, snapshot, final_docx_sha256):
                return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.SNAPSHOT_METADATA_CONFLICT)
            return FinalSnapshotSaveResult(status=FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL)

    # -- read -------------------------------------------------------------------

    def _row_to_domain(
        self, row: CvDocxFinalSnapshotRow, owner_key: JobArtifactOwnerKey
    ) -> FinalDocxSnapshot:
        return FinalDocxSnapshot(
            owner_key=owner_key,
            source_proposal_artifact_fingerprint=row.source_proposal_artifact_fingerprint,
            source_proposal_revision=row.source_proposal_revision,
            generated_proposal_sha256=row.generated_proposal_sha256,
            final_docx_sha256=row.final_docx_sha256,
            edited_by_user=bool(row.edited_by_user),
            finalization_policy_version=row.finalization_policy_version,
            snapshot_schema_version=row.snapshot_schema_version,
            final_snapshot_fingerprint=row.final_snapshot_fingerprint,
        )

    def get_final_docx_snapshot(self, final_snapshot_fingerprint: str) -> FinalDocxSnapshot | None:
        with self._session_factory() as session:
            row = session.get(CvDocxFinalSnapshotRow, final_snapshot_fingerprint)
            if row is None:
                return None
            owner_key = require_owner_key(session, row.owner_key_fingerprint)
            return self._row_to_domain(row, owner_key)

    def retrieve_final_docx_bytes(self, final_docx_sha256: str) -> bytes | None:
        try:
            read_result = self._blob_store.read_blob(final_docx_sha256)
        except CvDocumentStorageError:
            return None
        if not read_result.success:
            return None
        return read_result.data
