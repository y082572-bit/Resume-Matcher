import pytest
from dataclasses import FrozenInstanceError

from app.services.project_metrics import (
    MetricEventType,
    MetricEntityType,
    MetricEventSource,
    MetricAvailability,
    MetricType,
    MetricEventInput,
    MetricMeasure,
    MetricValue,
    aggregate_events,
    MetricAggregationError,
    MetricCapabilities,
    MetricSummaryReport,
    RecordEventResult,
    RecordEventStatus,
)
from app.models import MetricEvent


def make_event(
    seq_id: int,
    event_id: str = "evt-1",
    operation_key: str = "op-1",
    event_type: MetricEventType = MetricEventType.ENTITY_CREATED,
    entity_type: MetricEntityType = MetricEntityType.APPLICATION,
    entity_id: str = "app-1",
    lifecycle_id: str = "lc-1",
    from_state: str = None,
    to_state: str = None,
    occurred_at: str = "2026-07-16T12:00:00Z",
    source: MetricEventSource = MetricEventSource.USER,
) -> MetricEvent:
    """Helper to construct MetricEvent ORM objects for aggregation tests."""
    return MetricEvent(
        sequence_id=seq_id,
        event_id=event_id,
        operation_key=operation_key,
        event_type=event_type.value,
        entity_type=entity_type.value,
        entity_id=entity_id,
        lifecycle_id=lifecycle_id,
        from_state=from_state,
        to_state=to_state,
        occurred_at=occurred_at,
        source=source.value,
        metadata_json={},
    )


# --- Enums and Dataclasses Tests ---

def test_enums_defined_correctly():
    assert MetricEventType.BASELINE == "BASELINE"
    assert MetricEntityType.APPLICATION == "APPLICATION"
    assert MetricEventSource.USER == "USER"
    assert MetricAvailability.UNSUPPORTED == "UNSUPPORTED"
    assert MetricType.STATUS_CHANGES == "status_changes"
    assert MetricType.APPLICATIONS_ACCEPTED == "applications_accepted"


def test_metric_measure_is_frozen():
    measure = MetricMeasure(value=10, availability=MetricAvailability.AVAILABLE)
    with pytest.raises(FrozenInstanceError):
        measure.value = 20  # type: ignore


def test_metric_value_is_frozen():
    val = MetricValue(
        metric_name=MetricType.INTERVIEWS,
        current=MetricMeasure(value=None, availability=MetricAvailability.UNSUPPORTED),
        lifetime=MetricMeasure(value=0, availability=MetricAvailability.AVAILABLE),
        peak=MetricMeasure(value=0, availability=MetricAvailability.AVAILABLE),
        archived=MetricMeasure(value=None, availability=MetricAvailability.UNSUPPORTED),
    )
    with pytest.raises(FrozenInstanceError):
        val.metric_name = MetricType.REJECTIONS  # type: ignore


def test_metric_event_input_is_frozen():
    inp = MetricEventInput(
        event_id="evt-1",
        operation_key="op-1",
        event_type=MetricEventType.ENTITY_CREATED,
        entity_type=MetricEntityType.JOB,
        entity_id="job-1",
        lifecycle_id="lc-1",
    )
    with pytest.raises(FrozenInstanceError):
        inp.event_id = "evt-2"  # type: ignore


def test_metric_capabilities_is_frozen():
    caps = MetricCapabilities()
    with pytest.raises(FrozenInstanceError):
        caps.projects = MetricAvailability.AVAILABLE  # type: ignore


def test_metric_summary_report_is_frozen():
    report = aggregate_events([], "2026-07-16T12:00:00Z", "2026-07-16T12:00:00Z", True)
    with pytest.raises(FrozenInstanceError):
        report.historical_complete = False  # type: ignore


def test_record_event_result_is_frozen():
    res = RecordEventResult(status=RecordEventStatus.RECORDED, event_id="evt-1", sequence_id=1)
    with pytest.raises(FrozenInstanceError):
        res.event_id = "evt-2"  # type: ignore


# --- Input Validation Tests ---

def test_validation_empty_event_id_raises():
    with pytest.raises(ValueError, match="event_id"):
        MetricEventInput(
            event_id=" ",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="saved",
        )


