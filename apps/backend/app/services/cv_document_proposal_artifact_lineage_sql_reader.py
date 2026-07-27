"""Explicit Provenance Stage P6-B2b-B: the synchronous SQL
``ProposalArtifactLineageReader`` implementation.

``ProposalArtifactLineageSqlReader`` implements
``ProposalArtifactLineageReader`` over a synchronous SQLAlchemy ``Session``
and the shared ``CvDocumentBlobStore`` -- the very same store every other
P6-B1/P6-B2b SQL-backed component uses. It never mutates
``cv_docx_proposal_artifacts``, never touches a proposal/PDF slot, and never
constructs its own engine.

Ordering is exactly: (1) a controlled, closed precondition check on the
caller's own arguments (``INVALID_REQUEST`` on failure, no SQL/blob touched
at all); (2) ``session.get`` the Proposal artifact row by
``artifact_fingerprint`` (``NOT_FOUND`` if absent); (3) owner check; (4)
revision check; (5) generated-metadata-hash check -- only once (2)-(5) all
pass is the Proposal's own blob ever read from the blob store; (6) an exact
SHA-256 re-hash of those blob bytes against the row's own
``generated_docx_content_hash`` (``BYTES_HASH_MISMATCH`` on any divergence).
Only a row that survives every one of these checks is ever assembled into a
``ProposalArtifactLineage``.

This module never raises ``SQLAlchemyError``, ``CvDocumentStorageError``, or
a bare storage ``OSError`` to its caller -- every one of those is mapped
onto ``STORAGE_UNAVAILABLE`` instead. It never catches
``BaseException``/``KeyboardInterrupt``/``SystemExit``, and never uses a
bare ``except:``.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_models import CvDocxProposalArtifactRow
from app.services.cv_document_proposal_artifact_lineage_reader import (
    ProposalArtifactLineage,
    ProposalArtifactLineageReadResult,
    ProposalArtifactLineageStatus,
    validate_lineage_request,
)


class ProposalArtifactLineageSqlReader:
    """Synchronous SQL + content-addressed-blob implementation of
    ``ProposalArtifactLineageReader``. Accepts an already-configured sync
    session factory and the shared ``CvDocumentBlobStore`` -- never
    constructs its own engine, never runs a migration."""

    def __init__(self, session_factory: sessionmaker[Session], blob_store: CvDocumentBlobStore) -> None:
        self._session_factory = session_factory
        self._blob_store = blob_store

    def read_verified_proposal_artifact_lineage(
        self,
        *,
        artifact_fingerprint: str,
        expected_owner_key_fingerprint: str,
        expected_proposal_revision: int,
        expected_generated_docx_sha256: str,
    ) -> ProposalArtifactLineageReadResult:
        if not validate_lineage_request(
            artifact_fingerprint=artifact_fingerprint,
            expected_owner_key_fingerprint=expected_owner_key_fingerprint,
            expected_proposal_revision=expected_proposal_revision,
            expected_generated_docx_sha256=expected_generated_docx_sha256,
        ):
            return ProposalArtifactLineageReadResult(
                status=ProposalArtifactLineageStatus.INVALID_REQUEST,
                error_message="one or more lineage lookup arguments are malformed",
            )

        try:
            return self._read_verified_unsafe(
                artifact_fingerprint=artifact_fingerprint,
                expected_owner_key_fingerprint=expected_owner_key_fingerprint,
                expected_proposal_revision=expected_proposal_revision,
                expected_generated_docx_sha256=expected_generated_docx_sha256,
            )
        except SQLAlchemyError as exc:
            return ProposalArtifactLineageReadResult(
                status=ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE,
                error_message=f"a SQL error occurred while reading proposal artifact lineage: {exc}",
            )
        except CvDocumentStorageError as exc:
            return ProposalArtifactLineageReadResult(
                status=ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE,
                error_message=f"the blob store raised while reading proposal artifact lineage: {exc}",
            )
        except OSError as exc:
            return ProposalArtifactLineageReadResult(
                status=ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE,
                error_message=f"an OS-level storage error occurred while reading proposal artifact lineage: {exc}",
            )

    def _read_verified_unsafe(
        self,
        *,
        artifact_fingerprint: str,
        expected_owner_key_fingerprint: str,
        expected_proposal_revision: int,
        expected_generated_docx_sha256: str,
    ) -> ProposalArtifactLineageReadResult:
        with self._session_factory() as session:
            row = session.get(CvDocxProposalArtifactRow, artifact_fingerprint)
            if row is None:
                return ProposalArtifactLineageReadResult(
                    status=ProposalArtifactLineageStatus.NOT_FOUND,
                    error_message="no proposal artifact row for artifact_fingerprint",
                )
            if row.owner_key_fingerprint != expected_owner_key_fingerprint:
                return ProposalArtifactLineageReadResult(
                    status=ProposalArtifactLineageStatus.OWNER_MISMATCH,
                    error_message="proposal artifact row belongs to a different owner",
                )
            if row.proposal_revision != expected_proposal_revision:
                return ProposalArtifactLineageReadResult(
                    status=ProposalArtifactLineageStatus.REVISION_MISMATCH,
                    error_message="proposal artifact row has a different proposal_revision",
                )
            if row.generated_docx_content_hash != expected_generated_docx_sha256:
                return ProposalArtifactLineageReadResult(
                    status=ProposalArtifactLineageStatus.GENERATED_HASH_MISMATCH,
                    error_message="proposal artifact row has a different generated_docx_content_hash",
                )

            blob_sha256 = row.blob_sha256
            owner_key_fingerprint = row.owner_key_fingerprint
            proposal_revision = row.proposal_revision
            generated_docx_content_hash = row.generated_docx_content_hash

        # The blob is only ever read once ordering steps (2)-(5) above have
        # all passed, and only after the Session block above has exited --
        # every value used below was already captured into local variables
        # from the authoritative row while the Session was still open.
        read_result = self._blob_store.read_blob(blob_sha256)
        if not read_result.success or read_result.data is None:
            return ProposalArtifactLineageReadResult(
                status=ProposalArtifactLineageStatus.STORAGE_UNAVAILABLE,
                error_message="blob store could not read the proposal artifact's blob",
            )

        raw = bytes(read_result.data)
        if hashlib.sha256(raw).hexdigest() != generated_docx_content_hash:
            return ProposalArtifactLineageReadResult(
                status=ProposalArtifactLineageStatus.BYTES_HASH_MISMATCH,
                error_message="proposal artifact blob bytes do not match generated_docx_content_hash",
            )

        lineage = ProposalArtifactLineage(
            artifact_fingerprint=artifact_fingerprint,
            owner_key_fingerprint=owner_key_fingerprint,
            proposal_revision=proposal_revision,
            generated_docx_content_hash=generated_docx_content_hash,
            docx_bytes=raw,
        )
        return ProposalArtifactLineageReadResult(status=ProposalArtifactLineageStatus.VERIFIED, lineage=lineage)
