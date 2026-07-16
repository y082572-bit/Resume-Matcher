import pytest
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models import Application, MetricEvent
from app.services.project_metrics import (
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    MetricAvailability,
    MetricType,
    ApplicationStatusConflictError,
    initialize_metric_tracking_state,
    build_metrics_report,
)


@pytest.fixture
async def initialized_db(isolated_db):
    async with isolated_db._session() as session:
        await initialize_metric_tracking_state(session, historical_complete=True)
        await session.commit()
    return isolated_db


# --- CREATE TESTS ---

async def test_manual_create_emits_entity_created_source_user(initialized_db):
    """1. Manual create_application emits ENTITY_CREATED with source USER."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
        source=MetricEventSource.USER,
    )

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        evt = events[0]
        assert evt.event_type == MetricEventType.ENTITY_CREATED.value
        assert evt.entity_type == MetricEntityType.APPLICATION.value
        assert evt.entity_id == app["application_id"]
        assert evt.source == MetricEventSource.USER.value


async def test_auto_create_emits_entity_created_source_system(initialized_db):
    """2. Automatic create_application emits ENTITY_CREATED with source SYSTEM."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="applied",
        source=MetricEventSource.SYSTEM,
    )

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        evt = events[0]
        assert evt.source == MetricEventSource.SYSTEM.value


async def test_create_event_contains_initial_status(initialized_db):
    """3. Verify event contains the initial status."""
    await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="interview",
        source=MetricEventSource.USER,
    )

    async with initialized_db._session() as session:
        evt = (await session.execute(select(MetricEvent))).scalar_one()
        assert evt.to_state == "interview"
        assert evt.from_state is None


async def test_create_event_contains_record_lifecycle_token(initialized_db):
    """4. Verify event contains lifecycle_token of the application record."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        evt = (await session.execute(select(MetricEvent))).scalar_one()
        assert row.lifecycle_token == evt.lifecycle_id


async def test_create_and_event_are_atomic_success(initialized_db):
    """5. Verify create and event save atomically."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row is not None
        evt = (await session.execute(select(MetricEvent))).scalar_one()
        assert evt is not None


@patch("app.database.record_metric_event")
async def test_create_event_error_rolls_back_application(mock_record, initialized_db):
    """6. Verify event write failure rolls back application creation."""
    mock_record.side_effect = Exception("Simulated event DB error")

    with pytest.raises(Exception, match="Simulated event DB error"):
        await initialized_db.create_application(
            job_id="job-1",
            resume_id="res-1",
            status="saved",
        )

    async with initialized_db._session() as session:
        apps = (await session.execute(select(Application))).scalars().all()
        assert len(apps) == 0


async def test_no_duplicate_event_when_application_exists(initialized_db):
    """7. Verify no second event is emitted if card already exists."""
    app1 = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    app2 = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    assert app1["application_id"] == app2["application_id"]

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1


# --- STATUS TESTS ---

async def test_status_change_increases_status_version(initialized_db):
    """8. Zmiana saved -> applied zwiększa status_version."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    # Verify version before
    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row.status_version == 0

    # Update status
    await initialized_db.update_application(app["application_id"], {"status": "applied"})

    # Verify version after
    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row.status_version == 1
        assert row.status == "applied"


async def test_status_change_emits_application_status_changed(initialized_db):
    """9. Zmiana statusu emituje APPLICATION_STATUS_CHANGED."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    await initialized_db.update_application(app["application_id"], {"status": "applied"})

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_status_change_event_contains_old_and_new_statuses(initialized_db):
    """10. Event zawiera old_status i new_status."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    await initialized_db.update_application(app["application_id"], {"status": "applied"})

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalar_one()
        assert evt.from_state == "saved"
        assert evt.to_state == "applied"


async def test_status_change_operation_key_contains_old_version(initialized_db):
    """11. Operation_key zawiera old_version."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    await initialized_db.update_application(app["application_id"], {"status": "applied"})

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalar_one()
        assert "ver:0" in evt.operation_key


