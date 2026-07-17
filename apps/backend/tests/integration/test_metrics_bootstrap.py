import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, Resume, Application, Improvement, MetricEvent, MetricBootstrapState, MetricTrackingState
from app.services.project_metrics import (
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    MetricEventInput,
    record_metric_event,
    build_metrics_report,
    MetricType,
    MetricAggregationError,
)
from app.services.project_metrics_bootstrap import bootstrap_metrics
from app.db_engine import init_models_sync


async def test_empty_bootstrap_creates_state(isolated_db):
    """1. Empty bootstrap creates MetricBootstrapState."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        res = await session.execute(select(MetricBootstrapState))
        row = res.scalar_one_or_none()
        assert row is not None
        assert row.bootstrap_key == "project_metrics_v1"


async def test_empty_bootstrap_baseline_event_count_zero(isolated_db):
    """2. Empty bootstrap has baseline_event_count=0."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row.baseline_event_count == 0


async def test_empty_bootstrap_database_was_empty_true(isolated_db):
    """3. Empty bootstrap records database_was_empty=True."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row.database_was_empty is True


async def test_empty_bootstrap_completeness_complete(isolated_db):
    """4. Empty bootstrap records history_completeness=COMPLETE."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row.history_completeness == "COMPLETE"


async def test_empty_bootstrap_second_run_is_noop(isolated_db):
    """5. Running bootstrap a second time is a no-op."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        row1 = (await session.execute(select(MetricBootstrapState))).scalar_one()
        time1 = row1.completed_at

    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        row2 = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row2.completed_at == time1


# --- JOB TESTS ---

async def test_existing_job_gets_baseline_create(isolated_db):
    """6. Existing Job gets baseline ENTITY_CREATED event."""
    # Insert a job directly via DB
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(
                (MetricEvent.entity_type == "JOB") & (MetricEvent.event_type == "ENTITY_CREATED")
            )
        )
        evt = res.scalar_one()
        assert evt.entity_id == "job-1"
        assert evt.lifecycle_id == "lc-job-1"


async def test_job_create_event_has_source_bootstrap(isolated_db):
    """7. Generated Job create event has source=BOOTSTRAP."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evt = (await session.execute(select(MetricEvent))).scalar_one()
        assert evt.source == "BOOTSTRAP"


async def test_job_create_event_has_correct_lifecycle_token(isolated_db):
    """8. Generated Job create event has correct lifecycle token."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evt = (await session.execute(select(MetricEvent))).scalar_one()
        assert evt.lifecycle_id == "lc-job-1"


async def test_job_with_improvement_gets_job_analyzed(isolated_db):
    """9. Job with improvement gets JOB_ANALYZED event."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        imp = Improvement(request_id="imp-1", job_id="job-1", original_resume_id="res-1", tailored_resume_id="res-2", improvements=[])
        session.add(job)
        session.add(imp)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "JOB_ANALYZED")
        )
        evt = res.scalar_one()
        assert evt.entity_id == "job-1"
        assert evt.source == "BOOTSTRAP"


async def test_job_with_multiple_improvements_gets_only_one_job_analyzed(isolated_db):
    """10. Job with multiple improvements gets exactly one JOB_ANALYZED event."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        imp1 = Improvement(request_id="imp-1", job_id="job-1", original_resume_id="res-1", tailored_resume_id="res-2", improvements=[])
        imp2 = Improvement(request_id="imp-2", job_id="job-1", original_resume_id="res-1", tailored_resume_id="res-3", improvements=[])
        session.add(job)
        session.add(imp1)
        session.add(imp2)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "JOB_ANALYZED")
        )
        evts = res.scalars().all()
        assert len(evts) == 1


async def test_job_without_improvement_does_not_get_job_analyzed(isolated_db):
    """11. Job without improvement does not get JOB_ANALYZED event."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "JOB_ANALYZED")
        )
        assert len(res.scalars().all()) == 0


