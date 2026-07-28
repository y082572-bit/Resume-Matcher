"""Explicit Provenance Stage P6-B3b-A: the ``FinalConfirmedPdfArtifact``
domain model and its cross-line current-authority observation.

``FinalConfirmedPdfArtifact`` is a wholly new, immutable, content-addressed
PDF artifact line -- strictly additive to (never a replacement of, never
built from) the legacy ``ConfirmedCvPdfArtifact`` chain
(``ValidatedCvDocxSnapshot`` -> ``ConfirmedCvPdfArtifact``). It is built
exclusively from a ``FinalDocxSnapshot`` (Explicit Provenance Stage P6-A
Final DOCX Snapshot addendum) and a ``ConverterRuntimeIdentity`` (Explicit
Provenance Stage P6-B3a) -- it never accepts ``approved_cv_content_fingerprint``,
``content_plan_fingerprint``, or the legacy ``provenance_mode``.

Identity is always a recomputed SHA-256 fingerprint, never a caller-supplied
claim: both ``conversion_request_fingerprint`` and ``artifact_fingerprint``
are always independently recomputed from the model's own other fields at
construction time and fail-closed rejected on any mismatch, exactly as
``ConverterRuntimeIdentity.runtime_identity_fingerprint`` already does. A
caller builds instances through :func:`build_final_confirmed_pdf_artifact`
rather than ever passing either fingerprint by hand.

Stage P6-B3b-A ships no composition root, no ``DocxToPdfConverter`` call,
no replay orchestration, no freshness evaluation, no API, and no frontend
for this model -- see P6-B3b-B for all of those.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity
from app.schemas.truth_entity import SHA256_PATTERN
from app.services.truth_fingerprint import canonical_json_bytes


CONVERSION_REQUEST_FINGERPRINT_SCHEMA_VERSION = (
    "cv-final-confirmed-pdf-request-fingerprint-schema-v1"
)
ARTIFACT_FINGERPRINT_SCHEMA_VERSION = "cv-final-confirmed-pdf-artifact-fingerprint-schema-v1"

#: Hard limit on the canonical JSON serialization of a ``ConverterRuntimeIdentity``
#: that is ever persisted as ``runtime_identity_json`` -- mirrored exactly by
#: the SQL CHECK constraint on ``cv_final_confirmed_pdf_artifacts.runtime_identity_json``.
RUNTIME_IDENTITY_JSON_MAX_BYTES = 8192

_MAX_POLICY_VERSION_LENGTH = 64


# -- Cross-line lineage --------------------------------------------------------


class ConfirmedPdfLineageKind(StrEnum):
    """The exactly two lineages ``cv_confirmed_pdf_current_authority`` can
    point at. Never a third value -- a new lineage kind is always an
    explicit, separately reviewed schema-version bump, never an implicit
    extension of this closed set."""

    LEGACY_VALIDATED_DOCX = "LEGACY_VALIDATED_DOCX"
    FINAL_DOCX_SNAPSHOT = "FINAL_DOCX_SNAPSHOT"


# -- Canonical runtime identity JSON -------------------------------------------


def compute_canonical_runtime_identity_json(runtime_identity: ConverterRuntimeIdentity) -> str:
    """The exact, canonical JSON text persisted as
    ``cv_final_confirmed_pdf_artifacts.runtime_identity_json`` -- always this
    function's output, never an ad hoc ``json.dumps`` call, and always
    re-derivable byte-for-byte from ``runtime_identity`` alone."""

    return canonical_json_bytes(runtime_identity.model_dump(mode="json")).decode("utf-8")


# -- Fingerprint functions ------------------------------------------------------


def compute_final_confirmed_pdf_request_fingerprint(
    *,
    owner_key_fingerprint: str,
    source_final_snapshot_fingerprint: str,
    source_final_docx_sha256: str,
    runtime_identity_fingerprint: str,
    conversion_policy_version: str,
    request_schema_version: str = CONVERSION_REQUEST_FINGERPRINT_SCHEMA_VERSION,
) -> str:
    content = {
        "fingerprint_version": request_schema_version,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "source_final_snapshot_fingerprint": source_final_snapshot_fingerprint,
            "source_final_docx_sha256": source_final_docx_sha256,
            "runtime_identity_fingerprint": runtime_identity_fingerprint,
            "conversion_policy_version": conversion_policy_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def compute_final_confirmed_pdf_artifact_fingerprint(
    *,
    conversion_request_fingerprint: str,
    pdf_sha256: str,
    artifact_schema_version: str = ARTIFACT_FINGERPRINT_SCHEMA_VERSION,
) -> str:
    content = {
        "fingerprint_version": artifact_schema_version,
        "content": {
            "conversion_request_fingerprint": conversion_request_fingerprint,
            "pdf_sha256": pdf_sha256,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


# -- Domain artifact --------------------------------------------------------------


class FinalConfirmedPdfArtifact(BaseModel):
    """An immutable, content-addressed confirmed PDF built from a
    ``FinalDocxSnapshot``. Never carries a mutable lifecycle flag (no
    ``is_current``/``superseded``/``deleted``/status field) -- current
    status for this line is always exclusively expressed by
    ``cv_confirmed_pdf_current_authority``, never a column on this row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_fingerprint: str = Field(pattern=SHA256_PATTERN)
    owner_key: JobArtifactOwnerKey
    source_final_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_final_docx_sha256: str = Field(pattern=SHA256_PATTERN)
    conversion_request_fingerprint: str = Field(pattern=SHA256_PATTERN)
    runtime_identity: ConverterRuntimeIdentity
    runtime_identity_fingerprint: str = Field(pattern=SHA256_PATTERN)
    conversion_policy_version: str = Field(min_length=1, max_length=_MAX_POLICY_VERSION_LENGTH)
    pdf_sha256: str = Field(pattern=SHA256_PATTERN)
    conversion_request_fingerprint_schema_version: str = Field(
        min_length=1, max_length=128
    )
    artifact_fingerprint_schema_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _invariants(self) -> "FinalConfirmedPdfArtifact":
        if not self.conversion_policy_version.strip():
            raise ValueError("conversion_policy_version must not be blank")
        if self.runtime_identity_fingerprint != self.runtime_identity.runtime_identity_fingerprint:
            raise ValueError(
                "runtime_identity_fingerprint does not match runtime_identity.runtime_identity_fingerprint"
            )

        expected_request_fingerprint = compute_final_confirmed_pdf_request_fingerprint(
            owner_key_fingerprint=self.owner_key.owner_key_fingerprint,
            source_final_snapshot_fingerprint=self.source_final_snapshot_fingerprint,
            source_final_docx_sha256=self.source_final_docx_sha256,
            runtime_identity_fingerprint=self.runtime_identity_fingerprint,
            conversion_policy_version=self.conversion_policy_version,
            request_schema_version=self.conversion_request_fingerprint_schema_version,
        )
        if expected_request_fingerprint != self.conversion_request_fingerprint:
            raise ValueError(
                "conversion_request_fingerprint does not match a fingerprint recomputed "
                "from the other fields"
            )

        expected_artifact_fingerprint = compute_final_confirmed_pdf_artifact_fingerprint(
            conversion_request_fingerprint=self.conversion_request_fingerprint,
            pdf_sha256=self.pdf_sha256,
            artifact_schema_version=self.artifact_fingerprint_schema_version,
        )
        if expected_artifact_fingerprint != self.artifact_fingerprint:
            raise ValueError(
                "artifact_fingerprint does not match a fingerprint recomputed from the "
                "other fields"
            )

        runtime_identity_json = compute_canonical_runtime_identity_json(self.runtime_identity)
        if len(runtime_identity_json.encode("utf-8")) > RUNTIME_IDENTITY_JSON_MAX_BYTES:
            raise ValueError(
                f"canonical runtime_identity_json exceeds {RUNTIME_IDENTITY_JSON_MAX_BYTES} UTF-8 bytes"
            )
        return self