def test_validation_empty_operation_key_raises():
    with pytest.raises(ValueError, match="operation_key"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="saved",
        )


def test_validation_empty_entity_id_raises():
    with pytest.raises(ValueError, match="entity_id"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="",
            lifecycle_id="lc-1",
            to_state="saved",
        )


def test_validation_empty_lifecycle_id_raises():
    with pytest.raises(ValueError, match="lifecycle_id"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="   ",
            to_state="saved",
        )


def test_validation_invalid_enum_types_raises():
    with pytest.raises(TypeError):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type="INVALID",  # type: ignore
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="saved",
        )


def test_validation_status_changed_without_from_state_raises():
    with pytest.raises(ValueError, match="from_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="applied",
        )


def test_validation_status_changed_without_to_state_raises():
    with pytest.raises(ValueError, match="to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            from_state="saved",
        )


def test_validation_status_changed_equal_states_raises():
    with pytest.raises(ValueError, match="from_state != to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            from_state="applied",
            to_state="applied",
        )


def test_validation_unsupported_states_for_other_event_types_raises():
    with pytest.raises(ValueError, match="cannot have from_state or to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
            from_state="applied",
        )


def test_validation_resume_generated_entity_mismatch_raises():
    with pytest.raises(ValueError, match="RESUME_GENERATED is only allowed for RESUME"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.RESUME_GENERATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
        )


def test_validation_job_analyzed_entity_mismatch_raises():
    with pytest.raises(ValueError, match="JOB_ANALYZED is only allowed for JOB"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.JOB_ANALYZED,
            entity_type=MetricEntityType.RESUME,
            entity_id="res-1",
            lifecycle_id="lc-1",
        )


def test_validation_status_changed_entity_mismatch_raises():
    with pytest.raises(ValueError, match="APPLICATION_STATUS_CHANGED is only allowed for APPLICATION"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.APPLICATION_STATUS_CHANGED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
            from_state="saved",
            to_state="applied",
        )


def test_validation_occurred_at_invalid_timezone_raises():
    with pytest.raises(ValueError, match="Timestamp must include timezone"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
            occurred_at="2026-07-16T12:00:00",
        )


def test_validation_occurred_at_naive_timezone_raises():
    with pytest.raises(ValueError, match="Timestamp must represent UTC time"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
            occurred_at="2026-07-16T12:00:00+02:00",
        )


def test_validation_metadata_json_non_dict_raises():
    with pytest.raises(TypeError, match="metadata_json must be a dictionary"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
            metadata_json="INVALID",  # type: ignore
        )


def test_validation_unknown_status_raises():
    with pytest.raises(ValueError, match="Invalid to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="UNKNOWN_STATUS",
        )


def test_status_with_leading_whitespace_is_rejected():
    with pytest.raises(ValueError, match="Invalid to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state=" saved",
        )


def test_status_with_trailing_whitespace_is_rejected():
    with pytest.raises(ValueError, match="Invalid to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="applied ",
        )


def test_application_created_without_initial_status_is_rejected():
    with pytest.raises(ValueError, match="requires a valid to_state"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state=None,
        )


def test_application_created_with_from_state_is_rejected():
    with pytest.raises(ValueError, match="requires from_state to be None"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            from_state="saved",
            to_state="applied",
        )


def test_id_with_whitespace_is_rejected_event_id():
    with pytest.raises(ValueError, match="cannot be empty or contain leading/trailing whitespace"):
        MetricEventInput(
            event_id=" evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
        )


def test_id_with_whitespace_is_rejected_operation_key():
    with pytest.raises(ValueError, match="cannot be empty or contain leading/trailing whitespace"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1 ",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id="lc-1",
        )


def test_id_with_whitespace_is_rejected_entity_id():
    with pytest.raises(ValueError, match="cannot be empty or contain leading/trailing whitespace"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id=" job-1 ",
            lifecycle_id="lc-1",
        )


def test_id_with_whitespace_is_rejected_lifecycle_id():
    with pytest.raises(ValueError, match="cannot be empty or contain leading/trailing whitespace"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.ENTITY_CREATED,
            entity_type=MetricEntityType.JOB,
            entity_id="job-1",
            lifecycle_id=" lc-1",
        )


