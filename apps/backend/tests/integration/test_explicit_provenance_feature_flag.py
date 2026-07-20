"""P1 flag is strict configuration only and cannot activate production behavior."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import app


BACKEND_ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_FILES = (
    BACKEND_ROOT / "app/schemas/truth_entity.py",
    BACKEND_ROOT / "app/schemas/truth_fact.py",
    BACKEND_ROOT / "app/services/truth_identity.py",
    BACKEND_ROOT / "app/services/truth_fingerprint.py",
    BACKEND_ROOT / "app/services/truth_policy.py",
    BACKEND_ROOT / "app/services/truth_repository.py",
)


def settings(monkeypatch, value=None):
    monkeypatch.delenv("EXPLICIT_PROVENANCE_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("EXPLICIT_PROVENANCE_ENABLED", value)
    return Settings(_env_file=None)


def test_flag_defaults_false(monkeypatch):
    assert settings(monkeypatch).explicit_provenance_enabled is False


@pytest.mark.parametrize(("value", "expected"), [("false", False), ("true", True)])
def test_flag_accepts_explicit_booleans(monkeypatch, value, expected):
    assert settings(monkeypatch, value).explicit_provenance_enabled is expected


def test_invalid_flag_fails_without_silent_fallback(monkeypatch):
    with pytest.raises(ValidationError):
        settings(monkeypatch, "sometimes")


def test_no_public_truth_v2_api_is_registered():
    paths = {route.path for route in app.routes}
    assert not any("truth-v2" in path or "explicit-provenance" in path for path in paths)


def test_flag_is_not_read_by_legacy_runtime():
    runtime_files = [
        BACKEND_ROOT / "app/routers/career_positioning.py",
        BACKEND_ROOT / "app/services/cv_transformation_generation.py",
        BACKEND_ROOT / "app/services/cv_transformation_truth_validation.py",
        BACKEND_ROOT / "app/services/truth_index.py",
        BACKEND_ROOT / "app/services/truth_validator.py",
    ]
    assert all(
        "explicit_provenance_enabled" not in path.read_text()
        for path in runtime_files
        if path.exists()
    )


def test_foundation_has_no_legacy_generation_llm_or_text_identity_coupling():
    source = "\n".join(path.read_text() for path in FOUNDATION_FILES)
    forbidden = (
        "TruthIndex",
        "cv_transformation_generation",
        "cv_transformation_truth_validation",
        "litellm",
        "openai",
        ".startswith(source_reference",
        "target_path",
        "bullet_index",
    )
    assert all(token not in source for token in forbidden)


def test_database_does_not_dual_write_legacy_operations():
    source = (BACKEND_ROOT / "app/database.py").read_text()
    assert "create_entity(" not in source
    assert "create_fact(" not in source
    assert "explicit_provenance_enabled" not in source
