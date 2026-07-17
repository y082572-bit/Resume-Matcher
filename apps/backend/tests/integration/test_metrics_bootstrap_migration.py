import pytest
from datetime import datetime, timezone
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import MetricBootstrapState
from app.db_engine import init_models_sync


async def test_fresh_database_contains_bootstrap_table(isolated_db):
    """1. Fresh database contains metric_bootstrap_state table."""
    async with isolated_db._session() as session:
        res = await session.execute(select(MetricBootstrapState))
        rows = res.scalars().all()
        assert len(rows) == 0


def test_migration_adds_bootstrap_table_to_existing_db(isolated_db):
    """2. Existing database receives the table after init_models_sync."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    # Drop the table to simulate an older database version without it
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS metric_bootstrap_state")

    # Run init_models_sync (re-create missing tables)
    init_models_sync(engine)

    # Verify table exists
    with engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info(metric_bootstrap_state)").mappings().all()
        assert len(cols) > 0
        col_names = {c["name"] for c in cols}
        assert "bootstrap_key" in col_names


def test_table_has_required_columns(isolated_db):
    """3. Table has all required columns with correct types."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    with engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info(metric_bootstrap_state)").mappings().all()
        col_map = {c["name"]: c for c in cols}

        assert "bootstrap_key" in col_map
        assert "schema_version" in col_map
        assert "baseline_at" in col_map
        assert "completed_at" in col_map
        assert "history_completeness" in col_map
        assert "baseline_event_count" in col_map
        assert "database_was_empty" in col_map


async def test_bootstrap_key_is_primary_key(isolated_db):
    """4. bootstrap_key is primary key (unique)."""
    async with isolated_db._session() as session:
        state1 = MetricBootstrapState(
            bootstrap_key="key1",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state1)
        await session.commit()

    # Try inserting another record with the same key using a fresh session
    async with isolated_db._session() as session:
        state2 = MetricBootstrapState(
            bootstrap_key="key1",
            schema_version=2,
            baseline_at="2026-07-16T21:45:00Z",
            completed_at="2026-07-16T21:45:00Z",
            history_completeness="PARTIAL_BASELINE",
            baseline_event_count=5,
            database_was_empty=False,
        )
        session.add(state2)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_schema_version_not_null(isolated_db):
    """5. schema_version cannot be NULL."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="key_null_schema",
            schema_version=None,  # type: ignore
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_baseline_at_not_null(isolated_db):
    """6. baseline_at cannot be NULL."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="key_null_baseline",
            schema_version=1,
            baseline_at=None,  # type: ignore
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_completed_at_not_null(isolated_db):
    """7. completed_at cannot be NULL."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="key_null_completed",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at=None,  # type: ignore
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_history_completeness_not_null(isolated_db):
    """8. history_completeness cannot be NULL."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="key_null_completeness",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness=None,  # type: ignore
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_baseline_event_count_not_null(isolated_db):
    """9. baseline_event_count cannot be NULL."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="key_null_count",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=None,  # type: ignore
            database_was_empty=True,
        )
        session.add(state)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_database_was_empty_not_null(isolated_db):
    """10. database_was_empty cannot be NULL."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="key_null_empty",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=None,  # type: ignore
        )
        session.add(state)
        with pytest.raises(IntegrityError):
            await session.commit()


def test_init_db_is_idempotent(isolated_db):
    """11. Re-running database initialization is idempotent and does not crash."""
    isolated_db._ensure_initialized()
    engine = isolated_db._sync_engine

    # 1. Run init_models_sync first
    init_models_sync(engine)

    # Insert a project_metrics_v1 record to verify it is preserved after subsequent init_models_sync
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        state = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=10,
            database_was_empty=True,
        )
        session.add(state)
        session.commit()

    # 2. Run init_models_sync again (second call)
    init_models_sync(engine)

    # Explicit checks:
    # A. Check table exists
    with engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info(metric_bootstrap_state)").mappings().all()
        assert len(cols) > 0, "Table metric_bootstrap_state should still exist"

        # B. Check required columns still exist
        col_names = {c["name"] for c in cols}
        expected_cols = {
            "bootstrap_key", "schema_version", "baseline_at", "completed_at",
            "history_completeness", "baseline_event_count", "database_was_empty"
        }
        assert expected_cols.issubset(col_names), f"Missing columns in metric_bootstrap_state: {expected_cols - col_names}"

        # C. Check that no duplicate tables/views were created (only one table exists)
        table_list = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metric_bootstrap_state'"
        ).scalars().all()
        assert len(table_list) == 1, "Only one table 'metric_bootstrap_state' should exist"

    # D. Check if record 'project_metrics_v1' still exists, is unique, and values are unchanged
    with Session(engine) as session:
        records = session.query(MetricBootstrapState).filter_by(bootstrap_key="project_metrics_v1").all()
        assert len(records) == 1, "Exactly one 'project_metrics_v1' record must exist after init_models_sync"
        record = records[0]
        assert record.schema_version == 1
        assert record.baseline_at == "2026-07-16T21:40:00Z"
        assert record.completed_at == "2026-07-16T21:40:00Z"
        assert record.history_completeness == "COMPLETE"
        assert record.baseline_event_count == 10
        assert record.database_was_empty is True


