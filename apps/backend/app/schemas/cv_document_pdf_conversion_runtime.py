"""Explicit Provenance Stage P6-B3a: converter runtime identity.

``ConverterRuntimeIdentity`` is the closed, content-derived description of
*exactly which* LibreOffice/soffice binary a converter adapter observed at
initialization -- never a caller-supplied claim. Its
``runtime_identity_fingerprint`` is always recomputed from the model's own
other fields at construction time (``compute_converter_runtime_identity_fingerprint``)
and the model fail-closed rejects any mismatch -- a caller can never hand
in an unverified fingerprint and have it accepted at face value.

This stage implements no repository, no SQL, no API, and no frontend for
this model -- it exists purely so a converter adapter has a closed value
object to cache at initialization and re-verify against on every
conversion request. See
``docs/explicit-provenance-stage-p6b3a-pdf-converter-foundation.md``.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator


RUNTIME_IDENTITY_SCHEMA_VERSION = "cv-document-pdf-converter-runtime-identity-schema-v1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

_MAX_SHORT_FIELD_LENGTH = 255
_MAX_PATH_FIELD_LENGTH = 4096
_MAX_IDENTITY_FIELD_LENGTH = 512

_NO_NUL_FIELDS = (
    "implementation_id",
    "implementation_version",
    "executable_canonical_path",
    "executable_file_identity",
    "platform_identity",
    "font_environment_id",
    "runtime_identity_schema_version",
)


def compute_converter_runtime_identity_fingerprint(
    *,
    implementation_id: str,
    implementation_version: str,
    executable_canonical_path: str,
    executable_file_identity: str,
    executable_sha256: str,
    platform_identity: str,
    font_environment_id: str,
    runtime_identity_schema_version: str,
) -> str:
    """Deterministic SHA-256 over every non-fingerprint field of
    ``ConverterRuntimeIdentity`` plus an explicit schema version, so a
    fingerprint computed under an older schema can never collide with one
    computed under a newer field set."""

    payload = {
        "runtime_identity_schema_version": runtime_identity_schema_version,
        "implementation_id": implementation_id,
        "implementation_version": implementation_version,
        "executable_canonical_path": executable_canonical_path,
        "executable_file_identity": executable_file_identity,
        "executable_sha256": executable_sha256,
        "platform_identity": platform_identity,
        "font_environment_id": font_environment_id,
    }
    canonical_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


class ConverterRuntimeIdentity(BaseModel):
    """The closed, content-derived identity of one observed converter
    executable. ``runtime_identity_fingerprint`` is always recomputed and
    verified at construction -- never trusted from caller input alone.
    Build instances through ``build_converter_runtime_identity`` rather
    than passing a fingerprint by hand."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    implementation_id: str = Field(min_length=1, max_length=_MAX_SHORT_FIELD_LENGTH)
    implementation_version: str = Field(min_length=1, max_length=_MAX_SHORT_FIELD_LENGTH)
    executable_canonical_path: str = Field(min_length=1, max_length=_MAX_PATH_FIELD_LENGTH)
    executable_file_identity: str = Field(min_length=1, max_length=_MAX_IDENTITY_FIELD_LENGTH)
    executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    platform_identity: str = Field(min_length=1, max_length=_MAX_SHORT_FIELD_LENGTH)
    font_environment_id: str = Field(min_length=1, max_length=_MAX_SHORT_FIELD_LENGTH)
    runtime_identity_schema_version: str = Field(min_length=1, max_length=_MAX_SHORT_FIELD_LENGTH)
    runtime_identity_fingerprint: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_no_nul_and_fingerprint(self) -> "ConverterRuntimeIdentity":
        for field_name in _NO_NUL_FIELDS:
            if "\x00" in getattr(self, field_name):
                raise ValueError(f"{field_name} must not contain NUL characters")

        expected_fingerprint = compute_converter_runtime_identity_fingerprint(
            implementation_id=self.implementation_id,
            implementation_version=self.implementation_version,
            executable_canonical_path=self.executable_canonical_path,
            executable_file_identity=self.executable_file_identity,
            executable_sha256=self.executable_sha256,
            platform_identity=self.platform_identity,
            font_environment_id=self.font_environment_id,
            runtime_identity_schema_version=self.runtime_identity_schema_version,
        )
        if expected_fingerprint != self.runtime_identity_fingerprint:
            raise ValueError(
                "runtime_identity_fingerprint does not match the fingerprint "
                "recomputed from the other fields"
            )
        return self


def build_converter_runtime_identity(
    *,
    implementation_id: str,
    implementation_version: str,
    executable_canonical_path: str,
    executable_file_identity: str,
    executable_sha256: str,
    platform_identity: str,
    font_environment_id: str,
    runtime_identity_schema_version: str = RUNTIME_IDENTITY_SCHEMA_VERSION,
) -> ConverterRuntimeIdentity:
    """The only intended construction path: computes
    ``runtime_identity_fingerprint`` from the supplied fields and builds a
    ``ConverterRuntimeIdentity`` (whose own validator re-verifies that same
    fingerprint fail-closed)."""

    fingerprint = compute_converter_runtime_identity_fingerprint(
        implementation_id=implementation_id,
        implementation_version=implementation_version,
        executable_canonical_path=executable_canonical_path,
        executable_file_identity=executable_file_identity,
        executable_sha256=executable_sha256,
        platform_identity=platform_identity,
        font_environment_id=font_environment_id,
        runtime_identity_schema_version=runtime_identity_schema_version,
    )
    return ConverterRuntimeIdentity(
        implementation_id=implementation_id,
        implementation_version=implementation_version,
        executable_canonical_path=executable_canonical_path,
        executable_file_identity=executable_file_identity,
        executable_sha256=executable_sha256,
        platform_identity=platform_identity,
        font_environment_id=font_environment_id,
        runtime_identity_schema_version=runtime_identity_schema_version,
        runtime_identity_fingerprint=fingerprint,
    )
