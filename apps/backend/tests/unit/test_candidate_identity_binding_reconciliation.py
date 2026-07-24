"""Unit tests for ``run_candidate_identity_binding_reconciliation``: standard
mode is read-only, and every drift category (missing entity, non-PERSON,
non-ACTIVE informational, missing resume, non-master resume, PersonalInfo
missing/invalid, blank full_name, fingerprint mismatch, schema-version
mismatch) is detected.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db_engine import init_models_sync
from app.models import CandidateIdentityBinding as CandidateIdentityBindingRow
from app.models import Resume, TruthEntity
from app.services.candidate_identity_binding_reconciliation import (
    CandidateIdentityBindingReconciliationIssueCode,
    run_candidate_identity_binding_reconciliation,
)
from app.services.candidate_identity_binding_sql_repository import CandidateIdentityBindingSqlService


def _fake_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_person_entity(session_factory, *, entity_type="PERSON", status="ACTIVE") -> str:
    entity_id = str(uuid4())
    with session_factory() as session:
        with session.begin():
            session.add(
                TruthEntity(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    display_name="Test Person",
                    status=status,
                    content_fingerprint=_fake_sha256(entity_id),
                )
            )
    return entity_id


def _make_resume(session_factory, *, is_master=True, personal_info="__default__") -> str:
    resume_id = str(uuid4())
    if personal_info == "__default__":
        personal_info = {"name": "Jane Doe"}
    processed_data = {"personalInfo": personal_info} if personal_info is not None else None
    with session_factory() as session:
        with session.begin():
            session.add(
                Resume(
                    resume_id=resume_id, content="# R", is_master=is_master, processed_data=processed_data
                )
            )
    return resume_id


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}", future=True)
    init_models_sync(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    service = CandidateIdentityBindingSqlService(session_factory)
    return service, session_factory, engine


def _issue_codes(report):
    return {issue.issue_code for issue in report.issues}


# 57: standard mode read-only ------------------------------------------------------------


def test_standard_mode_is_read_only(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    result = service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)
    assert result.status.value == "CREATED"

    before = run_candidate_identity_binding_reconciliation(session_factory)
    again = run_candidate_identity_binding_reconciliation(session_factory)
    assert before == again
    assert before.issues == ()
    assert before.scanned_binding_count == 1

    with session_factory() as session:
        rows = list(session.execute(select(CandidateIdentityBindingRow)).scalars())
        assert len(rows) == 1


# 58: missing entity detected -------------------------------------------------------------


def test_missing_entity_detected(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(text("DELETE FROM truth_entities WHERE entity_id = :id"), {"id": person_id})
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.MISSING_PERSON_ENTITY in _issue_codes(report)


# 59: non-PERSON entity detected ------------------------------------------------------------


def test_non_person_entity_detected(env) -> None:
    service, session_factory, engine = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    # entity_type is otherwise immutable (trg_truth_entities_type_immutable,
    # a pre-existing P1 invariant) -- drop it only to manufacture the
    # corrupted state this reconciliation check exists to detect.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_truth_entities_type_immutable")
    with session_factory() as session:
        session.execute(
            text("UPDATE truth_entities SET entity_type = 'EMPLOYMENT' WHERE entity_id = :id"),
            {"id": person_id},
        )
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.ENTITY_IS_NOT_PERSON in _issue_codes(report)


# 60: non-ACTIVE status informational --------------------------------------------------------


def test_non_active_status_is_informational(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        entity = session.get(TruthEntity, person_id)
        entity.status = "REVIEW_REQUIRED"
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    codes = _issue_codes(report)
    assert CandidateIdentityBindingReconciliationIssueCode.PERSON_ENTITY_NOT_ACTIVE in codes
    # Informational only -- never reported as MISSING_PERSON_ENTITY or ENTITY_IS_NOT_PERSON.
    assert CandidateIdentityBindingReconciliationIssueCode.MISSING_PERSON_ENTITY not in codes
    assert CandidateIdentityBindingReconciliationIssueCode.ENTITY_IS_NOT_PERSON not in codes


# 61: missing Resume detected -----------------------------------------------------------------


def test_missing_resume_detected(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(text("DELETE FROM resumes WHERE resume_id = :id"), {"id": resume_id})
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.MISSING_MASTER_RESUME in _issue_codes(report)


# 62: non-master Resume detected --------------------------------------------------------------


def test_non_master_resume_detected(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        resume = session.get(Resume, resume_id)
        resume.is_master = False
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.RESUME_IS_NOT_MASTER in _issue_codes(report)


# 63: PersonalInfo missing detected -------------------------------------------------------------


def test_personal_info_missing_detected(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        resume = session.get(Resume, resume_id)
        resume.processed_data = None
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.PERSONAL_INFO_MISSING in _issue_codes(report)


# 64: PersonalInfo invalid detected --------------------------------------------------------------


def test_personal_info_invalid_detected(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        resume = session.get(Resume, resume_id)
        resume.processed_data = {"personalInfo": {"name": "Jane", "email": 12345}}
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.PERSONAL_INFO_INVALID in _issue_codes(report)


# 65: blank full_name detected ------------------------------------------------------------------


def test_blank_full_name_detected(env) -> None:
    service, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    service.create_binding(person_entity_id=UUID(person_id), master_resume_id=resume_id)

    with session_factory() as session:
        resume = session.get(Resume, resume_id)
        resume.processed_data = {"personalInfo": {"name": "   "}}
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.BLANK_FULL_NAME in _issue_codes(report)


# 66: binding fingerprint mismatch detected --------------------------------------------------------


def test_binding_fingerprint_mismatch_detected(env) -> None:
    service, session_factory, engine = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    result = service.create_binding(
        person_entity_id=UUID(person_id), master_resume_id=resume_id
    )

    # candidate_identity_bindings is otherwise fully immutable
    # (trg_candidate_identity_bindings_immutable) -- drop it only to
    # manufacture the corrupted state this reconciliation check exists to
    # detect (e.g. a legacy row written before this fingerprint scheme).
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_candidate_identity_bindings_immutable")
    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE candidate_identity_bindings SET binding_fingerprint = :bad "
                "WHERE binding_fingerprint = :fp"
            ),
            {"bad": "f" * 64, "fp": result.binding.binding_fingerprint},
        )
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.BINDING_FINGERPRINT_MISMATCH in _issue_codes(
        report
    )


# 67: schema-version mismatch detected -----------------------------------------------------------


def test_schema_version_mismatch_detected(env) -> None:
    service, session_factory, engine = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    result = service.create_binding(
        person_entity_id=UUID(person_id), master_resume_id=resume_id
    )

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_candidate_identity_bindings_immutable")
    with session_factory() as session:
        session.execute(text("PRAGMA ignore_check_constraints=1"))
        session.execute(
            text(
                "UPDATE candidate_identity_bindings SET binding_schema_version = :bad "
                "WHERE binding_fingerprint = :fp"
            ),
            {"bad": "candidate-identity-binding-schema-v0", "fp": result.binding.binding_fingerprint},
        )
        session.commit()

    report = run_candidate_identity_binding_reconciliation(session_factory)
    assert CandidateIdentityBindingReconciliationIssueCode.SCHEMA_VERSION_MISMATCH in _issue_codes(report)
