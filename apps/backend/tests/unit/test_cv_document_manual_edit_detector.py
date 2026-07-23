"""Unit tests for Explicit Provenance Stage P6-A: manual-edit detection.

Raw SHA-256 bytes comparison only -- never mtime, filesize, filename, or
path.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_document_artifact import ManualEditStatus
from app.services.cv_document_manual_edit_detector import compute_raw_sha256, detect_manual_edit


def test_identical_bytes_are_unmodified() -> None:
    content = b"identical docx bytes"
    expected = compute_raw_sha256(content)
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=content)
    assert result.status == ManualEditStatus.UNMODIFIED
    assert result.observed_sha256 == expected


def test_changed_bytes_are_user_edited() -> None:
    original = b"original docx bytes"
    edited = b"user edited docx bytes, much longer than original"
    expected = compute_raw_sha256(original)
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=edited)
    assert result.status == ManualEditStatus.USER_EDITED
    assert result.observed_sha256 == compute_raw_sha256(edited)
    assert result.observed_sha256 != expected


def test_single_byte_difference_is_still_user_edited() -> None:
    """A false positive here (e.g. from ZIP metadata Word itself rewrites)
    is an accepted, safe, fail-closed outcome -- never silently ignored."""

    original = b"docx-content-AAAA"
    edited = b"docx-content-AAAB"
    expected = compute_raw_sha256(original)
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=edited)
    assert result.status == ManualEditStatus.USER_EDITED


def test_missing_bytes_is_file_missing() -> None:
    expected = compute_raw_sha256(b"whatever")
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=None)
    assert result.status == ManualEditStatus.FILE_MISSING
    assert result.observed_sha256 is None


def test_current_hash_is_computed_from_actual_bytes_each_call() -> None:
    """The function never trusts a caller-supplied "current" hash -- it
    always recomputes SHA-256 from the exact bytes handed in for *this*
    call, so calling it twice with different bytes gives different
    results even though ``expected_sha256`` never changes."""

    expected = compute_raw_sha256(b"expected-content")
    first = detect_manual_edit(expected_sha256=expected, observed_bytes=b"observation-one")
    second = detect_manual_edit(expected_sha256=expected, observed_bytes=b"observation-two-different")
    assert first.observed_sha256 != second.observed_sha256
    assert first.observed_sha256 == compute_raw_sha256(b"observation-one")
    assert second.observed_sha256 == compute_raw_sha256(b"observation-two-different")


def test_stored_hash_field_is_never_trusted_as_current() -> None:
    """Simulates a caller who might be tempted to pass a stale, previously
    stored hash as if it were fresh -- the detector always independently
    recomputes from ``observed_bytes``, so a genuinely modified document is
    still caught even if some unrelated stored hash still matches."""

    original_bytes = b"original approved content"
    stale_stored_hash = compute_raw_sha256(original_bytes)
    modified_bytes = b"a user has since modified this document"

    result = detect_manual_edit(expected_sha256=stale_stored_hash, observed_bytes=modified_bytes)
    assert result.status == ManualEditStatus.USER_EDITED
    assert result.observed_sha256 == compute_raw_sha256(modified_bytes)


def test_reread_mismatch_is_file_changed_during_read() -> None:
    first_read = b"read-one-content"
    second_read = b"read-two-content-different"
    expected = compute_raw_sha256(first_read)
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=first_read, reread_bytes=second_read)
    assert result.status == ManualEditStatus.FILE_CHANGED_DURING_READ
    assert result.observed_sha256 is None


def test_freshness_not_verified_when_explicitly_unverifiable() -> None:
    expected = compute_raw_sha256(b"whatever")
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=b"whatever", freshness_verified=False)
    assert result.status == ManualEditStatus.FRESHNESS_NOT_VERIFIED
    assert result.observed_sha256 is None


def test_mtime_and_filesize_never_participate() -> None:
    """The detector's signature accepts no mtime/filesize/filename/path
    argument at all -- two byte strings of identical length but different
    content are still correctly told apart."""

    content_a = b"AAAAAAAAAAAAAAAAAAAA"
    content_b = b"BBBBBBBBBBBBBBBBBBBB"
    assert len(content_a) == len(content_b)
    expected = compute_raw_sha256(content_a)
    result = detect_manual_edit(expected_sha256=expected, observed_bytes=content_b)
    assert result.status == ManualEditStatus.USER_EDITED
