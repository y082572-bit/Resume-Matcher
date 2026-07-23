"""Job Posting Analysis replay / staleness detection: no current input
means unverified (never guessed FRESH), an identical replay input is FRESH,
and any change -- posting, model configuration, or prompt version -- makes
it STALE. ``ready_for_candidacy_thesis`` is always computed internally.

All posting text and evidence is synthetic; no real job posting or
employer data is used.
"""

from __future__ import annotations

import pytest

from app.schemas.job_posting_analysis import (
    AnalysisAssertionBasis,
    ControlledJobAnalysisResponse,
    JobAnalysisAdapterResponse,
    JobAnalysisContext,
    JobAnalysisFreshnessStatus,
    JobAnalysisModelConfiguration,
    JobPostingAnalysisStatus,
    JobPostingSourceType,
    ResponseRoleObjectiveItem,
)
from app.services.job_analysis_builder import build_job_posting_analysis
from app.services.job_analysis_replay import (
    evaluate_job_analysis_freshness,
    replay_input_from_job_analysis_result,
)
from app.services.job_posting_snapshot import build_job_posting_snapshot, segment_job_posting


RAW_POSTING = "Sales Manager\n\nDESCRIPTION:\n- Lead the regional team\n- Own the pipeline"


def _snapshot_and_segments(raw_text: str = RAW_POSTING):
    snapshot = build_job_posting_snapshot(
        posting_id="job-1",
        source_type=JobPostingSourceType.MANUAL_TEXT,
        raw_text=raw_text,
        source_language="en",
        role_title="Sales Manager",
        company_name="Acme Corp",
    )
    return snapshot, segment_job_posting(snapshot)


def _context(prompt_template_version: str = "controlled-job-analysis-prompt-v1", **overrides):
    model_configuration = overrides.pop(
        "model_configuration",
        JobAnalysisModelConfiguration(
            provider="fake",
            model_identifier="fake-model",
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=1000,
            timeout_seconds=30.0,
            maximum_attempts=2,
            response_schema_version="controlled-job-analysis-response-v1",
            retry_policy_version="job-analysis-retry-policy-v1",
        ),
    )
    defaults = dict(
        analysis_policy_version="job-posting-analysis-policy-v1",
        prompt_template_version=prompt_template_version,
        retry_policy_version="job-analysis-retry-policy-v1",
        response_schema_version="controlled-job-analysis-response-v1",
        model_configuration=model_configuration,
    )
    defaults.update(overrides)
    return JobAnalysisContext(**defaults)


class _FakeAdapter:
    def __init__(self, response: ControlledJobAnalysisResponse):
        self._response = response

    async def analyze(self, *, request, model_configuration):
        return JobAnalysisAdapterResponse(
            provider="fake",
            model_identifier="fake-model",
            raw_response_text="ok",
            structured_response=self._response,
        )


async def _build_result(snapshot, segments, context):
    evidence = tuple(s.segment_id for s in segments if "lead the regional team" in s.normalized_text)
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive regional sales execution",
            assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    return await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=context, llm_adapter=_FakeAdapter(response)
    )


# -- 45. No current replay input -> FRESHNESS_NOT_VERIFIED, never guessed FRESH --


@pytest.mark.asyncio
async def test_no_current_replay_input_is_freshness_not_verified():
    snapshot, segments = _snapshot_and_segments()
    context = _context()
    result = await _build_result(snapshot, segments, context)
    previous = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    evaluation = evaluate_job_analysis_freshness(previous, None)
    assert evaluation.freshness_status == JobAnalysisFreshnessStatus.FRESHNESS_NOT_VERIFIED


# -- 46. Identical replay input -> FRESH --


@pytest.mark.asyncio
async def test_identical_replay_input_is_fresh():
    snapshot, segments = _snapshot_and_segments()
    context = _context()
    result = await _build_result(snapshot, segments, context)
    previous = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    current = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    evaluation = evaluate_job_analysis_freshness(previous, current)
    assert evaluation.freshness_status == JobAnalysisFreshnessStatus.FRESH
    assert evaluation.stale_fields == ()


