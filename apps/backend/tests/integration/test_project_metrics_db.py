import pytest
from sqlalchemy import select, update, delete
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from app.models import MetricEvent, MetricTrackingState
from app.services.project_metrics import (
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    MetricAvailability,
    MetricType,
    MetricEventInput,
    RecordEventStatus,
    record_metric_event,
    build_metrics_report,
    initialize_metric_tracking_state,
    MetricsTrackingNotInitializedError,
)


@pytest.fixture
def clean_metrics_input():
    return MetricEventInput(
        event_id="evt-123",
        operation_key="op-key-123",
        event_type=MetricEventType.ENTITY_CREATED,
        entity_type=MetricEntityType.APPLICATION,
        entity_id="app-123",
        lifecycle_id="lc-123",
        to_state="saved",
    )


# --- DB & Schema tests ---

async def test_schema_tables_exist(isolated_db):
    """Verify that metric_events and metric_tracking_state tables exist in DB."""
    async with isolated_db._session() as session:
        res_state = await session.execute(select(MetricTrackingState))
        assert res_state.scalars().all() == []

        res_events = await session.execute(select(MetricEvent))
        assert res_events.scalars().all() == []


# --- Append-Only Triggers tests ---

async def test_trigger_metric_events_no_update_is_prohibited(isolated_db, clean_metrics_input):
    """Verify that updates on metric_events table raise an exception via SQLite triggers."""
    async with isolated_db._session() as session:
        res = await record_metric_event(session, clean_metrics_input)
        assert res.status == RecordEventStatus.RECORDED
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                update(MetricEvent)
                .where(MetricEvent.event_id == "evt-123")
                .values(source="SYSTEM")
            )
            await session.commit()


async def test_trigger_metric_events_no_delete_is_prohibited(isolated_db, clean_metrics_input):
    """Verify that deletes on metric_events table raise an exception via SQLite triggers."""
    async with isolated_db._session() as session:
        res = await record_metric_event(session, clean_metrics_input)
        assert res.status == RecordEventStatus.RECORDED
        await session.commit()

    async with isolated_db._session() as session:
        with pytest.raises((OperationalError, DBAPIError)):
            await session.execute(
                delete(MetricEvent).where(MetricEvent.event_id == "evt-123")
            )
            await session.commit()


# --- Idempotency & Conflict tests ---

async def test_duplicate_operation_key_does_not_poison_transaction(isolated_db, clean_metrics_input):
    """Verify that duplicate operation_key does not poison the transaction."""
    async with isolated_db._session() as session:
        res1 = await record_metric_event(session, clean_metrics_input)
        assert res1.status == RecordEventStatus.RECORDED

        # Insert same operation_key, different event_id
        duplicate_input = MetricEventInput(
            event_id="evt-456",
            operation_key="op-key-123",  # duplicate operation_key
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-123",
            lifecycle_id="lc-123",
            to_state="saved",
        )
        res2 = await record_metric_event(session, duplicate_input)
        assert res2.status == RecordEventStatus.DUPLICATE
        assert res2.sequence_id is None

        # Check session is still usable and event was recorded
        events = await session.execute(select(MetricEvent))
        assert len(events.scalars().all()) == 1
        await session.commit()


async def test_duplicate_event_id_does_not_poison_transaction(isolated_db, clean_metrics_input):
    """Verify that duplicate event_id does not poison the transaction."""
    async with isolated_db._session() as session:
        res1 = await record_metric_event(session, clean_metrics_input)
        assert res1.status == RecordEventStatus.RECORDED

        # Insert same event_id, different operation_key
        duplicate_input = MetricEventInput(
            event_id="evt-123",  # duplicate event_id
            operation_key="op-key-456",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-123",
            lifecycle_id="lc-123",
            to_state="saved",
        )
        res2 = await record_metric_event(session, duplicate_input)
        assert res2.status == RecordEventStatus.DUPLICATE
        assert res2.sequence_id is None

        # Check session is still usable and event was recorded
        events = await session.execute(select(MetricEvent))
        assert len(events.scalars().all()) == 1
        await session.commit()


