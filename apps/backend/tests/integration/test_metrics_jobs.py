import pytest
from unittest.mock import patch
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError, OperationalError, DBAPIError
from httpx import ASGITransport, AsyncClient
from uuid import uuid4

from app.models import Job, Improvement, MetricEvent
from app.services.project_metrics import (
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    MetricType,
    MetricAvailability,
    MetricEventInput,
    RecordEventStatus,
    record_metric_event,
    initialize_metric_tracking_state,
    build_metrics_report,
)
from app.main import app as fastapi_app


@pytest.fixture
async def initialized_db(isolated_db):
    from app.services.project_metrics_bootstrap import bootstrap_metrics
    await bootstrap_metrics(isolated_db)
    return isolated_db


async def _force_create_job(db, job_id: str, lifecycle_token: str, content: str = "JD"):
    """Helper to force insert a Job with a specific job_id and lifecycle_token."""
    async with db._session() as session:
        from app.database import _now
        job = Job(
            job_id=job_id,
            content=content,
            resume_id=None,
            created_at=_now(),
            metadata_json={},
            lifecycle_token=lifecycle_token
        )
        session.add(job)
        await session.flush()

        event_input = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"create:job:{job_id}:lc:{lifecycle_token}",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id=job_id,
            lifecycle_id=lifecycle_token,
            from_state=None,
            to_state=None,
            source=MetricEventSource.USER,
        )
        await record_metric_event(session, event_input)
        await session.commit()


# --- CREATE ---

async def test_manual_create_emits_entity_created_source_user(initialized_db):
    """1. Verify manual create_job emits ENTITY_CREATED with source USER."""
    job = await initialized_db.create_job(content="JD content", source=MetricEventSource.USER)

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == MetricEventType.ENTITY_CREATED.value
        assert evt.entity_type == MetricEntityType.JOB.value
        assert evt.entity_id == job["job_id"]
        assert evt.source == MetricEventSource.USER.value


async def test_system_create_emits_source_system_db_interface(initialized_db):
    """2. Verify database interface supports system create_job emitting ENTITY_CREATED with source SYSTEM."""
    job = await initialized_db.create_job(content="JD content", source=MetricEventSource.SYSTEM)

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        evt = events[0]
        assert evt.source == MetricEventSource.SYSTEM.value


async def test_event_contains_lifecycle_token(initialized_db):
    """3. Verify event contains the generated lifecycle_token."""
    job = await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        row = (await session.execute(select(Job).where(Job.job_id == job["job_id"]))).scalar_one()
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].lifecycle_id == row.lifecycle_token


async def test_event_contains_job_id(initialized_db):
    """4. Verify event contains the correct job_id."""
    job = await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].entity_id == job["job_id"]


async def test_create_and_event_are_atomic(initialized_db):
    """5. Verify create_job and event are committed atomically."""
    job = await initialized_db.create_job(content="JD content")

    async with initialized_db._session() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 1
        row = jobs[0]
        assert row.job_id == job["job_id"]

        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == MetricEventType.ENTITY_CREATED.value
        assert evt.entity_id == row.job_id
        assert evt.lifecycle_id == row.lifecycle_token


async def test_event_error_rolls_back_job(initialized_db):
    """6. Verify exception in event recording rolls back Job creation."""
    async def mock_record_event(session, event_input):
        raise RuntimeError("simulated event error")

    with patch("app.database.record_metric_event", mock_record_event):
        with pytest.raises(RuntimeError, match="simulated event error"):
            await initialized_db.create_job(content="JD content")

    # Verify no job and no event exist
    async with initialized_db._session() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 0
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 0


