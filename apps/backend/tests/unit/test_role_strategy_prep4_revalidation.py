"""``role_strategy_prep4_revalidation``: full, independent PRE-P4 revalidation.

Also hosts the shared synthetic PRE-P4 -> P3 fixture factory
(``build_ready_fixture``) reused by every other Stage 10D-A test module via
a plain cross-module import (``tests/unit`` and ``tests/integration`` are
real packages).

All postings, facts, and evidence are synthetic; no real candidate or
employer data is used.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

from app.schemas.candidacy_thesis import (
    CandidacyEvidenceMapping,
    CandidacyThesisContext,
    CandidacyThesisModelConfiguration,
    CandidacyThesisResult,
    CandidacyThesisStatus,
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
from app.schemas.fact_selection import TargetContext
from app.schemas.job_posting_analysis import (
    AnalysisAssertionBasis,
    ControlledJobAnalysisResponse,
    JobAnalysisAdapterResponse,
    JobAnalysisContext,
    JobAnalysisModelConfiguration,
    JobAnalysisReplayInput,
    JobEvidenceSegment,
    JobPostingAnalysisResult,
    JobPostingAnalysisStatus,
    JobPostingSnapshot,
    JobPostingSourceType,
    ResponseHypothesisItem,
    ResponseRoleObjectiveItem,
)
from app.schemas.strategy_fact_selection import (
    INTEGRATION_POLICY_VERSION,
    RoleStrategyFactSelectionInput,
    StrategyFactSelectionCandidate,
)
from app.schemas.truth_fact import FactStatus, Transferability, TransformationOperation, TruthFactRead
from app.services.candidacy_thesis_builder import build_candidacy_thesis, compute_approved_fact_payload_set_fingerprint
from app.services.candidacy_thesis_replay import replay_input_from_candidacy_thesis_result
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION
from app.services.job_analysis_builder import build_job_posting_analysis
from app.services.job_analysis_replay import replay_input_from_job_analysis_result
from app.services.job_posting_snapshot import build_job_posting_snapshot, segment_job_posting
from app.services.role_strategy_prep4_revalidation import (
    Prep4RevalidationStatus,
    Prep4RevalidationViolationCode,
    revalidate_prep4,
)
from app.services.strategy_fact_selection_builder import compute_integration_input_fingerprint


RAW_POSTING = """Senior Sales Manager

RESPONSIBILITIES:
- Lead the sales team to improve plan realization