async def test_existing_job_create_event_is_not_duplicated(isolated_db):
    """12. Existing ENTITY_CREATED event for Job lifecycle is not duplicated by bootstrap."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

        # Seed ENTITY_CREATED manually via record_metric_event
        evt = MetricEventInput(
            event_id="evt-manual-1",
            operation_key="job-create-manual",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-job-1",
        )
        await record_metric_event(session, evt)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(
                (MetricEvent.entity_id == "job-1") & (MetricEvent.event_type == "ENTITY_CREATED")
            )
        )
        assert len(res.scalars().all()) == 1


async def test_existing_job_analyzed_event_is_not_duplicated(isolated_db):
    """13. Existing JOB_ANALYZED event is not duplicated by bootstrap."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        imp = Improvement(request_id="imp-1", job_id="job-1", original_resume_id="res-1", tailored_resume_id="res-2", improvements=[])
        session.add(job)
        session.add(imp)
        await session.commit()

        evt = MetricEventInput(
            event_id="evt-manual-1",
            operation_key="job-analyzed-manual",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-job-1",
        )
        await record_metric_event(session, evt)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "JOB_ANALYZED")
        )
        assert len(res.scalars().all()) == 1


# --- RESUME TESTS ---

async def test_uploaded_resume_gets_only_create_event(isolated_db):
    """14. Uploaded Resume (parent_id=None) gets only ENTITY_CREATED event."""
    async with isolated_db._session() as session:
        res = Resume(
            resume_id="res-1",
            content="Markdown",
            content_type="md",
            is_master=True,
            parent_id=None,
            processing_status="completed",
            lifecycle_token="lc-res-1",
        )
        session.add(res)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 1
        assert evts[0].event_type == "ENTITY_CREATED"


async def test_generated_resume_gets_create_and_generated_events(isolated_db):
    """15. Generated Resume (parent_id is not None) gets ENTITY_CREATED and RESUME_GENERATED."""
    async with isolated_db._session() as session:
        res = Resume(
            resume_id="res-1",
            content="Markdown",
            content_type="md",
            is_master=False,
            parent_id="master-1",
            processing_status="completed",
            lifecycle_token="lc-res-1",
        )
        session.add(res)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(
            select(MetricEvent).order_by(MetricEvent.sequence_id)
        )).scalars().all()
        assert len(evts) == 2
        assert evts[0].event_type == "ENTITY_CREATED"
        assert evts[1].event_type == "RESUME_GENERATED"


async def test_resume_not_generated_without_parent_even_if_master_false(isolated_db):
    """16. Resume without parent is not generated, even if is_master=False."""
    async with isolated_db._session() as session:
        res = Resume(
            resume_id="res-1",
            content="Markdown",
            content_type="md",
            is_master=False,
            parent_id=None,
            processing_status="completed",
            lifecycle_token="lc-res-1",
        )
        session.add(res)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 1
        assert evts[0].event_type == "ENTITY_CREATED"


async def test_existing_resume_events_are_not_duplicated(isolated_db):
    """17. Existing resume events (create and generated) are not duplicated."""
    async with isolated_db._session() as session:
        res = Resume(
            resume_id="res-1",
            content="Markdown",
            content_type="md",
            is_master=False,
            parent_id="master-1",
            processing_status="completed",
            lifecycle_token="lc-res-1",
        )
        session.add(res)
        await session.commit()

        # Seed both events manually
        await record_metric_event(session, MetricEventInput(
            event_id="e1", operation_key="k1", event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.RESUME, entity_id="res-1", lifecycle_id="lc-res-1"
        ))
        await record_metric_event(session, MetricEventInput(
            event_id="e2", operation_key="k2", event_type=MetricEventType.RESUME_GENERATED,
            entity_type=MetricEntityType.RESUME, entity_id="res-1", lifecycle_id="lc-res-1"
        ))
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 2


async def test_generated_resume_with_deleted_parent_is_still_baseline_generated(isolated_db):
    """18. Generated Resume with non-existent parent still gets RESUME_GENERATED."""
    async with isolated_db._session() as session:
        res = Resume(
            resume_id="res-1",
            content="Markdown",
            content_type="md",
            is_master=False,
            parent_id="deleted-parent-id",
            processing_status="completed",
            lifecycle_token="lc-res-1",
        )
        session.add(res)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res_evts = await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "RESUME_GENERATED")
        )
        assert len(res_evts.scalars().all()) == 1


# --- APPLICATION TESTS ---

async def test_application_gets_create_event(isolated_db):
    """19. Application gets baseline ENTITY_CREATED event."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="saved", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "ENTITY_CREATED")
        )).scalar_one()
        assert evt.entity_id == "app-1"
        assert evt.to_state is None


async def test_application_without_status_event_gets_baseline_current_status(isolated_db):
    """20. Application without status events gets baseline status event."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="interview", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "BASELINE")
        )).scalar_one()
        assert evt.entity_id == "app-1"
        assert evt.to_state == "interview"
        assert evt.from_state is None


