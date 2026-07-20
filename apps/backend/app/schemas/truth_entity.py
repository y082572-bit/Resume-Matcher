"""Pydantic contracts for stable Explicit Provenance entity identity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator


TRUTH_SCHEMA_VERSION = "1.0"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class EntityType(StrEnum):
    PERSON = "PERSON"
    EMPLOYMENT = "EMPLOYMENT"
    PROJECT = "PROJECT"
    EDUCATION = "EDUCATION"
    CERTIFICATION = "CERTIFICATION"
    COURSE = "COURSE"
    LANGUAGE = "LANGUAGE"
    SKILL = "SKILL"
    TOOL = "TOOL"
    TECHNOLOGY = "TECHNOLOGY"
    AWARD = "AWARD"
    CUSTOM_ENTITY = "CUSTOM_ENTITY"


class EntityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


def _require_timezone(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamp must be timezone-aware")
    return value


class TruthEntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: UUID4 = Field(default_factory=uuid4)
    entity_type: EntityType
    parent_entity_id: UUID4 | None = None
    display_name: str = Field(min_length=1, max_length=500)
    status: EntityStatus = EntityStatus.ACTIVE
    source_reference: str | None = Field(default=None, max_length=1024)
    schema_version: Literal["1.0"] = TRUTH_SCHEMA_VERSION

    @field_validator("display_name")
    @classmethod
    def normalized_display_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("display_name cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_parent(self) -> "TruthEntityCreate":
        if self.parent_entity_id == self.entity_id:
            raise ValueError("parent_entity_id cannot reference the entity itself")
        return self


class TruthEntityRead(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    entity_id: UUID4
    entity_type: EntityType
    parent_entity_id: UUID4 | None
    display_name: str
    status: EntityStatus
    source_reference: str | None
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

    @model_validator(mode="after")
    def validate_archive(self) -> "TruthEntityRead":
        if (self.status == EntityStatus.ARCHIVED) != (self.archived_at is not None):
            raise ValueError("ARCHIVED status and archived_at must change together")
        if self.parent_entity_id == self.entity_id:
            raise ValueError("parent_entity_id cannot reference the entity itself")
        return self


class TruthEntityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    parent_entity_id: UUID4 | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=500)
    status: EntityStatus | None = None
    source_reference: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def require_change(self) -> "TruthEntityUpdate":
        changed = self.model_fields_set - {"expected_revision"}
        if not changed:
            raise ValueError("entity update requires at least one changed field")
        return self