async def test_create_job_collision_primary_key(initialized_db):
    """7 & 8. Verify that create_job returns the existing record upon collision of the same job_id, without leaving an orphan event."""
    job_uuid = uuid4()
    lc_uuid1 = uuid4()
    lc_uuid2 = uuid4()
    evt_uuid1 = uuid4()
    evt_uuid2 = uuid4()

    uuid_returns = [job_uuid, lc_uuid1, evt_uuid1, job_uuid, lc_uuid2, evt_uuid2]
    call_idx = 0

    def mock_uuid4():
        nonlocal call_idx
        res = uuid_returns[call_idx]
        call_idx += 1
        return res

    with patch("app.database.uuid4", mock_uuid4):
        # First call succeeds
        job1 = await initialized_db.create_job(content="JD 1")
        assert job1["job_id"] == str(job_uuid)

        # Second call conflicts on job_id (already exists in DB)
        job2 = await initialized_db.create_job(content="JD 2")
        assert job2["job_id"] == str(job_uuid)

    # Check database state: exactly 1 Job and 1 event
    async with initialized_db._session() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].content == "JD 1"

        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].operation_key == f"create:job:{job_uuid}:lc:{lc_uuid1}"


async def test_create_job_other_integrity_error_is_reraised(initialized_db):
    """Verify that an IntegrityError not related to an existing job_id is re-raised."""
    async def mock_flush(self, *args, **kwargs):
        raise IntegrityError("mock foreign key conflict", params=None, orig=None)

    with patch.object(AsyncSession, "flush", mock_flush):
        with pytest.raises(IntegrityError, match="mock foreign key conflict"):
            await initialized_db.create_job(content="JD")


async def test_create_job_existing_id_does_not_mask_lifecycle_token_integrity_error(initialized_db):
    """Verify that create_job does not mask IntegrityErrors other than UNIQUE constraint failed: jobs.job_id."""
    job_uuid = uuid4()
    lc_uuid = uuid4()
    evt_uuid = uuid4()

    uuid_returns = [job_uuid, lc_uuid, evt_uuid]
    call_idx = 0

    def mock_uuid4():
        nonlocal call_idx
        res = uuid_returns[call_idx]
        call_idx += 1
        return res

    async def mock_flush(self, *args, **kwargs):
        raise IntegrityError("mock UNIQUE constraint failed: jobs.lifecycle_token", params=None, orig=None)

    with patch("app.database.uuid4", mock_uuid4):
        with patch.object(AsyncSession, "flush", mock_flush):
            with pytest.raises(IntegrityError, match="mock UNIQUE constraint failed: jobs.lifecycle_token"):
                await initialized_db.create_job(content="JD 2")


async def test_update_does_not_change_lifecycle_token(initialized_db):
    """9. Verify updating job content does not alter lifecycle_token."""
    job = await initialized_db.create_job(content="JD content")

    async with initialized_db._session() as session:
        row1 = (await session.execute(select(Job).where(Job.job_id == job["job_id"]))).scalar_one()
        tok1 = row1.lifecycle_token

    await initialized_db.update_job(job["job_id"], {"content": "updated content"})

    async with initialized_db._session() as session:
        row2 = (await session.execute(select(Job).where(Job.job_id == job["job_id"]))).scalar_one()
        assert row2.lifecycle_token == tok1


async def test_update_does_not_emit_create_event(initialized_db):
    """10. Verify updating job does not write another ENTITY_CREATED event."""
    job = await initialized_db.create_job(content="JD")

    await initialized_db.update_job(job["job_id"], {"content": "updated content"})

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == MetricEventType.ENTITY_CREATED.value


# --- DELETE ---