async def test_noop_status_does_not_increase_version(initialized_db):
    """12. No-op status nie zwiększa wersji."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="applied",
    )
    await initialized_db.update_application(app["application_id"], {"status": "applied"})

    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row.status_version == 0


async def test_noop_status_does_not_emit_event(initialized_db):
    """13. No-op status nie emituje eventu."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="applied",
    )
    await initialized_db.update_application(app["application_id"], {"status": "applied"})

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_update_position_only_does_not_increase_version(initialized_db):
    """14. Update pozycji nie zwiększa wersji."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    await initialized_db.update_application(app["application_id"], {"position": 5})

    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row.status_version == 0


async def test_update_position_only_does_not_emit_status_event(initialized_db):
    """15. Update pozycji nie emituje status eventu."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    await initialized_db.update_application(app["application_id"], {"position": 5})

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_cyclic_status_changes_emit_multiple_events(initialized_db):
    """16. A -> B -> A generuje dwa eventy i dwie wersje."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )
    await initialized_db.update_application(app["application_id"], {"status": "applied"})
    await initialized_db.update_application(app["application_id"], {"status": "saved"})

    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row.status_version == 2

        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 2


async def test_status_version_conflict_raises_exception(initialized_db):
    """17. Konflikt status_version zgłasza ApplicationStatusConflictError."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    original_execute = AsyncSession.execute
    async def mock_execute(self, clause, params=None, *args, **kwargs):
        if hasattr(clause, "is_update") and clause.is_update:
            stmt_params = clause.compile().params
            if params is None:
                params = {}
            for k, v in stmt_params.items():
                if k not in params:
                    params[k] = v
            for k, v in list(params.items()):
                if k.startswith("status_version") and v == 0:
                    params[k] = 5
        return await original_execute(self, clause, params, *args, **kwargs)

    with patch.object(AsyncSession, "execute", mock_execute):
        with pytest.raises(ApplicationStatusConflictError):
            await initialized_db.update_application(app["application_id"], {"status": "applied"})


async def test_status_version_conflict_does_not_save_event(initialized_db):
    """18. Konflikt nie zapisuje eventu."""
    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    original_execute = AsyncSession.execute
    async def mock_execute(self, clause, params=None, *args, **kwargs):
        if hasattr(clause, "is_update") and clause.is_update:
            stmt_params = clause.compile().params
            if params is None:
                params = {}
            for k, v in stmt_params.items():
                if k not in params:
                    params[k] = v
            for k, v in list(params.items()):
                if k.startswith("status_version") and v == 0:
                    params[k] = 5
        return await original_execute(self, clause, params, *args, **kwargs)

    try:
        with patch.object(AsyncSession, "execute", mock_execute):
            await initialized_db.update_application(app["application_id"], {"status": "applied"})
    except ApplicationStatusConflictError:
        pass

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_api_maps_status_conflict_to_409(initialized_db):
    """19. API mapuje konflikt na HTTP 409."""
    from app.routers.applications import update_application as api_update_app
    from app.schemas import ApplicationUpdate, ApplicationStatus

    app = await initialized_db.create_application(
        job_id="job-1",
        resume_id="res-1",
        status="saved",
    )

    original_execute = AsyncSession.execute
    async def mock_execute(self, clause, params=None, *args, **kwargs):
        if hasattr(clause, "is_update") and clause.is_update:
            stmt_params = clause.compile().params
            if params is None:
                params = {}
            for k, v in stmt_params.items():
                if k not in params:
                    params[k] = v
            for k, v in list(params.items()):
                if k.startswith("status_version") and v == 0:
                    params[k] = 5
        return await original_execute(self, clause, params, *args, **kwargs)

    req = ApplicationUpdate(status=ApplicationStatus.applied)
    with patch.object(AsyncSession, "execute", mock_execute):
        with pytest.raises(HTTPException) as exc_info:
            await api_update_app(app["application_id"], req)

    assert exc_info.value.status_code == 409


# --- BULK UPDATE TESTS ---

async def test_bulk_update_increases_version_for_changed_cards(initialized_db):
    """20. Bulk update zwiększa wersję każdej zmienionej karty."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    await initialized_db.bulk_update_applications([app1["application_id"], app2["application_id"]], "applied")

    async with initialized_db._session() as session:
        r1 = await session.get(Application, app1["application_id"])
        r2 = await session.get(Application, app2["application_id"])
        assert r1.status_version == 1
        assert r2.status_version == 1


async def test_bulk_update_emits_separate_event_for_each_change(initialized_db):
    """21. Bulk update emituje osobny event dla każdej zmiany."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    await initialized_db.bulk_update_applications([app1["application_id"], app2["application_id"]], "applied")

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 2


