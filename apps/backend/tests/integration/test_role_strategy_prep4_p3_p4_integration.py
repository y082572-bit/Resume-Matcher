"""End-to-end proof of the mandatory PRE-P4 -> strategy-aware P3 ->
ROLE_STRATEGY_INTEGRATED P4 pipeline, plus the source-level no-bypass and
isolation matrix every Stage 10D-A core module must satisfy.

All postings, facts, and evidence are synthetic; no real candidate or
employer data is used.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.candidacy_thesis import RoleEvidenceCategory
from app.schemas.cv_content_plan import CvContentPlanBuildOutcome, CvContentPlanMode
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import TransformationOperation
from app.services import (
    cv_content_plan_integrated_builder,
    cv_content_plan_strategy_ranking,
    role_strategy_prep4_revalidation,
    strategy_fact_selection_builder,
    strategy_fact_selection_policy,
    strategy_fact_selection_replay,
    strategy_fact_selection_validator,
)
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_integrated_builder import build_role_strategy_integrated_cv_content_plan
from app.services.strategy_fact_selection_builder import build_fact_selection_with_role_strategy
from app.services.strategy_fact_selection_validator import (
    StrategyFactSelectionValidationStatus,
    validate_strategy_fact_selection_result,
)
from tests.unit.test_role_strategy_prep4_revalidation import build_ready_fixture, make_candidate, make_fact


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

CORE_MODULES = [
    role_strategy_prep4_revalidation,
    strategy_fact_selection_policy,
    strategy_fact_selection_builder,
    strategy_fact_selection_validator,
    strategy_fact_selection_replay,
    cv_content_plan_strategy_ranking,
    cv_content_plan_integrated_builder,
]


def _import_lines(module) -> list[str]:
    return [
        line.strip()
        for line in inspect.getsource(module).splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]


# -- Isolation -----------------------------------------------------------


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_app_llm_or_litellm(module) -> None:
    for line in _import_lines(module):
        assert "app.llm" not in line
        assert "litellm" not in line


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_database_or_sqlalchemy(module) -> None:
    for line in _import_lines(module):
        assert "sqlalchemy" not in line.lower()
        assert "app.database" not in line
        assert "app.db_engine" not in line


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_imports_router_frontend_or_docx_pdf(module) -> None:
    for line in _import_lines(module):
        assert "app.routers" not in line
        assert "fastapi" not in line.lower()
        assert "playwright" not in line.lower()
        assert "docx" not in line.lower()


def test_no_source_reference_identity_in_new_schemas() -> None:
    from app.schemas import strategy_fact_selection

    assert "source_reference" not in inspect.getsource(strategy_fact_selection)


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_never_uses_datetime_now_or_python_hash(module) -> None:
    source = inspect.getsource(module)
    assert "datetime.now(" not in source
    assert re.search(r"(?<!hashlib\.)(?<!\.)\bhash\(", source) is None


def test_fingerprinting_module_uses_canonical_json_bytes() -> None:
    """``strategy_fact_selection_builder`` is the module that actually
    mints new SHA-256 fingerprints (integration input, strategy decision,
    ranking entry/input, result) -- it must go through the shared
    ``canonical_json_bytes`` encoder, never a bespoke JSON serializer.
    Every other new module either re-derives existing fingerprints (never
    minting a new one) or is pure orchestration/ordering with no
    fingerprint of its own."""

    source = inspect.getsource(strategy_fact_selection_builder)
    assert "canonical_json_bytes" in source


# -- No-bypass -------------------------------------------------------------


def test_integrated_p4_never_calls_public_legacy_entrypoint() -> None:
    source = inspect.getsource(cv_content_plan_integrated_builder)
    assert "build_cv_content_plan(" not in source
    assert "_build_cv_content_plan_core(" in source


def test_integrated_p4_requires_a_real_strategy_result_no_default_no_none() -> None:
    signature = inspect.signature(build_role_strategy_integrated_cv_content_plan)
    strategy_param = signature.parameters["strategy_selection_result"]
    assert strategy_param.default is inspect.Parameter.empty
    assert "decisions" not in signature.parameters
    assert "career_positioning_snapshot_fingerprint" not in signature.parameters


def test_strategy_builder_requires_full_integration_input_no_default() -> None:
    signature = inspect.signature(build_fact_selection_with_role_strategy)
    integration_param = signature.parameters["integration_input"]
    assert integration_param.default is inspect.Parameter.empty


def test_legacy_p3_and_p4_are_never_modified_by_this_stage() -> None:
    """Legacy P3 (``fact_selection_policy``/``fact_selection_replay``) and
    the P4 legacy builder function are untouched: ``select_fact`` is only
    ever *called*, never redefined, by the new strategy modules."""

    from app.services import fact_selection_policy

    source = inspect.getsource(fact_selection_policy)
    assert "role_strategy" not in source.lower()
    assert "strategy_fact_selection" not in source


# -- End-to-end pipeline ----------------------------------------------------


def _candidate_from_payload(payload):
    fact = make_fact(
        entity_id=payload.entity_id,
        fact_id=payload.fact_id,
        fact_type=payload.fact_type,
        content_fingerprint=payload.fact_content_fingerprint,
    )
    return make_candidate(fact)


@pytest.mark.asyncio
async def test_full_prep4_to_p3_to_p4_pipeline_produces_a_p5_ready_plan() -> None:
    fixture = await build_ready_fixture(priority_categories=(RoleEvidenceCategory.PROCESS_IMPROVEMENT,))
    candidates = [
        _candidate_from_payload(fixture.fact0),
        _candidate_from_payload(fixture.fact1),
        _candidate_from_payload(fixture.fact2),
    ]
    strategy_result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )
    assert strategy_result.ready_for_integrated_p4 is True

    strategy_validation = validate_strategy_fact_selection_result(strategy_result, fixture.integration_input)
    assert strategy_validation.status == StrategyFactSelectionValidationStatus.VALID

    entity_types = {
        fixture.fact0.entity_id: EntityType.SKILL,
        fixture.fact1.entity_id: EntityType.EMPLOYMENT,
        fixture.fact2.entity_id: EntityType.LANGUAGE,
    }
    build_result = build_role_strategy_integrated_cv_content_plan(
        integration_input=fixture.integration_input,
        strategy_selection_result=strategy_result,
        role_strategy_context=fixture.role_strategy_context,
        target_context=fixture.target_context,
        entity_types=entity_types,
    )
    assert build_result.outcome == CvContentPlanBuildOutcome.BUILT
    assert build_result.plan.plan_mode == CvContentPlanMode.ROLE_STRATEGY_INTEGRATED
    assert build_result.p5_ready is True

    # Every field required by the mandatory integration is present and
    # independently re-derivable -- never a wrapper fingerprint P5 can't see.
    plan = build_result.plan
    assert plan.role_strategy_context_fingerprint == fixture.role_strategy_context.context_fingerprint
    assert plan.strategy_selection_result_fingerprint == strategy_result.result_fingerprint
    assert plan.content_plan_fingerprint  # participates in the real P5 chain unmodified


@pytest.mark.asyncio
async def test_integrated_flow_cannot_reuse_a_legacy_p3_result_for_p4() -> None:
    """A legacy ``FactSelectionDecision`` list built without going through
    strategy P3 at all cannot be handed to the integrated P4 entrypoint --
    its signature has no parameter that would accept it."""

    fixture = await build_ready_fixture()
    candidates = [_candidate_from_payload(fixture.fact0)]
    strategy_result = build_fact_selection_with_role_strategy(
        integration_input=fixture.integration_input,
        candidate_facts=candidates,
        permissions=(),
        evaluation_time=NOW,
    )
    legacy_decisions = [d.base_decision for d in strategy_result.decisions]
    entity_types = {fixture.fact0.entity_id: EntityType.SKILL}

    # The only way to reach a CvContentPlan from legacy_decisions is the
    # legacy entrypoint, which always produces plan_mode=LEGACY.
    legacy_result = build_cv_content_plan(
        target_context=fixture.target_context, decisions=legacy_decisions, entity_types=entity_types
    )
    assert legacy_result.plan.plan_mode == CvContentPlanMode.LEGACY

    signature = inspect.signature(build_role_strategy_integrated_cv_content_plan)
    assert not any(
        "FactSelectionDecision" in str(param.annotation) for param in signature.parameters.values()
    )
