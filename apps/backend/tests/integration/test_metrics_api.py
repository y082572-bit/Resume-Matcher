import pytest
from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func

from app.main import app
from app.models import Job, MetricEvent, MetricBootstrapState, Resume, Application
from app.services.project_metrics import MetricType
from app.services.project_metrics_bootstrap import bootstrap_metrics
from app.schemas.metrics import MetricsResponseSchema


@pytest.fixture
def client():
    """Async HTTP client for endpoint testing."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_get_metrics_returns_200(isolated_db, client):
    """1. GET /api/v1/metrics returns 200 after bootstrap."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert resp.status_code == 200


async def test_response_contains_schema_version(isolated_db, client):
    """2. Response payload contains schema_version key."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "schema_version" in resp.json()


async def test_schema_version_is_one_point_zero(isolated_db, client):
    """3. schema_version is exactly '1.0'."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert resp.json()["schema_version"] == "1.0"


async def test_generated_at_is_timezone_aware(isolated_db, client):
    """4. generated_at is a timezone-aware ISO-8601 string."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    gen_at = resp.json()["generated_at"]
    assert gen_at.endswith("Z") or "+" in gen_at
    dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
    assert dt.tzinfo is not None


async def test_response_contains_history(isolated_db, client):
    """5. Response payload contains history block."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "history" in resp.json()


async def test_response_contains_capabilities(isolated_db, client):
    """6. Response payload contains capabilities block."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "capabilities" in resp.json()


async def test_response_contains_metrics(isolated_db, client):
    """7. Response payload contains metrics list."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "metrics" in resp.json()
    assert isinstance(resp.json()["metrics"], list)


async def test_all_expected_metrics_present(isolated_db, client):
    """8. All expected metrics are present in response."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    metrics = resp.json()["metrics"]
    names = {m["metric_name"] for m in metrics}
    expected = {t.value for t in MetricType}
    assert expected.issubset(names)


async def test_current_has_correct_availability(isolated_db, client):
    """9. current sub-measure has correct availability."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    metrics = resp.json()["metrics"]
    for m in metrics:
        if m["metric_name"] != "status_changes":
            assert m["current"]["availability"] == "AVAILABLE"
        else:
            assert m["current"]["availability"] == "UNSUPPORTED"


async def test_archived_remains_unsupported(isolated_db, client):
    """10. archived sub-measure remains UNSUPPORTED for all metrics."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    metrics = resp.json()["metrics"]
    for m in metrics:
        assert m["archived"]["availability"] == "UNSUPPORTED"
        assert m["archived"]["value"] is None


async def test_history_completeness_is_correct(isolated_db, client):
    """11. history completeness is correctly serialized as COMPLETE for empty db."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert resp.json()["history"]["completeness"] == "COMPLETE"


async def test_baseline_at_is_serialized(isolated_db, client):
    """12. baseline_at is serialized correctly in history."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "baseline_at" in resp.json()["history"]


async def test_empty_database_yields_zero_counters(isolated_db, client):
    """13. Empty database yields zero counters across all metrics."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    metrics = resp.json()["metrics"]
    for m in metrics:
        if m["current"]["availability"] == "AVAILABLE":
            assert m["current"]["value"] == 0
        if m["lifetime"]["availability"] == "AVAILABLE":
            assert m["lifetime"]["value"] == 0
        if m["peak"]["availability"] == "AVAILABLE":
            assert m["peak"]["value"] == 0


async def test_existing_data_yields_correct_current_values(isolated_db, client):
    """14. Existing data yields correct current values after bootstrap."""
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with client:
        resp = await client.get("/api/v1/metrics")
    metrics = resp.json()["metrics"]
    job_metric = next(m for m in metrics if m["metric_name"] == "job_offers_saved")
    assert job_metric["current"]["value"] == 1


async def test_get_does_not_create_events(isolated_db, client):
    """15. GET /api/v1/metrics does not create new metric events."""
    await bootstrap_metrics(isolated_db)
    async with isolated_db._session() as session:
        count1 = (await session.execute(select(func.count()).select_from(MetricEvent))).scalar()

    async with client:
        await client.get("/api/v1/metrics")

    async with isolated_db._session() as session:
        count2 = (await session.execute(select(func.count()).select_from(MetricEvent))).scalar()
        assert count1 == count2


async def test_second_get_does_not_create_events(isolated_db, client):
    """16. Subsequent GET request does not create new metric events either."""
    await bootstrap_metrics(isolated_db)
    async with client:
        await client.get("/api/v1/metrics")
        await client.get("/api/v1/metrics")

    async with isolated_db._session() as session:
        count = (await session.execute(select(func.count()).select_from(MetricEvent))).scalar()
        # Since DB was empty before bootstrap, count should be 0
        assert count == 0


