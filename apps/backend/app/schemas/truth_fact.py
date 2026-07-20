"""Pydantic contracts for Explicit Provenance facts and audit records."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from app.schemas.truth_entity import SHA256_PATTERN, _require_timezone
from app.services.truth_fingerprint import canonicalize_json_value


FACT_TYPE_PATTERN = r"^[A-Z][A-Z0-9_]{0,127}$"
PERMISSION_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_PERMISSION_UTC_PATTERN = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"\.(?P<microsecond>[0-9]{6})Z$"
)


def normalize_permission_datetime(value: datetime | None) -> datetime | None:
    """Normalize permission boundaries before validation and persistence."""

    aware = _require_timezone(value)
    return None if aware is None else aware.astimezone(timezone.utc)


def serialize_permission_datetime(value: datetime | None) -> str | None:
    normalized = normalize_permission_datetime(value)
    if normalized is None:
        return None
    serialized = (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}"
        f"T{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}"
        f".{normalized.microsecond:06d}Z"
    )
    if len(serialized) != 27:  # Defensive invariant for the supported datetime range.
        raise ValueError("permission timestamp serialization must be fixed-width")
    return serialized


def parse_permission_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return normalize_permission_datetime(value)
    try:
        match = _PERMISSION_UTC_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("not fixed-width canonical UTC")
        parsed = datetime(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=int(match.group("second")),
            microsecond=int(match.group("microsecond")),
            tzinfo=timezone.utc,
        )
    except ValueError as error:
        raise ValueError("permission timestamp must use canonical UTC format") from error
    return parsed


class FactStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class Transferability(StrEnum):
    GLOBAL_TRANSFERABLE = "GLOBAL_TRANSFERABLE"
    ROLE_TRANSFERABLE = "ROLE_TRANSFERABLE"
    INDUSTRY_TRANSFERABLE = "INDUSTRY_TRANSFERABLE"
    EMPLOYMENT_SCOPED = "EMPLOYMENT_SCOPED"
    ENTITY_SCOPED = "ENTITY_SCOPED"
    NON_TRANSFERABLE = "NON_TRANSFERABLE"
    EXACT_ONLY = "EXACT_ONLY"


class VariantStatus(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class TransformationOperation(StrEnum):
    EXACT_COPY = "EXACT_COPY"
    FORMAT_NORMALIZATION = "FORMAT_NORMALIZATION"
    CONTROLLED_REPHRASE = "CONTROLLED_REPHRASE"
    FLATTEN_FOR_LOWER_ROLE = "FLATTEN_FOR_LOWER_ROLE"
    ELEVATE_PRESENTATION_FOR_SENIOR_ROLE = "ELEVATE_PRESENTATION_FOR_SENIOR_ROLE"
    COMBINE_APPROVED_FACTS = "COMBINE_APPROVED_FACTS"
    SPLIT_APPROVED_FACT = "SPLIT_APPROVED_FACT"
    SUMMARIZE_APPROVED_FACTS = "SUMMARIZE_APPROVED_FACTS"
    OMIT = "OMIT"
    REORDER = "REORDER"


class VariantOperation(StrEnum):
    EXACT_COPY = "EXACT_COPY"
    FORMAT_NORMALIZATION = "FORMAT_NORMALIZATION"
    CONTROLLED_REPHRASE = "CONTROLLED_REPHRASE"


class EvidenceType(StrEnum):
    DOCUMENT = "DOCUMENT"
    USER_CONFIRMATION = "USER_CONFIRMATION"
    IMPORT = "IMPORT"
    SYSTEM_RECORD = "SYSTEM_RECORD"


class EvidenceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class PermissionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVOKED = "REVOKED"
    ARCHIVED = "ARCHIVED"


def _archive_matches(status: StrEnum, archived_at: datetime | None) -> None:
    if (status.value == "ARCHIVED") != (archived_at is not None):
        raise ValueError("ARCHIVED status and archived_at must change together")


class _ReadBase(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    schema_version: Literal["1.0"]
    revision: int = Field(ge=1)
    content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None

    @field_validator("created_at", "updated_at", "archived_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value)


class TruthFactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: UUID4 = Field(default_factory=uuid4)
    entity_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    value_json: Any
    normalized_value_json: Any
    status: FactStatus = FactStatus.DRAFT
    use_in_cv: bool = False
    requires_approval: bool = True
    transferability: Transferability
    employment_scope_entity_id: UUID4 | None = None
    source_reference: str | None = Field(default=None, max_length=1024)
    schema_version: Literal["1.0"] = "1.0"

    @field_validator("value_json", "normalized_value_json")
    @classmethod
    def value_is_canonicalizable(cls, value: Any) -> Any:
        return canonicalize_json_value(value)


class TruthFactRead(_ReadBase):
    fact_id: UUID4
    entity_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    value_json: Any
    normalized_value_json: Any
    status: FactStatus
    use_in_cv: bool
    requires_approval: bool
    transferability: Transferability
    employment_scope_entity_id: UUID4 | None
    source_reference: str | None

    @model_validator(mode="after")
    def validate_archive(self) -> "TruthFactRead":
        _archive_matches(self.status, self.archived_at)
        return self


class TruthFactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    value_json: Any | None = None
    normalized_value_json: Any | None = None
    status: FactStatus | None = None
    use_in_cv: bool | None = None
    requires_approval: bool | None = None
    transferability: Transferability | None = None
    employment_scope_entity_id: UUID4 | None = None
    source_reference: str | None = Field(default=None, max_length=1024)

    @field_validator("value_json", "normalized_value_json")
    @classmethod
    def value_is_canonicalizable(cls, value: Any | None) -> Any | None:
        return None if value is None else canonicalize_json_value(value)

    @model_validator(mode="after")
    def require_change(self) -> "TruthFactUpdate":
        if not self.model_fields_set - {"expected_revision"}:
            raise ValueError("fact update requires at least one changed field")
        return self


class TruthFactVariantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: UUID4 = Field(default_factory=uuid4)
    fact_id: UUID4
    text: str = Field(min_length=1, max_length=10_000)
    normalized_text: str = Field(min_length=1, max_length=10_000)
    status: VariantStatus = VariantStatus.DRAFT
    operation: VariantOperation
    schema_version: Literal["1.0"] = "1.0"


class TruthFactVariantRead(_ReadBase):
    variant_id: UUID4
    fact_id: UUID4
    text: str
    normalized_text: str
    status: VariantStatus
    operation: VariantOperation

    @model_validator(mode="after")
    def validate_archive(self) -> "TruthFactVariantRead":
        _archive_matches(self.status, self.archived_at)
        return self


class TruthFactVariantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=10_000)
    normalized_text: str | None = Field(default=None, min_length=1, max_length=10_000)
    status: VariantStatus | None = None
    operation: VariantOperation | None = None

    @model_validator(mode="after")
    def require_change(self) -> "TruthFactVariantUpdate":
        if not self.model_fields_set - {"expected_revision"}:
            raise ValueError("variant update requires at least one changed field")
        return self


class TruthEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID4 = Field(default_factory=uuid4)
    fact_id: UUID4
    evidence_type: EvidenceType
    source_entity_id: UUID4 | None = None
    source_reference: str | None = Field(default=None, max_length=1024)
    source_locator: str | None = Field(default=None, max_length=2048)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    status: EvidenceStatus = EvidenceStatus.REVIEW_REQUIRED
    captured_at: datetime
    confirmed_at: datetime | None = None
    confirmed_by: str | None = Field(default=None, max_length=500)
    schema_version: Literal["1.0"] = "1.0"

    @field_validator("captured_at", "confirmed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value)


class TruthEvidenceRead(_ReadBase):
    evidence_id: UUID4
    fact_id: UUID4
    evidence_type: EvidenceType
    source_entity_id: UUID4 | None
    source_reference: str | None
    source_locator: str | None
    content_hash: str = Field(pattern=SHA256_PATTERN)
    status: EvidenceStatus
    captured_at: datetime
    confirmed_at: datetime | None
    confirmed_by: str | None

    @field_validator("captured_at", "confirmed_at")
    @classmethod
    def evidence_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value)

    @model_validator(mode="after")
    def validate_archive(self) -> "TruthEvidenceRead":
        _archive_matches(self.status, self.archived_at)
        return self


class TruthEvidenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    source_entity_id: UUID4 | None = None
    source_reference: str | None = Field(default=None, max_length=1024)
    source_locator: str | None = Field(default=None, max_length=2048)
    content_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    status: EvidenceStatus | None = None
    captured_at: datetime | None = None
    confirmed_at: datetime | None = None
    confirmed_by: str | None = Field(default=None, max_length=500)

    @field_validator("captured_at", "confirmed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _require_timezone(value)

    @model_validator(mode="after")
    def require_change(self) -> "TruthEvidenceUpdate":
        if not self.model_fields_set - {"expected_revision"}:
            raise ValueError("evidence update requires at least one changed field")
        return self


class TruthPermissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_id: UUID4 = Field(default_factory=uuid4)
    fact_id: UUID4
    target_scope: str = Field(min_length=1, max_length=512)
    allowed_operations: list[TransformationOperation] = Field(min_length=1)
    constraints_json: dict[str, Any] = Field(default_factory=dict)
    status: PermissionStatus = PermissionStatus.REVIEW_REQUIRED
    approved_by: str | None = Field(default=None, max_length=500)
    approved_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    schema_version: Literal["1.0"] = "1.0"

    @field_validator("allowed_operations")
    @classmethod
    def operations_are_canonical(
        cls, value: list[TransformationOperation]
    ) -> list[TransformationOperation]:
        return sorted(set(value), key=lambda item: item.value)

    @field_validator("constraints_json")
    @classmethod
    def constraints_are_canonicalizable(cls, value: dict[str, Any]) -> dict[str, Any]:
        normalized = canonicalize_json_value(value)
        if not isinstance(normalized, dict):
            raise ValueError("constraints_json must be an object")
        return normalized

    @field_validator("approved_at", "valid_from", "valid_until")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return normalize_permission_datetime(value)

    @model_validator(mode="after")
    def validate_window(self) -> "TruthPermissionCreate":
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class TruthPermissionRead(_ReadBase):
    permission_id: UUID4
    fact_id: UUID4
    target_scope: str
    allowed_operations: list[TransformationOperation]
    constraints_json: dict[str, Any]
    status: PermissionStatus
    approved_by: str | None
    approved_at: datetime | None
    valid_from: datetime | None
    valid_until: datetime | None

    @field_validator("approved_at", "valid_from", "valid_until")
    @classmethod
    def permission_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return normalize_permission_datetime(value)

    @model_validator(mode="after")
    def validate_permission(self) -> "TruthPermissionRead":
        _archive_matches(self.status, self.archived_at)
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class TruthPermissionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    target_scope: str | None = Field(default=None, min_length=1, max_length=512)
    allowed_operations: list[TransformationOperation] | None = None
    constraints_json: dict[str, Any] | None = None
    status: PermissionStatus | None = None
    approved_by: str | None = Field(default=None, max_length=500)
    approved_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator("allowed_operations")
    @classmethod
    def operations_are_canonical(
        cls, value: list[TransformationOperation] | None
    ) -> list[TransformationOperation] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("allowed_operations cannot be empty")
        return sorted(set(value), key=lambda item: item.value)

    @field_validator("constraints_json")
    @classmethod
    def constraints_are_canonicalizable(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        normalized = canonicalize_json_value(value)
        if not isinstance(normalized, dict):
            raise ValueError("constraints_json must be an object")
        return normalized

    @field_validator("approved_at", "valid_from", "valid_until")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return normalize_permission_datetime(value)

    @model_validator(mode="after")
    def require_change(self) -> "TruthPermissionUpdate":
        if not self.model_fields_set - {"expected_revision"}:
            raise ValueError("permission update requires at least one changed field")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        return self


class TruthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: Literal["truth-snapshot-v1"] = "truth-snapshot-v1"
    fingerprint: str = Field(pattern=SHA256_PATTERN)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    variants: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    permissions: list[dict[str, Any]] = Field(default_factory=list)
    policy_registry_version: str
