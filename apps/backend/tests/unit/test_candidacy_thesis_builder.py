"""``build_candidacy_thesis``: freshness gating against the Job Analysis,
approved-facts-only evidence, identity/provenance re-verification, and the
thesis-safety guard.

All postings, facts, and evidence are synthetic; no real candidate or
employer data is used.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.schemas.candidacy_thesis import (
    CandidacyEvidenceMapping,
    CandidacyThesisContext,
    CandidacyThesisModelConfiguration,
    CandidacyThesisProviderErrorCode,
    CandidacyThesisStatus,
    CandidacyThesisViolationCode,
    ControlledCandidacyThesisResponse,
    PositioningLevel,
    ResponseEvidenceMappingItem,
    ResponseStrategicContributionItem,
    ResponseThesisStatementItem,
    RoleEvidenceCategory,
    CandidacyThesisAdapterResponse,
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


async def _build_job_analysis(raw_text: str = RAW_POSTING):
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=raw_text,
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
    assert result.status.value == "ANALYZED"
    current_replay = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=context, result=result
    )
    return snapshot, segments, context, result, current_replay


def _approved_fact(**overrides) -> ApprovedFactPayloadSnapshot:
    defaults = dict(
        person_entity_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        fact_id=uuid.uuid4(),
        fact_type="ACHIEVEMENT",
        fact_revision=1,
        fact_content_fingerprint="d" * 64,
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


def _thesis_response(job_result, fact, positioning=PositioningLevel.MANAGEMENT, **overrides):
    hyp_fp = job_result.hypotheses[0].hypothesis_fingerprint
    defaults = dict(
        response_schema_version="controlled-candidacy-thesis-response-v1",
        posting_fingerprint=job_result.posting_fingerprint,
        analysis_result_fingerprint=job_result.result_fingerprint,
        acknowledged_positioning_level=positioning,
        target_narrative_emphasis=(RoleEvidenceCategory.BUSINESS_RESULT,),
        evidence_mappings=(
            ResponseEvidenceMappingItem(
                response_item_id="ev-1",
                entity_id=fact.entity_id,
                fact_id=fact.fact_id,
                fact_revision=fact.fact_revision,
                payload_fingerprint=fact.payload_fingerprint,
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
    defaults.update(overrides)
    return ControlledCandidacyThesisResponse(**defaults)


async def _run_thesis(job_result, snapshot, segments, job_context, current_replay, fact_set, response, thesis_context=None):
    adapter = ScriptedThesisAdapter(
        [
            CandidacyThesisAdapterResponse(
                provider="fake", model_identifier="fake-model", raw_response_text="ok",
                structured_response=response,
            )
        ]
    )
    result = await build_candidacy_thesis(
        job_analysis_result=job_result,
        job_analysis_snapshot=snapshot,
        job_analysis_segments=segments,
        job_analysis_context=job_context,
        current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set,
        thesis_context=thesis_context or _thesis_context(),
        llm_adapter=adapter,
    )
    return result, adapter


# -- 52-53. Thesis requires a fresh Job Analysis --


@pytest.mark.asyncio
async def test_thesis_requires_current_replay_input_to_be_supplied():
    snapshot, segments, job_context, job_result, _ = await _build_job_analysis()
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    response = _thesis_response(job_result, fact)
    result, adapter = await _run_thesis(
        job_result, snapshot, segments, job_context, None, fact_set, response
    )
    assert result.status == CandidacyThesisStatus.INVALID_INPUT
    assert any(
        v.violation_code == CandidacyThesisViolationCode.JOB_ANALYSIS_FRESHNESS_NOT_VERIFIED
        for v in result.violations
    )
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_stale_job_analysis_blocks_thesis():
    snapshot, segments, job_context, job_result, _ = await _build_job_analysis()
    stale_current_replay = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=job_context, result=job_result
    ).model_copy(update={"prompt_version": "controlled-job-analysis-prompt-v2"})
    fact = _approved_fact()
    fact_set = _fact_set(fact)
    response = _thesis_response(job_result, fact)
    result, adapter = await _run_thesis(
        job_result, snapshot, segments, job_context, stale_current_replay, fact_set, response
    )
    assert result.status == CandidacyThesisStatus.BLOCKED
    assert any(v.violation_code == CandidacyThesisViolationCode.JOB_ANALYSIS_STALE for v in result.violations)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_thesis_requires_job_analysis_ready_for_candidacy_thesis():
    # A job analysis with no role_objective in the response is a genuinely
    # BLOCKED, self-consistent result (fingerprint included) -- never a
    # hand-tampered one.
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=RAW_POSTING,
        source_language="en", role_title="Senior Sales Manager", company_name="Acme Corp",
    )
    segments = segment_job_posting(snapshot)
    response = ControlledJobAnalysisResponse(
        response_schema_version="controlled-job-analysis-response-v1",
        posting_fingerprint=snapshot.snapshot_fingerprint,
    )
    job_context = _job_context()
    not_ready = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=job_context,
        llm_adapter=_FakeJobAdapter(response),
    )
    assert not_ready.status.value == "BLOCKED"
    assert not_ready.ready_for_candidacy_thesis is False
    current_replay = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=job_context, result=not_ready
    )

    fact = _approved_fact()
    fact_set = _fact_set(fact)
    # There is no role_objective/hypothesis to build a meaningful thesis
    # response from, but the freshness/readiness gate must reject before
    # the adapter is ever consulted.
    adapter = ScriptedThesisAdapter([])
    from app.services.candidacy_thesis_builder import build_candidacy_thesis as _build

    result = await _build(
        job_analysis_result=not_ready,
        job_analysis_snapshot=snapshot,
        job_analysis_segments=segments,
        job_analysis_context=job_context,
        current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set,
        thesis_context=_thesis_context(),
        llm_adapter=adapter,
    )
    assert result.status == CandidacyThesisStatus.BLOCKED
    assert any(
        v.violation_code == CandidacyThesisViolationCode.JOB_ANALYSIS_NOT_READY for v in result.violations
    )
    assert adapter.calls == []


# -- 54-56. Thesis uses only approved facts; pending/rejected facts cannot
# influence it because they are simply never in the supplied set --


@pytest.mark.asyncio
async def test_evidence_mapping_referencing_a_fact_outside_the_approved_set_is_unknown():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    approved_fact = _approved_fact()
    not_supplied_fact_id = uuid.uuid4()  # e.g. a pending/rejected fact never handed to the builder
    response = _thesis_response(job_result, approved_fact)
    tampered_mapping = response.evidence_mappings[0].model_copy(update={"fact_id": not_supplied_fact_id})
    response = response.model_copy(update={"evidence_mappings": (tampered_mapping,)})
    fact_set = _fact_set(approved_fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert any(b.violation_code == CandidacyThesisViolationCode.UNKNOWN_FACT for b in result.blocked_items)


def test_build_candidacy_thesis_signature_has_no_pending_or_rejected_facts_parameter():
    signature = inspect.signature(build_candidacy_thesis)
    assert "pending_facts" not in signature.parameters
    assert "rejected_facts" not in signature.parameters
    assert "master_resume" not in signature.parameters


# -- 63-65. Unknown fact / missing fact / payload mismatch block --


@pytest.mark.asyncio
async def test_missing_fact_entity_mismatch_blocks():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    tampered_mapping = response.evidence_mappings[0].model_copy(update={"entity_id": uuid.uuid4()})
    response = response.model_copy(update={"evidence_mappings": (tampered_mapping,)})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert any(b.violation_code == CandidacyThesisViolationCode.MISSING_FACT for b in result.blocked_items)


@pytest.mark.asyncio
async def test_payload_fingerprint_mismatch_blocks():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    tampered_mapping = response.evidence_mappings[0].model_copy(update={"payload_fingerprint": "9" * 64})
    response = response.model_copy(update={"evidence_mappings": (tampered_mapping,)})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert any(b.violation_code == CandidacyThesisViolationCode.PAYLOAD_MISMATCH for b in result.blocked_items)


# -- 66-67. Unknown hypothesis / unknown role objective block --


@pytest.mark.asyncio
async def test_unknown_hypothesis_reference_blocks():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    tampered_mapping = response.evidence_mappings[0].model_copy(
        update={"linked_hypothesis_fingerprint": "7" * 64}
    )
    response = response.model_copy(update={"evidence_mappings": (tampered_mapping,)})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert any(
        b.violation_code == CandidacyThesisViolationCode.UNKNOWN_HYPOTHESIS for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_unknown_role_objective_reference_blocks():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    tampered_mapping = response.evidence_mappings[0].model_copy(
        update={
            "linked_hypothesis_fingerprint": None,
            "linked_role_objective_fingerprint": "8" * 64,
        }
    )
    response = response.model_copy(update={"evidence_mappings": (tampered_mapping,)})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert any(
        b.violation_code == CandidacyThesisViolationCode.UNKNOWN_ROLE_OBJECTIVE
        for b in result.blocked_items
    )


# -- 70. Duplicate evidence mapping blocks --


@pytest.mark.asyncio
async def test_duplicate_evidence_mapping_blocks():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    duplicate_item = response.evidence_mappings[0].model_copy(update={"response_item_id": "ev-2"})
    response = response.model_copy(
        update={"evidence_mappings": response.evidence_mappings + (duplicate_item,)}
    )
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert any(
        b.violation_code == CandidacyThesisViolationCode.DUPLICATE_EVIDENCE_MAPPING
        for b in result.blocked_items
    )
    assert len(result.evidence_mappings) == 1


# -- 73-77. Thesis safety: no new facts, no future guarantees, no scoring --


@pytest.mark.asyncio
async def test_future_outcome_guarantee_blocks_thesis_statement():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    guaranteed = response.thesis_statement.model_copy(
        update={"statement": "Marek is guaranteed to improve results to 96%"}
    )
    response = response.model_copy(update={"thesis_statement": guaranteed})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert result.thesis_statement is None
    assert any(
        b.violation_code == CandidacyThesisViolationCode.FUTURE_OUTCOME_GUARANTEE
        for b in result.blocked_items
    )
    assert result.status != CandidacyThesisStatus.READY


@pytest.mark.asyncio
async def test_bare_future_result_verb_blocks_thesis_statement():
    # "zwiększy" carries no "na pewno"/"z pewnością" hedge word and no
    # number -- it is blocked purely as an unambiguous future-result verb.
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    updated = response.thesis_statement.model_copy(
        update={"statement": "Marek zwiększy sprzedaż w tej roli"}
    )
    response = response.model_copy(update={"thesis_statement": updated})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert result.thesis_statement is None
    assert any(
        b.violation_code == CandidacyThesisViolationCode.FUTURE_OUTCOME_GUARANTEE
        for b in result.blocked_items
    )
    assert result.status != CandidacyThesisStatus.READY


@pytest.mark.asyncio
async def test_future_numeric_kpi_claim_blocks_strategic_contribution():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    updated = response.strategic_contribution_statements[0].model_copy(
        update={"statement": "Marek osiągnie 96% realizacji planu w nowej roli"}
    )
    response = response.model_copy(update={"strategic_contribution_statements": (updated,)})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert result.strategic_contribution_statements == ()
    assert any(
        b.violation_code == CandidacyThesisViolationCode.FUTURE_OUTCOME_GUARANTEE
        for b in result.blocked_items
    )


@pytest.mark.asyncio
async def test_accepted_thesis_statement_text_is_not_rewritten_by_the_guard():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert result.thesis_statement is not None
    assert result.thesis_statement.statement == response.thesis_statement.statement


@pytest.mark.asyncio
async def test_perfect_candidate_language_blocks_strategic_contribution():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    perfect = response.strategic_contribution_statements[0].model_copy(
        update={"statement": "Marek is the perfect candidate for this exact role"}
    )
    response = response.model_copy(update={"strategic_contribution_statements": (perfect,)})
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert result.strategic_contribution_statements == ()
    assert any(
        b.violation_code == CandidacyThesisViolationCode.UNSUPPORTED_CLAIM for b in result.blocked_items
    )


# -- successful path: READY + ready_for_p4 --


@pytest.mark.asyncio
async def test_valid_thesis_is_ready_for_p4():
    snapshot, segments, job_context, job_result, current_replay = await _build_job_analysis()
    fact = _approved_fact()
    response = _thesis_response(job_result, fact)
    fact_set = _fact_set(fact)
    result, _ = await _run_thesis(
        job_result, snapshot, segments, job_context, current_replay, fact_set, response
    )
    assert result.status == CandidacyThesisStatus.READY
    assert result.ready_for_p4 is True
    assert result.role_strategy_context is not None


# -- 92. ready_for_p4 is never caller-supplied --


def test_build_candidacy_thesis_signature_has_no_caller_settable_ready_flag():
    signature = inspect.signature(build_candidacy_thesis)
    assert "ready_for_p4" not in signature.parameters
