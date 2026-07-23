"""``strategy_fact_selection_replay``: replay and stale-result detection."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from app.schemas.candidacy_thesis import RoleEvidenceCategory
from app.schemas.strategy_fact_selection import StrategyAwareFactSelectionFreshnessStatus
from app.services.strategy_fact_selection_builder import build_fact_selection_with_role_strategy
from app.services.strategy_fact_selection_replay import (
    current_strategy_selection_replay_input,
    evaluate_strategy_selection_freshness,
    is_strategy_selection_stale,
    replay_input_from_strategy_selection_result,
    stale_strategy_selection_fields,
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


async def _build(fixture, extra_facts=()):
    candidates = [_candidate_from_payload(fixture.fact0), *[_candidate_from_payload(f) for f in extra_facts]]
    return build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )


def _current_from(fixture, result):
    return current_strategy_selection_replay_input(
        integration_input=fixture.integration_input,
        decisions=result.decisions,
        ranking=result.ranking,
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )


@pytest.mark.asyncio
async def test_identical_previous_and_current_are_fresh() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)
    current = _current_from(fixture, result)
    freshness = evaluate_strategy_selection_freshness(previous, current)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.FRESH
    assert not is_strategy_selection_stale(previous, current)


@pytest.mark.asyncio
async def test_current_none_is_freshness_not_verified() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)
    freshness = evaluate_strategy_selection_freshness(previous, None)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.FRESHNESS_NOT_VERIFIED


@pytest.mark.asyncio
async def test_result_fingerprint_is_recalculated_and_covered_by_replay() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)
    assert previous.result_fingerprint == result.result_fingerprint


@pytest.mark.asyncio
async def test_changing_job_analysis_gives_stale() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)

    stale_job_replay = fixture.current_job_replay.model_copy(
        update={"canonical_text_fingerprint": "1" * 64}
    )
    tampered_integration_input = fixture.integration_input.model_copy(
        update={"current_job_analysis_replay_input": stale_job_replay}
    )
    current = current_strategy_selection_replay_input(
        integration_input=tampered_integration_input,
        decisions=result.decisions,
        ranking=result.ranking,
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )
    freshness = evaluate_strategy_selection_freshness(previous, current)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.STALE
    assert "integration_input_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_changing_thesis_gives_stale() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)

    stale_thesis_replay = fixture.integration_input.current_candidacy_thesis_replay_input.model_copy(
        update={"thesis_statement_fingerprint": "2" * 64}
    )
    tampered_integration_input = fixture.integration_input.model_copy(
        update={"current_candidacy_thesis_replay_input": stale_thesis_replay}
    )
    current = current_strategy_selection_replay_input(
        integration_input=tampered_integration_input,
        decisions=result.decisions,
        ranking=result.ranking,
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )
    freshness = evaluate_strategy_selection_freshness(previous, current)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.STALE


@pytest.mark.asyncio
async def test_changing_role_strategy_context_gives_stale() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)

    current = _current_from(fixture, result)
    tampered_current = dataclasses.replace(current, role_strategy_context_fingerprint="3" * 64)
    freshness = evaluate_strategy_selection_freshness(previous, tampered_current)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.STALE
    assert "role_strategy_context_fingerprint" in freshness.stale_fields


@pytest.mark.asyncio
async def test_changing_payload_gives_stale() -> None:
    from app.services.candidacy_thesis_builder import compute_approved_fact_payload_set_fingerprint

    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)

    changed_fact0 = fixture.fact0.model_copy(update={"payload_fingerprint": "4" * 64})
    changed_payloads = (changed_fact0, fixture.fact1, fixture.fact2)
    tampered_payload_set = fixture.payload_set.model_copy(
        update={
            "payloads": changed_payloads,
            "payload_set_fingerprint": compute_approved_fact_payload_set_fingerprint(changed_payloads),
        }
    )
    tampered_integration_input = fixture.integration_input.model_copy(
        update={"approved_fact_payload_set": tampered_payload_set}
    )
    current = current_strategy_selection_replay_input(
        integration_input=tampered_integration_input,
        decisions=result.decisions,
        ranking=result.ranking,
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )
    freshness = evaluate_strategy_selection_freshness(previous, current)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.STALE


@pytest.mark.asyncio
async def test_changing_category_mapping_version_gives_stale() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)
    current = current_strategy_selection_replay_input(
        integration_input=fixture.integration_input,
        decisions=result.decisions,
        ranking=result.ranking,
        evidence_category_mapping_version="role-evidence-category-mapping-v999",
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )
    freshness = evaluate_strategy_selection_freshness(previous, current)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.STALE
    assert "evidence_category_mapping_version" in freshness.stale_fields


@pytest.mark.asyncio
async def test_no_current_replay_blocks_freshness_verification() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)
    freshness = evaluate_strategy_selection_freshness(previous, None)
    assert freshness.freshness_status == StrategyAwareFactSelectionFreshnessStatus.FRESHNESS_NOT_VERIFIED
    assert freshness.stale_fields == ()


@pytest.mark.asyncio
async def test_stale_fields_helper_matches_evaluate_result() -> None:
    fixture = await build_ready_fixture()
    result = await _build(fixture)
    previous = replay_input_from_strategy_selection_result(result)
    stale_thesis_replay = fixture.integration_input.current_candidacy_thesis_replay_input.model_copy(
        update={"thesis_statement_fingerprint": "5" * 64}
    )
    tampered_integration_input = fixture.integration_input.model_copy(
        update={"current_candidacy_thesis_replay_input": stale_thesis_replay}
    )
    current = current_strategy_selection_replay_input(
        integration_input=tampered_integration_input,
        decisions=result.decisions,
        ranking=result.ranking,
        evidence_category_mapping_version=result.evidence_category_mapping_version,
        integration_policy_version=result.integration_policy_version,
        result_fingerprint=result.result_fingerprint,
    )
    fields = stale_strategy_selection_fields(previous, current)
    freshness = evaluate_strategy_selection_freshness(previous, current)
    assert fields == freshness.stale_fields
    assert fields != ()
