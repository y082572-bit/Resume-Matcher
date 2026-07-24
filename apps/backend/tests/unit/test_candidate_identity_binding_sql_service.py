"""Unit tests for ``CandidateIdentityBindingSqlService.create_binding``:
the full validation chain, idempotent/identical-binding semantics,
concurrency, the closed error channel (no leaked ``IntegrityError``/
``OperationalError``), Session discipline, and immutability at the raw SQL
layer.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.db_engine import init_models_sync
from app.models import Resume, TruthEntity
from app.schemas.candidate_identity_binding import BindMasterResumeToPersonEntityStatus
from app.services.candidate_identity_binding_sql_repository import (
    CandidateIdentityBindingSqlRepository,
    CandidateIdentityBindingSqlService,
)


def _fake_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_person_entity(
    session_factory, *, entity_type: str = "PERSON", status: str = "ACTIVE"
) -> UUID:
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
    return UUID(entity_id)


def _make_resume(
    session_factory, *, is_master: bool = True, personal_info: dict | None | str = "__default__"
) -> str:
    resume_id = str(uuid4())
    if personal_info == "__default__":
        personal_info = {"name": "Jane Doe", "email": "jane@example.com"}
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


def _setup_valid(session_factory) -> tuple[UUID, str]:
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)
    return person_id, resume_id


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'candidate_identity_binding.db'}", future=True)
    init_models_sync(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    service = CandidateIdentityBindingSqlService(session_factory)
    repo = CandidateIdentityBindingSqlRepository(session_factory)
    return service, repo, session_factory, engine


# 1-2: creation / idempotent identical bind --------------------------------------------


def test_valid_person_and_master_resume_creates_binding(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)

    assert result.status == BindMasterResumeToPersonEntityStatus.CREATED
    assert result.binding is not None
    assert result.binding.person_entity_id == person_id
    assert result.binding.master_resume_id == resume_id
    assert result.binding.binding_schema_version == "candidate-identity-binding-schema-v1"
    assert len(result.binding.binding_fingerprint) == 64


def test_identical_bind_is_idempotent(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)

    first = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    second = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)

    assert first.status == BindMasterResumeToPersonEntityStatus.CREATED
    assert second.status == BindMasterResumeToPersonEntityStatus.BINDING_ALREADY_EXISTS_IDENTICAL
    assert second.binding == first.binding


# 3-9: validation chain -----------------------------------------------------------------


def test_person_not_found(env) -> None:
    service, _, session_factory, _ = env
    _, resume_id = _setup_valid(session_factory)

    result = service.create_binding(person_entity_id=uuid4(), master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.PERSON_ENTITY_NOT_FOUND
    assert result.binding is None


def test_entity_not_person(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory, entity_type="EMPLOYMENT")
    resume_id = _make_resume(session_factory)

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.ENTITY_IS_NOT_PERSON


def test_resume_not_found(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory)

    result = service.create_binding(person_entity_id=person_id, master_resume_id=str(uuid4()))
    assert result.status == BindMasterResumeToPersonEntityStatus.MASTER_RESUME_NOT_FOUND


def test_resume_not_master(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory, is_master=False)

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.RESUME_IS_NOT_MASTER


def test_personal_info_missing(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory, personal_info=None)

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.PERSONAL_INFO_MISSING


def test_personal_info_invalid(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    # email must be a string -- an int fails PersonalInfo validation.
    resume_id = _make_resume(session_factory, personal_info={"name": "Jane Doe", "email": 12345})

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.PERSONAL_INFO_INVALID


def test_blank_full_name(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory, personal_info={"name": "   "})

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.FULL_NAME_MISSING


# 10-11: one binding per person / per resume ---------------------------------------------


def test_same_person_cannot_bind_two_resumes(env) -> None:
    service, _, session_factory, _ = env
    person_id = _make_person_entity(session_factory)
    resume_a = _make_resume(session_factory)

    first = service.create_binding(person_entity_id=person_id, master_resume_id=resume_a)
    assert first.status == BindMasterResumeToPersonEntityStatus.CREATED

    # resume_a stops being master; a different resume becomes master (only
    # one resume may hold is_master=1 at a time).
    _set_is_master(session_factory, resume_a, False)
    resume_b = _make_resume(session_factory)

    second = service.create_binding(person_entity_id=person_id, master_resume_id=resume_b)
    assert second.status == BindMasterResumeToPersonEntityStatus.PERSON_ALREADY_BOUND
    assert second.binding is None


def test_same_resume_cannot_bind_two_persons(env) -> None:
    service, _, session_factory, _ = env
    person_a = _make_person_entity(session_factory)
    person_b = _make_person_entity(session_factory)
    resume_id = _make_resume(session_factory)

    first = service.create_binding(person_entity_id=person_a, master_resume_id=resume_id)
    assert first.status == BindMasterResumeToPersonEntityStatus.CREATED

    second = service.create_binding(person_entity_id=person_b, master_resume_id=resume_id)
    assert second.status == BindMasterResumeToPersonEntityStatus.MASTER_RESUME_ALREADY_BOUND
    assert second.binding is None


# 12/20-22: concurrency, created_at divergence never breaks identical classification ------


def test_identical_concurrent_binds_converge(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        results[name] = service.create_binding(
            person_entity_id=person_id, master_resume_id=resume_id
        )

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r.status.value for r in results.values())
    assert statuses == sorted(
        [
            BindMasterResumeToPersonEntityStatus.CREATED.value,
            BindMasterResumeToPersonEntityStatus.BINDING_ALREADY_EXISTS_IDENTICAL.value,
        ]
    )
    # Both results reference the exact same converged, persisted row --
    # differing local created_at computations by either racer never break
    # identical-binding classification.
    bindings = [r.binding for r in results.values()]
    assert bindings[0] == bindings[1]

    with session_factory() as session:
        from app.models import CandidateIdentityBinding as Row
        from sqlalchemy import select

        count = len(list(session.execute(select(Row)).scalars()))
        assert count == 1


# 13: no raw exception leak ---------------------------------------------------------------


def test_no_raw_exception_leaks_from_create_binding(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)

    try:
        service.create_binding(person_entity_id=uuid4(), master_resume_id="nope")
        service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
        service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    except (IntegrityError, OperationalError):
        pytest.fail("create_binding must never leak a raw SQLAlchemy exception")


# 14: first attempt uses exactly one Session -----------------------------------------------


def test_first_attempt_uses_one_session(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)

    call_count = {"n": 0}
    real_factory = service._session_factory

    def counting_factory():
        call_count["n"] += 1
        return real_factory()

    service._session_factory = counting_factory
    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)

    assert result.status == BindMasterResumeToPersonEntityStatus.CREATED
    assert call_count["n"] == 1


# 15: reread after IntegrityError uses a literal new Session -------------------------------


def test_reread_after_integrity_error_uses_new_session(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)
    first = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert first.status == BindMasterResumeToPersonEntityStatus.CREATED

    real_factory = service._session_factory
    seen_sessions: list[object] = []
    call_count = {"n": 0}

    class _ExplodingSession:
        def __init__(self, real_session):
            self._real = real_session

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

        def begin(self):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))
            return self._real.begin()

        def __getattr__(self, item):
            return getattr(self._real, item)

    def fake_factory():
        session = _ExplodingSession(real_factory())
        seen_sessions.append(session)
        return session

    monkeypatch.setattr(service, "_session_factory", fake_factory)

    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.BINDING_ALREADY_EXISTS_IDENTICAL
    assert len(seen_sessions) == 2
    assert seen_sessions[0] is not seen_sessions[1]


# 16: session-factory failure maps STORAGE_UNAVAILABLE --------------------------------------


def test_session_factory_failure_maps_storage_unavailable(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _ = env

    def boom():
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(service, "_session_factory", boom)
    result = service.create_binding(person_entity_id=uuid4(), master_resume_id="whatever")
    assert result.status == BindMasterResumeToPersonEntityStatus.STORAGE_UNAVAILABLE


# 17: no async persistence constructs in the new production modules -------------------------


_NEW_PRODUCTION_MODULES = (
    "app.schemas.candidate_identity_binding",
    "app.services.candidate_identity_binding_repository",
    "app.services.candidate_identity_binding_sql_repository",
    "app.services.cv_docx_header_binding_builder",
    "app.services.candidate_identity_binding_schema_manifest",
    "app.services.candidate_identity_binding_reconciliation",
)

_FORBIDDEN_ASYNC_NAMES = {"AsyncSession", "async_sessionmaker", "create_async_engine", "AsyncEngine"}


def test_new_modules_use_only_sync_sqlalchemy_persistence() -> None:
    import importlib

    for module_name in _NEW_PRODUCTION_MODULES:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"{module_name} defines an async def: {node.name}"
            )
            assert not isinstance(node, ast.Await), f"{module_name} contains an await expression"
            if isinstance(node, ast.Name):
                assert node.id not in _FORBIDDEN_ASYNC_NAMES, (
                    f"{module_name} references forbidden async construct {node.id}"
                )
            if isinstance(node, ast.Attribute):
                assert node.attr not in _FORBIDDEN_ASYNC_NAMES, (
                    f"{module_name} references forbidden async construct {node.attr}"
                )


# 39-40: immutability enforced at the raw SQL layer ------------------------------------------


def test_raw_sql_update_is_blocked(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)
    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.CREATED

    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "UPDATE candidate_identity_bindings SET created_at = 'tampered' "
                    "WHERE binding_fingerprint = :fp"
                ),
                {"fp": result.binding.binding_fingerprint},
            )
            session.commit()


def test_raw_sql_delete_is_blocked(env) -> None:
    service, _, session_factory, _ = env
    person_id, resume_id = _setup_valid(session_factory)
    result = service.create_binding(person_entity_id=person_id, master_resume_id=resume_id)
    assert result.status == BindMasterResumeToPersonEntityStatus.CREATED

    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "DELETE FROM candidate_identity_bindings WHERE binding_fingerprint = :fp"
                ),
                {"fp": result.binding.binding_fingerprint},
            )
            session.commit()
