"""Public FastAPI and isolated-db coverage for Stage 10B approvals."""

import asyncio
from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.main import app
from app.models import Application, CVTransformationPlanApproval, MetricEvent, Resume


APPROVED_TRUTH = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def truth_library():
    return {
        "meta": {"wersja": "anonymous-test"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "private-source-id",
                        "firma": "Example Company",
                        "stanowisko": "Engineering Manager",
                        "okresOd": "2020-01",
                        "okresDo": "2025-12",
                        "aktywnosci": ["Python", "zarządzanie zespołem"],
                        "wynikLiczbowy": "Wzrost o 25%",
                        "status": APPROVED_TRUTH,
                        "uzywacWCV": True,
                    }
                ]
            },
            "kompetencje": {"wpisy": []},
            "narzedzia": {"wpisy": []},
            "technologie": {"wpisy": []},
            "certyfikaty": {"wpisy": []},
            "kursy": {"wpisy": []},
            "wyksztalcenie": {"wpisy": []},
            "jezyki": {"wpisy": []},
            "osiagnieciaLiczbowe": {"wpisy": []},
            "skalaOdpowiedzialnosci": {"wpisy": []},
            "tranferowalneKompetencje": {"mapowania": {}},
            "branze": {"zatwierdzoneBranze": []},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED_TRUTH],
                "statusyWymagajaceAkceptacji": ["DO_AKCEPTACJI"],
                "statusyZablokowane": ["ZABLOKOWANE"],
            },
        },
    }


def processed_resume():
    return {
        "personalInfo": {"name": "Example Candidate"},
        "summary": "Python engineering manager",
        "workExperience": [
            {
                "id": 1,
                "title": "Engineering Manager",
                "company": "Example Company",
                "years": "2020-2025",
                "description": ["Wzrost o 25%", "Python delivery"],
            }
        ],
        "additional": {
            "technicalSkills": ["Python"],
            "languages": [],
            "certificationsTraining": [],
            "awards": [],
        },
    }


@pytest.fixture
def api_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def records(isolated_db):
    resume = await isolated_db.create_resume(
        content="# Anonymous resume",
        processed_data=processed_resume(),
        processing_status="ready",
    )
    job = await isolated_db.create_job(
        content="Python Engineering Manager\nPython leadership",
        resume_id=resume["resume_id"],
    )
    return resume, job


@pytest.fixture(autouse=True)
def install_truth(monkeypatch):
    value = truth_library()
    monkeypatch.setattr(
        "app.routers.career_positioning.get_truth_library",
        lambda: deepcopy(value),
    )


def plan_path(records):
    resume, job = records
    return f"/api/v1/jobs/{job['job_id']}/resumes/{resume['resume_id']}/transformation-plan"


def approval_path(records):
    return f"{plan_path(records)}/approval"


async def current_plan(client, records):
    response = await client.get(plan_path(records))
    assert response.status_code == 200
    return response.json()


def all_references(plan):
    return sorted(
        item["source_reference"]
        for key in (
            "summary_plan",
            "experience_plan",
            "competencies_plan",
            "achievements_plan",
            "tools_plan",
        )
        for item in plan[key]
    )


def payload(plan, *, decision="ACCEPT", submit=True, acknowledged=True):
    return {
        "plan_fingerprint": plan["plan_fingerprint"],
        "decisions": [
            {"source_reference": reference, "decision": decision}
            for reference in all_references(plan)
        ],
        "guardrails_acknowledged": acknowledged,
        "submit": submit,
    }


async def approval_metric_count(database):
    async with database._session() as session:
        return int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(MetricEvent)
                    .where(MetricEvent.event_type == "TRANSFORMATION_PLAN_DECIDED")
                )
            )
            or 0
        )


async def approval_row_count(database):
    async with database._session() as session:
        return int(
            (await session.scalar(select(func.count()).select_from(CVTransformationPlanApproval)))
            or 0
        )