async def test_delete_emits_entity_deleted(initialized_db):
    """11. Verify delete_job emits ENTITY_DELETED event."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].entity_type == MetricEntityType.JOB.value
        assert events[0].entity_id == job["job_id"]


async def test_delete_event_contains_correct_lifecycle(initialized_db):
    """12. Verify delete event contains correct lifecycle_token."""
    job = await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        row = (await session.execute(select(Job).where(Job.job_id == job["job_id"]))).scalar_one()
        tok = row.lifecycle_token

    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalar_one()
        assert evt.lifecycle_id == tok


async def test_delete_is_atomic(initialized_db):
    """13 & 14. Verify delete and event logging are atomic (failure rolls back delete)."""
    job = await initialized_db.create_job(content="JD")

    async def mock_record_event(session, event_input):
        raise RuntimeError("simulated delete event error")

    with patch("app.database.record_metric_event", mock_record_event):
        with pytest.raises(RuntimeError, match="simulated delete event error"):
            await initialized_db.delete_job(job["job_id"])

    # Verify job still exists in DB
    async with initialized_db._session() as session:
        row = await session.get(Job, job["job_id"])
        assert row is not None


async def test_delete_nonexistent_job_no_event(initialized_db):
    """15. Verify deleting nonexistent job doesn't log any delete events."""
    res = await initialized_db.delete_job("nonexistent-id")
    assert res is False

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_delete_does_not_decrease_lifetime(initialized_db):
    """16. Verify deleting job does not decrease job_offers_saved lifetime metric."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    assert mv.lifetime.value == 1


async def test_delete_decreases_current(initialized_db):
    """17. Verify deleting job decreases job_offers_saved current metric."""
    job = await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        report1 = await build_metrics_report(session)
    mv1 = next(m for m in report1.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    assert mv1.current.value == 1

    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        report2 = await build_metrics_report(session)
    mv2 = next(m for m in report2.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    assert mv2.current.value == 0


async def test_recreate_same_job_id_new_lifecycle(initialized_db):
    """18. Verify recreation of the same job_id generates a brand new lifecycle_token."""
    jid = "constant-job-id"
    tok1 = "lifecycle-first"
    tok2 = "lifecycle-second"

    await _force_create_job(initialized_db, jid, tok1)
    await initialized_db.delete_job(jid)

    # Recreate with identical job_id and new lifecycle_token
    await _force_create_job(initialized_db, jid, tok2)

    async with initialized_db._session() as session:
        row2 = await session.get(Job, jid)
        assert row2.lifecycle_token == tok2
        assert row2.lifecycle_token != tok1


async def test_recreate_after_delete_can_increase_lifetime_again(initialized_db):
    """19. Verify recreation of job after deletion increases lifetime again."""
    job1 = await initialized_db.create_job(content="JD")
    await initialized_db.delete_job(job1["job_id"])

    await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    assert mv.lifetime.value == 2


# --- ANALYSIS ---

async def test_first_completed_analysis_emits_job_analyzed(initialized_db):
    """20. Verify first completed analysis emits JOB_ANALYZED event."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement(
        original_resume_id="r-orig",
        tailored_resume_id="r-tail",
        job_id=job["job_id"],
        improvements=[]
    )

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].entity_id == job["job_id"]


async def test_analysis_event_source_is_system(initialized_db):
    """21. Verify JOB_ANALYZED event has source SYSTEM."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalar_one()
        assert evt.source == MetricEventSource.SYSTEM.value


async def test_analysis_event_contains_lifecycle(initialized_db):
    """22. Verify JOB_ANALYZED event contains Job's lifecycle token."""
    job = await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        row = await session.get(Job, job["job_id"])
        tok = row.lifecycle_token

    await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalar_one()
        assert evt.lifecycle_id == tok


async def test_analysis_operation_key_format(initialized_db):
    """23. Verify operation_key format is analyze:job:{job_id}:lc:{lifecycle_token}."""
    job = await initialized_db.create_job(content="JD")

    async with initialized_db._session() as session:
        row = await session.get(Job, job["job_id"])
        tok = row.lifecycle_token

    await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalar_one()
        assert evt.operation_key == f"analyze:job:{job['job_id']}:lc:{tok}"