# -- 47. Any changed field -> STALE --


@pytest.mark.asyncio
async def test_changed_replay_input_is_stale():
    snapshot, segments = _snapshot_and_segments()
    context = _context()
    result = await _build_result(snapshot, segments, context)
    previous = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    current = previous.model_copy(update={"prompt_version": "controlled-job-analysis-prompt-v2"})
    evaluation = evaluate_job_analysis_freshness(previous, current)
    assert evaluation.freshness_status == JobAnalysisFreshnessStatus.STALE
    assert "prompt_version" in evaluation.stale_fields


# -- 48. A changed posting invalidates the analysis --


@pytest.mark.asyncio
async def test_changed_posting_makes_the_analysis_stale():
    snapshot, segments = _snapshot_and_segments()
    context = _context()
    result = await _build_result(snapshot, segments, context)
    previous = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )

    changed_snapshot, changed_segments = _snapshot_and_segments(
        raw_text=RAW_POSTING + "\n- New duty added later"
    )
    # `current` represents "what the posting looks like now" -- built against
    # the *original* result on purpose, to detect drift.
    current = replay_input_from_job_analysis_result(
        snapshot=changed_snapshot, segments=changed_segments, analysis_context=context, result=result
    )
    evaluation = evaluate_job_analysis_freshness(previous, current)
    assert evaluation.freshness_status == JobAnalysisFreshnessStatus.STALE
    assert "posting_fingerprint" in evaluation.stale_fields or "canonical_text_fingerprint" in evaluation.stale_fields


# -- 49. A changed analysis model configuration invalidates the analysis --


@pytest.mark.asyncio
async def test_changed_model_configuration_makes_the_analysis_stale():
    snapshot, segments = _snapshot_and_segments()
    context = _context()
    result = await _build_result(snapshot, segments, context)
    previous = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    other_context = _context(
        model_configuration=JobAnalysisModelConfiguration(
            provider="fake",
            model_identifier="a-different-model",
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=1000,
            timeout_seconds=30.0,
            maximum_attempts=2,
            response_schema_version="controlled-job-analysis-response-v1",
            retry_policy_version="job-analysis-retry-policy-v1",
        )
    )
    current = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=other_context, result=result
    )
    evaluation = evaluate_job_analysis_freshness(previous, current)
    assert evaluation.freshness_status == JobAnalysisFreshnessStatus.STALE
    assert "model_configuration_fingerprint" in evaluation.stale_fields


# -- 50. A changed prompt version invalidates the analysis --


@pytest.mark.asyncio
async def test_changed_prompt_version_makes_the_analysis_stale():
    snapshot, segments = _snapshot_and_segments()
    context = _context(prompt_template_version="controlled-job-analysis-prompt-v1")
    result = await _build_result(snapshot, segments, context)
    previous = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    other_context = _context(prompt_template_version="controlled-job-analysis-prompt-v2")
    current = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=other_context, result=result
    )
    evaluation = evaluate_job_analysis_freshness(previous, current)
    assert evaluation.freshness_status == JobAnalysisFreshnessStatus.STALE
    assert "prompt_version" in evaluation.stale_fields
    assert "analysis_context_fingerprint" in evaluation.stale_fields


# -- 51. ready_for_candidacy_thesis is never caller-supplied --


def test_job_posting_analysis_result_has_no_caller_settable_ready_flag_parameter():
    import inspect

    from app.services.job_analysis_builder import build_job_posting_analysis as builder

    signature = inspect.signature(builder)
    assert "ready_for_candidacy_thesis" not in signature.parameters


@pytest.mark.asyncio
async def test_ready_for_candidacy_thesis_is_computed_from_status_and_objective():
    snapshot, segments = _snapshot_and_segments()
    context = _context()
    result = await _build_result(snapshot, segments, context)
    assert result.status == JobPostingAnalysisStatus.ANALYZED
    assert result.ready_for_candidacy_thesis is True
