"""Project Metrics Collector v1 service.

Handles metrics events logging, idempotency verification, and deterministic aggregation.
"""

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Tuple

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import MetricEvent, MetricTrackingState


class MetricEventType(str, Enum):
    BASELINE = "BASELINE"
    ENTITY_CREATED = "ENTITY_CREATED"
    ENTITY_DELETED = "ENTITY_DELETED"
    RESUME_GENERATED = "RESUME_GENERATED"
    JOB_ANALYZED = "JOB_ANALYZED"
    APPLICATION_STATUS_CHANGED = "APPLICATION_STATUS_CHANGED"
    TRANSFORMATION_PLAN_DECIDED = "TRANSFORMATION_PLAN_DECIDED"


class MetricEntityType(str, Enum):
    RESUME = "RESUME"
    JOB = "JOB"
    APPLICATION = "APPLICATION"


class MetricEventSource(str, Enum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    BOOTSTRAP = "BOOTSTRAP"


class MetricAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"


class MetricType(str, Enum):
    JOB_OFFERS_SAVED = "job_offers_saved"
    JOB_OFFERS_ANALYZED = "job_offers_analyzed"
    RESUMES_GENERATED = "resumes_generated"
    APPLICATIONS_CREATED = "applications_created"
    APPLICATIONS_SUBMITTED = "applications_submitted"
    EMPLOYER_RESPONSES = "employer_responses"
    INTERVIEWS = "interviews"
    REJECTIONS = "rejections"
    APPLICATIONS_ACCEPTED = "applications_accepted"
    STATUS_CHANGES = "status_changes"


class RecordEventStatus(str, Enum):
    RECORDED = "RECORDED"
    DUPLICATE = "DUPLICATE"


class MetricsTrackingNotInitializedError(Exception):
    """Raised when metric tracking state is queried but has not been initialized."""
    pass


class MetricAggregationError(Exception):
    """Raised when there is an inconsistency or validation error during event aggregation."""
    pass


class ApplicationStatusConflictError(Exception):
    """Raised when a concurrency conflict occurs during optimistic status update."""
    pass



VALID_APPLICATION_STATUSES = {"saved", "applied", "no_response", "response", "interview", "accepted", "rejected"}


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with Z indicator."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_utc_iso_timestamp(ts: Optional[str]) -> None:
    if ts is None:
        return
    if not isinstance(ts, str):
        raise TypeError("Timestamp must be a string")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        raise ValueError(f"Invalid ISO-8601 timestamp: {ts}")
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp must include timezone: {ts}")
    if dt.utcoffset().total_seconds() != 0:
        raise ValueError(f"Timestamp must represent UTC time: {ts}")


@dataclass(frozen=True)
class MetricEventInput:
    event_id: str
    operation_key: str
    event_type: MetricEventType
    entity_type: MetricEntityType
    entity_id: str
    lifecycle_id: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    occurred_at: Optional[str] = None
    source: MetricEventSource = MetricEventSource.USER
    metadata_json: Optional[dict] = None

    def __post_init__(self):
        # 1. Validate occurred_at timezone and UTC format
        _validate_utc_iso_timestamp(self.occurred_at)

        # 2. Validate metadata_json is dict or None
        if self.metadata_json is not None and not isinstance(self.metadata_json, dict):
            raise TypeError("metadata_json must be a dictionary or None")

        # Basic type and whitespace validations
        def validate_id(val, name):
            if not isinstance(val, str):
                raise TypeError(f"{name} must be a string")
            if not val or val != val.strip():
                raise ValueError(f"{name} cannot be empty or contain leading/trailing whitespace")

        validate_id(self.event_id, "event_id")
        validate_id(self.operation_key, "operation_key")
        validate_id(self.entity_id, "entity_id")
        validate_id(self.lifecycle_id, "lifecycle_id")

        # Type checking for Enums
        if not isinstance(self.event_type, MetricEventType):
            raise TypeError("Invalid event_type")
        if not isinstance(self.entity_type, MetricEntityType):
            raise TypeError("Invalid entity_type")
        if not isinstance(self.source, MetricEventSource):
            raise TypeError("Invalid source")

        # Status validation helpers (strictly kanoniczne statusy - no whitespace or casing checks)
        def check_status(status_val, name):
            if status_val is not None:
                if not isinstance(status_val, str) or status_val not in VALID_APPLICATION_STATUSES:
                    raise ValueError(f"Invalid {name}: {status_val}")

        check_status(self.from_state, "from_state")
        check_status(self.to_state, "to_state")

        # Matrix Event Type / Entity Type Validation
        if self.event_type == MetricEventType.RESUME_GENERATED:
            if self.entity_type != MetricEntityType.RESUME:
                raise ValueError("RESUME_GENERATED is only allowed for RESUME")
            if self.from_state is not None or self.to_state is not None:
                raise ValueError("RESUME_GENERATED cannot have from_state or to_state")

        elif self.event_type == MetricEventType.JOB_ANALYZED:
            if self.entity_type != MetricEntityType.JOB:
                raise ValueError("JOB_ANALYZED is only allowed for JOB")
            if self.from_state is not None or self.to_state is not None:
                raise ValueError("JOB_ANALYZED cannot have from_state or to_state")

        elif self.event_type == MetricEventType.TRANSFORMATION_PLAN_DECIDED:
            if self.entity_type != MetricEntityType.JOB:
                raise ValueError("TRANSFORMATION_PLAN_DECIDED is only allowed for JOB")
            if self.from_state is not None or self.to_state is not None:
                raise ValueError(
                    "TRANSFORMATION_PLAN_DECIDED cannot have from_state or to_state"
                )

        elif self.event_type == MetricEventType.APPLICATION_STATUS_CHANGED:
            if self.entity_type != MetricEntityType.APPLICATION:
                raise ValueError("APPLICATION_STATUS_CHANGED is only allowed for APPLICATION")
            if self.from_state is None or self.to_state is None:
                raise ValueError("APPLICATION_STATUS_CHANGED requires both from_state and to_state")
            if self.from_state == self.to_state:
                raise ValueError("APPLICATION_STATUS_CHANGED requires from_state != to_state")

        elif self.event_type == MetricEventType.BASELINE:
            if self.entity_type != MetricEntityType.APPLICATION or self.source != MetricEventSource.BOOTSTRAP:
                raise ValueError("BASELINE is only allowed for APPLICATION with source=BOOTSTRAP")
            if self.from_state is not None:
                raise ValueError("BASELINE requires from_state to be None")
            if self.to_state is None:
                raise ValueError("BASELINE requires a valid to_state")

        elif self.event_type == MetricEventType.ENTITY_CREATED:
            if self.entity_type not in (MetricEntityType.JOB, MetricEntityType.APPLICATION, MetricEntityType.RESUME):
                raise ValueError("ENTITY_CREATED is only allowed for JOB, APPLICATION, or RESUME")
            if self.entity_type == MetricEntityType.APPLICATION:
                if self.from_state is not None:
                    raise ValueError("ENTITY_CREATED for APPLICATION requires from_state to be None")
                if self.source in (MetricEventSource.USER, MetricEventSource.SYSTEM):
                    if self.to_state is None:
                        raise ValueError("ENTITY_CREATED for APPLICATION requires a valid to_state")
                if self.source == MetricEventSource.BOOTSTRAP:
                    if self.to_state is not None:
                        raise ValueError("ENTITY_CREATED for APPLICATION with BOOTSTRAP source requires to_state to be None")
            else:
                if self.from_state is not None or self.to_state is not None:
                    raise ValueError("ENTITY_CREATED for JOB or RESUME cannot have from_state or to_state")

        elif self.event_type == MetricEventType.ENTITY_DELETED:
            if self.entity_type not in (MetricEntityType.JOB, MetricEntityType.APPLICATION, MetricEntityType.RESUME):
                raise ValueError("ENTITY_DELETED is only allowed for JOB, APPLICATION, or RESUME")
            if self.from_state is not None or self.to_state is not None:
                raise ValueError("ENTITY_DELETED cannot have from_state or to_state")


@dataclass(frozen=True)
class MetricMeasure:
    value: Optional[int]
    availability: MetricAvailability


@dataclass(frozen=True)
class MetricValue:
    metric_name: MetricType
    current: MetricMeasure
    lifetime: MetricMeasure
    peak: MetricMeasure
    archived: MetricMeasure


@dataclass(frozen=True)
class MetricCapabilities:
    projects: MetricAvailability = MetricAvailability.UNSUPPORTED
    archive: MetricAvailability = MetricAvailability.UNSUPPORTED


@dataclass(frozen=True)
class MetricHistoryInfo:
    bootstrap_version: int
    baseline_at: str
    complete_since: str
    completeness: str
    baseline_event_count: int


@dataclass(frozen=True)
class MetricSummaryReport:
    metrics: Tuple[MetricValue, ...]
    tracking_started_at: str
    generated_at: str
    historical_complete: bool
    capabilities: MetricCapabilities
    history: Optional[MetricHistoryInfo] = None



@dataclass(frozen=True)
class RecordEventResult:
    status: RecordEventStatus
    event_id: str
    sequence_id: Optional[int]


async def record_metric_event(
    session: AsyncSession,
    event: MetricEventInput,
) -> RecordEventResult:
    """Record metric event safely with ON CONFLICT DO NOTHING."""
    metadata = copy.deepcopy(event.metadata_json) if event.metadata_json is not None else {}
    occurred = event.occurred_at if event.occurred_at is not None else _utcnow_iso()

    stmt = sqlite_insert(MetricEvent).values(
        event_id=event.event_id,
        operation_key=event.operation_key,
        event_type=event.event_type.value,
        entity_type=event.entity_type.value,
        entity_id=event.entity_id,
        lifecycle_id=event.lifecycle_id,
        from_state=event.from_state,
        to_state=event.to_state,
        occurred_at=occurred,
        source=event.source.value,
        metadata_json=metadata,
    ).on_conflict_do_nothing().returning(
        MetricEvent.sequence_id,
        MetricEvent.event_id,
    )

    result = await session.execute(stmt)
    row = result.first()

    if row is None:
        return RecordEventResult(
            status=RecordEventStatus.DUPLICATE,
            event_id=event.event_id,
            sequence_id=None,
        )

    return RecordEventResult(
        status=RecordEventStatus.RECORDED,
        event_id=row.event_id,
        sequence_id=row.sequence_id,
    )


def aggregate_events(
    events: list[MetricEvent],
    tracking_started_at: str,
    generated_at: str,
    historical_complete: bool,
) -> MetricSummaryReport:
    """Deterministically aggregates events from memory sorted by sequence_id."""
    sorted_events = sorted(events, key=lambda e: e.sequence_id)

    # Sets for active and created lifecycles
    created_job_lifecycles = set()
    active_job_lifecycles = set()

    created_application_lifecycles = set()
    active_application_lifecycles = set()

    created_resume_lifecycles = set()
    active_resume_lifecycles = set()

    generated_resume_lifecycles = set()
    active_generated_resume_lifecycles = set()

    analyzed_job_lifecycles = set()
    active_analyzed_job_lifecycles = set()

    # Domain counters
    job_offers_saved_lifetime = 0
    job_offers_saved_peak = 0
    job_offers_saved_current = 0

    job_offers_analyzed_lifetime = 0
    job_offers_analyzed_peak = 0
    job_offers_analyzed_current = 0

    resumes_generated_lifetime = 0
    resumes_generated_peak = 0
    resumes_generated_current = 0

    applications_created_lifetime = 0
    applications_created_peak = 0
    applications_created_current = 0

    # Milestones (sets of lifecycle_ids)
    submitted_lifecycles = set()
    interview_lifecycles = set()
    accepted_lifecycles = set()
    rejected_lifecycles = set()
    response_lifecycles = set()

    # Active states per Application lifecycle_id
    app_active_status = {}

    status_changes_lifetime = 0

    # Peaks for milestone statuses
    submitted_current = 0
    submitted_peak = 0

    interview_current = 0
    interview_peak = 0

    accepted_current = 0
    accepted_peak = 0

    rejected_current = 0
    rejected_peak = 0

    response_current = 0
    response_peak = 0

    for event in sorted_events:
        e_type = event.event_type
        ent_type = event.entity_type
        lc_id = event.lifecycle_id

        if ent_type == "JOB":
            if e_type == "ENTITY_CREATED":
                if lc_id not in created_job_lifecycles:
                    created_job_lifecycles.add(lc_id)
                    active_job_lifecycles.add(lc_id)
                    job_offers_saved_lifetime += 1
                    job_offers_saved_current += 1
                    job_offers_saved_peak = max(job_offers_saved_peak, job_offers_saved_current)
            elif e_type == "JOB_ANALYZED":
                if lc_id not in analyzed_job_lifecycles:
                    analyzed_job_lifecycles.add(lc_id)
                    active_analyzed_job_lifecycles.add(lc_id)
                    job_offers_analyzed_lifetime += 1
                    job_offers_analyzed_current += 1
                    job_offers_analyzed_peak = max(job_offers_analyzed_peak, job_offers_analyzed_current)
            elif e_type == "ENTITY_DELETED":
                if lc_id in active_job_lifecycles:
                    active_job_lifecycles.remove(lc_id)
                    job_offers_saved_current = max(0, job_offers_saved_current - 1)
                if lc_id in active_analyzed_job_lifecycles:
                    active_analyzed_job_lifecycles.remove(lc_id)
                    job_offers_analyzed_current = max(0, job_offers_analyzed_current - 1)

        elif ent_type == "RESUME":
            if e_type == "ENTITY_CREATED":
                if lc_id not in created_resume_lifecycles:
                    created_resume_lifecycles.add(lc_id)
                    active_resume_lifecycles.add(lc_id)
            elif e_type == "RESUME_GENERATED":
                if lc_id not in generated_resume_lifecycles:
                    generated_resume_lifecycles.add(lc_id)
                    active_generated_resume_lifecycles.add(lc_id)
                    resumes_generated_lifetime += 1
                    resumes_generated_current += 1
                    resumes_generated_peak = max(resumes_generated_peak, resumes_generated_current)
            elif e_type == "ENTITY_DELETED":
                if lc_id in active_resume_lifecycles:
                    active_resume_lifecycles.remove(lc_id)
                if lc_id in active_generated_resume_lifecycles:
                    active_generated_resume_lifecycles.remove(lc_id)
                    resumes_generated_current = max(0, resumes_generated_current - 1)

        elif ent_type == "APPLICATION":
            if e_type == "ENTITY_CREATED":
                if lc_id not in created_application_lifecycles:
                    created_application_lifecycles.add(lc_id)
                    active_application_lifecycles.add(lc_id)
                    applications_created_lifetime += 1
                    applications_created_current += 1
                    applications_created_peak = max(applications_created_peak, applications_created_current)

                    to_state = event.to_state
                    if to_state:
                        app_active_status[lc_id] = to_state

                        if to_state == "applied":
                            submitted_current += 1
                            submitted_peak = max(submitted_peak, submitted_current)
                            submitted_lifecycles.add(lc_id)
                        elif to_state == "interview":
                            interview_current += 1
                            interview_peak = max(interview_peak, interview_current)
                            interview_lifecycles.add(lc_id)
                        elif to_state == "accepted":
                            accepted_current += 1
                            accepted_peak = max(accepted_peak, accepted_current)
                            accepted_lifecycles.add(lc_id)
                        elif to_state == "rejected":
                            rejected_current += 1
                            rejected_peak = max(rejected_peak, rejected_current)
                            rejected_lifecycles.add(lc_id)

                        if to_state in ("response", "interview", "accepted", "rejected"):
                            response_current += 1
                            response_peak = max(response_peak, response_current)
                            response_lifecycles.add(lc_id)

            elif e_type == "BASELINE":
                if lc_id not in created_application_lifecycles:
                    created_application_lifecycles.add(lc_id)
                    active_application_lifecycles.add(lc_id)
                    applications_created_lifetime += 1
                    applications_created_current += 1
                    applications_created_peak = max(applications_created_peak, applications_created_current)

                old_state = app_active_status.get(lc_id)
                if old_state:
                    if old_state == "applied":
                        submitted_current = max(0, submitted_current - 1)
                    elif old_state == "interview":
                        interview_current = max(0, interview_current - 1)
                    elif old_state == "accepted":
                        accepted_current = max(0, accepted_current - 1)
                    elif old_state == "rejected":
                        rejected_current = max(0, rejected_current - 1)

                    if old_state in ("response", "interview", "accepted", "rejected"):
                        response_current = max(0, response_current - 1)

                to_state = event.to_state
                if to_state:
                    app_active_status[lc_id] = to_state

                    if to_state == "applied":
                        submitted_current += 1
                        submitted_peak = max(submitted_peak, submitted_current)
                        submitted_lifecycles.add(lc_id)
                    elif to_state == "interview":
                        interview_current += 1
                        interview_peak = max(interview_peak, interview_current)
                        interview_lifecycles.add(lc_id)
                    elif to_state == "accepted":
                        accepted_current += 1
                        accepted_peak = max(accepted_peak, accepted_current)
                        accepted_lifecycles.add(lc_id)
                    elif to_state == "rejected":
                        rejected_current += 1
                        rejected_peak = max(rejected_peak, rejected_current)
                        rejected_lifecycles.add(lc_id)

                    if to_state in ("response", "interview", "accepted", "rejected"):
                        response_current += 1
                        response_peak = max(response_peak, response_current)
                        response_lifecycles.add(lc_id)

            elif e_type == "APPLICATION_STATUS_CHANGED":
                if lc_id not in active_application_lifecycles:
                    raise MetricAggregationError(f"Application status change for deleted/inactive lifecycle: {lc_id}")

                from_state = event.from_state
                to_state = event.to_state

                stored_status = app_active_status.get(lc_id)
                if stored_status != from_state:
                    raise MetricAggregationError(
                        f"Inconsistent from_state for lifecycle {lc_id}. Expected {stored_status}, got {from_state}"
                    )

                status_changes_lifetime += 1

                if stored_status == "applied":
                    submitted_current = max(0, submitted_current - 1)
                elif stored_status == "interview":
                    interview_current = max(0, interview_current - 1)
                elif stored_status == "accepted":
                    accepted_current = max(0, accepted_current - 1)
                elif stored_status == "rejected":
                    rejected_current = max(0, rejected_current - 1)

                if stored_status in ("response", "interview", "accepted", "rejected"):
                    response_current = max(0, response_current - 1)

                app_active_status[lc_id] = to_state

                if to_state == "applied":
                    submitted_current += 1
                    submitted_peak = max(submitted_peak, submitted_current)
                    submitted_lifecycles.add(lc_id)
                elif to_state == "interview":
                    interview_current += 1
                    interview_peak = max(interview_peak, interview_current)
                    interview_lifecycles.add(lc_id)
                elif to_state == "accepted":
                    accepted_current += 1
                    accepted_peak = max(accepted_peak, accepted_current)
                    accepted_lifecycles.add(lc_id)
                elif to_state == "rejected":
                    rejected_current += 1
                    rejected_peak = max(rejected_peak, rejected_current)
                    rejected_lifecycles.add(lc_id)

                if to_state in ("response", "interview", "accepted", "rejected"):
                    response_current += 1
                    response_peak = max(response_peak, response_current)
                    response_lifecycles.add(lc_id)

            elif e_type == "ENTITY_DELETED":
                if lc_id in active_application_lifecycles:
                    active_application_lifecycles.remove(lc_id)
                    applications_created_current = max(0, applications_created_current - 1)

                    old_state = app_active_status.pop(lc_id, None)
                    if old_state == "applied":
                        submitted_current = max(0, submitted_current - 1)
                    elif old_state == "interview":
                        interview_current = max(0, interview_current - 1)
                    elif old_state == "accepted":
                        accepted_current = max(0, accepted_current - 1)
                    elif old_state == "rejected":
                        rejected_current = max(0, rejected_current - 1)

                    if old_state in ("response", "interview", "accepted", "rejected"):
                        response_current = max(0, response_current - 1)

    availability_val = MetricAvailability.AVAILABLE
    has_bootstrap_or_baseline = any(
        e.source == "BOOTSTRAP" or e.event_type == "BASELINE" for e in events
    )
    effective_historical_complete = historical_complete and not has_bootstrap_or_baseline

    def make_metric(metric_name: MetricType, lifetime_val: int, peak_val: Optional[int]) -> MetricValue:
        return MetricValue(
            metric_name=metric_name,
            current=MetricMeasure(value=None, availability=MetricAvailability.UNSUPPORTED),
            lifetime=MetricMeasure(value=lifetime_val, availability=availability_val),
            peak=MetricMeasure(value=peak_val, availability=availability_val if peak_val is not None else MetricAvailability.UNSUPPORTED),
            archived=MetricMeasure(value=None, availability=MetricAvailability.UNSUPPORTED),
        )

    metrics_tuple = (
        make_metric(MetricType.JOB_OFFERS_SAVED, job_offers_saved_lifetime, job_offers_saved_peak),
        make_metric(MetricType.JOB_OFFERS_ANALYZED, job_offers_analyzed_lifetime, job_offers_analyzed_peak),
        make_metric(MetricType.RESUMES_GENERATED, resumes_generated_lifetime, resumes_generated_peak),
        make_metric(MetricType.APPLICATIONS_CREATED, applications_created_lifetime, applications_created_peak),
        make_metric(MetricType.APPLICATIONS_SUBMITTED, len(submitted_lifecycles), submitted_peak),
        make_metric(MetricType.EMPLOYER_RESPONSES, len(response_lifecycles), response_peak),
        make_metric(MetricType.INTERVIEWS, len(interview_lifecycles), interview_peak),
        make_metric(MetricType.REJECTIONS, len(rejected_lifecycles), rejected_peak),
        make_metric(MetricType.APPLICATIONS_ACCEPTED, len(accepted_lifecycles), accepted_peak),
        make_metric(MetricType.STATUS_CHANGES, status_changes_lifetime, None),
    )

    return MetricSummaryReport(
        metrics=metrics_tuple,
        tracking_started_at=tracking_started_at,
        generated_at=generated_at,
        historical_complete=effective_historical_complete,
        capabilities=MetricCapabilities(),
    )


async def build_metrics_report(session: AsyncSession) -> MetricSummaryReport:
    """Build report based on current metric_events table."""
    state_result = await session.execute(
        select(MetricTrackingState).where(MetricTrackingState.id == 1)
    )
    state = state_result.scalar_one_or_none()

    if state is None:
        raise MetricsTrackingNotInitializedError("Metric tracking has not been initialized.")

    from app.models import MetricBootstrapState
    bootstrap_result = await session.execute(
        select(MetricBootstrapState).where(MetricBootstrapState.bootstrap_key == "project_metrics_v1")
    )
    bootstrap_state = bootstrap_result.scalar_one_or_none()

    if bootstrap_state is None:
        raise MetricsTrackingNotInitializedError("Metrics bootstrap has not been completed.")

    history_info = MetricHistoryInfo(
        bootstrap_version=bootstrap_state.schema_version,
        baseline_at=bootstrap_state.baseline_at,
        complete_since=bootstrap_state.baseline_at,
        completeness=bootstrap_state.history_completeness,
        baseline_event_count=bootstrap_state.baseline_event_count,
    )

    tracking_started_at = state.tracking_started_at
    historical_complete = state.historical_complete

    event_result = await session.execute(select(MetricEvent))
    events = event_result.scalars().all()

    base_report = aggregate_events(
        events=list(events),
        tracking_started_at=tracking_started_at,
        generated_at=_utcnow_iso(),
        historical_complete=historical_complete,
    )


    from app.models import Application, Job, Resume
    from sqlalchemy import func
    res_apps = await session.execute(select(Application.status))
    app_statuses = res_apps.scalars().all()

    total_apps = len(app_statuses)
    applied_apps = sum(1 for s in app_statuses if s == "applied")
    response_apps = sum(1 for s in app_statuses if s in ("response", "interview", "accepted", "rejected"))
    interview_apps = sum(1 for s in app_statuses if s == "interview")
    rejected_apps = sum(1 for s in app_statuses if s == "rejected")
    accepted_apps = sum(1 for s in app_statuses if s == "accepted")

    # job_offers_saved current
    res_jobs = await session.execute(select(func.count()).select_from(Job))
    job_offers_saved_current = res_jobs.scalar() or 0

    # job_offers_analyzed current (distinct lifecycle_tokens of existing jobs having a JOB_ANALYZED metric event in their current lifecycle)
    res_analyzed = await session.execute(
        select(func.count(func.distinct(Job.lifecycle_token))).join(
            MetricEvent,
            (MetricEvent.event_type == MetricEventType.JOB_ANALYZED.value) &
            (MetricEvent.entity_type == MetricEntityType.JOB.value) &
            (MetricEvent.entity_id == Job.job_id) &
            (MetricEvent.lifecycle_id == Job.lifecycle_token)
        )
    )
    job_offers_analyzed_current = res_analyzed.scalar() or 0

    # resumes_generated current
    res_resumes = await session.execute(
        select(func.count()).select_from(Resume).where(Resume.parent_id.is_not(None))
    )
    resumes_generated_current = res_resumes.scalar() or 0

    app_current_map = {
        MetricType.APPLICATIONS_CREATED: total_apps,
        MetricType.APPLICATIONS_SUBMITTED: applied_apps,
        MetricType.EMPLOYER_RESPONSES: response_apps,
        MetricType.INTERVIEWS: interview_apps,
        MetricType.REJECTIONS: rejected_apps,
        MetricType.APPLICATIONS_ACCEPTED: accepted_apps,
        MetricType.JOB_OFFERS_SAVED: job_offers_saved_current,
        MetricType.JOB_OFFERS_ANALYZED: job_offers_analyzed_current,
        MetricType.RESUMES_GENERATED: resumes_generated_current,
    }

    new_metrics = []
    for mv in base_report.metrics:
        if mv.metric_name in app_current_map:
            val = app_current_map[mv.metric_name]
            new_mv = MetricValue(
                metric_name=mv.metric_name,
                current=MetricMeasure(value=val, availability=MetricAvailability.AVAILABLE),
                lifetime=mv.lifetime,
                peak=mv.peak,
                archived=mv.archived,
            )
            new_metrics.append(new_mv)
        elif mv.metric_name == MetricType.STATUS_CHANGES:
            new_mv = MetricValue(
                metric_name=mv.metric_name,
                current=MetricMeasure(value=None, availability=MetricAvailability.UNSUPPORTED),
                lifetime=mv.lifetime,
                peak=mv.peak,
                archived=mv.archived,
            )
            new_metrics.append(new_mv)
        else:
            new_metrics.append(mv)

    return MetricSummaryReport(
        metrics=tuple(new_metrics),
        tracking_started_at=base_report.tracking_started_at,
        generated_at=base_report.generated_at,
        historical_complete=base_report.historical_complete,
        capabilities=base_report.capabilities,
        history=history_info,
    )


async def initialize_metric_tracking_state(
    session: AsyncSession,
    historical_complete: bool,
    tracking_started_at: Optional[str] = None,
) -> MetricTrackingState:
    """Initialize tracking state singleton (id=1) idempotent-ly."""
    start_time = tracking_started_at if tracking_started_at is not None else _utcnow_iso()
    _validate_utc_iso_timestamp(start_time)

    stmt = sqlite_insert(MetricTrackingState).values(
        id=1,
        schema_version=1,
        tracking_started_at=start_time,
        bootstrap_completed_at=None,
        historical_complete=historical_complete,
    ).on_conflict_do_nothing()

    await session.execute(stmt)

    state_res = await session.execute(
        select(MetricTrackingState).where(MetricTrackingState.id == 1)
    )
    return state_res.scalar_one()
