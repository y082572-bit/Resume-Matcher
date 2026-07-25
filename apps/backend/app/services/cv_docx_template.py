"""Explicit Provenance Stage P6-B2a: deterministic, in-code Proposal DOCX
template (Template Strategy T2).

``build_base_document`` constructs a blank Word document purely from
``CvDocxVisualContract`` -- page geometry, margins, and named paragraph
styles (``CvName``, ``CvContactLine``, ``CvSectionHeader``, ``CvBullet``,
plus ``Normal`` for body text) -- with every volatile core-property field
pinned to a fixed constant. No checked-in binary asset is ever read; the
template is produced fresh, in memory, on every call, and canonically
packaged before its bytes are ever hashed, so
``DocxTemplateHandle.template_fingerprint`` (the exact raw SHA-256 of
``template_bytes``) is stable across processes and hosts for a given
``CvDocxVisualContract``, and changes whenever any field of that contract
changes -- including a version-only bump, since every version string is
embedded into a fixed core property (see ``_apply_fixed_core_properties``).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO

import docx
from docx.document import Document as DocxDocument
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.shared import Cm, Pt

from app.schemas.cv_docx_visual_contract import CvDocxVisualContract
from app.services.cv_docx_canonical_packaging import canonicalize_docx_package
from app.services.cv_document_adapters import DocxTemplateHandle


TEMPLATE_ADAPTER_ID = "cv-docx-deterministic-template-v1"

_FIXED_CORE_DATETIME = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _apply_fixed_core_properties(document: DocxDocument, visual_contract: CvDocxVisualContract) -> None:
    """Pin every volatile core-property field to a fixed constant --
    never ``datetime.now()``. ``category`` embeds every version string on
    ``visual_contract`` so a version-only bump (no other field changed)
    still yields different bytes."""

    props = document.core_properties
    props.author = "Resume Matcher"
    props.last_modified_by = "Resume Matcher"
    props.created = _FIXED_CORE_DATETIME
    props.modified = _FIXED_CORE_DATETIME
    props.title = ""
    props.subject = ""
    props.comments = ""
    props.keywords = ""
    props.revision = 1
    props.category = (
        f"{visual_contract.visual_contract_version}|"
        f"{visual_contract.template_version}|"
        f"{visual_contract.renderer_version}"
    )


def _configure_styles(document: DocxDocument, visual_contract: CvDocxVisualContract) -> None:
    styles = document.styles

    normal = styles["Normal"]
    normal.font.name = visual_contract.body_font_name
    normal.font.size = Pt(visual_contract.body_font_size_pt)
    normal.paragraph_format.space_after = Pt(visual_contract.body_paragraph_space_after_pt)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE

    name_style = styles.add_style("CvName", WD_STYLE_TYPE.PARAGRAPH)
    name_style.base_style = normal
    name_style.font.name = visual_contract.body_font_name
    name_style.font.size = Pt(visual_contract.name_header_font_size_pt)
    name_style.font.bold = True
    name_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name_style.paragraph_format.space_after = Pt(visual_contract.section_header_space_after_pt)

    contact_style = styles.add_style("CvContactLine", WD_STYLE_TYPE.PARAGRAPH)
    contact_style.base_style = normal
    contact_style.font.name = visual_contract.body_font_name
    contact_style.font.size = Pt(visual_contract.contact_line_font_size_pt)
    contact_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_style.paragraph_format.space_after = Pt(visual_contract.section_header_space_after_pt)
    contact_style.paragraph_format.keep_together = True

    section_header_style = styles.add_style("CvSectionHeader", WD_STYLE_TYPE.PARAGRAPH)
    section_header_style.base_style = normal
    section_header_style.font.name = visual_contract.body_font_name
    section_header_style.font.size = Pt(visual_contract.section_header_font_size_pt)
    section_header_style.font.bold = True
    section_header_style.paragraph_format.space_before = Pt(
        visual_contract.section_header_space_before_pt
    )
    section_header_style.paragraph_format.space_after = Pt(
        visual_contract.section_header_space_after_pt
    )

    bullet_style = styles.add_style("CvBullet", WD_STYLE_TYPE.PARAGRAPH)
    bullet_style.base_style = normal
    bullet_style.font.name = visual_contract.body_font_name
    bullet_style.font.size = Pt(visual_contract.body_font_size_pt)
    bullet_style.paragraph_format.space_after = Pt(visual_contract.bullet_space_after_pt)
    bullet_style.paragraph_format.left_indent = Cm(visual_contract.bullet_hanging_indent_cm)
    bullet_style.paragraph_format.first_line_indent = Cm(-visual_contract.bullet_hanging_indent_cm)


def build_base_document(visual_contract: CvDocxVisualContract) -> DocxDocument:
    """Build a blank Word document purely from ``visual_contract`` --
    never reads a checked-in binary template asset."""

    document = docx.Document()

    section = document.sections[0]
    section.page_width = Cm(visual_contract.page_width_cm)
    section.page_height = Cm(visual_contract.page_height_cm)
    section.top_margin = Cm(visual_contract.margin_top_cm)
    section.bottom_margin = Cm(visual_contract.margin_bottom_cm)
    section.left_margin = Cm(visual_contract.margin_left_cm)
    section.right_margin = Cm(visual_contract.margin_right_cm)

    _configure_styles(document, visual_contract)
    _apply_fixed_core_properties(document, visual_contract)

    return document


def serialize_canonical_document(document: DocxDocument) -> bytes:
    """Save ``document`` to an in-memory buffer and canonically repackage
    it -- never writes to a filesystem path."""

    raw = BytesIO()
    document.save(raw)
    return canonicalize_docx_package(raw.getvalue())


class DeterministicDocxTemplateProvider:
    """Implements ``DocxTemplateProvider`` (Template Strategy T2): every
    call rebuilds the base document purely in code from a fixed
    ``CvDocxVisualContract`` -- no checked-in binary asset is ever read."""

    def __init__(self, visual_contract: CvDocxVisualContract) -> None:
        self._visual_contract = visual_contract

    def get_template(self) -> DocxTemplateHandle:
        document = build_base_document(self._visual_contract)
        template_bytes = serialize_canonical_document(document)
        template_fingerprint = hashlib.sha256(template_bytes).hexdigest()
        return DocxTemplateHandle(
            template_bytes=template_bytes,
            template_fingerprint=template_fingerprint,
            template_adapter_id=TEMPLATE_ADAPTER_ID,
        )
