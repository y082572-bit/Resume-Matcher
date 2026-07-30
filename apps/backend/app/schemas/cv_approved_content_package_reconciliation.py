"""Pydantic contracts for Explicit Provenance Stage P6-C1b-A: the read-only
Approved Content Package reconciliation report.

``cv_approved_content_package_reconciliation.run_reconciliation`` is the only
producer of these models. It never performs an ``INSERT``/``UPDATE``/
``DELETE``, never promotes an authority row, and never repairs a row it
finds inconsistent -- it only ever reads and reports.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.truth_entity import SHA256_PATTERN


class ApprovedContentPackageReconciliationIssueCode(StrEnum):
    OWNER_MISSING = "OWNER_MISSING"
    OWNER_PAYLOAD_MISMATCH = "OWNER_PAYLOAD_MISMATCH"
    PACKAGE_FINGERPRINT_MISMATCH = "PACKAGE_FINGERPRINT_MISMATCH"
    DOCUMENT_INPUT_FINGERPRINT_MISMATCH = "DOCUMENT_INPUT_FINGERPRINT_MISMATCH"
    PAYLOAD_SHA256_MISMATCH = "PAYLOAD_SHA256_MISMATCH"
    PAYLOAD_SIZE_MISMATCH = "PAYLOAD_SIZE_MISMATCH"
    PAYLOAD_NOT_CANONICAL = "PAYLOAD_NOT_CANONICAL"
    PAYLOAD_RECONSTRUCTION_FAILED = "PAYLOAD_RECONSTRUCTION_FAILED"
    APPROVAL_DECISIONS_FINGERPRINT_MISMATCH = "APPROVAL_DECISIONS_FINGERPRINT_MISMATCH"
    FACT_SELECTION_IDENTITY_FINGERPRINT_MISMATCH = "FACT_SELECTION_IDENTITY_FINGERPRINT_MISMATCH"
    AUTHORITY_MISSING_PACKAGE = "AUTHORITY_MISSING_PACKAGE"
    AUTHORITY_OWNER_MISMATCH = "AUTHORITY_OWNER_MISMATCH"
    INVALID_SLOT_VERSION = "INVALID_SLOT_VERSION"
    IMMUTABILITY_TRIGGER_MISSING = "IMMUTABILITY_TRIGGER_MISSING"
    AUTHORITY_TRIGGER_MISSING = "AUTHORITY_TRIGGER_MISSING"
    SCHEMA_MANIFEST_MISMATCH = "SCHEMA_MANIFEST_MISMATCH"
    UNSUPPORTED_PACKAGE_SCHEMA_VERSION = "UNSUPPORTED_PACKAGE_SCHEMA_VERSION"
    UNSUPPORTED_PACKAGE_POLICY_VERSION = "UNSUPPORTED_PACKAGE_POLICY_VERSION"


class ApprovedContentPackageReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_code: ApprovedContentPackageReconciliationIssueCode
    package_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    owner_key_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    detail: str = Field(min_length=1, max_length=1000)


class ApprovedContentPackageReconciliationReport(BaseModel):
    """The single return value of ``run_reconciliation``. Always reflects
    exactly what was read -- never mutates a row count, a slot pointer, or
    any table this stage inspects."""

    model_config = ConfigDict(extra="forbid")

    package_row_count: int = Field(ge=0)
    authority_row_count: int = Field(ge=0)
    issues: tuple[ApprovedContentPackageReconciliationIssue, ...] = Field(default_factory=tuple)
