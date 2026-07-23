"""``build_controlled_job_analysis_request`` / prompt fingerprinting: the
request carries only the posting's public fields and its deterministic
evidence segments, always declares the complete closed prohibition set,
and its fingerprint is sensitive to every one of those inputs.

All posting text is synthetic; no real job posting or employer data is
used.
"""

from __future__ import annotations

from app.schemas.job_posting_analysis import ALL_JOB_ANALYSIS_PROHIBITIONS, JobPostingSourceType
from app.services.job_analysis_prompt import (
    build_controlled_job_analysis_request,
    compute_job_analysis_prompt_fingerprint,
)
from app.services.job_posting_snapshot import build_job_posting_snapshot, segment_job_posting


def _snapshot(**overrides):
    defaults = dict(
        posting_id="job-1",
        source_type=JobPostingSourceType.MANUAL_TEXT,
        raw_text="Sales Manager\n\nDESCRIPTION:\n- Lead the regional team\n- Own the pipeline",
        source_language="en",
        role_title="Sales Manager",
        company_name="Acme Corp",
    )
    defaults.update(overrides)
    return build_job_posting_snapshot(**defaults)


def test_request_carries_snapshot_public_fields():
    snapshot = _snapshot()
    segments = segment_job_posting(snapshot)
    request = build_controlled_job_analysis_request(
        snapshot=snapshot,
        segments=segments,
        prompt_template_version="controlled-job-analysis-prompt-v1",
        response_schema_version="controlled-job-analysis-response-v1",
    )
    assert request.posting_fingerprint == snapshot.snapshot_fingerprint
    assert request.source_language == snapshot.source_language
    assert request.role_title == snapshot.role_title
    assert request.company_name == snapshot.company_name
    assert request.segments == segments


def test_request_always_declares_the_complete_prohibition_set():
    snapshot = _snapshot()
    segments = segment_job_posting(snapshot)
    request = build_controlled_job_analysis_request(
        snapshot=snapshot,
        segments=segments,
        prompt_template_version="v1",
        response_schema_version="v1",
    )
    assert set(request.prohibitions) == set(ALL_JOB_ANALYSIS_PROHIBITIONS)


def test_prompt_fingerprint_is_stable_for_identical_request():
    snapshot = _snapshot()
    segments = segment_job_posting(snapshot)
    request_a = build_controlled_job_analysis_request(
        snapshot=snapshot, segments=segments, prompt_template_version="v1", response_schema_version="v1"
    )
    request_b = build_controlled_job_analysis_request(
        snapshot=snapshot, segments=segments, prompt_template_version="v1", response_schema_version="v1"
    )
    assert compute_job_analysis_prompt_fingerprint(
        request_a
    ) == compute_job_analysis_prompt_fingerprint(request_b)


def test_prompt_fingerprint_changes_when_posting_text_changes():
    snapshot_a = _snapshot()
    snapshot_b = _snapshot(raw_text=snapshot_a.canonical_text + "\n- Additional duty")
    segments_a = segment_job_posting(snapshot_a)
    segments_b = segment_job_posting(snapshot_b)
    request_a = build_controlled_job_analysis_request(
        snapshot=snapshot_a, segments=segments_a, prompt_template_version="v1", response_schema_version="v1"
    )
    request_b = build_controlled_job_analysis_request(
        snapshot=snapshot_b, segments=segments_b, prompt_template_version="v1", response_schema_version="v1"
    )
    assert compute_job_analysis_prompt_fingerprint(
        request_a
    ) != compute_job_analysis_prompt_fingerprint(request_b)


def test_prompt_fingerprint_changes_when_prompt_template_version_changes():
    snapshot = _snapshot()
    segments = segment_job_posting(snapshot)
    request_a = build_controlled_job_analysis_request(
        snapshot=snapshot, segments=segments, prompt_template_version="v1", response_schema_version="v1"
    )
    request_b = build_controlled_job_analysis_request(
        snapshot=snapshot, segments=segments, prompt_template_version="v2", response_schema_version="v1"
    )
    assert compute_job_analysis_prompt_fingerprint(
        request_a
    ) != compute_job_analysis_prompt_fingerprint(request_b)


def test_prompt_carries_no_candidate_or_cv_fields():
    request = build_controlled_job_analysis_request(
        snapshot=_snapshot(),
        segments=segment_job_posting(_snapshot()),
        prompt_template_version="v1",
        response_schema_version="v1",
    )
    field_names = set(type(request).model_fields)
    assert field_names.isdisjoint({"cv_text", "candidate_facts", "master_resume", "candidate_profile"})