async def test_bulk_noop_does_not_emit_event(initialized_db):
    """22. Bulk no-op nie emituje eventu."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="applied")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="applied")

    await initialized_db.bulk_update_applications([app1["application_id"], app2["application_id"]], "applied")

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_bulk_update_conflict_rolls_back_entire_bulk(initialized_db):
    """24. Konflikt jednej karty wycofuje cały bulk update."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    original_execute = AsyncSession.execute
    async def mock_execute(self, clause, params=None, *args, **kwargs):
        if hasattr(clause, "is_update") and clause.is_update:
            stmt_params = clause.compile().params
            if params is None:
                params = {}
            for k, v in stmt_params.items():
                if k not in params:
                    params[k] = v
            # For bulk update conflict, we only target the parameter matching app2 application_id
            is_app2 = any(
                k.startswith("application_id") and v == app2["application_id"]
                for k, v in params.items()
            )
            if is_app2:
                for k, v in list(params.items()):
                    if k.startswith("status_version") and v == 0:
                        params[k] = 5
        return await original_execute(self, clause, params, *args, **kwargs)

    with patch.object(AsyncSession, "execute", mock_execute):
        with pytest.raises(ApplicationStatusConflictError):
            await initialized_db.bulk_update_applications([app1["application_id"], app2["application_id"]], "applied")

    # Verify app1 was NOT modified (rolled back)
    async with initialized_db._session() as session:
        r1 = await session.get(Application, app1["application_id"])
        assert r1.status == "saved"
        assert r1.status_version == 0


async def test_bulk_update_conflict_rolls_back_all_events(initialized_db):
    """25. Konflikt jednej karty wycofuje wszystkie eventy bulk."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    original_execute = AsyncSession.execute
    async def mock_execute(self, clause, params=None, *args, **kwargs):
        if hasattr(clause, "is_update") and clause.is_update:
            stmt_params = clause.compile().params
            if params is None:
                params = {}
            for k, v in stmt_params.items():
                if k not in params:
                    params[k] = v
            is_app2 = any(
                k.startswith("application_id") and v == app2["application_id"]
                for k, v in params.items()
            )
            if is_app2:
                for k, v in list(params.items()):
                    if k.startswith("status_version") and v == 0:
                        params[k] = 5
        return await original_execute(self, clause, params, *args, **kwargs)

    try:
        with patch.object(AsyncSession, "execute", mock_execute):
            await initialized_db.bulk_update_applications([app1["application_id"], app2["application_id"]], "applied")
    except ApplicationStatusConflictError:
        pass

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


# --- DELETE TESTS ---

async def test_delete_emits_entity_deleted(initialized_db):
    """26. Delete emituje ENTITY_DELETED."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    await initialized_db.delete_application(app["application_id"])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_delete_event_contains_lifecycle_token(initialized_db):
    """27. Event delete zawiera prawidłowy lifecycle_token."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    # Store token from DB
    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        token = row.lifecycle_token

    await initialized_db.delete_application(app["application_id"])

    async with initialized_db._session() as session:
        evt = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalar_one()
        assert evt.lifecycle_id == token


async def test_delete_does_not_decrease_lifetime_created_metric(initialized_db):
    """28. Delete nie zmniejsza lifetime applications_created."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    async with initialized_db._session() as session:
        report_before = await build_metrics_report(session)
    created_lifetime_before = next(m for m in report_before.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED).lifetime.value

    await initialized_db.delete_application(app["application_id"])

    async with initialized_db._session() as session:
        report_after = await build_metrics_report(session)
    created_lifetime_after = next(m for m in report_after.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED).lifetime.value

    assert created_lifetime_before == 1
    assert created_lifetime_after == 1