def test_baseline_with_source_other_than_bootstrap_is_rejected():
    with pytest.raises(ValueError, match="BASELINE is only allowed for APPLICATION with source=BOOTSTRAP"):
        MetricEventInput(
            event_id="evt-1",
            operation_key="op-1",
            event_type=MetricEventType.BASELINE,
            entity_type=MetricEntityType.APPLICATION,
            entity_id="app-1",
            lifecycle_id="lc-1",
            to_state="saved",
            source=MetricEventSource.USER,
        )


# --- Pure Aggregator Tests ---

def test_aggregator_unsupported_projects_and_archived():
    report = aggregate_events([], "2026-07-16T12:00:00Z", "2026-07-16T12:05:00Z", True)
    assert report.capabilities.projects == MetricAvailability.UNSUPPORTED
    assert report.capabilities.archive == MetricAvailability.UNSUPPORTED

    for m in report.metrics:
        assert m.current.availability == MetricAvailability.UNSUPPORTED
        assert m.current.value is None
        assert m.archived.availability == MetricAvailability.UNSUPPORTED
        assert m.archived.value is None


def test_aggregator_sequence_id_sorting():
    e1 = make_event(seq_id=1, event_id="evt-1", operation_key="op-1", event_type=MetricEventType.ENTITY_CREATED, occurred_at="2026-07-16T12:10:00Z", to_state="saved")
    e2 = make_event(seq_id=2, event_id="evt-2", operation_key="op-2", event_type=MetricEventType.ENTITY_DELETED, occurred_at="2026-07-16T12:00:00Z")

    report = aggregate_events([e2, e1], "2026-07-16T12:00:00Z", "2026-07-16T12:05:00Z", True)
    m_created = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m_created.lifetime.value == 1
    assert m_created.peak.value == 1


def test_two_events_same_occurred_at_sorted_by_sequence_id():
    e1 = make_event(seq_id=1, event_id="evt-1", operation_key="op-1", event_type=MetricEventType.ENTITY_CREATED, occurred_at="2026-07-16T12:00:00Z", to_state="saved")
    e2 = make_event(seq_id=2, event_id="evt-2", operation_key="op-2", event_type=MetricEventType.ENTITY_DELETED, occurred_at="2026-07-16T12:00:00Z")

    report = aggregate_events([e2, e1], "2026-07-16T12:00:00Z", "2026-07-16T12:00:00Z", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m.peak.value == 1


def test_application_created_saved_then_transition_to_applied():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="saved", to_state="applied")

    report = aggregate_events([e1, e2], "2026-07-16T12:00:00Z", "2026-07-16T12:00:00Z", True)
    m_created = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    m_submitted = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_SUBMITTED)
    m_changes = next(m for m in report.metrics if m.metric_name == MetricType.STATUS_CHANGES)

    assert m_created.lifetime.value == 1
    assert m_submitted.lifetime.value == 1
    assert m_changes.lifetime.value == 1


def test_baseline_job_does_not_mean_job_analyzed():
    e = make_event(seq_id=1, event_type=MetricEventType.BASELINE, entity_type=MetricEntityType.JOB, lifecycle_id="lc-job-1", source=MetricEventSource.BOOTSTRAP)
    report = aggregate_events([e], "2026", "2026", True)
    m_saved = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    m_analyzed = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert m_saved.lifetime.value == 0
    assert m_analyzed.lifetime.value == 0


def test_baseline_resume_does_not_mean_resume_generated():
    e = make_event(seq_id=1, event_type=MetricEventType.BASELINE, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-res-1", source=MetricEventSource.BOOTSTRAP)
    report = aggregate_events([e], "2026", "2026", True)
    m_generated = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert m_generated.lifetime.value == 0


def test_entity_created_application_with_applied_counts_submitted():
    e = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="applied")
    report = aggregate_events([e], "2026", "2026", True)

    m_created = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    m_submitted = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_SUBMITTED)
    assert m_created.lifetime.value == 1
    assert m_submitted.lifetime.value == 1