async def test_application_status_baseline_event_has_source_bootstrap(isolated_db):
    """21. Application baseline status event has source=BOOTSTRAP."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="interview", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "BASELINE")
        )).scalar_one()
        assert evt.source == "BOOTSTRAP"


async def test_application_status_baseline_event_does_not_increment_status_changes(isolated_db):
    """22. Application baseline status event does not increment status_changes in report."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="applied", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        report = await build_metrics_report(session)
        # Find status_changes metric
        status_metric = next(m for m in report.metrics if m.metric_name == MetricType.STATUS_CHANGES)
        assert status_metric.lifetime.value == 0


async def test_actual_status_change_after_bootstrap_increments_status_changes_by_one(isolated_db):
    """23. Actual status change after bootstrap increments status_changes lifetime by exactly 1."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="saved", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    # Perform a real status update
    await isolated_db.update_application("app-1", {"status": "applied"})

    async with isolated_db._session() as session:
        report = await build_metrics_report(session)
        status_metric = next(m for m in report.metrics if m.metric_name == MetricType.STATUS_CHANGES)
        assert status_metric.lifetime.value == 1


async def test_existing_application_status_event_is_not_duplicated(isolated_db):
    """24. Existing application status event prevents bootstrap from writing baseline status."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="interview", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

        # Seed status change event manually
        await record_metric_event(session, MetricEventInput(
            event_id="e-status", operation_key="op-status",
            event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
            entity_type=MetricEntityType.APPLICATION, entity_id="app-1", lifecycle_id="lc-app-1",
            from_state="saved", to_state="interview"
        ))
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        baseline_evts = await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "BASELINE")
        )
        assert len(baseline_evts.scalars().all()) == 0


async def test_intermediate_application_statuses_are_not_inferred(isolated_db):
    """25. Intermediate application statuses are not inferred by bootstrap status event."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="interview", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        # Should have exactly 2 events: ENTITY_CREATED (saved) and BASELINE (interview)
        assert len(evts) == 2
        types = {e.event_type for e in evts}
        assert types == {"ENTITY_CREATED", "BASELINE"}


# --- TRANSACTION TESTS ---

async def test_event_insert_error_rolls_back_all_events(isolated_db):
    """26. Event insert error rolls back all events."""
    # Seed duplicate ENTITY_CREATED event but with a different lifecycle to trigger unique key violation in check
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

        # Insert a conflict event in SQLite with the same operation key that bootstrap would generate,
        # but with a different entity_id, causing validate_idempotent_event to raise MetricAggregationError.
        conflict_evt = MetricEventInput(
            event_id="e-conflict",
            operation_key="bootstrap:v1:create:job:job-1:lc:lc-job-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-mismatched-id",
            lifecycle_id="lc-job-1",
        )
        await record_metric_event(session, conflict_evt)
        await session.commit()

    # Bootstrap should fail due to MetricAggregationError
    with pytest.raises(MetricAggregationError):
        await bootstrap_metrics(isolated_db)

    # Check that MetricBootstrapState was NOT created
    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one_or_none()
        assert row is None


async def test_state_insert_error_rolls_back_events(isolated_db):
    """27. State insert error rolls back events."""
    # Force state insert error by manually seeding the bootstrap row before running bootstrap,
    # but since bootstrap checks for it, let's bypass that by patching the check or using SQL constraint triggers.
    # Actually, we can insert a trigger that raises fail on metric_bootstrap_state insert!
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER fail_bootstrap_insert
            BEFORE INSERT ON metric_bootstrap_state
            BEGIN
                SELECT RAISE(FAIL, 'Mock insert failure');
            END;
            """
        )

    # Now add a job so bootstrap attempts to create an event
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    # Running bootstrap will fail during commit of MetricBootstrapState
    with pytest.raises(Exception):
        await bootstrap_metrics(isolated_db)

    # Verify that the ENTITY_CREATED event was rolled back
    async with isolated_db._session() as session:
        res = await session.execute(select(MetricEvent).where(MetricEvent.entity_id == "job-1"))
        assert len(res.scalars().all()) == 0

    # Clean up trigger
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER fail_bootstrap_insert")


