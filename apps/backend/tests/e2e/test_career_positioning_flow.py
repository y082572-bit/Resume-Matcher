"""Public-API E2E coverage for the Career Positioning flow.

Every report in this module is obtained through the FastAPI endpoint while the
application uses the disposable ``isolated_db`` fixture and an anonymized
Truth Library file.
"""

import json
from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models import Application, Job, MetricEvent, Resume


APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
ENDPOINT = "/api/v1/jobs/{job_id}/career-positioning"


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def write_truth_library(tmp_path, monkeypatch):
    import app.services.truth_library_loader as loader
    from app.config import settings

    library_path = tmp_path / "truth-library.json"
    monkeypatch.setattr(settings, "truth_library_path", library_path)

    def write(payload: dict) -> None:
        library_path.write_text(json.dumps(payload), encoding="utf-8")
        loader.get_truth_library(force_reload=True)

    return write


def experience(
    *,
    entry_id: str,
    title: str,
    activities: list[str] | None = None,
    start: str = "2020-01",
    end: str = "2023-12",
    status: str | None = APPROVED,
    use_in_cv: bool = True,
    requires_approval: bool = False,
    scale: str | None = None,
) -> dict:
    item = {
        "id": entry_id,
        "firma": f"Firma testowa {entry_id}",
        "stanowisko": title,
        "okresOd": start,
        "okresDo": end,
        "aktywnosci": activities or [],
        "uzywacWCV": use_in_cv,
    }
    if status is not None:
        item["status"] = status
    if requires_approval:
        item["wymagaAkceptacji"] = True
    if scale is not None:
        item["skalaOdpowiedzialnosci"] = scale
    return item


def truth_library(
    entries: list[dict] | None = None,
    *,
    competencies: list[dict] | None = None,
    tools: list[dict] | None = None,
) -> dict:
    categories = {
        "doswiadczenieZawodowe": {"wpisy": entries or []},
        "tranferowalneKompetencje": {
            "mapowania": {"współpraca z partnerami": "partner management"}
        },
        "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
        "reguly": {
            "statusyDoAutomatycznegoCV": [APPROVED],
            "statusyWymagajaceAkceptacji": [
                "PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA"
            ],
            "statusyZablokowane": ["USUNIĘTE"],
        },
        "branze": {"zatwierdzoneBranze": ["Branża testowa"]},
    }
    if competencies is not None:
        categories["kompetencje"] = {"wpisy": competencies}
    if tools is not None:
        categories["narzedzia"] = {"wpisy": tools}
    return {
        "meta": {
            "kandidat": "Kandydat testowy",
            "wersja": "1.1",
            "dataAktualizacji": "2026-07-18",
            "status": "AKTYWNA",
        },
        "kategorie": categories,
    }


async def report_for(isolated_db, client, write_truth_library, *, job_text: str, library: dict):
    write_truth_library(library)
    job = await isolated_db.create_job(content=job_text)
    response = await client.get(ENDPOINT.format(job_id=job["job_id"]))
    assert response.status_code == 200, response.text
    return response.json()


def without_generated_at(report: dict) -> dict:
    comparable = deepcopy(report)
    comparable.pop("generated_at")
    comparable.pop("job_id")
    return comparable


def narrative(report: dict, variant_type: str) -> dict:
    return next(
        item for item in report["strategy"]["narrative_variants"] if item["type"] == variant_type
    )


def all_emphasis(report: dict) -> str:
    return " ".join(
        item
        for variant in report["strategy"]["narrative_variants"]
        for item in variant["elements_to_emphasize"]
    ).lower()


async def database_snapshot(isolated_db) -> dict:
    models = (Job, Resume, Application, MetricEvent)
    snapshot = {}
    async with isolated_db._session() as session:
        for model in models:
            result = await session.execute(select(model))
            rows = result.scalars().all()
            snapshot[model.__name__] = [
                tuple(getattr(row, column.name) for column in model.__table__.columns)
                for row in rows
            ]
    return snapshot


@pytest.mark.asyncio
async def test_expert_controlled_flattening_has_correct_levels_gap_strategy_and_risk(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Specjalista ds. analiz\nSamodzielna analiza procesów.",
        library=truth_library(
            [experience(entry_id="exp-director", title="Dyrektor obszaru")]
        ),
    )

    positioning = report["positioning"]
    assert positioning["detected_job_level"] == "SPECIALIST"
    assert positioning["candidate_profile_level"] == "DIRECTOR"
    assert positioning["level_gap"] == 3
    assert positioning["positioning_strategy"] == "CONTROLLED_FLATTENING"
    assert report["risks"]["overqualification"]["level"] == "HIGH"