async def test_record_metric_event_does_not_commit_automatically(isolated_db, clean_metrics_input):
    """Verify that record_metric_event does not commit automatically."""
    async with isolated_db._session() as session:
        await record_metric_event(session, clean_metrics_input)
        await session.close()

    async with isolated_db._session() as session:
        events = await session.execute(select(MetricEvent))
        assert len(events.scalars().all()) == 0


async def test_record_metric_event_commits_with_parent_session(isolated_db, clean_metrics_input):
    """Verify that event commits with the parent session."""
    async with isolated_db._session() as session:
        await record_metric_event(session, clean_metrics_input)
        await session.commit()

    async with isolated_db._session() as session:
        events = await session.execute(select(MetricEvent))
        assert len(events.scalars().all()) == 1


async def test_rollback_parent_session_rolls_back_event(isolated_db, clean_metrics_input):
    """Verify rollback works for metric events."""
    async with isolated_db._session() as session:
        await record_metric_event(session, clean_metrics_input)
        await session.rollback()

    async with isolated_db._session() as session:
        events = await session.execute(select(MetricEvent))
        assert len(events.scalars().all()) == 0


async def test_sequence_id_auto_increments(isolated_db, clean_metrics_input):
    e1 = clean_metrics_input
    e2 = MetricEventInput(
        event_id="evt-456",
        operation_key="op-key-456",
        event_type=MetricEventType.ENTITY_CREATED,
        entity_type=MetricEntityType.APPLICATION,
        entity_id="app-123",
        lifecycle_id="lc-123",
        to_state="saved",
    )

    async with isolated_db._session() as session:
        res1 = await record_metric_event(session, e1)
        res2 = await record_metric_event(session, e2)
        await session.commit()

        assert res1.sequence_id is not None
        assert res2.sequence_id is not None
        assert res2.sequence_id == res1.sequence_id + 1


# --- Tracking State Singleton & Error tests ---

async def test_build_report_without_tracking_state_raises_error(isolated_db):
    """Verify build_metrics_report raises MetricsTrackingNotInitializedError if state is missing."""
    async with isolated_db._session() as session:
        with pytest.raises(MetricsTrackingNotInitializedError):
            await build_metrics_report(session)


async def test_initialize_metric_tracking_state_is_idempotent(isolated_db):
    """Verify idempotent initialization of metric tracking state."""
    async with isolated_db._session() as session:
        state1 = await initialize_metric_tracking_state(session, historical_complete=False)
        state2 = await initialize_metric_tracking_state(session, historical_complete=True)
        await session.commit()

        assert state1.id == 1
        assert state2.id == 1
        assert state2.historical_complete is False  # Not overwritten

        states = (await session.execute(select(MetricTrackingState))).scalars().all()
        assert len(states) == 1


async def test_initialize_metric_tracking_state_does_not_commit(isolated_db):
    """Verify that initialize_metric_tracking_state does not commit automatically."""
    async with isolated_db._session() as session:
        await initialize_metric_tracking_state(session, historical_complete=True)
        await session.close()

    async with isolated_db._session() as session:
        states = (await session.execute(select(MetricTrackingState))).scalars().all()
        assert len(states) == 0


async def test_tracking_started_at_remains_stable(isolated_db):
    """Verify tracking_started_at timestamp remains stable and is not overwritten."""
    async with isolated_db._session() as session:
        await initialize_metric_tracking_state(session, historical_complete=True, tracking_started_at="2026-07-16T12:00:00Z")
        await session.commit()

    async with isolated_db._session() as session:
        state2 = await initialize_metric_tracking_state(session, historical_complete=True, tracking_started_at="2026-07-16T13:00:00Z")
        assert state2.tracking_started_at == "2026-07-16T12:00:00Z"


