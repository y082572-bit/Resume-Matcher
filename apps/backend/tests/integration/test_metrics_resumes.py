import pytest
import json
from unittest.mock import patch, AsyncMock
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from httpx import ASGITransport, AsyncClient
from uuid import uuid4
from types import SimpleNamespace

from app.models import Resume, MetricEvent, Job
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
from app.routers.resumes import _hash_improved_data
from app.main import app as fastapi_app


@pytest.fixture
async def initialized_db(isolated_db):
    async with isolated_db._session() as session:
        await initialize_metric_tracking_state(session, historical_complete=True)
        from app.database import _now
        master_resume = Resume(
            resume_id="master-1",
            content="Master content",
            content_type="md",
            is_master=True,
            parent_id=None,
            processing_status="completed",
            created_at=_now(),
            updated_at=_now(),
            lifecycle_token="lc-master-1"
        )
        session.add(master_resume)
        await session.commit()
    return isolated_db



def _mock_improver():
    improved_data = {
        "personalInfo": {"name": "Master JD", "email": "a@b.com", "phone": "123"},
        "summary": "Tailored summary",
        "workExperience": [],
        "education": [],
        "personalProjects": [],
        "additional": {"technicalSkills": [], "languages": [], "certificationsTraining": [], "awards": []}
    }
    refine_stats = SimpleNamespace(
        passes_completed=1,
        keywords_injected=1,
        ai_phrases_removed=[],
        alignment_violations_fixed=0,
        initial_match_percentage=50.0,
        final_match_percentage=80.0
    )

    p1 = patch("app.routers.resumes.extract_job_keywords", new_callable=AsyncMock, return_value=["keyword"])
    p2 = patch("app.routers.resumes.improve_resume", new_callable=AsyncMock, return_value=improved_data)
    p3 = patch("app.routers.resumes.refine_resume", new_callable=AsyncMock, return_value=(improved_data, refine_stats))
    p4 = patch("app.routers.resumes.generate_improvements", return_value=[])
    p5 = patch("app.routers.resumes.generate_cover_letter", new_callable=AsyncMock, return_value="cover letter")
    p6 = patch("app.routers.resumes.generate_outreach_message", new_callable=AsyncMock, return_value="outreach")
    p7 = patch("app.routers.resumes.generate_resume_title", new_callable=AsyncMock, return_value="title")
    p8 = patch("app.routers.resumes.generate_interview_prep", new_callable=AsyncMock, return_value=None)
    p9 = patch("app.routers.resumes._load_config", return_value={"enable_cover_letter": False, "enable_outreach_message": False, "enable_interview_prep": False})

    class MultiPatch:
        def __enter__(self):
            p1.start()
            p2.start()
            p3.start()
            p4.start()
            p5.start()
            p6.start()
            p7.start()
            p8.start()
            p9.start()
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            p9.stop()
            p8.stop()
            p7.stop()
            p6.stop()
            p5.stop()
            p4.stop()
            p3.stop()
            p2.stop()
            p1.stop()

    return MultiPatch()


async def _force_create_resume(db, resume_id: str, lifecycle_token: str, parent_id: str | None = None):
    """Helper to force insert a Resume with specific resume_id and lifecycle_token."""
    async with db._session() as session:
        from app.database import _now
        resume = Resume(
            resume_id=resume_id,
            content="Resume content",
            content_type="md",
            is_master=False,
            parent_id=parent_id,
            processing_status="completed",
            created_at=_now(),
            updated_at=_now(),
            lifecycle_token=lifecycle_token
        )
        session.add(resume)
        await session.flush()

        event_input = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"create:resume:{resume_id}:lc:{lifecycle_token}",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.RESUME,
            entity_id=resume_id,
            lifecycle_id=lifecycle_token,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM if parent_id is not None else MetricEventSource.USER,
        )
        await record_metric_event(session, event_input)

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
            await record_metric_event(session, gen_event_input)

        await session.commit()


# --- LIFECYCLE I CREATE ---

async def test_upload_emit_entity_created_source_user(initialized_db):
    """1. Verify uploading master CV emits ENTITY_CREATED with source USER."""
    resume = await initialized_db.create_resume(content="Resume content", source=MetricEventSource.USER)

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == MetricEventType.ENTITY_CREATED.value
        assert events[0].entity_type == MetricEntityType.RESUME.value
        assert events[0].entity_id == resume["resume_id"]
        assert events[0].source == MetricEventSource.USER.value