async def test_retry_after_rollback_succeeds(isolated_db):
    """28. Retrying bootstrap after a rollback succeeds once the issue is fixed."""
    # Setup trigger that fails insert once, then disable it
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER fail_bootstrap_once
            BEFORE INSERT ON metric_bootstrap_state
            BEGIN
                SELECT RAISE(FAIL, 'Failed');
            END;
            """
        )

    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    with pytest.raises(Exception):
        await bootstrap_metrics(isolated_db)

    # Remove trigger
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER fail_bootstrap_once")

    # Retry should succeed
    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row.baseline_event_count == 1


async def test_completed_state_not_written_on_failure(isolated_db):
    """29. Completed state is not written on bootstrap failure."""
    # Simulates test 26 failure
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

        conflict_evt = MetricEventInput(
            event_id="e-conflict",
            operation_key="bootstrap:v1:create:job:job-1:lc:lc-job-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-mismatched-id",
            lifecycle_id="lc-job-1",
        )
        await record_metric_event(session, conflict_evt)
        await session.commit()

    with pytest.raises(MetricAggregationError):
        await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one_or_none()
        assert row is None


# --- IDEMPOTENCY TESTS ---

async def test_second_run_does_not_change_event_count(isolated_db):
    """30. Second run of bootstrap does not change event count."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        count1 = (await session.execute(select(func.count()).select_from(MetricEvent))).scalar()

    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        count2 = (await session.execute(select(func.count()).select_from(MetricEvent))).scalar()
        assert count1 == count2


async def test_second_run_does_not_change_state_timestamps(isolated_db):
    """31. Second run does not change MetricBootstrapState timestamps."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        state1 = (await session.execute(select(MetricBootstrapState))).scalar_one()
        baseline1 = state1.baseline_at
        completed1 = state1.completed_at

    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        state2 = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert state2.baseline_at == baseline1
        assert state2.completed_at == completed1


async def test_identical_existing_operation_key_accepted(isolated_db):
    """32. Identical existing operation_key is accepted (idempotency)."""
    async with isolated_db._session() as session:
        # 1. Create a Job
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)

        # 2. Create a generated Resume to provide domain data for newly recorded bootstrap events
        resume = Resume(
            resume_id="res-1",
            content="Tailored",
            content_type="md",
            is_master=False,
            parent_id="master-1",
            processing_status="completed",
            lifecycle_token="lc-res-1"
        )
        session.add(resume)
        await session.commit()

        # 3. Pre-seed the identical ENTITY_CREATED event for job-1
        evt = MetricEventInput(
            event_id="evt-1",
            operation_key="bootstrap:v1:create:job:job-1:lc:lc-job-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-job-1",
            source=MetricEventSource.BOOTSTRAP,
        )
        await record_metric_event(session, evt)
        await session.commit()

    # 4. Running bootstrap must not raise any error
    await bootstrap_metrics(isolated_db)

    # 5. Verify database contents explicitly
    async with isolated_db._session() as session:
        # A. MetricBootstrapState exists and was saved
        res_state = await session.execute(select(MetricBootstrapState))
        state = res_state.scalar_one()
        assert state is not None
        assert state.bootstrap_key == "project_metrics_v1"

        # B. baseline_event_count is exactly 2 (for the resume events; the job event was pre-seeded and not re-recorded)
        assert state.baseline_event_count == 2

        # C. Check the job event exists and is not duplicated (count = 1)
        res_job_evts = await session.execute(
            select(MetricEvent).where(MetricEvent.operation_key == "bootstrap:v1:create:job:job-1:lc:lc-job-1")
        )
        job_evts = res_job_evts.scalars().all()
        assert len(job_evts) == 1
        assert job_evts[0].event_id == "evt-1"  # must be the pre-seeded one

        # D. Check the newly created resume events exist (each has count = 1)
        res_res_evts = await session.execute(
            select(MetricEvent).where(MetricEvent.operation_key == "bootstrap:v1:create:resume:res-1:lc:lc-res-1")
        )
        assert len(res_res_evts.scalars().all()) == 1

        res_gen_evts = await session.execute(
            select(MetricEvent).where(MetricEvent.operation_key == "bootstrap:v1:generate:resume:res-1:lc:lc-res-1")
        )
        assert len(res_gen_evts.scalars().all()) == 1


async def test_mismatched_existing_event_with_same_operation_key_fails(isolated_db):
    """33. Existing event with same operation_key but mismatched data raises error."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

        # Pre-seed mismatched event
        evt = MetricEventInput(
            event_id="evt-1",
            operation_key="bootstrap:v1:create:job:job-1:lc:lc-job-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-mismatched",  # mismatched id
            lifecycle_id="lc-job-1",
            source=MetricEventSource.BOOTSTRAP,
        )
        await record_metric_event(session, evt)
        await session.commit()

    with pytest.raises(MetricAggregationError):
        await bootstrap_metrics(isolated_db)


