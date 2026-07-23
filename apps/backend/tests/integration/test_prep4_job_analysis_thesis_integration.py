"""End-to-end PRE-P4 integration: Job Posting Analysis feeding a fresh
Candidacy Thesis feeding a ``RoleStrategyContext``, plus the source-level
isolation matrix that every PRE-P4 core module must satisfy.

This stage does not modify P4 and does not yet wire ``RoleStrategyContext``
into P4 -- that is the deferred, mandatory PRE-P4-to-P4 integration gate
described in ``docs/explicit-provenance-stage-pre-p4.md``. Existing P4/P5a/
P5b/P5.5 high-risk suites are run separately (see that document) and are
not duplicated here.

All postings, facts, and evidence are synthetic; no real candidate or
employer data is used.
"""

from __future__ import annotations

import inspect
import re
import uuid

import pytest

from app.schemas.candidacy_thesis import (
    CandidacyThesisContext,
    CandidacyThesisModelConfiguration,
    CandidacyThesisStatus,
    CandidacyThesisAdapterResponse,
    ControlledCandidacyThesisResponse,
    PositioningLevel,
    ResponseEvidenceMappingItem,
    ResponseStrategicContributionItem,
    ResponseThesisStatementItem,
    RoleEvidenceCategory,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSet, ApprovedFactPayloadSnapshot
from app.schemas.job_posting_analysis import (
    AnalysisAssertionBasis,
    ControlledJobAnalysisResponse,
    JobAnalysisAdapterResponse,
    JobAnalysisContext,
    JobAnalysisModelConfiguration,
    JobPostingAnalysisStatus,
    JobPostingSourceType,
    ResponseHypothesisItem,
    ResponseRoleObjectiveItem,
)
from app.services import (
    candidacy_thesis_builder,
    candidacy_thesis_llm,
    candidacy_thesis_policy,
    candidacy_thesis_prompt,
    candidacy_thesis_replay,
    job_analysis_builder,
    job_analysis_llm,
    job_analysis_policy,
    job_analysis_prompt,
    job_analysis_replay,
    job_posting_snapshot,
)
from app.services.candidacy_thesis_builder import (
    build_candidacy_thesis,
    compute_approved_fact_payload_set_fingerprint,
)
from app.services.job_analysis_builder import build_job_posting_analysis
from app.services.job_analysis_replay import replay_input_from_job_analysis_result
from app.services.job_posting_snapshot import build_job_posting_snapshot, segment_job_posting


CORE_MODULES = [
    job_posting_snapshot,
    job_analysis_policy,
    job_analysis_prompt,
    job_analysis_llm,
    job_analysis_builder,
    job_analysis_replay,
    candidacy_thesis_policy,
    candidacy_thesis_prompt,
    candidacy_thesis_llm,
    candidacy_thesis_builder,
    candidacy_thesis_replay,
]

RAW_POSTING = """Senior Sales Manager

RESPONSIBILITIES:
- Lead the sales team to improve plan realization

We improved plan realization from 36% to 96% last year and need someone to sustain that.
"""


# -- ISOLATION: 102-108 --


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_app_llm(module):
    import_lines = [
        line.strip()
        for line in inspect.getsource(module).splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]
    for line in import_lines:
        assert "app.llm" not in line
        assert "litellm" not in line


def _import_lines(module) -> list[str]:
    return [
        line.strip()
        for line in inspect.getsource(module).splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_database_or_sqlalchemy(module):
    for line in _import_lines(module):
        assert "sqlalchemy" not in line.lower()
        assert "app.database" not in line
        assert "app.db_engine" not in line


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_router_frontend_or_document_rendering(module):
    for line in _import_lines(module):
        assert "app.routers" not in line
        assert "fastapi" not in line.lower()
        assert "playwright" not in line.lower()
        assert "docx" not in line.lower()


# -- 109. No source_reference identity anywhere in PRE-P4 core schemas --


def test_no_source_reference_identity_in_pre_p4_schemas():
    from app.schemas import candidacy_thesis, job_posting_analysis

    for module in (job_posting_analysis, candidacy_thesis):
        source = inspect.getsource(module)
        assert "source_reference" not in source


# -- 110. No list index used as the sole identity --


def test_segment_and_response_item_ids_are_never_pure_list_indices():
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=RAW_POSTING,
        source_language="en", role_title="Senior Sales Manager",
    )
    segments = segment_job_posting(snapshot)
    for segment in segments:
        assert segment.segment_id != str(segment.segment_order)
        assert re.match(r"^[0-9a-f]{64}$", segment.segment_id)


# -- 111-112. No wall-clock time or Python hash() in any fingerprint path --


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_uses_datetime_now_or_python_hash(module):
    source = inspect.getsource(module)
    assert "datetime.now(" not in source
    assert re.search(r"(?<!hashlib\.)(?<!\.)\bhash\(", source) is None


# -- 113. Every fingerprint is built from canonical_json_bytes --


@pytest.mark.parametrize(
    "module",
    [
        job_posting_snapshot,
        job_analysis_builder,
        job_analysis_prompt,
        candidacy_thesis_builder,
        candidacy_thesis_prompt,
        candidacy_thesis_replay,
    ],
)
def test_fingerprint_modules_use_canonical_json_bytes(module):
    source = inspect.getsource(module)
    assert "canonical_json_bytes" in source


# -- 114-115. Synthetic test data and deterministic fake adapters --


class _DeterministicJobAdapter:
    def __init__(self, response):
        self._response = response

    async def analyze(self, *, request, model_configuration):
        return JobAnalysisAdapterResponse(
            provider="fake", model_identifier="fake-model", raw_response_text="ok",
            structured_response=self._response,
        )


@pytest.mark.asyncio
async def test_fake_job_adapter_is_deterministic_across_runs():
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=RAW_POSTING,
        source_language="en", role_title="Senior Sales Manager", company_name="Acme Corp",
    )
    segments = segment_job_posting(snapshot)
    evidence = tuple(s.segment_id for s in segments if "lead the sales team" in s.normalized_text)
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
    context = JobAnalysisContext(
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
    result_a = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=context,
        llm_adapter=_DeterministicJobAdapter(response),
    )
    result_b = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=context,
        llm_adapter=_DeterministicJobAdapter(response),
    )
    assert result_a.result_fingerprint == result_b.result_fingerprint


