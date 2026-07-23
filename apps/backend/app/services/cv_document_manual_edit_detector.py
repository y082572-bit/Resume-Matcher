"""Explicit Provenance Stage P6-A: manual-edit detection.

Raw SHA-256 bytes comparison only. Never uses mtime, filesize, filename,
path, or Word metadata as evidence -- a "current" hash is always computed
fresh from the exact bytes the caller hands in for *this* call, never
copied from a previously stored ``current_file_hash``/
``generated_docx_content_hash`` field. Any raw-byte difference (even one
caused only by ZIP metadata Word itself rewrites on open/save) is reported
as ``USER_EDITED`` -- a false positive here is an accepted, safe,
fail-closed outcome.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import ManualEditDetectionResult, ManualEditStatus


def compute_raw_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def detect_manual_edit(
    *,
    expected_sha256: str,
    observed_bytes: bytes | None,
    reread_bytes: bytes | None = None,
    freshness_verified: bool = True,
) -> ManualEditDetectionResult:
    """Compare ``expected_sha256`` (the pipeline's own generated hash)
    against the exact bytes currently observed.

    ``freshness_verified=False`` means the caller could not obtain a
    current read at all (e.g. an unreachable store) -- reported as
    ``FRESHNESS_NOT_VERIFIED``, never guessed as ``UNMODIFIED``.
    ``reread_bytes`` is an optional second read of the same document taken
    moments after ``observed_bytes``; a difference between the two reads
    itself (a TOCTOU race), independent of ``expected_sha256``, is reported
    as ``FILE_CHANGED_DURING_READ``.
    """

    if not freshness_verified:
        return ManualEditDetectionResult(
            status=ManualEditStatus.FRESHNESS_NOT_VERIFIED,
            expected_sha256=expected_sha256,
            observed_sha256=None,
        )

    if observed_bytes is None:
        return ManualEditDetectionResult(
            status=ManualEditStatus.FILE_MISSING,
            expected_sha256=expected_sha256,
            observed_sha256=None,
        )

    observed_sha256 = compute_raw_sha256(observed_bytes)

    if reread_bytes is not None and compute_raw_sha256(reread_bytes) != observed_sha256:
        return ManualEditDetectionResult(
            status=ManualEditStatus.FILE_CHANGED_DURING_READ,
            expected_sha256=expected_sha256,
            observed_sha256=None,
        )

    if observed_sha256 == expected_sha256:
        return ManualEditDetectionResult(
            status=ManualEditStatus.UNMODIFIED,
            expected_sha256=expected_sha256,
            observed_sha256=observed_sha256,
        )

    return ManualEditDetectionResult(
        status=ManualEditStatus.USER_EDITED,
        expected_sha256=expected_sha256,
        observed_sha256=observed_sha256,
    )
