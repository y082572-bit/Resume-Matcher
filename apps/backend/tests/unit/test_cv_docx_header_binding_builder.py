"""Unit tests for ``CandidateIdentityHeaderSqlService.resolve_header_binding``:
owner-key revalidation before any Session opens, the atomic single-Session
read, field resolution/canonicalization, website omission, fingerprint
sensitivity, and fail-closed behavior.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.db_engine import init_models_sync
from app.models import CandidateIdentityBinding as CandidateIdentityBindingRow
from app.models import Resume, TruthEntity
from app.schemas.candidate_identity_binding import CvDocxHeaderBinding, CvDocxHeaderBindingStatus
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.services.candidate_identity_binding_sql_repository import CandidateIdentityBindingSqlService
from app.services.cv_docx_header_binding_builder import (
    CandidateIdentityHeaderSqlService,
    _binding_owner_matches,
    compute_source_personal_info_fingerprint,
)
from app.services.cv_document_owner_identity import build_owner_key


def _fake_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_person_entity(session_factory) -> UUID:
    entity_id = str(uuid4())
    with session_factory() as session:
        with session.begin():
            session.add(
                TruthEntity(
                    entity_id=entity_id,
                    entity_type="PERSON",
                    display_name="Test Person",
                    status="ACTIVE",
                    content_fingerprint=_fake_sha256(entity_id),
                )
            )
    return UUID(entity_id)


def _make_resume(session_factory, *, is_master: bool = True, personal_info: dict | None) -> str:
    resume_id = str(uuid4())
    processed_data = {"personalInfo": personal_info} if personal_info is not None else None
    with session_factory() as session:
        with session.begin():
            session.add(
                Resume(
                    resume_id=resume_id,
                    content="# Resume",
                    is_master=is_master,
                    processed_data=processed_data,
                )
            )
    return resume_id


def _set_is_master(session_factory, resume_id: str, is_master: bool) -> None:
    with session_factory() as session:
        with session.begin():
            resume = session.get(Resume, resume_id)
            resume.is_master = is_master


_FULL_PERSONAL_INFO = {
    "name": "  Marek Grabowski  ",
    "email": "  marek@example.com ",
    "phone": " +48 600 000 000 ",
    "location": " Warsaw, Poland ",
    "linkedin": "linkedin.com/in/marekgrabowski",
    "website": "https://marekgrabowski.dev",
    "github": "https://github.com/marekgrabowski",
}


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'header_binding.db'}", future=True)
    init_models_sync(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    binding_service = CandidateIdentityBindingSqlService(session_factory)
    header_service = CandidateIdentityHeaderSqlService(session_factory)
    return header_service, binding_service, session_factory, engine


def _bind(session_factory, binding_service, *, personal_info=_FULL_PERSONAL_INFO, is_master=True):
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory, is_master=is_master, personal_info=personal_info)
    result = binding_service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status.value == "CREATED"
    return person_id, resume_id


def _owner_key(person_id: UUID, reference_id: str = "job-1"):
    return build_owner_key(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )


# 18: valid resolution ------------------------------------------------------------------


def test_valid_owner_key_resolves_header(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, resume_id = _bind(session_factory, binding_service)

    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))
    assert result.status == CvDocxHeaderBindingStatus.RESOLVED
    assert result.header is not None
    assert result.header.person_entity_id == person_id
    assert result.header.source_master_resume_id == resume_id


# 19: owner fingerprint mismatch before Session open --------------------------------------


def test_owner_fingerprint_mismatch_never_opens_session(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)
    tampered = _owner_key(person_id).model_copy(update={"owner_reference_id": "tampered-ref"})

    call_count = {"n": 0}
    real_factory = header_service._session_factory

    def counting_factory():
        call_count["n"] += 1
        return real_factory()

    header_service._session_factory = counting_factory
    result = header_service.resolve_header_binding(owner_key=tampered)

    assert result.status == CvDocxHeaderBindingStatus.OWNER_KEY_FINGERPRINT_MISMATCH
    assert result.header is None
    assert call_count["n"] == 0


# 20-21: model_copy tamper attacks rejected ------------------------------------------------


def test_model_copy_owner_reference_attack_rejected(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)
    tampered = _owner_key(person_id).model_copy(update={"owner_reference_id": "different-job"})

    result = header_service.resolve_header_binding(owner_key=tampered)
    assert result.status == CvDocxHeaderBindingStatus.OWNER_KEY_FINGERPRINT_MISMATCH


def test_model_copy_person_id_attack_rejected(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, resume_id = _bind(session_factory, binding_service)
    _set_is_master(session_factory, resume_id, False)
    other_person_id, _ = _bind(session_factory, binding_service, personal_info={"name": "Other Person"})

    tampered = _owner_key(person_id).model_copy(update={"person_entity_id": other_person_id})
    result = header_service.resolve_header_binding(owner_key=tampered)
    assert result.status == CvDocxHeaderBindingStatus.OWNER_KEY_FINGERPRINT_MISMATCH


# 22: binding owner mismatch guard (direct unit test of the pure helper) ------------------


def test_binding_owner_mismatch_guard() -> None:
    person_id = uuid4()
    other_person_id = uuid4()
    row = CandidateIdentityBindingRow(
        binding_fingerprint="a" * 64,
        person_entity_id=str(other_person_id),
        master_resume_id="resume-1",
        binding_schema_version="candidate-identity-binding-schema-v1",
    )
    owner_key = _owner_key(person_id)
    assert _binding_owner_matches(row, owner_key) is False

    matching_row = CandidateIdentityBindingRow(
        binding_fingerprint="a" * 64,
        person_entity_id=str(person_id),
        master_resume_id="resume-1",
        binding_schema_version="candidate-identity-binding-schema-v1",
    )
    assert _binding_owner_matches(matching_row, owner_key) is True


# 23-24: exactly one Session / one transaction ---------------------------------------------


def test_resolution_uses_exactly_one_session(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)

    call_count = {"n": 0}
    real_factory = header_service._session_factory

    def counting_factory():
        call_count["n"] += 1
        return real_factory()

    header_service._session_factory = counting_factory
    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))

    assert result.status == CvDocxHeaderBindingStatus.RESOLVED
    assert call_count["n"] == 1


def test_resolution_uses_exactly_one_transaction(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)

    begin_count = {"n": 0}
    real_factory = header_service._session_factory

    class _CountingSession:
        def __init__(self, real_session):
            self._real = real_session

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

        def begin(self):
            begin_count["n"] += 1
            return self._real.begin()

        def __getattr__(self, item):
            return getattr(self._real, item)

    def fake_factory():
        return _CountingSession(real_factory())

    header_service._session_factory = fake_factory
    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))

    assert result.status == CvDocxHeaderBindingStatus.RESOLVED
    assert begin_count["n"] == 1


# 25-31: field resolution / canonicalization ------------------------------------------------


def test_resolved_header_fields(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)

    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))
    header = result.header
    assert header.full_name == "Marek Grabowski"
    assert header.email == "marek@example.com"
    assert header.phone == "+48 600 000 000"
    assert header.location == "Warsaw, Poland"
    assert header.linkedin == "https://linkedin.com/in/marekgrabowski"
    assert "marekgrabowski" in header.linkedin


def test_website_is_never_part_of_header_model() -> None:
    assert "website" not in CvDocxHeaderBinding.model_fields
    assert "github" not in CvDocxHeaderBinding.model_fields
    assert "title" not in CvDocxHeaderBinding.model_fields


# 32-34: fingerprint sensitivity -------------------------------------------------------------


def test_website_only_change_leaves_fingerprint_unchanged(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_a, resume_a = _bind(session_factory, binding_service, personal_info=_FULL_PERSONAL_INFO)
    header_a = header_service.resolve_header_binding(owner_key=_owner_key(person_a, "job-a")).header

    _set_is_master(session_factory, resume_a, False)
    other_info = dict(_FULL_PERSONAL_INFO, website="https://a-totally-different-site.example")
    person_b, _ = _bind(session_factory, binding_service, personal_info=other_info)
    header_b = header_service.resolve_header_binding(owner_key=_owner_key(person_b, "job-b")).header

    assert header_a.source_personal_info_fingerprint == header_b.source_personal_info_fingerprint


def test_linkedin_change_changes_fingerprint(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_a, resume_a = _bind(session_factory, binding_service, personal_info=_FULL_PERSONAL_INFO)
    header_a = header_service.resolve_header_binding(owner_key=_owner_key(person_a, "job-a")).header

    _set_is_master(session_factory, resume_a, False)
    other_info = dict(_FULL_PERSONAL_INFO, linkedin="linkedin.com/in/someone-else")
    person_b, _ = _bind(session_factory, binding_service, personal_info=other_info)
    header_b = header_service.resolve_header_binding(owner_key=_owner_key(person_b, "job-b")).header

    assert header_a.source_personal_info_fingerprint != header_b.source_personal_info_fingerprint


def test_full_name_change_changes_fingerprint(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_a, resume_a = _bind(session_factory, binding_service, personal_info=_FULL_PERSONAL_INFO)
    header_a = header_service.resolve_header_binding(owner_key=_owner_key(person_a, "job-a")).header

    _set_is_master(session_factory, resume_a, False)
    other_info = dict(_FULL_PERSONAL_INFO, name="Someone Else Entirely")
    person_b, _ = _bind(session_factory, binding_service, personal_info=other_info)
    header_b = header_service.resolve_header_binding(owner_key=_owner_key(person_b, "job-b")).header

    assert header_a.source_personal_info_fingerprint != header_b.source_personal_info_fingerprint


def test_compute_source_personal_info_fingerprint_matches_resolved_header(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)

    header = header_service.resolve_header_binding(owner_key=_owner_key(person_id)).header
    recomputed = compute_source_personal_info_fingerprint(
        full_name=header.full_name,
        email=header.email,
        phone=header.phone,
        location=header.location,
        linkedin=header.linkedin,
    )
    assert recomputed == header.source_personal_info_fingerprint


# 35-36: fail-closed re-verification ------------------------------------------------------------


def test_resume_no_longer_master_fails_closed(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, resume_id = _bind(session_factory, binding_service)

    with session_factory() as session:
        with session.begin():
            resume = session.get(Resume, resume_id)
            resume.is_master = False

    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))
    assert result.status == CvDocxHeaderBindingStatus.RESUME_IS_NOT_MASTER
    assert result.header is None


def test_missing_binding_fails_closed(env) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)

    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))
    assert result.status == CvDocxHeaderBindingStatus.IDENTITY_BINDING_NOT_FOUND
    assert result.header is None


# 37: session-factory failure maps STORAGE_UNAVAILABLE --------------------------------------


def test_session_factory_failure_maps_storage_unavailable(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    header_service, binding_service, session_factory, _ = env
    person_id, _ = _bind(session_factory, binding_service)

    def boom():
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(header_service, "_session_factory", boom)
    result = header_service.resolve_header_binding(owner_key=_owner_key(person_id))
    assert result.status == CvDocxHeaderBindingStatus.STORAGE_UNAVAILABLE
