"""Stage 10B database opens additively under Stage 10C."""

from app.db_engine import init_models_sync, make_sync_engine


def test_stage10b_database_adds_generation_table_without_rebuilding_history(tmp_path):
    engine = make_sync_engine(tmp_path / "stage10b.db")
    try:
        init_models_sync(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE cv_transformation_generations")
            connection.exec_driver_sql(
                """
                INSERT INTO jobs (job_id, content, resume_id, created_at, metadata_json, lifecycle_token)
                VALUES ('job-example', 'Anonymous job', NULL, '2026-07-19T00:00:00+00:00', '{}', 'job-lifecycle')
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO cv_transformation_plan_approvals
                (approval_id, plan_version, plan_fingerprint, resume_id, job_id, status,
                 decisions, guardrails_acknowledged, created_at, updated_at)
                VALUES ('approval-example', '1.1', ?, 'resume-example', 'job-example',
                        'APPROVED', '[]', 1, '2026-07-19T00:00:00+00:00',
                        '2026-07-19T00:00:00+00:00')
                """,
                ("a" * 64,),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO metric_events
                (event_id, operation_key, event_type, entity_type, entity_id, lifecycle_id,
                 occurred_at, recorded_at, source, metadata_json)
                VALUES ('event-example', 'operation-example', 'JOB_ANALYZED', 'JOB',
                        'job-example', 'job-lifecycle', '2026-07-19T00:00:00+00:00',
                        '2026-07-19T00:00:00+00:00', 'SYSTEM', '{}')
                """
            )

        with engine.connect() as connection:
            approval_before = connection.exec_driver_sql(
                "SELECT * FROM cv_transformation_plan_approvals"
            ).mappings().one()
            event_before = connection.exec_driver_sql(
                "SELECT * FROM metric_events"
            ).mappings().one()

        init_models_sync(engine)
        init_models_sync(engine)

        with engine.connect() as connection:
            tables = {
                row["name"]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).mappings()
            }
            indexes = {
                row["name"]
                for row in connection.exec_driver_sql(
                    "PRAGMA index_list(cv_transformation_generations)"
                ).mappings()
            }
            approval_after = connection.exec_driver_sql(
                "SELECT * FROM cv_transformation_plan_approvals"
            ).mappings().one()
            event_after = connection.exec_driver_sql(
                "SELECT * FROM metric_events"
            ).mappings().one()
            generation_count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM cv_transformation_generations"
            ).scalar_one()

        assert "cv_transformation_generations" in tables
        assert {
            "sqlite_autoindex_cv_transformation_generations_2",
            "ix_generation_current_scope",
            "ix_cv_transformation_generations_status",
        }.issubset(indexes)
        assert dict(approval_after) == dict(approval_before)
        assert dict(event_after) == dict(event_before)
        assert generation_count == 0
    finally:
        engine.dispose()
