"""SQLAlchemy (SQLite) data layer for Resume Matcher.

This is a behavior-preserving replacement for the original TinyDB wrapper. The
``Database`` facade keeps the same method names/signatures and returns **plain
dicts** (never ORM rows), so the ~50 call sites only needed ``await`` added.

Two engines back one SQLite file:
- an **async** engine (``aiosqlite``) for the document tables and applications;
- a **sync** engine for the encrypted ``api_keys`` table, which is read on the
  synchronous LLM hot path (``get_llm_config`` → ``resolve_api_key``).
"""

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db_engine import init_models_sync, make_async_engine, make_sync_engine
from app.models import (
    ApiKey,
    Application,
    CVTransformationPlanApproval,
    CVTransformationGeneration,
    Improvement,
    Job,
    MetricEvent,
    Resume,
)
from app.services.project_metrics import (
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    MetricEventInput,
    record_metric_event,
    RecordEventStatus,
    ApplicationStatusConflictError,
)
from app.services.truth_repository import TruthService

logger = logging.getLogger(__name__)


def _require_metric_event_recorded(result, msg: str) -> None:
    if result.status == RecordEventStatus.DUPLICATE:
        raise ValueError(f"Duplicate metric event detected: {msg}")


# Columns that are first-class on the jobs table; everything else the pipeline
# attaches dynamically is stored in ``metadata_json`` (see Job model).
_JOB_CORE_FIELDS = frozenset({"job_id", "content", "resume_id", "created_at"})

# Application status columns (stable keys, decoupled from i18n labels).
APPLICATION_STATUSES: tuple[str, ...] = (
    "saved",
    "applied",
    "no_response",
    "response",
    "interview",
    "accepted",
    "rejected",
)


