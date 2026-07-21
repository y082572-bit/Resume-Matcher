"""P3 + the existing Career Positioning Engine (CPE): advisory-only integration.

The CPE never creates, mutates, approves, or reclassifies a fact; it only
supplies an existing ``CareerPositioningResponse`` as read-only signal
material. P3 builds its own ``CareerPositioningSignal`` from that response
and can only use it to downgrade an otherwise-``SELECTED`` fact to
``NOT_RELEVANT`` -- and only by an explicit, stable ``fact_id``.
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.career_positioning import (
    CompetenciesSchema,
    EvidenceSchema,
    LimitationsSchema,
    NarrativeVariantSchema,
    PositioningDetailsSchema,
    RiskItemSchema,
    RisksSchema,
    StrategySchema,
    CareerPositioningResponse,
)
from app.schemas.fact_selection import CareerPositioningSignal, SelectionDecisionOutcome, TargetContext
from app.schemas.truth_fact import (
    FactStatus,
    PermissionStatus,
    Transferability,
    TransformationOperation,
    TruthFactRead,
    TruthPermissionRead,
)
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, select_fact


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _career_positioning_response() -> CareerPositioningResponse:
    return CareerPositioningResponse(
        generated_at=NOW,
        job_id="job-1",
        job_title="Senior Backend Engineer",
        positioning=PositioningDetailsSchema(
            detected_job_level="EXPERT",
            candidate_profile_level="EXPERT",
            level_gap=0,
            positioning_strategy="EXPERT_AUTHORITY",
            title_treatment="KEEP",
            fit_level="STRONG",
            fit_score=90.0,
            confidence=0.9,
            requires_human_review=False,
        ),
        risks=RisksSchema(
            overqualification=RiskItemSchema(availability="AVAILABLE", level="LOW", rationale=["ok"]),
            underqualification=RiskItemSchema(availability="AVAILABLE", level="NONE", rationale=["ok2"]),
            stability_concern=RiskItemSchema(availability="UNSUPPORTED", level=None, rationale=[]),
            sector_gap=RiskItemSchema(availability="UNSUPPORTED", level=None, rationale=[]),
            functional_gap=RiskItemSchema(availability="UNSUPPORTED", level=None, rationale=[]),
        ),
        competencies=CompetenciesSchema(),
        strategy=StrategySchema(
            recommendation="Emphasize backend leadership and delivery ownership",
            recommended_narrative="EXPERT",
            elements_to_emphasize=["ownership"],
            title_positioning="Senior Backend Engineer",
            summary_positioning="Backend expert with proven delivery",
            narrative_variants=[
                NarrativeVariantSchema(
                    type="EXPERT",
                    availability="AVAILABLE",
                    recommended=True,
                    title_positioning="Senior Backend Engineer",
                    summary_positioning="Backend expert with proven delivery",
                    risk_level="LOW",
                ),
                NarrativeVariantSchema(
                    type="MANAGER", availability="UNSUPPORTED", recommended=False, risk_level="NONE"
                ),
                NarrativeVariantSchema(
                    type="DIRECTOR", availability="UNSUPPORTED", recommended=False, risk_level="NONE"
                ),
            ],
        ),
        evidence=EvidenceSchema(
            used_truth_items_count=1,
            direct_evidence_count=1,
            transferable_evidence_count=0,
            unresolved_items_count=0,
        ),
        limitations=LimitationsSchema(insufficient_data=False, messages=[]),
    )


def _signal_from_response(
    response: CareerPositioningResponse, **overrides
) -> CareerPositioningSignal:
    values = dict(
        source_job_id=response.job_id,
        source_generated_at=response.generated_at,
        target_role_level=response.positioning.detected_job_level,
        target_role_family=None,
        seniority_direction="MAINTAIN",
        flattening_required=False,
        transferability_risk="NONE",
        requires_human_review=response.positioning.requires_human_review,
        not_relevant_fact_ids=(),
    )
    values.update(overrides)
    return CareerPositioningSignal(**values)


def _fact(**overrides) -> TruthFactRead:
    values = dict(
        schema_version="1.0",
        revision=1,
        content_fingerprint="a" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        fact_id=uuid4(),
        entity_id=uuid4(),
        fact_type="ACHIEVEMENT",
        value_json={"text": "Led backend migration"},
        normalized_value_json={"text": "Led backend migration"},
        status=FactStatus.CONFIRMED,
        use_in_cv=True,
        requires_approval=False,
        transferability=Transferability.GLOBAL_TRANSFERABLE,
        employment_scope_entity_id=None,
        source_reference=None,
    )
    values.update(overrides)
    return TruthFactRead(**values)


def _target_context(**overrides) -> TargetContext:
    values = dict(
        target_context_id=uuid4(),
        person_entity_id=uuid4(),
        job_or_application_id="job-1",
        target_role_title="Senior Backend Engineer",
        target_role_family="ENGINEERING",
        target_seniority="SENIOR",
        target_organization_or_industry="SOFTWARE",
        employment_entity_id=None,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )
    values.update(overrides)
    return TargetContext(**values)


def test_uses_existing_career_positioning_response_as_signal_source() -> None:
    response = _career_positioning_response()
    signal = _signal_from_response(response)
    assert signal.source_job_id == response.job_id
    assert signal.source_generated_at == response.generated_at
    assert signal.requires_human_review == response.positioning.requires_human_review


def test_no_second_positioning_engine_is_introduced() -> None:
    import app.services.fact_selection_policy as module

    forbidden_names = {"CareerPositioningEngine", "PositioningEngine", "score_job_level"}
    assert not forbidden_names & set(dir(module))


def test_career_positioning_response_is_not_mutated() -> None:
    response = _career_positioning_response()
    before = response.model_dump()
    _signal_from_response(response, requires_human_review=True, flattening_required=True)
    after = response.model_dump()
    assert before == after


def test_requires_human_review_is_advisory_only() -> None:
    fact = _fact()
    target_context = _target_context()
    common_kwargs = dict(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
    )

    response = _career_positioning_response()
    without_review = select_fact(
        **common_kwargs,
        positioning_signal=_signal_from_response(response, requires_human_review=False),
    )
    with_review = select_fact(
        **common_kwargs,
        positioning_signal=_signal_from_response(response, requires_human_review=True),
    )

    assert without_review.decision == with_review.decision == SelectionDecisionOutcome.SELECTED
    assert without_review.reason_codes == with_review.reason_codes
    assert "CPE_REQUIRES_HUMAN_REVIEW" not in without_review.advisory_flags
    assert "CPE_REQUIRES_HUMAN_REVIEW" in with_review.advisory_flags


def test_confirmed_exact_copy_does_not_require_reapproval() -> None:
    fact = _fact(status=FactStatus.CONFIRMED, use_in_cv=True, requires_approval=False)
    response = _career_positioning_response()
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(response, requires_human_review=True),
    )
    assert decision.decision == SelectionDecisionOutcome.SELECTED
    assert decision.requires_user_approval is False


def test_explicit_not_relevant_only_via_stable_fact_id() -> None:
    fact = _fact()
    other_fact = _fact()
    target_context = _target_context()
    response = _career_positioning_response()

    flagged = select_fact(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(
            response, not_relevant_fact_ids=(fact.fact_id,)
        ),
    )
    assert flagged.decision == SelectionDecisionOutcome.NOT_RELEVANT

    # Changing free text (job title / target role title) must never trigger
    # NOT_RELEVANT for a fact whose id was not explicitly flagged.
    unflagged = select_fact(
        fact=other_fact,
        target_context=_target_context(target_role_title="Totally Different Title"),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(
            response, not_relevant_fact_ids=(fact.fact_id,)
        ),
    )
    assert unflagged.decision == SelectionDecisionOutcome.SELECTED


def test_flattening_operation_requires_explicit_permission() -> None:
    fact = _fact()
    response = _career_positioning_response()
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(response, flattening_required=True),
    )
    assert decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert decision.allowed_operation is None

    permission = TruthPermissionRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint="b" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        permission_id=uuid4(),
        fact_id=fact.fact_id,
        target_scope="EXPERIENCE",
        allowed_operations=[TransformationOperation.FLATTEN_FOR_LOWER_ROLE],
        constraints_json={},
        status=PermissionStatus.ACTIVE,
        approved_by=None,
        approved_at=None,
        valid_from=None,
        valid_until=None,
    )
    granted = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(response, flattening_required=True),
    )
    assert granted.decision == SelectionDecisionOutcome.SELECTED


def test_cpe_cannot_bypass_status_gate() -> None:
    fact = _fact(status=FactStatus.DRAFT, use_in_cv=True, requires_approval=False)
    response = _career_positioning_response()
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(
            response, requires_human_review=False, not_relevant_fact_ids=()
        ),
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED


def test_cpe_cannot_bypass_permission_gate() -> None:
    fact = _fact()
    response = _career_positioning_response()
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),
        requested_operation=TransformationOperation.CONTROLLED_REPHRASE,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(response),
    )
    assert decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED


def test_cpe_cannot_bypass_transferability_gate() -> None:
    fact = _fact(transferability=Transferability.NON_TRANSFERABLE)
    response = _career_positioning_response()
    decision = select_fact(
        fact=fact,
        target_context=_target_context(),  # different person_entity_id than fact.entity_id
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=_signal_from_response(response),
    )
    assert decision.decision == SelectionDecisionOutcome.BLOCKED
