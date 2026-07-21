"""SQLAlchemy ORM models for Resume Matcher.

A single declarative ``Base`` backs all tables (doc tables migrated from
TinyDB plus the new ``applications`` and ``api_keys`` tables). The facade in
``app/database.py`` converts ORM rows to plain dicts so the rest of the app
never sees ORM objects — preserving the TinyDB-era contracts.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Timestamps are stored as strings (not native datetimes) to preserve the
    TinyDB-era behavior: code compares them lexically and returns them to
    clients verbatim.
    """
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    """Declarative base shared by every table."""


class Resume(Base):
    """A resume document (master or tailored)."""

    __tablename__ = "resumes"

    resume_id: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String, default="md")
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_status: Mapped[str] = mapped_column(String, default="pending")
    cover_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    outreach_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    interview_prep: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # original_markdown has *absence* semantics in the TinyDB era: the key was
    # omitted entirely when None. The facade reproduces that by only emitting
    # the key when this column is non-null.
    original_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    lifecycle_token: Mapped[str] = mapped_column(
        String,
        unique=True,
        nullable=False,
        default=lambda: str(uuid4()),
    )

    __table_args__ = (
        # At most one master resume. Partial unique index enforces the invariant
        # at the storage layer; ``_master_resume_lock`` remains the primary
        # (race-free) mechanism in the facade.
        Index(
            "ux_resumes_single_master",
            "is_master",
            unique=True,
            sqlite_where=text("is_master = 1"),
        ),
    )