async def test_delete_decreases_current_applications_created(initialized_db):
    """29. Delete zmniejsza applications_created.current."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    async with initialized_db._session() as session:
        report_before = await build_metrics_report(session)
    curr_before = next(m for m in report_before.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED).current.value

    await initialized_db.delete_application(app["application_id"])

    async with initialized_db._session() as session:
        report_after = await build_metrics_report(session)
    curr_after = next(m for m in report_after.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED).current.value

    assert curr_before == 1
    assert curr_after == 0


async def test_delete_nonexistent_application_does_not_emit_event(initialized_db):
    """30. Delete nieistniejącej aplikacji nie emituje eventu."""
    await initialized_db.delete_application("nonexistent-id")

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 0


@patch("app.database.record_metric_event")
async def test_delete_event_error_rolls_back_deletion(mock_record, initialized_db):
    """31. Błąd eventu wycofuje delete."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    mock_record.side_effect = Exception("Simulated DB error")

    with pytest.raises(Exception, match="Simulated DB error"):
        await initialized_db.delete_application(app["application_id"])

    # Verify application is still in DB (rolled back)
    async with initialized_db._session() as session:
        row = await session.get(Application, app["application_id"])
        assert row is not None


async def test_bulk_delete_emits_event_for_each_existing_record(initialized_db):
    """32. Bulk delete emituje event dla każdego istniejącego rekordu."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    # Pass one nonexistent too
    await initialized_db.bulk_delete_applications([app1["application_id"], app2["application_id"], "nonexistent-id"])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 2


async def test_bulk_delete_is_atomic(initialized_db):
    """33. Bulk delete jest atomowy."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    from app.services.project_metrics import record_metric_event as prod_record_metric_event
    calls = 0

    async def mock_record_event(session, event_input):
        nonlocal calls
        if event_input.event_type == MetricEventType.ENTITY_DELETED:
            calls += 1
            if calls == 2:
                raise RuntimeError("Simulated bulk delete error")
        await prod_record_metric_event(session, event_input)

    with patch("app.database.record_metric_event", mock_record_event):
        with pytest.raises(RuntimeError, match="Simulated bulk delete error"):
            await initialized_db.bulk_delete_applications([app1["application_id"], app2["application_id"]])

    # Verify both apps are still there
    async with initialized_db._session() as session:
        r1 = await session.get(Application, app1["application_id"])
        r2 = await session.get(Application, app2["application_id"])
        assert r1 is not None
        assert r2 is not None

        # zero ENTITY_DELETED
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 0

    # current count didn't change
    async with initialized_db._session() as session:
        report = await build_metrics_report(session)
    curr = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED).current.value
    assert curr == 2


async def test_reuse_application_id_with_new_lifecycle_does_not_conflict(initialized_db):
    """34. Ponowne użycie application_id z nowym lifecycle nie koliduje z historycznym delete."""
    app_id = "reused-id"

    # Create and delete first lifecycle card
    async with initialized_db._session() as session:
        row1 = Application(
            application_id=app_id,
            job_id="job-1",
            resume_id="res-1",
            status="saved",
            lifecycle_token="lc-first",
        )
        session.add(row1)
        await session.commit()
    await initialized_db.delete_application(app_id)

    # Create and delete second lifecycle card
    async with initialized_db._session() as session:
        row2 = Application(
            application_id=app_id,
            job_id="job-1",
            resume_id="res-1",
            status="saved",
            lifecycle_token="lc-second",
        )
        session.add(row2)
        await session.commit()
    await initialized_db.delete_application(app_id)

    # Verify both delete events are stored
    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 2


# --- REPORT TESTS ---