async def test_multiple_improvements_same_lifecycle_no_lifetime_increase(initialized_db):
    """24. Verify multiple improvements on the same Job lifecycle do not increase lifetime metric."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement("r-orig", "r-tail-1", job["job_id"], [])
    await initialized_db.create_improvement("r-orig", "r-tail-2", job["job_id"], [])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalars().all()
        assert len(events) == 1

        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.lifetime.value == 1


async def test_duplicate_job_analyzed_is_idempotent(initialized_db):
    """25. Verify two JOB_ANALYZED event writes with the same operation_key yield only one event, and the parent transaction remains usable."""
    job = await initialized_db.create_job(content="JD")
    jid = job["job_id"]

    async with initialized_db._session() as session:
        row = await session.get(Job, jid)
        tok = row.lifecycle_token

    async with initialized_db._session() as session:
        evt1 = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"analyze:job:{jid}:lc:{tok}",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.JOB,
            entity_id=jid,
            lifecycle_id=tok,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        evt2 = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"analyze:job:{jid}:lc:{tok}",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.JOB,
            entity_id=jid,
            lifecycle_id=tok,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )

        res1 = await record_metric_event(session, evt1)
        assert res1.status == RecordEventStatus.RECORDED

        res2 = await record_metric_event(session, evt2)
        assert res2.status == RecordEventStatus.DUPLICATE

        # Verify transaction remains usable and commit succeeds
        await session.commit()

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_improvement_and_job_analyzed_are_atomic(initialized_db):
    """26. Verify Improvement and event logging are atomic (failure rolls back both)."""
    job = await initialized_db.create_job(content="JD")

    async def mock_record_event(session, event_input):
        raise RuntimeError("simulated improvement event error")

    with patch("app.database.record_metric_event", mock_record_event):
        with pytest.raises(RuntimeError, match="simulated improvement event error"):
            await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])

    # Verify neither improvement nor event exist
    async with initialized_db._session() as session:
        imps = (await session.execute(select(Improvement))).scalars().all()
        assert len(imps) == 0
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_improvement_commit_error_rolls_back_improvement_and_job_analyzed(initialized_db):
    """27 & 28. Verify that a commit error rolls back both the Improvement and the JOB_ANALYZED event."""
    job = await initialized_db.create_job(content="JD")

    async def mock_commit(self):
        raise RuntimeError("simulated commit failure")

    with patch.object(AsyncSession, "commit", mock_commit):
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])

    # Verify neither improvement nor event exist in DB
    async with initialized_db._session() as session:
        imps = (await session.execute(select(Improvement))).scalars().all()
        assert len(imps) == 0
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_create_improvement_for_missing_job_does_not_emit_job_analyzed(initialized_db):
    """Verify that create_improvement for a missing job creates the Improvement but does not emit JOB_ANALYZED."""
    imp = await initialized_db.create_improvement(
        original_resume_id="r-orig",
        tailored_resume_id="r-tail",
        job_id="missing-job-id",
        improvements=[]
    )
    assert imp is not None
    assert imp["job_id"] == "missing-job-id"

    # Check that improvement was written, but no event was emitted
    async with initialized_db._session() as session:
        row = await session.get(Improvement, imp["request_id"])
        assert row is not None

        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_delete_job_does_not_decrease_lifetime_analyzed(initialized_db):
    """29. Verify deleting job does not decrease job_offers_analyzed lifetime metric."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])
    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.lifetime.value == 1