# --- HISTORY TESTS ---

async def test_non_empty_database_yields_partial_baseline(isolated_db):
    """34. Non-empty database at bootstrap yields history_completeness=PARTIAL_BASELINE."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row.history_completeness == "PARTIAL_BASELINE"


async def test_baseline_at_is_recorded(isolated_db):
    """35. baseline_at is recorded in MetricBootstrapState."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert row.baseline_at is not None
        assert len(row.baseline_at) > 0


async def test_complete_since_equals_baseline_at(isolated_db):
    """36. Report complete_since equals baseline_at."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        report = await build_metrics_report(session)
        assert report.history is not None
        assert report.history.complete_since == report.history.baseline_at


async def test_baseline_event_count_is_accurate(isolated_db):
    """37. baseline_event_count in state equals actual inserted event count."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        resume = Resume(
            resume_id="res-1", content="Markdown", content_type="md",
            is_master=False, parent_id="master-1", processing_status="completed",
            lifecycle_token="lc-res-1"
        )
        session.add(job)
        session.add(resume)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        row = (await session.execute(select(MetricBootstrapState))).scalar_one()
        # Expects: job create (1) + resume create (1) + resume generated (1) = 3 events
        assert row.baseline_event_count == 3


# --- AGGREGATION TESTS ---

async def test_baseline_events_feed_lifetime_metric(isolated_db):
    """38. Baseline events feed lifetime metrics."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        report = await build_metrics_report(session)
        job_metric = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
        assert job_metric.lifetime.value == 1


async def test_baseline_events_feed_peak_metric(isolated_db):
    """39. Baseline events feed peak metrics."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        report = await build_metrics_report(session)
        job_metric = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
        assert job_metric.peak.value == 1