def build_final_confirmed_pdf_artifact(
    *,
    owner_key: JobArtifactOwnerKey,
    source_final_snapshot_fingerprint: str,
    source_final_docx_sha256: str,
    runtime_identity: ConverterRuntimeIdentity,
    conversion_policy_version: str,
    pdf_sha256: str,
) -> FinalConfirmedPdfArtifact:
    """The only intended construction path -- the caller never supplies
    ``conversion_request_fingerprint`` or ``artifact_fingerprint`` directly;
    both are always derived here (and re-verified by the model's own
    validator)."""

    request_fingerprint = compute_final_confirmed_pdf_request_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_final_snapshot_fingerprint=source_final_snapshot_fingerprint,
        source_final_docx_sha256=source_final_docx_sha256,
        runtime_identity_fingerprint=runtime_identity.runtime_identity_fingerprint,
        conversion_policy_version=conversion_policy_version,
    )
    artifact_fingerprint = compute_final_confirmed_pdf_artifact_fingerprint(
        conversion_request_fingerprint=request_fingerprint,
        pdf_sha256=pdf_sha256,
    )
    return FinalConfirmedPdfArtifact(
        artifact_fingerprint=artifact_fingerprint,
        owner_key=owner_key,
        source_final_snapshot_fingerprint=source_final_snapshot_fingerprint,
        source_final_docx_sha256=source_final_docx_sha256,
        conversion_request_fingerprint=request_fingerprint,
        runtime_identity=runtime_identity,
        runtime_identity_fingerprint=runtime_identity.runtime_identity_fingerprint,
        conversion_policy_version=conversion_policy_version,
        pdf_sha256=pdf_sha256,
        conversion_request_fingerprint_schema_version=CONVERSION_REQUEST_FINGERPRINT_SCHEMA_VERSION,
        artifact_fingerprint_schema_version=ARTIFACT_FINGERPRINT_SCHEMA_VERSION,
    )


# -- Cross-line current-authority observation ------------------------------------


class ConfirmedPdfCurrentAuthorityObservation(BaseModel):
    """A read-only snapshot of one owner's row (or absence) in
    ``cv_confirmed_pdf_current_authority``. Never a mutable cursor -- every
    read produces a fresh, independent instance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exists: bool
    lineage_kind: ConfirmedPdfLineageKind | None = None
    current_artifact_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    slot_version: int | None = None

    @model_validator(mode="after")
    def _invariants(self) -> "ConfirmedPdfCurrentAuthorityObservation":
        if self.exists:
            if self.lineage_kind is None:
                raise ValueError("exists=True requires lineage_kind")
            if self.current_artifact_fingerprint is None:
                raise ValueError("exists=True requires current_artifact_fingerprint")
            if self.slot_version is None or self.slot_version < 1:
                raise ValueError("exists=True requires slot_version >= 1")
        else:
            if self.lineage_kind is not None:
                raise ValueError("exists=False must carry no lineage_kind")
            if self.current_artifact_fingerprint is not None:
                raise ValueError("exists=False must carry no current_artifact_fingerprint")
            if self.slot_version is not None:
                raise ValueError("exists=False must carry no slot_version")
        return self