async def test_generated_resume_emit_entity_created_source_system(initialized_db):
    """2. Verify generated tailored resume emits ENTITY_CREATED with source SYSTEM."""
    resume = await initialized_db.create_resume(
        content="Resume content",
        parent_id="master-1",
        source=MetricEventSource.SYSTEM
    )

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_CREATED.value)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].source == MetricEventSource.SYSTEM.value


async def test_create_event_contains_resume_id(initialized_db):
    """3. Verify create event entity_id matches resume_id."""
    resume = await initialized_db.create_resume(content="Resume")

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].entity_id == resume["resume_id"]


async def test_create_event_contains_lifecycle_token(initialized_db):
    """4. Verify create event lifecycle_id matches generated lifecycle_token."""
    resume = await initialized_db.create_resume(content="Resume")

    async with initialized_db._session() as session:
        row = (await session.execute(select(Resume).where(Resume.resume_id == resume["resume_id"]))).scalar_one()
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].lifecycle_id == row.lifecycle_token


async def test_create_resume_and_event_are_atomic(initialized_db):
    """5. Verify the create_resume and event are committed atomically."""
    resume = await initialized_db.create_resume(content="Resume content")

    async with initialized_db._session() as session:
        resumes = (await session.execute(select(Resume))).scalars().all()
        r = next((x for x in resumes if x.resume_id == resume["resume_id"]), None)
        assert r is not None

        events = (await session.execute(select(MetricEvent).where(MetricEvent.entity_id == resume["resume_id"]))).scalars().all()
        assert len(events) == 1
        assert events[0].entity_id == resume["resume_id"]



async def test_two_new_resumes_have_different_lifecycles(initialized_db):
    """7. Verify two new resumes have different lifecycle tokens."""
    res1 = await initialized_db.create_resume(content="R1")
    res2 = await initialized_db.create_resume(content="R2")

    async with initialized_db._session() as session:
        row1 = (await session.execute(select(Resume).where(Resume.resume_id == res1["resume_id"]))).scalar_one()
        row2 = (await session.execute(select(Resume).where(Resume.resume_id == res2["resume_id"]))).scalar_one()
        assert row1.lifecycle_token != row2.lifecycle_token


async def test_update_does_not_change_lifecycle(initialized_db):
    """8. Verify updating metadata doesn't change lifecycle token."""
    resume = await initialized_db.create_resume(content="Resume content")

    async with initialized_db._session() as session:
        row1 = (await session.execute(select(Resume).where(Resume.resume_id == resume["resume_id"]))).scalar_one()
        tok1 = row1.lifecycle_token

    await initialized_db.update_resume(resume["resume_id"], {"content": "updated content"})

    async with initialized_db._session() as session:
        row2 = (await session.execute(select(Resume).where(Resume.resume_id == resume["resume_id"]))).scalar_one()
        assert row2.lifecycle_token == tok1


async def test_update_does_not_emit_create_event(initialized_db):
    """9. Verify updating metadata doesn't write new ENTITY_CREATED event."""
    resume = await initialized_db.create_resume(content="Resume")

    await initialized_db.update_resume(resume["resume_id"], {"content": "updated content"})

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == MetricEventType.ENTITY_CREATED.value


async def test_lifecycle_token_immutable_on_update_resume(initialized_db):
    """10. Verify update_resume rejects changing lifecycle_token."""
    resume = await initialized_db.create_resume(content="Resume")

    with pytest.raises(ValueError, match="lifecycle_token is immutable"):
        await initialized_db.update_resume(resume["resume_id"], {"lifecycle_token": "new-token"})


# --- UPLOAD ---

async def test_first_upload_does_not_emit_resume_generated(initialized_db):
    """11. Verify first master upload doesn't emit RESUME_GENERATED."""
    resume = await initialized_db.create_resume_atomic_master(content="Resume")

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == MetricEventType.ENTITY_CREATED.value


async def test_subsequent_upload_does_not_emit_resume_generated(initialized_db):
    """12. Verify subsequent master uploads don't emit RESUME_GENERATED."""
    await initialized_db.create_resume_atomic_master(content="R1")
    await initialized_db.create_resume_atomic_master(content="R2")

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        # Should only have 2 ENTITY_CREATED events
        assert len(events) == 2
        for e in events:
            assert e.event_type == MetricEventType.ENTITY_CREATED.value