async def test_deterministic_ordering_independent_of_event_id_lexicographical_order(isolated_db):
    """40. Deterministic ordering: ENTITY_CREATED comes first even if event_ids are leksykograficznie reversed."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="applied", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    # Run bootstrap
    await bootstrap_metrics(isolated_db)

    # Verify order of sequence_id
    async with isolated_db._session() as session:
        evts = (await session.execute(
            select(MetricEvent).order_by(MetricEvent.sequence_id)
        )).scalars().all()
        assert len(evts) == 2
        assert evts[0].event_type == "ENTITY_CREATED"
        assert evts[1].event_type == "BASELINE"


async def test_two_lifecycles_with_same_id_remain_distinguished(isolated_db):
    """41. Two different lifecycles of same entity_id remain distinguished."""
    # e.g. Job recreation with same job_id but different lifecycle_tokens
    async with isolated_db._session() as session:
        job1 = Job(job_id="job-1", content="Desc 1", lifecycle_token="lc-job-1")
        session.add(job1)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    # Simulate deleting job1, adding new job with same job_id but new lifecycle
    async with isolated_db._session() as session:
        await session.execute(text("DELETE FROM jobs WHERE job_id = 'job-1'"))
        job2 = Job(job_id="job-1", content="Desc 2", lifecycle_token="lc-job-2")
        session.add(job2)
        await session.commit()

    # Reset bootstrap state to allow running it again
    async with isolated_db._session() as session:
        await session.execute(text("DROP TABLE IF EXISTS metric_bootstrap_state"))
        init_models_sync(isolated_db._sync_engine)

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricEvent).where(MetricEvent.entity_id == "job-1")
        )
        evts = res.scalars().all()
        # Should have ENTITY_CREATED for lc-job-1 (seeded in run 1) and ENTITY_CREATED for lc-job-2 (seeded in run 2)
        lifecycle_ids = {e.lifecycle_id for e in evts}
        assert "lc-job-1" in lifecycle_ids
        assert "lc-job-2" in lifecycle_ids


async def test_current_metric_value_still_originates_from_domain_tables(isolated_db):
    """42. current metric values originate from domain tables, not from event aggregation count."""
    async with isolated_db._session() as session:
        app = Application(
            application_id="app-1", job_id="job-1", resume_id="res-1",
            status="applied", position=1, created_at="2026", updated_at="2026",
            lifecycle_token="lc-app-1"
        )
        session.add(app)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    # Perform status change that isn't logged in metric events (simulation of out-of-sync or direct DB update)
    async with isolated_db._session() as session:
        await session.execute(
            text("UPDATE applications SET status = 'interview' WHERE application_id = 'app-1'")
        )
        await session.commit()

    # Metric report should show current status based on the database, not metric events
    async with isolated_db._session() as session:
        report = await build_metrics_report(session)
        interview_metric = next(m for m in report.metrics if m.metric_name == MetricType.INTERVIEWS)
        assert interview_metric.current.value == 1


# --- CONCURRENCY TESTS ---

async def test_concurrent_bootstrap_yields_one_completed_state(isolated_db):
    """44. Concurrent bootstrap yields exactly one completed state record."""
    await asyncio.gather(
        bootstrap_metrics(isolated_db),
        bootstrap_metrics(isolated_db)
    )

    async with isolated_db._session() as session:
        states = (await session.execute(select(MetricBootstrapState))).scalars().all()
        assert len(states) == 1


async def test_orphan_improvement_no_event(isolated_db):
    """45. Orphan Improvement (with no Job) does not create event but makes database_was_empty=False."""
    async with isolated_db._session() as session:
        imp = Improvement(request_id="imp-1", job_id="job-orphan", original_resume_id="res-1", tailored_resume_id="res-2")
        session.add(imp)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 0

        state = (await session.execute(select(MetricBootstrapState))).scalar_one()
        assert state.database_was_empty is False
        assert state.history_completeness == "PARTIAL_BASELINE"


async def test_sqlite_transaction_rollback_on_second_event(isolated_db):
    """46. Trigger error on secondary event causes rollback of ENTITY_CREATED and bootstrap state."""
    async with isolated_db._session() as session:
        r = Resume(
            resume_id="res-rollback",
            content="Tailored",
            content_type="md",
            is_master=False,
            parent_id="master-1",
            processing_status="completed",
            lifecycle_token="lc-rollback"
        )
        session.add(r)
        await session.commit()

    async with isolated_db._session() as session:
        await session.execute(text(
            """
            CREATE TRIGGER IF NOT EXISTS test_rollback_trigger
            BEFORE INSERT ON metric_events
            WHEN new.event_type = 'RESUME_GENERATED'
            BEGIN
                SELECT RAISE(FAIL, 'Simulated database error on RESUME_GENERATED');
            END;
            """
        ))
        await session.commit()

    with pytest.raises(Exception) as exc_info:
        await bootstrap_metrics(isolated_db)
    assert "Simulated database error on RESUME_GENERATED" in str(exc_info.value)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 0

        boot = (await session.execute(select(MetricBootstrapState))).scalar_one_or_none()
        assert boot is None

        track = (await session.execute(select(MetricTrackingState))).scalar_one_or_none()
        assert track is None

    async with isolated_db._session() as session:
        await session.execute(text("DROP TRIGGER test_rollback_trigger"))
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with isolated_db._session() as session:
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 2
        evt_types = {e.event_type for e in evts}
        assert "ENTITY_CREATED" in evt_types
        assert "RESUME_GENERATED" in evt_types

        boot = (await session.execute(select(MetricBootstrapState))).scalar_one_or_none()
        assert boot is not None
        assert boot.baseline_event_count == 2

        track = (await session.execute(select(MetricTrackingState))).scalar_one_or_none()
        assert track is not None


async def test_concurrent_bootstrap_with_data(isolated_db):
    """47. Concurrent bootstrap attempts with pre-existing data do not duplicate events or state records."""
    async with isolated_db._session() as session:
        j1 = Job(job_id="job-1", content="JD 1", lifecycle_token="lc-job-1")
        j2 = Job(job_id="job-2", content="JD 2", lifecycle_token="lc-job-2")
        session.add(j1)
        session.add(j2)

        imp = Improvement(request_id="imp-2", job_id="job-2", original_resume_id="res-1", tailored_resume_id="res-2")
        session.add(imp)

        res = Resume(
            resume_id="res-gen",
            content="Tailored",
            content_type="md",
            is_master=False,
            parent_id="master-1",
            processing_status="completed",
            lifecycle_token="lc-res-gen"
        )
        session.add(res)

        app = Application(
            application_id="app-int",
            job_id="job-1",
            resume_id="res-gen",
            status="interview",
            position=1,
            created_at="2026-07-16T21:40:00Z",
            updated_at="2026-07-16T21:40:00Z",
            lifecycle_token="lc-app-int"
        )
        session.add(app)
        await session.commit()

    await asyncio.gather(
        bootstrap_metrics(isolated_db),
        bootstrap_metrics(isolated_db)
    )

    async with isolated_db._session() as session:
        states = (await session.execute(select(MetricBootstrapState))).scalars().all()
        assert len(states) == 1
        boot_state = states[0]

        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 7
        assert boot_state.baseline_event_count == 7

        op_keys = [e.operation_key for e in evts]
        assert len(set(op_keys)) == 7

        report = await build_metrics_report(session)

        job_saved = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
        assert job_saved.lifetime.value == 2
        assert job_saved.peak.value == 2

        job_analyzed = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
        assert job_analyzed.lifetime.value == 1
        assert job_analyzed.peak.value == 1

        resumes_gen = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
        assert resumes_gen.lifetime.value == 1
        assert resumes_gen.peak.value == 1

        apps_created = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
        assert apps_created.lifetime.value == 1
        assert apps_created.peak.value == 1

        interviews = next(m for m in report.metrics if m.metric_name == MetricType.INTERVIEWS)
        assert interviews.lifetime.value == 1
        assert interviews.peak.value == 1

        employer_resp = next(m for m in report.metrics if m.metric_name == MetricType.EMPLOYER_RESPONSES)
        assert employer_resp.lifetime.value == 1
        assert employer_resp.peak.value == 1


async def test_deterministic_ordering_with_reverse_lexicographical_event_ids(isolated_db):
    """48. Event aggregation sorts by sequence_id, not lexicographically by event_id."""
    evt_created = MetricEvent(
        event_id="z-created",
        sequence_id=1,
        operation_key="op-create",
        event_type="ENTITY_CREATED",
        entity_type="APPLICATION",
        entity_id="app-ordered",
        lifecycle_id="lc-ordered",
        occurred_at="2026-07-16T21:40:00Z",
        source="BOOTSTRAP",
        to_state=None
    )
    evt_baseline = MetricEvent(
        event_id="a-baseline",
        sequence_id=2,
        operation_key="op-baseline",
        event_type="BASELINE",
        entity_type="APPLICATION",
        entity_id="app-ordered",
        lifecycle_id="lc-ordered",
        occurred_at="2026-07-16T21:40:01Z",
        source="BOOTSTRAP",
        to_state="interview"
    )

    from app.services.project_metrics import aggregate_events
    report = aggregate_events(
        events=[evt_baseline, evt_created],
        tracking_started_at="2026-07-16T21:40:00Z",
        generated_at="2026-07-16T21:45:00Z",
        historical_complete=True
    )

    interviews = next(m for m in report.metrics if m.metric_name == MetricType.INTERVIEWS)
    assert interviews.lifetime.value == 1


async def test_duplicate_event_id_collision_causes_rollback(isolated_db):
    """49. Event ID collision on a different operation_key raises error and rolls back."""
    from unittest.mock import patch
    async with isolated_db._session() as session:
        evt1 = MetricEvent(
            event_id="colliding-id",
            operation_key="op-1",
            event_type="ENTITY_CREATED",
            entity_type="JOB",
            entity_id="job-1",
            lifecycle_id="lc-1",
            occurred_at="2026-07-16T21:40:00Z",
            source="BOOTSTRAP",
        )
        session.add(evt1)

        job = Job(job_id="job-new", content="JD", lifecycle_token="lc-new")
        session.add(job)
        await session.commit()

    with patch("app.services.project_metrics_bootstrap.uuid4", return_value="colliding-id"):
        with pytest.raises(MetricAggregationError) as exc_info:
            await bootstrap_metrics(isolated_db)
        assert "Duplicate event status returned but event with operation_key" in str(exc_info.value)

    async with isolated_db._session() as session:
        boot = (await session.execute(select(MetricBootstrapState))).scalar_one_or_none()
        assert boot is None
        evts = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(evts) == 1
        assert evts[0].operation_key == "op-1"


async def test_bootstrap_schema_version_mismatch_raises_error(isolated_db):
    """50. bootstrap_metrics raises error if schema_version in existing state is not 1."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=99,
            baseline_at="2026",
            completed_at="2026",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        await session.commit()

    with pytest.raises(MetricAggregationError) as exc_info:
        await bootstrap_metrics(isolated_db)
    assert "Unsupported bootstrap schema version" in str(exc_info.value)


