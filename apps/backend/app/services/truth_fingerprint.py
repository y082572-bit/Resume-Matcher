"""Versioned canonical JSON and fingerprints for Explicit Provenance P1."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID


CANONICAL_JSON_VERSION = "truth-canonical-json-v1"
ENTITY_FINGERPRINT_VERSION = "truth-entity-content-v1"
FACT_FINGERPRINT_VERSION = "truth-fact-content-v1"
VARIANT_FINGERPRINT_VERSION = "truth-variant-content-v1"
EVIDENCE_FINGERPRINT_VERSION = "truth-evidence-content-v1"
PERMISSION_FINGERPRINT_VERSION = "truth-permission-content-v1"
TRUTH_SNAPSHOT_VERSION = "truth-snapshot-v1"
POLICY_REGISTRY_VERSION = "truth-transformation-policy-v1"

_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_COMMON_AUDIT_FIELDS = {
    "created_at",
    "updated_at",
    "archived_at",
    "source_reference",
    "revision",
}


def _normalize_string(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(
        _HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip()
        for line in normalized.split("\n")
    )
    return normalized.strip()


def _path(parent: str, child: str) -> str:
    return child if not parent else f"{parent}.{child}"


def canonicalize_json_value(
    value: Any,
    *,
    set_like_paths: frozenset[str] = frozenset(),
    _current_path: str = "",
) -> Any:
    """Return a JSON-compatible semantic projection with explicit list policy."""

    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and Infinity are not valid canonical JSON numbers")
        return value
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, Enum):
        return canonicalize_json_value(
            value.value,
            set_like_paths=set_like_paths,
            _current_path=_current_path,
        )
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical datetime must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        return {
            key: canonicalize_json_value(
                value[key],
                set_like_paths=set_like_paths,
                _current_path=_path(_current_path, key),
            )
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        items = [
            canonicalize_json_value(
                item,
                set_like_paths=set_like_paths,
                _current_path=_current_path,
            )
            for item in value
        ]
        if _current_path in set_like_paths:
            unique = {
                canonical_json_bytes(item).decode("utf-8"): item for item in items
            }
            return [unique[key] for key in sorted(unique)]
        return items
    if isinstance(value, (set, frozenset)):
        raise ValueError("sets require an explicit JSON array and set-like path")
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(
    value: Any, *, set_like_paths: frozenset[str] = frozenset()
) -> bytes:
    """Serialize canonical JSON using stable UTF-8 bytes."""

    normalized = canonicalize_json_value(value, set_like_paths=set_like_paths)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any, *, set_like_paths: frozenset[str] = frozenset()) -> str:
    return hashlib.sha256(
        canonical_json_bytes(value, set_like_paths=set_like_paths)
    ).hexdigest()


def _content_projection(
    value: Mapping[str, Any], *, version: str, own_id_field: str
) -> dict[str, Any]:
    excluded = _COMMON_AUDIT_FIELDS | {own_id_field, "content_fingerprint"}
    return {
        "fingerprint_version": version,
        "content": {key: item for key, item in value.items() if key not in excluded},
    }


def compute_entity_fingerprint(value: Mapping[str, Any]) -> str:
    return _sha256(
        _content_projection(
            value, version=ENTITY_FINGERPRINT_VERSION, own_id_field="entity_id"
        )
    )


def compute_fact_fingerprint(value: Mapping[str, Any]) -> str:
    return _sha256(
        _content_projection(
            value, version=FACT_FINGERPRINT_VERSION, own_id_field="fact_id"
        )
    )


def compute_variant_fingerprint(value: Mapping[str, Any]) -> str:
    return _sha256(
        _content_projection(
            value, version=VARIANT_FINGERPRINT_VERSION, own_id_field="variant_id"
        )
    )


def compute_evidence_fingerprint(value: Mapping[str, Any]) -> str:
    projection = _content_projection(
        value, version=EVIDENCE_FINGERPRINT_VERSION, own_id_field="evidence_id"
    )
    projection["content"].pop("captured_at", None)
    projection["content"].pop("confirmed_at", None)
    projection["content"].pop("confirmed_by", None)
    return _sha256(projection)


def compute_permission_fingerprint(value: Mapping[str, Any]) -> str:
    projection = _content_projection(
        value,
        version=PERMISSION_FINGERPRINT_VERSION,
        own_id_field="permission_id",
    )
    projection["content"].pop("approved_at", None)
    projection["content"].pop("approved_by", None)
    return _sha256(projection, set_like_paths=frozenset({"content.allowed_operations"}))


def _snapshot_item(value: Mapping[str, Any], id_field: str) -> dict[str, Any]:
    return {
        id_field: value[id_field],
        "revision": value["revision"],
        "content_fingerprint": value["content_fingerprint"],
        "status": value["status"],
        "source_reference": value.get("source_reference"),
        **(
            {"content_hash": value["content_hash"]}
            if "content_hash" in value
            else {}
        ),
    }


def build_truth_snapshot_projection(
    *,
    entities: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    variants: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    permissions: Sequence[Mapping[str, Any]],
    policy_registry_version: str = POLICY_REGISTRY_VERSION,
) -> dict[str, Any]:
    """Build the exact set-like projection hashed by a Truth snapshot."""

    collections = (
        ("entities", entities, "entity_id"),
        ("facts", facts, "fact_id"),
        ("variants", variants, "variant_id"),
        ("evidence", evidence, "evidence_id"),
        ("permissions", permissions, "permission_id"),
    )
    projection: dict[str, Any] = {
        "snapshot_version": TRUTH_SNAPSHOT_VERSION,
        "policy_registry_version": policy_registry_version,
    }
    for name, values, id_field in collections:
        projection[name] = sorted(
            (_snapshot_item(value, id_field) for value in values),
            key=lambda item: str(item[id_field]),
        )
    return projection


def compute_truth_snapshot_fingerprint(
    *,
    entities: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    variants: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    permissions: Sequence[Mapping[str, Any]],
    policy_registry_version: str = POLICY_REGISTRY_VERSION,
) -> str:
    return _sha256(
        build_truth_snapshot_projection(
            entities=entities,
            facts=facts,
            variants=variants,
            evidence=evidence,
            permissions=permissions,
            policy_registry_version=policy_registry_version,
        )
    )