async def test_non_master_upload_not_counted_as_generated(initialized_db):
    """13. Verify uploads that are non-master are not counted as generated."""
    # Force a non-master resume with parent_id=None
    async with initialized_db._session() as session:
        from app.database import _now
        r = Resume(
            resume_id="non-master-upload",
            content="Content",
            is_master=False,
            parent_id=None,
            processing_status="completed",
            created_at=_now(),
            updated_at=_now(),
            lifecycle_token="lc-non-master"
        )
        session.add(r)
        await session.commit()

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 0
    assert mv.lifetime.value == 0


async def test_set_master_resume_does_not_emit_resume_generated(initialized_db):
    """14. Verify set_master_resume doesn't emit RESUME_GENERATED."""
    res = await initialized_db.create_resume(content="Resume")
    await initialized_db.set_master_resume(res["resume_id"])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.RESUME_GENERATED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_upload_does_not_increase_generated_lifetime(initialized_db):
    """15. Verify upload doesn't increase resumes_generated lifetime."""
    await initialized_db.create_resume(content="Resume")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 0


async def test_upload_does_not_increase_generated_current(initialized_db):
    """16. Verify upload doesn't increase resumes_generated current."""
    await initialized_db.create_resume(content="Resume")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 0


# --- GENERATED ---

async def test_tailored_resume_emits_resume_generated(initialized_db):
    """17. Verify tailored resume creation emits RESUME_GENERATED event."""
    await initialized_db.create_resume(content="Tailored Content", parent_id="master-1")

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.RESUME_GENERATED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_resume_generated_has_source_system(initialized_db):
    """18. Verify RESUME_GENERATED event has source SYSTEM."""
    await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.RESUME_GENERATED.value)
        )).scalar_one()
        assert evt.source == MetricEventSource.SYSTEM.value


async def test_resume_generated_event_contains_lifecycle(initialized_db):
    """19. Verify RESUME_GENERATED event lifecycle_id matches lifecycle_token."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        row = await session.get(Resume, res["resume_id"])
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.RESUME_GENERATED.value)
        )).scalar_one()
        assert evt.lifecycle_id == row.lifecycle_token


async def test_resume_generated_operation_key_format(initialized_db):
    """20. Verify operation_key format is generate:resume:{resume_id}:lc:{lifecycle_token}."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        row = await session.get(Resume, res["resume_id"])
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.RESUME_GENERATED.value)
        )).scalar_one()
        assert evt.operation_key == f"generate:resume:{res['resume_id']}:lc:{row.lifecycle_token}"


async def test_resume_create_and_events_are_atomic(initialized_db):
    """21. Verify Resume, ENTITY_CREATED, and RESUME_GENERATED commit atomically."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        rows = (await session.execute(select(Resume))).scalars().all()
        r = next((x for x in rows if x.resume_id == res["resume_id"]), None)
        assert r is not None

        events = (await session.execute(select(MetricEvent).where(MetricEvent.entity_id == res["resume_id"]))).scalars().all()
        # ENTITY_CREATED + RESUME_GENERATED
        assert len(events) == 2



async def test_multiple_updates_same_resume_no_lifetime_increase(initialized_db):
    """23. Verify updating tailored resume multiple times doesn't increase lifetime."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    await initialized_db.update_resume(res["resume_id"], {"content": "updated 1"})
    await initialized_db.update_resume(res["resume_id"], {"content": "updated 2"})

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1


