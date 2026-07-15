"""Unit tests for Truth Library loader service."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.truth_library_loader import (
    TruthLibraryInvalidJsonError,
    TruthLibraryNotFoundError,
    TruthLibraryStructureError,
    get_truth_library,
    load_truth_library,
)


# Fixtures

@pytest.fixture
def valid_truth_library():
    """A minimal but valid True Library structure."""
    return {
        "meta": {
            "kandidat": "Test",
            "wersja": "1.0",
            "dataAktualizacji": "2026-01-01",
            "status": "AKTYWNA",
        },
        "kategorie": {
            "daneOsobowe": {"wpisy": []},
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "firma": "Test Corp",
                        "stanowisko": "Test Role",
                        "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
                    }
                ]
            },
            "zakazy": {"wpisy": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": ["AUTOMATYCZNE"],
                "statusyWymagajaceAkceptacji": ["DO_AKCEPTACJI"],
                "statusyZablokowane": ["ZABLOKOWANE"],
            },
        },
    }


@pytest.fixture
def temp_truth_library(valid_truth_library: dict):
    """Create a temporary Truth Library file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)
    yield path
    # Cleanup
    path.unlink(missing_ok=True)


# Tests: load_truth_library()


def test_load_valid_file(temp_truth_library: Path, valid_truth_library: dict):
    """Test that a valid file is loaded correctly."""
    result = load_truth_library(temp_truth_library)
    assert result == valid_truth_library


def test_load_file_not_found():
    """Test that TruthLibraryNotFoundError is raised for missing file."""
    with pytest.raises(TruthLibraryNotFoundError):
        load_truth_library(Path("/nonexistent/path/truth.json"))


def test_load_invalid_json():
    """Test that TruthLibraryInvalidJsonError is raised for malformed JSON."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write("{broken json")
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryInvalidJsonError):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_meta(valid_truth_library: dict):
    """Test that missing 'meta' raises TruthLibraryStructureError."""
    del valid_truth_library["meta"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError, match="meta"):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_kategorie(valid_truth_library: dict):
    """Test that missing 'kategorie' raises TruthLibraryStructureError."""
    del valid_truth_library["kategorie"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError, match="kategorie"):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_doswiadczenie_zawodowe(valid_truth_library: dict):
    """Test that missing 'doswiadczenieZawodowe' raises TruthLibraryStructureError."""
    del valid_truth_library["kategorie"]["doswiadczenieZawodowe"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(
            TruthLibraryStructureError, match="doswiadczenieZawodowe"
        ):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_wpisy(valid_truth_library: dict):
    """Test that missing 'doswiadczenieZawodowe.wpisy' raises error."""
    del valid_truth_library["kategorie"]["doswiadczenieZawodowe"]["wpisy"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError, match="wpisy"):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_wpisy_not_list(valid_truth_library: dict):
    """Test that non-list 'wpisy' is rejected."""
    valid_truth_library["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = {
        "invalid": "dict"
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError, match="list"):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_zakazy(valid_truth_library: dict):
    """Test that missing 'zakazy' raises error."""
    del valid_truth_library["kategorie"]["zakazy"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError, match="zakazy"):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_reguly(valid_truth_library: dict):
    """Test that missing 'reguly' raises error."""
    del valid_truth_library["kategorie"]["reguly"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError, match="reguly"):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_statuy_do_automatycznego_cv(valid_truth_library: dict):
    """Test that missing 'statusyDoAutomatycznegoCV' raises error."""
    del valid_truth_library["kategorie"]["reguly"]["statusyDoAutomatycznegoCV"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_statuy_wymagajace_akceptacji(valid_truth_library: dict):
    """Test that missing 'statusyWymagajaceAkceptacji' raises error."""
    del valid_truth_library["kategorie"]["reguly"]["statusyWymagajaceAkceptacji"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


def test_load_missing_statuy_zablokowane(valid_truth_library: dict):
    """Test that missing 'statusyZablokowane' raises error."""
    del valid_truth_library["kategorie"]["reguly"]["statusyZablokowane"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(valid_truth_library, f)
        path = Path(f.name)

    try:
        with pytest.raises(TruthLibraryStructureError):
            load_truth_library(path)
    finally:
        path.unlink(missing_ok=True)


# Tests: get_truth_library()


def test_get_truth_library_loads_first_time(temp_truth_library: Path, valid_truth_library: dict):
    """Test that first call loads from file."""
    # Reset global cache
    import app.services.truth_library_loader as loader_module
    loader_module._truth_library_cache = None

    with patch("app.config.settings") as mock_settings:
        mock_settings.truth_library_path = temp_truth_library
        result = get_truth_library()
        assert result == valid_truth_library


def test_get_truth_library_caches(temp_truth_library: Path, valid_truth_library: dict):
    """Test that second call uses cache."""
    # Reset cache
    import app.services.truth_library_loader as loader_module
    loader_module._truth_library_cache = None

    with patch("app.config.settings") as mock_settings:
        mock_settings.truth_library_path = temp_truth_library

        # First call
        result1 = get_truth_library()
        
        # Delete file to prove second call uses cache
        temp_truth_library.unlink()
        
        # Second call should still work (from cache)
        result2 = get_truth_library()
        assert result1 == result2 == valid_truth_library


def test_get_truth_library_force_reload(temp_truth_library: Path, valid_truth_library: dict):
    """Test that force_reload=True reloads from file."""
    # Reset cache
    import app.services.truth_library_loader as loader_module
    loader_module._truth_library_cache = None

    with patch("app.config.settings") as mock_settings:
        mock_settings.truth_library_path = temp_truth_library

        # Initial load
        get_truth_library()

        # Modify file
        modified = valid_truth_library.copy()
        modified["meta"]["wersja"] = "2.0"
        with open(temp_truth_library, "w", encoding="utf-8") as f:
            json.dump(modified, f)

        # force_reload=True should pick up the change
        result = get_truth_library(force_reload=True)
        assert result["meta"]["wersja"] == "2.0"


def test_get_truth_library_returns_deep_copy(temp_truth_library: Path):
    """Test that returned data is a deep copy."""
    # Reset cache
    import app.services.truth_library_loader as loader_module
    loader_module._truth_library_cache = None

    with patch("app.config.settings") as mock_settings:
        mock_settings.truth_library_path = temp_truth_library

        result1 = get_truth_library()
        result2 = get_truth_library()

        # Modify result1
        result1["meta"]["wersja"] = "modified"

        # result2 should not be affected
        assert result2["meta"]["wersja"] != "modified"


def test_get_truth_library_mutation_doesnt_affect_cache(temp_truth_library: Path, valid_truth_library: dict):
    """Test that mutating returned data doesn't corrupt the cache."""
    # Reset cache
    import app.services.truth_library_loader as loader_module
    loader_module._truth_library_cache = None

    with patch("app.config.settings") as mock_settings:
        mock_settings.truth_library_path = temp_truth_library

        result1 = get_truth_library()
        result1["meta"]["wersja"] = "hacked"

        result2 = get_truth_library()
        # result2 should still be original
        assert result2["meta"]["wersja"] == valid_truth_library["meta"]["wersja"]


# Tests: environment variable support


def test_truth_library_path_respects_env_var(monkeypatch, temp_truth_library: Path):
    """Test that TRUTH_LIBRARY_PATH environment variable is respected."""
    monkeypatch.setenv("TRUTH_LIBRARY_PATH", str(temp_truth_library))

    # Reimport to pick up new env var
    from app.config import Settings
    settings = Settings()
    assert settings.truth_library_path == temp_truth_library