async def test_second_tracking_state_with_id_not_1_is_blocked(isolated_db):
    """Verify that check constraint on MetricTrackingState blocks id != 1."""
    async with isolated_db._session() as session:
        state = MetricTrackingState(
            id=2,  # id != 1
            schema_version=1,
            tracking_started_at="2026-07-16T12:00:00Z",
            historical_complete=True,
        )
        session.add(state)
        with pytest.raises((IntegrityError, OperationalError, DBAPIError)):
            await session.commit()


# --- Database Constraints checks ---

async def test_database_check_constraints_prevent_invalid_event_type(isolated_db):
    async with isolated_db._session() as session:
        # direct insert of invalid event_type bypassing input validation
        evt = MetricEvent(
            event_id="evt-1",
            operation_key="op-1",
            event_type="INVALID_TYPE",
            entity_type="APPLICATION",
            entity_id="app-1",
            lifecycle_id="lc-1",
            occurred_at="2026-07-16T12:00:00Z",
            recorded_at="2026-07-16T12:00:00Z",
        )
        session.add(evt)
        with pytest.raises((IntegrityError, OperationalError, DBAPIError)):
            await session.commit()


async def test_database_check_constraints_prevent_invalid_entity_type(isolated_db):
    async with isolated_db._session() as session:
        evt = MetricEvent(
            event_id="evt-1",
            operation_key="op-1",
            event_type="ENTITY_CREATED",
            entity_type="INVALID_ENTITY",
            entity_id="app-1",
            lifecycle_id="lc-1",
            occurred_at="2026-07-16T12:00:00Z",
            recorded_at="2026-07-16T12:00:00Z",
        )
        session.add(evt)
        with pytest.raises((IntegrityError, OperationalError, DBAPIError)):
            await session.commit()


async def test_database_check_constraints_prevent_invalid_source(isolated_db):
    async with isolated_db._session() as session:
        evt = MetricEvent(
            event_id="evt-1",
            operation_key="op-1",
            event_type="ENTITY_CREATED",
            entity_type="APPLICATION",
            entity_id="app-1",
            lifecycle_id="lc-1",
            occurred_at="2026-07-16T12:00:00Z",
            recorded_at="2026-07-16T12:00:00Z",
            source="INVALID_SOURCE",
        )
        session.add(evt)
        with pytest.raises((IntegrityError, OperationalError, DBAPIError)):
            await session.commit()


async def test_metadata_json_is_defensively_copied(isolated_db):
    meta = {"key": "val"}
    event_input = MetricEventInput(
        event_id="evt-1",
        operation_key="op-1",
        event_type=MetricEventType.ENTITY_CREATED,
        entity_type=MetricEntityType.APPLICATION,
        entity_id="app-1",
        lifecycle_id="lc-1",
        to_state="saved",
        metadata_json=meta,
    )

    async with isolated_db._session() as session:
        await record_metric_event(session, event_input)
        await session.commit()

        # Mutate local metadata dictionary
        meta["key"] = "mutated"

        # Check that stored event metadata has the original value
        res = await session.execute(select(MetricEvent).where(MetricEvent.event_id == "evt-1"))
        stored_event = res.scalar_one()
        assert stored_event.metadata_json["key"] == "val"


async def test_tracking_state_concurrent_initialization(isolated_db):
    """Test that two parallel/sequential initializations do not crash or duplicate."""
    async with isolated_db._session() as s1:
        await initialize_metric_tracking_state(s1, historical_complete=True, tracking_started_at="2026-07-16T12:00:00Z")
        await s1.commit()

    async with isolated_db._session() as s2:
        await initialize_metric_tracking_state(s2, historical_complete=False, tracking_started_at="2026-07-16T13:00:00Z")
        await s2.commit()

    async with isolated_db._session() as session:
        states = (await session.execute(select(MetricTrackingState))).scalars().all()
        assert len(states) == 1
        assert states[0].historical_complete is True
        assert states[0].tracking_started_at == "2026-07-16T12:00:00Z"