def test_duplicate_create_same_lifecycle_does_not_increase_lifetime():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    e2 = make_event(seq_id=2, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    report = aggregate_events([e1, e2], "2026", "2026", True)

    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m.lifetime.value == 1


def test_duplicate_delete_does_not_decrease_state():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    e2 = make_event(seq_id=2, event_type=MetricEventType.ENTITY_DELETED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1")
    e3 = make_event(seq_id=3, event_type=MetricEventType.ENTITY_DELETED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1")
    report = aggregate_events([e1, e2, e3], "2026", "2026", True)

    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m.lifetime.value == 1
    assert m.peak.value == 1


def test_delete_unknown_lifecycle_does_not_change_result():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_DELETED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-unknown")
    report = aggregate_events([e1], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m.lifetime.value == 0


def test_delete_master_resume_does_not_decrease_generated_resume():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-master")
    e2 = make_event(seq_id=2, event_type=MetricEventType.RESUME_GENERATED, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-tailored")
    e3 = make_event(seq_id=3, event_type=MetricEventType.ENTITY_DELETED, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-master")

    report = aggregate_events([e1, e2, e3], "2026", "2026", True)
    m_generated = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert m_generated.lifetime.value == 1
    assert m_generated.peak.value == 1


def test_duplicate_resume_generated_same_lifecycle_does_not_double_result():
    e1 = make_event(seq_id=1, event_type=MetricEventType.RESUME_GENERATED, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-res")
    e2 = make_event(seq_id=2, event_type=MetricEventType.RESUME_GENERATED, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-res")
    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert m.lifetime.value == 1


def test_inconsistent_from_state_raises_metric_aggregation_error():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="applied")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="interview", to_state="accepted")

    with pytest.raises(MetricAggregationError, match="Inconsistent from_state"):
        aggregate_events([e1, e2], "2026", "2026", True)


def test_status_change_before_creating_application_is_rejected():
    e1 = make_event(seq_id=1, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="saved", to_state="applied")
    with pytest.raises(MetricAggregationError, match="deleted/inactive lifecycle"):
        aggregate_events([e1], "2026", "2026", True)


def test_event_after_deleting_application_is_rejected():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    e2 = make_event(seq_id=2, event_type=MetricEventType.ENTITY_DELETED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1")
    e3 = make_event(seq_id=3, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="saved", to_state="applied")

    with pytest.raises(MetricAggregationError, match="deleted/inactive lifecycle"):
        aggregate_events([e1, e2, e3], "2026", "2026", True)


def test_report_historical_complete_is_consistent_with_availability():
    report = aggregate_events([], "2026", "2026", True)
    assert report.historical_complete is True
    for m in report.metrics:
        if m.metric_name != MetricType.STATUS_CHANGES:
            assert m.lifetime.availability == MetricAvailability.AVAILABLE
            assert m.peak.availability == MetricAvailability.AVAILABLE


# --- Stare scenariusze regresyjne ---

def test_entity_created_increases_lifetime():
    e = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, to_state="saved")
    report = aggregate_events([e], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m.lifetime.value == 1


def test_entity_deleted_reduces_peak_simulated_only_for_active_lifecycle():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    e2 = make_event(seq_id=2, event_type=MetricEventType.ENTITY_DELETED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1")
    e3 = make_event(seq_id=3, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-2", to_state="saved")

    report = aggregate_events([e1, e2, e3], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    assert m.lifetime.value == 2
    assert m.peak.value == 1


def test_resume_generated_increases_generated_metric():
    e = make_event(seq_id=1, event_type=MetricEventType.RESUME_GENERATED, entity_type=MetricEntityType.RESUME)
    report = aggregate_events([e], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)
    assert m.lifetime.value == 1


def test_job_analyzed_counted_once_per_lifecycle():
    e1 = make_event(seq_id=1, event_type=MetricEventType.JOB_ANALYZED, entity_type=MetricEntityType.JOB, lifecycle_id="lc-job-1")
    e2 = make_event(seq_id=2, event_type=MetricEventType.JOB_ANALYZED, entity_type=MetricEntityType.JOB, lifecycle_id="lc-job-1")

    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert m.lifetime.value == 1


def test_job_analyzed_with_different_lifecycle_increases_lifetime():
    e1 = make_event(seq_id=1, event_type=MetricEventType.JOB_ANALYZED, entity_type=MetricEntityType.JOB, lifecycle_id="lc-job-1")
    e2 = make_event(seq_id=2, event_type=MetricEventType.JOB_ANALYZED, entity_type=MetricEntityType.JOB, lifecycle_id="lc-job-2")

    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_ANALYZED)
    assert m.lifetime.value == 2


def test_milestone_submitted_counted_once_per_lifecycle():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", to_state="saved")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="saved", to_state="applied")
    e3 = make_event(seq_id=3, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="applied", to_state="interview")
    e4 = make_event(seq_id=4, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-1", from_state="interview", to_state="applied")

    report = aggregate_events([e1, e2, e3, e4], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_SUBMITTED)
    assert m.lifetime.value == 1


def test_milestone_interview_counted_once():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, lifecycle_id="lc-1", to_state="applied")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, lifecycle_id="lc-1", from_state="applied", to_state="interview")

    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.INTERVIEWS)
    assert m.lifetime.value == 1


def test_milestone_rejected_counted_once():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, lifecycle_id="lc-1", to_state="applied")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, lifecycle_id="lc-1", from_state="applied", to_state="rejected")
    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.REJECTIONS)
    assert m.lifetime.value == 1


def test_milestone_accepted_counted_once():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, lifecycle_id="lc-1", to_state="applied")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, lifecycle_id="lc-1", from_state="applied", to_state="accepted")
    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_ACCEPTED)
    assert m.lifetime.value == 1


def test_milestone_employer_responses_counted_once():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, lifecycle_id="lc-1", to_state="applied")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, lifecycle_id="lc-1", from_state="applied", to_state="interview")

    report = aggregate_events([e1, e2], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.EMPLOYER_RESPONSES)
    assert m.lifetime.value == 1


