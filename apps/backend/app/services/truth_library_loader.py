"""Truth Library loader service.

Provides safe, read-only access to the Truth Library JSON file.
All operations preserve the original file; returned data is deep-copied
to prevent accidental mutations of cached state.
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TruthLibraryNotFoundError(Exception):
    """Raised when the Truth Library file does not exist."""

    pass


class TruthLibraryInvalidJsonError(Exception):
    """Raised when the Truth Library file contains invalid JSON."""

    pass


class TruthLibraryStructureError(Exception):
    """Raised when the Truth Library is missing required fields or has invalid structure."""

    pass


# Process-level cache
_truth_library_cache: dict[str, Any] | None = None


def load_truth_library(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the Truth Library from a JSON file.

    Reads the file fresh each call (does not use cache). Validates structure,
    returns a deep copy to prevent cache mutations.

    Args:
        path: Path to the Truth Library JSON file. If None, uses settings.truth_library_path.

    Returns:
        Deep copy of the validated Truth Library dict.

    Raises:
        TruthLibraryNotFoundError: If the file does not exist.
        TruthLibraryInvalidJsonError: If the file is not valid JSON.
        TruthLibraryStructureError: If required fields are missing or invalid.
    """
    from app.config import settings

    # Use provided path or fall back to settings
    file_path = Path(path) if path else settings.truth_library_path
    file_path = Path(file_path)  # Ensure it's a Path object

    logger.debug(f"Loading Truth Library from {file_path}")

    # Check file exists
    if not file_path.exists():
        raise TruthLibraryNotFoundError(
            f"Truth Library file not found: {file_path.absolute()}"
        )

    # Read and parse JSON
    try:
        content = file_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise TruthLibraryInvalidJsonError(
            f"Invalid JSON in Truth Library {file_path.absolute()}: {e}"
        ) from e
    except (OSError, UnicodeDecodeError) as e:
        raise TruthLibraryInvalidJsonError(
            f"Failed to read Truth Library {file_path.absolute()}: {e}"
        ) from e

    # Validate structure
    _validate_truth_library_structure(data)

    logger.debug("Truth Library loaded and validated successfully")
    # Return a deep copy so mutations don't affect cache
    return copy.deepcopy(data)


def _validate_truth_library_structure(data: Any) -> None:
    """Validate that data is a valid Truth Library structure.

    Checks required fields and types.

    Args:
        data: The parsed JSON data to validate.

    Raises:
        TruthLibraryStructureError: If validation fails.
    """
    if not isinstance(data, dict):
        raise TruthLibraryStructureError(
            "Truth Library root must be a JSON object (dict)"
        )

    # Check meta
    if "meta" not in data:
        raise TruthLibraryStructureError("Truth Library missing required field: meta")

    # Check kategorie (top-level categories)
    if "kategorie" not in data:
        raise TruthLibraryStructureError(
            "Truth Library missing required field: kategorie"
        )

    kategorie = data["kategorie"]
    if not isinstance(kategorie, dict):
        raise TruthLibraryStructureError(
            "kategorie must be a JSON object (dict), got {type(kategorie).__name__}"
        )

    # Check doswiadczenieZawodowe (professional experience)
    if "doswiadczenieZawodowe" not in kategorie:
        raise TruthLibraryStructureError(
            "kategorie missing required field: doswiadczenieZawodowe"
        )

    doz = kategorie["doswiadczenieZawodowe"]
    if not isinstance(doz, dict):
        raise TruthLibraryStructureError(
            f"doswiadczenieZawodowe must be a dict, got {type(doz).__name__}"
        )

    # Check wpisy (entries)
    if "wpisy" not in doz:
        raise TruthLibraryStructureError(
            "doswiadczenieZawodowe missing required field: wpisy"
        )

    wpisy = doz["wpisy"]
    if not isinstance(wpisy, list):
        raise TruthLibraryStructureError(
            f"doswiadczenieZawodowe.wpisy must be a list, got {type(wpisy).__name__}"
        )

    # Check zakazy (prohibitions)
    if "zakazy" not in kategorie:
        raise TruthLibraryStructureError("kategorie missing required field: zakazy")

    # Check reguly (rules)
    if "reguly" not in kategorie:
        raise TruthLibraryStructureError("kategorie missing required field: reguly")

    reguly = kategorie["reguly"]
    if not isinstance(reguly, dict):
        raise TruthLibraryStructureError(
            f"reguly must be a dict, got {type(reguly).__name__}"
        )

    # Check required rule status lists
    required_status_fields = [
        "statusyDoAutomatycznegoCV",
        "statusyWymagajaceAkceptacji",
        "statusyZablokowane",
    ]
    for field in required_status_fields:
        if field not in reguly:
            raise TruthLibraryStructureError(
                f"reguly missing required field: {field}"
            )


def get_truth_library(force_reload: bool = False) -> dict[str, Any]:
    """Get the Truth Library with process-level caching.

    Returns a deep copy on each call; mutations do not affect cache or file.

    Args:
        force_reload: If True, bypass cache and reload from disk.

    Returns:
        Deep copy of the Truth Library dict.

    Raises:
        TruthLibraryNotFoundError: If the file does not exist.
        TruthLibraryInvalidJsonError: If the file is not valid JSON.
        TruthLibraryStructureError: If required fields are missing or invalid.
    """
    global _truth_library_cache

    if force_reload or _truth_library_cache is None:
        logger.debug(
            f"{'Force-reloading' if force_reload else 'Loading'} Truth Library"
        )
        _truth_library_cache = load_truth_library()

    # Always return a deep copy to prevent cache corruption
    return copy.deepcopy(_truth_library_cache)