async def test_delete_job_decreases_current_analyzed(initialized_db):
    """30. Verify deleting job decreases job_offers_analyzed current metric."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])

    async with initialized_db._session() as session:
        report1 = await build_metrics_report(session)
    mv1 = next(m for m in report1.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv1.current.value == 1

    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        report2 = await build_metrics_report(session)
    mv2 = next(m for m in report2.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv2.current.value == 0


async def test_recreate_job_id_new_lifecycle_can_be_analyzed_again(initialized_db):
    """31. Verify recreation with new lifecycle allows counting another analysis in lifetime."""
    jid = "recreate-analysis-job"
    tok1 = "lc-ana-1"
    tok2 = "lc-ana-2"

    await _force_create_job(initialized_db, jid, tok1)
    await initialized_db.create_improvement("r-orig", "r-tail-1", jid, [])
    await initialized_db.delete_job(jid)

    # Recreate with identical job_id and new lifecycle_token
    await _force_create_job(initialized_db, jid, tok2)
    await initialized_db.create_improvement("r-orig", "r-tail-2", jid, [])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.lifetime.value == 2
    assert mv.current.value == 1
    assert mv.peak.value == 1


async def test_recreated_same_job_id_without_new_analysis_is_not_current_analyzed(initialized_db):
    """Verify that recreated job_id without a new analysis does not count towards current analyzed."""
    jid = "recreate-no-ana-job"
    tok1 = "lc-no-ana-1"
    tok2 = "lc-no-ana-2"

    await _force_create_job(initialized_db, jid, tok1)
    await initialized_db.create_improvement("r-orig", "r-tail-1", jid, [])
    await initialized_db.delete_job(jid)

    # Recreate but do NOT analyze
    await _force_create_job(initialized_db, jid, tok2)

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 0


async def test_recreated_same_job_id_after_new_analysis_is_current_analyzed(initialized_db):
    """Verify that recreated job_id after a new analysis counts towards current analyzed with increased lifetime."""
    jid = "recreate-yes-ana-job"
    tok1 = "lc-yes-ana-1"
    tok2 = "lc-yes-ana-2"

    await _force_create_job(initialized_db, jid, tok1)
    await initialized_db.create_improvement("r-orig", "r-tail-1", jid, [])
    await initialized_db.delete_job(jid)

    # Recreate and analyze
    await _force_create_job(initialized_db, jid, tok2)
    await initialized_db.create_improvement("r-orig", "r-tail-2", jid, [])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 1
    assert mv.lifetime.value == 2


# --- REPORT ---

async def test_saved_current_comes_from_jobs_table(initialized_db):
    """32. Verify job_offers_saved current is counted from jobs table."""
    await initialized_db.create_job(content="JD 1")
    await initialized_db.create_job(content="JD 2")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    assert mv.current.value == 2


async def test_analyzed_current_counts_unique_existing_jobs(initialized_db):
    """33. Verify job_offers_analyzed current counts unique existing analyzed jobs."""
    job1 = await initialized_db.create_job(content="JD 1")
    job2 = await initialized_db.create_job(content="JD 2")
    await initialized_db.create_improvement("r-orig", "r-tail-1", job1["job_id"], [])
    await initialized_db.create_improvement("r-orig", "r-tail-2", job2["job_id"], [])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 2


async def test_multiple_improvements_same_job_gives_current_1(initialized_db):
    """34. Verify multiple improvements for the same Job yields current analyzed count of 1."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement("r-orig", "r-tail-1", job["job_id"], [])
    await initialized_db.create_improvement("r-orig", "r-tail-2", job["job_id"], [])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 1


async def test_deleted_job_not_counted_as_current_analyzed(initialized_db):
    """35. Verify deleted job is not counted as current analyzed."""
    job = await initialized_db.create_job(content="JD")
    await initialized_db.create_improvement("r-orig", "r-tail", job["job_id"], [])
    await initialized_db.delete_job(job["job_id"])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 0


async def test_saved_lifetime_comes_from_events(initialized_db):
    """36. Verify job_offers_saved lifetime is counted from metric events."""
    # Write a fake manual metric event
    async with initialized_db._session() as session:
        event = MetricEventInput(
            event_id="evt-saved-fake",
            operation_key="create:job:fake:lc:fake",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="fake-id",
            lifecycle_id="fake-lc",
            from_state=None,
            to_state=None,
            source=MetricEventSource.USER,
        )
        await record_metric_event(session, event)
        await session.commit()

        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    # 0 in jobs table, but 1 in events -> lifetime should be 1
    assert mv.lifetime.value == 1
    assert mv.current.value == 0


