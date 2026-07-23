"""Candidacy Thesis provider retry, replay/staleness detection, and
``RoleStrategyContext`` fingerprint sensitivity.

All postings, facts, and evidence are synthetic; no real candidate or
employer data is used.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.candidacy_thesis import (
    CandidacyThesisContext,
    CandidacyThesisFreshnessStatus,
    CandidacyThesisModelConfiguration,
    CandidacyThesisProviderErrorCode,
    CandidacyThesisStatus,
    CandidacyThesisViolationCode,
    CandidacyThesisAdapterResponse,
    ControlledCandidacyThesisResponse,
    PositioningLevel,
    ResponseEvidenceMappingItem,
    ResponseStrategicContributionItem,
    ResponseThesisStatementItem,
    RoleEvidenceCategory,
    RoleStrategyContext,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSet, ApprovedFactPayloadSnapshot
from app.schemas.job_posting_analysis import (
    AnalysisAssertionBasis,
    ControlledJobAnalysisResponse,
    JobAnalysisAdapterResponse,
    JobAnalysisContext,
    JobAnalysisModelConfiguration,
    JobPostingSourceType,
    ResponseHypothesisItem,
    ResponseRoleObjectiveItem,
)
from app.services.candidacy_thesis_builder import (
    build_candidacy_thesis,
    compute_approved_fact_payload_set_fingerprint,
    compute_role_strategy_context_fingerprint,
)
from app.services.candidacy_thesis_replay import (
    evaluate_candidacy_thesis_freshness,
    replay_input_from_candidacy_thesis_result,
    compute_positioning_context_fingerprint,
)
from app.services.job_analysis_builder import build_job_posting_analysis
from app.services.job_analysis_replay import replay_input_from_job_analysis_result
from app.services.job_posting_snapshot import build_job_posting_snapshot, segment_job_posting


RAW_POSTING = """Senior Sales Manager

RESPONSIBILITIES:
- Lead the sales team to improve plan realization

