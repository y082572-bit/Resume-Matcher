"""Explicit Provenance Stage P6-B3b-B: the ``generate_final_confirmed_pdf``
composition root -- final confirmed PDF orchestration, replay, and CAS
promotion.

This module never imports SQLAlchemy, never opens a ``Session``/
transaction, and never imports ``CvDocumentBlobStore``/
``CvDocumentStorageError`` -- every technical/lineage failure the source
reader, converter, or repository can report is already a closed status, and
this module's only job is to sequence those three collaborators and map
their closed statuses onto ``FinalConfirmedPdfGenerationStatus``, exactly
once each, in the fixed order documented on ``generate_final_confirmed_pdf``
itself.

The public caller never supplies DOCX bytes, PDF bytes, a runtime identity,
or any fingerprint -- every one of those is independently read/recomputed
from ``owner_key``/``final_snapshot_fingerprint``/
``conversion_policy_version`` and the three injected collaborators.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfLineageKind,
    FinalConfirmedPdfArtifact,
    build_final_confirmed_pdf_artifact,
    compute_final_confirmed_pdf_request_fingerprint,
)
from app.schemas.cv_document_final_confirmed_pdf_orchestration import (
    FinalConfirmedPdfGenerationResult,
    FinalConfirmedPdfGenerationStatus,
)
from app.schemas.cv_document_final_confirmed_pdf_source import FinalDocxSnapshotPdfSourceReadStatus
from app.schemas.cv_document_pdf_conversion import DocxToPdfConversionStatus
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfAuthorityCasStatus,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfCanonicalReadStatus,
    FinalConfirmedPdfRepository,
    FinalConfirmedPdfRetrievalStatus,
    FinalConfirmedPdfSaveStatus,
)
from app.services.cv_document_final_docx_snapshot_pdf_source_reader import FinalDocxSnapshotPdfSourceReader
from app.services.cv_document_pdf_converter_protocol import DocxToPdfConverter


_Status = FinalConfirmedPdfGenerationStatus

#: Step 1 -- total mapping of every FinalDocxSnapshotPdfSourceReadStatus
#: other than VERIFIED.
_SOURCE_STATUS_MAP: dict[FinalDocxSnapshotPdfSourceReadStatus, _Status] = {
    FinalDocxSnapshotPdfSourceReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH: _Status.OWNER_KEY_FINGERPRINT_MISMATCH,
    FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_NOT_FOUND: _Status.FINAL_SNAPSHOT_NOT_FOUND,
    FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_OWNER_MISMATCH: _Status.FINAL_SNAPSHOT_OWNER_MISMATCH,
    FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_BYTES_NOT_FOUND: _Status.FINAL_DOCX_BYTES_NOT_FOUND,
    FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_HASH_MISMATCH: _Status.FINAL_DOCX_HASH_MISMATCH,
    FinalDocxSnapshotPdfSourceReadStatus.STORAGE_UNAVAILABLE: _Status.STORAGE_UNAVAILABLE,
}

#: Steps 6/11 -- total mapping of every FinalConfirmedPdfRetrievalStatus
#: other than OK.
_RETRIEVAL_STATUS_MAP: dict[FinalConfirmedPdfRetrievalStatus, _Status] = {
    FinalConfirmedPdfRetrievalStatus.ARTIFACT_NOT_FOUND: _Status.PDF_ARTIFACT_NOT_FOUND,
    FinalConfirmedPdfRetrievalStatus.BLOB_MISSING: _Status.PDF_BLOB_MISSING,
    FinalConfirmedPdfRetrievalStatus.BLOB_HASH_MISMATCH: _Status.PDF_BLOB_HASH_MISMATCH,
    FinalConfirmedPdfRetrievalStatus.BLOB_SIZE_MISMATCH: _Status.PDF_BLOB_SIZE_MISMATCH,
    FinalConfirmedPdfRetrievalStatus.BLOB_MEDIA_TYPE_MISMATCH: _Status.PDF_BLOB_MEDIA_TYPE_MISMATCH,
    FinalConfirmedPdfRetrievalStatus.STORAGE_METADATA_CONFLICT: _Status.STORAGE_METADATA_CONFLICT,
    FinalConfirmedPdfRetrievalStatus.STORAGE_UNAVAILABLE: _Status.STORAGE_UNAVAILABLE,
}

#: Step 7 -- total, name-preserving mapping of every DocxToPdfConversionStatus
#: other than SUCCEEDED.
_CONVERTER_STATUS_MAP: dict[DocxToPdfConversionStatus, _Status] = {
    DocxToPdfConversionStatus.INVALID_REQUEST: _Status.INVALID_REQUEST,
    DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE: _Status.CONVERTER_UNAVAILABLE,
    DocxToPdfConversionStatus.PROCESS_START_FAILED: _Status.PROCESS_START_FAILED,
    DocxToPdfConversionStatus.PROCESS_OUTPUT_LIMIT_EXCEEDED: _Status.PROCESS_OUTPUT_LIMIT_EXCEEDED,
    DocxToPdfConversionStatus.CONVERSION_TIMEOUT: _Status.CONVERSION_TIMEOUT,
    DocxToPdfConversionStatus.PROCESS_TERMINATION_FAILED: _Status.PROCESS_TERMINATION_FAILED,
    DocxToPdfConversionStatus.CONVERSION_FAILED: _Status.CONVERSION_FAILED,
    DocxToPdfConversionStatus.PDF_OUTPUT_MISSING: _Status.PDF_OUTPUT_MISSING,
    DocxToPdfConversionStatus.PDF_OUTPUT_LOCKED: _Status.PDF_OUTPUT_LOCKED,
    DocxToPdfConversionStatus.PDF_OUTPUT_UNREADABLE: _Status.PDF_OUTPUT_UNREADABLE,
    DocxToPdfConversionStatus.PDF_OUTPUT_CHANGED_DURING_READ: _Status.PDF_OUTPUT_CHANGED_DURING_READ,
    DocxToPdfConversionStatus.PDF_OUTPUT_TOO_LARGE: _Status.PDF_OUTPUT_TOO_LARGE,
    DocxToPdfConversionStatus.PDF_OUTPUT_INVALID: _Status.PDF_OUTPUT_INVALID,
    DocxToPdfConversionStatus.WORKSPACE_SETUP_FAILED: _Status.WORKSPACE_SETUP_FAILED,
    DocxToPdfConversionStatus.WORKSPACE_CLEANUP_FAILED: _Status.WORKSPACE_CLEANUP_FAILED,
    DocxToPdfConversionStatus.CONVERTER_CONTRACT_VIOLATION: _Status.CONVERTER_CONTRACT_VIOLATION,
}

#: Step 10 -- total mapping of every failure FinalConfirmedPdfSaveStatus.
_SAVE_FAILURE_STATUS_MAP: dict[FinalConfirmedPdfSaveStatus, _Status] = {
    FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND: _Status.OWNER_NOT_FOUND,
    FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND: _Status.SOURCE_FINAL_SNAPSHOT_NOT_FOUND,
    FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH: _Status.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH,
    FinalConfirmedPdfSaveStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH: _Status.SOURCE_FINAL_DOCX_HASH_MISMATCH,
    FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT: _Status.STORAGE_METADATA_CONFLICT,
    FinalConfirmedPdfSaveStatus.STORAGE_UNAVAILABLE: _Status.STORAGE_UNAVAILABLE,
}

#: Step 12 -- total mapping of every failure FinalConfirmedPdfAuthorityCasStatus
#: (PROMOTED/STALE_REVISION are handled separately, never via this map).
_CAS_FAILURE_STATUS_MAP: dict[FinalConfirmedPdfAuthorityCasStatus, _Status] = {
    FinalConfirmedPdfAuthorityCasStatus.OWNER_NOT_FOUND: _Status.OWNER_NOT_FOUND,
    FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_NOT_FOUND: _Status.STORAGE_METADATA_CONFLICT,
    FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_OWNER_MISMATCH: _Status.STORAGE_METADATA_CONFLICT,
    FinalConfirmedPdfAuthorityCasStatus.STORAGE_METADATA_CONFLICT: _Status.STORAGE_METADATA_CONFLICT,
    FinalConfirmedPdfAuthorityCasStatus.STORAGE_UNAVAILABLE: _Status.STORAGE_UNAVAILABLE,
}


def _failure(
    status: _Status,
    *,
    replay_used: bool = False,
    canonical_artifact_reused: bool = False,
    conversion_performed: bool = False,
) -> FinalConfirmedPdfGenerationResult:
    return FinalConfirmedPdfGenerationResult(
        status=status,
        replay_used=replay_used,
        canonical_artifact_reused=canonical_artifact_reused,
        conversion_performed=conversion_performed,
    )


def generate_final_confirmed_pdf(
    *,
    owner_key: JobArtifactOwnerKey,
    final_snapshot_fingerprint: str,
    conversion_policy_version: str,
    source_reader: FinalDocxSnapshotPdfSourceReader,
    converter: DocxToPdfConverter,
    repository: FinalConfirmedPdfRepository,
) -> FinalConfirmedPdfGenerationResult:
    """Final Confirmed PDF orchestration: ``FinalDocxSnapshot`` -> verified
    exact DOCX bytes -> runtime identity -> replay lookup -> optional
    conversion -> canonical ``FinalConfirmedPdfArtifact`` -> exact canonical
    PDF bytes -> CAS current authority -> closed result.

    Fixed step order, exactly once each: (1) source verification, (2)
    runtime identity, (3) request fingerprint, (4) capture authority, (5)
    replay lookup, (6) optional conversion, (7) converter validation, (8)
    artifact build, (9) artifact save, (10) canonical resolution, (11)
    canonical bytes retrieval when required, (12) one CAS call, (13)
    fresh-state classification after STALE_REVISION, (14) result. Never
    opens a SQL transaction around the converter subprocess -- every
    collaborator call here is a single closed-Protocol call.
    """

    # -- 1. source verification -------------------------------------------------
    source_result = source_reader.read_source(
        owner_key=owner_key, final_snapshot_fingerprint=final_snapshot_fingerprint
    )
    if source_result.status != FinalDocxSnapshotPdfSourceReadStatus.VERIFIED:
        return _failure(_SOURCE_STATUS_MAP[source_result.status])

    snapshot = source_result.snapshot
    docx_bytes = source_result.final_docx_bytes
    assert snapshot is not None and docx_bytes is not None

    # -- 2. runtime identity ---------------------------------------------------
    try:
        identity = converter.runtime_identity
    except Exception:
        return _failure(_Status.CONVERTER_CONTRACT_VIOLATION)

    if identity is None:
        return _failure(_Status.CONVERTER_UNAVAILABLE)
    if not isinstance(identity, ConverterRuntimeIdentity):
        return _failure(_Status.CONVERTER_CONTRACT_VIOLATION)

    # -- 3. request fingerprint --------------------------------------------------
    request_fingerprint = compute_final_confirmed_pdf_request_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        conversion_policy_version=conversion_policy_version,
    )

    # -- 4. capture authority ------------------------------------------------------
    authority_read = repository.read_current_authority(owner_key)
    if authority_read.status == FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT:
        return _failure(_Status.STORAGE_METADATA_CONFLICT)
    if authority_read.status == FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE:
        return _failure(_Status.STORAGE_UNAVAILABLE)
    captured_observation = authority_read.observation
    assert captured_observation is not None

    # -- 5. replay lookup --------------------------------------------------------
    replay_result = repository.read_canonical_artifact_by_conversion_request_fingerprint(request_fingerprint)
    if replay_result.status == FinalConfirmedPdfCanonicalReadStatus.STORAGE_METADATA_CONFLICT:
        return _failure(_Status.STORAGE_METADATA_CONFLICT)
    if replay_result.status == FinalConfirmedPdfCanonicalReadStatus.STORAGE_UNAVAILABLE:
        return _failure(_Status.STORAGE_UNAVAILABLE)

    replay_used = False
    canonical_artifact_reused = False
    conversion_performed = False
    target_artifact: FinalConfirmedPdfArtifact
    pdf_bytes: bytes

    if replay_result.status == FinalConfirmedPdfCanonicalReadStatus.FOUND:
        assert replay_result.artifact is not None
        replay_used = True
        canonical_artifact_reused = True
        target_artifact = replay_result.artifact

        # -- 6. replay retrieval -------------------------------------------------
        retrieval = repository.retrieve_pdf_bytes_by_artifact(target_artifact.artifact_fingerprint)
        if retrieval.status != FinalConfirmedPdfRetrievalStatus.OK:
            return _failure(
                _RETRIEVAL_STATUS_MAP[retrieval.status],
                replay_used=replay_used,
                canonical_artifact_reused=canonical_artifact_reused,
            )
        assert retrieval.pdf_bytes is not None
        pdf_bytes = retrieval.pdf_bytes
    else:
        # -- 7. real conversion ----------------------------------------------------
        conversion_performed = True
        conversion_result = converter.convert_docx_to_pdf(
            docx_bytes=docx_bytes,
            source_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
            source_docx_sha256=snapshot.final_docx_sha256,
            conversion_policy_version=conversion_policy_version,
        )
        if conversion_result.status != DocxToPdfConversionStatus.SUCCEEDED:
            return _failure(
                _CONVERTER_STATUS_MAP[conversion_result.status], conversion_performed=conversion_performed
            )

        # -- 8. converter success validation ----------------------------------------
        if conversion_result.runtime_identity is None or conversion_result.runtime_identity != identity:
            return _failure(_Status.CONVERTER_CONTRACT_VIOLATION, conversion_performed=conversion_performed)
        if not conversion_result.pdf_bytes or conversion_result.pdf_sha256 is None:
            return _failure(_Status.CONVERTER_CONTRACT_VIOLATION, conversion_performed=conversion_performed)
        if hashlib.sha256(conversion_result.pdf_bytes).hexdigest() != conversion_result.pdf_sha256:
            return _failure(_Status.CONVERTER_CONTRACT_VIOLATION, conversion_performed=conversion_performed)
        if conversion_result.conversion_policy_version != conversion_policy_version:
            return _failure(_Status.CONVERTER_CONTRACT_VIOLATION, conversion_performed=conversion_performed)

        # -- 9. artifact build --------------------------------------------------------
        candidate_artifact = build_final_confirmed_pdf_artifact(
            owner_key=owner_key,
            source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
            source_final_docx_sha256=snapshot.final_docx_sha256,
            runtime_identity=identity,
            conversion_policy_version=conversion_policy_version,
            pdf_sha256=conversion_result.pdf_sha256,
        )
        if candidate_artifact.conversion_request_fingerprint != request_fingerprint:
            return _failure(_Status.CONVERTER_CONTRACT_VIOLATION, conversion_performed=conversion_performed)

        # -- 10. artifact save ----------------------------------------------------------
        save_result = repository.save_artifact(candidate_artifact, conversion_result.pdf_bytes)
        if save_result.status == FinalConfirmedPdfSaveStatus.SAVED:
            assert save_result.artifact is not None
            target_artifact = save_result.artifact
            canonical_artifact_reused = False
            pdf_bytes = conversion_result.pdf_bytes
        elif save_result.status in (
            FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL,
            FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL,
        ):
            assert save_result.artifact is not None
            target_artifact = save_result.artifact
            canonical_artifact_reused = True

            # -- 11. canonical bytes retrieval after a save race -------------------
            retrieval = repository.retrieve_pdf_bytes_by_artifact(target_artifact.artifact_fingerprint)
            if retrieval.status != FinalConfirmedPdfRetrievalStatus.OK:
                return _failure(
                    _RETRIEVAL_STATUS_MAP[retrieval.status], conversion_performed=conversion_performed
                )
            assert retrieval.pdf_bytes is not None
            pdf_bytes = retrieval.pdf_bytes
        else:
            return _failure(
                _SAVE_FAILURE_STATUS_MAP[save_result.status], conversion_performed=conversion_performed
            )

    # -- captured authority already current: no CAS write --------------------------
    if (
        captured_observation.exists
        and captured_observation.lineage_kind == ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT
        and captured_observation.current_artifact_fingerprint == target_artifact.artifact_fingerprint
    ):
        return FinalConfirmedPdfGenerationResult(
            status=_Status.ALREADY_CURRENT,
            artifact=target_artifact,
            pdf_bytes=pdf_bytes,
            source_final_snapshot=snapshot,
            replay_used=replay_used,
            canonical_artifact_reused=canonical_artifact_reused,
            conversion_performed=conversion_performed,
            current_authority_promoted=False,
            final_authority_observation=captured_observation,
        )

    # -- 12. one CAS call -----------------------------------------------------------
    cas_result = repository.promote_current_authority(
        owner_key, target_artifact.artifact_fingerprint, captured_observation
    )

    if cas_result.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED:
        assert cas_result.current_observation is not None
        status = (
            _Status.REPLAYED_AND_PROMOTED if canonical_artifact_reused else _Status.CONFIRMED_PDF_CREATED
        )
        return FinalConfirmedPdfGenerationResult(
            status=status,
            artifact=target_artifact,
            pdf_bytes=pdf_bytes,
            source_final_snapshot=snapshot,
            replay_used=replay_used,
            canonical_artifact_reused=canonical_artifact_reused,
            conversion_performed=conversion_performed,
            current_authority_promoted=True,
            final_authority_observation=cas_result.current_observation,
        )

    if cas_result.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION:
        # -- 13. fresh-state classification after STALE_REVISION --------------------
        fresh_observation = cas_result.current_observation
        assert fresh_observation is not None
        already_current = (
            fresh_observation.exists
            and fresh_observation.lineage_kind == ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT
            and fresh_observation.current_artifact_fingerprint == target_artifact.artifact_fingerprint
        )
        return FinalConfirmedPdfGenerationResult(
            status=_Status.ALREADY_CURRENT if already_current else _Status.CURRENT_PDF_SLOT_CONFLICT,
            artifact=target_artifact,
            pdf_bytes=pdf_bytes,
            source_final_snapshot=snapshot,
            replay_used=replay_used,
            canonical_artifact_reused=canonical_artifact_reused,
            conversion_performed=conversion_performed,
            current_authority_promoted=False,
            final_authority_observation=fresh_observation,
        )

    return _failure(
        _CAS_FAILURE_STATUS_MAP[cas_result.status],
        replay_used=replay_used,
        canonical_artifact_reused=canonical_artifact_reused,
        conversion_performed=conversion_performed,
    )