@pytest.mark.asyncio
async def test_expert_does_not_invent_leadership_finance_board_or_technical_claims(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert procesowy\nSamodzielne usprawnianie procesów.",
        library=truth_library(
            [
                experience(
                    entry_id="exp-expert",
                    title="Starszy ekspert procesowy",
                    activities=["samodzielne usprawnianie procesów"],
                )
            ]
        ),
    )

    emphasis = all_emphasis(report)
    for unsupported_claim in (
        "p&l",
        "board",
        "budżet",
        "zarządzanie zespołem",
        "techniczne",
    ):
        assert unsupported_claim not in emphasis


@pytest.mark.asyncio
async def test_expert_keeps_unproven_sql_and_python_in_missing_critical(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert danych\nWymagane SQL oraz Python.",
        library=truth_library(
            [experience(entry_id="exp-analysis", title="Starszy analityk")]
        ),
    )

    direct = {item["name"] for item in report["competencies"]["direct_matches"]}
    missing = {item["name"] for item in report["competencies"]["missing_critical"]}
    assert {"SQL", "Python"}.isdisjoint(direct)
    assert {"SQL", "Python"}.issubset(missing)


@pytest.mark.asyncio
async def test_manager_variant_requires_approved_team_proof(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Kierownik zespołu\nZarządzanie zespołem operacyjnym.",
        library=truth_library(
            [
                experience(
                    entry_id="exp-manager",
                    title="Manager operacyjny",
                    activities=["zarządzanie zespołem 7 osób"],
                )
            ]
        ),
    )

    manager = narrative(report, "MANAGER")
    assert report["positioning"]["candidate_profile_level"] == "MANAGER"
    assert manager["availability"] == "AVAILABLE"
    assert "Zarządzanie zespołem (wielkość i struktura zespołu)" in manager[
        "elements_to_emphasize"
    ]


@pytest.mark.asyncio
async def test_manager_word_alone_is_not_people_management_proof(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Manager produktu\nOdpowiedzialność za rozwój produktu.",
        library=truth_library(
            [
                experience(
                    entry_id="exp-product",
                    title="Manager produktu",
                    activities=["koordynacja backlogu produktu"],
                )
            ]
        ),
    )

    assert narrative(report, "MANAGER")["availability"] == "UNSUPPORTED"


@pytest.mark.asyncio
async def test_manager_duration_contributes_to_overqualification_threshold(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Starszy ekspert\nSamodzielne doradztwo.",
        library=truth_library(
            [
                experience(
                    entry_id="exp-eight-years",
                    title="Manager operacyjny",
                    activities=["zarządzanie zespołem 6 osób"],
                    start="2016-01",
                    end="2023-12",
                )
            ]
        ),
    )

    assert report["positioning"]["level_gap"] == 1
    assert report["risks"]["overqualification"]["level"] == "MEDIUM"


@pytest.mark.asyncio
async def test_manager_uses_maximum_team_size_and_is_order_independent(
    isolated_db, client, write_truth_library
):
    entries = [
        experience(
            entry_id="exp-small-team",
            title="Manager operacyjny",
            activities=["zarządzanie zespołem 4 osób"],
            start="2020-01",
            end="2021-12",
        ),
        experience(
            entry_id="exp-large-team",
            title="Manager regionalny",
            activities=["zarządzanie zespołem 12 osób"],
            start="2022-01",
            end="2023-12",
        ),
    ]
    job_text = "Specjalista operacyjny\nSamodzielna realizacja zadań."
    first = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text=job_text,
        library=truth_library(entries),
    )
    second = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text=job_text,
        library=truth_library(list(reversed(entries))),
    )

    assert without_generated_at(first)["positioning"] == without_generated_at(second)["positioning"]
    excessive = {item["name"] for item in first["competencies"]["excessive_for_role"]}
    assert "Skala kierowania zespołem (12 osób)" in excessive


@pytest.mark.asyncio
async def test_manager_does_not_add_budget_coaching_or_mentoring_without_proof(
    isolated_db, client, write_truth_library
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Kierownik zespołu\nZarządzanie zespołem.",
        library=truth_library(
            [
                experience(
                    entry_id="exp-team-only",
                    title="Manager operacyjny",
                    activities=["zarządzanie zespołem 5 osób"],
                )
            ]
        ),
    )

    manager_text = " ".join(narrative(report, "MANAGER")["elements_to_emphasize"]).lower()
    assert "budżet" not in manager_text
    assert "coach" not in manager_text
    assert "mentor" not in manager_text


