"""Explicit Provenance Stage P6-B2a: pure builder for ``CvDocxRenderPlan``.

``build_cv_docx_render_plan`` never calls a provider, never reorders or
mutates approved content, and never invents an ``ApprovedCvContentElement``
that is not already present in ``approved_content_result.draft.sections``.
Header/contact text is accepted only as an explicit, caller-supplied
``CvDocxHeaderLines`` -- the builder never fabricates a name, email, phone
number, or LinkedIn URL on its own. Neither ``ApprovedCvContentResult`` nor
``CvContentPlan`` (the exact and only inputs P6-A's ``CvDocxRenderingAdapter``
Protocol receives) carries a candidate name or contact info, so the
concrete renderer wired into that Protocol (``cv_docx_renderer.py``) always
calls this builder with ``header_lines=None`` -- no ``NAME_HEADER``/
``CONTACT_LINE`` block is ever emitted in production use today. A future
stage that gains a person/contact-provenance input can supply
``header_lines`` without any change to this module.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cv_content_approval import (
    ApprovedCvContentElement,
    ApprovedCvContentResult,
    ApprovedCvExperienceSection,
    ApprovedCvFlatSection,
    ApprovedCvSection,
    ApprovalAssemblyStatus,
)
from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_docx_render_plan import (
    CvDocxRenderBlock,
    CvDocxRenderBlockKind,
    CvDocxRenderPlan,
    compute_render_plan_fingerprint,
)
from app.schemas.cv_docx_visual_contract import (
    CvDocxVisualContract,
    compute_visual_contract_fingerprint,
)


class CvDocxHeaderLines(BaseModel):
    """Explicit, caller-supplied name/contact text. Never fabricated by
    the render plan builder -- ``header_lines=None`` at the call site
    simply means no header/contact blocks are emitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_name: str = Field(min_length=1, max_length=255)
    contact_tokens: tuple[str, ...] = Field(default_factory=tuple)


def _elements_for_section(section: ApprovedCvSection) -> list[ApprovedCvContentElement]:
    if isinstance(section, ApprovedCvExperienceSection):
        ordered: list[ApprovedCvContentElement] = []
        for entry in section.experience_entries:
            ordered.extend(entry.header_elements)
            ordered.extend(entry.responsibility_elements)
            ordered.extend(entry.achievement_elements)
        return ordered
    assert isinstance(section, ApprovedCvFlatSection)
    return list(section.elements)


def _section_kind(section: ApprovedCvSection) -> CvSection:
    if isinstance(section, ApprovedCvExperienceSection):
        return CvSection.EXPERIENCE
    assert isinstance(section, ApprovedCvFlatSection)
    return section.section


def build_cv_docx_render_plan(
    *,
    approved_content_result: ApprovedCvContentResult,
    visual_contract: CvDocxVisualContract,
    header_lines: CvDocxHeaderLines | None = None,
) -> CvDocxRenderPlan:
    if approved_content_result.status != ApprovalAssemblyStatus.READY:
        raise ValueError("approved_content_result.status must be READY to build a render plan")
    draft = approved_content_result.draft
    assert draft is not None  # guaranteed by ApprovedCvContentResult's own READY invariant

    visual_contract_fingerprint = compute_visual_contract_fingerprint(visual_contract)
    blocks: list[CvDocxRenderBlock] = []

    if header_lines is not None:
        blocks.append(
            CvDocxRenderBlock(kind=CvDocxRenderBlockKind.NAME_HEADER, text=header_lines.full_name)
        )
        if header_lines.contact_tokens:
            contact_text = visual_contract.contact_separator.join(header_lines.contact_tokens)
            blocks.append(
                CvDocxRenderBlock(kind=CvDocxRenderBlockKind.CONTACT_LINE, text=contact_text)
            )

    for section in draft.sections:
        section_kind = _section_kind(section)
        if section_kind == visual_contract.page_break_before_section:
            blocks.append(
                CvDocxRenderBlock(kind=CvDocxRenderBlockKind.PAGE_BREAK, section=section_kind)
            )

        blocks.append(
            CvDocxRenderBlock(
                kind=CvDocxRenderBlockKind.SECTION_HEADER,
                text=section_kind.value.replace("_", " "),
                section=section_kind,
            )
        )
        for element in _elements_for_section(section):
            block_kind = (
                CvDocxRenderBlockKind.BULLET_ITEM
                if section_kind == CvSection.EXPERIENCE
                else CvDocxRenderBlockKind.BODY_PARAGRAPH
            )
            blocks.append(
                CvDocxRenderBlock(
                    kind=block_kind,
                    text=element.content_text,
                    section=section_kind,
                    source_element_fingerprint=element.element_fingerprint,
                )
            )

    blocks_tuple = tuple(blocks)
    render_plan_fingerprint = compute_render_plan_fingerprint(
        approved_cv_content_fingerprint=approved_content_result.result_fingerprint,
        content_plan_fingerprint=approved_content_result.content_plan_fingerprint,
        visual_contract_fingerprint=visual_contract_fingerprint,
        blocks=blocks_tuple,
    )
    return CvDocxRenderPlan(
        approved_cv_content_fingerprint=approved_content_result.result_fingerprint,
        content_plan_fingerprint=approved_content_result.content_plan_fingerprint,
        visual_contract_fingerprint=visual_contract_fingerprint,
        blocks=blocks_tuple,
        render_plan_fingerprint=render_plan_fingerprint,
    )