def _now() -> str:
    """Current UTC time as an ISO-8601 string (TinyDB-era format)."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Async SQLAlchemy facade for resume matcher data."""

    # Serializes concurrent master-resume promotion. Stays the *primary*
    # mechanism for the single-master invariant (the partial unique index is a
    # storage-level backstop).
    _master_resume_lock = asyncio.Lock()

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or settings.sqlite_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._async_engine = None
        self._async_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._sync_engine = None
        self._sync_session_factory: sessionmaker[Session] | None = None
        self._initialized = False

    # -- engine / session plumbing ------------------------------------------

    def _ensure_initialized(self) -> None:
        """Create engines and tables once (idempotent).

        Tables are created via the **sync** engine so both the sync (api_keys)
        and async (docs) paths see them immediately, without needing an event
        loop. Both engines point at the same file.
        """
        if self._initialized:
            return
        self._sync_engine = make_sync_engine(self.db_path)
        self._sync_session_factory = sessionmaker(self._sync_engine, expire_on_commit=False)
        init_models_sync(self._sync_engine)
        self._async_engine = make_async_engine(self.db_path)
        self._async_session_factory = async_sessionmaker(
            self._async_engine, expire_on_commit=False
        )
        self._initialized = True

    @property
    def _session(self) -> async_sessionmaker[AsyncSession]:
        self._ensure_initialized()
        assert self._async_session_factory is not None
        return self._async_session_factory

    @property
    def _sync(self) -> sessionmaker[Session]:
        self._ensure_initialized()
        assert self._sync_session_factory is not None
        return self._sync_session_factory

    @property
    def truth_service(self) -> TruthService:
        """Return the inactive P1 internal service without wiring legacy flows."""

        return TruthService(self._session)

    async def close(self) -> None:
        """Dispose engines and release file handles."""
        if self._async_engine is not None:
            await self._async_engine.dispose()
            self._async_engine = None
            self._async_session_factory = None
        if self._sync_engine is not None:
            self._sync_engine.dispose()
            self._sync_engine = None
            self._sync_session_factory = None
        self._initialized = False

    # -- row -> dict converters ---------------------------------------------

    @staticmethod
    def _resume_to_dict(row: Resume) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "resume_id": row.resume_id,
            "content": row.content,
            "content_type": row.content_type,
            "filename": row.filename,
            "is_master": row.is_master,
            "parent_id": row.parent_id,
            "processed_data": row.processed_data,
            "processing_status": row.processing_status,
            "cover_letter": row.cover_letter,
            "outreach_message": row.outreach_message,
            "interview_prep": row.interview_prep,
            "title": row.title,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        # Preserve TinyDB absence semantics: omit the key entirely when None.
        if row.original_markdown is not None:
            doc["original_markdown"] = row.original_markdown
        return doc

    @staticmethod
    def _job_to_dict(row: Job) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "job_id": row.job_id,
            "content": row.content,
            "resume_id": row.resume_id,
            "created_at": row.created_at,
        }
        meta = row.metadata_json or {}
        if isinstance(meta, dict):
            for k, v in meta.items():
                if k != "lifecycle_token":
                    doc[k] = v
        return doc

    @staticmethod
    def _improvement_to_dict(row: Improvement) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "original_resume_id": row.original_resume_id,
            "tailored_resume_id": row.tailored_resume_id,
            "job_id": row.job_id,
            "improvements": row.improvements,
            "created_at": row.created_at,
        }

    @staticmethod
    def _application_to_dict(row: Application) -> dict[str, Any]:
        return {
            "application_id": row.application_id,
            "job_id": row.job_id,
            "resume_id": row.resume_id,
            "master_resume_id": row.master_resume_id,
            "status": row.status,
            "company": row.company,
            "role": row.role,
            "applied_at": row.applied_at,
            "notes": row.notes,
            "position": row.position,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _transformation_approval_to_dict(
        row: CVTransformationPlanApproval,
    ) -> dict[str, Any]:
        return {
            "approval_id": row.approval_id,
            "plan_version": row.plan_version,
            "plan_fingerprint": row.plan_fingerprint,
            "resume_id": row.resume_id,
            "job_id": row.job_id,
            "status": row.status,
            "decisions": row.decisions,
            "guardrails_acknowledged": row.guardrails_acknowledged,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _transformation_generation_to_dict(
        row: CVTransformationGeneration,
    ) -> dict[str, Any]:
        return {
            "generation_id": row.generation_id,
            "approval_id": row.approval_id,
            "resume_id": row.resume_id,
            "job_id": row.job_id,
            "plan_version": row.plan_version,
            "plan_fingerprint": row.plan_fingerprint,
            "generation_version": row.generation_version,
            "prompt_version": row.prompt_version,
            "generation_input_fingerprint": row.generation_input_fingerprint,
            "status": row.status,
            "provider": row.provider,
            "model": row.model,
            "draft_resume": row.draft_json,
            "provenance": row.provenance_json or [],
            "failure_code": row.failure_code,
            "attempt_count": row.attempt_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
            "requires_truth_validation": True,
            "truth_validation_status": "NOT_RUN",
            "applied_to_resume": False,
        }

    # -- Resume operations --------------------------------------------------

    async def create_resume(
        self,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        is_master: bool = False,
        parent_id: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        title: str | None = None,
        original_markdown: str | None = None,
        interview_prep: str | None = None,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> dict[str, Any]:
        """Create a new resume entry.

        processing_status: "pending", "processing", "ready", "failed"
        """
        resume_id = str(uuid4())
        now = _now()
        lifecycle_token = str(uuid4())
        async with self._session() as session:
            try:
                if parent_id is not None:
                    if not isinstance(parent_id, str):
                        raise ValueError("parent_id must be a string")
                    if not parent_id.strip():
                        raise ValueError("parent_id cannot be empty or blank")
                    parent_exists = await session.get(Resume, parent_id)
                    if parent_exists is None:
                        raise ValueError(f"Parent resume not found: {parent_id}")

                effective_source = MetricEventSource.SYSTEM if parent_id is not None else source

                row = Resume(
                    resume_id=resume_id,
                    content=content,
                    content_type=content_type,
                    filename=filename,
                    is_master=is_master,
                    parent_id=parent_id,
                    processed_data=processed_data,
                    processing_status=processing_status,
                    cover_letter=cover_letter,
                    outreach_message=outreach_message,
                    interview_prep=interview_prep,
                    title=title,
                    original_markdown=original_markdown,
                    created_at=now,
                    updated_at=now,
                    lifecycle_token=lifecycle_token,
                )
                session.add(row)
                await session.flush()

                # 1. ENTITY_CREATED event
                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"create:resume:{resume_id}:lc:{lifecycle_token}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.RESUME,
                    entity_id=resume_id,
                    lifecycle_id=lifecycle_token,
                    from_state=None,
                    to_state=None,
                    source=effective_source,
                )
                res_created = await record_metric_event(session, event_input)
                _require_metric_event_recorded(res_created, "ENTITY_CREATED duplicate")

                # 2. RESUME_GENERATED event if parent_id is not None
                if parent_id is not None:
                    gen_event_input = MetricEventInput(
                        event_id=str(uuid4()),
                        operation_key=f"generate:resume:{resume_id}:lc:{lifecycle_token}",
                        event_type=MetricEventType.RESUME_GENERATED,
                        entity_type=MetricEntityType.RESUME,
                        entity_id=resume_id,
                        lifecycle_id=lifecycle_token,
                        from_state=None,
                        to_state=None,
                        source=MetricEventSource.SYSTEM,
                    )
                    res_generated = await record_metric_event(session, gen_event_input)
                    _require_metric_event_recorded(res_generated, "RESUME_GENERATED duplicate")

                await session.commit()
            except Exception:
                await session.rollback()
                raise

        doc: dict[str, Any] = {
            "resume_id": resume_id,
            "content": content,
            "content_type": content_type,
            "filename": filename,
            "is_master": is_master,
            "parent_id": parent_id,
            "processed_data": processed_data,
            "processing_status": processing_status,
            "cover_letter": cover_letter,
            "outreach_message": outreach_message,
            "interview_prep": interview_prep,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }
        if original_markdown is not None:
            doc["original_markdown"] = original_markdown
        return doc

    async def create_resume_atomic_master(
        self,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        original_markdown: str | None = None,
        title: str | None = None,
        interview_prep: str | None = None,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> dict[str, Any]:
        """Create a new resume with atomic master assignment.

        Uses an asyncio.Lock to prevent race conditions when multiple uploads
        happen concurrently and both try to become master.
        """
        async with self._master_resume_lock:
            current_master = await self.get_master_resume()
            is_master = current_master is None

            # Recovery: if the current master is stuck failed/processing, demote
            # it so this upload can become the new master.
            if current_master and current_master.get("processing_status") in (
                "failed",
                "processing",
            ):
                async with self._session() as session:
                    row = await session.get(Resume, current_master["resume_id"])
                    if row is not None:
                        row.is_master = False
                        await session.commit()
                is_master = True

            return await self.create_resume(
                content=content,
                content_type=content_type,
                filename=filename,
                is_master=is_master,
                processed_data=processed_data,
                processing_status=processing_status,
                cover_letter=cover_letter,
                outreach_message=outreach_message,
                interview_prep=interview_prep,
                original_markdown=original_markdown,
                title=title,
                source=source,
            )

    async def get_resume(self, resume_id: str) -> dict[str, Any] | None:
        """Get resume by ID."""
        async with self._session() as session:
            row = await session.get(Resume, resume_id)
            return self._resume_to_dict(row) if row else None

    async def get_master_resume(self) -> dict[str, Any] | None:
        """Get the master resume if exists."""
        async with self._session() as session:
            result = await session.execute(
                select(Resume).where(Resume.is_master.is_(True))
            )
            row = result.scalars().first()
            return self._resume_to_dict(row) if row else None

    async def update_resume(self, resume_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update resume by ID.

        Raises:
            ValueError: If resume not found.
        """
        async with self._session() as session:
            row = await session.get(Resume, resume_id)
            if row is None:
                raise ValueError(f"Resume not found: {resume_id}")
            if "parent_id" in updates and updates["parent_id"] != row.parent_id:
                raise ValueError("parent_id is immutable")
            if "lifecycle_token" in updates and updates["lifecycle_token"] != row.lifecycle_token:
                raise ValueError("lifecycle_token is immutable")
            for key, value in updates.items():
                if hasattr(row, key):
                    setattr(row, key, value)
                else:
                    logger.warning("Ignoring unknown resume field on update: %s", key)
            row.updated_at = _now()
            await session.commit()
            return self._resume_to_dict(row)

    async def delete_resume(
        self,
        resume_id: str,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> bool:
        """Delete resume by ID."""
        async with self._session() as session:
            try:
                row = await session.get(Resume, resume_id)
                if row is None:
                    return False
                lifecycle_token = row.lifecycle_token
                # Record ENTITY_DELETED event
                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"delete:resume:{resume_id}:lc:{lifecycle_token}",
                    event_type=MetricEventType.ENTITY_DELETED,
                    entity_type=MetricEntityType.RESUME,
                    entity_id=resume_id,
                    lifecycle_id=lifecycle_token,
                    from_state=None,
                    to_state=None,
                    source=source,
                )
                res_deleted = await record_metric_event(session, event_input)
                _require_metric_event_recorded(res_deleted, "ENTITY_DELETED duplicate")
                await session.delete(row)
                await session.commit()
                return True
            except Exception:
                await session.rollback()
                raise

    async def list_resumes(self) -> list[dict[str, Any]]:
        """List all resumes."""
        async with self._session() as session:
            result = await session.execute(select(Resume).order_by(Resume.created_at))
            return [self._resume_to_dict(row) for row in result.scalars().all()]

    async def set_master_resume(self, resume_id: str) -> bool:
        """Set a resume as the master, unsetting any existing master.

        Returns False if the resume doesn't exist. Demote-then-promote happens
        in a single transaction so the partial unique index is never violated.
        """
        async with self._session() as session:
            target = await session.get(Resume, resume_id)
            if target is None:
                logger.warning("Cannot set master: resume %s not found", resume_id)
                return False

            current = await session.execute(
                select(Resume).where(Resume.is_master.is_(True))
            )
            for row in current.scalars().all():
                if row.resume_id != resume_id:
                    row.is_master = False
            # Flush the demotions before promoting to satisfy the unique index.
            await session.flush()
            target.is_master = True
            await session.commit()
            return True

    # -- Job operations -----------------------------------------------------

    async def create_job(
        self,
        content: str,
        resume_id: str | None = None,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> dict[str, Any]:
        """Create a new job description entry."""
        job_id = str(uuid4())
        now = _now()
        lifecycle_token = str(uuid4())
        async with self._session() as session:
            try:
                row = Job(
                    job_id=job_id,
                    content=content,
                    resume_id=resume_id,
                    created_at=now,
                    metadata_json={},
                    lifecycle_token=lifecycle_token
                )
                session.add(row)
                await session.flush()

                # Record MetricEvent
                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"create:job:{job_id}:lc:{lifecycle_token}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.JOB,
                    entity_id=job_id,
                    lifecycle_id=lifecycle_token,
                    from_state=None,
                    to_state=None,
                    source=source,
                )
                await record_metric_event(session, event_input)
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                orig_msg = str(e.orig) if e.orig else ""
                if "UNIQUE constraint failed: jobs.job_id" in orig_msg:
                    async with self._session() as check_session:
                        dup = await check_session.execute(
                            select(Job).where(Job.job_id == job_id)
                        )
                        found = dup.scalars().first()
                        if found is not None:
                            return self._job_to_dict(found)
                raise
            return {
                "job_id": job_id,
                "content": content,
                "resume_id": resume_id,
                "created_at": now,
            }

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get job by ID (dynamic fields flattened to top level)."""
        async with self._session() as session:
            row = await session.get(Job, job_id)
            return self._job_to_dict(row) if row else None

    async def update_job(
        self, job_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a job by ID.

        Core columns are set directly; every other key is merged into
        ``metadata_json`` so dynamic pipeline fields (``preview_hash``,
        ``job_keywords``, ``company``/``role``, …) round-trip through
        ``get_job`` as top-level keys.
        """
        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return None
            meta = dict(row.metadata_json or {})
            for key, value in updates.items():
                if key == "lifecycle_token":
                    continue
                if key in _JOB_CORE_FIELDS:
                    setattr(row, key, value)
                else:
                    meta[key] = value
            # Reassign so SQLAlchemy detects the JSON mutation.
            row.metadata_json = meta
            await session.commit()
            return self._job_to_dict(row)

    async def delete_job(
        self,
        job_id: str,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> bool:
        """Delete a job by ID (used to clean up an orphaned manual-add job)."""
        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return False
            lifecycle_token = row.lifecycle_token

            event_input = MetricEventInput(
                event_id=str(uuid4()),
                operation_key=f"delete:job:{job_id}:lc:{lifecycle_token}",
                event_type=MetricEventType.ENTITY_DELETED,
                entity_type=MetricEntityType.JOB,
                entity_id=job_id,
                lifecycle_id=lifecycle_token,
                from_state=None,
                to_state=None,
                source=source,
            )
            await record_metric_event(session, event_input)

            await session.delete(row)
            await session.commit()
            return True

    # -- Improvement operations ---------------------------------------------

    async def create_improvement(
        self,
        original_resume_id: str,
        tailored_resume_id: str,
        job_id: str,
        improvements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create an improvement result entry."""
        request_id = str(uuid4())
        now = _now()
        async with self._session() as session:
            session.add(
                Improvement(
                    request_id=request_id,
                    original_resume_id=original_resume_id,
                    tailored_resume_id=tailored_resume_id,
                    job_id=job_id,
                    improvements=improvements,
                    created_at=now,
                )
            )

            # Get Job lifecycle_token if job exists
            job_row = await session.get(Job, job_id)
            if job_row is not None:
                lifecycle_token = job_row.lifecycle_token

                # Check if JOB_ANALYZED event already exists for this lifecycle
                existing_evt = await session.execute(
                    select(MetricEvent).where(
                        MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value,
                        MetricEvent.entity_type == MetricEntityType.JOB.value,
                        MetricEvent.entity_id == job_id,
                        MetricEvent.lifecycle_id == lifecycle_token
                    )
                )
                if existing_evt.scalars().first() is None:
                    event_input = MetricEventInput(
                        event_id=str(uuid4()),
                        operation_key=f"analyze:job:{job_id}:lc:{lifecycle_token}",
                        event_type=MetricEventType.JOB_ANALYZED,
                        entity_type=MetricEntityType.JOB,
                        entity_id=job_id,
                        lifecycle_id=lifecycle_token,
                        from_state=None,
                        to_state=None,
                        source=MetricEventSource.SYSTEM,
                    )
                    await record_metric_event(session, event_input)

            await session.commit()
        return {
            "request_id": request_id,
            "original_resume_id": original_resume_id,
            "tailored_resume_id": tailored_resume_id,
            "job_id": job_id,
            "improvements": improvements,
            "created_at": now,
        }

    async def get_improvement_by_tailored_resume(
        self, tailored_resume_id: str
    ) -> dict[str, Any] | None:
        """Get improvement record by tailored resume ID."""
        async with self._session() as session:
            result = await session.execute(
                select(Improvement).where(
                    Improvement.tailored_resume_id == tailored_resume_id
                )
            )
            row = result.scalars().first()
            return self._improvement_to_dict(row) if row else None

    # -- CV transformation-plan approval operations ------------------------

    async def get_transformation_plan_approval(
        self,
        *,
        job_id: str,
        resume_id: str,
        plan_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an exact approval, or the most recently updated scoped row."""

        async with self._session() as session:
            stmt = select(CVTransformationPlanApproval).where(
                CVTransformationPlanApproval.job_id == job_id,
                CVTransformationPlanApproval.resume_id == resume_id,
            )
            if plan_fingerprint is not None:
                stmt = stmt.where(
                    CVTransformationPlanApproval.plan_fingerprint == plan_fingerprint
                )
            stmt = stmt.order_by(CVTransformationPlanApproval.updated_at.desc())
            row = (await session.execute(stmt)).scalars().first()
            return self._transformation_approval_to_dict(row) if row else None

    async def save_transformation_plan_approval(
        self,
        *,
        job_id: str,
        resume_id: str,
        plan_version: str,
        plan_fingerprint: str,
        status: str,
        decisions: list[dict[str, str]],
        guardrails_acknowledged: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Upsert one plan revision and record its first explicit decision once."""

        now = _now()
        async with self._session() as session:
            approval_id = str(uuid4())
            inserted_id = await session.scalar(
                sqlite_insert(CVTransformationPlanApproval)
                .values(
                    approval_id=approval_id,
                    plan_version=plan_version,
                    plan_fingerprint=plan_fingerprint,
                    resume_id=resume_id,
                    job_id=job_id,
                    status=status,
                    decisions=decisions,
                    guardrails_acknowledged=guardrails_acknowledged,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["job_id", "resume_id", "plan_fingerprint"]
                )
                .returning(CVTransformationPlanApproval.approval_id)
            )
            created = inserted_id is not None
            row = (
                await session.execute(
                    select(CVTransformationPlanApproval).where(
                        CVTransformationPlanApproval.job_id == job_id,
                        CVTransformationPlanApproval.resume_id == resume_id,
                        CVTransformationPlanApproval.plan_fingerprint
                        == plan_fingerprint,
                    )
                )
            ).scalars().first()
            if row is None:
                raise RuntimeError("Approval upsert did not return its persisted row")

            changed = False
            if created:
                older = (
                    await session.execute(
                        select(CVTransformationPlanApproval).where(
                            CVTransformationPlanApproval.job_id == job_id,
                            CVTransformationPlanApproval.resume_id == resume_id,
                            CVTransformationPlanApproval.plan_fingerprint
                            != plan_fingerprint,
                            CVTransformationPlanApproval.status != "SUPERSEDED",
                        )
                    )
                ).scalars().all()
                for previous in older:
                    previous.status = "SUPERSEDED"
                    previous.updated_at = now
            else:
                changed = (
                    row.status != status
                    or row.decisions != decisions
                    or row.guardrails_acknowledged != guardrails_acknowledged
                )
                if changed:
                    row.status = status
                    row.decisions = decisions
                    row.guardrails_acknowledged = guardrails_acknowledged
                    row.updated_at = now

            metric_recorded = await self._record_transformation_decision_metric(session, row)
            if created or changed or metric_recorded:
                await session.commit()
            return self._transformation_approval_to_dict(row), created

    async def _record_transformation_decision_metric(
        self,
        session: AsyncSession,
        approval: CVTransformationPlanApproval,
        *,
        job_row: Job | None = None,
    ) -> bool:
        """Record exactly one event once at least one explicit decision exists."""

        if not approval.decisions:
            return False
        job_row = job_row or await session.get(Job, approval.job_id)
        if job_row is None:
            raise ValueError("Job not found")
        result = await record_metric_event(
            session,
            MetricEventInput(
                event_id=str(uuid4()),
                operation_key=f"transformation_plan_decision:{approval.approval_id}",
                event_type=MetricEventType.TRANSFORMATION_PLAN_DECIDED,
                entity_type=MetricEntityType.JOB,
                entity_id=approval.job_id,
                lifecycle_id=job_row.lifecycle_token,
                source=MetricEventSource.USER,
            ),
        )
        return result.status == RecordEventStatus.RECORDED

    # -- Approved-plan generation operations -------------------------------

    async def claim_transformation_generation(
        self, *, generation_input_fingerprint: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create the sole GENERATING row for an exact input."""

        now = _now()
        generation_id = str(uuid4())
        async with self._session() as session:
            inserted_id = await session.scalar(
                sqlite_insert(CVTransformationGeneration)
                .values(
                    generation_id=generation_id,
                    generation_input_fingerprint=generation_input_fingerprint,
                    status="GENERATING",
                    attempt_count=1,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
                .on_conflict_do_nothing(
                    index_elements=["generation_input_fingerprint"]
                )
                .returning(CVTransformationGeneration.generation_id)
            )
            await session.commit()
            row = (
                await session.execute(
                    select(CVTransformationGeneration).where(
                        CVTransformationGeneration.generation_input_fingerprint
                        == generation_input_fingerprint
                    )
                )
            ).scalars().one()
            return self._transformation_generation_to_dict(row), inserted_id is not None

    async def retry_failed_transformation_generation(
        self, generation_id: str
    ) -> tuple[dict[str, Any], bool]:
        """Atomically let one retry caller move FAILED back to GENERATING."""

        now = _now()
        async with self._session() as session:
            result = await session.execute(
                update(CVTransformationGeneration)
                .where(
                    CVTransformationGeneration.generation_id == generation_id,
                    CVTransformationGeneration.status == "FAILED",
                )
                .values(
                    status="GENERATING",
                    failure_code=None,
                    draft_json=None,
                    provenance_json=None,
                    completed_at=None,
                    updated_at=now,
                    attempt_count=CVTransformationGeneration.attempt_count + 1,
                )
            )
            await session.commit()
            row = await session.get(CVTransformationGeneration, generation_id)
            if row is None:
                raise ValueError("Generation not found")
            return self._transformation_generation_to_dict(row), result.rowcount == 1

    async def recover_superseded_transformation_generation(
        self, generation_id: str
    ) -> tuple[dict[str, Any], bool]:
        """Atomically let one caller reclaim the exact SUPERSEDED input row."""

        now = _now()
        async with self._session() as session:
            result = await session.execute(
                update(CVTransformationGeneration)
                .where(
                    CVTransformationGeneration.generation_id == generation_id,
                    CVTransformationGeneration.status == "SUPERSEDED",
                )
                .values(
                    status="GENERATING",
                    failure_code=None,
                    draft_json=None,
                    provenance_json=None,
                    completed_at=None,
                    updated_at=now,
                    attempt_count=CVTransformationGeneration.attempt_count + 1,
                )
            )
            await session.commit()
            row = await session.get(CVTransformationGeneration, generation_id)
            if row is None:
                raise ValueError("Generation not found")
            return self._transformation_generation_to_dict(row), result.rowcount == 1

    async def finish_transformation_generation(
        self,
        generation_id: str,
        *,
        status: str,
        draft_resume: dict[str, Any] | None = None,
        provenance: list[dict[str, Any]] | None = None,
        failure_code: str | None = None,
        expected_attempt_count: int | None = None,
        expected_generation_input_fingerprint: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Compare-and-set a claimed attempt without storing a partial draft."""

        now = _now()
        async with self._session() as session:
            conditions = [
                CVTransformationGeneration.generation_id == generation_id,
                CVTransformationGeneration.status == "GENERATING",
            ]
            if expected_attempt_count is not None:
                conditions.append(
                    CVTransformationGeneration.attempt_count == expected_attempt_count
                )
            if expected_generation_input_fingerprint is not None:
                conditions.append(
                    CVTransformationGeneration.generation_input_fingerprint
                    == expected_generation_input_fingerprint
                )
            result = await session.execute(
                update(CVTransformationGeneration)
                .where(*conditions)
                .values(
                    status=status,
                    draft_json=draft_resume if status == "GENERATED" else None,
                    provenance_json=provenance if status == "GENERATED" else None,
                    failure_code=failure_code,
                    completed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            row = await session.get(CVTransformationGeneration, generation_id)
            if row is None:
                raise ValueError("Generation not found")
            return self._transformation_generation_to_dict(row), result.rowcount == 1

    async def get_transformation_generation(
        self,
        *,
        generation_id: str | None = None,
        job_id: str | None = None,
        resume_id: str | None = None,
        plan_fingerprint: str | None = None,
        generation_input_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._session() as session:
            stmt = select(CVTransformationGeneration)
            if generation_id is not None:
                stmt = stmt.where(CVTransformationGeneration.generation_id == generation_id)
            if job_id is not None:
                stmt = stmt.where(CVTransformationGeneration.job_id == job_id)
            if resume_id is not None:
                stmt = stmt.where(CVTransformationGeneration.resume_id == resume_id)
            if plan_fingerprint is not None:
                stmt = stmt.where(
                    CVTransformationGeneration.plan_fingerprint == plan_fingerprint
                )
            if generation_input_fingerprint is not None:
                stmt = stmt.where(
                    CVTransformationGeneration.generation_input_fingerprint
                    == generation_input_fingerprint
                )
            stmt = stmt.order_by(CVTransformationGeneration.updated_at.desc())
            row = (await session.execute(stmt)).scalars().first()
            return self._transformation_generation_to_dict(row) if row else None

    # -- Application (tracker) operations -----------------------------------

    async def _next_position(self, session: AsyncSession, status: str) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Application)
            .where(Application.status == status)
        )
        return int(result.scalar() or 0)

    async def _renumber(self, session: AsyncSession, status: str) -> None:
        """Renumber a column's positions to a contiguous 0..n-1 sequence."""
        result = await session.execute(
            select(Application)
            .where(Application.status == status)
            .order_by(Application.position, Application.created_at)
        )
        for index, row in enumerate(result.scalars().all()):
            if row.position != index:
                row.position = index
    async def create_application(
        self,
        job_id: str,
        resume_id: str,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> dict[str, Any]:
        """Create a tracker card, deduped on (job_id, resume_id).

        If a card for the same job+resume already exists it is returned as-is
        (survives double-submit / retried confirms).
        """
        async with self._session() as session:
            existing = await session.execute(
                select(Application).where(
                    Application.job_id == job_id, Application.resume_id == resume_id
                )
            )
            found = existing.scalars().first()
            if found is not None:
                return self._application_to_dict(found)

            now = _now()
            if applied_at is None and status != "saved":
                applied_at = now
            position = await self._next_position(session, status)
            lifecycle_token = str(uuid4())
            row = Application(
                application_id=str(uuid4()),
                job_id=job_id,
                resume_id=resume_id,
                master_resume_id=master_resume_id,
                status=status,
                company=company,
                role=role,
                applied_at=applied_at,
                notes=notes,
                position=position,
                status_version=0,
                lifecycle_token=lifecycle_token,
                created_at=now,
                updated_at=now,
            )
            try:
                session.add(row)
                await session.flush()

                # Record MetricEvent
                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"create:application:{row.application_id}:lc:{lifecycle_token}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.APPLICATION,
                    entity_id=row.application_id,
                    lifecycle_id=lifecycle_token,
                    from_state=None,
                    to_state=status,
                    source=source,
                )
                await record_metric_event(session, event_input)
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                async with self._session() as check_session:
                    dup = await check_session.execute(
                        select(Application).where(
                            Application.job_id == job_id,
                            Application.resume_id == resume_id,
                        )
                    )
                    found = dup.scalars().first()
                    if found is not None:
                        logger.debug(
                            "Deduped concurrent application create for job=%s resume=%s",
                            job_id,
                            resume_id,
                        )
                        return self._application_to_dict(found)
                raise e
            return self._application_to_dict(row)

    async def list_applications(self, status: str | None = None) -> list[dict[str, Any]]:
        """List applications ordered by (status, position)."""
        async with self._session() as session:
            stmt = select(Application)
            if status is not None:
                stmt = stmt.where(Application.status == status)
            stmt = stmt.order_by(Application.status, Application.position)
            result = await session.execute(stmt)
            return [self._application_to_dict(row) for row in result.scalars().all()]

    async def get_application(self, application_id: str) -> dict[str, Any] | None:
        """Get an application by ID."""
        async with self._session() as session:
            row = await session.get(Application, application_id)
            return self._application_to_dict(row) if row else None

    async def update_application(
        self,
        application_id: str,
        updates: dict[str, Any],
        source: MetricEventSource = MetricEventSource.USER,
    ) -> dict[str, Any] | None:
        """Update an application; renumber columns when status/position change.

        ``position`` is interpreted as the desired index within the (possibly
        new) ``status`` column; siblings are renumbered server-side so the
        column stays a contiguous 0..n-1 sequence.
        """
        async with self._session() as session:
            row = await session.get(Application, application_id)
            if row is None:
                return None

            old_status = row.status
            old_version = row.status_version
            lifecycle_token = row.lifecycle_token
            new_status = updates.get("status", old_status)
            target_position = updates.get("position", None)

            status_changed = ("status" in updates) and (old_status != new_status)

            if status_changed:
                from sqlalchemy import update as sqla_update
                upd_values = {
                    "status": new_status,
                    "status_version": old_version + 1,
                    "updated_at": _now()
                }
                for key in ("company", "role", "applied_at", "notes"):
                    if key in updates:
                        upd_values[key] = updates[key]

                stmt = (
                    sqla_update(Application)
                    .where(
                        Application.application_id == application_id,
                        Application.status_version == old_version
                    )
                    .values(**upd_values)
                )
                res = await session.execute(stmt)
                if res.rowcount != 1:
                    await session.rollback()
                    raise ApplicationStatusConflictError(
                        f"Conflict updating status for application {application_id}. "
                        f"Expected version {old_version}."
                    )

                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"status_change:application:{application_id}:ver:{old_version}:from:{old_status}:to:{new_status}",
                    event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
                    entity_type=MetricEntityType.APPLICATION,
                    entity_id=application_id,
                    lifecycle_id=lifecycle_token,
                    from_state=old_status,
                    to_state=new_status,
                    source=source,
                )
                await record_metric_event(session, event_input)
                await session.refresh(row)
            else:
                for key in ("company", "role", "applied_at", "notes"):
                    if key in updates:
                        setattr(row, key, updates[key])

            moved = status_changed or "position" in updates
            if moved:
                row.status = new_status
                row.position = 10_000_000
                await session.flush()
                if old_status != new_status:
                    await self._renumber(session, old_status)
                siblings = await session.execute(
                    select(Application)
                    .where(
                        Application.status == new_status,
                        Application.application_id != application_id,
                    )
                    .order_by(Application.position, Application.created_at)
                )
                ordered = list(siblings.scalars().all())
                if target_position is None or target_position > len(ordered):
                    target_position = len(ordered)
                if target_position < 0:
                    target_position = 0
                ordered.insert(target_position, row)
                for index, item in enumerate(ordered):
                    item.position = index

            row.updated_at = _now()
            await session.commit()
            return self._application_to_dict(row)

    async def bulk_update_applications(
        self,
        application_ids: list[str],
        status: str,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> int:
        """Move many applications to the end of ``status``. Returns count moved."""
        # Deduplicate application_ids while preserving order
        unique_ids = []
        seen = set()
        for app_id in application_ids:
            if app_id not in seen:
                seen.add(app_id)
                unique_ids.append(app_id)
        application_ids = unique_ids

        moved = 0
        async with self._session() as session:
            apps_to_update = []
            for application_id in application_ids:
                row = await session.get(Application, application_id)
                if row is not None:
                    apps_to_update.append(row)

            affected_old: set[str] = set()
            from sqlalchemy import update as sqla_update

            for row in apps_to_update:
                old_status = row.status
                old_version = row.status_version
                lifecycle_token = row.lifecycle_token

                if old_status != status:
                    stmt = (
                        sqla_update(Application)
                        .where(
                            Application.application_id == row.application_id,
                            Application.status_version == old_version
                        )
                        .values(
                            status=status,
                            status_version=old_version + 1,
                            updated_at=_now()
                        )
                    )
                    res = await session.execute(stmt)
                    if res.rowcount != 1:
                        conflict_id = row.application_id
                        await session.rollback()
                        raise ApplicationStatusConflictError(
                            f"Conflict updating status in bulk for application {conflict_id}."
                        )

                    event_input = MetricEventInput(
                        event_id=str(uuid4()),
                        operation_key=f"status_change:application:{row.application_id}:ver:{old_version}:from:{old_status}:to:{status}",
                        event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
                        entity_type=MetricEntityType.APPLICATION,
                        entity_id=row.application_id,
                        lifecycle_id=lifecycle_token,
                        from_state=old_status,
                        to_state=status,
                        source=source,
                    )
                    await record_metric_event(session, event_input)

                    affected_old.add(old_status)
                    row.status = status
                    row.position = 20_000_000 + moved
                    row.updated_at = _now()
                    moved += 1

            await session.flush()
            for old_status in affected_old - {status}:
                await self._renumber(session, old_status)
            if moved > 0:
                await self._renumber(session, status)
            await session.commit()
        return moved

    async def delete_application(
        self,
        application_id: str,
        source: MetricEventSource = MetricEventSource.USER,
    ) -> bool:
        """Delete an application; renumber its column."""
        async with self._session() as session:
            row = await session.get(Application, application_id)
            if row is None:
                return False
            status = row.status
            lifecycle_token = row.lifecycle_token

            event_input = MetricEventInput(
                event_id=str(uuid4()),
                operation_key=f"delete:application:{application_id}:lc:{lifecycle_token}",
                event_type=MetricEventType.ENTITY_DELETED,
                entity_type=MetricEntityType.APPLICATION,
                entity_id=application_id,
                lifecycle_id=lifecycle_token,
                from_state=None,
                to_state=None,
                source=source,
            )
            await record_metric_event(session, event_input)

            await session.delete(row)
            await session.flush()
            await self._renumber(session, status)
            await session.commit()
            return True

    async def bulk_delete_applications(
        self,
        application_ids: list[str],
        source: MetricEventSource = MetricEventSource.USER,
    ) -> int:
        """Delete many applications; renumber affected columns. Returns count."""
        # Deduplicate application_ids while preserving order
        unique_ids = []
        seen = set()
        for app_id in application_ids:
            if app_id not in seen:
                seen.add(app_id)
                unique_ids.append(app_id)
        application_ids = unique_ids

        deleted = 0
        async with self._session() as session:
            affected: set[str] = set()
            for application_id in application_ids:
                row = await session.get(Application, application_id)
                if row is None:
                    continue
                status = row.status
                lifecycle_token = row.lifecycle_token

                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"delete:application:{application_id}:lc:{lifecycle_token}",
                    event_type=MetricEventType.ENTITY_DELETED,
                    entity_type=MetricEntityType.APPLICATION,
                    entity_id=application_id,
                    lifecycle_id=lifecycle_token,
                    from_state=None,
                    to_state=None,
                    source=source,
                )
                await record_metric_event(session, event_input)

                affected.add(status)
                await session.delete(row)
                deleted += 1
            await session.flush()
            for status in affected:
                await self._renumber(session, status)
            await session.commit()
        return deleted

    # -- Encrypted API key store (sync; read on the LLM hot path) -----------

    def get_api_key_ciphertexts(self) -> dict[str, str]:
        """Return ``{provider: ciphertext}`` for all stored keys (sync)."""
        with self._sync() as session:
            rows = session.execute(select(ApiKey)).scalars().all()
            return {row.provider: row.ciphertext for row in rows}

    def set_api_key_ciphertext(self, provider: str, ciphertext: str) -> None:
        """Upsert one provider's ciphertext (sync)."""
        with self._sync() as session:
            row = session.get(ApiKey, provider)
            if row is None:
                session.add(
                    ApiKey(provider=provider, ciphertext=ciphertext, updated_at=_now())
                )
            else:
                row.ciphertext = ciphertext
                row.updated_at = _now()
            session.commit()

    def delete_api_key(self, provider: str) -> None:
        """Delete one provider's key (sync)."""
        with self._sync() as session:
            row = session.get(ApiKey, provider)
            if row is not None:
                session.delete(row)
                session.commit()

    def clear_api_keys(self) -> None:
        """Delete all stored keys (sync)."""
        with self._sync() as session:
            session.execute(delete(ApiKey))
            session.commit()

    def replace_api_keys(self, ciphertexts: dict[str, str]) -> None:
        """Atomically replace the whole key store (clear + insert in one txn).

        A single transaction means a failure mid-write can't leave the store
        half-cleared and wipe a user's previously saved keys.
        """
        with self._sync() as session:
            session.execute(delete(ApiKey))
            now = _now()
            for provider, ciphertext in ciphertexts.items():
                if ciphertext:
                    session.add(
                        ApiKey(provider=provider, ciphertext=ciphertext, updated_at=now)
                    )
            session.commit()

    # -- Stats / maintenance ------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """Get database statistics."""
        async with self._session() as session:
            resumes = await session.scalar(select(func.count()).select_from(Resume))
            jobs = await session.scalar(select(func.count()).select_from(Job))
            improvements = await session.scalar(
                select(func.count()).select_from(Improvement)
            )
            master = await session.execute(
                select(Resume.resume_id).where(Resume.is_master.is_(True)).limit(1)
            )
            return {
                "total_resumes": int(resumes or 0),
                "total_jobs": int(jobs or 0),
                "total_improvements": int(improvements or 0),
                "has_master_resume": master.first() is not None,
            }

    async def reset_database(self) -> None:
        """Reset by truncating user-document tables and clearing uploads.

        Clears resumes/jobs/improvements **and** tracker applications (leaving
        orphaned cards after a full data reset would be a bug). Encrypted
        ``api_keys`` are preserved — matching the pre-existing behavior where a
        reset never wiped the user's stored credentials.
        """
        async with self._session() as session:
            await session.execute(delete(CVTransformationGeneration))
            await session.execute(delete(CVTransformationPlanApproval))
            await session.execute(delete(Application))
            await session.execute(delete(Improvement))
            await session.execute(delete(Job))
            await session.execute(delete(Resume))
            await session.commit()

        uploads_dir = settings.data_dir / "uploads"
        if uploads_dir.exists():
            shutil.rmtree(uploads_dir)
            uploads_dir.mkdir(parents=True, exist_ok=True)


# Global database instance
db = Database()