DIRECTOR_CASES = (
    pytest.param(
        ["tworzenie strategii biznesowej"],
        None,
        "Długofalowa strategia biznesowa i wizja",
        ("P&L", "wielopoziomowymi", "Board", "Skala organizacyjna"),
        id="strategy-proof",
    ),
    pytest.param(
        ["zarządzanie budżetem operacyjnym"],
        None,
        "Odpowiedzialność budżetowa",
        ("P&L", "wielopoziomowymi", "Board", "Skala organizacyjna"),
        id="budget-proof",
    ),
    pytest.param(
        ["odpowiedzialność za P&L"],
        None,
        "Pełna odpowiedzialność budżetowa P&L",
        ("strategia biznesowa", "wielopoziomowymi", "Board", "Skala organizacyjna"),
        id="profit-and-loss-proof",
    ),
    pytest.param(
        ["tworzenie strategii biznesowej"],
        "zarządzanie 12 oddziałów",
        "Skala organizacyjna i zarządzanie jednostkami",
        ("P&L", "wielopoziomowymi", "Board"),
        id="organizational-scale-proof",
    ),
    pytest.param(
        ["zarządzanie menedżerami działów"],
        None,
        "Zarządzanie strukturami wielopoziomowymi (zarządzanie menedżerami)",
        ("strategia biznesowa", "P&L", "Board", "Skala organizacyjna"),
        id="managing-managers-proof",
    ),
    pytest.param(
        ["raportowanie do zarządu"],
        None,
        "Kluczowe relacje na poziomie Board / Enterprise partners",
        ("strategia biznesowa", "P&L", "wielopoziomowymi", "Skala organizacyjna"),
        id="board-proof",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("activities,scale,expected_claim,absent_claims", DIRECTOR_CASES)
async def test_director_claims_require_their_corresponding_evidence(
    isolated_db,
    client,
    write_truth_library,
    activities,
    scale,
    expected_claim,
    absent_claims,
):
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Dyrektor operacyjny\nOdpowiedzialność za obszar operacyjny.",
        library=truth_library(
            [
                experience(
                    entry_id="exp-director-proof",
                    title="Dyrektor operacyjny",
                    activities=activities,
                    scale=scale,
                )
            ]
        ),
    )

    director = narrative(report, "DIRECTOR")
    assert director["availability"] == "AVAILABLE"
    assert expected_claim in director["elements_to_emphasize"]
    director_text = " ".join(director["elements_to_emphasize"])
    for absent_claim in absent_claims:
        assert absent_claim not in director_text


UNAPPROVED_CASES = (
    pytest.param({"status": "NIEJASNE"}, id="unclear-status"),
    pytest.param({"status": "USUNIĘTE"}, id="deleted-status"),
    pytest.param(
        {"status": "PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA"},
        id="document-awaiting-approval",
    ),
    pytest.param({"status": APPROVED, "use_in_cv": False}, id="cv-disabled"),
    pytest.param(
        {"status": APPROVED, "requires_approval": True},
        id="explicit-approval-required",
    ),
    pytest.param({"status": None}, id="missing-status"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_options", UNAPPROVED_CASES)
async def test_unapproved_truth_entries_have_no_effect_on_the_public_report(
    isolated_db, client, write_truth_library, entry_options
):
    job_text = "Ekspert danych\nWymagane SQL oraz partner management."
    baseline = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text=job_text,
        library=truth_library(),
    )
    signal_entry = experience(
        entry_id="unapproved-signals",
        title="Dyrektor",
        activities=[
            "UNAPPROVED_BOARD_SIGNAL raportowanie do zarządu",
            "UNAPPROVED_PNL_SIGNAL odpowiedzialność za P&L",
            "UNAPPROVED_SQL_SIGNAL SQL",
            "UNAPPROVED_TEAM_SIGNAL zarządzanie zespołem 99 osób",
        ],
        **entry_options,
    )
    with_unapproved = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text=job_text,
        library=truth_library([signal_entry]),
    )

    assert without_generated_at(with_unapproved) == without_generated_at(baseline)


@pytest.mark.asyncio
async def test_api_response_excludes_private_values_internal_ids_metadata_and_full_library(
    isolated_db, client, write_truth_library
):
    private_values = (
        "Kandydat poufny",
        "Ulica Testowa 17",
        "+48 000 000 000",
        "NATIONAL-ID-PRIVATE-0001",
        "candidate" + chr(64) + "example.invalid",
        "private-trace-value",
        "/" + "Users/private/profile.json",
        "FULL_PRIVATE_LIBRARY_MARKER",
    )
    item = experience(entry_id="private-entry", title="Starszy ekspert")
    item.update(
        {
            "name": private_values[0],
            "address": private_values[1],
            "phone": private_values[2],
            "national_id": private_values[3],
            "e" + "mail": private_values[4],
            "life" + "cycle_token": private_values[5],
            "operation" + "_key": private_values[5],
            "correlation" + "_id": private_values[5],
            "truth" + "_item_id": private_values[5],
            "raw_" + "metadata_json": private_values[6],
            "private_blob": private_values[7],
        }
    )
    report = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert procesowy\nSamodzielna analiza.",
        library=truth_library([item]),
    )
    serialized = json.dumps(report, ensure_ascii=False)

    for private_value in private_values:
        assert private_value not in serialized
    for internal_key in (
        "life" + "cycle_token",
        "operation" + "_key",
        "correlation" + "_id",
        "truth" + "_item_id",
        "metadata" + "_json",
    ):
        assert internal_key not in serialized