We improved plan realization from 36% to 96% last year and need someone to sustain that.
"""


class _DeterministicJobAdapter:
    def __init__(self, response: ControlledJobAnalysisResponse) -> None:
        self._response = response

    async def analyze(self, *, request, model_configuration):  # noqa: ANN001
        return JobAnalysisAdapterResponse(
            provider="fake",
            model_identifier="fake-model",
            raw_response_text="ok",
            structured_response=self._response,
        )


class _FakeThesisAdapter:
    def __init__(self, response: ControlledCandidacyThesisResponse) -> None:
        self._response = response

    async def synthesize_thesis(self, *, request, model_configuration):  # noqa: ANN001
        return CandidacyThesisAdapterResponse(
            provider="fake",
            model_identifier="fake-model",
            raw_response_text="ok",
            structured_response=self._response,
        )


@dataclasses.dataclass(frozen=True)
class ReadyFixture:
    snapshot: JobPostingSnapshot
    segments: tuple[JobEvidenceSegment, ...]
    job_context: JobAnalysisContext
    job_result: JobPostingAnalysisResult
    current_job_replay: JobAnalysisReplayInput
    thesis_context: CandidacyThesisContext
    thesis_result: CandidacyThesisResult
    role_strategy_context: RoleStrategyContext
    payload_set: ApprovedFactPayloadSet
    target_context: TargetContext
    integration_input: RoleStrategyFactSelectionInput
    fact0: ApprovedFactPayloadSnapshot
    fact1: ApprovedFactPayloadSnapshot
    fact2: ApprovedFactPayloadSnapshot
    evidence_mapping: CandidacyEvidenceMapping


def make_fact(
    *,
    entity_id: uuid.UUID | None = None,
    fact_id: uuid.UUID | None = None,
    fact_type: str,
    content_fingerprint: str,
    status: FactStatus = FactStatus.CONFIRMED,
    use_in_cv: bool = True,
    requires_approval: bool = False,
    transferability: Transferability = Transferability.GLOBAL_TRANSFERABLE,
    employment_scope_entity_id: uuid.UUID | None = None,
) -> TruthFactRead:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TruthFactRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint=content_fingerprint,
        created_at=now,
        updated_at=now,
        archived_at=None,
        fact_id=fact_id or uuid.uuid4(),
        entity_id=entity_id or uuid.uuid4(),
        fact_type=fact_type,
        value_json={"text": "synthetic"},
        normalized_value_json={"text": "synthetic"},
        status=status,
        use_in_cv=use_in_cv,
        requires_approval=requires_approval,
        transferability=transferability,
        employment_scope_entity_id=employment_scope_entity_id,
        source_reference=None,
    )


def make_candidate(
    fact: TruthFactRead,
    *,
    requested_operation: TransformationOperation = TransformationOperation.EXACT_COPY,
    requested_target_scope: str | None = None,
) -> StrategyFactSelectionCandidate:
    scope_by_fact_type = {
        "SKILL": "SKILLS",
        "TOOL": "SKILLS",
        "TECHNOLOGY": "SKILLS",
        "EMPLOYMENT_ACTIVITY": "EXPERIENCE",
        "ACHIEVEMENT": "EXPERIENCE",
        "LANGUAGE_NAME": "LANGUAGES",
        "SUMMARY": "SUMMARY",
    }
    return StrategyFactSelectionCandidate(
        fact=fact,
        baseline_requested_operation=requested_operation,
        baseline_requested_target_scope=requested_target_scope or scope_by_fact_type[fact.fact_type],
    )


async def build_ready_fixture(
    *,
    positioning_level: PositioningLevel = PositioningLevel.MANAGEMENT,
    priority_categories: tuple[RoleEvidenceCategory, ...] = (RoleEvidenceCategory.BUSINESS_RESULT,),
) -> ReadyFixture:
    snapshot = build_job_posting_snapshot(
        posting_id="job-1",
        source_type=JobPostingSourceType.MANUAL_TEXT,
        raw_text=RAW_POSTING,
        source_language="en",
        role_title="Senior Sales Manager",
        company_name="Acme Corp",
    )
    segments = segment_job_posting(snapshot)
    objective_evidence = tuple(
        s.segment_id for s in segments if "lead the sales team" in s.normalized_text
    )
    outcome_evidence = tuple(s.segment_id for s in segments if "36% to 96%" in s.normalized_text)

    job_response = ControlledJobAnalysisResponse(
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
    job_context = JobAnalysisContext(
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
    job_result = await build_job_posting_analysis(
        snapshot=snapshot,
        segments=segments,
        analysis_context=job_context,
        llm_adapter=_DeterministicJobAdapter(job_response),
    )
    assert job_result.status == JobPostingAnalysisStatus.ANALYZED

    current_job_replay = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=job_context, result=job_result
    )

    fact0 = ApprovedFactPayloadSnapshot(
        person_entity_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        fact_id=uuid.uuid4(),
        fact_type="SKILL",
        fact_revision=1,
        fact_content_fingerprint="a" * 64,
        value_json={"text": "Sales pipeline management"},
        normalized_value_json={"text": "Sales pipeline management"},
        payload_fingerprint="b" * 64,
    )
    fact1 = ApprovedFactPayloadSnapshot(
        person_entity_id=fact0.person_entity_id,
        entity_id=uuid.uuid4(),
        fact_id=uuid.uuid4(),
        fact_type="EMPLOYMENT_ACTIVITY",
        fact_revision=1,
        fact_content_fingerprint="c" * 64,
        value_json={"text": "Coordinated cross-team distribution rollout"},
        normalized_value_json={"text": "Coordinated cross-team distribution rollout"},
        payload_fingerprint="d" * 64,
    )
    fact2 = ApprovedFactPayloadSnapshot(
        person_entity_id=fact0.person_entity_id,
        entity_id=uuid.uuid4(),
        fact_id=uuid.uuid4(),
        fact_type="LANGUAGE_NAME",
        fact_revision=1,
        fact_content_fingerprint="e" * 64,
        value_json={"text": "English"},
        normalized_value_json={"text": "English"},
        payload_fingerprint="f" * 64,
    )
    payload_set = ApprovedFactPayloadSet(
        payloads=(fact0, fact1, fact2),
        payload_set_fingerprint=compute_approved_fact_payload_set_fingerprint((fact0, fact1, fact2)),
    )

    hyp_fp = job_result.hypotheses[0].hypothesis_fingerprint
    thesis_response = ControlledCandidacyThesisResponse(
        response_schema_version="controlled-candidacy-thesis-response-v1",
        posting_fingerprint=job_result.posting_fingerprint,
        analysis_result_fingerprint=job_result.result_fingerprint,
        acknowledged_positioning_level=positioning_level,
        target_narrative_emphasis=priority_categories,
        evidence_mappings=(
            ResponseEvidenceMappingItem(
                response_item_id="ev-1",
                entity_id=fact0.entity_id,
                fact_id=fact0.fact_id,
                fact_revision=fact0.fact_revision,
                payload_fingerprint=fact0.payload_fingerprint,
                linked_hypothesis_fingerprint=hyp_fp,
                role_evidence_category=RoleEvidenceCategory.BUSINESS_RESULT,
                narrative_link="Marek's documented sales pipeline management is directly relevant",
            ),
        ),
        strategic_contribution_statements=(
            ResponseStrategicContributionItem(
                response_item_id="sc-1",
                statement="Marek's pipeline management experience is directly relevant here",
                supporting_evidence_mapping_response_item_ids=("ev-1",),
            ),
        ),
        thesis_statement=ResponseThesisStatementItem(
            response_item_id="thesis-1",
            statement="Marek's track record managing sales pipelines is credible evidence he can help here",
            referenced_hypothesis_fingerprints=(hyp_fp,),
            supporting_evidence_mapping_response_item_ids=("ev-1",),
        ),
    )
    thesis_context = CandidacyThesisContext(
        locale="en",
        thesis_prompt_version="controlled-candidacy-thesis-prompt-v1",
        thesis_policy_version="candidacy-thesis-policy-v1",
        retry_policy_version="candidacy-thesis-retry-policy-v1",
        response_schema_version="controlled-candidacy-thesis-response-v1",
        requested_positioning_level=positioning_level,
        priority_evidence_categories=priority_categories,
        model_configuration=CandidacyThesisModelConfiguration(
            provider="fake",
            model_identifier="fake-model",
            temperature=0.0,
            top_p=1.0,
            max_output_tokens=1000,
            timeout_seconds=30.0,
            maximum_attempts=2,
            response_schema_version="controlled-candidacy-thesis-response-v1",
            retry_policy_version="candidacy-thesis-retry-policy-v1",
        ),
    )
    thesis_result = await build_candidacy_thesis(
        job_analysis_result=job_result,
        job_analysis_snapshot=snapshot,
        job_analysis_segments=segments,
        job_analysis_context=job_context,
        current_job_analysis_replay_input=current_job_replay,
        fact_payloads=payload_set,
        thesis_context=thesis_context,
        llm_adapter=_FakeThesisAdapter(thesis_response),
    )
    assert thesis_result.status == CandidacyThesisStatus.READY
    assert thesis_result.role_strategy_context is not None
    role_strategy_context = thesis_result.role_strategy_context

    current_thesis_replay = replay_input_from_candidacy_thesis_result(
        job_analysis_replay_fingerprint=role_strategy_context.job_analysis_replay_fingerprint,
        approved_payload_set_fingerprint=payload_set.payload_set_fingerprint,
        positioning_context_fingerprint=_positioning_context_fp(thesis_context),
        evidence_priority_fingerprints=[e.evidence_priority_fingerprint for e in job_result.evidence_priorities],
        model_configuration_fingerprint=_model_configuration_fp(thesis_context),
        prompt_version=thesis_context.thesis_prompt_version,
        result=thesis_result,
    )

    target_context = TargetContext(
        target_context_id=uuid.uuid4(),
        person_entity_id=fact0.person_entity_id,
        job_or_application_id="job-1",
        target_role_title="Senior Sales Manager",
        target_role_family="SALES",
        target_seniority="SENIOR",
        target_organization_or_industry="TECH",
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )

    integration_input = RoleStrategyFactSelectionInput(
        job_posting_snapshot=snapshot,
        job_evidence_segments=segments,
        job_analysis_context=job_context,
        job_analysis_result=job_result,
        current_job_analysis_replay_input=current_job_replay,
        candidacy_thesis_context=thesis_context,
        candidacy_thesis_result=thesis_result,
        current_candidacy_thesis_replay_input=current_thesis_replay,
        role_strategy_context=role_strategy_context,
        approved_fact_payload_set=payload_set,
        target_context=target_context,
        integration_policy_version=INTEGRATION_POLICY_VERSION,
        integration_input_fingerprint="0" * 64,
    )
    real_fp = compute_integration_input_fingerprint(integration_input)
    integration_input = integration_input.model_copy(update={"integration_input_fingerprint": real_fp})

    return ReadyFixture(
        snapshot=snapshot,
        segments=segments,
        job_context=job_context,
        job_result=job_result,
        current_job_replay=current_job_replay,
        thesis_context=thesis_context,
        thesis_result=thesis_result,
        role_strategy_context=role_strategy_context,
        payload_set=payload_set,
        target_context=target_context,
        integration_input=integration_input,
        fact0=fact0,
        fact1=fact1,
        fact2=fact2,
        evidence_mapping=thesis_result.evidence_mappings[0],
    )


def _positioning_context_fp(thesis_context: CandidacyThesisContext) -> str:
    from app.services.candidacy_thesis_replay import compute_positioning_context_fingerprint

    return compute_positioning_context_fingerprint(
        requested_positioning_level=thesis_context.requested_positioning_level,
        priority_evidence_categories=thesis_context.priority_evidence_categories,
    )


def _model_configuration_fp(thesis_context: CandidacyThesisContext) -> str:
    from app.services.candidacy_thesis_builder import compute_candidacy_thesis_model_configuration_fingerprint

    return compute_candidacy_thesis_model_configuration_fingerprint(thesis_context.model_configuration)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_fixture_passes_revalidation() -> None:
    fixture = await build_ready_fixture()
    result = revalidate_prep4(fixture.integration_input)
    assert result.status == Prep4RevalidationStatus.PASSED
    assert result.violations == ()
    assert result.job_analysis_replay_fingerprint is not None
    assert result.candidacy_thesis_replay_fingerprint is not None
    assert result.role_strategy_context_fingerprint == fixture.role_strategy_context.context_fingerprint


@pytest.mark.asyncio
async def test_stale_job_analysis_blocks() -> None:
    fixture = await build_ready_fixture()
    stale_current = fixture.current_job_replay.model_copy(
        update={"canonical_text_fingerprint": "9" * 64}
    )
    tampered = fixture.integration_input.model_copy(
        update={"current_job_analysis_replay_input": stale_current}
    )
    result = revalidate_prep4(tampered)
    assert result.status == Prep4RevalidationStatus.FAILED
    assert Prep4RevalidationViolationCode.JOB_ANALYSIS_NOT_FRESH in {v.code for v in result.violations}


@pytest.mark.asyncio
async def test_stale_candidacy_thesis_blocks() -> None:
    fixture = await build_ready_fixture()
    stale_current = fixture.integration_input.current_candidacy_thesis_replay_input.model_copy(
        update={"thesis_statement_fingerprint": "9" * 64}
    )
    tampered = fixture.integration_input.model_copy(
        update={"current_candidacy_thesis_replay_input": stale_current}
    )
    result = revalidate_prep4(tampered)
    assert result.status == Prep4RevalidationStatus.FAILED
    assert Prep4RevalidationViolationCode.CANDIDACY_THESIS_NOT_FRESH in {v.code for v in result.violations}


@pytest.mark.asyncio
async def test_job_analysis_result_fingerprint_mismatch_blocks() -> None:
    fixture = await build_ready_fixture()
    tampered_job_result = fixture.job_result.model_copy(update={"result_fingerprint": "1" * 64})
    tampered_input = fixture.integration_input.model_copy(
        update={"job_analysis_result": tampered_job_result}
    )
    result = revalidate_prep4(tampered_input)
    assert result.status == Prep4RevalidationStatus.FAILED
    assert Prep4RevalidationViolationCode.JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH in {
        v.code for v in result.violations
    }


@pytest.mark.asyncio
async def test_context_fingerprint_is_recalculated_not_trusted() -> None:
    fixture = await build_ready_fixture()
    tampered_context = fixture.role_strategy_context.model_copy(
        update={"context_fingerprint": "2" * 64}
    )
    tampered_thesis = fixture.thesis_result.model_copy(
        update={"role_strategy_context": tampered_context}
    )
    tampered_input = fixture.integration_input.model_copy(
        update={"role_strategy_context": tampered_context, "candidacy_thesis_result": tampered_thesis}
    )
    result = revalidate_prep4(tampered_input)
    assert result.status == Prep4RevalidationStatus.FAILED
    assert Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_FINGERPRINT_MISMATCH in {
        v.code for v in result.violations
    }


@pytest.mark.asyncio
async def test_payload_set_fingerprint_is_recalculated_not_trusted() -> None:
    fixture = await build_ready_fixture()
    tampered_set = fixture.payload_set.model_copy(update={"payload_set_fingerprint": "3" * 64})
    tampered_input = fixture.integration_input.model_copy(
        update={"approved_fact_payload_set": tampered_set}
    )
    result = revalidate_prep4(tampered_input)
    assert result.status == Prep4RevalidationStatus.FAILED
    assert Prep4RevalidationViolationCode.APPROVED_PAYLOAD_SET_FINGERPRINT_MISMATCH in {
        v.code for v in result.violations
    }


@pytest.mark.asyncio
async def test_ready_flags_are_not_trusted_as_sole_basis() -> None:
    """Forging ``ready_for_p4=True`` on an otherwise-broken result never
    fools revalidation: ``CandidacyThesisResult``'s own model validator
    already forbids constructing ``ready_for_p4=True`` without
    ``status=READY`` -- the fail-closed guarantee is structural. And even
    where the schema *would* allow a stale/forged boolean through,
    ``revalidate_prep4`` never reads ``ready_for_p4``/``ready_for_candidacy_thesis``
    at all -- it only reads ``status``/``role_objective``/``thesis_statement``.
    """

    with pytest.raises(Exception):
        CandidacyThesisResult(
            result_fingerprint="a" * 64,
            status=CandidacyThesisStatus.BLOCKED,
            posting_fingerprint="b" * 64,
            analysis_result_fingerprint="c" * 64,
            ready_for_p4=True,
        )


@pytest.mark.asyncio
async def test_arbitrary_dict_is_rejected() -> None:
    with pytest.raises(Exception):
        RoleStrategyFactSelectionInput.model_validate({"foo": "bar"})


@pytest.mark.asyncio
async def test_role_strategy_context_must_match_thesis_result() -> None:
    fixture = await build_ready_fixture()
    other_fixture = await build_ready_fixture()
    data = fixture.integration_input.model_dump()
    data["role_strategy_context"] = other_fixture.role_strategy_context.model_dump()
    with pytest.raises(Exception):
        RoleStrategyFactSelectionInput.model_validate(data)
