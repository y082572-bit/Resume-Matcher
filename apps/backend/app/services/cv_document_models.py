"""Explicit Provenance Stage P6-B1: SQLAlchemy ORM models for the document
storage foundation.

Seven tables back the P6-A domain chain (``JobArtifactOwnerKey`` ->
``CvDocxProposalArtifact`` -> ``ValidatedCvDocxSnapshot`` ->
``ConfirmedCvPdfArtifact``) plus a single, globally content-addressed blob
store and two independent current-slot tables. Every table uses the same
declarative ``Base`` as the rest of the app (``app.models.Base``) -- there is
no second SQLite database and no separate document store engine.

Identity is always a recomputed SHA-256 fingerprint, never a caller-supplied
boolean or a filesystem path:

- ``cv_document_blobs.blob_sha256`` is the identity of exact bytes, shared
  globally across every owner and every artifact kind (deduplication).
- ``cv_docx_proposal_artifacts.artifact_fingerprint``,
  ``cv_docx_validated_snapshots.snapshot_fingerprint``, and
  ``cv_confirmed_pdf_artifacts.artifact_fingerprint`` are each their own
  domain fingerprint, independent of the blob identity they point at.
- ``cv_document_proposal_slots``/``cv_document_pdf_slots`` are the only
  source of "current" status -- there is no ``is_current`` boolean column on
  any artifact table.

No row on any of these tables is ever treated as domain authority by
itself: ``cv_document_sql_repository.py`` always recomputes/re-verifies a
fingerprint or a bytes hash before trusting a stored row.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Duplicated (not imported) from ``app.models`` -- P6-B1 must not modify or
# import private helpers from that file, and these are small, stable, and
# self-contained.
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


def _nullable_sha256_check(name: str) -> str:
    return f"{name} IS NULL OR ({_SHA256_CHECK.format(name=name)})"


class CvDocumentArtifactOwner(Base):
    """One document artifact owner (one job or one application).

    ``owner_key_fingerprint`` is the sole identity -- constituent fields are
    never updated for an existing fingerprint (see
    ``cv_document_sql_repository.py``'s owner-upsert contract: identical
    constituents are idempotent, any mismatch is a fail-closed
    ``OWNER_IDENTITY_MISMATCH``).
    """

    __tablename__ = "cv_document_artifact_owners"

    owner_key_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    person_entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_reference_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_key_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="owner_key_fingerprint"),
            name="ck_cv_document_owners_fingerprint",
        ),
        CheckConstraint(
            _UUID_CHECK.format(name="person_entity_id"),
            name="ck_cv_document_owners_person_uuid",
        ),
        CheckConstraint(
            "owner_kind IN ('JOB', 'APPLICATION')",
            name="ck_cv_document_owners_kind",
        ),
        CheckConstraint(
            "length(owner_reference_id) >= 1",
            name="ck_cv_document_owners_reference_id",
        ),
        UniqueConstraint(
            "person_entity_id",
            "owner_kind",
            "owner_reference_id",
            "owner_key_schema_version",
            name="uq_cv_document_owners_identity",
        ),
    )


class CvDocumentBlob(Base):
    """One globally deduplicated, content-addressed blob.

    ``blob_sha256`` is the exact-bytes identity. ``storage_locator`` is
    diagnostic metadata derived from the hash -- it is never itself content
    identity and is never trusted as an authority ahead of a bytes re-hash.
    """

    __tablename__ = "cv_document_blobs"

    blob_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_locator: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    storage_status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)
    verified_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="blob_sha256"),
            name="ck_cv_document_blobs_sha256",
        ),
        CheckConstraint("byte_size >= 0", name="ck_cv_document_blobs_size"),
        CheckConstraint(
            "storage_status IN ('OK', 'MISSING', 'HASH_MISMATCH', 'QUARANTINED')",
            name="ck_cv_document_blobs_status",
        ),
    )


class CvDocxProposalArtifactRow(Base):
    """One immutable DOCX proposal artifact row.

    Never carries a caller-supplied "is current" flag -- current status is
    only ever expressed by ``CvDocumentProposalSlot``.
    """

    __tablename__ = "cv_docx_proposal_artifacts"

    artifact_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_cv_content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    role_strategy_context_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rendering_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_adapter_id: Mapped[str] = mapped_column(String(255), nullable=False)
    template_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_docx_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validated_file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    provenance_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_storage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    blob_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_blobs.blob_sha256", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="artifact_fingerprint"),
            name="ck_cv_docx_proposal_fingerprint",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="approved_cv_content_fingerprint"),
            name="ck_cv_docx_proposal_approved_content_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_plan_fingerprint"),
            name="ck_cv_docx_proposal_content_plan_fp",
        ),
        CheckConstraint(
            _nullable_sha256_check("role_strategy_context_fingerprint"),
            name="ck_cv_docx_proposal_role_strategy_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="template_fingerprint"),
            name="ck_cv_docx_proposal_template_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="generation_input_fingerprint"),
            name="ck_cv_docx_proposal_generation_input_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="generated_docx_content_hash"),
            name="ck_cv_docx_proposal_generated_hash",
        ),
        CheckConstraint(
            _nullable_sha256_check("current_file_hash"),
            name="ck_cv_docx_proposal_current_file_hash",
        ),
        CheckConstraint(
            _nullable_sha256_check("validated_file_hash"),
            name="ck_cv_docx_proposal_validated_file_hash",
        ),
        CheckConstraint("proposal_revision >= 1", name="ck_cv_docx_proposal_revision"),
        CheckConstraint(
            "provenance_mode IN ('GENERATED_BY_RENDERER')",
            name="ck_cv_docx_proposal_provenance_mode",
        ),
        CheckConstraint(
            "artifact_storage_status IN ('ACTIVE', 'SUPERSEDED', 'STORAGE_UNAVAILABLE')",
            name="ck_cv_docx_proposal_storage_status",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="blob_sha256"),
            name="ck_cv_docx_proposal_blob_sha256",
        ),
        CheckConstraint(
            "generated_docx_content_hash = blob_sha256",
            name="ck_cv_docx_proposal_hash_eq_blob",
        ),
        UniqueConstraint(
            "owner_key_fingerprint", "proposal_revision", name="uq_cv_docx_proposal_owner_revision"
        ),
        Index("ix_cv_docx_proposal_owner", "owner_key_fingerprint"),
        Index("ix_cv_docx_proposal_blob", "blob_sha256"),
    )


class CvDocxValidatedSnapshotRow(Base):
    """One immutable, content-addressed validated DOCX snapshot row.

    Keyed by ``snapshot_fingerprint``, never by ``blob_sha256`` -- the same
    ``blob_sha256`` may legitimately be pointed at by many snapshot rows
    (e.g. two independent manual confirmations of byte-identical content).
    """

    __tablename__ = "cv_docx_validated_snapshots"

    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    proposal_artifact_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_docx_proposal_artifacts.artifact_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_blobs.blob_sha256", ondelete="RESTRICT"),
        nullable=False,
    )
    exact_docx_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_cv_content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    template_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manual_confirmation_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_mode: Mapped[str] = mapped_column(String(48), nullable=False)
    artifact_storage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="snapshot_fingerprint"),
            name="ck_cv_docx_snapshot_fingerprint",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="exact_docx_sha256"),
            name="ck_cv_docx_snapshot_exact_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="approved_cv_content_fingerprint"),
            name="ck_cv_docx_snapshot_approved_content_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_plan_fingerprint"),
            name="ck_cv_docx_snapshot_content_plan_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="template_fingerprint"),
            name="ck_cv_docx_snapshot_template_fp",
        ),
        CheckConstraint(
            _nullable_sha256_check("manual_confirmation_fingerprint"),
            name="ck_cv_docx_snapshot_manual_confirmation_fp",
        ),
        CheckConstraint("proposal_revision >= 1", name="ck_cv_docx_snapshot_proposal_revision"),
        CheckConstraint(
            "provenance_mode IN ('PIPELINE_UNMODIFIED_DOCUMENT', 'USER_CONFIRMED_MANUAL_DOCUMENT')",
            name="ck_cv_docx_snapshot_provenance_mode",
        ),
        CheckConstraint(
            "artifact_storage_status IN ('ACTIVE', 'SUPERSEDED', 'STORAGE_UNAVAILABLE')",
            name="ck_cv_docx_snapshot_storage_status",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="blob_sha256"),
            name="ck_cv_docx_snapshot_blob_sha256",
        ),
        CheckConstraint(
            "exact_docx_sha256 = blob_sha256",
            name="ck_cv_docx_snapshot_hash_eq_blob",
        ),
        Index("ix_cv_docx_snapshot_owner", "owner_key_fingerprint"),
        Index("ix_cv_docx_snapshot_proposal", "proposal_artifact_fingerprint"),
        Index("ix_cv_docx_snapshot_blob", "blob_sha256"),
    )


class CvConfirmedPdfArtifactRow(Base):
    """One immutable confirmed-PDF artifact row.

    Never carries a caller-supplied "is current" flag -- current status is
    only ever expressed by ``CvDocumentPdfSlot``.
    """

    __tablename__ = "cv_confirmed_pdf_artifacts"

    artifact_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_validated_docx_snapshot_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_docx_validated_snapshots.snapshot_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    source_docx_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_cv_content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    conversion_adapter_id: Mapped[str] = mapped_column(String(255), nullable=False)
    conversion_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_mode: Mapped[str] = mapped_column(String(48), nullable=False)
    pdf_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_blobs.blob_sha256", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_storage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="artifact_fingerprint"),
            name="ck_cv_confirmed_pdf_fingerprint",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="source_docx_sha256"),
            name="ck_cv_confirmed_pdf_source_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="approved_cv_content_fingerprint"),
            name="ck_cv_confirmed_pdf_approved_content_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="content_plan_fingerprint"),
            name="ck_cv_confirmed_pdf_content_plan_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="pdf_sha256"),
            name="ck_cv_confirmed_pdf_sha256",
        ),
        CheckConstraint("pdf_revision >= 1", name="ck_cv_confirmed_pdf_revision"),
        CheckConstraint(
            "provenance_mode IN ('PIPELINE_UNMODIFIED_DOCUMENT', 'USER_CONFIRMED_MANUAL_DOCUMENT')",
            name="ck_cv_confirmed_pdf_provenance_mode",
        ),
        CheckConstraint(
            "artifact_storage_status IN ('ACTIVE', 'SUPERSEDED', 'STORAGE_UNAVAILABLE')",
            name="ck_cv_confirmed_pdf_storage_status",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="blob_sha256"),
            name="ck_cv_confirmed_pdf_blob_sha256",
        ),
        CheckConstraint(
            "pdf_sha256 = blob_sha256",
            name="ck_cv_confirmed_pdf_hash_eq_blob",
        ),
        UniqueConstraint(
            "owner_key_fingerprint", "pdf_revision", name="uq_cv_confirmed_pdf_owner_revision"
        ),
        Index("ix_cv_confirmed_pdf_owner", "owner_key_fingerprint"),
        Index("ix_cv_confirmed_pdf_snapshot", "source_validated_docx_snapshot_fingerprint"),
        Index("ix_cv_confirmed_pdf_blob", "blob_sha256"),
    )


class CvDocumentProposalSlot(Base):
    """The single current-proposal pointer for one owner.

    Current status is exclusively derived from this row -- never from a
    boolean on ``cv_docx_proposal_artifacts``. Physically independent from
    ``CvDocumentPdfSlot``: replacing one never reads or writes the other.
    """

    __tablename__ = "cv_document_proposal_slots"

    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        primary_key=True,
    )
    current_artifact_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("cv_docx_proposal_artifacts.artifact_fingerprint", ondelete="RESTRICT"),
        nullable=True,
    )
    current_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slot_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            "(current_artifact_fingerprint IS NULL AND current_revision IS NULL) OR "
            "(current_artifact_fingerprint IS NOT NULL AND current_revision IS NOT NULL)",
            name="ck_cv_document_proposal_slot_pair",
        ),
        CheckConstraint(
            "current_revision IS NULL OR current_revision >= 1",
            name="ck_cv_document_proposal_slot_revision",
        ),
        CheckConstraint("slot_version >= 0", name="ck_cv_document_proposal_slot_version"),
        Index("ix_cv_document_proposal_slot_artifact", "current_artifact_fingerprint"),
    )


class CvDocumentPdfSlot(Base):
    """The single current-PDF pointer for one owner.

    Current status is exclusively derived from this row -- never from a
    boolean on ``cv_confirmed_pdf_artifacts``. Physically independent from
    ``CvDocumentProposalSlot``.
    """

    __tablename__ = "cv_document_pdf_slots"

    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        primary_key=True,
    )
    current_artifact_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("cv_confirmed_pdf_artifacts.artifact_fingerprint", ondelete="RESTRICT"),
        nullable=True,
    )
    current_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slot_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            "(current_artifact_fingerprint IS NULL AND current_revision IS NULL) OR "
            "(current_artifact_fingerprint IS NOT NULL AND current_revision IS NOT NULL)",
            name="ck_cv_document_pdf_slot_pair",
        ),
        CheckConstraint(
            "current_revision IS NULL OR current_revision >= 1",
            name="ck_cv_document_pdf_slot_revision",
        ),
        CheckConstraint("slot_version >= 0", name="ck_cv_document_pdf_slot_version"),
        Index("ix_cv_document_pdf_slot_artifact", "current_artifact_fingerprint"),
    )


P6B1_TABLE_NAMES: frozenset[str] = frozenset(
    {
        CvDocumentArtifactOwner.__tablename__,
        CvDocumentBlob.__tablename__,
        CvDocxProposalArtifactRow.__tablename__,
        CvDocxValidatedSnapshotRow.__tablename__,
        CvConfirmedPdfArtifactRow.__tablename__,
        CvDocumentProposalSlot.__tablename__,
        CvDocumentPdfSlot.__tablename__,
    }
)


class CvDocxFinalSnapshotRow(Base):
    """One immutable Final DOCX snapshot metadata row (Explicit Provenance
    P6-B1 SQL storage addendum for ``FinalDocxSnapshotRepository``).

    Deliberately a wholly separate table from ``cv_docx_validated_snapshots``
    -- it backs ``FinalDocxSnapshotRepository``, an independent, additive
    Protocol that never reuses or extends ``CvDocumentArtifactRepository``
    (see ``cv_document_final_snapshot_repository.py``). ``final_docx_sha256``
    always FK's into the shared ``cv_document_blobs`` store, but deliberately
    carries no ``UNIQUE`` constraint of its own and no separate
    ``blob_sha256`` column: many snapshot metadata rows (distinct owner/
    proposal lineage) may legitimately point at byte-identical final DOCX
    content, and identity for FK purposes is always ``final_docx_sha256``
    itself.
    """

    __tablename__ = "cv_docx_final_snapshots"

    final_snapshot_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    source_proposal_artifact_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_docx_proposal_artifacts.artifact_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    source_proposal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_proposal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    final_docx_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_blobs.blob_sha256", ondelete="RESTRICT"),
        nullable=False,
    )
    edited_by_user: Mapped[int] = mapped_column(Integer, nullable=False)
    finalization_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="final_snapshot_fingerprint"),
            name="ck_cv_docx_final_snapshot_fingerprint",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="generated_proposal_sha256"),
            name="ck_cv_docx_final_snapshot_generated_proposal_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="final_docx_sha256"),
            name="ck_cv_docx_final_snapshot_final_docx_sha256",
        ),
        CheckConstraint(
            "source_proposal_revision >= 1", name="ck_cv_docx_final_snapshot_revision"
        ),
        CheckConstraint(
            "snapshot_schema_version = 'cv-document-final-snapshot-schema-v1'",
            name="ck_cv_docx_final_snapshot_schema_version",
        ),
        CheckConstraint(
            "trim(finalization_policy_version) != ''",
            name="ck_cv_docx_final_snapshot_policy_version_not_blank",
        ),
        CheckConstraint(
            "length(finalization_policy_version) <= 64",
            name="ck_cv_docx_final_snapshot_policy_version_length",
        ),
        CheckConstraint(
            "edited_by_user IN (0, 1)", name="ck_cv_docx_final_snapshot_edited_by_user_bool"
        ),
        CheckConstraint(
            "(edited_by_user = 1 AND generated_proposal_sha256 != final_docx_sha256) OR "
            "(edited_by_user = 0 AND generated_proposal_sha256 = final_docx_sha256)",
            name="ck_cv_docx_final_snapshot_edited_by_user_consistency",
        ),
        Index("ix_cv_docx_final_snapshot_owner", "owner_key_fingerprint"),
        Index("ix_cv_docx_final_snapshot_source_proposal", "source_proposal_artifact_fingerprint"),
        Index("ix_cv_docx_final_snapshot_blob", "final_docx_sha256"),
    )


FINAL_DOCX_SNAPSHOT_TABLE_NAMES: frozenset[str] = frozenset({CvDocxFinalSnapshotRow.__tablename__})


# ---------------------------------------------------------------------------
# Explicit Provenance Stage P6-B3b-A -- final confirmed PDF storage and
# cross-line current authority.
#
# Two wholly new, additive tables. ``cv_final_confirmed_pdf_artifacts`` is a
# strictly separate, immutable artifact line from the legacy
# ``cv_confirmed_pdf_artifacts`` -- it never reuses that table, never carries
# ``approved_cv_content_fingerprint``/``content_plan_fingerprint``/the legacy
# ``provenance_mode``, and never has a mutable lifecycle column of its own.
# ``cv_confirmed_pdf_current_authority`` is the single product-facing
# current-PDF authority for *both* lineages (``LEGACY_VALIDATED_DOCX`` and
# ``FINAL_DOCX_SNAPSHOT``); the legacy ``cv_document_pdf_slots`` table is
# frozen (INSERT/UPDATE/DELETE all blocked by trigger -- installed by
# ``cv_document_final_confirmed_pdf_sql_repository.py``'s own cutover
# installer, never here) the moment this cutover completes. See
# ``cv_document_final_confirmed_pdf_cutover_state.py`` for the full
# migration/trigger contract.
# ---------------------------------------------------------------------------


class CvFinalConfirmedPdfArtifactRow(Base):
    """One immutable ``FinalConfirmedPdfArtifact`` row. Never carries an
    ``is_current``/``superseded``/``deleted``/status column -- current
    status for this line is exclusively expressed by
    ``cv_confirmed_pdf_current_authority``."""

    __tablename__ = "cv_final_confirmed_pdf_artifacts"

    artifact_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    source_final_snapshot_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_docx_final_snapshots.final_snapshot_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    source_final_docx_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    conversion_request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_identity_json: Mapped[str] = mapped_column(Text, nullable=False)
    conversion_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    blob_sha256: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_blobs.blob_sha256", ondelete="RESTRICT"),
        nullable=False,
    )
    conversion_request_fingerprint_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    artifact_fingerprint_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="artifact_fingerprint"),
            name="ck_cv_final_confirmed_pdf_artifact_fingerprint",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="source_final_snapshot_fingerprint"),
            name="ck_cv_final_confirmed_pdf_source_snapshot_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="source_final_docx_sha256"),
            name="ck_cv_final_confirmed_pdf_source_docx_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="conversion_request_fingerprint"),
            name="ck_cv_final_confirmed_pdf_request_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="runtime_identity_fingerprint"),
            name="ck_cv_final_confirmed_pdf_runtime_identity_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="pdf_sha256"),
            name="ck_cv_final_confirmed_pdf_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="blob_sha256"),
            name="ck_cv_final_confirmed_pdf_blob_sha256",
        ),
        CheckConstraint(
            "pdf_sha256 = blob_sha256",
            name="ck_cv_final_confirmed_pdf_hash_eq_blob",
        ),
        CheckConstraint(
            "trim(conversion_policy_version) != ''",
            name="ck_cv_final_confirmed_pdf_policy_version_not_blank",
        ),
        CheckConstraint(
            "length(conversion_policy_version) <= 64",
            name="ck_cv_final_confirmed_pdf_policy_version_length",
        ),
        CheckConstraint(
            "conversion_request_fingerprint_schema_version = "
            "'cv-final-confirmed-pdf-request-fingerprint-schema-v1'",
            name="ck_cv_final_confirmed_pdf_request_schema_version",
        ),
        CheckConstraint(
            "artifact_fingerprint_schema_version = "
            "'cv-final-confirmed-pdf-artifact-fingerprint-schema-v1'",
            name="ck_cv_final_confirmed_pdf_artifact_schema_version",
        ),
        CheckConstraint(
            "trim(runtime_identity_json) != ''",
            name="ck_cv_final_confirmed_pdf_runtime_identity_json_not_blank",
        ),
        CheckConstraint(
            "length(CAST(runtime_identity_json AS BLOB)) <= 8192",
            name="ck_cv_final_confirmed_pdf_runtime_identity_json_size",
        ),
        UniqueConstraint(
            "conversion_request_fingerprint", name="uq_cv_final_confirmed_pdf_request"
        ),
        Index("ix_cv_final_confirmed_pdf_owner", "owner_key_fingerprint"),
        Index("ix_cv_final_confirmed_pdf_source_snapshot", "source_final_snapshot_fingerprint"),
        Index("ix_cv_final_confirmed_pdf_blob", "blob_sha256"),
    )


class CvConfirmedPdfCurrentAuthorityRow(Base):
    """The single product-facing current-PDF authority row per owner,
    spanning both the legacy and the Final-DOCX-Snapshot lineages. Never
    carries a polymorphic FK to ``current_artifact_fingerprint`` -- lineage
    integrity is instead enforced entirely by trigger (see
    ``cv_document_final_confirmed_pdf_sql_repository.py``)."""

    __tablename__ = "cv_confirmed_pdf_current_authority"

    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        primary_key=True,
    )
    lineage_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    current_artifact_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            "lineage_kind IN ('LEGACY_VALIDATED_DOCX', 'FINAL_DOCX_SNAPSHOT')",
            name="ck_cv_confirmed_pdf_authority_lineage_kind",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="current_artifact_fingerprint"),
            name="ck_cv_confirmed_pdf_authority_artifact_fp",
        ),
        CheckConstraint(
            "slot_version >= 1",
            name="ck_cv_confirmed_pdf_authority_slot_version",
        ),
        Index("ix_cv_confirmed_pdf_authority_artifact", "current_artifact_fingerprint"),
    )


FINAL_CONFIRMED_PDF_TABLE_NAMES: frozenset[str] = frozenset(
    {
        CvFinalConfirmedPdfArtifactRow.__tablename__,
        CvConfirmedPdfCurrentAuthorityRow.__tablename__,
    }
)


# ---------------------------------------------------------------------------
# Explicit Provenance Stage P6-C1b-A -- Approved Content Package foundation.
#
# Two wholly new, additive tables. ``cv_approved_content_packages`` is an
# immutable, content-addressed snapshot of one already-validated
# ``ApprovedContentDocumentInput`` (never itself embedding that envelope --
# see ``cv_approved_content_package.ApprovedContentPackagePayload``).
# ``cv_approved_content_current_authority`` is the single current-package
# pointer per owner, independent of every other current-slot table in this
# module (``cv_document_proposal_slots``/``cv_document_pdf_slots``/
# ``cv_confirmed_pdf_current_authority``). Both tables' full DDL (including
# every CHECK/FK/UNIQUE/index and all six triggers) is independently
# hand-written in ``cv_approved_content_package_schema_manifest.py`` -- these
# ORM classes exist only so
# ``cv_approved_content_package_cutover_state.py``'s installer can issue
# ``Table.create(engine, checkfirst=True)``; they are never trusted as the
# schema's source of truth.
# ---------------------------------------------------------------------------


class CvApprovedContentPackageRow(Base):
    """One immutable ``ApprovedContentPackage`` row. Never carries an
    ``is_current``/``superseded``/``deleted``/status column -- current
    status is exclusively expressed by
    ``cv_approved_content_current_authority``."""

    __tablename__ = "cv_approved_content_packages"

    package_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    document_input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_decisions_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_selection_identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    package_fingerprint_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        UniqueConstraint(
            "owner_key_fingerprint",
            "package_fingerprint",
            name="uq_cv_approved_content_packages_owner_package",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="package_fingerprint"),
            name="ck_cv_approved_content_packages_fingerprint",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="owner_key_fingerprint"),
            name="ck_cv_approved_content_packages_owner_fingerprint",
        ),
        CheckConstraint(
            "owner_kind IN ('JOB', 'APPLICATION')",
            name="ck_cv_approved_content_packages_owner_kind",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="document_input_fingerprint"),
            name="ck_cv_approved_content_packages_document_input_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="approval_decisions_fingerprint"),
            name="ck_cv_approved_content_packages_approval_decisions_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="fact_selection_identity_fingerprint"),
            name="ck_cv_approved_content_packages_fact_selection_identity_fp",
        ),
        CheckConstraint(
            _SHA256_CHECK.format(name="payload_sha256"),
            name="ck_cv_approved_content_packages_payload_sha256",
        ),
        CheckConstraint(
            "payload_byte_size > 0 AND payload_byte_size <= 1048576",
            name="ck_cv_approved_content_packages_payload_size_bounds",
        ),
        CheckConstraint(
            "payload_byte_size = length(CAST(payload_json AS BLOB))",
            name="ck_cv_approved_content_packages_payload_size_eq_bytes",
        ),
        CheckConstraint(
            "package_fingerprint_schema_version = 'cv-approved-content-package-fingerprint-v1'",
            name="ck_cv_approved_content_packages_fingerprint_version",
        ),
        CheckConstraint(
            "package_schema_version = 'cv-approved-content-package-schema-v1'",
            name="ck_cv_approved_content_packages_schema_version",
        ),
        CheckConstraint(
            "package_policy_version = 'cv-approved-content-package-policy-v1'",
            name="ck_cv_approved_content_packages_policy_version",
        ),
        Index("ix_cv_approved_content_packages_owner", "owner_key_fingerprint"),
    )


class CvApprovedContentCurrentAuthorityRow(Base):
    """The single current-Approved-Content-Package pointer row per owner.
    Enforces exact-pair (owner, package) integrity by both a composite FK
    into ``cv_approved_content_packages`` and mirrored triggers (see
    ``cv_approved_content_package_schema_manifest.py``)."""

    __tablename__ = "cv_approved_content_current_authority"

    owner_key_fingerprint: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cv_document_artifact_owners.owner_key_fingerprint", ondelete="RESTRICT"),
        primary_key=True,
    )
    current_package_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_utcnow_iso)

    __table_args__ = (
        CheckConstraint(
            _SHA256_CHECK.format(name="current_package_fingerprint"),
            name="ck_cv_approved_content_authority_package_fp",
        ),
        CheckConstraint(
            "slot_version >= 1",
            name="ck_cv_approved_content_authority_slot_version",
        ),
        ForeignKeyConstraint(
            ["owner_key_fingerprint", "current_package_fingerprint"],
            [
                "cv_approved_content_packages.owner_key_fingerprint",
                "cv_approved_content_packages.package_fingerprint",
            ],
            ondelete="RESTRICT",
            name="fk_cv_approved_content_authority_owner_package",
        ),
        Index("ix_cv_approved_content_authority_package", "current_package_fingerprint"),
    )


APPROVED_CONTENT_PACKAGE_TABLE_NAMES: frozenset[str] = frozenset(
    {
        CvApprovedContentPackageRow.__tablename__,
        CvApprovedContentCurrentAuthorityRow.__tablename__,
    }
)
