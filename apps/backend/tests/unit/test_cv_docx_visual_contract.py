"""Unit tests for Explicit Provenance Stage P6-B2a: ``CvDocxVisualContract``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.cv_content_plan import CvSection
from app.schemas.cv_docx_visual_contract import (
    DEFAULT_VISUAL_CONTRACT,
    CvDocxVisualContract,
    compute_visual_contract_fingerprint,
)


def test_default_contract_is_calibri_10_body() -> None:
    assert DEFAULT_VISUAL_CONTRACT.body_font_name == "Calibri"
    assert DEFAULT_VISUAL_CONTRACT.body_font_size_pt == 10


def test_default_contract_breaks_before_experience() -> None:
    assert DEFAULT_VISUAL_CONTRACT.page_break_before_section == CvSection.EXPERIENCE


def test_contract_is_frozen() -> None:
    with pytest.raises(Exception):
        DEFAULT_VISUAL_CONTRACT.margin_top_cm = 5.0


def test_contract_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CvDocxVisualContract(**{**DEFAULT_VISUAL_CONTRACT.model_dump(), "unexpected_field": 1})


def test_fingerprint_is_deterministic_for_same_contract() -> None:
    fp1 = compute_visual_contract_fingerprint(DEFAULT_VISUAL_CONTRACT)
    fp2 = compute_visual_contract_fingerprint(DEFAULT_VISUAL_CONTRACT.model_copy())
    assert fp1 == fp2


def test_fingerprint_changes_with_visual_contract_version_only() -> None:
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(update={"visual_contract_version": "cv-docx-visual-contract-v2"})
    assert compute_visual_contract_fingerprint(DEFAULT_VISUAL_CONTRACT) != compute_visual_contract_fingerprint(bumped)


def test_fingerprint_changes_with_renderer_version_only() -> None:
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(update={"renderer_version": "cv-docx-deterministic-renderer-v2"})
    assert compute_visual_contract_fingerprint(DEFAULT_VISUAL_CONTRACT) != compute_visual_contract_fingerprint(bumped)


def test_fingerprint_changes_with_template_version_only() -> None:
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(update={"template_version": "cv-docx-template-v2"})
    assert compute_visual_contract_fingerprint(DEFAULT_VISUAL_CONTRACT) != compute_visual_contract_fingerprint(bumped)


def test_fingerprint_changes_with_margin_change() -> None:
    bumped = DEFAULT_VISUAL_CONTRACT.model_copy(update={"margin_top_cm": 2.5})
    assert compute_visual_contract_fingerprint(DEFAULT_VISUAL_CONTRACT) != compute_visual_contract_fingerprint(bumped)


def test_body_font_size_field_is_pinned_to_ten() -> None:
    with pytest.raises(ValidationError):
        CvDocxVisualContract(**{**DEFAULT_VISUAL_CONTRACT.model_dump(), "body_font_size_pt": 11})


def test_body_font_name_field_is_pinned_to_calibri() -> None:
    with pytest.raises(ValidationError):
        CvDocxVisualContract(**{**DEFAULT_VISUAL_CONTRACT.model_dump(), "body_font_name": "Arial"})
