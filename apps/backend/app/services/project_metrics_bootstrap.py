"""Project Metrics Collector v1 bootstrap service.

Handles one-time historical baseline events emission and tracking initialization.
"""

import logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.models import Job, Resume, Application, Improvement, MetricEvent, MetricBootstrapState, MetricTrackingState
from app.services.project_metrics import (
    MetricEventInput,
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    record_metric_event,
    RecordEventStatus,
    MetricAggregationError,
    initialize_metric_tracking_state,
)

logger = logging.getLogger(__name__)


async def bootstrap_metrics(db: Database) -> None:
    """Initialize historical metrics events and tracking state atomically."""
    async with db._session() as session:
        # Check if bootstrap already completed
        res_state = await session.execute(
            select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "project_metrics_v1")
        )
        existing_state = res_state.scalar_one_or_none()
        if existing_state is not None:
            if existing_state.schema_version != 1:
                raise MetricAggregationError(
                    f"Unsupported bootstrap schema version: {existing_state.schema_version}. Expected 1."
                )
            logger.info("Bootstrap project_metrics_v1 already completed. Skipping.")
            return

        res_track = await session.execute(
            select(MetricTrackingState).where(MetricTrackingState.id == 1)
        )
        track_state = res_track.scalar_one_or_none()

        # Start transaction
        try:
            # 1. Fetch domain data
            res_jobs = await session.execute(select(Job))
            jobs = res_jobs.scalars().all()

            res_resumes = await session.execute(select(Resume))
            resumes = res_resumes.scalars().all()

            res_apps = await session.execute(select(Application))
            apps = res_apps.scalars().all()

            res_imp = await session.execute(select(Improvement.job_id).distinct())
            job_ids_with_improvements = set(res_imp.scalars().all())

            # Query existing MetricEvents to build deduplication filters
            res_events = await session.execute(
                select(
                    MetricEvent.operation_key,
                    MetricEvent.event_type,
                    MetricEvent.entity_type,
                    MetricEvent.lifecycle_id
                )
            )
            existing_events = res_events.all()

            existing_events_by_key = {row.operation_key for row in existing_events}
            existing_events_by_lc = set()
            has_status_events = set()
            for row in existing_events:
                existing_events_by_lc.add((row.event_type, row.entity_type, row.lifecycle_id))
                if row.entity_type == "APPLICATION" and row.event_type in ("APPLICATION_STATUS_CHANGED", "BASELINE"):
                    has_status_events.add(row.lifecycle_id)

            # Query counts to determine if DB was empty
            res_job_cnt = await session.execute(select(func.count()).select_from(Job))
            job_cnt = res_job_cnt.scalar() or 0

            res_resume_cnt = await session.execute(select(func.count()).select_from(Resume))
            resume_cnt = res_resume_cnt.scalar() or 0

            res_app_cnt = await session.execute(select(func.count()).select_from(Application))
            app_cnt = res_app_cnt.scalar() or 0

            res_imp_cnt = await session.execute(select(func.count()).select_from(Improvement))
            imp_cnt = res_imp_cnt.scalar() or 0

            res_evt_cnt = await session.execute(select(func.count()).select_from(MetricEvent))
            evt_cnt = res_evt_cnt.scalar() or 0

            database_was_empty = (
                job_cnt == 0 and
                resume_cnt == 0 and
                app_cnt == 0 and
                imp_cnt == 0 and
                evt_cnt == 0
            )
            history_completeness = "COMPLETE" if database_was_empty else "PARTIAL_BASELINE"

            # 3. Create baseline timestamp
            baseline_dt = datetime.now(timezone.utc)
            baseline_str = baseline_dt.isoformat().replace("+00:00", "Z")

            if track_state is None:
                await initialize_metric_tracking_state(
                    session,
                    historical_complete=database_was_empty,
                    tracking_started_at=baseline_str,
                )
            secondary_dt = baseline_dt + timedelta(seconds=1)
            secondary_str = secondary_dt.isoformat().replace("+00:00", "Z")

            # Collect events to record
            entity_created_events = []
            secondary_events = []

            # Helpers to avoid duplicate event generation in memory
            generated_operation_keys = set()

            def add_event(evt_input: MetricEventInput, is_created: bool):
                # Verify operation_key uniqueness in this run
                if evt_input.operation_key in generated_operation_keys:
                    return
                generated_operation_keys.add(evt_input.operation_key)

                # Skip if we already have this event type for the lifecycle (meaning it's not a missing baseline)
                if (evt_input.event_type.value, evt_input.entity_type.value, evt_input.lifecycle_id) in existing_events_by_lc:
                    if evt_input.operation_key not in existing_events_by_key:
                        return

                # For baseline status, if lifecycle already has status events, skip it
                if evt_input.event_type == MetricEventType.BASELINE and evt_input.lifecycle_id in has_status_events:
                    if evt_input.operation_key not in existing_events_by_key:
                        return

                if is_created:
                    entity_created_events.append(evt_input)
                else:
                    secondary_events.append(evt_input)

            # A. Jobs
            for job in jobs:
                jid = job.job_id
                lc = job.lifecycle_token
                # ENTITY_CREATED
                evt = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"bootstrap:v1:create:job:{jid}:lc:{lc}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.JOB,
                    entity_id=jid,
                    lifecycle_id=lc,
                    occurred_at=baseline_str,
                    source=MetricEventSource.BOOTSTRAP,
                )
                add_event(evt, is_created=True)

                # JOB_ANALYZED
                if jid in job_ids_with_improvements:
                    evt = MetricEventInput(
                        event_id=str(uuid4()),
                        operation_key=f"bootstrap:v1:analyze:job:{jid}:lc:{lc}",
                        event_type=MetricEventType.JOB_ANALYZED,
                        entity_type=MetricEntityType.JOB,
                        entity_id=jid,
                        lifecycle_id=lc,
                        occurred_at=secondary_str,
                        source=MetricEventSource.BOOTSTRAP,
                    )
                    add_event(evt, is_created=False)

            # B. Resumes
            for resume in resumes:
                rid = resume.resume_id
                lc = resume.lifecycle_token
                # ENTITY_CREATED
                evt = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"bootstrap:v1:create:resume:{rid}:lc:{lc}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.RESUME,
                    entity_id=rid,
                    lifecycle_id=lc,
                    occurred_at=baseline_str,
                    source=MetricEventSource.BOOTSTRAP,
                )
                add_event(evt, is_created=True)

                # RESUME_GENERATED
                if resume.parent_id is not None:
                    evt = MetricEventInput(
                        event_id=str(uuid4()),
                        operation_key=f"bootstrap:v1:generate:resume:{rid}:lc:{lc}",
                        event_type=MetricEventType.RESUME_GENERATED,
                        entity_type=MetricEntityType.RESUME,
                        entity_id=rid,
                        lifecycle_id=lc,
                        occurred_at=secondary_str,
                        source=MetricEventSource.BOOTSTRAP,
                    )
                    add_event(evt, is_created=False)

            # C. Applications
            for app in apps:
                aid = app.application_id
                lc = app.lifecycle_token
                # ENTITY_CREATED
                evt = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"bootstrap:v1:create:application:{aid}:lc:{lc}",
                    event_type=MetricEventType.ENTITY_CREATED,
                    entity_type=MetricEntityType.APPLICATION,
                    entity_id=aid,
                    lifecycle_id=lc,
                    from_state=None,
                    to_state=None,
                    occurred_at=baseline_str,
                    source=MetricEventSource.BOOTSTRAP,
                )
                add_event(evt, is_created=True)

                # BASELINE status event
                evt = MetricEventInput(
                    event_id=str(uuid4()),
                    operation_key=f"bootstrap:v1:status:application:{aid}:lc:{lc}:to:{app.status}",
                    event_type=MetricEventType.BASELINE,
                    entity_type=MetricEntityType.APPLICATION,
                    entity_id=aid,
                    lifecycle_id=lc,
                    from_state=None,
                    to_state=app.status,
                    occurred_at=secondary_str,
                    source=MetricEventSource.BOOTSTRAP,
                )
                add_event(evt, is_created=False)

            # 4. Insert events in order (first entity_created, then secondary)
            recorded_count = 0
            all_events = entity_created_events + secondary_events
            for evt in all_events:
                res = await record_metric_event(session, evt)
                if res.status == RecordEventStatus.RECORDED:
                    recorded_count += 1
                elif res.status == RecordEventStatus.DUPLICATE:
                    # Query the event by operation_key to verify it exists and is correct
                    db_evt_res = await session.execute(
                        select(MetricEvent).where(MetricEvent.operation_key == evt.operation_key)
                    )
                    db_evt = db_evt_res.scalar_one_or_none()
                    if db_evt is None:
                        raise MetricAggregationError(
                            f"Duplicate event status returned but event with operation_key '{evt.operation_key}' "
                            f"does not exist. Likely a conflict on event_id or another constraint."
                        )
                    # Verify fields match
                    if (db_evt.event_type != evt.event_type.value or
                        db_evt.entity_type != evt.entity_type.value or
                        db_evt.entity_id != evt.entity_id or
                        db_evt.lifecycle_id != evt.lifecycle_id or
                        db_evt.source != evt.source.value):
                        raise MetricAggregationError(
                            f"Conflict: event with operation_key '{evt.operation_key}' already exists "
                            f"but has different fields. Existing: {db_evt}, New: {evt}"
                        )

            # 5. Insert MetricBootstrapState
            completed_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            state = MetricBootstrapState(
                bootstrap_key="project_metrics_v1",
                schema_version=1,
                baseline_at=baseline_str,
                completed_at=completed_str,
                history_completeness=history_completeness,
                baseline_event_count=recorded_count,
                database_was_empty=database_was_empty,
            )
            session.add(state)
            await session.commit()
            logger.info(
                f"Metrics bootstrap completed. Recorded {recorded_count} baseline events. Completeness: {history_completeness}"
            )

        except IntegrityError as exc:
            await session.rollback()
            # If this is a UNIQUE constraint failure on metric_bootstrap_state.bootstrap_key
            orig_msg = str(exc.orig) if exc.orig is not None else str(exc)
            if "UNIQUE constraint failed: metric_bootstrap_state.bootstrap_key" in orig_msg:
                # Re-check the database to verify if it is completed and check schema version
                async with db._session() as check_session:
                    res_state = await check_session.execute(
                        select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "project_metrics_v1")
                    )
                    existing_state = res_state.scalar_one_or_none()
                    if existing_state is not None:
                        if existing_state.schema_version != 1:
                            raise MetricAggregationError(
                                f"Unsupported bootstrap schema version: {existing_state.schema_version}. Expected 1."
                            )
                        logger.info("Concurrent bootstrap finished by another task. Exiting gracefully.")
                        return
            raise

        except Exception as e:
            await session.rollback()
            logger.error(f"Error during metrics bootstrap: {e}")
            raise
