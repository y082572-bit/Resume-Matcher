"""``strategy_fact_selection_builder`` and ``strategy_fact_selection_validator``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.schemas.candidacy_thesis import PositioningLevel, RoleEvidenceCategory
from app.schemas.fact_selection import CareerPositioningSignal, SelectionDecisionOutcome
from app.schemas.strategy_fact_selection import (
    StrategyAwareFactSelectionStatus,
    StrategyOverrideOutcome,
    StrategyRankTier,
)
from app.services.strategy_fact_selection_builder import build_fact_selection_with_role_strategy
from app.services.strategy_fact_selection_validator import (
    StrategyFactSelectionValidationStatus,
    validate_strategy_fact_selection_result,
)
from tests.unit.test_role_strategy_prep4_revalidation import build_ready_fixture, make_candidate, make_fact


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candidate_from_payload(payload):
    fact = make_fact(
        entity_id=payload.entity_id,
        fact_id=payload.fact_id,
        fact_type=payload.fact_type,
        content_fingerprint=payload.fact_content_fingerprint,
    )
    return make_candidate(fact)


@pytest.mark.asyncio
async def test_build_produces_ready_result_with_decisions_and_ranking() -> None:
    fixture = await build_ready_fixture()
    candidates = [_candidate_from_payload(fixture.fact0)]
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )
    assert result.status == StrategyAwareFactSelectionStatus.READY
    assert result.ready_for_integrated_p4 is True
    assert len(result.decisions) == 1
    assert result.ranking is not None
    assert result.decisions[0].strategy_rank_tier == StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT


@pytest.mark.asyncio
async def test_exact_thesis_fact_is_overridden_to_selected_in_full_build() -> None:
    fixture = await build_ready_fixture()
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[make_candidate(fact)],
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    decision = result.decisions[0]
    assert decision.base_decision_outcome == SelectionDecisionOutcome.NOT_RELEVANT
    assert decision.final_decision == SelectionDecisionOutcome.SELECTED
    assert decision.override_outcome == StrategyOverrideOutcome.OVERRIDDEN_TO_SELECTED
    assert decision.strategy_rank_tier == StrategyRankTier.TIER_0_EXACT_THESIS_SUPPORTING_FACT


@pytest.mark.asyncio
async def test_category_match_gives_tier_1_in_full_build() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))
    candidate = _candidate_from_payload(fixture.fact1)
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[candidate],
        permissions=(),
        evaluation_time=NOW,
    )
    decision = result.decisions[0]
    assert decision.final_decision == SelectionDecisionOutcome.SELECTED
    assert decision.strategy_rank_tier == StrategyRankTier.TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH
    assert decision.role_evidence_category == RoleEvidenceCategory.PROCESS_IMPROVEMENT


@pytest.mark.asyncio
async def test_legacy_only_fact_gives_tier_2_in_full_build() -> None:
    fixture = await build_ready_fixture()
    candidate = _candidate_from_payload(fixture.fact2)
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[candidate],
        permissions=(),
        evaluation_time=NOW,
    )
    decision = result.decisions[0]
    assert decision.final_decision == SelectionDecisionOutcome.SELECTED
    assert decision.strategy_rank_tier == StrategyRankTier.TIER_2_LEGACY_ONLY


@pytest.mark.asyncio
async def test_unknown_pending_rejected_or_payload_mismatched_fact_fails_closed() -> None:
    from app.schemas.truth_fact import FactStatus

    fixture = await build_ready_fixture()
    fact = make_fact(fact_type="SKILL", content_fingerprint="c" * 64, status=FactStatus.DRAFT)
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[make_candidate(fact)],
        permissions=(),
        evaluation_time=NOW,
    )
    decision = result.decisions[0]
    assert decision.final_decision == SelectionDecisionOutcome.BLOCKED
    assert decision.override_outcome == StrategyOverrideOutcome.NOT_OVERRIDDEN


@pytest.mark.asyncio
async def test_empty_candidate_facts_raises() -> None:
    fixture = await build_ready_fixture()
    with pytest.raises(ValueError):
        build_fact_selection_with_role_strategy(
            integration_input=fixture.integration_input,
            candidate_facts=[],
            permissions=(),
            evaluation_time=NOW,
        )


@pytest.mark.asyncio
async def test_tampered_integration_input_fingerprint_blocks_entire_result() -> None:
    fixture = await build_ready_fixture()
    tampered = fixture.integration_input.model_copy(
        update={"integration_input_fingerprint": "0" * 64}
    )
    result = build_fact_selection_with_role_strategy(
        integration_input=tampered,
        candidate_facts=[_candidate_from_payload(fixture.fact0)],
        permissions=(),
        evaluation_time=NOW,
    )
    assert result.status == StrategyAwareFactSelectionStatus.BLOCKED
    assert result.decisions == ()
    assert result.ranking is None
    assert result.ready_for_integrated_p4 is False


@pytest.mark.asyncio
async def test_failed_prep4_revalidation_blocks_entire_result() -> None:
    fixture = await build_ready_fixture()
    stale_current = fixture.current_job_replay.model_copy(
        update={"canonical_text_fingerprint": "9" * 64}
    )
    tampered = fixture.integration_input.model_copy(
        update={"current_job_analysis_replay_input": stale_current}
    )
    result = build_fact_selection_with_role_strategy(
        integration_input=tampered,
        candidate_facts=[_candidate_from_payload(fixture.fact0)],
        permissions=(),
        evaluation_time=NOW,
    )
    assert result.status == StrategyAwareFactSelectionStatus.BLOCKED
    assert result.decisions == ()


@pytest.mark.asyncio
async def test_base_decision_fingerprint_never_changes_in_full_build() -> None:
    fixture = await build_ready_fixture()
    fact = make_fact(
        entity_id=fixture.fact0.entity_id,
        fact_id=fixture.fact0.fact_id,
        fact_type=fixture.fact0.fact_type,
        content_fingerprint=fixture.fact0.fact_content_fingerprint,
    )
    signal = CareerPositioningSignal(
        source_job_id="job-1", source_generated_at=NOW, not_relevant_fact_ids=(fact.fact_id,)
    )
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[make_candidate(fact)],
        permissions=(),
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    decision = result.decisions[0]
    assert decision.base_decision.decision == SelectionDecisionOutcome.NOT_RELEVANT
    assert decision.base_decision_fingerprint == decision.base_decision.decision_fingerprint


# -- Validator --


@pytest.mark.asyncio
async def test_validator_accepts_a_freshly_built_result() -> None:
    fixture = await build_ready_fixture()
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[_candidate_from_payload(fixture.fact0), _candidate_from_payload(fixture.fact2)],
        permissions=(),
        evaluation_time=NOW,
    )
    validation = validate_strategy_fact_selection_result(result, fixture.integration_input)
    assert validation.status == StrategyFactSelectionValidationStatus.VALID
    assert validation.violations == ()
    assert validation.verified_ready_for_integrated_p4 is True


@pytest.mark.asyncio
async def test_validator_rejects_tampered_strategy_decision_fingerprint() -> None:
    fixture = await build_ready_fixture()
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[_candidate_from_payload(fixture.fact0)],
        permissions=(),
        evaluation_time=NOW,
    )
    tampered_decision = result.decisions[0].model_copy(
        update={"strategy_decision_fingerprint": "1" * 64}
    )
    tampered_result = result.model_copy(update={"decisions": (tampered_decision,)})
    validation = validate_strategy_fact_selection_result(tampered_result, fixture.integration_input)
    assert validation.status == StrategyFactSelectionValidationStatus.INVALID


@pytest.mark.asyncio
async def test_validator_rejects_tampered_ranking_fingerprint() -> None:
    fixture = await build_ready_fixture()
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[_candidate_from_payload(fixture.fact0)],
        permissions=(),
        evaluation_time=NOW,
    )
    tampered_ranking = result.ranking.model_copy(
        update={"strategy_ranking_input_fingerprint": "2" * 64}
    )
    tampered_result = result.model_copy(update={"ranking": tampered_ranking})
    validation = validate_strategy_fact_selection_result(tampered_result, fixture.integration_input)
    assert validation.status == StrategyFactSelectionValidationStatus.INVALID


@pytest.mark.asyncio
async def test_validator_rejects_tampered_result_fingerprint() -> None:
    fixture = await build_ready_fixture()
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[_candidate_from_payload(fixture.fact0)],
        permissions=(),
        evaluation_time=NOW,
    )
    tampered_result = result.model_copy(update={"result_fingerprint": "3" * 64})
    validation = validate_strategy_fact_selection_result(tampered_result, fixture.integration_input)
    assert validation.status == StrategyFactSelectionValidationStatus.INVALID


@pytest.mark.asyncio
async def test_validator_does_not_trust_forged_ready_flag() -> None:
    fixture = await build_ready_fixture()
    result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=[_candidate_from_payload(fixture.fact0)],
        permissions=(),
        evaluation_time=NOW,
    )
    tampered_decision = result.decisions[0].model_copy(
        update={"strategy_decision_fingerprint": "4" * 64}
    )
    tampered_result = result.model_copy(update={"decisions": (tampered_decision,)})
    validation = validate_strategy_fact_selection_result(tampered_result, fixture.integration_input)
    # ready_for_integrated_p4 on the pydantic object is still True (it was
    # only checked at construction time against the untampered decision),
    # but the validator must independently re-derive readiness, not trust it.
    assert tampered_result.ready_for_integrated_p4 is True
    assert validation.verified_ready_for_integrated_p4 is False
    assert validation.status == StrategyFactSelectionValidationStatus.INVALID