# -- End-to-end: Job Posting Analysis -> Candidacy Thesis -> RoleStrategyContext --


class _FakeThesisAdapter:
    def __init__(self, response):
        self._response = response

    async def synthesize_thesis(self, *, request, model_configuration):
        return CandidacyThesisAdapterResponse(
            provider="fake", model_identifier="fake-model", raw_response_text="ok",
            structured_response=self._response,
        )


@pytest.mark.asyncio
async def test_full_prep4_pipeline_produces_a_ready_role_strategy_context():
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=RAW_POSTING,
        source_language="en", role_title="Senior Sales Manager", company_name="Acme Corp",
    )
    segments = segment_job_posting(snapshot)
    objective_evidence = tuple(s.segment_id for s in segments if "lead the sales team" in s.normalized_text)
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
            provider="fake", model_identifier="fake-model", temperature=0.0, top_p=1.0,
            max_output_tokens=1000, timeout_seconds=30.0, maximum_attempts=2,
            response_schema_version="controlled-job-analysis-response-v1",
            retry_policy_version="job-analysis-retry-policy-v1",
        ),
    )
    job_result = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=job_context,
        llm_adapter=_DeterministicJobAdapter(job_response),
    )
    assert job_result.status == JobPostingAnalysisStatus.ANALYZED
    assert job_result.ready_for_candidacy_thesis is True

    current_replay = replay_input_from_job_analysis_result(
        snapshot=snapshot, segments=segments, analysis_context=job_context, result=job_result
    )

    fact = ApprovedFactPayloadSnapshot(
        person_entity_id=uuid.uuid4(), entity_id=uuid.uuid4(), fact_id=uuid.uuid4(),
        fact_type="ACHIEVEMENT", fact_revision=1, fact_content_fingerprint="d" * 64,
        value_json={"description": "Improved plan realization from 36% to 96%"},
        normalized_value_json={"description": "Improved plan realization from 36% to 96%"},
        payload_fingerprint="e" * 64,
    )
    fact_set = ApprovedFactPayloadSet(
        payloads=(fact,), payload_set_fingerprint=compute_approved_fact_payload_set_fingerprint((fact,))
    )

    hyp_fp = job_result.hypotheses[0].hypothesis_fingerprint
    thesis_response = ControlledCandidacyThesisResponse(
        response_schema_version="controlled-candidacy-thesis-response-v1",
        posting_fingerprint=job_result.posting_fingerprint,
        analysis_result_fingerprint=job_result.result_fingerprint,
        acknowledged_positioning_level=PositioningLevel.MANAGEMENT,
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
    thesis_context = CandidacyThesisContext(
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
    thesis_result = await build_candidacy_thesis(
        job_analysis_result=job_result, job_analysis_snapshot=snapshot, job_analysis_segments=segments,
        job_analysis_context=job_context, current_job_analysis_replay_input=current_replay,
        fact_payloads=fact_set, thesis_context=thesis_context, llm_adapter=_FakeThesisAdapter(thesis_response),
    )

    assert thesis_result.status == CandidacyThesisStatus.READY
    assert thesis_result.ready_for_p4 is True
    assert thesis_result.role_strategy_context is not None
    ctx = thesis_result.role_strategy_context
    assert ctx.job_analysis_result_fingerprint == job_result.result_fingerprint
    assert ctx.candidacy_thesis_result_fingerprint == thesis_result.result_fingerprint
    assert ctx.posting_fingerprint == job_result.posting_fingerprint

    # No candidate scoring anywhere in the final, closed context.
    context_fields = set(type(ctx).model_fields)
    assert context_fields.isdisjoint({"match_score", "fit_score", "candidate_risk", "weaknesses"})


# -- Marek-fact changes never invalidate the Job Analysis --


@pytest.mark.asyncio
async def test_changing_approved_facts_never_changes_job_analysis_fingerprint():
    snapshot = build_job_posting_snapshot(
        posting_id="job-1", source_type=JobPostingSourceType.MANUAL_TEXT, raw_text=RAW_POSTING,
        source_language="en", role_title="Senior Sales Manager", company_name="Acme Corp",
    )
    segments = segment_job_posting(snapshot)
    evidence = tuple(s.segment_id for s in segments if "lead the sales team" in s.normalized_text)
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
    context = JobAnalysisContext(
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
    # Job Analysis has no parameter through which any fact could even be
    # supplied -- it structurally cannot depend on Marek's facts.
    signature = inspect.signature(build_job_posting_analysis)
    assert "fact_payloads" not in signature.parameters
    assert "approved_facts" not in signature.parameters

    result_a = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=context,
        llm_adapter=_DeterministicJobAdapter(response),
    )
    result_b = await build_job_posting_analysis(
        snapshot=snapshot, segments=segments, analysis_context=context,
        llm_adapter=_DeterministicJobAdapter(response),
    )
    assert result_a.result_fingerprint == result_b.result_fingerprint
