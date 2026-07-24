"""Explicit Provenance Stage P6-B2A-I1: the synchronous SQL
``CandidateIdentityBindingRepository`` read implementation and the sole
write authority, ``CandidateIdentityBindingSqlService.create_binding``.

Every persistence construct here is synchronous SQLAlchemy
(``sqlalchemy.orm.Session``/``sessionmaker``) -- never ``AsyncSession``,
``async_sessionmaker``, ``async def``, an async engine, ``await``, or a
threadpool adapter.

``create_binding``'s first attempt runs entirely inside one ``Session`` and
one transaction (load ``TruthEntity``, load ``Resume``, validate
``PersonalInfo``, compute the fingerprint, pre-check for an existing row by
fingerprint/person/resume, insert, flush, commit). A concurrent
``IntegrityError`` (two identical first-writers racing on the same
``binding_fingerprint`` primary key, or on either UNIQUE constraint) is
never resolved by reusing that ``Session`` -- it is closed, and a literally
new one is opened to re-read and classify the outcome. No raw
``IntegrityError``/``OperationalError``/``SQLAlchemyError`` ever leaves
``create_binding``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.models import CandidateIdentityBinding as CandidateIdentityBindingRow
from app.models import Resume, TruthEntity
from app.schemas.candidate_identity_binding import (
    CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION,
    BindMasterResumeToPersonEntityResult,
    BindMasterResumeToPersonEntityStatus,
    CandidateIdentityBinding,
)
from app.schemas.models import PersonalInfo
from app.schemas.truth_entity import EntityType
from app.services.candidate_identity_binding_repository import compute_binding_fingerprint


def _row_to_domain(row: CandidateIdentityBindingRow) -> CandidateIdentityBinding:
    return CandidateIdentityBinding(
        binding_fingerprint=row.binding_fingerprint,
        person_entity_id=UUID(row.person_entity_id),
        master_resume_id=row.master_resume_id,
        binding_schema_version=row.binding_schema_version,
        created_at=row.created_at,
    )


def _status(status: BindMasterResumeToPersonEntityStatus) -> BindMasterResumeToPersonEntityResult:
    return BindMasterResumeToPersonEntityResult(status=status)


class CandidateIdentityBindingSqlRepository:
    """Read-only SQL access to ``candidate_identity_bindings``. Exactly
    three public methods -- no ``create_binding``, no update, no delete."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_binding_by_person(self, person_entity_id: UUID) -> CandidateIdentityBinding | None:
        with self._session_factory() as session:
            row = session.execute(
                select(CandidateIdentityBindingRow).where(
                    CandidateIdentityBindingRow.person_entity_id == str(person_entity_id)
                )
            ).scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    def get_binding_by_master_resume(
        self, master_resume_id: str
    ) -> CandidateIdentityBinding | None:
        with self._session_factory() as session:
            row = session.execute(
                select(CandidateIdentityBindingRow).where(
                    CandidateIdentityBindingRow.master_resume_id == master_resume_id
                )
            ).scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    def list_all_bindings(self) -> tuple[CandidateIdentityBinding, ...]:
        with self._session_factory() as session:
            rows = session.execute(select(CandidateIdentityBindingRow)).scalars().all()
            return tuple(_row_to_domain(row) for row in rows)