async def test_unrelated_integrity_error_not_masked(isolated_db):
    """51. Unrelated unique constraint violation raises IntegrityError and is not masked."""
    async with isolated_db._session() as session:
        j = Job(job_id="job-1", content="JD", lifecycle_token="lc-job-1")
        session.add(j)
        await session.commit()

    async with isolated_db._session() as session:
        await session.execute(text(
            """
            CREATE TRIGGER IF NOT EXISTS test_unrelated_trigger
            BEFORE INSERT ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'UNIQUE constraint failed: metric_events.unrelated_field_is_missing');
            END;
            """
        ))
        await session.commit()

    with pytest.raises(Exception) as exc_info:
        await bootstrap_metrics(isolated_db)
    assert "UNIQUE constraint failed: metric_events.unrelated_field_is_missing" in str(exc_info.value)


async def test_real_migration_path_create_interview(isolated_db):
    """52. Real migration path with create_application(status='interview') and bootstrap_metrics."""
    # Create application via database API
    app_dict = await isolated_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="interview",
        source=MetricEventSource.USER,
    )
    app_id = app_dict["application_id"]
    # Confirm state before bootstrap
    async with isolated_db._session() as session:
        # Application exists
        app_row = (await session.execute(
            select(Application).where(Application.application_id == app_id)
        )).scalar_one()
        assert app_row is not None
        assert app_row.status == "interview"
        lc_token = app_row.lifecycle_token

        # ENTITY_CREATED event exists with source USER and to_state="interview"
        evt = (await session.execute(
            select(MetricEvent).where(
                (MetricEvent.entity_id == app_id) & (MetricEvent.event_type == "ENTITY_CREATED")
            )
        )).scalar_one()
        assert evt.source == "USER"
        assert evt.to_state == "interview"

        # No APPLICATION_STATUS_CHANGED event
        sc_evt = (await session.execute(
            select(MetricEvent).where(MetricEvent.event_type == "APPLICATION_STATUS_CHANGED")
        )).scalar_one_or_none()
        assert sc_evt is None

        # No MetricBootstrapState
        boot_state = (await session.execute(
            select(MetricBootstrapState)
        )).scalar_one_or_none()
        assert boot_state is None

    # Run bootstrap
    await bootstrap_metrics(isolated_db)

    # Assert state after bootstrap
    async with isolated_db._session() as session:
        # exactly one application lifecycle exists in MetricEvent
        all_evts = (await session.execute(
            select(MetricEvent).where(MetricEvent.entity_id == app_id)
        )).scalars().all()

        # We expect:
        # 1. The original ENTITY_CREATED source=USER to_state="interview"
        # 2. The bootstrap-generated BASELINE source=BOOTSTRAP to_state="interview"
        assert len(all_evts) == 2

        created_evts = [e for e in all_evts if e.event_type == "ENTITY_CREATED"]
        assert len(created_evts) == 1
        assert created_evts[0].source == "USER"

        baseline_evts = [e for e in all_evts if e.event_type == "BASELINE"]
        assert len(baseline_evts) == 1
        assert baseline_evts[0].source == "BOOTSTRAP"
        assert baseline_evts[0].to_state == "interview"

        # Build metrics report
        report = await build_metrics_report(session)

        m_created = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
        m_interviews = next(m for m in report.metrics if m.metric_name == MetricType.INTERVIEWS)
        m_responses = next(m for m in report.metrics if m.metric_name == MetricType.EMPLOYER_RESPONSES)
        m_status_changes = next(m for m in report.metrics if m.metric_name == MetricType.STATUS_CHANGES)

        assert m_created.lifetime.value == 1
        assert m_created.peak.value == 1
        assert m_interviews.lifetime.value == 1
        assert m_interviews.peak.value == 1
        assert m_responses.lifetime.value == 1
        assert m_responses.peak.value == 1
        assert m_status_changes.lifetime.value == 0


async def test_real_migration_path_create_applied(isolated_db):
    """53. Real migration path with create_application(status='applied') and bootstrap_metrics."""
    # Create application via database API
    app_dict = await isolated_db.create_application(
        job_id="job-2",
        resume_id="res-2",
        status="applied",
        source=MetricEventSource.USER,
    )
    app_id = app_dict["application_id"]

    # Run bootstrap
    await bootstrap_metrics(isolated_db)

    # Build metrics report
    async with isolated_db._session() as session:
        report = await build_metrics_report(session)

        m_submitted = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_SUBMITTED)
        assert m_submitted.lifetime.value == 1
        assert m_submitted.peak.value == 1
