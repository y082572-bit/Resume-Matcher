"""Pydantic contracts for the Explicit Provenance P2 legacy migration bridge.

See docs/explicit-provenance-stage-p2.md for the full design. These models
describe the CLI-facing ``MigrationReport`` produced by both ``--dry-run`` and
``--apply``; they carry no database or Truth Library I/O of their own.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field


MIGRATION_SCHEMA_VERSION = "1.0"

LEGACY_CATEGORIES = (
    "doswiadczenieZawodowe",
    "osiagnieciaLiczbowe",
    "skalaOdpowiedzialnosci",
    "kompetencje",
    "narzedzia",
    "technologie",
    "certyfikaty",
    "kursy",
    "wyksztalcenie",
    "jezyki",
    "branze",
    "daneOsobowe",
)
DEFERRED_POLICY_CATEGORIES = ("zakazy", "reguly", "tranferowalneKompetencje")


class LegacyClassification(StrEnum):
    MIGRATABLE = "MIGRATABLE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    REJECTED = "REJECTED"
    UNSUPPORTED = "UNSUPPORTED"


class CategoryDisposition(StrEnum):
    """Category-level disposition for dry-run reporting only (not persisted)."""

    PROCESSED = "PROCESSED"
    DEFERRED_POLICY = "DEFERRED_POLICY"


class MigrationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    MIGRATABLE: int = 0
    REQUIRES_REVIEW: int = 0
    REJECTED: int = 0
    UNSUPPORTED: int = 0

    def add(self, classification: LegacyClassification) -> None:
        setattr(self, classification.value, getattr(self, classification.value) + 1)


class CategoryPlan(BaseModel):
    """Category-level dry-run/apply summary (one entry per legacy category)."""

    model_config = ConfigDict(extra="forbid")

    category: str
    disposition: CategoryDisposition
    reason: str
    record_count: int = 0
    counts: MigrationCounts = Field(default_factory=MigrationCounts)


class RecordPlan(BaseModel):
    """One planned legacy record: its identity, fingerprint, and classification."""

    model_config = ConfigDict(extra="forbid")

    category: str
    legacy_record_key: str
    legacy_record_fingerprint: str
    classification: LegacyClassification
    reason: str
    has_stable_id: bool
    entity_type: str | None = None
    fact_type: str | None = None
    employment_legacy_record_key: str | None = None
    duplicate_of: str | None = None


class DuplicateCollision(BaseModel):
    """Two or more raw legacy elements collapsing onto one identity/fingerprint."""

    model_config = ConfigDict(extra="forbid")

    category: str
    legacy_record_key: str
    raw_item_count: int
    unique_identity_count: int


class FingerprintConflict(BaseModel):
    """An existing ledger row whose stored fingerprint no longer matches."""

    model_config = ConfigDict(extra="forbid")

    category: str
    legacy_record_key: str
    stored_fingerprint: str
    current_fingerprint: str


class MigrationReport(BaseModel):
    """The complete, serializable result of one dry-run or apply invocation."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["DRY_RUN", "APPLY"]
    legacy_source_id: str
    legacy_source_path: str
    migration_schema_version: str = MIGRATION_SCHEMA_VERSION
    person_entity_id: UUID4 | None = None
    person_entity_id_required_for_apply: bool = False

    counts: MigrationCounts = Field(default_factory=MigrationCounts)
    category_plans: list[CategoryPlan] = Field(default_factory=list)
    record_plans: list[RecordPlan] = Field(default_factory=list)
    duplicate_text_collisions: list[DuplicateCollision] = Field(default_factory=list)
    fingerprint_conflicts: list[FingerprintConflict] = Field(default_factory=list)

    created_entity_ids: list[UUID4] = Field(default_factory=list)
    created_fact_ids: list[UUID4] = Field(default_factory=list)
    created_map_row_ids: list[UUID4] = Field(default_factory=list)
    reused_map_row_count: int = 0

    aborted: bool = False
    abort_reason: str | None = None

    extra: dict[str, Any] = Field(default_factory=dict)