async def test_duplicate_resume_generated_is_idempotent(initialized_db):
    """24. Verify recording duplicate RESUME_GENERATED event is idempotent and does not fail transaction."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    rid = res["resume_id"]

    async with initialized_db._session() as session:
        row = await session.get(Resume, rid)
        tok = row.lifecycle_token

    async with initialized_db._session() as session:
        evt = MetricEventInput(
            event_id=str(uuid4()),
            operation_key=f"generate:resume:{rid}:lc:{tok}",
            event_type=MetricEventType.RESUME_GENERATED,
            entity_type=MetricEntityType.RESUME,
            entity_id=rid,
            lifecycle_id=tok,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        res_record = await record_metric_event(session, evt)
        assert res_record.status == RecordEventStatus.DUPLICATE
        await session.commit()

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.RESUME_GENERATED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_generated_resume_increases_lifetime(initialized_db):
    """25. Verify generated resume increases resumes_generated lifetime by 1."""
    await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1


async def test_generated_resume_increases_current(initialized_db):
    """26. Verify generated resume increases resumes_generated current by 1."""
    await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 1


async def test_generated_resume_updates_peak(initialized_db):
    """27. Verify generated resume updates resumes_generated peak."""
    await initialized_db.create_resume(content="Tailored 1", parent_id="master-1")
    await initialized_db.create_resume(content="Tailored 2", parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.peak.value == 2


# --- PARENT_ID ---

async def test_generated_resume_has_parent_id(initialized_db):
    """28. Verify generated resume has a non-null parent_id pointing to existing resume."""
    parent = await initialized_db.create_resume(content="Parent content")
    child = await initialized_db.create_resume(content="Tailored", parent_id=parent["resume_id"])
    assert child["parent_id"] == parent["resume_id"]


async def test_upload_has_parent_id_null(initialized_db):
    """29. Verify uploaded resume has parent_id = None."""
    res = await initialized_db.create_resume_atomic_master(content="Resume")
    assert res["parent_id"] is None


async def test_update_cannot_set_parent_id_to_upload(initialized_db):
    """30. Verify update_resume rejects setting parent_id to upload resume."""
    res = await initialized_db.create_resume_atomic_master(content="Resume")
    with pytest.raises(ValueError, match="parent_id is immutable"):
        await initialized_db.update_resume(res["resume_id"], {"parent_id": "some-parent"})


async def test_update_cannot_remove_parent_id_from_generated(initialized_db):
    """31. Verify update_resume rejects removing parent_id from generated resume."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    with pytest.raises(ValueError, match="parent_id is immutable"):
        await initialized_db.update_resume(res["resume_id"], {"parent_id": None})


async def test_is_master_false_without_parent_id_not_counted_as_generated(initialized_db):
    """32. Verify is_master=False with parent_id=None is NOT counted in resumes_generated.current or lifetime."""
    async with initialized_db._session() as session:
        from app.database import _now
        r = Resume(
            resume_id="non-master-no-parent",
            content="Content",
            is_master=False,
            parent_id=None,
            processing_status="completed",
            created_at=_now(),
            updated_at=_now(),
            lifecycle_token="lc-non-master-no-parent"
        )
        session.add(r)
        await session.commit()

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 0
    assert mv.lifetime.value == 0


# --- REGENERATE ---

async def test_regenerate_creating_new_resume_increases_lifetime(initialized_db):
    """33. Verify regenerate creating a new tailored resume record increases resumes_generated lifetime by 1."""
    master = await initialized_db.create_resume_atomic_master(content="Master JD")
    job = await initialized_db.create_job(content="Job desc")

    improved_data = {
        "personalInfo": {"name": "Master JD", "email": "a@b.com", "phone": "123"},
        "summary": "Tailored summary",
        "workExperience": [],
        "education": [],
        "personalProjects": [],
        "additional": {"technicalSkills": [], "languages": [], "certificationsTraining": [], "awards": []}
    }
    request_hash = _hash_improved_data(improved_data)

    # Pre-inject preview hash for job
    async with initialized_db._session() as session:
        db_job = await session.get(Job, job["job_id"])
        meta = dict(db_job.metadata_json or {})
        meta["preview_hash"] = request_hash
        db_job.metadata_json = meta
        await session.commit()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Call confirm endpoint once
        resp1 = await client.post(
            "/api/v1/resumes/improve/confirm",
            json={
                "resume_id": master["resume_id"],
                "job_id": job["job_id"],
                "improved_data": improved_data,
                "improvements": []
            }
        )
        assert resp1.status_code == 200

        # Call improve (regenerate) with mocked improver services
        with _mock_improver():
            resp2 = await client.post(
                "/api/v1/resumes/improve",
                json={
                    "resume_id": master["resume_id"],
                    "job_id": job["job_id"],
                    "prompt_id": None
                }
            )
            assert resp2.status_code == 200

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 2
    assert mv.current.value == 2
    assert mv.peak.value == 2


