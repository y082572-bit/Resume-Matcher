"""Unit tests for Explicit Provenance Stage P6-B2a: the deterministic
in-code Proposal DOCX template (``cv_docx_template.py``, Strategy T2)."""

from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO

import docx

from app.schemas.cv_docx_visual_contract import DEFAULT_VISUAL_CONTRACT
from app.services.cv_docx_template import (
    DeterministicDocxTemplateProvider,
    build_base_document,
    serialize_canonical_document,
)


def test_template_bytes_are_deterministic_across_calls() -> None:
    provider = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    handle_one = provider.get_template()
    handle_two = provider.get_template()
    assert handle_one.template_bytes == handle_two.template_bytes


def test_template_fingerprint_is_exact_sha256_of_bytes() -> None:
    provider = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    handle = provider.get_template()
    assert handle.template_fingerprint == hashlib.sha256(handle.template_bytes).hexdigest()


def test_template_fingerprint_changes_with_visual_contract_version() -> None:
    provider_a = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(
        update={"visual_contract_version": "cv-docx-visual-contract-v2"}
    )
    provider_b = DeterministicDocxTemplateProvider(bumped)
    assert provider_a.get_template().template_fingerprint != provider_b.get_template().template_fingerprint


def test_template_fingerprint_changes_with_template_version() -> None:
    provider_a = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(update={"template_version": "cv-docx-template-v2"})
    provider_b = DeterministicDocxTemplateProvider(bumped)
    assert provider_a.get_template().template_fingerprint != provider_b.get_template().template_fingerprint


def test_template_fingerprint_changes_with_margin() -> None:
    provider_a = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(update={"margin_top_cm": 3.0})
    provider_b = DeterministicDocxTemplateProvider(bumped)
    assert provider_a.get_template().template_fingerprint != provider_b.get_template().template_fingerprint


def test_core_properties_never_use_current_time() -> None:
    document = build_base_document(DEFAULT_VISUAL_CONTRACT)
    from datetime import datetime, timezone

    assert document.core_properties.created == datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert document.core_properties.modified == datetime(2000, 1, 1, tzinfo=timezone.utc)


def test_core_properties_are_byte_stable_across_builds() -> None:
    doc_one = build_base_document(DEFAULT_VISUAL_CONTRACT)
    doc_two = build_base_document(DEFAULT_VISUAL_CONTRACT)
    bytes_one = serialize_canonical_document(doc_one)
    bytes_two = serialize_canonical_document(doc_two)
    with zipfile.ZipFile(BytesIO(bytes_one)) as zf1, zipfile.ZipFile(BytesIO(bytes_two)) as zf2:
        assert zf1.read("docProps/core.xml") == zf2.read("docProps/core.xml")


def test_named_styles_use_calibri_ten_for_body() -> None:
    document = build_base_document(DEFAULT_VISUAL_CONTRACT)
    normal = document.styles["Normal"]
    assert normal.font.name == "Calibri"
    from docx.shared import Pt

    assert normal.font.size == Pt(10)


def test_name_style_is_centered_and_bold() -> None:
    document = build_base_document(DEFAULT_VISUAL_CONTRACT)
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    name_style = document.styles["CvName"]
    assert name_style.font.bold is True
    assert name_style.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.CENTER


def test_template_bytes_open_cleanly_with_python_docx() -> None:
    provider = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    handle = provider.get_template()
    document = docx.Document(BytesIO(handle.template_bytes))
    assert document.styles["CvBullet"] is not None


def test_template_package_has_required_opc_parts() -> None:
    provider = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    handle = provider.get_template()
    with zipfile.ZipFile(BytesIO(handle.template_bytes)) as zf:
        names = set(zf.namelist())
    for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml"):
        assert required in names


def test_template_package_has_no_vba_macro_part() -> None:
    provider = DeterministicDocxTemplateProvider(DEFAULT_VISUAL_CONTRACT)
    handle = provider.get_template()
    with zipfile.ZipFile(BytesIO(handle.template_bytes)) as zf:
        names = zf.namelist()
    assert not any("vba" in name.lower() for name in names)
