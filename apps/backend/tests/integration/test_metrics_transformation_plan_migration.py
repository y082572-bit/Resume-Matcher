"""Migration coverage for the pre-Stage-10B metric_events schema."""

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db_engine import init_models_sync, make_sync_engine


OLD_EVENT_TYPES = (
    "'BASELINE', 'ENTITY_CREATED', 'ENTITY_DELETED', 'RESUME_GENERATED', "
    "'JOB_ANALYZED', 'APPLICATION_STATUS_CHANGED'"
)


def _insert_event(conn, *, sequence_id, event_id, operation_key, event_type="BASELINE"):
    marker = sequence_id % 10 if sequence_id is not None else 0
    conn.exec_driver_sql(
        """
        INSERT INTO metric_events (
            sequence_id, event_id, operation_key, event_type, entity_type,
            entity_id, lifecycle_id, from_state, to_state, occurred_at,
            recorded_at, source, metadata_json
        ) VALUES (?, ?, ?, ?, 'JOB', ?, ?, NULL, NULL, ?, ?, 'SYSTEM', '{}')
        """,
        (
            sequence_id,
            event_id,
            operation_key,
            event_type,
            f"job-{sequence_id or 'next'}",
            f"lifecycle-{sequence_id or 'next'}",
            f"2026-07-19T10:00:0{marker}+00:00",
            f"2026-07-19T10:00:0{marker}+00:00",
        ),
    )


def _create_old_metric_events(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            CREATE TABLE metric_events (
                sequence_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                event_id VARCHAR NOT NULL UNIQUE,
                operation_key VARCHAR NOT NULL UNIQUE,
                event_type VARCHAR NOT NULL
                    CONSTRAINT ck_metric_events_event_type
                    CHECK (event_type IN ({OLD_EVENT_TYPES})),
                entity_type VARCHAR NOT NULL
                    CONSTRAINT ck_metric_events_entity_type
                    CHECK (entity_type IN ('RESUME', 'JOB', 'APPLICATION')),
                entity_id VARCHAR NOT NULL,
                lifecycle_id VARCHAR NOT NULL,
                from_state VARCHAR,
                to_state VARCHAR,
                occurred_at VARCHAR NOT NULL,
                recorded_at VARCHAR NOT NULL,
                source VARCHAR NOT NULL
                    CONSTRAINT ck_metric_events_source
                    CHECK (source IN ('USER', 'SYSTEM', 'BOOTSTRAP')),
                metadata_json JSON NOT NULL
            )
            """
        )
        for column in ("event_type", "entity_type", "entity_id", "lifecycle_id"):
            conn.exec_driver_sql(
                f"CREATE INDEX ix_metric_events_{column} ON metric_events ({column})"
            )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER metric_events_no_update
            BEFORE UPDATE ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'Updates on metric_events are prohibited');
            END
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER metric_events_no_delete
            BEFORE DELETE ON metric_events
            BEGIN
                SELECT RAISE(FAIL, 'Deletes on metric_events are prohibited');
            END
            """
        )
        _insert_event(
            conn, sequence_id=2, event_id="history-2", operation_key="history-op-2"
        )
        _insert_event(
            conn,
            sequence_id=5,
            event_id="history-5",
            operation_key="history-op-5",
            event_type="JOB_ANALYZED",
        )
        _insert_event(
            conn,
            sequence_id=9,
            event_id="history-9",
            operation_key="history-op-9",
            event_type="APPLICATION_STATUS_CHANGED",
        )


def _rows(engine):
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.exec_driver_sql(
                "SELECT * FROM metric_events ORDER BY sequence_id"
            ).mappings()
        ]


def test_old_metric_events_migration_preserves_history_constraints_and_triggers(tmp_path):
    engine = make_sync_engine(tmp_path / "old-metrics.db")
    try:
        _create_old_metric_events(engine)
        historical_before = _rows(engine)

        init_models_sync(engine)

        historical_after = _rows(engine)
        assert historical_after == historical_before
        assert [row["sequence_id"] for row in historical_after] == [2, 5, 9]

        with engine.begin() as conn:
            _insert_event(
                conn,
                sequence_id=None,
                event_id="decision-event",
                operation_key="transformation_plan_decision:approval-example",
                event_type="TRANSFORMATION_PLAN_DECIDED",
            )
        with engine.connect() as conn:
            next_sequence = conn.exec_driver_sql(
                "SELECT sequence_id FROM metric_events WHERE event_id = 'decision-event'"
            ).scalar_one()
            indexes = {
                row["name"]
                for row in conn.exec_driver_sql("PRAGMA index_list(metric_events)").mappings()
            }
            triggers = {
                row["name"]
                for row in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'metric_events'"
                ).mappings()
            }
        assert next_sequence > 9
        assert {
            "ix_metric_events_event_type",
            "ix_metric_events_entity_type",
            "ix_metric_events_entity_id",
            "ix_metric_events_lifecycle_id",
        }.issubset(indexes)
        assert {"metric_events_no_update", "metric_events_no_delete"}.issubset(triggers)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _insert_event(
                conn,
                sequence_id=None,
                event_id="decision-event",
                operation_key="unique-operation",
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _insert_event(
                conn,
                sequence_id=None,
                event_id="unique-event",
                operation_key="transformation_plan_decision:approval-example",
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _insert_event(
                conn,
                sequence_id=None,
                event_id="unknown-event",
                operation_key="unknown-operation",
                event_type="UNKNOWN_EVENT",
            )
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE metric_events SET source = 'USER' WHERE event_id = 'history-2'"
            )
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM metric_events WHERE event_id = 'history-2'")

        before_second_start = _rows(engine)
        init_models_sync(engine)
        assert _rows(engine) == before_second_start
        assert _rows(engine)[:3] == historical_before
    finally:
        engine.dispose()
