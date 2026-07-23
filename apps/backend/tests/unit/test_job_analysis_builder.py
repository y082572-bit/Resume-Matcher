"""``build_job_posting_analysis``: evidence requirements, assertion-basis
tagging, unknown-segment/overclaim/numeric-outcome guardrails, response
partition completeness/disjointness, and provider retry behavior.

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
    JobAnalysisModelConfiguration,
    JobAnalysisProviderErrorCode,
    JobAnalysisViolationCode,
    JobPostingAnalysisStatus,
    JobPostingSourceType,
    ResponseCompetencyItem,
    ResponseHypothesisItem,
    ResponseOperatingContextItem,
    ResponseOutcomeItem,
    ResponseResponsibilityItem,
    ResponseRoleObjectiveItem,
    ResponseSuccessIndicatorItem,
    CompetencyClass,
)
from app.services.job_analysis_builder import build_job_posting_analysis
from app.services.job_posting_snapshot import build_job_posting_snapshot, segment_job_posting


RAW_POSTING = """Senior Sales Manager

RESPONSIBILITIES:
- Lead the sales team to improve plan realization
- Own the CRM pipeline for enterprise accounts

REQUIREMENTS:
- 5+ years experience in B2B sales

We improved plan realization from 36% to 96% last year and need someone to sustain that.
Due to recent layoffs, the team needs additional capacity.
"""


def _snapshot_and_segments(raw_text: str = RAW_POSTING):
    snapshot = build_job_posting_snapshot(
        posting_id="job-1",
        source_type=JobPostingSourceType.MANUAL_TEXT,
        raw_text=raw_text,
        source_language="en",
        role_title="Senior Sales Manager",
        company_name="Acme Corp",
    )
    return snapshot, segment_job_posting(snapshot)


def _context(**overrides) -> JobAnalysisContext:
    defaults = dict(
        analysis_policy_version="job-posting-analysis-policy-v1",
        prompt_template_version="controlled-job-analysis-prompt-v1",
        retry_policy_version="job-analysis-retry-policy-v1",
        response_schema_version="controlled-job-analysis-response-v1",
        model_configuration=JobAnalysisModelConfiguration(
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
    defaults.update(overrides)
    return JobAnalysisContext(**defaults)


def _segment_ids(segments, needle: str) -> tuple[str, ...]:
    return tuple(s.segment_id for s in segments if needle in s.normalized_text)


class ScriptedAdapter:
    """A deterministic fake adapter returning a scripted sequence of
    responses -- never a real provider call."""

    def __init__(self, responses: list[JobAnalysisAdapterResponse]):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    async def analyze(self, *, request, model_configuration):
        self.calls.append((request, model_configuration))
        return self._responses.pop(0)


def _ok_response(posting_fingerprint: str, **overrides) -> JobAnalysisAdapterResponse:
    defaults = dict(
        provider="fake",
        model_identifier="fake-model",
        raw_response_text="ok",
        structured_response=ControlledJobAnalysisResponse(
            response_schema_version="controlled-job-analysis-response-v1",
            posting_fingerprint=posting_fingerprint,
        ),
    )
    defaults.update(overrides)
    return JobAnalysisAdapterResponse(**defaults)


async def _run(snapshot, segments, response: ControlledJobAnalysisResponse):
    adapter = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                raw_response_text="ok",
                structured_response=response,
            )
        ]
    )
    result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    return result, adapter


# -- 21-25. Every accepted item category requires evidence --


@pytest.mark.asyncio
async def test_role_objective_requires_evidence():
    snapshot, segments = _snapshot_and_segments()
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
            supporting_evidence_segment_ids=(),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.role_objective is None
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_role_objective_accepted_with_evidence():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.role_objective is not None
    assert result.status == JobPostingAnalysisStatus.ANALYZED


@pytest.mark.asyncio
async def test_responsibility_requires_evidence():
    snapshot, segments = _snapshot_and_segments()
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-1",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=(),
                priority_order=0,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.responsibilities == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_responsibility_accepted_with_evidence():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-1",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
                priority_order=0,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.responsibilities) == 1


@pytest.mark.asyncio
async def test_outcome_requires_evidence():
    snapshot, segments = _snapshot_and_segments()
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        outcomes=(
            ResponseOutcomeItem(
                response_item_id="out-1",
                expected_change="Sustain team performance",
                affected_business_area="Sales",
                assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
                supporting_evidence_segment_ids=(),
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.outcomes == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_competency_requires_evidence():
    snapshot, segments = _snapshot_and_segments()
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        competencies=(
            ResponseCompetencyItem(
                response_item_id="comp-1",
                competency_name="B2B sales",
                competency_class=CompetencyClass.FUNCTIONAL_SKILL,
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=(),
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.competencies == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_competency_accepted_with_evidence():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "5+ years experience")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        competencies=(
            ResponseCompetencyItem(
                response_item_id="comp-1",
                competency_name="B2B sales",
                competency_class=CompetencyClass.FUNCTIONAL_SKILL,
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.competencies) == 1


@pytest.mark.asyncio
async def test_hypothesis_requires_evidence():
    snapshot, segments = _snapshot_and_segments()
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        hypotheses=(
            ResponseHypothesisItem(
                response_item_id="hyp-1",
                problem_statement="Needs to sustain a recent improvement",
                affected_business_area="Sales",
                observable_signals_in_posting=("improved from 36% to 96%",),
                supporting_evidence_segment_ids=(),
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.hypotheses == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


# -- 26-27. Explicit vs inferred assertion basis is preserved --


@pytest.mark.asyncio
async def test_inferred_hypothesis_is_tagged_inferred():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "36% to 96%")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        hypotheses=(
            ResponseHypothesisItem(
                response_item_id="hyp-1",
                problem_statement="Needs to sustain a recent improvement in plan realization",
                affected_business_area="Sales",
                observable_signals_in_posting=("improved from 36% to 96%",),
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].assertion_basis == AnalysisAssertionBasis.INFERRED_FROM_POSTING


@pytest.mark.asyncio
async def test_explicit_responsibility_is_tagged_explicit():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-1",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
                priority_order=0,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.responsibilities[0].assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING


# -- 28. Unknown segment blocks --


@pytest.mark.asyncio
async def test_unknown_segment_reference_blocks_item():
    snapshot, segments = _snapshot_and_segments()
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-1",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=("f" * 64,),
                priority_order=0,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert any(
        b.violation_code == JobAnalysisViolationCode.UNKNOWN_SEGMENT for b in result.blocked_items
    )


# -- 29. Unsupported employer claim blocks --


@pytest.mark.asyncio
async def test_unsupported_absolute_employer_claim_blocks_hypothesis():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "5+ years experience")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        hypotheses=(
            ResponseHypothesisItem(
                response_item_id="hyp-1",
                problem_statement="The company is in crisis and losing customers",
                affected_business_area="Sales",
                observable_signals_in_posting=("hiring for this role",),
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.hypotheses == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.OVERCLAIM_UNSUPPORTED_HYPOTHESIS
        for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_supported_absolute_employer_claim_is_accepted():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "due to recent layoffs")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        hypotheses=(
            ResponseHypothesisItem(
                response_item_id="hyp-1",
                problem_statement="The company recently had layoffs and needs coverage",
                affected_business_area="Sales",
                observable_signals_in_posting=("recent layoffs mentioned",),
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.hypotheses) == 1


# -- 30. Numeric outcome without matching evidence number blocks --


@pytest.mark.asyncio
async def test_numeric_outcome_without_supporting_number_blocks():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "5+ years experience")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        outcomes=(
            ResponseOutcomeItem(
                response_item_id="out-1",
                expected_change="Improve conversion from 36% to 96%",
                affected_business_area="Sales",
                assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.outcomes == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.UNSUPPORTED_NUMERIC_OUTCOME
        for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_numeric_outcome_with_supporting_number_is_accepted():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "36% to 96%")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        outcomes=(
            ResponseOutcomeItem(
                response_item_id="out-1",
                expected_change="Improve conversion from 36% to 96%",
                affected_business_area="Sales",
                assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.outcomes) == 1


# -- 31. Duplicate analysis item id blocks the whole response --


@pytest.mark.asyncio
async def test_duplicate_response_item_id_invalidates_response():
    snapshot, segments = _snapshot_and_segments()
    evidence_1 = _segment_ids(segments, "lead the sales team")
    evidence_2 = _segment_ids(segments, "own the crm")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="dup",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence_1,
                priority_order=0,
            ),
            ResponseResponsibilityItem(
                response_item_id="dup",
                statement="Own the CRM pipeline",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence_2,
                priority_order=1,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.status == JobPostingAnalysisStatus.INVALID_INPUT
    assert any(
        v.violation_code == JobAnalysisViolationCode.DUPLICATE_RESPONSE_ITEM_ID
        for v in result.violations
    )


# -- 32-33. Partition completeness and disjointness --


@pytest.mark.asyncio
async def test_partition_is_complete_and_disjoint():
    snapshot, segments = _snapshot_and_segments()
    evidence_1 = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
            supporting_evidence_segment_ids=evidence_1,
        ),
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-ok",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence_1,
                priority_order=0,
            ),
            ResponseResponsibilityItem(
                response_item_id="resp-bad",
                statement="Unsupported duty",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=(),
                priority_order=1,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    total_submitted = 2
    accepted_ids = {r.responsibility_fingerprint for r in result.responsibilities}
    blocked_ids = {b.source_response_item_id for b in result.blocked_items}
    assert len(result.responsibilities) + len(result.blocked_items) == total_submitted
    assert "resp-bad" in blocked_ids
    assert len(accepted_ids) == 1


# -- 34. An unused segment is allowed --


@pytest.mark.asyncio
async def test_unused_segment_does_not_block_analysis():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.status == JobPostingAnalysisStatus.ANALYZED
    assert len(segments) > 1  # several segments were never cited by any item


# -- 35. One request maps to exactly one snapshot --


@pytest.mark.asyncio
async def test_one_request_targets_exactly_one_snapshot():
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter([_ok_response(snapshot.snapshot_fingerprint)])
    await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0].posting_fingerprint == snapshot.snapshot_fingerprint


# -- 36-37. Retry at most once, only for TIMEOUT/TRANSPORT_ERROR --


@pytest.mark.asyncio
async def test_timeout_is_retried_exactly_once():
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                provider_error_code=JobAnalysisProviderErrorCode.TIMEOUT,
            ),
            _ok_response(snapshot.snapshot_fingerprint),
        ]
    )
    result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    assert len(adapter.calls) == 2
    assert result.status == JobPostingAnalysisStatus.BLOCKED  # no role_objective in ok response
    assert result.provenance.attempt_count == 2


@pytest.mark.asyncio
async def test_transport_error_is_retried_exactly_once():
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                provider_error_code=JobAnalysisProviderErrorCode.TRANSPORT_ERROR,
            ),
            _ok_response(snapshot.snapshot_fingerprint),
        ]
    )
    result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    assert len(adapter.calls) == 2
    assert result.provenance.attempt_count == 2


@pytest.mark.asyncio
async def test_retry_exhausted_after_two_timeouts():
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                provider_error_code=JobAnalysisProviderErrorCode.TIMEOUT,
            ),
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                provider_error_code=JobAnalysisProviderErrorCode.TIMEOUT,
            ),
        ]
    )
    result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    assert len(adapter.calls) == 2
    assert result.status == JobPostingAnalysisStatus.PROVIDER_ERROR
    assert result.blocked_items[0].violation_code == JobAnalysisViolationCode.RETRY_EXHAUSTED


# -- 38-41. No retry for non-transient provider errors --


@pytest.mark.parametrize(
    "error_code",
    [
        JobAnalysisProviderErrorCode.INVALID_RESPONSE_JSON,
        JobAnalysisProviderErrorCode.RESPONSE_SCHEMA_MISMATCH,
        JobAnalysisProviderErrorCode.EMPTY_RESPONSE,
        JobAnalysisProviderErrorCode.CONTENT_FILTERED,
    ],
)
@pytest.mark.asyncio
async def test_non_transient_provider_errors_are_never_retried(error_code):
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake", model_identifier="fake-model", provider_error_code=error_code
            )
        ]
    )
    result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    assert len(adapter.calls) == 1
    assert result.status == JobPostingAnalysisStatus.PROVIDER_ERROR
    assert result.blocked_items[0].violation_code == JobAnalysisViolationCode.PROVIDER_ERROR


# -- 42. No model fallback across attempts --


@pytest.mark.asyncio
async def test_no_model_fallback_across_retry_attempts():
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                provider_error_code=JobAnalysisProviderErrorCode.TIMEOUT,
            ),
            _ok_response(snapshot.snapshot_fingerprint),
        ]
    )
    await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    first_config = adapter.calls[0][1]
    second_config = adapter.calls[1][1]
    assert first_config == second_config


# -- 43. provider_request_id never participates in identity --


@pytest.mark.asyncio
async def test_provider_request_id_does_not_affect_result_fingerprint():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    adapter_a = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                structured_response=response,
                raw_response_text="ok",
                provider_request_id="req-aaaa",
            )
        ]
    )
    adapter_b = ScriptedAdapter(
        [
            JobAnalysisAdapterResponse(
                provider="fake",
                model_identifier="fake-model",
                structured_response=response,
                raw_response_text="ok",
                provider_request_id="req-bbbb-totally-different",
            )
        ]
    )
    result_a = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter_a
    )
    result_b = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter_b
    )
    assert result_a.result_fingerprint == result_b.result_fingerprint


# -- 44. Only the injected fake adapter is ever invoked --


@pytest.mark.asyncio
async def test_only_the_injected_adapter_is_invoked_never_a_real_provider():
    snapshot, segments = _snapshot_and_segments()
    adapter = ScriptedAdapter([_ok_response(snapshot.snapshot_fingerprint)])
    await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=_context(), llm_adapter=adapter
    )
    assert adapter.calls[0][0].posting_fingerprint == snapshot.snapshot_fingerprint
    assert isinstance(adapter, ScriptedAdapter)


# -- Assertion-basis validation: EXPLICIT_IN_POSTING is never trusted on
# the model's self-report alone -- it is independently re-checked against
# the item's own cited evidence segments. --


@pytest.mark.asyncio
async def test_explicit_objective_exactly_supported_by_cited_evidence_passes():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Lead the sales team to improve plan realization",
            assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.role_objective is not None
    assert result.role_objective.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING


@pytest.mark.asyncio
async def test_explicit_objective_not_in_cited_evidence_is_blocked():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.role_objective is None
    assert result.status == JobPostingAnalysisStatus.BLOCKED
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_explicit_responsibility_exactly_supported_passes():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-1",
                statement="Lead the sales team",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
                priority_order=0,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.responsibilities) == 1


@pytest.mark.asyncio
async def test_explicit_responsibility_not_in_cited_evidence_is_blocked():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        responsibilities=(
            ResponseResponsibilityItem(
                response_item_id="resp-1",
                statement="Own the CRM pipeline for enterprise accounts",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
                priority_order=0,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.responsibilities == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_explicit_outcome_exactly_supported_passes():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "36% to 96%")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        outcomes=(
            ResponseOutcomeItem(
                response_item_id="out-1",
                expected_change="We improved plan realization from 36% to 96% last year",
                affected_business_area="Sales",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.outcomes) == 1


@pytest.mark.asyncio
async def test_explicit_outcome_only_in_noncited_segment_is_blocked():
    snapshot, segments = _snapshot_and_segments()
    # the claim text appears verbatim only in the "36% to 96%" paragraph,
    # never in the CRM bullet this item actually cites.
    wrong_evidence = _segment_ids(segments, "own the crm")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        outcomes=(
            ResponseOutcomeItem(
                response_item_id="out-1",
                expected_change="We improved plan realization from 36% to 96% last year",
                affected_business_area="Sales",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=wrong_evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.outcomes == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_explicit_competency_exactly_supported_passes():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "5+ years experience")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        competencies=(
            ResponseCompetencyItem(
                response_item_id="comp-1",
                competency_name="B2B sales",
                competency_class=CompetencyClass.FUNCTIONAL_SKILL,
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.competencies) == 1


@pytest.mark.asyncio
async def test_explicit_competency_only_in_different_segment_is_blocked():
    snapshot, segments = _snapshot_and_segments()
    wrong_evidence = _segment_ids(segments, "own the crm")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        competencies=(
            ResponseCompetencyItem(
                response_item_id="comp-1",
                competency_name="B2B sales",
                competency_class=CompetencyClass.FUNCTIONAL_SKILL,
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=wrong_evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.competencies == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_explicit_success_indicator_exactly_supported_passes():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "36% to 96%")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        success_indicators=(
            ResponseSuccessIndicatorItem(
                response_item_id="si-1",
                indicator_statement="We improved plan realization from 36% to 96% last year",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.success_indicators) == 1


@pytest.mark.asyncio
async def test_explicit_success_indicator_not_in_cited_evidence_is_blocked():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "5+ years experience")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        success_indicators=(
            ResponseSuccessIndicatorItem(
                response_item_id="si-1",
                indicator_statement="We improved plan realization from 36% to 96% last year",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.success_indicators == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_explicit_operating_context_exactly_supported_passes():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "due to recent layoffs")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        operating_context=(
            ResponseOperatingContextItem(
                response_item_id="oc-1",
                context_statement="Due to recent layoffs, the team needs additional capacity",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.operating_context) == 1


@pytest.mark.asyncio
async def test_explicit_operating_context_not_in_cited_evidence_is_blocked():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "5+ years experience")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        operating_context=(
            ResponseOperatingContextItem(
                response_item_id="oc-1",
                context_statement="Due to recent layoffs, the team needs additional capacity",
                assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.operating_context == ()
    assert any(
        b.violation_code == JobAnalysisViolationCode.MISSING_EVIDENCE for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_inferred_objective_may_pass_without_literal_claim_text():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert result.role_objective is not None
    assert result.role_objective.assertion_basis == AnalysisAssertionBasis.INFERRED_FROM_POSTING


@pytest.mark.asyncio
async def test_unsupported_explicit_claim_is_blocked_not_silently_downgraded():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "lead the sales team")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution",
            assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
            supporting_evidence_segment_ids=evidence,
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    # never silently accepted as an INFERRED item -- it is blocked
    # outright, with no accepted role_objective at all.
    assert result.role_objective is None
    assert result.status == JobPostingAnalysisStatus.BLOCKED
    assert len(result.blocked_items) == 1


@pytest.mark.asyncio
async def test_employer_problem_hypothesis_assertion_basis_always_inferred():
    snapshot, segments = _snapshot_and_segments()
    evidence = _segment_ids(segments, "36% to 96%")
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        hypotheses=(
            ResponseHypothesisItem(
                response_item_id="hyp-1",
                problem_statement="Needs to sustain a recent improvement in plan realization",
                affected_business_area="Sales",
                observable_signals_in_posting=("improved from 36% to 96%",),
                supporting_evidence_segment_ids=evidence,
            ),
        ),
    )
    result, _ = await _run(snapshot, segments, response)
    assert len(result.hypotheses) == 1
    assert result.hypotheses[0].assertion_basis == AnalysisAssertionBasis.INFERRED_FROM_POSTING
    # ResponseHypothesisItem has no assertion_basis field at all -- the
    # model can never even attempt to self-report EXPLICIT for a
    # hypothesis.
    assert "assertion_basis" not in ResponseHypothesisItem.model_fields