async def test_duplicate_bootstrap_key_blocked(isolated_db):
    """12. Inserting two distinct rows with same bootstrap_key fails due to PK constraint."""
    async with isolated_db._session() as session:
        s1 = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(s1)
        await session.commit()

    async with isolated_db._session() as session:
        s2 = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=1,
            baseline_at="2026-07-16T21:50:00Z",
            completed_at="2026-07-16T21:50:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_existing_record_preserved_on_init_db(isolated_db):
    """13. Existing bootstrap state record is preserved on subsequent db initializations."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=10,
            database_was_empty=True,
        )
        session.add(state)
        await session.commit()

    isolated_db._ensure_initialized()
    init_models_sync(isolated_db._sync_engine)

    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "project_metrics_v1")
        )
        row = res.scalar_one()
        assert row.baseline_event_count == 10


async def test_no_duplicate_v1_records(isolated_db):
    """14. Confirm schema constraints enforce single key record."""
    async with isolated_db._session() as session:
        s1 = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=1,
            baseline_at="2026",
            completed_at="2026",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(s1)
        await session.commit()

    # Try to insert a second record for v1 in a separate session context
    async with isolated_db._session() as session:
        s2 = MetricBootstrapState(
            bootstrap_key="project_metrics_v1",
            schema_version=1,
            baseline_at="2026-2",
            completed_at="2026-2",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_times_serialization_format(isolated_db):
    """15. Time columns successfully accept and preserve ISO-8601 UTC standard strings."""
    now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="format_test",
            schema_version=1,
            baseline_at=now_str,
            completed_at=now_str,
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        await session.commit()

        res = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "format_test")
        )
        row = res.scalar_one()
        assert row.baseline_at == now_str
        assert row.completed_at == now_str


async def test_trigger_prevents_updates(isolated_db):
    """16. Trigger prevents UPDATE statements on metric_bootstrap_state."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="immutable_test",
            schema_version=1,
            baseline_at="2026",
            completed_at="2026",
            history_completeness="COMPLETE",
            baseline_event_count=0,
            database_was_empty=True,
        )
        session.add(state)
        await session.commit()

    # Try updating the row
    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "immutable_test")
        )
        row = res.scalar_one()
        row.baseline_event_count = 100
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "Updates on metric_bootstrap_state are prohibited" in str(exc_info.value)


async def test_trigger_prevents_deletes_and_updates(isolated_db):
    """17. Trigger prevents UPDATE and DELETE statements on metric_bootstrap_state, keeping records unchanged."""
    async with isolated_db._session() as session:
        state = MetricBootstrapState(
            bootstrap_key="immutability_delete_test",
            schema_version=1,
            baseline_at="2026-07-16T21:40:00Z",
            completed_at="2026-07-16T21:40:00Z",
            history_completeness="COMPLETE",
            baseline_event_count=5,
            database_was_empty=True,
        )
        session.add(state)
        await session.commit()

    # 1. Test update is blocked
    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "immutability_delete_test")
        )
        row = res.scalar_one()
        row.baseline_event_count = 99
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "Updates on metric_bootstrap_state are prohibited" in str(exc_info.value)

    # 2. Test delete is blocked
    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "immutability_delete_test")
        )
        row = res.scalar_one()
        await session.delete(row)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()
        assert "Deletes on metric_bootstrap_state are prohibited" in str(exc_info.value)

    # 3. Verify record remains unchanged in DB
    async with isolated_db._session() as session:
        res = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "immutability_delete_test")
        )
        row = res.scalar_one()
        assert row.baseline_event_count == 5
        assert row.schema_version == 1
