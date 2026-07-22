"""Pydantic contracts for Explicit Provenance Stage P5a: deterministic CV
content generation.

P5a is a pure, in-memory, deterministic transformation of an already
``p5_ready`` P4 ``CvContentPlan`` plus a caller-supplied
``ApprovedFactPayloadSet`` into a ``CvGeneratedContentDraft``. It creates no
new fact, no new entity, and never changes a P4 result partition. It
performs no database I/O, reads no ``TruthFact``/``TruthPermission`` from a
store, never invokes an LLM, and never touches the Master Resume or a
DOCX/PDF renderer. See ``docs/explicit-provenance-stage-p5.md`` for the full
scope boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from app.schemas.cv_content_plan import CvSection, EntryOrderSource
from app.schemas.truth_entity import SHA256_PATTERN
from app.schemas.truth_fact import FACT_TYPE_PATTERN, TransformationOperation
from app.services.truth_fingerprint import canonicalize_json_value


CV_CONTENT_GENERATION_SCHEMA_VERSION = "cv-content-generation-schema-v1"

#: The closed set of ``TransformationOperation`` values P5a can actually
#: render. Every other operation value is unsupported by definition -- see
#: ``cv_content_generation_policy.SUPPORTED_OPERATIONS``.
GeneratedOperation = Literal[
    TransformationOperation.EXACT_COPY,
    TransformationOperation.FORMAT_NORMALIZATION,
    TransformationOperation.REORDER,
]


class GenerationStatus(StrEnum):
    GENERATED = "GENERATED"
    BLOCKED = "BLOCKED"
    INVALID_INPUT = "INVALID_INPUT"


class GenerationMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"


class GenerationApprovalState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"


class GenerationFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class GenerationViolationCode(StrEnum):
    """Closed set of problems the P5a generator can report.

    Never extended by ``startswith``, substring, or text-similarity
    classification -- every code here is reached only through an explicit,
    versioned check in ``cv_content_generation_builder``/``_policy``.
    """

    INPUT_PLAN_NOT_READY = "INPUT_PLAN_NOT_READY"
    INPUT_PLAN_STRUCTURALLY_INVALID = "INPUT_PLAN_STRUCTURALLY_INVALID"
    INPUT_PLAN_STALE = "INPUT_PLAN_STALE"
    INPUT_PLAN_HAS_CONFLICTS = "INPUT_PLAN_HAS_CONFLICTS"
    PAYLOAD_SET_FINGERPRINT_MISMATCH = "PAYLOAD_SET_FINGERPRINT_MISMATCH"
    PAYLOAD_DUPLICATE_FACT_ID = "PAYLOAD_DUPLICATE_FACT_ID"
    PAYLOAD_DUPLICATE_FINGERPRINT = "PAYLOAD_DUPLICATE_FINGERPRINT"
    PAYLOAD_EXTRA_FACT = "PAYLOAD_EXTRA_FACT"
    PAYLOAD_MISSING = "PAYLOAD_MISSING"
    PAYLOAD_PERSON_MISMATCH = "PAYLOAD_PERSON_MISMATCH"
    PAYLOAD_ENTITY_MISMATCH = "PAYLOAD_ENTITY_MISMATCH"
    PAYLOAD_FACT_TYPE_MISMATCH = "PAYLOAD_FACT_TYPE_MISMATCH"
    PAYLOAD_REVISION_MISMATCH = "PAYLOAD_REVISION_MISMATCH"
    PAYLOAD_CONTENT_FINGERPRINT_MISMATCH = "PAYLOAD_CONTENT_FINGERPRINT_MISMATCH"
    PAYLOAD_FINGERPRINT_MISMATCH = "PAYLOAD_FINGERPRINT_MISMATCH"
    PAYLOAD_SHAPE_NOT_ALLOWED = "PAYLOAD_SHAPE_NOT_ALLOWED"
    PAYLOAD_REQUIRED_FIELD_MISSING = "PAYLOAD_REQUIRED_FIELD_MISSING"
    OPERATION_NOT_SUPPORTED_IN_P5A = "OPERATION_NOT_SUPPORTED_IN_P5A"
    OMIT_NOT_GENERATABLE = "OMIT_NOT_GENERATABLE"
    RENDERING_FAILED = "RENDERING_FAILED"
    RESULT_PARTITION_VIOLATION = "RESULT_PARTITION_VIOLATION"
    SECTION_STRUCTURE_MISMATCH = "SECTION_STRUCTURE_MISMATCH"
    EXPERIENCE_STRUCTURE_MISMATCH = "EXPERIENCE_STRUCTURE_MISMATCH"
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"


class ApprovedFactPayloadSnapshot(BaseModel):
    """One caller-approved fact payload, keyed only by stable ids.

    Never stores a ``source_reference``, a legacy record key, or a list
    index -- identity is expressed only through
    ``(person_entity_id, entity_id, fact_id, fact_type, fact_revision,
    fact_content_fingerprint)`` plus the payload content itself.
    """

    model_config = ConfigDict(extra="forbid")

    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    value_json: object
    normalized_value_json: object
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _values_are_canonicalizable(self) -> "ApprovedFactPayloadSnapshot":
        canonicalize_json_value(self.value_json)
        canonicalize_json_value(self.normalized_value_json)
        return self


class ApprovedFactPayloadSet(BaseModel):
    """The complete, caller-supplied snapshot of approved fact payloads.

    Payload order never changes ``payload_set_fingerprint`` -- see
    ``cv_content_generation_builder.compute_fact_payload_set_fingerprint``.
    """

    model_config = ConfigDict(extra="forbid")

    payloads: tuple[ApprovedFactPayloadSnapshot, ...]
    payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)


class GenerationContext(BaseModel):
    """Only the data that can influence a deterministic renderer.

    Never carries job-ad text, a Truth Library snapshot, the Master Resume,
    an LLM provider, a temperature, or a prompt.
    """

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(min_length=1, max_length=32)
    deterministic_template_version: str = Field(min_length=1, max_length=64)
    generation_policy_version: str = Field(min_length=1, max_length=64)


class GenerationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_schema_version: str = Field(min_length=1, max_length=64)
    generation_policy_version: str = Field(min_length=1, max_length=64)
    deterministic_template_version: str = Field(min_length=1, max_length=64)
    generation_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    renderer_contract_version: str = Field(min_length=1, max_length=64)
    renderer_name: str = Field(min_length=1, max_length=255)


class GeneratedCvElement(BaseModel):
    """One deterministically rendered CV element for exactly one ``PlannedFactUse``."""

    model_config = ConfigDict(extra="forbid")

    generated_element_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    section_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    allowed_operation: GeneratedOperation
    placement_order: int = Field(ge=0)
    employment_scope_entity_id: UUID4 | None
    generation_mode: Literal[GenerationMode.DETERMINISTIC] = GenerationMode.DETERMINISTIC
    generated_text: str = Field(min_length=1, max_length=20_000)
    approval_state: Literal[GenerationApprovalState.NOT_REQUIRED] = (
        GenerationApprovalState.NOT_REQUIRED
    )
    generation_provenance: GenerationProvenance


class BlockedGenerationItem(BaseModel):
    """Exactly one ``PlannedFactUse`` that P5a could not generate.

    Never carries a ``generated_text`` -- an omission is never disguised as
    generated content.
    """

    model_config = ConfigDict(extra="forbid")

    blocked_item_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprint: str = Field(pattern=SHA256_PATTERN)
    fact_selection_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    entity_id: UUID4
    fact_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    fact_revision: int = Field(ge=1)
    fact_content_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_section: CvSection
    target_scope: str = Field(min_length=1, max_length=512)
    requested_operation: TransformationOperation
    allowed_operation: TransformationOperation
    violation_code: GenerationViolationCode
    detail: str = Field(min_length=1, max_length=1000)


class CvGeneratedExperienceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experience_entry_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_experience_entry_fingerprint: str = Field(pattern=SHA256_PATTERN)
    employment_entity_id: UUID4
    entry_order: int = Field(ge=0)
    entry_order_source: EntryOrderSource
    header_elements: tuple[GeneratedCvElement, ...] = Field(default_factory=tuple)
    responsibility_elements: tuple[GeneratedCvElement, ...] = Field(default_factory=tuple)
    achievement_elements: tuple[GeneratedCvElement, ...] = Field(default_factory=tuple)


class CvGeneratedExperienceSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["EXPERIENCE"] = "EXPERIENCE"
    section: Literal[CvSection.EXPERIENCE] = CvSection.EXPERIENCE
    section_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_section_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_experience_entries: tuple[CvGeneratedExperienceEntry, ...] = Field(
        default_factory=tuple
    )


class CvGeneratedFlatSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["FLAT"] = "FLAT"
    section: CvSection
    section_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_section_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_elements: tuple[GeneratedCvElement, ...] = Field(default_factory=tuple)


CvGeneratedSection = Annotated[
    Union[CvGeneratedExperienceSection, CvGeneratedFlatSection], Field(discriminator="kind")
]


class CvGeneratedContentDraft(BaseModel):
    """Structural mirror of the source ``CvContentPlan`` -- one generated
    section per plan section, in the same order, never moved or merged."""

    model_config = ConfigDict(extra="forbid")

    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_content_draft_fingerprint: str = Field(pattern=SHA256_PATTERN)
    generated_sections: tuple[CvGeneratedSection, ...]

    @model_validator(mode="after")
    def _exactly_one_section_per_cv_section(self) -> "CvGeneratedContentDraft":
        sections_seen = [entry.section for entry in self.generated_sections]
        if len(sections_seen) != len(set(sections_seen)):
            raise ValueError("generated_sections must contain exactly one entry per CvSection")
        if set(sections_seen) != set(CvSection):
            raise ValueError("generated_sections must cover every CvSection value")
        return self


class CvContentGenerationViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_code: GenerationViolationCode
    detail: str = Field(min_length=1, max_length=1000)
    fact_id: UUID4 | None = None
    planned_fact_use_fingerprint: str | None = None


class CvContentGenerationResult(BaseModel):
    """The single return value of ``generate_cv_content_draft``."""

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: GenerationStatus
    ready_for_p55: bool
    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    payload_set_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    generation_context_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    draft: CvGeneratedContentDraft | None
    blocked_items: tuple[BlockedGenerationItem, ...] = Field(default_factory=tuple)
    violations: tuple[CvContentGenerationViolation, ...] = Field(default_factory=tuple)
    pending_approval_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    omitted_fact_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    conflict_fingerprints: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvContentGenerationResult":
        if self.ready_for_p55 and self.status != GenerationStatus.GENERATED:
            raise ValueError("ready_for_p55 requires GenerationStatus.GENERATED")
        if self.status == GenerationStatus.INVALID_INPUT:
            if self.draft is not None:
                raise ValueError("INVALID_INPUT must carry draft=None")
            if self.blocked_items:
                raise ValueError("INVALID_INPUT must carry no blocked_items")
            if not self.violations:
                raise ValueError("INVALID_INPUT must carry at least one violation")
        if self.status == GenerationStatus.GENERATED:
            if self.draft is None:
                raise ValueError("GENERATED must carry a non-null draft")
            if self.blocked_items:
                raise ValueError("GENERATED must carry no blocked_items")
        if self.status == GenerationStatus.BLOCKED:
            if self.draft is not None and not self.blocked_items:
                raise ValueError("BLOCKED with a draft must carry at least one blocked item")
            if self.draft is None and not self.violations:
                raise ValueError("BLOCKED without a draft must carry at least one violation")
        return self


class CvContentGenerationReplayInput(BaseModel):
    """The exact, generation-derived fields whose change makes a result stale."""

    model_config = ConfigDict(extra="forbid")

    content_plan_fingerprint: str = Field(pattern=SHA256_PATTERN)
    content_plan_schema_version: str = Field(min_length=1, max_length=64)
    content_policy_version: str = Field(min_length=1, max_length=64)
    generation_schema_version: str = Field(min_length=1, max_length=64)
    generation_policy_version: str = Field(min_length=1, max_length=64)
    fact_payload_set_fingerprint: str = Field(pattern=SHA256_PATTERN)
    planned_fact_use_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    generation_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    deterministic_template_version: str = Field(min_length=1, max_length=64)
    generated_element_fingerprints: tuple[str, ...] = Field(default_factory=tuple)
    blocked_item_fingerprints: tuple[str, ...] = Field(default_factory=tuple)


class CvContentGenerationReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: GenerationFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