async def test_report_current_metrics_counted_from_applications_table(initialized_db):
    """35 to 42. Verify report counts current application metrics directly from applications table."""
    # Insert applications of varying statuses
    await initialized_db.create_application(job_id="j-1", resume_id="r-1", status="applied")
    await initialized_db.create_application(job_id="j-1", resume_id="r-2", status="applied")
    await initialized_db.create_application(job_id="j-1", resume_id="r-3", status="saved")
    await initialized_db.create_application(job_id="j-1", resume_id="r-4", status="interview")
    await initialized_db.create_application(job_id="j-1", resume_id="r-5", status="rejected")
    await initialized_db.create_application(job_id="j-1", resume_id="r-6", status="accepted")
    await initialized_db.create_application(job_id="j-1", resume_id="r-7", status="response")

    async with initialized_db._session() as session:
        report = await build_metrics_report(session)

    def get_measure(name: MetricType):
        mv = next(m for m in report.metrics if m.metric_name == name)
        return mv.current

    # 35. applications_created.current: 7 total cards
    assert get_measure(MetricType.APPLICATIONS_CREATED).value == 7
    assert get_measure(MetricType.APPLICATIONS_CREATED).availability == MetricAvailability.AVAILABLE

    # 36. applications_submitted.current: 2 applied
    assert get_measure(MetricType.APPLICATIONS_SUBMITTED).value == 2
    assert get_measure(MetricType.APPLICATIONS_SUBMITTED).availability == MetricAvailability.AVAILABLE

    # 37. employer_responses.current: 4 response/interview/accepted/rejected
    assert get_measure(MetricType.EMPLOYER_RESPONSES).value == 4
    assert get_measure(MetricType.EMPLOYER_RESPONSES).availability == MetricAvailability.AVAILABLE

    # 38. interviews.current: 1 interview
    assert get_measure(MetricType.INTERVIEWS).value == 1
    assert get_measure(MetricType.INTERVIEWS).availability == MetricAvailability.AVAILABLE

    # 39. rejections.current: 1 rejected
    assert get_measure(MetricType.REJECTIONS).value == 1
    assert get_measure(MetricType.REJECTIONS).availability == MetricAvailability.AVAILABLE

    # 40. applications_accepted.current: 1 accepted
    assert get_measure(MetricType.APPLICATIONS_ACCEPTED).value == 1
    assert get_measure(MetricType.APPLICATIONS_ACCEPTED).availability == MetricAvailability.AVAILABLE

    # 41. status_changes.current: value=None, availability=UNSUPPORTED
    assert get_measure(MetricType.STATUS_CHANGES).value is None
    assert get_measure(MetricType.STATUS_CHANGES).availability == MetricAvailability.UNSUPPORTED

    # 42. Job & Resume current: Job is now AVAILABLE (value 0), Resume is UNSUPPORTED
    assert get_measure(MetricType.JOB_OFFERS_SAVED).value == 0
    assert get_measure(MetricType.JOB_OFFERS_SAVED).availability == MetricAvailability.AVAILABLE
    assert get_measure(MetricType.RESUMES_GENERATED).value is None
    assert get_measure(MetricType.RESUMES_GENERATED).availability == MetricAvailability.UNSUPPORTED


# --- TRACKER AUTOCREATE TESTS ---

async def test_tracker_autocreate_emits_event_system(initialized_db):
    """43 & 45. Verify that resume confirm autocreate emits event with source SYSTEM and retry does not duplicate."""
    from app.routers.resumes import _auto_create_tracker_application

    # 1. Trigger autocreate
    await _auto_create_tracker_application(
        job_id="job-1",
        tailored_resume_id="res-1",
        master_resume_id="res-master",
        job={"company": "Acme", "role": "Engineer"},
        title="Senior Developer",
    )

    async with initialized_db._session() as session:
        events = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].source == MetricEventSource.SYSTEM.value
        assert events[0].to_state == "applied"

    # 2. Retry autocreate (should not duplicate)
    await _auto_create_tracker_application(
        job_id="job-1",
        tailored_resume_id="res-1",
        master_resume_id="res-master",
        job={"company": "Acme", "role": "Engineer"},
        title="Senior Developer",
    )

    async with initialized_db._session() as session:
        events2 = (await session.execute(select(MetricEvent))).scalars().all()
        assert len(events2) == 1