async def test_regular_update_of_generated_resume_does_not_increase_lifetime(initialized_db):
    """34. Verify updating the same resume record doesn't write new events or increase lifetime."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    await initialized_db.update_resume(res["resume_id"], {"content": "modified text"})

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1


async def test_subsequent_improvements_same_tailored_resume_no_lifetime_increase(initialized_db):
    """35. Verify multiple improvements on the same tailored resume do not increase resumes_generated metrics."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    # Simulate adding improvements
    await initialized_db.create_improvement(
        original_resume_id="master-1",
        tailored_resume_id=res["resume_id"],
        job_id="job-1",
        improvements=[]
    )
    await initialized_db.create_improvement(
        original_resume_id="master-1",
        tailored_resume_id=res["resume_id"],
        job_id="job-1",
        improvements=["Another change"]
    )

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1


async def test_new_resume_new_lifecycle_can_be_counted_again(initialized_db):
    """36. Verify creating new tailored resume with same parent_id but new lifecycle is counted again."""
    await initialized_db.create_resume(content="Tailored 1", parent_id="master-1")
    await initialized_db.create_resume(content="Tailored 2", parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 2


# --- DELETE ---

async def test_delete_generated_emits_entity_deleted(initialized_db):
    """37. Verify deleting generated resume emits ENTITY_DELETED event."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 1
        assert events[0].entity_id == res["resume_id"]
        assert events[0].entity_type == MetricEntityType.RESUME.value


async def test_delete_event_contains_correct_lifecycle(initialized_db):
    """38. Verify delete event lifecycle_id matches token."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        row = await session.get(Resume, res["resume_id"])
        tok = row.lifecycle_token

    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalar_one()
        assert evt.lifecycle_id == tok


async def test_delete_and_event_are_atomic(initialized_db):
    """39. Verify delete and event logging are committed atomically."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        row = await session.get(Resume, res["resume_id"])
        assert row is None

        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 1



async def test_delete_nonexistent_resume_no_event(initialized_db):
    """41. Verify deleting nonexistent resume doesn't emit event or raise error."""
    res = await initialized_db.delete_resume("nonexistent-id")
    assert res is False

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_delete_generated_decreases_current(initialized_db):
    """42. Verify deleting generated resume decreases current."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        report1 = await build_metrics_report(session)
    mv1 = next(m for m in report1.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv1.current.value == 1

    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        report2 = await build_metrics_report(session)
    mv2 = next(m for m in report2.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv2.current.value == 0


async def test_delete_generated_does_not_decrease_lifetime(initialized_db):
    """43. Verify deleting generated resume doesn't decrease lifetime."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1


async def test_delete_upload_does_not_change_generated_current(initialized_db):
    """44. Verify deleting upload resume doesn't decrease resumes_generated current."""
    res = await initialized_db.create_resume(content="Resume")
    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 0


async def test_recreate_same_resume_id_has_new_lifecycle(initialized_db):
    """45. Verify recreating the same resume_id gives it a new lifecycle token."""
    rid = "recreate-res"
    tok1 = "lifecycle-1"
    tok2 = "lifecycle-2"

    await _force_create_resume(initialized_db, rid, tok1)
    await initialized_db.delete_resume(rid)
    await _force_create_resume(initialized_db, rid, tok2)

    async with initialized_db._session() as session:
        row = await session.get(Resume, rid)
        assert row.lifecycle_token == tok2
        assert row.lifecycle_token != tok1


async def test_two_subsequent_lifecycles_give_lifetime_2_current_1_peak_1(initialized_db):
    """46. Verify subsequent lifecycles of same resume_id yield lifetime=2, current=1, peak=1."""
    rid = "recreate-peak-res"
    tok1 = "lc-1"
    tok2 = "lc-2"

    await _force_create_resume(initialized_db, rid, tok1, parent_id="master-1")
    await initialized_db.delete_resume(rid)
    await _force_create_resume(initialized_db, rid, tok2, parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 2
    assert mv.current.value == 1
    assert mv.peak.value == 1


# --- REPORT ---

async def test_current_comes_from_resumes_table(initialized_db):
    """47. Verify resumes_generated.current is counted from resumes table."""
    await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 1


async def test_current_does_not_come_solely_from_events(initialized_db):
    """48. Verify resumes_generated.current matches DB state even if events differ."""
    # Write a fake RESUME_GENERATED event with no corresponding Resume row in DB
    async with initialized_db._session() as session:
        event = MetricEventInput(
            event_id="evt-fake-res",
            operation_key="generate:resume:fake:lc:fake",
            event_type=MetricEventType.RESUME_GENERATED,
            entity_type=MetricEntityType.RESUME,
            entity_id="fake-id",
            lifecycle_id="fake-lc",
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        await record_metric_event(session, event)
        await session.commit()

        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1
    assert mv.current.value == 0  # No Resume row exists with parent_id


async def test_parent_id_null_is_not_counted(initialized_db):
    """49. Verify parent_id=None is excluded from current."""
    await initialized_db.create_resume(content="R1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 0


async def test_parent_id_not_null_is_counted(initialized_db):
    """50. Verify parent_id IS NOT NULL is included in current."""
    await initialized_db.create_resume(content="Tailored", parent_id="master-1")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 1


async def test_deleted_generated_resume_is_not_current(initialized_db):
    """51. Verify deleted generated resume is not included in current."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    await initialized_db.delete_resume(res["resume_id"])

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.value == 0


async def test_multiple_events_same_lifecycle_gives_lifetime_1(initialized_db):
    """52. Verify multiple events for the same lifecycle yield lifetime=1."""
    res = await initialized_db.create_resume(content="Tailored", parent_id="master-1")
    rid = res["resume_id"]

    async with initialized_db._session() as session:
        row = await session.get(Resume, rid)
        tok = row.lifecycle_token

    # Write a second event for the same lifecycle manually
    async with initialized_db._session() as session:
        event = MetricEventInput(
            event_id="evt-dup-res",
            operation_key="generate:resume:xyz:lc:xyz", # different operation key but same lifecycle
            event_type=MetricEventType.RESUME_GENERATED,
            entity_type=MetricEntityType.RESUME,
            entity_id=rid,
            lifecycle_id=tok,
            from_state=None,
            to_state=None,
            source=MetricEventSource.SYSTEM,
        )
        await record_metric_event(session, event)
        await session.commit()

        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.lifetime.value == 1


async def test_resumes_generated_availability_is_available(initialized_db):
    """53. Verify report has resumes_generated availability = AVAILABLE."""
    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.current.availability == MetricAvailability.AVAILABLE


async def test_archived_remains_unsupported(initialized_db):
    """54. Verify report has archived availability = UNSUPPORTED."""
    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    mv = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert mv.archived.availability == MetricAvailability.UNSUPPORTED


# --- API ---

async def test_lifecycle_token_not_returned_in_post_upload(initialized_db):
    """55. Verify lifecycle_token is not in POST /resumes/upload response."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Dummy PDF content
        files = {"file": ("resume.pdf", b"%PDF-1.4 dummy", "application/pdf")}
        resp = await client.post("/api/v1/resumes/upload", files=files)
        assert resp.status_code == 200
        body = resp.json()
        assert "lifecycle_token" not in body


async def test_lifecycle_token_not_returned_in_get_resume(initialized_db):
    """56. Verify lifecycle_token is not in GET /resumes?resume_id={id} response."""
    res = await initialized_db.create_resume(content="Resume")
    rid = res["resume_id"]

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/resumes?resume_id={rid}")
        assert resp.status_code == 200
        body = resp.json()
        assert "lifecycle_token" not in body["data"]


async def test_lifecycle_token_not_returned_in_list_resume(initialized_db):
    """57. Verify lifecycle_token is not in GET /resumes/list response."""
    await initialized_db.create_resume(content="Resume")

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/resumes/list")
        assert resp.status_code == 200
        body = resp.json()
        for r in body["data"]:
            assert "lifecycle_token" not in r


async def test_lifecycle_token_not_returned_in_update_resume(initialized_db):
    """58. Verify lifecycle_token is not in PATCH /resumes/{id} response."""
    res = await initialized_db.create_resume(content="Resume")
    rid = res["resume_id"]

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/api/v1/resumes/{rid}",
            json={
                "personalInfo": {"name": "Updated Name", "email": "a@b.com", "phone": "123"},
                "education": [],
                "workExperience": [],
                "skills": [],
                "languages": [],
                "projects": [],
                "certifications": []
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "lifecycle_token" not in body["data"]


async def test_lifecycle_token_not_returned_in_confirm(initialized_db):
    """59. Verify lifecycle_token is not in POST /resumes/improve/confirm response."""
    master = await initialized_db.create_resume_atomic_master(content="Master JD")
    job = await initialized_db.create_job(content="Job desc")

    improved_data = {
        "personalInfo": {"name": "Master JD", "email": "a@b.com", "phone": "123"},
        "summary": "Tailored summary",
        "workExperience": [],
        "education": [],
        "personalProjects": [],
        "additional": {"technicalSkills": [], "languages": [], "certificationsTraining": [], "awards": []}
    }
    request_hash = _hash_improved_data(improved_data)

    # Pre-inject preview hash for job
    async with initialized_db._session() as session:
        db_job = await session.get(Job, job["job_id"])
        meta = dict(db_job.metadata_json or {})
        meta["preview_hash"] = request_hash
        db_job.metadata_json = meta
        await session.commit()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/resumes/improve/confirm",
            json={
                "resume_id": master["resume_id"],
                "job_id": job["job_id"],
                "improved_data": improved_data,
                "improvements": []
            }
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "lifecycle_token" not in body


async def test_lifecycle_token_not_returned_in_regenerate(initialized_db):
    """60. Verify lifecycle_token is not in POST /resumes/improve response."""
    master = await initialized_db.create_resume_atomic_master(content="Master JD")
    job = await initialized_db.create_job(content="Job desc")

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with _mock_improver():
            resp = await client.post(
                "/api/v1/resumes/improve",
                json={
                    "resume_id": master["resume_id"],
                    "job_id": job["job_id"],
                    "prompt_id": None
                }
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "lifecycle_token" not in body


async def test_generated_resume_requires_existing_parent(initialized_db):
    """Verify that create_resume raises ValueError if parent_id is specified but does not exist."""
    async with initialized_db._session() as session:
        resumes_before = (await session.execute(select(Resume))).scalars().all()
        events_before = (await session.execute(select(MetricEvent))).scalars().all()

    with pytest.raises(ValueError, match="Parent resume not found"):
        await initialized_db.create_resume(content="Tailored", parent_id="nonexistent-parent-id")

    async with initialized_db._session() as session:
        resumes_after = (await session.execute(select(Resume))).scalars().all()
        events_after = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(resumes_after) == len(resumes_before)
        assert len(events_after) == len(events_before)


async def test_generated_resume_rejects_blank_parent_id(initialized_db):
    """Verify that create_resume raises ValueError if parent_id is empty or whitespace."""
    with pytest.raises(ValueError, match="parent_id must be a string|parent_id cannot be empty or blank"):
        await initialized_db.create_resume(content="Tailored", parent_id="")

    with pytest.raises(ValueError, match="parent_id must be a string|parent_id cannot be empty or blank"):
        await initialized_db.create_resume(content="Tailored", parent_id="   ")


async def test_generated_resume_automatically_uses_system_source(initialized_db):
    """Verify that child resume automatically gets SYSTEM source for both ENTITY_CREATED and RESUME_GENERATED."""
    parent = await initialized_db.create_resume(content="Parent")
    child = await initialized_db.create_resume(content="Tailored", parent_id=parent["resume_id"])

    async with initialized_db._session() as session:
        evts = (await session.execute(
            select(MetricEvent).where(MetricEvent.entity_id == child["resume_id"])
        )).scalars().all()
        assert len(evts) == 2
        for e in evts:
            assert e.source == MetricEventSource.SYSTEM.value


async def test_duplicate_create_event_rolls_back_resume(initialized_db):
    """Verify that duplicate ENTITY_CREATED event rolls back resume and raises exception."""
    res_id = str(uuid4())
    lc_tok = str(uuid4())

    async with initialized_db._session() as session:
        duplicate_event = MetricEvent(
            event_id=str(uuid4()),
            operation_key=f"create:resume:{res_id}:lc:{lc_tok}",
            event_type=MetricEventType.ENTITY_CREATED.value,
            entity_type=MetricEntityType.RESUME.value,
            entity_id=res_id,
            lifecycle_id=lc_tok,
            source=MetricEventSource.USER.value,
            occurred_at="2026-07-16T18:00:00Z"
        )
        session.add(duplicate_event)
        await session.commit()

    call_idx = 0
    def mock_uuid4():
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return res_id
        if call_idx == 2:
            return lc_tok
        return uuid4()

    with patch("app.database.uuid4", mock_uuid4):
        with pytest.raises(ValueError, match="Duplicate metric event detected: ENTITY_CREATED duplicate"):
            await initialized_db.create_resume(content="Unique Content")

    async with initialized_db._session() as session:
        r = await session.get(Resume, res_id)
        assert r is None
        evts = (await session.execute(
            select(MetricEvent).where(MetricEvent.operation_key == f"create:resume:{res_id}:lc:{lc_tok}")
        )).scalars().all()
        assert len(evts) == 1


async def test_duplicate_generated_event_rolls_back_resume_and_create_event(initialized_db):
    """Verify that duplicate RESUME_GENERATED event rolls back child creation and ENTITY_CREATED event."""
    parent = await initialized_db.create_resume(content="Parent content")
    child_id = str(uuid4())
    lc_tok = str(uuid4())

    async with initialized_db._session() as session:
        duplicate_event = MetricEvent(
            event_id=str(uuid4()),
            operation_key=f"generate:resume:{child_id}:lc:{lc_tok}",
            event_type=MetricEventType.RESUME_GENERATED.value,
            entity_type=MetricEntityType.RESUME.value,
            entity_id=child_id,
            lifecycle_id=lc_tok,
            source=MetricEventSource.SYSTEM.value,
            occurred_at="2026-07-16T18:00:00Z"
        )
        session.add(duplicate_event)
        await session.commit()

    call_idx = 0
    def mock_uuid4():
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return child_id
        if call_idx == 2:
            return lc_tok
        return uuid4()

    with patch("app.database.uuid4", mock_uuid4):
        with pytest.raises(ValueError, match="Duplicate metric event detected: RESUME_GENERATED duplicate"):
            await initialized_db.create_resume(content="Tailored Content", parent_id=parent["resume_id"])

    async with initialized_db._session() as session:
        c = await session.get(Resume, child_id)
        assert c is None

        evts_created = (await session.execute(
            select(MetricEvent).where(MetricEvent.operation_key == f"create:resume:{child_id}:lc:{lc_tok}")
        )).scalars().all()
        assert len(evts_created) == 0

        evts_gen = (await session.execute(
            select(MetricEvent).where(MetricEvent.operation_key == f"generate:resume:{child_id}:lc:{lc_tok}")
        )).scalars().all()
        assert len(evts_gen) == 1


async def test_duplicate_delete_event_does_not_delete_resume(initialized_db):
    """Verify that duplicate ENTITY_DELETED event prevents resume deletion and rolls back."""
    res = await initialized_db.create_resume(content="To delete")
    rid = res["resume_id"]

    async with initialized_db._session() as session:
        row = await session.get(Resume, rid)
        lc_tok = row.lifecycle_token

    async with initialized_db._session() as session:
        duplicate_event = MetricEvent(
            event_id=str(uuid4()),
            operation_key=f"delete:resume:{rid}:lc:{lc_tok}",
            event_type=MetricEventType.ENTITY_DELETED.value,
            entity_type=MetricEntityType.RESUME.value,
            entity_id=rid,
            lifecycle_id=lc_tok,
            source=MetricEventSource.USER.value,
            occurred_at="2026-07-16T18:00:00Z"
        )
        session.add(duplicate_event)
        await session.commit()

    with pytest.raises(ValueError, match="Duplicate metric event detected: ENTITY_DELETED duplicate"):
        await initialized_db.delete_resume(rid)

    async with initialized_db._session() as session:
        r = await session.get(Resume, rid)
        assert r is not None


async def test_manual_delete_event_source_user(initialized_db):
    """Verify that manual delete_resume uses source USER."""
    res = await initialized_db.create_resume(content="Manual delete")
    rid = res["resume_id"]

    await initialized_db.delete_resume(rid)

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(
                (MetricEvent.entity_id == rid) &
                (MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
            )
        )).scalar_one()
        assert evt.source == MetricEventSource.USER.value


async def test_system_cleanup_delete_event_source_system(initialized_db):
    """Verify that delete_resume with source SYSTEM uses source SYSTEM."""
    res = await initialized_db.create_resume(content="System delete")
    rid = res["resume_id"]

    await initialized_db.delete_resume(rid, source=MetricEventSource.SYSTEM)

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(
                (MetricEvent.entity_id == rid) &
                (MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
            )
        )).scalar_one()
        assert evt.source == MetricEventSource.SYSTEM.value
