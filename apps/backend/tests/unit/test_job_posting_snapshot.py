"""``JobPostingSnapshot`` construction and deterministic evidence
segmentation: normalization, content-only identity, and segment fingerprint
integrity.

All posting text is synthetic; no real job posting or employer data is
used.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.schemas.job_posting_analysis import JobPostingSourceType
from app.services.job_posting_snapshot import (
    InvalidJobPostingSnapshotError,
    build_job_posting_snapshot,
    find_duplicate_segment_ids,
    recompute_job_evidence_segment_fingerprint,
    recompute_job_posting_snapshot_fingerprint,
    segment_job_posting,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

RAW_POSTING = """Senior Sales Manager

RESPONSIBILITIES:
- Lead the sales team to improve plan realization
- Own the CRM pipeline for enterprise accounts
- Fluent English required

REQUIREMENTS:
- 5+ years experience in B2B sales
- Fluent English required
"""


def _build_snapshot(**overrides):
    defaults = dict(
        posting_id="job-1",
        source_type=JobPostingSourceType.MANUAL_TEXT,
        raw_text=RAW_POSTING,
        source_language="en",
        role_title="Senior Sales Manager",
        company_name="Acme Corp",
    )
    defaults.update(overrides)
    return build_job_posting_snapshot(**defaults)


# -- 1. Empty posting blocks snapshot --


def test_empty_posting_text_raises():
    with pytest.raises(InvalidJobPostingSnapshotError):
        build_job_posting_snapshot(
            posting_id=None,
            source_type=JobPostingSourceType.MANUAL_TEXT,
            raw_text="",
            source_language="en",
        )


def test_whitespace_only_posting_text_raises():
    with pytest.raises(InvalidJobPostingSnapshotError):
        build_job_posting_snapshot(
            posting_id=None,
            source_type=JobPostingSourceType.MANUAL_TEXT,
            raw_text="   \n\n\t  \n",
            source_language="en",
        )


def test_empty_canonical_text_field_rejected_by_model():
    from app.schemas.job_posting_analysis import JobPostingSnapshot

    with pytest.raises(ValidationError):
        JobPostingSnapshot(
            source_type=JobPostingSourceType.MANUAL_TEXT,
            canonical_text="",
            source_language="en",
            snapshot_fingerprint="a" * 64,
        )


# -- 2. Snapshot fingerprint is recalculated (never trusted as self-reported) --


def test_snapshot_fingerprint_is_recomputed_and_matches_valid_snapshot():
    snapshot = _build_snapshot()
    assert recompute_job_posting_snapshot_fingerprint(snapshot) == snapshot.snapshot_fingerprint


def test_tampered_snapshot_fingerprint_is_detected():
    snapshot = _build_snapshot()
    tampered = snapshot.model_copy(update={"snapshot_fingerprint": "f" * 64})
    assert recompute_job_posting_snapshot_fingerprint(tampered) != tampered.snapshot_fingerprint


# -- 3. Changing the text changes the fingerprint --


def test_changing_canonical_text_changes_fingerprint():
    a = _build_snapshot(raw_text=RAW_POSTING)
    b = _build_snapshot(raw_text=RAW_POSTING + "\nAdditional requirement: SQL")
    assert a.snapshot_fingerprint != b.snapshot_fingerprint


def test_identical_text_produces_identical_fingerprint():
    a = _build_snapshot()
    b = _build_snapshot()
    assert a.snapshot_fingerprint == b.snapshot_fingerprint


# -- 4. URL is never the sole identity --


def test_source_url_does_not_participate_in_identity():
    a = _build_snapshot(source_url="https://example.com/jobs/1")
    b = _build_snapshot(source_url="https://example.com/jobs/2-different")
    assert a.snapshot_fingerprint == b.snapshot_fingerprint
    assert a.source_url != b.source_url


# -- 5. posting_id is never the sole identity --


def test_posting_id_does_not_participate_in_identity():
    a = _build_snapshot(posting_id="job-alpha")
    b = _build_snapshot(posting_id="job-beta")
    assert a.snapshot_fingerprint == b.snapshot_fingerprint
    assert a.posting_id != b.posting_id


# -- 6. Segmentation is deterministic --


def test_segmentation_is_deterministic_across_runs():
    snapshot = _build_snapshot()
    first = segment_job_posting(snapshot)
    second = segment_job_posting(snapshot)
    assert first == second


def test_segmentation_produces_expected_evidence():
    snapshot = _build_snapshot()
    segments = segment_job_posting(snapshot)
    assert len(segments) > 0
    assert all(SHA256_RE.match(s.segment_id) for s in segments)
    assert all(SHA256_RE.match(s.segment_fingerprint) for s in segments)
    assert all(s.posting_fingerprint == snapshot.snapshot_fingerprint for s in segments)


# -- 7. segment_id is not a global list index --


def test_segment_id_is_not_the_segment_order_value():
    snapshot = _build_snapshot()
    segments = segment_job_posting(snapshot)
    for segment in segments:
        assert segment.segment_id != str(segment.segment_order)


def test_duplicate_content_segments_get_distinct_ids_via_discriminator():
    snapshot = _build_snapshot()
    segments = segment_job_posting(snapshot)
    fluent_english_segments = [s for s in segments if s.normalized_text == "- fluent english required"]
    assert len(fluent_english_segments) == 2
    assert fluent_english_segments[0].segment_id != fluent_english_segments[1].segment_id
    assert fluent_english_segments[0].source_text == fluent_english_segments[1].source_text


def test_two_snapshots_sharing_a_segment_order_get_different_segment_ids():
    a = _build_snapshot(raw_text="Alpha role\n\nDESCRIPTION:\n- First distinct duty")
    b = _build_snapshot(raw_text="Beta role\n\nDESCRIPTION:\n- Second distinct duty")
    seg_a = segment_job_posting(a)[0]
    seg_b = segment_job_posting(b)[0]
    assert seg_a.segment_order == seg_b.segment_order == 0
    assert seg_a.segment_id != seg_b.segment_id


# -- 8. Duplicate segment id blocks --


def test_find_duplicate_segment_ids_detects_collision():
    snapshot = _build_snapshot()
    segments = list(segment_job_posting(snapshot))
    tampered_second = segments[1].model_copy(update={"segment_id": segments[0].segment_id})
    segments[1] = tampered_second
    duplicates = find_duplicate_segment_ids(segments)
    assert duplicates == (segments[0].segment_id,)


def test_find_duplicate_segment_ids_empty_when_all_unique():
    snapshot = _build_snapshot()
    segments = segment_job_posting(snapshot)
    assert find_duplicate_segment_ids(segments) == ()


# -- 9. Segment fingerprint mismatch blocks --


def test_tampered_segment_fingerprint_is_detected():
    snapshot = _build_snapshot()
    segment = segment_job_posting(snapshot)[0]
    tampered = segment.model_copy(update={"segment_fingerprint": "e" * 64})
    assert recompute_job_evidence_segment_fingerprint(tampered) != tampered.segment_fingerprint


def test_untampered_segment_fingerprint_matches():
    snapshot = _build_snapshot()
    for segment in segment_job_posting(snapshot):
        assert recompute_job_evidence_segment_fingerprint(segment) == segment.segment_fingerprint


# -- 10. Changing segment text changes its fingerprint --


def test_changing_segment_source_text_changes_fingerprint():
    snapshot = _build_snapshot()
    segment = segment_job_posting(snapshot)[0]
    mutated = segment.model_copy(update={"source_text": segment.source_text + " (edited)"})
    assert (
        recompute_job_evidence_segment_fingerprint(mutated)
        != recompute_job_evidence_segment_fingerprint(segment)
    )
