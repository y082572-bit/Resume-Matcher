"""Integration tests for Career Positioning Engine endpoints."""

import pytest
from datetime import datetime, timezone
from typing import Dict, Any, List
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from pydantic import ValidationError

from app.main import app
from app.models import Job, Resume, Application, MetricEvent
from app.services.career_positioning import RoleLevel, PositioningStrategy, TitleTreatment
from app.services.career_positioning_report import build_career_positioning_report
from app.schemas.career_positioning import CareerPositioningResponse, RisksSchema, RiskItemSchema, NarrativeVariantSchema, StrategySchema


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def get_valid_payload_fixture() -> Dict[str, Any]:
    """Factory returning a typed, mutable fixture payload for test isolation."""
    return {
        "meta": {
            "kandidat": "Jan Testowy",
            "wersja": "1.1",
            "dataAktualizacji": "2026-07-16",
            "status": "AKTYWNA"
        },
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1",
                        "firma": "Firma Alpha",
                        "stanowisko": "Regionalny Menedżer Sprzedaży",
                        "okresOd": "2024-08",
                        "okresDo": "2025-07",
                        "aktywnosci": ["zarządzanie sprzedażą", "budżet marketingowy", "odpowiedzialność za P&L"],
                        "wynikLiczbowy": "123% wolumenu",
                        "skalaOdpowiedzialnosci": "zarządzanie zespołem do 10 osób",
                        "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
                        "uzywacWCV": True,
                    },
                    {
                        "id": "dz-2",
                        "firma": "Firma Beta",
                        "stanowisko": "Menedżer ds. Projektów",
                        "okresOd": "2023-04",
                        "okresDo": "2024-03",
                        "aktywnosci": ["współpraca z partnerami"],
                        "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
                        "uzywacWCV": True,
                    }
                ]
            },
            "tranferowalneKompetencje": {
                "mapowania": {
                    "współpraca z partnerami": "partner management"
                }
            },
            "zakazy": {
                "bezwzgledneZakazy": ["praca w banku"],
                "wyjatki": []
            },
            "reguly": {
                "statusyDoAutomatycznegoCV": ["PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": []
            },
            "branze": {
                "zatwierdzoneBranze": ["Ubezpieczenia", "Finanse"]
            }
        }
    }


@pytest.fixture
def sample_job() -> Dict[str, Any]:
    return {
        "job_id": "job-123",
        "content": "Poszukujemy Regionalnego Menedżera Sprzedaży. Wymagania: 5 lat doświadczenia. Zarządzanie zespołem, budżet.",
        "role": "Regionalny Menedżer Sprzedaży",
        "created_at": "2026-01-01T00:00:00Z",
        "metadata_json": {"role": "Regionalny Menedżer Sprzedaży"}
    }


