"""Explicit Provenance Stage P6-B2A-I1: closed, frozen Pydantic contracts
for the Candidate Identity Binding Foundation.

A ``CandidateIdentityBinding`` is the explicit, immutable, auditable link
between one ``PERSON`` ``TruthEntity`` and exactly one master ``Resume``.
It carries no personal data of its own -- only the two identities and a
content-derived ``binding_fingerprint``. ``created_at`` is pure audit
metadata: it never participates in the fingerprint and never affects
binding identity.

A ``CvDocxHeaderBinding`` is the separate, read-only projection built by
``cv_docx_header_binding_builder.py`` at header-resolution time: the exact,
canonicalized personal-info fields a DOCX header may render, resolved
through a ``CandidateIdentityBinding`` and re-verified against the live
``TruthEntity``/``Resume``/``PersonalInfo`` rows every time. It never
includes ``website``, ``github``, or ``title`` -- those are deliberately
out of scope for a document header and must never leak into a fingerprint
or a rendered field.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from app.schemas.truth_entity import SHA256_PATTERN


CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION = "candidate-identity-binding-schema-v1"
CV_DOCX_HEADER_BINDING_SCHEMA_VERSION = "cv-docx-header-binding-schema-v1"


# -- Candidate identity binding -----------------------------------------------


class CandidateIdentityBinding(BaseModel):
    """The immutable, auditable binding of one PERSON entity to one master
    resume. Never updated, superseded, or replaced -- see the four
    ``trg_candidate_identity_bindings_*`` triggers in ``app/db_engine.py``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    master_resume_id: str = Field(min_length=1)
    binding_schema_version: Literal["candidate-identity-binding-schema-v1"] = (
        CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION
    )
    # Audit metadata only -- never part of binding_fingerprint, never part
    # of identical-binding comparison.
    created_at: str = Field(min_length=1)


# -- CV DOCX header binding ----------------------------------------------------


class CvDocxHeaderBinding(BaseModel):
    """A resolved, closed projection of the fields a DOCX header may render.

    Deliberately has no ``website``, ``github``, or ``title`` field --
    those are never read, never copied, and never allowed to influence
    ``source_personal_info_fingerprint``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_key_fingerprint: str = Field(pattern=SHA256_PATTERN)
    person_entity_id: UUID4
    source_master_resume_id: str = Field(min_length=1)
    source_personal_info_fingerprint: str = Field(pattern=SHA256_PATTERN)
    full_name: str = Field(min_length=1)
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    header_schema_version: Literal["cv-docx-header-binding-schema-v1"] = (
        CV_DOCX_HEADER_BINDING_SCHEMA_VERSION
    )


# -- Binding creation result ----------------------------------------------------


class BindMasterResumeToPersonEntityStatus(StrEnum):
    """Closed set of outcomes ``CandidateIdentityBindingSqlService.create_binding``
    can return. Never a raw ``IntegrityError``/``SQLAlchemyError``."""

    CREATED = "CREATED"
    PERSON_ENTITY_NOT_FOUND = "PERSON_ENTITY_NOT_FOUND"
    ENTITY_IS_NOT_PERSON = "ENTITY_IS_NOT_PERSON"
    MASTER_RESUME_NOT_FOUND = "MASTER_RESUME_NOT_FOUND"
    RESUME_IS_NOT_MASTER = "RESUME_IS_NOT_MASTER"
    PERSONAL_INFO_MISSING = "PERSONAL_INFO_MISSING"
    PERSONAL_INFO_INVALID = "PERSONAL_INFO_INVALID"
    FULL_NAME_MISSING = "FULL_NAME_MISSING"
    BINDING_ALREADY_EXISTS_IDENTICAL = "BINDING_ALREADY_EXISTS_IDENTICAL"
    PERSON_ALREADY_BOUND = "PERSON_ALREADY_BOUND"
    MASTER_RESUME_ALREADY_BOUND = "MASTER_RESUME_ALREADY_BOUND"
    STORAGE_CONFLICT = "STORAGE_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class BindMasterResumeToPersonEntityResult(BaseModel):
    """The single return value of ``CandidateIdentityBindingSqlService.create_binding``.

    Only ``CREATED`` and ``BINDING_ALREADY_EXISTS_IDENTICAL`` ever carry a
    binding (the identical, already-converged one); every other status
    carries none.
    """

    model_config = ConfigDict(extra="forbid")

    status: BindMasterResumeToPersonEntityStatus
    binding: CandidateIdentityBinding | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "BindMasterResumeToPersonEntityResult":
        carries_binding = {
            BindMasterResumeToPersonEntityStatus.CREATED,
            BindMasterResumeToPersonEntityStatus.BINDING_ALREADY_EXISTS_IDENTICAL,
        }
        if self.status in carries_binding:
            if self.binding is None:
                raise ValueError(f"{self.status} requires a binding")
        elif self.binding is not None:
            raise ValueError(f"{self.status} must carry no binding")
        return self


# -- Header resolution result ---------------------------------------------------


class CvDocxHeaderBindingStatus(StrEnum):
    """Closed set of outcomes ``CandidateIdentityHeaderSqlService.resolve_header_binding``
    can return. Never a raw exception."""

    RESOLVED = "RESOLVED"
    OWNER_KEY_FINGERPRINT_MISMATCH = "OWNER_KEY_FINGERPRINT_MISMATCH"
    IDENTITY_BINDING_NOT_FOUND = "IDENTITY_BINDING_NOT_FOUND"
    BINDING_OWNER_MISMATCH = "BINDING_OWNER_MISMATCH"
    PERSON_ENTITY_NOT_FOUND = "PERSON_ENTITY_NOT_FOUND"
    ENTITY_IS_NOT_PERSON = "ENTITY_IS_NOT_PERSON"
    MASTER_RESUME_NOT_FOUND = "MASTER_RESUME_NOT_FOUND"
    RESUME_IS_NOT_MASTER = "RESUME_IS_NOT_MASTER"
    PERSONAL_INFO_MISSING = "PERSONAL_INFO_MISSING"
    FULL_NAME_MISSING = "FULL_NAME_MISSING"
    PERSONAL_INFO_INVALID = "PERSONAL_INFO_INVALID"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class CvDocxHeaderBindingResult(BaseModel):
    """The single return value of
    ``CandidateIdentityHeaderSqlService.resolve_header_binding``. Only
    ``RESOLVED`` ever carries a header; every other status carries none."""

    model_config = ConfigDict(extra="forbid")

    status: CvDocxHeaderBindingStatus
    header: CvDocxHeaderBinding | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "CvDocxHeaderBindingResult":
        if self.status == CvDocxHeaderBindingStatus.RESOLVED:
            if self.header is None:
                raise ValueError("RESOLVED requires a header")
        elif self.header is not None:
            raise ValueError(f"{self.status} must carry no header")
        return self
