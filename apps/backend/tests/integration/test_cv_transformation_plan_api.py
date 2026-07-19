"""Public API integration coverage for Stage 10A using the isolated database."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.main import app
from app.models import MetricEvent
from app.services.career_positioning_report import build_career_positioning_report


APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def library(entries=None):
    return {
        "meta": {"wersja": "test"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": entries
                if entries is not None
                else [
                    {
                        "id": "internal-truth-id",
                        "firma": "Example Company",
                        "stanowisko": "Engineering Manager",
                        "okresOd": "2020-01",
                        "okresDo": "2025-12",
                        "aktywnosci": ["zarządzanie zespołem 6 osób", "Python", "SQL"],
                        "wynikLiczbowy": "Wzrost o 25%",
                        "status": APPROVED,
                        "uzywacWCV": True,
                    },
                    {
                        "id": "internal-truth-id-2",
                        "firma": "Example Lab",
                        "stanowisko": "Python Specialist",
                        "okresOd": "2017-01",
                        "okresDo": "2019-12",
                        "aktywnosci": ["analiza danych"],
                        "status": APPROVED,
                        "uzywacWCV": True,
                    },
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
            "tranferowalneKompetencje": {"mapowania": {"analiza danych": "business intelligence"}},
            "branze": {"zatwierdzoneBranze": []},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
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
        "education": [],
        "personalProjects": [],
        "additional": {
            "technicalSkills": ["Python", "SQL", "Docker"],
            "languages": [],
            "certificationsTraining": [],
            "awards": [],
        },
        "sectionMeta": [],
        "customSections": {},
    }


@pytest.fixture
def api_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def linked_records(isolated_db):
    resume = await isolated_db.create_resume(
        content="# Resume",
        processed_data=processed_resume(),
        processing_status="ready",
    )
    job = await isolated_db.create_job(
        content="Python Engineering Manager\nWymagane Python SQL i zarządzanie zespołem",
        resume_id=resume["resume_id"],
    )
    return resume, job


def install_truth(monkeypatch, value=None):
    monkeypatch.setattr(
        "app.routers.career_positioning.get_truth_library",
        lambda: deepcopy(value if value is not None else library()),
    )


def endpoint(records):
    resume, job = records
    return f"/api/v1/jobs/{job['job_id']}/resumes/{resume['resume_id']}/transformation-plan"


async def metric_count(database):
    async with database._session() as session:
        return int((await session.scalar(select(func.count()).select_from(MetricEvent))) or 0)


async def test_01_returns_http_200(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    response = await api_client.get(endpoint(linked_records))
    assert response.status_code == 200


async def test_02_missing_job_returns_404(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    resume, _ = linked_records
    response = await api_client.get(f"/api/v1/jobs/missing/resumes/{resume['resume_id']}/transformation-plan")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "JOB_NOT_FOUND"


async def test_03_missing_resume_returns_404(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    _, job = linked_records
    response = await api_client.get(f"/api/v1/jobs/{job['job_id']}/resumes/missing/transformation-plan")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RESUME_NOT_FOUND"


async def test_04_mismatched_relation_returns_422(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    _, job = linked_records
    other = await isolated_db.create_resume(content="Other", processed_data=processed_resume(), processing_status="ready")
    response = await api_client.get(f"/api/v1/jobs/{job['job_id']}/resumes/{other['resume_id']}/transformation-plan")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INCONSISTENT_JOB_RESUME_RELATION"


async def test_05_unstructured_resume_returns_422(api_client, isolated_db, monkeypatch):
    install_truth(monkeypatch)
    resume = await isolated_db.create_resume(content="Pending")
    job = await isolated_db.create_job(content="Role", resume_id=resume["resume_id"])
    response = await api_client.get(endpoint((resume, job)))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "RESUME_NOT_READY"


async def test_06_response_contains_complete_contract(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    data = (await api_client.get(endpoint(linked_records))).json()
    assert set(data) == {
        "plan_version", "plan_fingerprint", "resume_id", "job_id", "generated_at", "detected_job_level",
        "candidate_profile_level", "positioning_strategy", "requires_human_review",
        "summary_plan", "experience_plan", "competencies_plan", "achievements_plan",
        "tools_plan", "evidence_permissions", "prohibited_claims", "limitations",
        "source_summary",
    }


async def test_07_all_plan_sections_are_arrays(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    data = (await api_client.get(endpoint(linked_records))).json()
    for key in ("summary_plan", "experience_plan", "competencies_plan", "achievements_plan", "tools_plan", "prohibited_claims", "limitations"):
        assert isinstance(data[key], list)


@pytest.mark.parametrize(
    ("strategy", "level"),
    [("EXPERT_AUTHORITY", "EXPERT"), ("MANAGER_LEADERSHIP", "MANAGER"), ("DIRECTOR_SCALE", "DIRECTOR")],
)
async def test_08_to_10_positioning_scenarios(
    strategy, level, api_client, isolated_db, linked_records, monkeypatch
):
    install_truth(monkeypatch)
    original = build_career_positioning_report

    def scenario_report(job, truth, now):
        report = original(job, truth, now)
        report.positioning.positioning_strategy = strategy
        report.positioning.detected_job_level = level
        return report

    monkeypatch.setattr("app.routers.career_positioning.build_career_positioning_report", scenario_report)
    data = (await api_client.get(endpoint(linked_records))).json()
    assert data["positioning_strategy"] == strategy
    assert data["detected_job_level"] == level


async def test_11_returns_structured_prohibited_claims(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch, library(entries=[]))
    claims = (await api_client.get(endpoint(linked_records))).json()["prohibited_claims"]
    assert len(claims) == 10
    assert all(set(item) == {"code", "reason_code", "reason", "human_review_required"} for item in claims)


async def test_12_response_does_not_expose_private_fields(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    text = (await api_client.get(endpoint(linked_records))).text
    for forbidden in ("internal-truth-id", "truth_item_id", "metadata_json", "lifecycle_token", "operation_key"):
        assert forbidden not in text


async def test_13_get_creates_no_metric_event(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    before = await metric_count(isolated_db)
    await api_client.get(endpoint(linked_records))
    assert await metric_count(isolated_db) == before


async def test_14_get_does_not_mutate_resume_or_job(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    resume, job = linked_records
    before_resume = await isolated_db.get_resume(resume["resume_id"])
    before_job = await isolated_db.get_job(job["job_id"])
    await api_client.get(endpoint(linked_records))
    assert await isolated_db.get_resume(resume["resume_id"]) == before_resume
    assert await isolated_db.get_job(job["job_id"]) == before_job


async def test_15_repeated_get_is_deterministic(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    first = (await api_client.get(endpoint(linked_records))).json()
    second = (await api_client.get(endpoint(linked_records))).json()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


async def test_16_unapproved_data_is_filtered(api_client, isolated_db, linked_records, monkeypatch):
    unapproved = {
        "id": "hidden-id",
        "firma": "Hidden",
        "stanowisko": "Director",
        "aktywnosci": ["odpowiedzialność P&L"],
        "status": "NIEJASNE",
        "uzywacWCV": True,
    }
    value = library(entries=[unapproved])
    value["kategorie"]["reguly"]["statusyDoAutomatycznegoCV"].append("NIEJASNE")
    install_truth(monkeypatch, value)
    data = (await api_client.get(endpoint(linked_records))).json()
    assert data["source_summary"]["approved_truth_items_count"] == 0
    assert "PNL_WITHOUT_EVIDENCE" in {item["code"] for item in data["prohibited_claims"]}


async def test_17_endpoint_does_not_invoke_text_generation(api_client, isolated_db, linked_records, monkeypatch):
    install_truth(monkeypatch)
    called = False

    async def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("text generation must not be called")

    monkeypatch.setattr("app.services.improver.generate_resume_diffs", forbidden)
    response = await api_client.get(endpoint(linked_records))
    assert response.status_code == 200
    assert called is False


async def test_18_application_is_unchanged_by_plan_preview(
    api_client, isolated_db, linked_records, monkeypatch
):
    install_truth(monkeypatch)
    resume, job = linked_records
    application = await isolated_db.create_application(
        job_id=job["job_id"],
        resume_id=resume["resume_id"],
        company="Example Company",
        role="Engineering Manager",
        notes="Unchanged note",
    )
    before = await isolated_db.get_application(application["application_id"])
    metrics_before = await metric_count(isolated_db)
    response = await api_client.get(endpoint(linked_records))
    assert response.status_code == 200
    assert await isolated_db.get_application(application["application_id"]) == before
    assert await metric_count(isolated_db) == metrics_before


async def test_19_all_ten_guardrails_remain_with_approved_evidence(
    api_client, isolated_db, linked_records, monkeypatch
):
    install_truth(monkeypatch)
    data = (await api_client.get(endpoint(linked_records))).json()
    codes = [item["code"] for item in data["prohibited_claims"]]
    assert len(codes) == 10
    assert codes == sorted(set(codes))
    assert "TECHNICAL_SKILL_WITHOUT_EVIDENCE" in codes
    assert "QUANTIFIED_RESULT_WITHOUT_EVIDENCE" in codes


async def test_20_permissions_are_concrete_and_scoped(
    api_client, isolated_db, linked_records, monkeypatch
):
    install_truth(monkeypatch)
    data = (await api_client.get(endpoint(linked_records))).json()
    item_references = {
        item["source_reference"]
        for section in (
            "summary_plan",
            "experience_plan",
            "competencies_plan",
            "achievements_plan",
            "tools_plan",
        )
        for item in data[section]
    }
    permissions = data["evidence_permissions"]
    assert permissions
    assert all(item["source_reference"] in item_references for item in permissions)
    assert all(item["allowed_operation"] != "GENERATE_NEW" for item in permissions)
    achievement_reference = data["achievements_plan"][0]["source_reference"]
    assert any(
        item["claim_code"] == "QUANTIFIED_RESULT_WITHOUT_EVIDENCE"
        and item["source_reference"] == achievement_reference
        for item in permissions
    )


async def test_21_raw_truth_content_is_not_exposed(
    api_client, isolated_db, linked_records, monkeypatch
):
    value = library()
    value["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append(
        "confidential-private-truth-value"
    )
    install_truth(monkeypatch, value)
    text = (await api_client.get(endpoint(linked_records))).text
    assert "confidential-private-truth-value" not in text
    assert "internal-truth-id" not in text