async def test_concurrent_create_integrity_error_during_flush_returns_existing_card(initialized_db):
    """Verify that IntegrityError during session flush in create_application rolls back and returns the concurrently created card."""
    from sqlalchemy.exc import IntegrityError
    from uuid import uuid4

    job_id = "job-conc"
    resume_id = "res-conc"

    original_flush = AsyncSession.flush
    flush_called = False

    async def mock_flush(self, *args, **kwargs):
        nonlocal flush_called
        has_app = any(isinstance(obj, Application) for obj in self.new)
        if has_app and not flush_called:
            flush_called = True
            # Simulate winning concurrent transaction
            async with initialized_db._session() as other_session:
                dup_row = Application(
                    application_id=str(uuid4()),
                    job_id=job_id,
                    resume_id=resume_id,
                    status="saved",
                    status_version=0,
                    lifecycle_token=str(uuid4()),
                    created_at=_now(),
                    updated_at=_now(),
                )
                other_session.add(dup_row)

                # Add ENTITY_CREATED event for the winning card
                from app.services.project_metrics import MetricEventInput, record_metric_event
                event_input = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"create:application:{dup_row.application_id}:lc:{dup_row.lifecycle_token}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.APPLICATION,
                    entity_id=dup_row.application_id,
                    lifecycle_id=dup_row.lifecycle_token,
                    from_state=None,
                    to_state="saved",
                    source=MetricEventSource.USER,
                )
                await record_metric_event(other_session, event_input)
                await other_session.commit()

            raise IntegrityError("mock UNIQUE constraint failed", params=None, orig=None)

        await original_flush(self, *args, **kwargs)

    # Use a helper function or import _now if needed
    from app.database import _now

    with patch.object(AsyncSession, "flush", mock_flush):
        res = await initialized_db.create_application(
            job_id=job_id,
            resume_id=resume_id,
            status="saved",
        )

    # Verify existing card was returned
    assert res is not None
    assert res["job_id"] == job_id
    assert res["resume_id"] == resume_id

    # Verify: one card, one ENTITY_CREATED event, no orphan event
    async with initialized_db._session() as session:
        apps = (await session.execute(select(Application))).scalars().all()
        # Find apps matching job-conc
        matched_apps = [a for a in apps if a.job_id == job_id]
        assert len(matched_apps) == 1

        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_CREATED.value)
        )).scalars().all()
        matched_events = [e for e in events if e.lifecycle_id == matched_apps[0].lifecycle_token]
        assert len(matched_events) == 1


async def test_noop_status_preserves_position_and_column_order(initialized_db):
    """Verify that updating with identical status does not move the card or alter column order."""
    # Create 3 cards in the same column
    a = await initialized_db.create_application(job_id="j-a", resume_id="r-a", status="saved")
    b = await initialized_db.create_application(job_id="j-b", resume_id="r-b", status="saved")
    c = await initialized_db.create_application(job_id="j-c", resume_id="r-c", status="saved")

    # Verify initial positions
    async with initialized_db._session() as session:
        ra = (await session.execute(select(Application).where(Application.application_id == a["application_id"]))).scalar_one()
        rb = (await session.execute(select(Application).where(Application.application_id == b["application_id"]))).scalar_one()
        rc = (await session.execute(select(Application).where(Application.application_id == c["application_id"]))).scalar_one()
        assert ra.position == 0
        assert rb.position == 1
        assert rc.position == 2

    # Update a with identical status (saved)
    await initialized_db.update_application(a["application_id"], {"status": "saved"})

    # Verify positions and order are unchanged
    async with initialized_db._session() as session:
        ra = (await session.execute(select(Application).where(Application.application_id == a["application_id"]))).scalar_one()
        rb = (await session.execute(select(Application).where(Application.application_id == b["application_id"]))).scalar_one()
        rc = (await session.execute(select(Application).where(Application.application_id == c["application_id"]))).scalar_one()
        assert ra.position == 0
        assert rb.position == 1
        assert rc.position == 2