@pytest.mark.asyncio
async def test_two_requests_are_identical_except_for_generation_timestamp(
    isolated_db, client, write_truth_library
):
    write_truth_library(
        truth_library([experience(entry_id="exp-repeat", title="Starszy ekspert")])
    )
    job = await isolated_db.create_job(content="Ekspert procesowy\nSamodzielna analiza.")
    first = await client.get(ENDPOINT.format(job_id=job["job_id"]))
    second = await client.get(ENDPOINT.format(job_id=job["job_id"]))

    assert first.status_code == second.status_code == 200
    assert without_generated_at(first.json()) == without_generated_at(second.json())


@pytest.mark.asyncio
async def test_experience_order_does_not_change_report(
    isolated_db, client, write_truth_library
):
    entries = [
        experience(entry_id="exp-a", title="Starszy ekspert", start="2018-01", end="2019-12"),
        experience(entry_id="exp-b", title="Ekspert procesowy", start="2020-01", end="2021-12"),
    ]
    first = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert procesowy\nSamodzielna analiza.",
        library=truth_library(entries),
    )
    second = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert procesowy\nSamodzielna analiza.",
        library=truth_library(list(reversed(entries))),
    )

    assert without_generated_at(first) == without_generated_at(second)


@pytest.mark.asyncio
async def test_competency_order_does_not_change_report(
    isolated_db, client, write_truth_library
):
    competencies = [
        {"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True},
        {"nazwa": "Python", "status": APPROVED, "uzywacWCV": True},
    ]
    entry = experience(entry_id="exp-skills", title="Starszy ekspert")
    first = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert danych\nWymagane SQL oraz Python.",
        library=truth_library([entry], competencies=competencies),
    )
    second = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert danych\nWymagane SQL oraz Python.",
        library=truth_library([entry], competencies=list(reversed(competencies))),
    )

    assert without_generated_at(first) == without_generated_at(second)


@pytest.mark.asyncio
async def test_tool_order_does_not_change_report(
    isolated_db, client, write_truth_library
):
    tools = [
        {"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True},
        {"nazwa": "Python", "status": APPROVED, "uzywacWCV": True},
    ]
    entry = experience(entry_id="exp-tools", title="Starszy ekspert")
    first = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert danych\nWymagane SQL oraz Python.",
        library=truth_library([entry], tools=tools),
    )
    second = await report_for(
        isolated_db,
        client,
        write_truth_library,
        job_text="Ekspert danych\nWymagane SQL oraz Python.",
        library=truth_library([entry], tools=list(reversed(tools))),
    )

    assert without_generated_at(first) == without_generated_at(second)


@pytest.mark.asyncio
async def test_endpoint_is_read_only_for_jobs_resumes_applications_and_metrics(
    isolated_db, client, write_truth_library
):
    write_truth_library(
        truth_library([experience(entry_id="exp-read-only", title="Starszy ekspert")])
    )
    resume = await isolated_db.create_resume(
        content="Anonimowe CV testowe", title="CV testowe", processing_status="ready"
    )
    job = await isolated_db.create_job(
        content="Ekspert procesowy\nSamodzielna analiza.", resume_id=resume["resume_id"]
    )
    await isolated_db.create_application(
        job_id=job["job_id"], resume_id=resume["resume_id"], status="saved"
    )
    before = await database_snapshot(isolated_db)

    first = await client.get(ENDPOINT.format(job_id=job["job_id"]))
    middle = await database_snapshot(isolated_db)
    second = await client.get(ENDPOINT.format(job_id=job["job_id"]))
    after = await database_snapshot(isolated_db)

    assert first.status_code == second.status_code == 200
    assert before == middle == after


@pytest.mark.asyncio
async def test_unknown_job_is_reported_by_public_endpoint(isolated_db, client, write_truth_library):
    write_truth_library(truth_library())
    response = await client.get(ENDPOINT.format(job_id="missing-job"))

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "JOB_NOT_FOUND"
