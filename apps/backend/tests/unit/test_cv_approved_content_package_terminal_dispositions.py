"""Unit tests for Explicit Provenance Stage P6-C1b-A: terminal-disposition
acceptance in ``cv_approved_content_package_builder``.

Six terminal codes are accepted, four are always rejected
(``REQUIRES_REPLAN`` included), coverage of every ``PlannedFactUse`` must be
exact (no gap, no overlap), and there is deliberately no invented rule
requiring every CV section to carry an approved output.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.schemas.cv_content_approval import FinalDispositionCode
from app.schemas.cv_content_plan import CvExperienceSectionPlan
from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInputValidationResult,
    DocumentInputValidationStatus,
)
from app.schemas.cv_approved_content_package import ApprovedContentPackageBuildStatus
from app.services.cv_approved_content_package_builder import (
    _ACCEPTED_TERMINAL_DISPOSITION_CODES,
    _REJECTED_NON_TERMINAL_DISPOSITION_CODES,
    _iter_planned_fact_uses,
    build_approved_content_package,
)
from app.services.cv_document_owner_identity import build_owner_key
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


def _force_valid(owner_key, document_input):
    """Patch ``validate_approved_content_document_input`` (as imported into
    the builder module) to unconditionally report ``VALID`` for
    ``document_input`` -- isolating the builder's own terminal-disposition
    coverage check from the upstream P6-A envelope-freshness/fingerprint
    chain, which is exhaustively covered by its own dedicated test module
    (``tests.unit.test_cv_document_input_validation``) and is not what this
    module is responsible for testing."""

    return patch(
        "app.services.cv_approved_content_package_builder.validate_approved_content_document_input",
        return_value=ApprovedContentDocumentInputValidationResult(
            status=DocumentInputValidationStatus.VALID,
            owner_key=owner_key,
            recomputed_document_input_fingerprint=document_input.document_input_fingerprint,
        ),
    )


def test_six_accepted_terminal_codes() -> None:
    assert _ACCEPTED_TERMINAL_DISPOSITION_CODES == {
        FinalDispositionCode.INCLUDED_DETERMINISTIC,
        FinalDispositionCode.INCLUDED_APPROVED_PROPOSAL,
        FinalDispositionCode.REJECTED_BY_USER,
        FinalDispositionCode.SEMANTIC_VALIDATION_FAILED,
        FinalDispositionCode.INPUT_INVALID,
        FinalDispositionCode.NON_ELIGIBLE_BLOCKED,
    }
    assert len(_ACCEPTED_TERMINAL_DISPOSITION_CODES) == 6


def test_four_rejected_non_terminal_codes() -> None:
    assert _REJECTED_NON_TERMINAL_DISPOSITION_CODES == {
        FinalDispositionCode.PENDING_USER_DECISION,
        FinalDispositionCode.SEMANTIC_VALIDATION_INCONCLUSIVE,
        FinalDispositionCode.SEMANTIC_VALIDATION_PROVIDER_ERROR,
        FinalDispositionCode.REQUIRES_REPLAN,
    }
    assert len(_REJECTED_NON_TERMINAL_DISPOSITION_CODES) == 4
    assert _ACCEPTED_TERMINAL_DISPOSITION_CODES.isdisjoint(_REJECTED_NON_TERMINAL_DISPOSITION_CODES)


def test_requires_replan_is_rejected() -> None:
    assert FinalDispositionCode.REQUIRES_REPLAN in _REJECTED_NON_TERMINAL_DISPOSITION_CODES
    assert FinalDispositionCode.REQUIRES_REPLAN not in _ACCEPTED_TERMINAL_DISPOSITION_CODES


@pytest.mark.asyncio
async def test_exact_planned_fact_use_coverage_on_a_real_plan() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    result = ctx["result"]

    planned_fact_uses = _iter_planned_fact_uses(plan)
    planned_fingerprints = {use.planned_fact_use_fingerprint for use in planned_fact_uses}
    disposition_fingerprints = {item.planned_fact_use_fingerprint for item in result.dispositions}
    assert planned_fingerprints == disposition_fingerprints
    assert len(planned_fact_uses) == len(result.dispositions)

    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT


@pytest.mark.asyncio
async def test_missing_disposition_is_a_coverage_gap_violation() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )

    result = document_input.approved_content_result
    assert len(result.dispositions) >= 1
    truncated_result = result.model_copy(update={"dispositions": result.dispositions[1:]})
    tampered_input = document_input.model_copy(update={"approved_content_result": truncated_result})

    with _force_valid(owner_key, tampered_input):
        build_result = build_approved_content_package(owner_key=owner_key, document_input=tampered_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.TERMINAL_DISPOSITION_SET_VIOLATION


@pytest.mark.asyncio
async def test_extra_disposition_is_a_coverage_overlap_violation() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )

    result = document_input.approved_content_result
    assert len(result.dispositions) >= 1
    duplicated_result = result.model_copy(
        update={"dispositions": result.dispositions + (result.dispositions[0],)}
    )
    tampered_input = document_input.model_copy(update={"approved_content_result": duplicated_result})

    with _force_valid(owner_key, tampered_input):
        build_result = build_approved_content_package(owner_key=owner_key, document_input=tampered_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.TERMINAL_DISPOSITION_SET_VIOLATION


@pytest.mark.asyncio
async def test_non_terminal_disposition_code_is_rejected() -> None:
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )

    result = document_input.approved_content_result
    assert len(result.dispositions) >= 1
    first = result.dispositions[0]
    # ``model_copy(update=...)`` bypasses ``CvContentFinalDisposition``'s own
    # model validator (origin/element_fingerprint required only for
    # INCLUDED_* codes) -- exactly what lets us inject an otherwise-illegal
    # non-terminal disposition for this builder-only check.
    tampered_first = first.model_copy(
        update={
            "disposition_code": FinalDispositionCode.PENDING_USER_DECISION,
            "origin": None,
            "element_fingerprint": None,
        }
    )
    tampered_dispositions = (tampered_first,) + result.dispositions[1:]
    tampered_result = result.model_copy(update={"dispositions": tampered_dispositions})
    tampered_input = document_input.model_copy(update={"approved_content_result": tampered_result})

    with _force_valid(owner_key, tampered_input):
        build_result = build_approved_content_package(owner_key=owner_key, document_input=tampered_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.TERMINAL_DISPOSITION_SET_VIOLATION


@pytest.mark.asyncio
async def test_no_invented_every_section_must_have_output_rule() -> None:
    """A section with zero ``PlannedFactUse`` requires zero dispositions --
    the build must still succeed even though most ``CvSection`` values in
    this fixture carry no facts at all."""

    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]

    sections_with_uses = set()
    for section in plan.sections:
        if isinstance(section, CvExperienceSectionPlan):
            if section.experience_entries:
                sections_with_uses.add(section.section)
        elif section.planned_fact_uses:
            sections_with_uses.add(section.section)

    # This fixture only ever plans SKILL/LANGUAGE facts -- most sections are
    # deliberately empty.
    assert len(sections_with_uses) < len(plan.sections)

    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
