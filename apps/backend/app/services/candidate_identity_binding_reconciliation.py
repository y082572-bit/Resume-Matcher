"""Explicit Provenance Stage P6-B2A-I1: read-only reconciliation for
``candidate_identity_bindings``.

``run_candidate_identity_binding_reconciliation`` is a report-first pass. It
never repairs, updates, deletes, or rebinds anything -- not a
``candidate_identity_bindings`` row (which is unconditionally immutable at
the SQLite layer anyway, see the four ``trg_candidate_identity_bindings_*``
triggers), not a ``TruthEntity``, not a ``Resume``, and it never copies
``PersonalInfo``. Calling it twice in a row on unchanged storage always
yields an identical report.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import CandidateIdentityBinding as CandidateIdentityBindingRow
from app.models import Resume, TruthEntity
from app.schemas.candidate_identity_binding import CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION
from app.schemas.models import PersonalInfo
from app.schemas.truth_entity import EntityStatus, EntityType
from app.services.candidate_identity_binding_repository import compute_binding_fingerprint


class CandidateIdentityBindingReconciliationIssueCode(StrEnum):
    MISSING_PERSON_ENTITY = "MISSING_PERSON_ENTITY"
    ENTITY_IS_NOT_PERSON = "ENTITY_IS_NOT_PERSON"
    # Informational only -- a non-ACTIVE PERSON is never a gate on binding
    # creation (see CandidateIdentityBindingSqlService.create_binding) and
    # is never itself treated as an error by this reconciliation pass.
    PERSON_ENTITY_NOT_ACTIVE = "PERSON_ENTITY_NOT_ACTIVE"
    MISSING_MASTER_RESUME = "MISSING_MASTER_RESUME"
    RESUME_IS_NOT_MASTER = "RESUME_IS_NOT_MASTER"
    PERSONAL_INFO_MISSING = "PERSONAL_INFO_MISSING"
    PERSONAL_INFO_INVALID = "PERSONAL_INFO_INVALID"
    BLANK_FULL_NAME = "BLANK_FULL_NAME"
    DUPLICATE_PERSON_BINDING = "DUPLICATE_PERSON_BINDING"
    DUPLICATE_MASTER_RESUME_BINDING = "DUPLICATE_MASTER_RESUME_BINDING"
    BINDING_FINGERPRINT_MISMATCH = "BINDING_FINGERPRINT_MISMATCH"
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"


class CandidateIdentityBindingReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_code: CandidateIdentityBindingReconciliationIssueCode
    detail: str = Field(min_length=1, max_length=2000)
    binding_fingerprint: str | None = None
    person_entity_id: str | None = None
    master_resume_id: str | None = None


class CandidateIdentityBindingReconciliationReport(BaseModel):
    """The single return value of
    ``run_candidate_identity_binding_reconciliation``. Standard mode is
    always read-only -- producing this report never changes a single row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: tuple[CandidateIdentityBindingReconciliationIssue, ...] = Field(default_factory=tuple)
    scanned_binding_count: int = Field(ge=0)


