"""``build_controlled_candidacy_thesis_request`` / prompt fingerprinting:
the request carries only the fresh analysis's role objective and
hypotheses plus the caller-supplied approved facts, always declares the
complete closed prohibition set, and never carries a pending/rejected fact
or the full Truth Library.

All facts and postings are synthetic; no real candidate or employer data
is used.
"""

from __future__ import annotations

import uuid

from app.schemas.candidacy_thesis import (
    ALL_CANDIDACY_THESIS_PROHIBITIONS,
    PositioningLevel,
    RoleEvidenceCategory,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSnapshot
from app.schemas.job_posting_analysis import AnalysisAssertionBasis, EmployerProblemHypothesis, RoleObjective
from app.services.candidacy_thesis_prompt import (
    build_controlled_candidacy_thesis_request,
    compute_candidacy_thesis_prompt_fingerprint,
)


def _role_objective() -> RoleObjective:
    return RoleObjective(
        objective_id="a" * 64,
        concise_statement="Drive enterprise sales execution",
        assertion_basis=AnalysisAssertionBasis.EXPLICIT_IN_POSTING,
        supporting_evidence_segment_ids=("b" * 64,),
        objective_fingerprint="a" * 64,
    )


def _hypothesis() -> EmployerProblemHypothesis:
    from app.schemas.job_posting_analysis import OverclaimGuardStatus

    return EmployerProblemHypothesis(
        hypothesis_id="c" * 64,
        problem_statement="Needs to sustain a recent improvement in plan realization",
        affected_business_area="Sales",
        observable_signals_in_posting=("improved from 36% to 96%",),
        supporting_evidence_segment_ids=("b" * 64,),
        overclaim_guard_status=OverclaimGuardStatus.PASSED,
        hypothesis_fingerprint="c" * 64,
    )


def _approved_fact() -> ApprovedFactPayloadSnapshot:
    return ApprovedFactPayloadSnapshot(
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


def _build_request(**overrides):
    defaults = dict(
        posting_fingerprint="f" * 64,
        analysis_result_fingerprint="1" * 64,
        role_objective=_role_objective(),
        hypotheses=(_hypothesis(),),
        approved_facts=(_approved_fact(),),
        requested_positioning_level=PositioningLevel.MANAGEMENT,
        priority_evidence_categories=(RoleEvidenceCategory.BUSINESS_RESULT,),
        prompt_template_version="controlled-candidacy-thesis-prompt-v1",
        response_schema_version="controlled-candidacy-thesis-response-v1",
    )
    defaults.update(overrides)
    return build_controlled_candidacy_thesis_request(**defaults)


def test_request_carries_only_role_objective_and_hypothesis_projections():
    request = _build_request()
    assert request.role_objective_fingerprint == "a" * 64
    assert len(request.hypotheses) == 1
    assert request.hypotheses[0].hypothesis_fingerprint == "c" * 64


def test_request_carries_only_supplied_approved_facts():
    fact = _approved_fact()
    request = _build_request(approved_facts=(fact,))
    assert len(request.approved_facts) == 1
    assert request.approved_facts[0].fact_id == fact.fact_id


def test_request_always_declares_the_complete_prohibition_set():
    request = _build_request()
    assert set(request.prohibitions) == set(ALL_CANDIDACY_THESIS_PROHIBITIONS)


def test_request_carries_no_pending_rejected_or_truth_library_fields():
    field_names = set(type(_build_request()).model_fields)
    assert field_names.isdisjoint(
        {"pending_facts", "rejected_facts", "truth_library", "master_resume", "other_postings"}
    )


def test_prompt_fingerprint_is_stable_for_identical_request():
    fact = _approved_fact()
    a = _build_request(approved_facts=(fact,))
    b = _build_request(approved_facts=(fact,))
    assert compute_candidacy_thesis_prompt_fingerprint(
        a
    ) == compute_candidacy_thesis_prompt_fingerprint(b)


def test_prompt_fingerprint_changes_with_a_different_approved_fact():
    other_fact = _approved_fact()
    a = _build_request()
    b = _build_request(approved_facts=(other_fact,))
    assert compute_candidacy_thesis_prompt_fingerprint(
        a
    ) != compute_candidacy_thesis_prompt_fingerprint(b)


def test_prompt_fingerprint_changes_with_positioning_level():
    a = _build_request(requested_positioning_level=PositioningLevel.MANAGEMENT)
    b = _build_request(requested_positioning_level=PositioningLevel.EXECUTIVE)
    assert compute_candidacy_thesis_prompt_fingerprint(
        a
    ) != compute_candidacy_thesis_prompt_fingerprint(b)
