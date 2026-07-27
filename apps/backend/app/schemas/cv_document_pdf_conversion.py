"""Explicit Provenance Stage P6-B3a: the DOCX->PDF conversion result
contract.

``DocxToPdfConversionResult`` is a closed, ``frozen=True``/``extra="forbid"``
value object every ``DocxToPdfConverter`` implementation must return.
Its own ``model_validator`` -- not producer discipline -- enforces the
success/failure invariants: ``SUCCEEDED`` always carries non-empty
``pdf_bytes`` whose SHA-256 exactly matches ``pdf_sha256``, plus a
``runtime_identity`` and no ``error_code``; every other status carries no
``pdf_bytes``/``pdf_sha256`` and a mandatory non-empty ``error_code``.

This stage implements no repository, no SQL, no API, and no frontend for
this model. See
``docs/explicit-provenance-stage-p6b3a-pdf-converter-foundation.md``.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity
from app.schemas.truth_entity import SHA256_PATTERN


_MAX_DIAGNOSTICS_ENTRIES = 8
_MAX_DIAGNOSTIC_LENGTH = 1000
_MAX_POLICY_VERSION_LENGTH = 255
_MAX_ERROR_CODE_LENGTH = 255


class DocxToPdfConversionStatus(StrEnum):
    """The closed set of outcomes a ``DocxToPdfConverter`` may return.
    Exactly one of these is ever set -- a caller never juggles a raised
    exception alongside a separate result."""

    SUCCEEDED = "SUCCEEDED"
    INVALID_REQUEST = "INVALID_REQUEST"
    CONVERTER_UNAVAILABLE = "CONVERTER_UNAVAILABLE"
    PROCESS_START_FAILED = "PROCESS_START_FAILED"
    PROCESS_OUTPUT_LIMIT_EXCEEDED = "PROCESS_OUTPUT_LIMIT_EXCEEDED"
    CONVERSION_TIMEOUT = "CONVERSION_TIMEOUT"
    PROCESS_TERMINATION_FAILED = "PROCESS_TERMINATION_FAILED"
    CONVERSION_FAILED = "CONVERSION_FAILED"
    PDF_OUTPUT_MISSING = "PDF_OUTPUT_MISSING"
    PDF_OUTPUT_LOCKED = "PDF_OUTPUT_LOCKED"
    PDF_OUTPUT_UNREADABLE = "PDF_OUTPUT_UNREADABLE"
    PDF_OUTPUT_CHANGED_DURING_READ = "PDF_OUTPUT_CHANGED_DURING_READ"
    PDF_OUTPUT_TOO_LARGE = "PDF_OUTPUT_TOO_LARGE"
    PDF_OUTPUT_INVALID = "PDF_OUTPUT_INVALID"
    WORKSPACE_SETUP_FAILED = "WORKSPACE_SETUP_FAILED"
    WORKSPACE_CLEANUP_FAILED = "WORKSPACE_CLEANUP_FAILED"
    CONVERTER_CONTRACT_VIOLATION = "CONVERTER_CONTRACT_VIOLATION"


class DocxToPdfConversionResult(BaseModel):
    """The closed result of one ``convert_docx_to_pdf`` call. Every
    invariant below is enforced by this model's own validators -- never
    left to producer discipline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DocxToPdfConversionStatus
    pdf_bytes: bytes | None = None
    pdf_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    runtime_identity: ConverterRuntimeIdentity | None = None
    conversion_policy_version: str = Field(min_length=1, max_length=_MAX_POLICY_VERSION_LENGTH)
    error_code: str | None = Field(default=None, max_length=_MAX_ERROR_CODE_LENGTH)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _bound_diagnostics(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (tuple, list)):
            raise ValueError("diagnostics must be a tuple or list of strings")
        materialized = tuple(value)
        if len(materialized) > _MAX_DIAGNOSTICS_ENTRIES:
            raise ValueError(f"diagnostics must contain at most {_MAX_DIAGNOSTICS_ENTRIES} entries")
        for entry in materialized:
            if not isinstance(entry, str):
                raise ValueError("each diagnostics entry must be a string")
            if "\x00" in entry:
                raise ValueError("diagnostics entries must not contain NUL characters")
            if len(entry) > _MAX_DIAGNOSTIC_LENGTH:
                raise ValueError(
                    f"each diagnostics entry must be at most {_MAX_DIAGNOSTIC_LENGTH} characters"
                )
        return materialized

    @field_validator("conversion_policy_version")
    @classmethod
    def _validate_policy_version(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("conversion_policy_version must not contain NUL characters")
        return value

    @field_validator("error_code")
    @classmethod
    def _validate_error_code(cls, value: str | None) -> str | None:
        if value is not None:
            if not value:
                raise ValueError("error_code must not be empty when provided")
            if "\x00" in value:
                raise ValueError("error_code must not contain NUL characters")
        return value

    @model_validator(mode="after")
    def _validate_status_invariants(self) -> "DocxToPdfConversionResult":
        if self.status == DocxToPdfConversionStatus.SUCCEEDED:
            if not self.pdf_bytes:
                raise ValueError("SUCCEEDED requires non-empty pdf_bytes")
            if self.pdf_sha256 is None:
                raise ValueError("SUCCEEDED requires pdf_sha256")
            if hashlib.sha256(self.pdf_bytes).hexdigest() != self.pdf_sha256:
                raise ValueError("pdf_sha256 does not match sha256(pdf_bytes)")
            if self.runtime_identity is None:
                raise ValueError("SUCCEEDED requires runtime_identity")
            if self.error_code is not None:
                raise ValueError("SUCCEEDED must carry no error_code")
        else:
            if self.pdf_bytes is not None:
                raise ValueError(f"{self.status} must carry no pdf_bytes")
            if self.pdf_sha256 is not None:
                raise ValueError(f"{self.status} must carry no pdf_sha256")
            if not self.error_code:
                raise ValueError(f"{self.status} requires a non-empty error_code")
        return self