async def test_status_event_error_rolls_back_status_and_version(initialized_db):
    """Verify that an exception inside record_metric_event rolls back status and version update."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    from app.services.project_metrics import record_metric_event as prod_record_metric_event
    async def mock_record_event(session, event_input):
        if event_input.event_type == MetricEventType.APPLICATION_STATUS_CHANGED:
            raise RuntimeError("mock event log error")
        await prod_record_metric_event(session, event_input)

    with patch("app.database.record_metric_event", mock_record_event):
        with pytest.raises(RuntimeError, match="mock event log error"):
            await initialized_db.update_application(app["application_id"], {"status": "applied"})

    # Verify status and version rolled back
    async with initialized_db._session() as session:
        row = (await session.execute(select(Application).where(Application.application_id == app["application_id"]))).scalar_one()
        assert row.status == "saved"
        assert row.status_version == 0

        # Verify no status changed event exists
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_bulk_update_event_error_rolls_back_all_statuses_versions_and_events(initialized_db):
    """Verify that an exception on the second event in bulk update rolls back all updates and events."""
    app1 = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")
    app2 = await initialized_db.create_application(job_id="job-1", resume_id="res-2", status="saved")

    from app.services.project_metrics import record_metric_event as prod_record_metric_event
    calls = 0
    async def mock_record_event(session, event_input):
        nonlocal calls
        if event_input.event_type == MetricEventType.APPLICATION_STATUS_CHANGED:
            calls += 1
            if calls == 2:
                raise RuntimeError("mock bulk update event log error")
        await prod_record_metric_event(session, event_input)

    with patch("app.database.record_metric_event", mock_record_event):
        with pytest.raises(RuntimeError, match="mock bulk update event log error"):
            await initialized_db.bulk_update_applications([app1["application_id"], app2["application_id"]], "applied")

    # Verify all applications rolled back status and version
    async with initialized_db._session() as session:
        r1 = (await session.execute(select(Application).where(Application.application_id == app1["application_id"]))).scalar_one()
        r2 = (await session.execute(select(Application).where(Application.application_id == app2["application_id"]))).scalar_one()
        assert r1.status == "saved"
        assert r1.status_version == 0
        assert r2.status == "saved"
        assert r2.status_version == 0

        # zero status changed events
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 0


async def test_bulk_update_duplicate_ids_updates_once(initialized_db):
    """Verify that bulk update deduplicates IDs and updates once."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    affected = await initialized_db.bulk_update_applications([app["application_id"], app["application_id"]], "applied")
    assert affected == 1

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.APPLICATION_STATUS_CHANGED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_bulk_delete_duplicate_ids_deletes_once(initialized_db):
    """Verify that bulk delete deduplicates IDs and deletes once."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    affected = await initialized_db.bulk_delete_applications([app["application_id"], app["application_id"]])
    assert affected == 1

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_bulk_delete_duplicate_ids_emits_one_delete_event(initialized_db):
    """Verify that bulk delete duplicate IDs emits exactly one delete event."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    await initialized_db.bulk_delete_applications([app["application_id"], app["application_id"]])

    async with initialized_db._session() as session:
        events = (await session.execute(
            select(MetricEvent)
            .where(MetricEvent.event_type == MetricEventType.ENTITY_DELETED.value)
        )).scalars().all()
        assert len(events) == 1


async def test_bulk_delete_duplicate_ids_returns_one(initialized_db):
    """Verify that bulk delete duplicate IDs returns 1 (affected count)."""
    app = await initialized_db.create_application(job_id="job-1", resume_id="res-1", status="saved")

    affected = await initialized_db.bulk_delete_applications([app["application_id"], app["application_id"]])
    assert affected == 1


async def test_api_responses_do_not_leak_internal_fields(initialized_db):
    """Verify that Application responses in POST, PATCH, GET single, and GET list do not leak lifecycle_token or status_version."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app as fastapi_app

    app_data = await initialized_db.create_application(
        job_id="job-api-test",
        resume_id="res-api-test",
        status="saved",
    )
    app_id = app_data["application_id"]

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # POST
        from unittest.mock import AsyncMock
        with patch("app.routers.applications.extract_job_keywords", new_callable=AsyncMock, return_value={"company": "C", "role": "R"}):
            post_resp = await client.post(
                "/api/v1/applications",
                json={"resume_id": "res-api-post", "job_description": "JD", "company": "C", "role": "R"}
            )
        assert post_resp.status_code == 200
        post_body = post_resp.json()
        assert "lifecycle_token" not in post_body
        assert "status_version" not in post_body

        # PATCH
        patch_resp = await client.patch(
            f"/api/v1/applications/{app_id}",
            json={"notes": "updated notes"}
        )
        assert patch_resp.status_code == 200
        patch_body = patch_resp.json()
        assert "lifecycle_token" not in patch_body
        assert "status_version" not in patch_body

        # GET single
        get_single_resp = await client.get(f"/api/v1/applications/{app_id}")
        assert get_single_resp.status_code == 200
        get_single_body = get_single_resp.json()
        assert "lifecycle_token" not in get_single_body
        assert "status_version" not in get_single_body

        # GET list
        get_list_resp = await client.get("/api/v1/applications")
        assert get_list_resp.status_code == 200
        get_list_body = get_list_resp.json()
        columns = get_list_body["columns"]
        for status_list in columns.values():
            for card in status_list:
                assert "lifecycle_token" not in card
                assert "status_version" not in card