We improved plan realization from 36% to 96% last year and need someone to sustain that.
"""


def _job_context(**overrides) -> JobAnalysisContext:
    defaults = dict(
        analysis_policy_version="job-posting-analysis-policy-v1",
        prompt_template_version="controlled-job-analysis-prompt-v1",
        retry_policy_version="job-analysis-retry-policy-v1",
        response_schema_version="controlled-job-analysis-response-v1",
        model_configuration=JobAnalysisModelConfiguration(
            provider="fake", model_identifier="fake-model", temperature=0.0, top_p=1.0,
            max_output_tokens=1000, timeout_seconds=30.0, maximum_attempts=2,
            response_schema_version="controlled-job-analysis-response-v1",
            retry_policy_version="job-analysis-retry-policy-v1",
        ),
    )
    defaults.update(overrides)
    return JobAnalysisContext(**defaults)


class _FakeJobAdapter:
    def __init__(self, response):
        self._response = response

    async def analyze(self, *, request, model_configuration):
        return JobAnalysisAdapterResponse(
            provider="fake", model_identifier="fake-model", raw_response_text="ok",
            structured_response=self._response,
        )


async def _build_job_analysis():
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=RAW_POSTING,
        source_language="en", role_title="Senior Sales Manager", company_name="Acme Corp",
    )
    segments = segment_job_posting(snapshot)
    objective_evidence = tuple(s.segment_id for s in segments if "lead the sales team" in s.normalized_text)
    outcome_evidence = tuple(s.segment_id for s in segments if "36% to 96%" in s.normalized_text)
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
        role_objective=ResponseRoleObjectiveItem(
            response_item_id="obj-1",
            concise_statement="Drive enterprise sales execution and improve plan realization",
            assertion_basis=AnalysisAssertionBasis.INFERRED_FROM_POSTING,
            supporting_evidence_segment_ids=objective_evidence,
        ),
        hypotheses=(
            ResponseHypothesisItem(
                response_item_id="hyp-1",
                problem_statement="Needs to sustain a recent improvement in plan realization",
                affected_business_area="Sales",
                observable_signals_in_posting=("improved from 36% to 96%",),
                supporting_evidence_segment_ids=outcome_evidence,
            ),
        ),
    )
    context = _job_context()
    result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=context, llm_adapter=_FakeJobAdapter(response)
    )
    current_replay = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    return snapshot, segments, context, result, current_replay


def _approved_fact(**overrides) -> ApprovedFactPayloadSnapshot:
    defaults = dict(
        person_entity_id=uuid.uuid4(), entity_id=uuid.uuid4(), fact_id=uuid.uuid4(),
        fact_type="ACHIEVEMENT", fact_revision=1, fact_content_fingerprint="d" * 64,
        value_json={"description": "Improved plan realization from 36% to 96%"},
        normalized_value_json={"description": "Improved plan realization from 36% to 96%"},
        payload_fingerprint="e" * 64,
    )
    defaults.update(overrides)
    return ApprovedFactPayloadSnapshot(**defaults)


def _fact_set(*facts: ApprovedFactPayloadSnapshot) -> ApprovedFactPayloadSet:
    return ApprovedFactPayloadSet(
        payloads=tuple(facts), payload_set_fingerprint=compute_approved_fact_payload_set_fingerprint(facts)
    )


def _thesis_context(**overrides) -> CandidacyThesisContext:
    defaults = dict(
        locale="en",
        thesis_prompt_version="controlled-candidacy-thesis-prompt-v1",
        thesis_policy_version="candidacy-thesis-policy-v1",
        retry_policy_version="candidacy-thesis-retry-policy-v1",
        response_schema_version="controlled-candidacy-thesis-response-v1",
        requested_positioning_level=PositioningLevel.MANAGEMENT,
        priority_evidence_categories=(RoleEvidenceCategory.BUSINESS_RESULT,),
        model_configuration=CandidacyThesisModelConfiguration(
            provider="fake", model_identifier="fake-model", temperature=0.0, top_p=1.0,
            max_output_tokens=1000, timeout_seconds=30.0, maximum_attempts=2,
            response_schema_version="controlled-candidacy-thesis-response-v1",
            retry_policy_version="candidacy-thesis-retry-policy-v1",
        ),
    )
    defaults.update(overrides)
    return CandidacyThesisContext(**defaults)


class ScriptedThesisAdapter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    async def synthesize_thesis(self, *, request, model_configuration):
        self.calls.append((request, model_configuration))
        return self._responses.pop(0)


def _thesis_response(job_result, fact, positioning=PositioningLevel.MANAGEMENT):
    hyp_fp = job_result.hypotheses[0].hypothesis_fingerprint
    return ControlledCandidacyThesisResponse(
        response_schema_version="controlled-candidacy-thesis-response-v1",
        posting_fingerprint=job_result.posting_fingerprint,
        analysis_result_fingerprint=job_result.result_fingerprint,
        acknowledged_positioning_level=positioning,
        target_narrative_emphasis=(RoleEvidenceCategory.BUSINESS_RESULT,),
        evidence_mappings=(
            ResponseEvidenceMappingItem(
                response_item_id="ev-1", entity_id=fact.entity_id, fact_id=fact.fact_id,
                fact_revision=fact.fact_revision, payload_fingerprint=fact.payload_fingerprint,
                linked_hypothesis_fingerprint=hyp_fp,
                role_evidence_category=RoleEvidenceCategory.BUSINESS_RESULT,
                narrative_link="Marek's documented improvement in plan realization is directly relevant",
            ),
        ),
        strategic_contribution_statements=(
            ResponseStrategicContributionItem(
                response_item_id="sc-1",
                statement="Marek's experience diagnosing and improving plan realization is directly relevant",
                supporting_evidence_mapping_response_item_ids=("ev-1",),
            ),
        ),
        thesis_statement=ResponseThesisStatementItem(
            response_item_id="thesis-1",
            statement="Marek's track record of improving plan realization is credible evidence he can help here",
            referenced_hypothesis_fingerprints=(hyp_fp,),
            supporting_evidence_mapping_response_item_ids=("ev-1",),
        ),
    )


async def _build_ready_thesis(thesis_context=None, positioning=PositioningLevel.MANAGEMENT):
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    response = _thesis_response(job_result, fact, positioning=positioning)
    adapter = ScriptedThesisAdapter(
        [CandidacyThesisAdapterResponse(provider="fake", model_identifier="fake-model", raw_response_text="ok", structured_response=response)]
    )
    context = thesis_context or _thesis_context(requested_positioning_level=positioning)
    result = await build_candidacy_thesis(
        job_analysis_result=job_result, job_analysis_snapshot=snapshot, job_analysis_segments=segments,
        job_analysis_context=job_context, current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set, thesis_context=context, llm_adapter=adapter,
    )
    return result, adapter, job_result, snapshot, segments, job_context, current_replay, fact_set, context


# -- 82-83. Retry at most once, only for TIMEOUT/TRANSPORT_ERROR --


@pytest.mark.asyncio
async def test_timeout_is_retried_exactly_once():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    response = _thesis_response(job_result, fact)
    adapter = ScriptedThesisAdapter(
        [
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model",
                provider_error_code=CandidacyThesisProviderErrorCode.TIMEOUT,
            ),
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model", raw_response_text="ok",
                structured_response=response,
            ),
        ]
    )
    result = await build_candidacy_thesis(
        job_analysis_result=job_result, job_analysis_snapshot=snapshot, job_analysis_segments=segments,
        job_analysis_context=job_context, current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set, thesis_context=_thesis_context(), llm_adapter=adapter,
    )
    assert len(adapter.calls) == 2
    assert result.status == CandidacyThesisStatus.READY
    assert result.provenance.attempt_count == 2


@pytest.mark.asyncio
async def test_transport_error_is_retried_exactly_once():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    response = _thesis_response(job_result, fact)
    adapter = ScriptedThesisAdapter(
        [
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model",
                provider_error_code=CandidacyThesisProviderErrorCode.TRANSPORT_ERROR,
            ),
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model", raw_response_text="ok",
                structured_response=response,
            ),
        ]
    )
    result = await build_candidacy_thesis(
        job_analysis_result=job_result, job_analysis_snapshot=snapshot, job_analysis_segments=segments,
        job_analysis_context=job_context, current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set, thesis_context=_thesis_context(), llm_adapter=adapter,
    )
    assert len(adapter.calls) == 2
    assert result.provenance.attempt_count == 2


# -- 84-85. No retry for non-transient provider errors --


@pytest.mark.parametrize(
    "error_code",
    [
        CandidacyThesisProviderErrorCode.INVALID_RESPONSE_JSON,
        CandidacyThesisProviderErrorCode.RESPONSE_SCHEMA_MISMATCH,
    ],
)
@pytest.mark.asyncio
async def test_non_transient_provider_errors_are_never_retried(error_code):
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    adapter = ScriptedThesisAdapter(
        [CandidacyThesisAdapterResponse(provider="fake", model_identifier="fake-model", provider_error_code=error_code)]
    )
    result = await build_candidacy_thesis(
        job_analysis_result=job_result, job_analysis_snapshot=snapshot, job_analysis_segments=segments,
        job_analysis_context=job_context, current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set, thesis_context=_thesis_context(), llm_adapter=adapter,
    )
    assert len(adapter.calls) == 1
    assert result.status == CandidacyThesisStatus.PROVIDER_ERROR


# -- 86. No model fallback across attempts --


@pytest.mark.asyncio
async def test_no_model_fallback_across_retry_attempts():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    response = _thesis_response(job_result, fact)
    adapter = ScriptedThesisAdapter(
        [
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model",
                provider_error_code=CandidacyThesisProviderErrorCode.TIMEOUT,
            ),
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model", raw_response_text="ok",
                structured_response=response,
            ),
        ]
    )
    await build_candidacy_thesis(
        job_analysis_result=job_result, job_analysis_snapshot=snapshot, job_analysis_segments=segments,
        job_analysis_context=job_context, current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set, thesis_context=_thesis_context(), llm_adapter=adapter,
    )
    assert adapter.calls[0][1] == adapter.calls[1][1]


# -- 87-89. Thesis replay/freshness --


@pytest.mark.asyncio
async def test_no_current_thesis_replay_input_is_freshness_not_verified():
    result, *_ = await _build_ready_thesis()
    assert result.thesis_statement is not None
    previous = replay_input_from_candidacy_thesis_result(
        job_analysis_replay_fingerprint=result.analysis_replay_fingerprint,
        approved_payload_set_fingerprint=result.payload_set_fingerprint,
        positioning_context_fingerprint=compute_positioning_context_fingerprint(
            requested_positioning_level=result.positioning_level,
            priority_evidence_categories=result.priority_evidence_categories,
        ),
        evidence_priority_fingerprints=(),
        model_configuration_fingerprint=result.role_strategy_context.candidacy_thesis_result_fingerprint,
        prompt_version=result.provenance.prompt_version,
        result=result,
    )
    evaluation = evaluate_candidacy_thesis_freshness(previous, None)
    assert evaluation.freshness_status == CandidacyThesisFreshnessStatus.FRESHNESS_NOT_VERIFIED


@pytest.mark.asyncio
async def test_identical_thesis_replay_input_is_fresh():
    result, *_ = await _build_ready_thesis()
    kwargs = dict(
        job_analysis_replay_fingerprint=result.analysis_replay_fingerprint,
        approved_payload_set_fingerprint=result.payload_set_fingerprint,
        positioning_context_fingerprint=compute_positioning_context_fingerprint(
            requested_positioning_level=result.positioning_level,
            priority_evidence_categories=result.priority_evidence_categories,
        ),
        evidence_priority_fingerprints=(),
        model_configuration_fingerprint="a" * 64,
        prompt_version=result.provenance.prompt_version,
        result=result,
    )
    previous = replay_input_from_candidacy_thesis_result(**kwargs)
    current = replay_input_from_candidacy_thesis_result(**kwargs)
    evaluation = evaluate_candidacy_thesis_freshness(previous, current)
    assert evaluation.freshness_status == CandidacyThesisFreshnessStatus.FRESH


@pytest.mark.asyncio
async def test_changed_thesis_replay_input_is_stale():
    result, *_ = await _build_ready_thesis()
    kwargs = dict(
        job_analysis_replay_fingerprint=result.analysis_replay_fingerprint,
        approved_payload_set_fingerprint=result.payload_set_fingerprint,
        positioning_context_fingerprint=compute_positioning_context_fingerprint(
            requested_positioning_level=result.positioning_level,
            priority_evidence_categories=result.priority_evidence_categories,
        ),
        evidence_priority_fingerprints=(),
        model_configuration_fingerprint="a" * 64,
        prompt_version=result.provenance.prompt_version,
        result=result,
    )
    previous = replay_input_from_candidacy_thesis_result(**kwargs)
    current = previous.model_copy(update={"approved_payload_set_fingerprint": "1" * 64})
    evaluation = evaluate_candidacy_thesis_freshness(previous, current)
    assert evaluation.freshness_status == CandidacyThesisFreshnessStatus.STALE
    assert "approved_payload_set_fingerprint" in evaluation.stale_fields


# -- 90-91. A changed thesis model configuration / prompt version
# invalidates the thesis (result_fingerprint changes) --


@pytest.mark.asyncio
async def test_changed_thesis_model_configuration_changes_result_fingerprint():
    result_a, *_ = await _build_ready_thesis(
        thesis_context=_thesis_context(
            model_configuration=CandidacyThesisModelConfiguration(
                provider="fake", model_identifier="model-a", temperature=0.0, top_p=1.0,
                max_output_tokens=1000, timeout_seconds=30.0, maximum_attempts=2,
                response_schema_version="controlled-candidacy-thesis-response-v1",
                retry_policy_version="candidacy-thesis-retry-policy-v1",
            )
        )
    )
    result_b, *_ = await _build_ready_thesis(
        thesis_context=_thesis_context(
            model_configuration=CandidacyThesisModelConfiguration(
                provider="fake", model_identifier="model-b", temperature=0.0, top_p=1.0,
                max_output_tokens=1000, timeout_seconds=30.0, maximum_attempts=2,
                response_schema_version="controlled-candidacy-thesis-response-v1",
                retry_policy_version="candidacy-thesis-retry-policy-v1",
            )
        )
    )
    assert result_a.provenance.model_configuration_fingerprint != result_b.provenance.model_configuration_fingerprint


@pytest.mark.asyncio
async def test_changed_thesis_prompt_version_changes_provenance():
    result_a, *_ = await _build_ready_thesis(
        thesis_context=_thesis_context(thesis_prompt_version="controlled-candidacy-thesis-prompt-v1")
    )
    result_b, *_ = await _build_ready_thesis(
        thesis_context=_thesis_context(thesis_prompt_version="controlled-candidacy-thesis-prompt-v2")
    )
    assert result_a.provenance.prompt_version != result_b.provenance.prompt_version


# -- 93-97. RoleStrategyContext identity and closedness --


def test_role_strategy_context_is_closed_to_arbitrary_fields():
    with pytest.raises(ValidationError):
        RoleStrategyContext(
            job_analysis_result_fingerprint="a" * 64,
            job_analysis_replay_fingerprint="b" * 64,
            candidacy_thesis_result_fingerprint="c" * 64,
            candidacy_thesis_replay_fingerprint="d" * 64,
            posting_fingerprint="e" * 64,
            role_objective_fingerprint="f" * 64,
            priority_evidence_categories=(RoleEvidenceCategory.BUSINESS_RESULT,),
            positioning_level=PositioningLevel.MANAGEMENT,
            target_narrative_emphasis=(RoleEvidenceCategory.BUSINESS_RESULT,),
            approved_supporting_fact_fingerprints=("1" * 64,),
            context_fingerprint="9" * 64,
            match_score=0.9,
        )


def test_role_strategy_context_has_no_free_text_thesis_field():
    assert "thesis_text" not in RoleStrategyContext.model_fields
    assert "arbitrary_metadata" not in RoleStrategyContext.model_fields
    assert set(RoleStrategyContext.model_fields).isdisjoint({"match_score", "candidate_risk"})


@pytest.mark.asyncio
async def test_role_strategy_context_has_its_own_fingerprint():
    result, *_ = await _build_ready_thesis()
    ctx = result.role_strategy_context
    assert ctx.context_fingerprint != ctx.candidacy_thesis_result_fingerprint
    assert ctx.context_fingerprint != ctx.job_analysis_result_fingerprint


# -- 98-101. Context fingerprint sensitivity --


def _base_context_kwargs():
    return dict(
        job_analysis_result_fingerprint="a" * 64,
        job_analysis_replay_fingerprint="b" * 64,
        candidacy_thesis_result_fingerprint="c" * 64,
        candidacy_thesis_replay_fingerprint="d" * 64,
        posting_fingerprint="e" * 64,
        role_objective_fingerprint="f" * 64,
        employer_problem_hypothesis_fingerprints=("1" * 64,),
        priority_evidence_categories=(RoleEvidenceCategory.BUSINESS_RESULT,),
        positioning_level=PositioningLevel.MANAGEMENT,
        target_narrative_emphasis=(RoleEvidenceCategory.BUSINESS_RESULT,),
        approved_supporting_fact_fingerprints=("2" * 64,),
    )


def test_context_fingerprint_changes_with_analysis_fingerprint():
    a = compute_role_strategy_context_fingerprint(**_base_context_kwargs())
    kwargs = _base_context_kwargs()
    kwargs["job_analysis_result_fingerprint"] = "9" * 64
    b = compute_role_strategy_context_fingerprint(**kwargs)
    assert a != b


def test_context_fingerprint_changes_with_thesis_fingerprint():
    a = compute_role_strategy_context_fingerprint(**_base_context_kwargs())
    kwargs = _base_context_kwargs()
    kwargs["candidacy_thesis_result_fingerprint"] = "9" * 64
    b = compute_role_strategy_context_fingerprint(**kwargs)
    assert a != b


def test_context_fingerprint_changes_with_evidence_priorities():
    a = compute_role_strategy_context_fingerprint(**_base_context_kwargs())
    kwargs = _base_context_kwargs()
    kwargs["priority_evidence_categories"] = (RoleEvidenceCategory.TEAM_LEADERSHIP,)
    b = compute_role_strategy_context_fingerprint(**kwargs)
    assert a != b


def test_context_fingerprint_changes_with_positioning_level():
    a = compute_role_strategy_context_fingerprint(**_base_context_kwargs())
    kwargs = _base_context_kwargs()
    kwargs["positioning_level"] = PositioningLevel.EXECUTIVE
    b = compute_role_strategy_context_fingerprint(**kwargs)
    assert a != b
