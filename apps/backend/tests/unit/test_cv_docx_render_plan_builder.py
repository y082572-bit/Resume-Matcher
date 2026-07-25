"""Unit tests for Explicit Provenance Stage P6-B2a:
``build_cv_docx_render_plan``.

Uses a hand-constructed, pattern-valid ``ApprovedCvContentResult`` fixture
(``build_fixture_approved_content_result``, exported for reuse by sibling
P6-B2a test modules -- same cross-module-import convention already used by
``test_cv_document_input_validation.py``'s ``build_document_input``). This
fixture never re-verifies upstream P4/P5a/P5b/P5.5 cross-fingerprints
(that is P6-A's ``validate_approved_content_document_input`` concern,
already covered by its own suite, and re-exercised end-to-end by
``test_cv_docx_proposal_orchestration.py`` using the real pipeline
fixture) -- it exists solely to exercise the render-plan/renderer/template
logic in isolation with realistic, multi-section, multi-bullet content.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from app.schemas.cv_content_approval import (
    ApprovalAssemblyStatus,
    ApprovedCvContentDraft,
    ApprovedCvContentElement,
    ApprovedCvContentResult,
    ApprovedCvExperienceEntry,
    ApprovedCvExperienceSection,
    ApprovedCvFlatSection,
    EntryOrderSource,
    FinalContentOrigin,
)
from app.schemas.cv_content_plan import CvSection
from app.schemas.truth_fact import TransformationOperation
from app.schemas.cv_docx_render_plan import CvDocxRenderBlockKind
from app.schemas.cv_docx_visual_contract import DEFAULT_VISUAL_CONTRACT
from app.services.cv_docx_render_plan_builder import (
    CvDocxHeaderLines,
    build_cv_docx_render_plan,
)


CONTENT_PLAN_FINGERPRINT = "1" * 64
RESULT_FINGERPRINT = "2" * 64


def make_fixture_element(
    *,
    fact_type: str,
    target_section: CvSection,
    content_text: str,
    placement_order: int = 0,
    employment_scope_entity_id=None,
) -> ApprovedCvContentElement:
    element_fingerprint = hashlib.sha256(f"element::{content_text}".encode()).hexdigest()
    return ApprovedCvContentElement(
        element_fingerprint=element_fingerprint,
        origin=FinalContentOrigin.DETERMINISTIC_P5A,
        planned_fact_use_fingerprint="a" * 64,
        fact_selection_decision_fingerprint="b" * 64,
        person_entity_id=uuid4(),
        entity_id=uuid4(),
        fact_id=uuid4(),
        fact_type=fact_type,
        fact_revision=1,
        fact_content_fingerprint="c" * 64,
        payload_fingerprint="d" * 64,
        target_section=target_section,
        target_scope="global",
        employment_scope_entity_id=employment_scope_entity_id,
        placement_order=placement_order,
        allowed_operation=TransformationOperation.EXACT_COPY,
        content_text=content_text,
        generated_element_fingerprint="e" * 64,
        generation_provenance_fingerprint="f" * 64,
    )


def build_fixture_approved_content_result(
    *, content_plan_fingerprint: str = CONTENT_PLAN_FINGERPRINT, result_fingerprint: str = RESULT_FINGERPRINT
) -> ApprovedCvContentResult:
    profile = ApprovedCvFlatSection(
        section=CvSection.PROFILE,
        section_fingerprint="10" * 32,
        elements=(
            make_fixture_element(
                fact_type="SUMMARY", target_section=CvSection.PROFILE, content_text="Results-driven sales leader."
            ),
        ),
    )
    competencies = ApprovedCvFlatSection(
        section=CvSection.COMPETENCIES,
        section_fingerprint="11" * 32,
        elements=(
            make_fixture_element(
                fact_type="SKILL", target_section=CvSection.COMPETENCIES, content_text="Sales pipeline management"
            ),
            make_fixture_element(
                fact_type="SKILL",
                target_section=CvSection.COMPETENCIES,
                content_text="Key account negotiation",
                placement_order=1,
            ),
        ),
    )
    employment_id = uuid4()
    experience_entry = ApprovedCvExperienceEntry(
        entry_fingerprint="12" * 32,
        employment_entity_id=employment_id,
        entry_order=0,
        entry_order_source=EntryOrderSource.EXPLICIT_INPUT,
        header_elements=(
            make_fixture_element(
                fact_type="ROLE_HEADER",
                target_section=CvSection.EXPERIENCE,
                content_text="Senior Sales Manager, Acme Corp",
                employment_scope_entity_id=employment_id,
            ),
        ),
        responsibility_elements=(
            make_fixture_element(
                fact_type="RESPONSIBILITY",
                target_section=CvSection.EXPERIENCE,
                content_text="Managed a portfolio of 40 enterprise accounts",
                employment_scope_entity_id=employment_id,
                placement_order=1,
            ),
        ),
        achievement_elements=(
            make_fixture_element(
                fact_type="ACHIEVEMENT",
                target_section=CvSection.EXPERIENCE,
                content_text="Grew regional revenue by 32% in FY23",
                employment_scope_entity_id=employment_id,
                placement_order=2,
            ),
        ),
    )
    experience = ApprovedCvExperienceSection(
        section_fingerprint="13" * 32, experience_entries=(experience_entry,)
    )

    empty_sections = {
        CvSection.ACHIEVEMENTS: "14",
        CvSection.EDUCATION: "15",
        CvSection.CERTIFICATIONS: "16",
        CvSection.COURSES: "17",
        CvSection.LANGUAGES: "18",
        CvSection.TOOLS_TECHNOLOGIES: "19",
    }
    empty_flat_sections = [
        ApprovedCvFlatSection(section=section, section_fingerprint=prefix * 32, elements=())
        for section, prefix in empty_sections.items()
    ]

    sections = (profile, competencies, experience, *empty_flat_sections)
    draft = ApprovedCvContentDraft(
        content_plan_fingerprint=content_plan_fingerprint,
        draft_fingerprint="20" * 32,
        sections=sections,
    )
    return ApprovedCvContentResult(
        result_fingerprint=result_fingerprint,
        status=ApprovalAssemblyStatus.READY,
        ready_for_p6=True,
        content_plan_fingerprint=content_plan_fingerprint,
        draft=draft,
    )


def test_render_plan_covers_every_approved_element_in_order() -> None:
    approved = build_fixture_approved_content_result()
    plan = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )

    content_blocks = [
        block
        for block in plan.blocks
        if block.kind in (CvDocxRenderBlockKind.BODY_PARAGRAPH, CvDocxRenderBlockKind.BULLET_ITEM)
    ]
    expected_texts = [
        "Results-driven sales leader.",
        "Sales pipeline management",
        "Key account negotiation",
        "Senior Sales Manager, Acme Corp",
        "Managed a portfolio of 40 enterprise accounts",
        "Grew regional revenue by 32% in FY23",
    ]
    assert [block.text for block in content_blocks] == expected_texts

    all_approved_fingerprints = set()
    for section in approved.draft.sections:
        if isinstance(section, ApprovedCvExperienceSection):
            for entry in section.experience_entries:
                for element in (
                    entry.header_elements + entry.responsibility_elements + entry.achievement_elements
                ):
                    all_approved_fingerprints.add(element.element_fingerprint)
        else:
            for element in section.elements:
                all_approved_fingerprints.add(element.element_fingerprint)

    plan_fingerprints = {block.source_element_fingerprint for block in content_blocks}
    assert plan_fingerprints == all_approved_fingerprints


def test_render_plan_never_adds_or_drops_elements() -> None:
    approved = build_fixture_approved_content_result()
    plan = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )
    content_block_count = sum(
        1
        for block in plan.blocks
        if block.kind in (CvDocxRenderBlockKind.BODY_PARAGRAPH, CvDocxRenderBlockKind.BULLET_ITEM)
    )
    assert content_block_count == 6  # exactly the fixture's 6 approved elements


def test_render_plan_never_changes_content_text() -> None:
    approved = build_fixture_approved_content_result()
    plan = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )
    by_fingerprint = {
        block.source_element_fingerprint: block.text
        for block in plan.blocks
        if block.source_element_fingerprint is not None
    }
    for section in approved.draft.sections:
        if isinstance(section, ApprovedCvExperienceSection):
            elements = [
                e
                for entry in section.experience_entries
                for e in (entry.header_elements + entry.responsibility_elements + entry.achievement_elements)
            ]
        else:
            elements = list(section.elements)
        for element in elements:
            assert by_fingerprint[element.element_fingerprint] == element.content_text


def test_render_plan_places_explicit_page_break_immediately_before_experience() -> None:
    approved = build_fixture_approved_content_result()
    plan = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )
    kinds = [block.kind for block in plan.blocks]
    experience_header_index = next(
        i
        for i, block in enumerate(plan.blocks)
        if block.kind == CvDocxRenderBlockKind.SECTION_HEADER and block.section == CvSection.EXPERIENCE
    )
    assert kinds[experience_header_index - 1] == CvDocxRenderBlockKind.PAGE_BREAK


def test_render_plan_section_order_matches_draft_order() -> None:
    approved = build_fixture_approved_content_result()
    plan = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )
    section_header_order = [
        block.section for block in plan.blocks if block.kind == CvDocxRenderBlockKind.SECTION_HEADER
    ]
    expected_order = []
    for section in approved.draft.sections:
        expected_order.append(
            CvSection.EXPERIENCE if isinstance(section, ApprovedCvExperienceSection) else section.section
        )
    assert section_header_order == expected_order


def test_render_plan_no_header_blocks_when_header_lines_is_none() -> None:
    approved = build_fixture_approved_content_result()
    plan = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT, header_lines=None
    )
    assert not any(
        block.kind in (CvDocxRenderBlockKind.NAME_HEADER, CvDocxRenderBlockKind.CONTACT_LINE)
        for block in plan.blocks
    )


def test_render_plan_header_blocks_when_header_lines_supplied() -> None:
    approved = build_fixture_approved_content_result()
    header_lines = CvDocxHeaderLines(
        full_name="Marek Grabowski", contact_tokens=("marek@example.com", "+48 000 000 000", "linkedin.com/in/marek")
    )
    plan = build_cv_docx_render_plan(
        approved_content_result=approved,
        visual_contract=DEFAULT_VISUAL_CONTRACT,
        header_lines=header_lines,
    )
    assert plan.blocks[0].kind == CvDocxRenderBlockKind.NAME_HEADER
    assert plan.blocks[0].text == "Marek Grabowski"
    assert plan.blocks[1].kind == CvDocxRenderBlockKind.CONTACT_LINE
    assert "linkedin.com/in/marek" in plan.blocks[1].text
    # single contact-line block: LinkedIn is never split into its own block
    contact_blocks = [b for b in plan.blocks if b.kind == CvDocxRenderBlockKind.CONTACT_LINE]
    assert len(contact_blocks) == 1


def test_render_plan_fingerprint_changes_when_content_changes() -> None:
    approved = build_fixture_approved_content_result()
    plan_one = build_cv_docx_render_plan(
        approved_content_result=approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )

    mutated_element = make_fixture_element(
        fact_type="SUMMARY", target_section=CvSection.PROFILE, content_text="A completely different summary."
    )
    mutated_profile = approved.draft.sections[0].model_copy(update={"elements": (mutated_element,)})
    mutated_sections = (mutated_profile, *approved.draft.sections[1:])
    mutated_draft = approved.draft.model_copy(update={"sections": mutated_sections})
    mutated_approved = approved.model_copy(update={"draft": mutated_draft})

    plan_two = build_cv_docx_render_plan(
        approved_content_result=mutated_approved, visual_contract=DEFAULT_VISUAL_CONTRACT
    )
    assert plan_one.render_plan_fingerprint != plan_two.render_plan_fingerprint


def test_render_plan_rejects_non_ready_result() -> None:
    approved = build_fixture_approved_content_result()
    not_ready = approved.model_copy(update={"status": ApprovalAssemblyStatus.PENDING_APPROVAL})
    with pytest.raises(ValueError):
        build_cv_docx_render_plan(approved_content_result=not_ready, visual_contract=DEFAULT_VISUAL_CONTRACT)