async def test_response_does_not_contain_lifecycle_token(isolated_db, client):
    """17. Response payload does not expose lifecycle_token."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "lifecycle_token" not in resp.text
    assert "lifecycle_id" not in resp.text


async def test_response_does_not_contain_entity_id(isolated_db, client):
    """18. Response payload does not expose entity_id."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "entity_id" not in resp.text
    assert "job_id" not in resp.text
    assert "resume_id" not in resp.text
    assert "application_id" not in resp.text


async def test_response_does_not_contain_operation_key(isolated_db, client):
    """19. Response payload does not expose operation_key."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "operation_key" not in resp.text


async def test_response_does_not_contain_raw_event_list(isolated_db, client):
    """20. Response payload does not expose raw event list."""
    await bootstrap_metrics(isolated_db)
    async with client:
        resp = await client.get("/api/v1/metrics")
    assert "events" not in resp.json()


async def test_post_fails(client):
    """21. POST /api/v1/metrics is not supported (returns 405)."""
    async with client:
        resp = await client.post("/api/v1/metrics", json={})
    assert resp.status_code == 405


async def test_put_fails(client):
    """22. PUT /api/v1/metrics is not supported (returns 405)."""
    async with client:
        resp = await client.put("/api/v1/metrics", json={})
    assert resp.status_code == 405


async def test_patch_fails(client):
    """23. PATCH /api/v1/metrics is not supported (returns 405)."""
    async with client:
        resp = await client.patch("/api/v1/metrics", json={})
    assert resp.status_code == 405


async def test_delete_fails(client):
    """24. DELETE /api/v1/metrics is not supported (returns 405)."""
    async with client:
        resp = await client.delete("/api/v1/metrics")
    assert resp.status_code == 405


def test_response_model_rejects_negative_values():
    """25. Pydantic validation schema rejects negative values."""
    from pydantic import ValidationError
    # Attempting to load negative value in MetricMeasureSchema should raise ValidationError
    with pytest.raises(ValidationError):
        MetricsResponseSchema(
            generated_at="2026-07-16T21:40:00Z",
            history={
                "bootstrap_version": 1,
                "baseline_at": "2026-07-16T21:40:00Z",
                "complete_since": "2026-07-16T21:40:00Z",
                "completeness": "COMPLETE",
                "baseline_event_count": -5,  # negative
            },
            capabilities={},
            metrics=[],
        )


async def test_api_partial_baseline(isolated_db, client):
    """26. Test API on non-empty database with PARTIAL_BASELINE and verify capabilities are AVAILABLE."""
    # Seed some data to make database not empty
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()

    await bootstrap_metrics(isolated_db)

    async with client:
        resp = await client.get("/api/v1/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["history"]["completeness"] == "PARTIAL_BASELINE"

    # Verify capabilities/metrics availability are AVAILABLE (unaffected by PARTIAL_BASELINE)
    for metric in data["metrics"]:
        if metric["metric_name"] != "status_changes":
            assert metric["current"]["availability"] == "AVAILABLE"
            assert metric["lifetime"]["availability"] == "AVAILABLE"
            assert metric["peak"]["availability"] == "AVAILABLE"
        else:
            assert metric["current"]["availability"] == "UNSUPPORTED"
            assert metric["lifetime"]["availability"] == "AVAILABLE"


def test_schema_validation_rejections():
    """27. Schema validation rejections for wrong schema_version, availability, completeness, dates, etc."""
    from pydantic import ValidationError

    # Helper function to generate a valid base payload
    def get_valid_payload():
        return {
            "schema_version": "1.0",
            "generated_at": "2026-07-16T21:40:00Z",
            "history": {
                "bootstrap_version": 1,
                "baseline_at": "2026-07-16T21:40:00Z",
                "complete_since": "2026-07-16T21:40:00Z",
                "completeness": "COMPLETE",
                "baseline_event_count": 0,
            },
            "capabilities": {},
            "metrics": [
                {
                    "metric_name": "job_offers_saved",
                    "current": {"value": 0, "availability": "AVAILABLE"},
                    "lifetime": {"value": 0, "availability": "AVAILABLE"},
                    "peak": {"value": 0, "availability": "AVAILABLE"},
                    "archived": {"value": None, "availability": "UNSUPPORTED"},
                }
            ],
        }

    # 1. schema_version="999" (rejected)
    payload = get_valid_payload()
    payload["schema_version"] = "999"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 2. availability="WRONG" (rejected)
    payload = get_valid_payload()
    payload["metrics"][0]["current"]["availability"] = "WRONG"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 3. completeness="WRONG" (rejected)
    payload = get_valid_payload()
    payload["history"]["completeness"] = "WRONG"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 4. generated_at="not-a-date" (rejected)
    payload = get_valid_payload()
    payload["generated_at"] = "not-a-date"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 5. timestamp bez timezone (rejected)
    payload = get_valid_payload()
    payload["generated_at"] = "2026-07-16T21:40:00"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 6. timestamp z nie-UTC offsetem (rejected)
    payload = get_valid_payload()
    payload["generated_at"] = "2026-07-16T21:40:00+02:00"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 7. ujemne wartości każdej miary (rejected)
    # current value negative
    payload = get_valid_payload()
    payload["metrics"][0]["current"]["value"] = -1
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # baseline_event_count negative
    payload = get_valid_payload()
    payload["history"]["baseline_event_count"] = -10
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 8. nieznana nazwa metryki (rejected)
    payload = get_valid_payload()
    payload["metrics"][0]["metric_name"] = "unknown_metric"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)

    # 9. dodatkowe nieznane pola (extra="forbid")
    payload = get_valid_payload()
    payload["unknown_extra_field"] = "value"
    with pytest.raises(ValidationError):
        MetricsResponseSchema(**payload)


async def test_api_no_side_effects_get(isolated_db, client):
    """28. Two GET requests have zero side effects on the database state and records."""
    await bootstrap_metrics(isolated_db)

    async def get_db_snapshot():
        async with isolated_db._session() as session:
            evts_cnt = (await session.execute(select(func.count()).select_from(MetricEvent))).scalar()
            jobs_cnt = (await session.execute(select(func.count()).select_from(Job))).scalar()
            res_cnt = (await session.execute(select(func.count()).select_from(Resume))).scalar()
            app_cnt = (await session.execute(select(func.count()).select_from(Application))).scalar()
            from app.models import Improvement
            imp_cnt = (await session.execute(select(func.count()).select_from(Improvement))).scalar()
            boot_state = (await session.execute(select(MetricBootstrapState))).scalars().all()
            boot_data = []
            for bs in boot_state:
                boot_data.append({
                    "bootstrap_key": bs.bootstrap_key,
                    "schema_version": bs.schema_version,
                    "baseline_at": bs.baseline_at,
                    "completed_at": bs.completed_at,
                    "history_completeness": bs.history_completeness,
                    "baseline_event_count": bs.baseline_event_count,
                    "database_was_empty": bs.database_was_empty,
                })
            return (evts_cnt, jobs_cnt, res_cnt, app_cnt, imp_cnt, boot_data)

    snapshot_before = await get_db_snapshot()

    async with client:
        await client.get("/api/v1/metrics")
        await client.get("/api/v1/metrics")

    snapshot_after = await get_db_snapshot()
    assert snapshot_before == snapshot_after


async def test_api_no_side_effects_non_get(isolated_db, client):
    """29. POST, PUT, PATCH, DELETE return 405/404 and leave db completely unmodified."""
    await bootstrap_metrics(isolated_db)

    async def get_db_dump():
        # Get raw content of all tables to compare exactly
        async with isolated_db._session() as session:
            from app.models import Improvement
            evts = [(e.event_id, e.operation_key, e.event_type) for e in (await session.execute(select(MetricEvent))).scalars().all()]
            jobs = [j.job_id for j in (await session.execute(select(Job))).scalars().all()]
            resumes = [r.resume_id for r in (await session.execute(select(Resume))).scalars().all()]
            apps = [a.application_id for a in (await session.execute(select(Application))).scalars().all()]
            imps = [i.improvement_id for i in (await session.execute(select(Improvement))).scalars().all()]
            boot = [b.bootstrap_key for b in (await session.execute(select(MetricBootstrapState))).scalars().all()]
            return (evts, jobs, resumes, apps, imps, boot)

    snapshot_before = await get_db_dump()

    async with client:
        for method in ["post", "put", "patch", "delete"]:
            call_method = getattr(client, method)
            if method in ["post", "put", "patch"]:
                resp = await call_method("/api/v1/metrics", json={})
            else:
                resp = await call_method("/api/v1/metrics")
            assert resp.status_code in (404, 405)

    snapshot_after = await get_db_dump()
    assert snapshot_before == snapshot_after


async def test_privacy_check_expanded(isolated_db, client):
    """30. Metrics response does not contain database-internal keys or privacy sensitive keys."""
    # Seed some data to make sure metric counters are not all empty
    async with isolated_db._session() as session:
        job = Job(job_id="job-1", content="Desc", lifecycle_token="lc-job-1")
        session.add(job)
        await session.commit()
    await bootstrap_metrics(isolated_db)

    async with client:
        resp = await client.get("/api/v1/metrics")
    assert resp.status_code == 200
    body = resp.text

    forbidden_keys = [
        "job_id", "resume_id", "application_id", "entity_id",
        "lifecycle_id", "lifecycle_token", "operation_key",
        "content", "filename", "company", "title",
        "email", "phone", "events"
    ]
    for key in forbidden_keys:
        assert key not in body, f"Found private/internal key '{key}' in response: {body}"
