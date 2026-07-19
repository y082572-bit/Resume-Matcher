"""Stage 10C-R3 public API coverage without bypassing planner or validators."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.llm import LLMConfig
from app.main import app
from app.models import Improvement, MetricEvent, Resume


APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def _truth() -> dict:
    return {
        "meta": {"wersja": "anonymous-stage10c-r3"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "private-r3-api-scope",
                        "firma": "Example Company",
                        "stanowisko": "Engineering Manager",
                        "okresOd": "2020-01",
                        "okresDo": "2025-12",
                        "aktywnosci": ["Python delivery", "people management"],
                        "wynikLiczbowy": "Improved result by 25%",
                        "status": APPROVED,
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
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": ["DO_AKCEPTACJI"],
                "statusyZablokowane": ["ZABLOKOWANE"],
            },
        },
    }


def _processed() -> dict:
    return {
        "personalInfo": {"name": "Example Candidate", "email": "candidate@example.test"},
        "summary": "Manager with 20 years of experience",
        "workExperience": [
            {
                "id": 1,
                "title": "Engineering Manager",
                "company": "Example Company",
                "years": "2020-2025",
                "description": ["Improved result by 25%", "Python delivery"],
            }
        ],
        "education": [],
        "personalProjects": [],
        "additional": {
            "technicalSkills": [],
            "languages": [],
            "certificationsTraining": [],
            "awards": [],
        },
        "sectionMeta": [],
        "customSections": {},
    }


class FakeRouter:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        content = await self.handler(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


async def _valid_content(kwargs) -> str:
    request = json.loads(kwargs["messages"][-1]["content"])
    payload = request["source_payload"]
    if isinstance(payload, list):
        content = [
            {
                "index": bullet["index"],
                "source_fingerprint": bullet["source_fingerprint"],
                "text": bullet["text"],
            }
            for bullet in payload
        ]
    else:
        content = payload
    return json.dumps(
        {
            "source_reference": request["source_reference"],
            "prompt_version": "1.3",
            "content": content,
        }
    )


@pytest.fixture
def api_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def records(isolated_db):
    resume = await isolated_db.create_resume(
        content="# Anonymous R3 resume",
        processed_data=_processed(),
        processing_status="ready",
    )
    job = await isolated_db.create_job(
        content="Engineering Manager people management direct reports Python delivery results",
        resume_id=resume["resume_id"],
    )
    return resume, job


@pytest.fixture
def config_state(monkeypatch):
    state = {
        "config": LLMConfig(
            provider="openai",
            model="example-r3-model",
            api_key="synthetic-test-key",
            api_base=None,
        )
    }
    monkeypatch.setattr(
        "app.routers.career_positioning.get_truth_library",
        lambda: deepcopy(_truth()),
    )
    monkeypatch.setattr(
        "app.routers.career_positioning.get_llm_config",
        lambda: state["config"],
    )
    return state


def _plan_path(records) -> str:
    resume, job = records
    return f"/api/v1/jobs/{job['job_id']}/resumes/{resume['resume_id']}/transformation-plan"


def _references(plan: dict) -> list[str]:
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


async def _approve(client, records):
    plan = (await client.get(_plan_path(records))).json()
    approval = (
        await client.post(
            f"{_plan_path(records)}/approval",
            json={
                "plan_fingerprint": plan["plan_fingerprint"],
                "decisions": [
                    {"source_reference": ref, "decision": "ACCEPT"}
                    for ref in _references(plan)
                ],
                "guardrails_acknowledged": True,
                "submit": True,
            },
        )
    ).json()
    return plan, approval


async def _post(client, records, plan, approval):
    return await client.post(
        f"{_plan_path(records)}/generation",
        json={
            "approval_id": approval["approval_id"],
            "plan_fingerprint": plan["plan_fingerprint"],
        },
    )


async def _count(database, model) -> int:
    async with database._session() as session:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)


@pytest.mark.asyncio
async def test_public_api_runs_real_planner_compiler_validator_and_assembly(
    api_client, isolated_db, records, config_state, monkeypatch
):
    router = FakeRouter(_valid_content)
    monkeypatch.setattr("app.llm.get_router", lambda config: (router, config))
    async with api_client as client:
        plan, approval = await _approve(client, records)
        before = {
            "resume": await _count(isolated_db, Resume),
            "improvement": await _count(isolated_db, Improvement),
            "metric": await _count(isolated_db, MetricEvent),
        }
        response = await _post(client, records, plan, approval)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generation_version"] == body["prompt_version"] == "1.3"
    summary = next(item for item in body["provenance"] if item["section"] == "SUMMARY")
    assert summary["mode"] == "COPIED_PROTECTED_CONTENT"
    assert summary["llm_used"] is False
    assert body["draft_resume"]["summary"] == "Manager with 20 years of experience"
    assert body["draft_resume"]["workExperience"][0]["description"] == [
        "Improved result by 25%",
        "Python delivery",
    ]
    assert router.calls == 2
    assert await _count(isolated_db, Resume) == before["resume"]
    assert await _count(isolated_db, Improvement) == before["improvement"]
    assert await _count(isolated_db, MetricEvent) == before["metric"]


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{}\n```",
        "leading prose {}",
        "{malformed",
    ],
    ids=["fenced-json", "leading-prose", "malformed-json"],
)
@pytest.mark.asyncio
async def test_strict_json_content_failures_are_invalid_llm_response(
    content, api_client, records, config_state, monkeypatch
):
    async def invalid(_kwargs):
        return content

    router = FakeRouter(invalid)
    monkeypatch.setattr("app.llm.get_router", lambda config: (router, config))
    async with api_client as client:
        plan, approval = await _approve(client, records)
        response = await _post(client, records, plan, approval)
        current = await client.get(f"{_plan_path(records)}/generation")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "INVALID_LLM_RESPONSE"
    assert content not in response.text
    assert current.status_code == 200
    assert current.json()["failure_code"] == "INVALID_LLM_RESPONSE"


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionError("offline")])
@pytest.mark.asyncio
async def test_transport_failures_remain_provider_errors(
    error, api_client, records, config_state, monkeypatch
):
    async def failed(_kwargs):
        raise error

    router = FakeRouter(failed)
    monkeypatch.setattr("app.llm.get_router", lambda config: (router, config))
    async with api_client as client:
        plan, approval = await _approve(client, records)
        response = await _post(client, records, plan, approval)
        current = await client.get(f"{_plan_path(records)}/generation")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "LLM_PROVIDER_ERROR"
    assert current.status_code == 200
    assert current.json()["failure_code"] == "LLM_PROVIDER_ERROR"


@pytest.mark.asyncio
async def test_read_only_generation_endpoints_do_not_require_credentials(
    api_client, records, config_state, monkeypatch
):
    router = FakeRouter(_valid_content)
    monkeypatch.setattr("app.llm.get_router", lambda config: (router, config))
    async with api_client as client:
        plan, approval = await _approve(client, records)
        generated = await _post(client, records, plan, approval)
        assert generated.status_code == 200, generated.text
        generation_id = generated.json()["generation_id"]
        calls_after_post = router.calls

        config_state["config"] = LLMConfig(
            provider="openai",
            model="example-r3-model",
            api_key="",
            api_base=None,
        )
        current = await client.get(f"{_plan_path(records)}/generation")
        by_id = await client.get(f"{_plan_path(records)}/generation/{generation_id}")
        assert current.status_code == 200
        assert current.json()["status"] == "GENERATED"
        assert by_id.status_code == 200
        assert by_id.json()["draft_resume"] is not None
        assert router.calls == calls_after_post

        config_state["config"] = LLMConfig(
            provider="openai",
            model="different-model",
            api_key="",
            api_base=None,
        )
        changed_model = await client.get(
            f"{_plan_path(records)}/generation/{generation_id}"
        )
        assert changed_model.status_code == 200
        assert changed_model.json()["status"] == "SUPERSEDED"

        config_state["config"] = LLMConfig(
            provider="anthropic",
            model="example-r3-model",
            api_key="",
            api_base=None,
        )
        changed_provider = await client.get(
            f"{_plan_path(records)}/generation/{generation_id}"
        )
        assert changed_provider.status_code == 200
        assert changed_provider.json()["status"] == "SUPERSEDED"
        assert router.calls == calls_after_post