def test_status_changes_increments_for_each_real_transition():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, lifecycle_id="lc-1", to_state="applied")
    e2 = make_event(seq_id=2, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, lifecycle_id="lc-1", from_state="applied", to_state="interview")
    e3 = make_event(seq_id=3, event_type=MetricEventType.APPLICATION_STATUS_CHANGED, lifecycle_id="lc-1", from_state="interview", to_state="applied")

    report = aggregate_events([e1, e2, e3], "2026", "2026", True)
    m = next(m for m in report.metrics if m.metric_name == MetricType.STATUS_CHANGES)
    assert m.lifetime.value == 2


def test_bootstrap_or_baseline_causes_incomplete_history():
    e = make_event(seq_id=1, event_type=MetricEventType.BASELINE, entity_type=MetricEntityType.APPLICATION, to_state="saved", source=MetricEventSource.BOOTSTRAP)
    report = aggregate_events([e], "2026", "2026", True)
    assert report.historical_complete is False


def test_metric_capabilities_defaults():
    caps = MetricCapabilities()
    assert caps.projects == MetricAvailability.UNSUPPORTED
    assert caps.archive == MetricAvailability.UNSUPPORTED


def test_metric_event_input_default_source():
    inp = MetricEventInput(
        event_id="evt-1",
        operation_key="op-1",
        event_type=MetricEventType.ENTITY_CREATED,
        entity_type=MetricEntityType.JOB,
        entity_id="job-1",
        lifecycle_id="lc-1",
    )
    assert inp.source == MetricEventSource.USER


def test_aggregator_multiple_entity_types():
    e1 = make_event(seq_id=1, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.JOB, lifecycle_id="lc-job-1")
    e2 = make_event(seq_id=2, event_type=MetricEventType.ENTITY_CREATED, entity_type=MetricEntityType.APPLICATION, lifecycle_id="lc-app-1", to_state="saved")
    e3 = make_event(seq_id=3, event_type=MetricEventType.RESUME_GENERATED, entity_type=MetricEntityType.RESUME, lifecycle_id="lc-res-1")

    report = aggregate_events([e1, e2, e3], "2026", "2026", True)

    jobs = next(m for m in report.metrics if m.metric_name == MetricType.JOB_OFFERS_SAVED)
    apps = next(m for m in report.metrics if m.metric_name == MetricType.APPLICATIONS_CREATED)
    resumes = next(m for m in report.metrics if m.metric_name == MetricType.RESUMES_GENERATED)

    assert jobs.lifetime.value == 1
    assert apps.lifetime.value == 1
    assert resumes.lifetime.value == 1
