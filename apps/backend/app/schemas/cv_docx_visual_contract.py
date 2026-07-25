"""Explicit Provenance Stage P6-B2a: the versioned CV visual contract.

``CvDocxVisualContract`` is the single, closed source of truth for every
layout/typography decision the deterministic Proposal DOCX renderer makes
-- page geometry, fonts, spacing, and structural layout policy (e.g. which
section starts a forced page break). It carries no approved content, no
LLM output, and no filesystem path -- only fixed, versioned constants. A
visual-contract change is always expressed as a version bump
(``visual_contract_version``/``template_version``/``renderer_version``)
plus whichever concrete field changed; ``compute_visual_contract_fingerprint``
covers every field, so any change -- including a version-only bump with no
other field changed -- yields a different fingerprint.
"""

from __future__ import annotations

import hashlib

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cv_content_plan import CvSection
from app.services.truth_fingerprint import canonical_json_bytes


VISUAL_CONTRACT_VERSION = "cv-docx-visual-contract-v1"
VISUAL_CONTRACT_FINGERPRINT_VERSION = "cv-docx-visual-contract-fingerprint-v1"


class CvDocxVisualContract(BaseModel):
    """Closed, versioned layout/typography policy for the deterministic
    Proposal DOCX renderer. Never carries approved content, an LLM
    output, a filesystem path, or a random/time-derived value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    visual_contract_version: str = Field(min_length=1, max_length=64, default=VISUAL_CONTRACT_VERSION)
    renderer_version: str = Field(min_length=1, max_length=64)
    template_version: str = Field(min_length=1, max_length=64)

    # -- page geometry (cm) --
    page_width_cm: float = Field(gt=0)
    page_height_cm: float = Field(gt=0)
    margin_top_cm: float = Field(gt=0)
    margin_bottom_cm: float = Field(gt=0)
    margin_left_cm: float = Field(gt=0)
    margin_right_cm: float = Field(gt=0)

    # -- typography (body font/size are a fixed product requirement, never
    # a caller-tunable choice -- pinned via Literal, not merely a default) --
    body_font_name: Literal["Calibri"] = "Calibri"
    body_font_size_pt: Literal[10] = 10
    name_header_font_size_pt: int = Field(gt=0, le=72)
    section_header_font_size_pt: int = Field(gt=0, le=72)
    contact_line_font_size_pt: int = Field(gt=0, le=72)

    # -- spacing --
    section_header_space_before_pt: float = Field(ge=0)
    section_header_space_after_pt: float = Field(ge=0)
    body_paragraph_space_after_pt: float = Field(ge=0)
    bullet_space_after_pt: float = Field(ge=0)
    bullet_hanging_indent_cm: float = Field(ge=0)

    # -- structural layout policy --
    page_break_before_section: CvSection = CvSection.EXPERIENCE
    bullet_glyph: str = Field(min_length=1, max_length=4, default="•")
    contact_separator: str = Field(min_length=1, max_length=8, default=" · ")


def compute_visual_contract_fingerprint(contract: CvDocxVisualContract) -> str:
    content = {
        "fingerprint_version": VISUAL_CONTRACT_FINGERPRINT_VERSION,
        "content": contract.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


DEFAULT_VISUAL_CONTRACT = CvDocxVisualContract(
    renderer_version="cv-docx-deterministic-renderer-v1",
    template_version="cv-docx-template-v1",
    page_width_cm=21.0,
    page_height_cm=29.7,
    margin_top_cm=1.5,
    margin_bottom_cm=1.5,
    margin_left_cm=2.0,
    margin_right_cm=2.0,
    name_header_font_size_pt=18,
    section_header_font_size_pt=12,
    contact_line_font_size_pt=9,
    section_header_space_before_pt=12,
    section_header_space_after_pt=6,
    body_paragraph_space_after_pt=4,
    bullet_space_after_pt=2,
    bullet_hanging_indent_cm=0.63,
)