async def test_01_get_plan_contains_backend_fingerprint(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    assert len(plan["plan_fingerprint"]) == 64


async def test_02_repeated_get_has_stable_fingerprint(api_client, isolated_db, records):
    first = await current_plan(api_client, records)
    second = await current_plan(api_client, records)
    assert first["plan_fingerprint"] == second["plan_fingerprint"]


async def test_03_post_draft(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    response = await api_client.post(
        approval_path(records), json=payload(plan, submit=False, acknowledged=False)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT"


async def test_04_post_approved(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    response = await api_client.post(approval_path(records), json=payload(plan))
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"


async def test_05_get_current_approval(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    saved = (await api_client.post(approval_path(records), json=payload(plan))).json()
    fetched = await api_client.get(approval_path(records))
    assert fetched.status_code == 200
    assert fetched.json() == saved


async def test_06_missing_job_returns_404(api_client, isolated_db, records):
    resume, _ = records
    response = await api_client.post(
        f"/api/v1/jobs/missing/resumes/{resume['resume_id']}/transformation-plan/approval",
        json={"plan_fingerprint": "a" * 64},
    )
    assert response.status_code == 404


async def test_07_missing_resume_returns_404(api_client, isolated_db, records):
    _, job = records
    response = await api_client.post(
        f"/api/v1/jobs/{job['job_id']}/resumes/missing/transformation-plan/approval",
        json={"plan_fingerprint": "a" * 64},
    )
    assert response.status_code == 404


async def test_08_missing_approval_returns_404(api_client, isolated_db, records):
    response = await api_client.get(approval_path(records))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "TRANSFORMATION_PLAN_APPROVAL_NOT_FOUND"


async def test_09_stale_fingerprint_returns_409(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    body = payload(plan)
    body["plan_fingerprint"] = "f" * 64
    response = await api_client.post(approval_path(records), json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STALE_TRANSFORMATION_PLAN"


async def test_10_missing_decisions_returns_422(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    body = payload(plan)
    body["decisions"] = []
    response = await api_client.post(approval_path(records), json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INCOMPLETE_DECISIONS"


async def test_11_duplicate_reference_returns_422(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    body = payload(plan)
    body["decisions"].append(body["decisions"][0])
    assert (await api_client.post(approval_path(records), json=body)).status_code == 422


async def test_12_unknown_reference_returns_422(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    body = payload(plan, submit=False)
    body["decisions"] = [
        {"source_reference": "resume:summary:9999", "decision": "ACCEPT"}
    ]
    response = await api_client.post(approval_path(records), json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNKNOWN_SOURCE_REFERENCE"


async def test_13_identical_post_is_idempotent(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    first = (await api_client.post(approval_path(records), json=payload(plan))).json()
    second = (await api_client.post(approval_path(records), json=payload(plan))).json()
    assert second["approval_id"] == first["approval_id"]
    assert second["created_at"] == first["created_at"]


async def test_14_first_save_emits_one_metric(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    assert await approval_metric_count(isolated_db) == 0
    await api_client.post(approval_path(records), json=payload(plan, submit=False))
    assert await approval_metric_count(isolated_db) == 1


async def test_15_repeat_save_emits_no_duplicate_metric(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    body = payload(plan, submit=False)
    await api_client.post(approval_path(records), json=body)
    await api_client.post(approval_path(records), json=body)
    assert await approval_metric_count(isolated_db) == 1


async def test_16_post_does_not_mutate_resume(api_client, isolated_db, records):
    resume, _ = records
    plan = await current_plan(api_client, records)
    before = await isolated_db.get_resume(resume["resume_id"])
    await api_client.post(approval_path(records), json=payload(plan))
    assert await isolated_db.get_resume(resume["resume_id"]) == before


async def test_17_post_does_not_mutate_job(api_client, isolated_db, records):
    _, job = records
    plan = await current_plan(api_client, records)
    before = await isolated_db.get_job(job["job_id"])
    await api_client.post(approval_path(records), json=payload(plan))
    assert await isolated_db.get_job(job["job_id"]) == before


async def test_18_post_does_not_mutate_application(api_client, isolated_db, records):
    resume, job = records
    application = await isolated_db.create_application(
        job_id=job["job_id"], resume_id=resume["resume_id"]
    )
    before = await isolated_db.get_application(application["application_id"])
    plan = await current_plan(api_client, records)
    await api_client.post(approval_path(records), json=payload(plan))
    assert await isolated_db.get_application(application["application_id"]) == before


async def test_19_post_creates_no_resume(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    async with isolated_db._session() as session:
        before = await session.scalar(select(func.count()).select_from(Resume))
    await api_client.post(approval_path(records), json=payload(plan))
    async with isolated_db._session() as session:
        after = await session.scalar(select(func.count()).select_from(Resume))
    assert after == before


async def test_20_post_does_not_call_llm(api_client, isolated_db, records, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr("app.services.improver.generate_resume_diffs", forbidden)
    plan = await current_plan(api_client, records)
    assert (await api_client.post(approval_path(records), json=payload(plan))).status_code == 200


async def test_21_approval_dto_is_private(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    text = (await api_client.post(approval_path(records), json=payload(plan))).text
    for forbidden in (
        "private-source-id",
        "metadata_json",
        "lifecycle_token",
        "operation_key",
        "Anonymous resume",
    ):
        assert forbidden not in text


@pytest.mark.parametrize(
    ("decision", "status"),
    [("ACCEPT", "APPROVED"), ("REJECT", "APPROVED"), ("REQUEST_REVIEW", "REQUIRES_REVIEW")],
)
async def test_22_to_24_all_decisions(
    decision, status, api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    response = await api_client.post(
        approval_path(records), json=payload(plan, decision=decision)
    )
    assert response.status_code == 200
    assert response.json()["status"] == status


async def test_25_all_plan_sections_can_be_decided(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    response = await api_client.post(approval_path(records), json=payload(plan))
    assert len(response.json()["decisions"]) == len(all_references(plan))


async def test_26_resume_change_makes_old_request_stale(api_client, isolated_db, records):
    resume, _ = records
    plan = await current_plan(api_client, records)
    changed = processed_resume()
    changed["summary"] = "Changed anonymous summary"
    await isolated_db.update_resume(resume["resume_id"], {"processed_data": changed})
    assert (await api_client.post(approval_path(records), json=payload(plan))).status_code == 409


async def test_27_job_change_makes_old_request_stale(api_client, isolated_db, records):
    _, job = records
    plan = await current_plan(api_client, records)
    await isolated_db.update_job(job["job_id"], {"content": "Changed manager role"})
    assert (await api_client.post(approval_path(records), json=payload(plan))).status_code == 409


async def test_28_approval_does_not_remove_guardrails(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    await api_client.post(approval_path(records), json=payload(plan))
    refreshed = await current_plan(api_client, records)
    assert len(refreshed["prohibited_claims"]) == 10


async def test_29_saved_decisions_are_sorted(api_client, isolated_db, records):
    plan = await current_plan(api_client, records)
    body = payload(plan, submit=False)
    body["decisions"].reverse()
    saved = (await api_client.post(approval_path(records), json=body)).json()
    references = [item["source_reference"] for item in saved["decisions"]]
    assert references == sorted(references)


async def test_30_old_approval_is_exposed_as_superseded(api_client, isolated_db, records):
    resume, _ = records
    plan = await current_plan(api_client, records)
    await api_client.post(approval_path(records), json=payload(plan))
    changed = processed_resume()
    changed["summary"] = "Updated anonymous summary"
    await isolated_db.update_resume(resume["resume_id"], {"processed_data": changed})
    response = await api_client.get(approval_path(records))
    assert response.status_code == 200
    assert response.json()["status"] == "SUPERSEDED"


async def test_r1_empty_draft_creates_approval_without_decided_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    body = payload(plan, submit=False, acknowledged=False)
    body["decisions"] = []
    response = await api_client.post(approval_path(records), json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT"
    assert await approval_row_count(isolated_db) == 1
    assert await approval_metric_count(isolated_db) == 0


async def test_r1_first_decision_after_empty_draft_emits_one_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    empty = payload(plan, submit=False, acknowledged=False)
    empty["decisions"] = []
    saved = (await api_client.post(approval_path(records), json=empty)).json()
    first = deepcopy(empty)
    first["decisions"] = [
        {"source_reference": all_references(plan)[0], "decision": "ACCEPT"}
    ]
    updated = (await api_client.post(approval_path(records), json=first)).json()
    assert updated["approval_id"] == saved["approval_id"]
    assert await approval_metric_count(isolated_db) == 1


async def test_r1_later_decision_updates_emit_no_additional_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    body = payload(plan, submit=False, acknowledged=False)
    body["decisions"] = [
        {"source_reference": all_references(plan)[0], "decision": "ACCEPT"}
    ]
    await api_client.post(approval_path(records), json=body)
    body["decisions"][0]["decision"] = "REJECT"
    await api_client.post(approval_path(records), json=body)
    await api_client.post(approval_path(records), json=body)
    assert await approval_metric_count(isolated_db) == 1


async def test_r1_first_complete_approved_emits_one_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    response = await api_client.post(approval_path(records), json=payload(plan))
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert await approval_metric_count(isolated_db) == 1


async def test_r1_first_requires_review_emits_one_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    response = await api_client.post(
        approval_path(records), json=payload(plan, decision="REQUEST_REVIEW")
    )
    assert response.status_code == 200
    assert response.json()["status"] == "REQUIRES_REVIEW"
    assert await approval_metric_count(isolated_db) == 1


async def test_r1_guardrail_acknowledgment_without_decision_emits_no_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    body = payload(plan, submit=False, acknowledged=True)
    body["decisions"] = []
    response = await api_client.post(approval_path(records), json=body)
    assert response.status_code == 200
    assert await approval_metric_count(isolated_db) == 0


async def test_r1_parallel_identical_posts_share_approval_and_metric(
    api_client, isolated_db, records
):
    plan = await current_plan(api_client, records)
    body = payload(plan)
    first, second = await asyncio.gather(
        api_client.post(approval_path(records), json=body),
        api_client.post(approval_path(records), json=body),
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["approval_id"] == second.json()["approval_id"]
    assert await approval_row_count(isolated_db) == 1
    assert await approval_metric_count(isolated_db) == 1