async def test_analyzed_lifetime_comes_from_events(initialized_db):
    """37. Verify job_offers_analyzed lifetime is counted from metric events."""
    async with initialized_db._session() as session:
        event = MetricEventInput(
            event_id="evt-analyzed-fake",
            operation_key="analyze:job:fake:lc:fake",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.JOB,
            entity_id="fake-id",
            lifecycle_id="fake-lc",
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        await record_metric_event(session, event)
        await session.commit()

        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.lifetime.value == 1
    assert mv.current.value == 0


async def test_resume_current_is_supported(initialized_db):
    """38. Verify resumes_generated current is AVAILABLE."""
    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.availability == MetricAvailability.AVAILABLE
    assert mv.current.value == 0


# --- API LEAKAGE ---

async def test_lifecycle_token_not_returned_in_post_job(initialized_db):
    """39. Verify lifecycle_token is not returned in POST /jobs/upload response."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/jobs/upload",
            json={"job_descriptions": ["Engineer role description"]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "lifecycle_token" not in body


async def test_lifecycle_token_not_returned_in_get_job(initialized_db):
    """40. Verify lifecycle_token is not returned in GET /jobs/{job_id} response."""
    job = await initialized_db.create_job(content="JD")
    jid = job["job_id"]

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/jobs/{jid}")
        assert resp.status_code == 200
        body = resp.json()
        assert "lifecycle_token" not in body


async def test_job_to_dict_excludes_lifecycle_token(initialized_db):
    """41. Verify Database._job_to_dict and get_job output exclude lifecycle_token."""
    job = await initialized_db.create_job(content="JD")
    jid = job["job_id"]

    # Verify get_job output dictionary
    dict_out = await initialized_db.get_job(jid)
    assert "lifecycle_token" not in dict_out

    # Verify directly via _job_to_dict ORM method
    async with initialized_db._session() as session:
        row = await session.get(Job, jid)
        raw_dict = initialized_db._job_to_dict(row)
        assert "lifecycle_token" not in raw_dict


# --- AUDIT V2 ADDITIONAL TESTS ---

async def test_application_failure_cleanup_emits_job_deleted_source_system(initialized_db):
    """Verify that a failure in create_application triggers a job cleanup with source SYSTEM, sharing the same lifecycle token."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def mock_create_application(*args, **kwargs):
            raise RuntimeError("simulated application save error")

        with patch("app.database.Database.create_application", mock_create_application):
            resp = await client.post(
                "/api/v1/applications",
                json={
                    "resume_id": "res-cleanup-test",
                    "job_description": "Clean me up description",
                    "status": "saved",
                    "company": "Acme",
                    "role": "Engineer"
                }
            )
            assert resp.status_code == 500

    # Verify no job remains in the DB
    async with initialized_db._session() as session:
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 0

        # Verify events stream: exactly one ENTITY_CREATED (source USER) and one ENTITY_DELETED (source SYSTEM)
        events = (await session.execute(select(MetricEvent).order_by(MetricEvent.sequence_id))).scalars().all()
        # Filter for JOB entity events to isolate from initialization/other events
        job_events = [e for e in events if e.entity_type == MetricEntityType.JOB.value]
        assert len(job_events) == 2

        evt_created = job_events[0]
        assert evt_created.event_type == MetricEventType.ENTITY_CREATED.value
        assert evt_created.source == MetricEventSource.USER.value

        evt_deleted = job_events[1]
        assert evt_deleted.event_type == MetricEventType.ENTITY_DELETED.value
        assert evt_deleted.source == MetricEventSource.SYSTEM.value

        # Verify both share the same lifecycle_id
        assert evt_created.lifecycle_id == evt_deleted.lifecycle_id


async def test_wrong_entity_id_job_analyzed_event_does_not_mark_job_as_current_analyzed(initialized_db):
    """Verify that a JOB_ANALYZED event with the correct lifecycle_token but wrong entity_id does not count as analyzed."""
    job = await initialized_db.create_job(content="JD")
    jid = job["job_id"]

    async with initialized_db._session() as session:
        row = await session.get(Job, jid)
        tok = row.lifecycle_token

    # Write a JOB_ANALYZED event with wrong entity_id
    async with initialized_db._session() as session:
        event = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"analyze:job:wrong-id:lc:{tok}",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.JOB,
            entity_id="wrong-id",
            lifecycle_id=tok,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        await record_metric_event(session, event)
        await session.commit()

        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 0


async def test_wrong_entity_id_event_does_not_block_real_job_analyzed_event(initialized_db):
    """Verify that a JOB_ANALYZED event with a wrong entity_id does not block creating a real JOB_ANALYZED event."""
    job = await initialized_db.create_job(content="JD")
    jid = job["job_id"]

    async with initialized_db._session() as session:
        row = await session.get(Job, jid)
        tok = row.lifecycle_token

    # 1. Write event with wrong entity_id
    async with initialized_db._session() as session:
        event = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"analyze:job:wrong-id:lc:{tok}",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.JOB,
            entity_id="wrong-id",
            lifecycle_id=tok,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        await record_metric_event(session, event)
        await session.commit()

    # 2. Call create_improvement (should succeed and record the real event because entity_id didn't match)
    await initialized_db.create_improvement("r-orig", "r-tail", jid, [])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert mv.current.value == 1
    assert mv.lifetime.value == 1
