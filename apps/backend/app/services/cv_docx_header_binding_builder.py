"""Explicit Provenance Stage P6-B2A-I1: atomic, fail-closed resolution of a
``CvDocxHeaderBinding`` from a ``JobArtifactOwnerKey``.

``CandidateIdentityHeaderSqlService.resolve_header_binding`` never trusts a
caller-supplied ``JobArtifactOwnerKey`` at face value: it independently
recomputes ``owner_key_fingerprint`` *before ever opening a Session* --
a ``model_copy(update=...)`` that tampers with ``person_entity_id``,
``owner_kind``, ``owner_reference_id``, or ``owner_key_schema_version``
without also forging a matching fingerprint is rejected on that recompute
alone. Once past that gate, every read (the binding row, the ``TruthEntity``,
the ``Resume``, its ``processed_data``) happens inside exactly one
``Session`` and one read transaction -- this module never uses
``candidate_identity_binding_repository``/``candidate_identity_binding_sql_repository``
as a composition mechanism, precisely so the whole read stays atomic.

``website``/``github``/``title`` are never read from ``PersonalInfo`` here --
``CvDocxHeaderBinding`` has no field for them, and
``source_personal_info_fingerprint`` never covers them.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models import CandidateIdentityBinding as CandidateIdentityBindingRow
from app.models import Resume, TruthEntity
from app.schemas.candidate_identity_binding import CvDocxHeaderBinding, CvDocxHeaderBindingResult, CvDocxHeaderBindingStatus
from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.models import PersonalInfo
from app.schemas.truth_entity import EntityType
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint
from app.services.truth_fingerprint import canonical_json_bytes


SOURCE_PERSONAL_INFO_FINGERPRINT_VERSION = "candidate-identity-personal-info-v1"
_LINKEDIN_MAX_LENGTH = 512
_WHITESPACE_RE = re.compile(r"\s")
_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def compute_source_personal_info_fingerprint(
    *,
    full_name: str,
    email: str | None,
    phone: str | None,
    location: str | None,
    linkedin: str | None,
) -> str:
    """SHA-256 over exactly the normalized full_name/email/phone/location/
    linkedin -- never website, github, title, or a Resume id/timestamp."""

    content = {
        "fingerprint_version": SOURCE_PERSONAL_INFO_FINGERPRINT_VERSION,
        "content": {
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "location": location,
            "linkedin": linkedin,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


class _PersonalInfoInvalid(Exception):
    """Internal signal: a field failed semantic canonicalization."""


def _canonicalize_required(value: str) -> str | None:
    trimmed = value.strip()
    return trimmed or None


def _canonicalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _canonicalize_linkedin(value: str | None) -> str | None:
    """Trim; reject internal whitespace/newlines; cap at 512 chars; prefix a
    bare host/path with ``https://``. Never touches ``website``, and never
    strips or rewrites a slug (e.g. ``marekgrabowski``) -- only ever
    prepends a scheme."""

    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if _WHITESPACE_RE.search(trimmed):
        raise _PersonalInfoInvalid("linkedin must not contain internal whitespace")
    candidate = trimmed if _SCHEME_RE.match(trimmed) else f"https://{trimmed}"
    if len(candidate) > _LINKEDIN_MAX_LENGTH:
        raise _PersonalInfoInvalid("linkedin exceeds maximum length")
    return candidate


def _binding_owner_matches(
    binding_row: CandidateIdentityBindingRow, owner_key: JobArtifactOwnerKey
) -> bool:
    return binding_row.person_entity_id == str(owner_key.person_entity_id)


def _header_status(status: CvDocxHeaderBindingStatus) -> CvDocxHeaderBindingResult:
    return CvDocxHeaderBindingResult(status=status)


class CandidateIdentityHeaderSqlService:
    """Synchronous SQL resolution of one ``CvDocxHeaderBinding`` from a
    ``JobArtifactOwnerKey``. Receives a ``sessionmaker``, never an
    already-open ``Session``."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def resolve_header_binding(
        self, *, owner_key: JobArtifactOwnerKey
    ) -> CvDocxHeaderBindingResult:
        recomputed_owner_key_fingerprint = compute_owner_key_fingerprint(
            person_entity_id=owner_key.person_entity_id,
            owner_kind=owner_key.owner_kind,
            owner_reference_id=owner_key.owner_reference_id,
            owner_key_schema_version=owner_key.owner_key_schema_version,
        )
        if recomputed_owner_key_fingerprint != owner_key.owner_key_fingerprint:
            # No Session is ever opened for a tampered/mismatched owner_key.
            return _header_status(CvDocxHeaderBindingStatus.OWNER_KEY_FINGERPRINT_MISMATCH)

        try:
            with self._session_factory() as session:
                with session.begin():
                    return self._resolve_transaction(session, owner_key)
        except OperationalError:
            return _header_status(CvDocxHeaderBindingStatus.STORAGE_UNAVAILABLE)

    def _resolve_transaction(
        self, session: Session, owner_key: JobArtifactOwnerKey
    ) -> CvDocxHeaderBindingResult:
        binding_row = session.execute(
            select(CandidateIdentityBindingRow).where(
                CandidateIdentityBindingRow.person_entity_id == str(owner_key.person_entity_id)
            )
        ).scalar_one_or_none()
        if binding_row is None:
            return _header_status(CvDocxHeaderBindingStatus.IDENTITY_BINDING_NOT_FOUND)

        if not _binding_owner_matches(binding_row, owner_key):
            return _header_status(CvDocxHeaderBindingStatus.BINDING_OWNER_MISMATCH)

        entity = session.get(TruthEntity, binding_row.person_entity_id)
        if entity is None:
            return _header_status(CvDocxHeaderBindingStatus.PERSON_ENTITY_NOT_FOUND)
        if entity.entity_type != EntityType.PERSON.value:
            return _header_status(CvDocxHeaderBindingStatus.ENTITY_IS_NOT_PERSON)

        resume = session.get(Resume, binding_row.master_resume_id)
        if resume is None:
            return _header_status(CvDocxHeaderBindingStatus.MASTER_RESUME_NOT_FOUND)
        if not resume.is_master:
            return _header_status(CvDocxHeaderBindingStatus.RESUME_IS_NOT_MASTER)

        processed_data = resume.processed_data
        raw_personal_info = (
            processed_data.get("personalInfo") if isinstance(processed_data, dict) else None
        )
        if not isinstance(raw_personal_info, dict):
            return _header_status(CvDocxHeaderBindingStatus.PERSONAL_INFO_MISSING)

        try:
            personal_info = PersonalInfo.model_validate(raw_personal_info)
        except ValidationError:
            return _header_status(CvDocxHeaderBindingStatus.PERSONAL_INFO_INVALID)

        full_name = _canonicalize_required(personal_info.name)
        if full_name is None:
            return _header_status(CvDocxHeaderBindingStatus.FULL_NAME_MISSING)

        try:
            email = _canonicalize_optional(personal_info.email)
            phone = _canonicalize_optional(personal_info.phone)
            location = _canonicalize_optional(personal_info.location)
            linkedin = _canonicalize_linkedin(personal_info.linkedin)
        except _PersonalInfoInvalid:
            return _header_status(CvDocxHeaderBindingStatus.PERSONAL_INFO_INVALID)

        source_personal_info_fingerprint = compute_source_personal_info_fingerprint(
            full_name=full_name, email=email, phone=phone, location=location, linkedin=linkedin
        )

        header = CvDocxHeaderBinding(
            owner_key_fingerprint=owner_key.owner_key_fingerprint,
            person_entity_id=owner_key.person_entity_id,
            source_master_resume_id=binding_row.master_resume_id,
            source_personal_info_fingerprint=source_personal_info_fingerprint,
            full_name=full_name,
            email=email,
            phone=phone,
            location=location,
            linkedin=linkedin,
        )
        return CvDocxHeaderBindingResult(status=CvDocxHeaderBindingStatus.RESOLVED, header=header)