class Job(Base):
    """A job description.

    Only the stable columns are first-class; everything the pipeline attaches
    dynamically (``job_keywords``, ``job_keywords_hash``, ``preview_hash``,
    ``preview_hashes``, ``preview_prompt_id``, ``company``, ``role``) lives in
    ``metadata_json``. The facade flattens that map to top-level keys on read
    and merges non-core keys into it on update, reproducing TinyDB semantics.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    resume_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lifecycle_token: Mapped[str] = mapped_column(String, unique=True, nullable=False, default=lambda: str(uuid4()))


class Improvement(Base):
    """A tailoring result linking an original resume, a tailored resume, and a job."""

    __tablename__ = "improvements"

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    original_resume_id: Mapped[str] = mapped_column(String)
    tailored_resume_id: Mapped[str] = mapped_column(String, index=True)
    job_id: Mapped[str] = mapped_column(String)
    improvements: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class Application(Base):
    """A Kanban application-tracker card."""

    __tablename__ = "applications"
    __table_args__ = (
        # Concurrency-safe dedupe: a card is unique per (job, applied resume).
        # The app-level select-then-insert relies on this to collapse races.
        UniqueConstraint("job_id", "resume_id", name="uq_application_job_resume"),
    )

    application_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    # The applied/tailored resume shown in the modal and opened by "Edit".
    resume_id: Mapped[str] = mapped_column(String, index=True)
    # Optional base resume the tailored one descends from (powers "stack" grouping).
    master_resume_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="applied", index=True)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    status_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifecycle_token: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()), unique=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class CVTransformationPlanApproval(Base):
    """Auditable user decisions for one exact public transformation plan."""

    __tablename__ = "cv_transformation_plan_approvals"

    approval_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_version: Mapped[str] = mapped_column(String, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    decisions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    guardrails_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "resume_id",
            "plan_fingerprint",
            name="uq_transformation_plan_approval_scope",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'APPROVED', 'REQUIRES_REVIEW', 'SUPERSEDED')",
            name="ck_transformation_plan_approval_status",
        ),
    )


class CVTransformationGeneration(Base):
    """Technical draft generated from one exact approved transformation plan."""

    __tablename__ = "cv_transformation_generations"

    generation_id: Mapped[str] = mapped_column(String, primary_key=True)
    approval_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    resume_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    plan_version: Mapped[str] = mapped_column(String, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    generation_version: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    generation_input_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    draft_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provenance_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('GENERATING', 'GENERATED', 'FAILED', 'SUPERSEDED')",
            name="ck_transformation_generation_status",
        ),
        Index(
            "ix_generation_current_scope",
            "job_id",
            "resume_id",
            "plan_fingerprint",
            "updated_at",
        ),
    )


_UUID_CHECK = (
    "length({name}) = 36 "
    "AND substr({name}, 9, 1) = '-' AND substr({name}, 14, 1) = '-' "
    "AND substr({name}, 19, 1) = '-' AND substr({name}, 24, 1) = '-' "
    "AND length(replace({name}, '-', '')) = 32 "
    "AND replace({name}, '-', '') NOT GLOB '*[^0-9a-f]*' "
    "AND substr({name}, 15, 1) = '4' "
    "AND substr({name}, 20, 1) IN ('8','9','a','b')"
)
_SHA256_CHECK = (
    "length({name}) = 64 AND lower({name}) = {name} "
    "AND {name} NOT GLOB '*[^0-9a-f]*'"
)
_UTC_TIMESTAMP_CHECK = (
    "({name} IS NULL OR (length({name}) = 27 "
    "AND {name} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
    "[0-9][0-9][0-9][0-9][0-9][0-9]Z' "
    "AND CAST(substr({name}, 1, 4) AS INTEGER) BETWEEN 1 AND 9999 "
    "AND CAST(substr({name}, 6, 2) AS INTEGER) BETWEEN 1 AND 12 "
    "AND CAST(substr({name}, 9, 2) AS INTEGER) BETWEEN 1 AND "
    "CASE CAST(substr({name}, 6, 2) AS INTEGER) "
    "WHEN 2 THEN 28 + CASE "
    "WHEN (CAST(substr({name}, 1, 4) AS INTEGER) % 4 = 0 "
    "AND (CAST(substr({name}, 1, 4) AS INTEGER) % 100 != 0 "
    "OR CAST(substr({name}, 1, 4) AS INTEGER) % 400 = 0)) THEN 1 ELSE 0 END "
    "WHEN 4 THEN 30 WHEN 6 THEN 30 WHEN 9 THEN 30 WHEN 11 THEN 30 "
    "ELSE 31 END "
    "AND CAST(substr({name}, 12, 2) AS INTEGER) BETWEEN 0 AND 23 "
    "AND CAST(substr({name}, 15, 2) AS INTEGER) BETWEEN 0 AND 59 "
    "AND CAST(substr({name}, 18, 2) AS INTEGER) BETWEEN 0 AND 59 "
    "AND substr({name}, 1, 19) = strftime("
    "'%Y-%m-%dT%H:%M:%S', substr({name}, 1, 19) || 'Z')"
    "))"
)


class TruthEntity(Base):
    """Stable UUID identity for one durable Truth Library entity."""

    __tablename__ = "truth_entities"

    entity_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("truth_entities.entity_id", ondelete="RESTRICT"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(8), nullable=False, default="1.0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(_UUID_CHECK.format(name="entity_id"), name="ck_truth_entities_uuid"),
        CheckConstraint(
            "entity_type IN ('PERSON','EMPLOYMENT','PROJECT','EDUCATION',"
            "'CERTIFICATION','COURSE','LANGUAGE','SKILL','TOOL','TECHNOLOGY',"
            "'AWARD','CUSTOM_ENTITY')",
            name="ck_truth_entities_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','REVIEW_REQUIRED','REJECTED','ARCHIVED')",
            name="ck_truth_entities_status",
        ),
        CheckConstraint("schema_version = '1.0'", name="ck_truth_entities_schema"),
        CheckConstraint("revision >= 1", name="ck_truth_entities_revision"),
        CheckConstraint(
            "parent_entity_id IS NULL OR parent_entity_id != entity_id",
            name="ck_truth_entities_not_self_parent",
        ),
        CheckConstraint(
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL) OR "
            "(status != 'ARCHIVED' AND archived_at IS NULL)",
            name="ck_truth_entities_archive",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_fingerprint"),
            name="ck_truth_entities_fingerprint",
        ),
        Index("ix_truth_entities_type", "entity_type"),
        Index("ix_truth_entities_status", "status"),
        Index("ix_truth_entities_parent", "parent_entity_id"),
        Index("ix_truth_entities_fingerprint", "content_fingerprint"),
    )


class TruthFact(Base):
    """Atomic, versioned Truth fact attached to exactly one entity."""

    __tablename__ = "truth_facts"

    fact_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("truth_entities.entity_id", ondelete="RESTRICT"), nullable=False
    )
    fact_type: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    normalized_value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    use_in_cv: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    transferability: Mapped[str] = mapped_column(String(32), nullable=False)
    employment_scope_entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("truth_entities.entity_id", ondelete="RESTRICT"), nullable=True
    )
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(8), nullable=False, default="1.0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(_UUID_CHECK.format(name="fact_id"), name="ck_truth_facts_uuid"),
        CheckConstraint(
            "length(fact_type) BETWEEN 1 AND 128 AND fact_type = upper(fact_type) "
            "AND fact_type NOT GLOB '*[^A-Z0-9_]*' "
            "AND substr(fact_type, 1, 1) GLOB '[A-Z]'",
            name="ck_truth_facts_type",
        ),
        CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','REVIEW_REQUIRED','REJECTED','ARCHIVED')",
            name="ck_truth_facts_status",
        ),
        CheckConstraint(
            "transferability IN ('GLOBAL_TRANSFERABLE','ROLE_TRANSFERABLE',"
            "'INDUSTRY_TRANSFERABLE','EMPLOYMENT_SCOPED','ENTITY_SCOPED',"
            "'NON_TRANSFERABLE','EXACT_ONLY')",
            name="ck_truth_facts_transferability",
        ),
        CheckConstraint("json_valid(value_json)", name="ck_truth_facts_value_json"),
        CheckConstraint(
            "json_valid(normalized_value_json)",
            name="ck_truth_facts_normalized_value_json",
        ),
        CheckConstraint("schema_version = '1.0'", name="ck_truth_facts_schema"),
        CheckConstraint("revision >= 1", name="ck_truth_facts_revision"),
        CheckConstraint(
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL) OR "
            "(status != 'ARCHIVED' AND archived_at IS NULL)",
            name="ck_truth_facts_archive",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_fingerprint"),
            name="ck_truth_facts_fingerprint",
        ),
        Index("ix_truth_facts_entity", "entity_id"),
        Index("ix_truth_facts_entity_status", "entity_id", "status"),
        Index("ix_truth_facts_type", "fact_type"),
        Index("ix_truth_facts_employment_scope", "employment_scope_entity_id"),
        Index("ix_truth_facts_fingerprint", "content_fingerprint"),
    )


class TruthFactVariant(Base):
    __tablename__ = "truth_fact_variants"

    variant_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    fact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("truth_facts.fact_id", ondelete="RESTRICT"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(8), nullable=False, default="1.0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "fact_id", "normalized_text", "operation",
            name="uq_truth_fact_variant_semantics",
        ),
        CheckConstraint(_UUID_CHECK.format(name="variant_id"), name="ck_truth_variants_uuid"),
        CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','REJECTED','ARCHIVED')",
            name="ck_truth_variants_status",
        ),
        CheckConstraint(
            "operation IN ('EXACT_COPY','FORMAT_NORMALIZATION','CONTROLLED_REPHRASE')",
            name="ck_truth_variants_operation",
        ),
        CheckConstraint("schema_version = '1.0'", name="ck_truth_variants_schema"),
        CheckConstraint("revision >= 1", name="ck_truth_variants_revision"),
        CheckConstraint(
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL) OR "
            "(status != 'ARCHIVED' AND archived_at IS NULL)",
            name="ck_truth_variants_archive",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_fingerprint"),
            name="ck_truth_variants_fingerprint",
        ),
        Index("ix_truth_variants_fact", "fact_id"),
        Index("ix_truth_variants_fact_status", "fact_id", "status"),
        Index("ix_truth_variants_fingerprint", "content_fingerprint"),
    )


class TruthEvidence(Base):
    __tablename__ = "truth_evidence"

    evidence_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    fact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("truth_facts.fact_id", ondelete="RESTRICT"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("truth_entities.entity_id", ondelete="RESTRICT"), nullable=True
    )
    source_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="REVIEW_REQUIRED")
    captured_at: Mapped[str] = mapped_column(String, nullable=False)
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(500), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(8), nullable=False, default="1.0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(_UUID_CHECK.format(name="evidence_id"), name="ck_truth_evidence_uuid"),
        CheckConstraint(
            "evidence_type IN ('DOCUMENT','USER_CONFIRMATION','IMPORT','SYSTEM_RECORD')",
            name="ck_truth_evidence_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','REVIEW_REQUIRED','REJECTED','ARCHIVED')",
            name="ck_truth_evidence_status",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_hash"),
            name="ck_truth_evidence_content_hash",
        ),
        CheckConstraint("schema_version = '1.0'", name="ck_truth_evidence_schema"),
        CheckConstraint("revision >= 1", name="ck_truth_evidence_revision"),
        CheckConstraint(
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL) OR "
            "(status != 'ARCHIVED' AND archived_at IS NULL)",
            name="ck_truth_evidence_archive",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_fingerprint"),
            name="ck_truth_evidence_fingerprint",
        ),
        Index("ix_truth_evidence_fact", "fact_id"),
        Index("ix_truth_evidence_fact_status", "fact_id", "status"),
        Index("ix_truth_evidence_source_entity", "source_entity_id"),
        Index("ix_truth_evidence_fingerprint", "content_fingerprint"),
    )


class TruthPermission(Base):
    __tablename__ = "truth_permissions"

    permission_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    fact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("truth_facts.fact_id", ondelete="RESTRICT"), nullable=False
    )
    target_scope: Mapped[str] = mapped_column(String(512), nullable=False)
    allowed_operations: Mapped[list] = mapped_column(JSON, nullable=False)
    constraints_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="REVIEW_REQUIRED")
    approved_by: Mapped[str | None] = mapped_column(String(500), nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_from: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_until: Mapped[str | None] = mapped_column(String, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(8), nullable=False, default="1.0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(_UUID_CHECK.format(name="permission_id"), name="ck_truth_permissions_uuid"),
        CheckConstraint(
            "status IN ('ACTIVE','REVIEW_REQUIRED','REVOKED','ARCHIVED')",
            name="ck_truth_permissions_status",
        ),
        CheckConstraint(
            "json_valid(allowed_operations) AND json_type(allowed_operations) = 'array'",
            name="ck_truth_permissions_operations_json",
        ),
        CheckConstraint(
            "json_valid(constraints_json) AND json_type(constraints_json) = 'object'",
            name="ck_truth_permissions_constraints_json",
        ),
        CheckConstraint(
            _UTC_TIMESTAMP_CHECK.format(name="valid_from"),
            name="ck_truth_permissions_valid_from_utc",
        ),
        CheckConstraint(
            _UTC_TIMESTAMP_CHECK.format(name="valid_until"),
            name="ck_truth_permissions_valid_until_utc",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_until IS NULL OR valid_until > valid_from",
            name="ck_truth_permissions_valid_window",
        ),
        CheckConstraint("schema_version = '1.0'", name="ck_truth_permissions_schema"),
        CheckConstraint("revision >= 1", name="ck_truth_permissions_revision"),
        CheckConstraint(
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL) OR "
            "(status != 'ARCHIVED' AND archived_at IS NULL)",
            name="ck_truth_permissions_archive",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_fingerprint"),
            name="ck_truth_permissions_fingerprint",
        ),
        Index("ix_truth_permissions_fact", "fact_id"),
        Index("ix_truth_permissions_fact_status", "fact_id", "status"),
        Index("ix_truth_permissions_target", "target_scope"),
        Index("ix_truth_permissions_fingerprint", "content_fingerprint"),
        Index(
            "uq_truth_permissions_active_scope",
            "fact_id",
            "target_scope",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )


class TruthLegacyMigrationMap(Base):
    """Audit ledger mapping one legacy Truth Library record to P1 identity.

    Owned by Explicit Provenance P2 (see docs/explicit-provenance-stage-p2.md).
    One row per legacy record considered for migration, regardless of whether
    it produced a ``TruthEntity``/``TruthFact`` (``target_entity_id`` and
    ``target_fact_id`` are nullable — a REJECTED or UNSUPPORTED record still
    gets a ledger row with no target).
    """

    __tablename__ = "truth_legacy_migration_map"

    migration_map_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    person_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("truth_entities.entity_id", ondelete="RESTRICT"), nullable=False
    )
    legacy_source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Audit metadata only — deliberately excluded from identity/fingerprint so
    # environment (Mac/Docker/CI) never changes migrated record identity.
    legacy_source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    legacy_category: Mapped[str] = mapped_column(String(64), nullable=False)
    legacy_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    legacy_record_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("truth_entities.entity_id", ondelete="RESTRICT"), nullable=True
    )
    target_fact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("truth_facts.fact_id", ondelete="RESTRICT"), nullable=True
    )
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    migration_schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        UniqueConstraint(
            "person_entity_id",
            "legacy_source_id",
            "migration_schema_version",
            "legacy_category",
            "legacy_record_key",
            name="uq_truth_legacy_migration_map_identity",
        ),
        CheckConstraint(
            _UUID_CHECK.format(name="migration_map_id"),
            name="ck_truth_legacy_migration_map_uuid",
        ),
        CheckConstraint(
            _UUID_CHECK.format(name="person_entity_id"),
            name="ck_truth_legacy_migration_map_person_uuid",
        ),
        CheckConstraint(
            "(target_entity_id IS NULL) OR (" + _UUID_CHECK.format(name="target_entity_id") + ")",
            name="ck_truth_legacy_migration_map_target_entity_uuid",
        ),
        CheckConstraint(
            "(target_fact_id IS NULL) OR (" + _UUID_CHECK.format(name="target_fact_id") + ")",
            name="ck_truth_legacy_migration_map_target_fact_uuid",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="legacy_record_fingerprint"),
            name="ck_truth_legacy_migration_map_fingerprint",
        ),
        CheckConstraint(
            "legacy_category IN ("
            "'doswiadczenieZawodowe','osiagnieciaLiczbowe','skalaOdpowiedzialnosci',"
            "'kompetencje','narzedzia','technologie','certyfikaty','kursy',"
            "'wyksztalcenie','jezyki','branze','daneOsobowe')",
            name="ck_truth_legacy_migration_map_category",
        ),
        CheckConstraint(
            "classification IN ('MIGRATABLE','REQUIRES_REVIEW','REJECTED','UNSUPPORTED')",
            name="ck_truth_legacy_migration_map_classification",
        ),
        CheckConstraint(
            "migration_schema_version = '1.0'",
            name="ck_truth_legacy_migration_map_schema",
        ),
        Index("ix_truth_legacy_migration_map_person", "person_entity_id"),
        Index(
            "ix_truth_legacy_migration_map_person_source_category",
            "person_entity_id",
            "legacy_source_id",
            "legacy_category",
        ),
        Index(
            "ix_truth_legacy_migration_map_person_source_fingerprint",
            "person_entity_id",
            "legacy_source_id",
            "legacy_record_fingerprint",
        ),
        Index("ix_truth_legacy_migration_map_target_entity", "target_entity_id"),
        Index("ix_truth_legacy_migration_map_target_fact", "target_fact_id"),
    )


class ApiKey(Base):
    """An encrypted LLM provider API key.

    ``provider`` is the *key-store* provider name (e.g. ``google`` for the
    ``gemini`` LLM provider, via ``_PROVIDER_KEY_MAP``). Only ciphertext is
    stored; plaintext exists in memory only at call time.
    """

    __tablename__ = "api_keys"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class MetricEvent(Base):
    """Zdarzenie metryki rejestrujące historyczną akcję użytkownika (append-only)."""

    __tablename__ = "metric_events"

    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    operation_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    lifecycle_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)
    source: Mapped[str] = mapped_column(String, default="USER", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('BASELINE', 'ENTITY_CREATED', 'ENTITY_DELETED', 'RESUME_GENERATED', 'JOB_ANALYZED', 'APPLICATION_STATUS_CHANGED', 'TRANSFORMATION_PLAN_DECIDED')",
            name="ck_metric_events_event_type"
        ),
        CheckConstraint(
            "entity_type IN ('RESUME', 'JOB', 'APPLICATION')",
            name="ck_metric_events_entity_type"
        ),
        CheckConstraint(
            "source IN ('USER', 'SYSTEM', 'BOOTSTRAP')",
            name="ck_metric_events_source"
        ),
    )


class MetricTrackingState(Base):
    """Przechowuje metadane dotyczące stanu i historii działania modułu kolektora."""

    __tablename__ = "metric_tracking_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    tracking_started_at: Mapped[str] = mapped_column(String, default=_utcnow_iso, nullable=False)
    bootstrap_completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    historical_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_metric_tracking_state_singleton"),
    )


class MetricBootstrapState(Base):
    """Zapisuje stan bootstrapowania metryk v1."""

    __tablename__ = "metric_bootstrap_state"

    bootstrap_key: Mapped[str] = mapped_column(String, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_at: Mapped[str] = mapped_column(String, nullable=False)
    completed_at: Mapped[str] = mapped_column(String, nullable=False)
    history_completeness: Mapped[str] = mapped_column(String, nullable=False)
    baseline_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    database_was_empty: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "history_completeness IN ('COMPLETE', 'PARTIAL_BASELINE')",
            name="ck_metric_bootstrap_state_completeness"
        ),
    )