class CandidateIdentityBindingSqlService:
    """The sole write authority over ``candidate_identity_bindings``.

    Receives a ``sessionmaker``, never an already-open ``Session`` -- it
    owns the full lifecycle of every ``Session`` it uses. Never calls into
    ``CandidateIdentityBindingSqlRepository`` or any other repository that
    would open its own, second ``Session`` mid-transaction.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_binding(
        self, *, person_entity_id: UUID, master_resume_id: str
    ) -> BindMasterResumeToPersonEntityResult:
        needs_reread = False
        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        result = self._create_binding_transaction(
                            session, person_entity_id, master_resume_id
                        )
                    return result
                except IntegrityError:
                    needs_reread = True
        except OperationalError:
            return _status(BindMasterResumeToPersonEntityStatus.STORAGE_UNAVAILABLE)

        assert needs_reread
        # The Session that raised IntegrityError is already closed (the
        # `with self._session_factory() as session:` block above has fully
        # exited) -- the reread below always opens a brand-new one.
        try:
            return self._reread_after_integrity_error(person_entity_id, master_resume_id)
        except OperationalError:
            return _status(BindMasterResumeToPersonEntityStatus.STORAGE_UNAVAILABLE)

    def _create_binding_transaction(
        self, session: Session, person_entity_id: UUID, master_resume_id: str
    ) -> BindMasterResumeToPersonEntityResult:
        entity = session.get(TruthEntity, str(person_entity_id))
        if entity is None:
            return _status(BindMasterResumeToPersonEntityStatus.PERSON_ENTITY_NOT_FOUND)
        if entity.entity_type != EntityType.PERSON.value:
            return _status(BindMasterResumeToPersonEntityStatus.ENTITY_IS_NOT_PERSON)

        resume = session.get(Resume, master_resume_id)
        if resume is None:
            return _status(BindMasterResumeToPersonEntityStatus.MASTER_RESUME_NOT_FOUND)
        if not resume.is_master:
            return _status(BindMasterResumeToPersonEntityStatus.RESUME_IS_NOT_MASTER)

        processed_data = resume.processed_data
        raw_personal_info = (
            processed_data.get("personalInfo") if isinstance(processed_data, dict) else None
        )
        if not isinstance(raw_personal_info, dict):
            return _status(BindMasterResumeToPersonEntityStatus.PERSONAL_INFO_MISSING)

        try:
            personal_info = PersonalInfo.model_validate(raw_personal_info)
        except ValidationError:
            return _status(BindMasterResumeToPersonEntityStatus.PERSONAL_INFO_INVALID)

        if not personal_info.name.strip():
            return _status(BindMasterResumeToPersonEntityStatus.FULL_NAME_MISSING)

        fingerprint = compute_binding_fingerprint(
            person_entity_id=person_entity_id,
            master_resume_id=master_resume_id,
            binding_schema_version=CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION,
        )

        # A single combined SELECT -- never three separate ones -- so the
        # fingerprint/person/resume pre-check can never be split across an
        # interleaving concurrent commit (SQLite/pysqlite has no implicit
        # transaction for a bare SELECT; only one statement here closes that
        # window). Two identical concurrent requests must still converge to
        # CREATED/BINDING_ALREADY_EXISTS_IDENTICAL, never a spurious
        # PERSON_ALREADY_BOUND/MASTER_RESUME_ALREADY_BOUND for the loser.
        conflicting_rows = session.execute(
            select(CandidateIdentityBindingRow).where(
                or_(
                    CandidateIdentityBindingRow.binding_fingerprint == fingerprint,
                    CandidateIdentityBindingRow.person_entity_id == str(person_entity_id),
                    CandidateIdentityBindingRow.master_resume_id == master_resume_id,
                )
            )
        ).scalars().all()

        identical_row = next(
            (row for row in conflicting_rows if row.binding_fingerprint == fingerprint), None
        )
        if identical_row is not None:
            return BindMasterResumeToPersonEntityResult(
                status=BindMasterResumeToPersonEntityStatus.BINDING_ALREADY_EXISTS_IDENTICAL,
                binding=_row_to_domain(identical_row),
            )

        if any(row.person_entity_id == str(person_entity_id) for row in conflicting_rows):
            return _status(BindMasterResumeToPersonEntityStatus.PERSON_ALREADY_BOUND)

        if any(row.master_resume_id == master_resume_id for row in conflicting_rows):
            return _status(BindMasterResumeToPersonEntityStatus.MASTER_RESUME_ALREADY_BOUND)

        row = CandidateIdentityBindingRow(
            binding_fingerprint=fingerprint,
            person_entity_id=str(person_entity_id),
            master_resume_id=master_resume_id,
            binding_schema_version=CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION,
        )
        session.add(row)
        session.flush()
        return BindMasterResumeToPersonEntityResult(
            status=BindMasterResumeToPersonEntityStatus.CREATED,
            binding=_row_to_domain(row),
        )

    def _reread_after_integrity_error(
        self, person_entity_id: UUID, master_resume_id: str
    ) -> BindMasterResumeToPersonEntityResult:
        fingerprint = compute_binding_fingerprint(
            person_entity_id=person_entity_id,
            master_resume_id=master_resume_id,
            binding_schema_version=CANDIDATE_IDENTITY_BINDING_SCHEMA_VERSION,
        )
        with self._session_factory() as session:
            row = session.get(CandidateIdentityBindingRow, fingerprint)
            if row is not None:
                return BindMasterResumeToPersonEntityResult(
                    status=BindMasterResumeToPersonEntityStatus.BINDING_ALREADY_EXISTS_IDENTICAL,
                    binding=_row_to_domain(row),
                )

            existing_by_person = session.execute(
                select(CandidateIdentityBindingRow).where(
                    CandidateIdentityBindingRow.person_entity_id == str(person_entity_id)
                )
            ).scalar_one_or_none()
            if existing_by_person is not None:
                return _status(BindMasterResumeToPersonEntityStatus.PERSON_ALREADY_BOUND)

            existing_by_resume = session.execute(
                select(CandidateIdentityBindingRow).where(
                    CandidateIdentityBindingRow.master_resume_id == master_resume_id
                )
            ).scalar_one_or_none()
            if existing_by_resume is not None:
                return _status(BindMasterResumeToPersonEntityStatus.MASTER_RESUME_ALREADY_BOUND)

            return _status(BindMasterResumeToPersonEntityStatus.STORAGE_CONFLICT)
