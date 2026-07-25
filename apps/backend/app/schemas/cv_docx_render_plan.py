"""Explicit Provenance Stage P6-B2a: the deterministic DOCX render plan.

``CvDocxRenderPlan`` is a closed, pure intermediate representation between
approved CV content (P5.5) and DOCX bytes. It separates *what* is rendered
(owned entirely by ``ApprovedCvContentResult`` -- never re-derived, never
re-paraphrased here) from *how* it is laid out (owned by
``CvDocxVisualContract``). Every content-bearing block is bound 1:1 to
exactly one ``ApprovedCvContentElement`` via ``source_element_fingerprint``;
every purely structural block (a section-header label, an explicit page
break, the optional name/contact header) carries no such fingerprint. A
render plan can never invent, drop, or reorder approved content relative to
``ApprovedCvContentDraft.sections``.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cv_content_plan import CvSection
from app.schemas.truth_entity import SHA256_PATTERN
from app.services.truth_fingerprint import canonical_json_bytes


RENDER_PLAN_SCHEMA_VERSION = "cv-docx-render-plan-schema-v1"
RENDER_PLAN_FINGERPRINT_VERSION = "cv-docx-render-plan-v1"


class CvDocxRenderBlockKind(StrEnum):
    NAME_HEADER = "NAME_HEADER"
    CONTACT_LINE = "CONTACT_LINE"
    SECTION_HEADER = "SECTION_HEADER"
    BODY_PARAGRAPH = "BODY_PARAGRAPH"
    BULLET_ITEM = "BULLET_ITEM"
    PAGE_BREAK = "PAGE_BREAK"


class CvDocxRenderBlock(BaseModel):
    """One ordered layout instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CvDocxRenderBlockKind
    text: str = Field(default="", max_length=20_000)
    section: CvSection | None = None
    source_element_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)


class CvDocxRenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    render_plan_schema_version: Literal["cv-docx-render-plan-schema-v1"] = (
        RENDER_PLAN_SCHEMA_VERSION
    )
    approved_cv_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    visual_contract_fingerprint: str = Field(pattern=SHA256_PATTERN)
    blocks: tuple[CvDocxRenderBlock, ...]
    render_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)


def compute_render_plan_fingerprint(
    *,
    approved_cv_content_fingerprint: str,
    content_plan_fingerprint: str,
    visual_contract_fingerprint: str,
    blocks: tuple[CvDocxRenderBlock, ...],
) -> str:
    content = {
        "fingerprint_version": RENDER_PLAN_FINGERPRINT_VERSION,
        "content": {
            "approved_cv_content_fingerprint": approved_cv_content_fingerprint,
            "content_plan_fingerprint": content_plan_fingerprint,
            "visual_contract_fingerprint": visual_contract_fingerprint,
            "blocks": [block.model_dump(mode="json") for block in blocks],
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()