# 1. Poprawny Job
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_1_valid_job(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == "job-123"
    assert data["job_title"] == "Regionalny Menedżer Sprzedaży"


# 2. Nieistniejący Job
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
async def test_2_nonexistent_job(mock_db: AsyncMock, client: AsyncClient) -> None:
    mock_db.get_job.return_value = None

    async with client:
        resp = await client.get("/api/v1/jobs/nonexistent/career-positioning")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "JOB_NOT_FOUND"
    assert detail["message"] == "Job not found"


# 3. Wynik ekspercki
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_3_expert_result(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient) -> None:
    job = {
        "job_id": "job-123",
        "content": "Szukamy Starszego Specjalisty ds. Analiz. Wymagane 10 lat doświadczenia, praca samodzielna.",
        "role": "Starszy Specjalista ds. Analiz"
    }
    library = get_valid_payload_fixture()
    library["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["stanowisko"] = "Starszy Specjalista"
    library["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1]["stanowisko"] = "Starszy Analityk"
    mock_db.get_job.return_value = job
    mock_truth.return_value = library

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy"]["recommended_narrative"] == "EXPERT"


# 4. Wynik menedżerski
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_4_manager_result(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy"]["recommended_narrative"] == "MANAGER"


# 5. Wynik dyrektorski
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_5_director_result(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient) -> None:
    job = {
        "job_id": "job-123",
        "content": "Dyrektor handlowy zarządzający regionem, odpowiedzialność za P&L i budżet 10M PLN.",
        "role": "Dyrektor handlowy"
    }
    library = get_valid_payload_fixture()
    library["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["stanowisko"] = "Dyrektor Zarządzający"
    mock_db.get_job.return_value = job
    mock_truth.return_value = library

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy"]["recommended_narrative"] == "DIRECTOR"


# 6. Overqualification
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_6_overqualification(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient) -> None:
    job = {
        "job_id": "job-123",
        "content": "Młodszy asystent wsparcia.",
        "role": "Młodszy asystent"
    }
    mock_db.get_job.return_value = job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["risks"]["overqualification"]["level"] in ("HIGH", "MEDIUM")


# 7. Underqualification
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_7_underqualification(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient) -> None:
    job = {
        "job_id": "job-123",
        "content": "Szukamy Członka Zarządu. Wymagane 20 lat doświadczenia strategicznego.",
        "role": "Członek Zarządu"
    }
    library = get_valid_payload_fixture()
    library["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["stanowisko"] = "Młodszy programista"
    library["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1]["stanowisko"] = "Asystent"
    mock_db.get_job.return_value = job
    mock_truth.return_value = library

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["risks"]["underqualification"]["level"] in ("HIGH", "MEDIUM")


# 8. Brak ryzyka
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_8_no_risk(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["risks"]["overqualification"]["level"] == "NONE"
    assert data["risks"]["underqualification"]["level"] == "NONE"


# 9. Kompetencje bezpośrednie
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_9_direct_matches(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["competencies"]["direct_matches"]) > 0


# 10. Kompetencje transferowalne
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_10_transferable_matches(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient) -> None:
    job = {
        "job_id": "job-123",
        "content": "Odpowiedzialność za partner management.",
        "role": "Partner Manager"
    }
    mock_db.get_job.return_value = job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["competencies"]["transferable_matches"]) > 0


# 11. Braki krytyczne
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_11_missing_critical(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient) -> None:
    job = {
        "job_id": "job-123",
        "content": "Wymagane Excel i arkusze.",
        "role": "Analityk Excel"
    }
    mock_db.get_job.return_value = job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert any("excel" in c["name"].lower() for c in data["competencies"]["missing_critical"])


# 12. Brak inferowania fałszywych danych
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_12_no_false_inference(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data["evidence"]["direct_evidence_count"] == len(data["competencies"]["direct_matches"])
    assert data["evidence"]["transferable_evidence_count"] == len(data["competencies"]["transferable_matches"])


# 13. Read-only endpoint (Facade assertion)
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_13_readonly(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        await client.get("/api/v1/jobs/job-123/career-positioning")
    assert mock_db.create_resume.call_count == 0
    assert mock_db.update_job.call_count == 0
    assert mock_db.create_application.call_count == 0


# 14. Dwa GET nie zmieniają bazy
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_14_two_gets_no_changes(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp1 = await client.get("/api/v1/jobs/job-123/career-positioning")
        resp2 = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["positioning"]["fit_score"] == resp2.json()["positioning"]["fit_score"]


# 15. Brak MetricEvent
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_15_no_metric_event(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        await client.get("/api/v1/jobs/job-123/career-positioning")
    assert not hasattr(mock_db, "record_metric_event") or mock_db.record_metric_event.call_count == 0


# 16. Response model validation
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_16_response_model(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert "schema_version" in data
    assert "generated_at" in data
    assert "job_id" in data
    assert "job_title" in data
    assert "positioning" in data
    assert "risks" in data
    assert "competencies" in data
    assert "strategy" in data
    assert "evidence" in data
    assert "limitations" in data


# 17. Brak prywatnych ID
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_17_no_private_ids(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    data = resp.json()
    str_data = str(data)
    assert "lifecycle_token" not in str_data
    assert "operation_key" not in str_data


# 18. Deterministyczny wynik
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_18_deterministic(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp1 = await client.get("/api/v1/jobs/job-123/career-positioning")
        resp2 = await client.get("/api/v1/jobs/job-123/career-positioning")
    d1 = resp1.json()
    d2 = resp2.json()
    d1.pop("generated_at", None)
    d2.pop("generated_at", None)
    assert d1 == d2


# 19. Timezone-aware generated_at
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_19_timezone_aware_timestamp(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    timestamp = resp.json()["generated_at"]
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert dt.tzinfo is not None


# 20. Schema version
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_20_schema_version(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.return_value = get_valid_payload_fixture()

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 200
    assert resp.json()["schema_version"] == "1.0"


# 21. Filtrowanie statusów
def test_21_filtrowania_statusow() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["status"] = "NIEJASNE"

    report = build_career_positioning_report(
        {"content": "Manager requirements"},
        lib,
        datetime.now(timezone.utc)
    )
    assert report.evidence.used_truth_items_count == 1


# 22. uzywacWCV=false
def test_22_uzywacWCV_false() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["uzywacWCV"] = False

    report = build_career_positioning_report(
        {"content": "Manager requirements"},
        lib,
        datetime.now(timezone.utc)
    )
    assert report.evidence.used_truth_items_count == 1


# 23. Niezależność od kolejności wpisów
def test_23_niezaleznosci_od_kolejnosci() -> None:
    lib1 = get_valid_payload_fixture()
    lib2 = get_valid_payload_fixture()
    lib2["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        lib1["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1],
        lib1["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]
    ]

    now_time = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    report1 = build_career_positioning_report({"content": "Manager requirements"}, lib1, now_time)
    report2 = build_career_positioning_report({"content": "Manager requirements"}, lib2, now_time)

    d1 = report1.model_dump()
    d2 = report2.model_dump()
    d1.pop("generated_at", None)
    d2.pop("generated_at", None)
    assert d1 == d2


# 24. Bieżąca data przekazana jako now
def test_24_now_daty() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["okresDo"] = "obecnie"
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["stanowisko"] = "Specjalista ds. Analiz"
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = []
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["wynikLiczbowy"] = ""
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["skalaOdpowiedzialnosci"] = ""
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1]["stanowisko"] = "Specjalista ds. Sprzedaży"
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1]["aktywnosci"] = []

    now_2025 = datetime(2025, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    report_2025 = build_career_positioning_report(
        {
            "role": "Starszy Specjalista ds. Analiz",
            "content": "Starszy Specjalista ds. Analiz. Wymagane 3 lata doświadczenia."
        },
        lib,
        now_2025
    )
    now_2026 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    report_2026 = build_career_positioning_report(
        {
            "role": "Starszy Specjalista ds. Analiz",
            "content": "Starszy Specjalista ds. Analiz. Wymagane 3 lata doświadczenia."
        },
        lib,
        now_2026
    )

    assert report_2026.risks.underqualification.level != report_2025.risks.underqualification.level


# 25. Błędna data
def test_25_bledna_data() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["okresOd"] = "invalid-date"

    report = build_career_positioning_report(
        {"content": "Manager requirements"},
        lib,
        datetime.now(timezone.utc)
    )
    assert any("pominięty" in msg for msg in report.limitations.messages)


# 26. Brak domyślnych branż
def test_26_brak_domyslnych_branz() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["branze"] = {"zatwierdzoneBranze": []}

    report = build_career_positioning_report(
        {"content": "Finanse i ubezpieczenia"},
        lib,
        datetime.now(timezone.utc)
    )
    assert report.risks.sector_gap.availability == "UNSUPPORTED"
    assert report.risks.sector_gap.level is None


# 27. Brak domyślnego SPECIALIST (candidate_profile_level = UNKNOWN)
def test_27_brak_domyslnego_specialist() -> None:
    lib = {
        "meta": {"kandidat": "None", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {"wpisy": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": ["APPROVED"],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": []
            }
        }
    }
    report = build_career_positioning_report(
        {"content": "Developer job"},
        lib,
        datetime.now(timezone.utc)
    )
    assert report.positioning.candidate_profile_level == "UNKNOWN"
    assert report.limitations.insufficient_data is True


# 28. SQL obecny w Truth Library
def test_28_sql_obecny() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append("programowanie w języku SQL")

    report = build_career_positioning_report(
        {"content": "Praca z bazą SQL"},
        lib,
        datetime.now(timezone.utc)
    )
    assert any("SQL" in m.name for m in report.competencies.direct_matches)


# 29. SQL brakujący
def test_29_sql_brakujacy() -> None:
    report = build_career_positioning_report(
        {"content": "Praca z bazą SQL"},
        get_valid_payload_fixture(),
        datetime.now(timezone.utc)
    )
    assert any("SQL" in m.name for m in report.competencies.missing_critical)


# 30. Brak P&L w wariancie bez dowodu
def test_30_brak_p_and_l_bez_dowodu() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = ["zarządzanie sprzedażą", "strategia rozwoju", "raportowanie do zarządu"]

    report = build_career_positioning_report(
        {"content": "Dyrektor"},
        lib,
        datetime.now(timezone.utc)
    )
    director_var = next(v for v in report.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var.availability == "AVAILABLE"

    # Assert absence of P&L/budget keywords across the entire variant
    text_to_check = (
        (director_var.title_positioning or "") + " " +
        (director_var.summary_positioning or "") + " " +
        " ".join(director_var.elements_to_emphasize) + " " +
        " ".join(director_var.elements_to_flatten) + " " +
        " ".join(director_var.elements_to_remove_or_deprioritize)
    )
    for kw in ["P&L", "budget", "budżet"]:
        assert kw not in text_to_check


# 31. P&L w wariancie z dowodem
def test_31_p_and_l_z_dowodem() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append("odpowiedzialność za P&L")
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append("raportowanie do zarządu")
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append("strategia rozwoju")

    report = build_career_positioning_report(
        {"content": "Dyrektor"},
        lib,
        datetime.now(timezone.utc)
    )
    director_var = next(v for v in report.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var.availability == "AVAILABLE"
    assert any("P&L" in item for item in director_var.elements_to_emphasize)


# 32. Strict schema_version
def test_32_strict_schema_version() -> None:
    report = build_career_positioning_report(
        {"content": "Manager"},
        get_valid_payload_fixture(),
        datetime.now(timezone.utc)
    )
    assert report.schema_version == "1.0"


# 33. Nieznany enum odrzucony
def test_33_nieznany_enum_odrzucony() -> None:
    from app.schemas.career_positioning import PositioningDetailsSchema

    with pytest.raises(ValidationError):
        PositioningDetailsSchema(
            detected_job_level="SUPER_JUNIOR",  # invalid enum
            candidate_profile_level="SPECIALIST",
            level_gap=0,
            positioning_strategy="BALANCED",
            title_treatment="KEEP",
            fit_level="GOOD",
            fit_score=85.0,
            confidence=0.9,
            requires_human_review=False
        )


# 34. Extra field odrzucone
def test_34_extra_field_odrzucone() -> None:
    from app.schemas.career_positioning import PositioningDetailsSchema

    with pytest.raises(ValidationError):
        PositioningDetailsSchema(
            detected_job_level="JUNIOR",
            candidate_profile_level="SPECIALIST",
            level_gap=0,
            positioning_strategy="BALANCED",
            title_treatment="KEEP",
            fit_level="GOOD",
            fit_score=85.0,
            confidence=0.9,
            requires_human_review=False,
            extra_unwanted_field="some-value"  # extra field
        )


# 35. Read-only na rzeczywistej bazie testowej (isolated_db)
async def test_35_real_db_readonly(isolated_db: Any, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    job_row = await isolated_db.create_job(content=sample_job["content"])
    job_id = job_row["job_id"]

    async with isolated_db._session() as session:
        jobs_before = len((await session.execute(select(Job))).scalars().all())
        resumes_before = len((await session.execute(select(Resume))).scalars().all())
        apps_before = len((await session.execute(select(Application))).scalars().all())
        events_before = len((await session.execute(select(MetricEvent))).scalars().all())

    with patch("app.routers.career_positioning.get_truth_library", return_value=get_valid_payload_fixture()):
        async with client:
            resp = await client.get(f"/api/v1/jobs/{job_id}/career-positioning")
        assert resp.status_code == 200

    async with isolated_db._session() as session:
        jobs_after = len((await session.execute(select(Job))).scalars().all())
        resumes_after = len((await session.execute(select(Resume))).scalars().all())
        apps_after = len((await session.execute(select(Application))).scalars().all())
        events_after = len((await session.execute(select(MetricEvent))).scalars().all())

    assert jobs_after == jobs_before
    assert resumes_after == resumes_before
    assert apps_after == apps_before
    assert events_after == events_before


# 36. Staż menedżerski: Project Manager bez zespołu nie zwiększa management_years
def test_36_manager_without_team_not_management_years() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["stanowisko"] = "Project Manager"
    # remove activities that indicate budget or team management
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = ["współpraca", "statusy"]
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["skalaOdpowiedzialnosci"] = ""
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["wynikLiczbowy"] = ""

    report = build_career_positioning_report(
        {"content": "Requirements"},
        lib,
        datetime.now(timezone.utc)
    )
    # The entry did not qualify as management role because it lacks team proof
    # dz-2 (Menedżer ds. Projektów) also lacks team proof, so mgmt_years must be 0!
    assert report.strategy.recommended_narrative == "EXPERT"


# 37. Staż menedżerski: Project Manager, Product Manager, Account Manager i Lead Developer testy oddzielne
def test_37_project_manager_unconfirmed_has_no_mgmt_years() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Project Manager",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["zarządzanie harmonogramem"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    assert report.strategy.recommended_narrative == "EXPERT"


def test_38_product_manager_unconfirmed_has_no_mgmt_years() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Product Manager",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["tworzenie roadmapy"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    assert report.strategy.recommended_narrative == "EXPERT"


def test_39_account_manager_unconfirmed_has_no_mgmt_years() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Account Manager",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["obsługa klientów kluczowych"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    assert report.strategy.recommended_narrative == "EXPERT"


def test_40_lead_developer_unconfirmed_has_no_mgmt_years() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Lead Developer",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["pisanie kodu", "code review"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    assert report.strategy.recommended_narrative == "EXPERT"


# 41. Ujednolicone filtrowanie: SQL występuje wyłącznie w polu technicznym/statusie
def test_41_sql_in_technical_field_only() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-sql-test",
            "firma": "Test",
            "stanowisko": "Developer",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["programowanie"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True
        }
    ]
    # "SQL" is not in any of the allowed text fields, only in ID. So it must go to missing_critical!
    report = build_career_positioning_report(
        {"content": "Requirements: SQL knowledge"},
        lib,
        datetime.now(timezone.utc)
    )
    assert any("SQL" in m.name for m in report.competencies.missing_critical)


# 42. Mechanizm wymagań kompetencyjnych: wykrywanie kompetencji spoza obecnej mapy
def test_42_competencies_outside_base_map() -> None:
    lib = get_valid_payload_fixture()
    # Add pricing and bancassurance to candidate facts
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append("odpowiedzialność za pricing")
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"].append("znajomość bancassurance")

    report = build_career_positioning_report(
        {"content": "Wymagania: znajomość bancassurance oraz pricing i regulatory compliance."},
        lib,
        datetime.now(timezone.utc)
    )

    # Pricing and Bancassurance should be direct matches
    assert any("pricing" in m.name.lower() for m in report.competencies.direct_matches)
    assert any("bancassurance" in m.name.lower() for m in report.competencies.direct_matches)
    # Regulatory compliance should be missing critical
    assert any("regulatory compliance" in m.name.lower() for m in report.competencies.missing_critical)


# 43. Pydantic walidatory: timezone-aware generated_at
def test_43_pydantic_timezone_aware_generated_at() -> None:
    from app.schemas.career_positioning import CareerPositioningResponse

    report = build_career_positioning_report(
        {"content": "Manager"},
        get_valid_payload_fixture(),
        datetime.now(timezone.utc) # defaults to timezone-aware via utc
    )

    # Manually serialize and deserialize with naive datetime
    data = report.model_dump()
    data["generated_at"] = datetime.now() # naive
    with pytest.raises(ValidationError):
        CareerPositioningResponse(**data)


# 44. Pydantic walidatory: AVAILABLE requires level not null, UNSUPPORTED requires level null
def test_44_pydantic_risk_item_levels() -> None:
    # AVAILABLE with null level is invalid
    with pytest.raises(ValidationError):
        RiskItemSchema(availability="AVAILABLE", level=None, rationale=["test"])

    # UNSUPPORTED with non-null level is invalid
    with pytest.raises(ValidationError):
        RiskItemSchema(availability="UNSUPPORTED", level="MEDIUM", rationale=[])


# 45. Pydantic walidatory: UNSUPPORTED narrative cannot be recommended
def test_45_pydantic_unsupported_narrative_not_recommended() -> None:
    with pytest.raises(ValidationError):
        NarrativeVariantSchema(
            type="MANAGER",
            availability="UNSUPPORTED",
            recommended=True, # cannot be recommended
            title_positioning=None,
            summary_positioning=None,
            risk_level="HIGH",
            limitations=["insufficient evidence"]
        )


# 46. Pydantic walidatory: strategy warianty i whitespace-only checks
def test_46_pydantic_strategy_whitespace_only() -> None:
    with pytest.raises(ValidationError):
        StrategySchema(
            recommendation="   ", # whitespace-only
            recommended_narrative="EXPERT",
            elements_to_emphasize=["OK"],
            title_positioning="OK",
            summary_positioning="OK",
            narrative_variants=[]
        )


# 47. Poziom kandydata: UNKNOWN zamiast fallbacka do SPECIALIST
def test_47_unknown_candidate_level_no_fallback() -> None:
    lib = get_valid_payload_fixture()
    # Replace titles with completely unclassifiable ones
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["stanowisko"] = "Random Staff Member"
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = []
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["wynikLiczbowy"] = ""
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["skalaOdpowiedzialnosci"] = ""
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1]["stanowisko"] = "Employee 123"
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][1]["aktywnosci"] = []

    report = build_career_positioning_report(
        {"content": "Manager requirements"},
        lib,
        datetime.now(timezone.utc)
    )
    assert report.positioning.candidate_profile_level == "UNKNOWN"
    assert report.positioning.requires_human_review is True
    assert report.limitations.insufficient_data is True


# 48. Funkcjonalna luka: zwraca UNSUPPORTED gdy brak wykrytych wymagań w ogłoszeniu
def test_48_functional_gap_unsupported_when_no_requirements() -> None:
    lib = get_valid_payload_fixture()

    report = build_career_positioning_report(
        {"content": "Praca w biurze na pełen etat. Miła atmosfera."},
        lib,
        datetime.now(timezone.utc)
    )
    assert report.risks.functional_gap.availability == "UNSUPPORTED"


# 49. Obsługa wyjątku: TruthLibraryNotFoundError
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_49_truth_library_not_found(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    from app.services.truth_library_loader import TruthLibraryNotFoundError
    mock_db.get_job.return_value = sample_job
    mock_truth.side_effect = TruthLibraryNotFoundError("File not found")

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "NO_TRUTH_LIBRARY"
    assert detail["message"] == "Truth Library file not found"


# 50. Obsługa wyjątku: TruthLibraryInvalidJsonError
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_50_truth_library_invalid_json(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    from app.services.truth_library_loader import TruthLibraryInvalidJsonError
    mock_db.get_job.return_value = sample_job
    mock_truth.side_effect = TruthLibraryInvalidJsonError("Invalid JSON")

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "INVALID_TRUTH_LIBRARY"
    assert detail["message"] == "Truth Library JSON is invalid"


# 51. Obsługa wyjątku: TruthLibraryStructureError
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_51_truth_library_structure_error(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    from app.services.truth_library_loader import TruthLibraryStructureError
    mock_db.get_job.return_value = sample_job
    mock_truth.side_effect = TruthLibraryStructureError("Invalid structure")

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "INVALID_TRUTH_LIBRARY"
    assert detail["message"] == "Invalid Truth Library structure"


# 52. Obsługa wyjątku: Inne niesklasyfikowane wyjątki
@patch("app.routers.career_positioning.db", new_callable=AsyncMock)
@patch("app.routers.career_positioning.get_truth_library")
async def test_52_unexpected_exception(mock_truth: AsyncMock, mock_db: AsyncMock, client: AsyncClient, sample_job: Dict[str, Any]) -> None:
    mock_db.get_job.return_value = sample_job
    mock_truth.side_effect = RuntimeError("Something unexpected")

    async with client:
        resp = await client.get("/api/v1/jobs/job-123/career-positioning")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "TRUTH_LIBRARY_READ_ERROR"
    assert detail["message"] == "Truth Library read error"


# 53. Pierwszy wpis ma team proof, ostatni nie
def test_53_team_proof_ordering_first_has_last_not() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Kierownik Sprzedaży",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["zarządzanie zespołem 5 osób"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        },
        {
            "id": "dz-2",
            "firma": "Firma Beta",
            "stanowisko": "Dyrektor",
            "okresOd": "2023-08",
            "okresDo": "2024-07",
            "aktywnosci": ["zadania administracyjne, bez zespołu"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    manager_variant = next(v for v in report.strategy.narrative_variants if v.type == "MANAGER")
    assert manager_variant.availability == "AVAILABLE"


# 54. Ostatni wpis ma team proof, pierwszy nie
def test_54_team_proof_ordering_last_has_first_not() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Dyrektor",
            "okresOd": "2023-08",
            "okresDo": "2024-07",
            "aktywnosci": ["zadania administracyjne, bez zespołu"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        },
        {
            "id": "dz-2",
            "firma": "Firma Beta",
            "stanowisko": "Kierownik Sprzedaży",
            "okresOd": "2024-08",
            "okresDo": "2025-07",
            "aktywnosci": ["zarządzanie zespołem 5 osób"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    manager_variant = next(v for v in report.strategy.narrative_variants if v.type == "MANAGER")
    assert manager_variant.availability == "AVAILABLE"


# 55. Wpis bez team proof nie zwiększa management_years
def test_55_entry_without_team_proof_does_not_increase_management_years() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"] = [
        {
            "id": "dz-1",
            "firma": "Firma Alpha",
            "stanowisko": "Menedżer",
            "okresOd": "2024-01",
            "okresDo": "2024-12",
            "aktywnosci": ["praca indywidualna bez zarządzania ludźmi"],
            "status": "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA",
            "uzywacWCV": True,
        }
    ]
    report = build_career_positioning_report({"content": "Manager"}, lib, datetime.now(timezone.utc))
    manager_variant = next(v for v in report.strategy.narrative_variants if v.type == "MANAGER")
    assert manager_variant.availability == "UNSUPPORTED"


# 56. Testy dowodów Board: pozytywne i negatywne
def test_56_board_evidence_positive_and_negative() -> None:
    # 56a. Board Positive: raportowanie do zarządu (strategy + budget + board = 3 points -> Director variant AVAILABLE)
    lib_pos = get_valid_payload_fixture()
    lib_pos["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = [
        "strategia rozwoju", "budżet marketingowy", "raportowanie do zarządu"
    ]
    report_pos = build_career_positioning_report({"content": "Dyrektor"}, lib_pos, datetime.now(timezone.utc))
    director_var_pos = next(v for v in report_pos.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var_pos.availability == "AVAILABLE"
    assert any("relacji z zarządem" in limit or "Board" in limit for limit in director_var_pos.limitations) is False

    # 56b. Board Negative: "zarządzanie zespołem" lub tylko "zarządzanie" (strategy + budget = 2 points -> Director variant AVAILABLE)
    lib_neg = get_valid_payload_fixture()
    lib_neg["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = [
        "strategia rozwoju", "budżet marketingowy", "zarządzanie zespołem", "zarządzanie projektem"
    ]
    report_neg = build_career_positioning_report({"content": "Dyrektor"}, lib_neg, datetime.now(timezone.utc))
    director_var_neg = next(v for v in report_neg.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var_neg.availability == "AVAILABLE"
    assert any("relacji z zarządem" in limit or "Board" in limit for limit in director_var_neg.limitations) is True


# 57. Testy dowodów P&L: pozytywne i negatywne
def test_57_p_and_l_evidence_positive_and_negative() -> None:
    # 57a. P&L Positive: odpowiedzialność za wynik finansowy (strategy + board + P&L = 3 points -> Director variant AVAILABLE)
    lib_pos = get_valid_payload_fixture()
    lib_pos["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = [
        "strategia rozwoju", "współpraca z zarządem", "odpowiedzialność za wynik finansowy"
    ]
    report_pos = build_career_positioning_report({"content": "Dyrektor"}, lib_pos, datetime.now(timezone.utc))
    director_var_pos = next(v for v in report_pos.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var_pos.availability == "AVAILABLE"
    assert any("budżet" in limit.lower() or "p&l" in limit.lower() for limit in director_var_pos.limitations) is False

    # 57b. P&L Negative: zwykłe "wynik finansowy" bez odpowiedzialności (strategy + board = 2 points -> Director variant AVAILABLE)
    lib_neg = get_valid_payload_fixture()
    lib_neg["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = [
        "strategia rozwoju", "współpraca z zarządem", "analizowanie i sprawdzanie: wynik finansowy"
    ]
    report_neg = build_career_positioning_report({"content": "Dyrektor"}, lib_neg, datetime.now(timezone.utc))
    director_var_neg = next(v for v in report_neg.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var_neg.availability == "AVAILABLE"
    assert any("budżet" in limit.lower() or "p&l" in limit.lower() for limit in director_var_neg.limitations) is True


# 58. Testy dowodów zarządzania menedżerami: pozytywne i negatywne
def test_58_managing_managers_evidence_positive_and_negative() -> None:
    # 58a. Managing Managers Positive: zarządzanie kierownikami (strategy + board + managing managers = 3 points -> Director variant AVAILABLE)
    lib_pos = get_valid_payload_fixture()
    lib_pos["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = [
        "strategia rozwoju", "współpraca z zarządem", "zarządzanie kierownikami regionalnymi"
    ]
    report_pos = build_career_positioning_report({"content": "Dyrektor"}, lib_pos, datetime.now(timezone.utc))
    director_var_pos = next(v for v in report_pos.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var_pos.availability == "AVAILABLE"
    assert any("menedżer" in limit.lower() for limit in director_var_pos.limitations) is False

    # 58b. Managing Managers Negative: zarządzanie oddziałami (managed units) nie oznacza managing managers (strategy + board = 2 points -> Director variant AVAILABLE)
    lib_neg = get_valid_payload_fixture()
    lib_neg["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = [
        "strategia rozwoju", "współpraca z zarządem", "zarządzanie 5 oddziałami"
    ]
    report_neg = build_career_positioning_report({"content": "Dyrektor"}, lib_neg, datetime.now(timezone.utc))
    director_var_neg = next(v for v in report_neg.strategy.narrative_variants if v.type == "DIRECTOR")
    assert director_var_neg.availability == "AVAILABLE"
    assert any("menedżer" in limit.lower() for limit in director_var_neg.limitations) is True


# 59. Testy fałszywych dowodów eksperckich (architektura, techniczny, wynik, wdrożen)
def test_59_expert_false_proofs_and_contexts() -> None:
    # 59a. "wdrożen" bez problem/optymalizac nie daje problem_solving_proof
    lib = get_valid_payload_fixture()
    lib["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = ["wdrożenie nowego modułu"]
    report = build_career_positioning_report({"content": "Senior Architect"}, lib, datetime.now(timezone.utc))
    expert_var = next(v for v in report.strategy.narrative_variants if v.type == "EXPERT")
    assert "Samodzielne rozwiązywanie skomplikowanych problemów" not in expert_var.elements_to_emphasize

    # 59b. "architektura" i "techniczny" bez kontekstu nie dają technical_proof
    lib_tech = get_valid_payload_fixture()
    lib_tech["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = ["stan techniczny maszyn", "architektura wnętrz"]
    report_tech = build_career_positioning_report({"content": "Senior Architect"}, lib_tech, datetime.now(timezone.utc))
    expert_var_tech = next(v for v in report_tech.strategy.narrative_variants if v.type == "EXPERT")
    assert "Zaawansowane kompetencje merytoryczne i techniczne" not in expert_var_tech.elements_to_emphasize

    # 59c. "wynik" bez kontekstu nie daje sales_result_responsibility
    lib_wynik = get_valid_payload_fixture()
    lib_wynik["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["aktywnosci"] = ["wynik spotkania z klientem", "sprawdzenie wyników testu"]
    lib_wynik["kategorie"]["doswiadczenieZawodowe"]["wpisy"][0]["wynikLiczbowy"] = ""
    report_wynik = build_career_positioning_report({"content": "Senior Architect"}, lib_wynik, datetime.now(timezone.utc))
    expert_var_wynik = next(v for v in report_wynik.strategy.narrative_variants if v.type == "EXPERT")
    assert "Konkretne rezultaty ilościowe i wolumeny (bez skali zespołu)" not in expert_var_wynik.elements_to_emphasize


# 60. Wpis bez statusu (np. SQL) jest ignorowany i trafia do missing_critical
def test_60_sql_without_status_rejected() -> None:
    lib = get_valid_payload_fixture()
    lib["kategorie"]["technologie"] = {
        "wpisy": [
            {
                "id": "tech-sql",
                "nazwa": "SQL",
                "uzywacWCV": True
            }
        ]
    }
    report = build_career_positioning_report(
        {"content": "Requirements: SQL"},
        lib,
        datetime.now(timezone.utc)
    )
    assert any("SQL" in item.name for item in report.competencies.missing_critical)