def run_candidate_identity_binding_reconciliation(
    session_factory: sessionmaker[Session],
) -> CandidateIdentityBindingReconciliationReport:
    issues: list[CandidateIdentityBindingReconciliationIssue] = []

    with session_factory() as session:
        rows = list(session.execute(select(CandidateIdentityBindingRow)).scalars())

        person_counts: dict[str, int] = {}
        resume_counts: dict[str, int] = {}
        for row in rows:
            person_counts[row.person_entity_id] = person_counts.get(row.person_entity_id, 0) + 1
            resume_counts[row.master_resume_id] = resume_counts.get(row.master_resume_id, 0) + 1

        for row in rows:
            entity = session.get(TruthEntity, row.person_entity_id)
            if entity is None:
                issues.append(
                    CandidateIdentityBindingReconciliationIssue(
                        issue_code=CandidateIdentityBindingReconciliationIssueCode.MISSING_PERSON_ENTITY,
                        detail="binding references a person_entity_id with no truth_entities row",
                        binding_fingerprint=row.binding_fingerprint,
                        person_entity_id=row.person_entity_id,
                        master_resume_id=row.master_resume_id,
                    )
                )
            else:
                if entity.entity_type != EntityType.PERSON.value:
                    issues.append(
                        CandidateIdentityBindingReconciliationIssue(
                            issue_code=CandidateIdentityBindingReconciliationIssueCode.ENTITY_IS_NOT_PERSON,
                            detail="binding's person_entity_id resolves to a non-PERSON TruthEntity",
                            binding_fingerprint=row.binding_fingerprint,
                            person_entity_id=row.person_entity_id,
                            master_resume_id=row.master_resume_id,
                        )
                    )
                if entity.status != EntityStatus.ACTIVE.value:
                    issues.append(
                        CandidateIdentityBindingReconciliationIssue(
                            issue_code=CandidateIdentityBindingReconciliationIssueCode.PERSON_ENTITY_NOT_ACTIVE,
                            detail=f"bound PERSON entity status is '{entity.status}', not ACTIVE (informational)",
                            binding_fingerprint=row.binding_fingerprint,
                            person_entity_id=row.person_entity_id,
                            master_resume_id=row.master_resume_id,
                        )
                    )

            resume = session.get(Resume, row.master_resume_id)
            if resume is None:
                issues.append(
                    CandidateIdentityBindingReconciliationIssue(
                        issue_code=CandidateIdentityBindingReconciliationIssueCode.MISSING_MASTER_RESUME,
                        detail="binding references a master_resume_id with no resumes row",
                        binding_fingerprint=row.binding_fingerprint,
                        person_entity_id=row.person_entity_id,
                        master_resume_id=row.master_resume_id,
                    )
                )
            else:
                if not resume.is_master:
                    issues.append(
                        CandidateIdentityBindingReconciliationIssue(
                            issue_code=CandidateIdentityBindingReconciliationIssueCode.RESUME_IS_NOT_MASTER,
                            detail="binding's master_resume_id no longer points at a master resume",
                            binding_fingerprint=row.binding_fingerprint,
                            person_entity_id=row.person_entity_id,
                            master_resume_id=row.master_resume_id,
                        )
                    )

                processed_data = resume.processed_data
                raw_personal_info = (
                    processed_data.get("personalInfo") if isinstance(processed_data, dict) else None
                )
                if not isinstance(raw_personal_info, dict):
                    issues.append(
                        CandidateIdentityBindingReconciliationIssue(
                            issue_code=CandidateIdentityBindingReconciliationIssueCode.PERSONAL_INFO_MISSING,
                            detail="bound master resume has no personalInfo in processed_data",
                            binding_fingerprint=row.binding_fingerprint,
                            person_entity_id=row.person_entity_id,
                            master_resume_id=row.master_resume_id,
                        )
                    )
                else:
                    try:
                        personal_info = PersonalInfo.model_validate(raw_personal_info)
                    except ValidationError:
                        issues.append(
                            CandidateIdentityBindingReconciliationIssue(
                                issue_code=CandidateIdentityBindingReconciliationIssueCode.PERSONAL_INFO_INVALID,
                                detail="bound master resume's personalInfo fails PersonalInfo validation",
                                binding_fingerprint=row.binding_fingerprint,
                                person_entity_id=row.person_entity_id,
                                master_resume_id=row.master_resume_id,
                            )
                        )
                    else:
                        if not personal_info.name.strip():
                            issues.append(
                                CandidateIdentityBindingReconciliationIssue(
                                    issue_code=CandidateIdentityBindingReconciliationIssueCode.BLANK_FULL_NAME,
                                    detail="bound master resume's personalInfo.name is blank",
                                    binding_fingerprint=row.binding_fingerprint,
                                    person_entity_id=row.person_entity_id,
                                    master_resume_id=row.master_resume_id,
                                )
                            )

            if person_counts[row.person_entity_id] > 1:
                issues.append(
                    CandidateIdentityBindingReconciliationIssue(
                        issue_code=CandidateIdentityBindingReconciliationIssueCode.DUPLICATE_PERSON_BINDING,
                        detail="more than one binding row shares this person_entity_id",
                        binding_fingerprint=row.binding_fingerprint,
                        person_entity_id=row.person_entity_id,
                        master_resume_id=row.master_resume_id,
                    )
                )
            if resume_counts[row.master_resume_id] > 1:
                issues.append(
                    CandidateIdentityBindingReconciliationIssue(
                        issue_code=CandidateIdentityBindingReconciliationIssueCode.DUPLICATE_MASTER_RESUME_BINDING,
                        detail="more than one binding row shares this master_resume_id",
                        binding_fingerprint=row.binding_fingerprint,
                        person_entity_id=row.person_entity_id,
                        master_resume_id=row.master_resume_id,
                    )
                )

            recomputed_fingerprint = compute_binding_fingerprint(
                person_entity_id=UUID(row.person_entity_id),
                master_resume_id=row.master_resume_id,
                binding_schema_version=row.binding_schema_version,
            )
            if recomputed_fingerprint != row.binding_fingerprint:
                issues.append(
                    CandidateIdentityBindingReconciliationIssue(
                        issue_code=CandidateIdentityBindingReconciliationIssueCode.BINDING_FINGERPRINT_MISMATCH,
                        detail="a recomputed binding_fingerprint does not match the stored row's own PK",
                        binding_fingerprint=row.binding_fingerprint,
                        person_entity_id=row.person_entity_id,
                        master_resume_id=row.master_resume_id,
                    )
                )

            if row.binding_schema_version != CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION:
                issues.append(
                    CandidateIdentityBindingReconciliationIssue(
                        issue_code=CandidateIdentityBindingReconciliationIssueCode.SCHEMA_VERSION_MISMATCH,
                        detail="binding_schema_version does not match the current schema version",
                        binding_fingerprint=row.binding_fingerprint,
                        person_entity_id=row.person_entity_id,
                        master_resume_id=row.master_resume_id,
                    )
                )

    return CandidateIdentityBindingReconciliationReport(
        issues=tuple(issues), scanned_binding_count=len(rows)
    )
