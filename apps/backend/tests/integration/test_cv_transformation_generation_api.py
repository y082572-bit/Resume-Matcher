"""Public API and persistence coverage for Stage 10C approved generation."""

import asyncio
from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.llm import LLMConfig
from app.main import app
from app.models import (
    Application,
    CVTransformationGeneration,
    Improvement,
    Job,
    MetricEvent,
    Resume,
)
from app.services.cv_transformation_generation import GenerationValidationError


APPROVED_TRUTH = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def truth_library():
    return {
        "meta": {"wersja": "anonymous-stage10c"},
        "kategorie": {
            "doswiadczenieZawodowe": {"wpisy": [{
                "id": "private-example-id", "firma": "Example Company",
                "stanowisko": "Engineering Manager", "okresOd": "2020-01",
                "okresDo": "2025-12", "aktywnosci": ["Python delivery"],
                "wynikLiczbowy": "Improved result by 25%", "status": APPROVED_TRUTH,
                "uzywacWCV": True,
            }]},
            "kompetencje": {"wpisy": []}, "narzedzia": {"wpisy": []},
            "technologie": {"wpisy": []}, "certyfikaty": {"wpisy": []},
            "kursy": {"wpisy": []}, "wyksztalcenie": {"wpisy": []},
            "jezyki": {"wpisy": []}, "osiagnieciaLiczbowe": {"wpisy": []},
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
        "personalInfo": {"name": "Example Candidate", "email": "candidate@example.test"},
        "summary": "Python engineering manager",
        "workExperience": [{
            "id": 1, "title": "Engineering Manager", "company": "Example Company",
            "years": "2020-2025", "description": ["Improved result by 25%", "Python delivery"],
        }],
        "education": [], "personalProjects": [],
        "additional": {"technicalSkills": ["Python"], "languages": ["English"], "certificationsTraining": [], "awards": []},
        "sectionMeta": [], "customSections": {},
    }


@pytest.fixture
def api_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def records(isolated_db):
    resume = await isolated_db.create_resume(
        content="# Anonymous resume", processed_data=processed_resume(), processing_status="ready"
    )
    job = await isolated_db.create_job(
        content="Python Engineering Manager\nPython delivery", resume_id=resume["resume_id"]
    )
    return resume, job


@pytest.fixture(autouse=True)
def configured_generation(monkeypatch):
    monkeypatch.setattr(
        "app.routers.career_positioning.get_truth_library", lambda: deepcopy(truth_library())
    )
    monkeypatch.setattr(
        "app.routers.career_positioning.get_llm_config",
        lambda: LLMConfig(provider="ollama", model="example-model", api_key="", api_base="http://127.0.0.1:11434"),
    )

    async def safe_outputs(spec, config):
        outputs = {}
        for value in spec.llm_items:
            payload = value.binding.source_payload
            if value.section == "EXPERIENCE": payload = payload["description"]
            if value.section == "ACHIEVEMENTS": payload = payload["content"]
            outputs[value.source_reference] = payload
        return outputs

    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", safe_outputs)


def plan_path(records):
    resume, job = records
    return f"/api/v1/jobs/{job['job_id']}/resumes/{resume['resume_id']}/transformation-plan"


def generation_path(records):
    return f"{plan_path(records)}/generation"


def references(plan):
    return sorted(
        item["source_reference"]
        for key in ("summary_plan", "experience_plan", "competencies_plan", "achievements_plan", "tools_plan")
        for item in plan[key]
    )


async def approve(client, records, *, submit=True, decision="ACCEPT"):
    plan = (await client.get(plan_path(records))).json()
    response = await client.post(
        f"{plan_path(records)}/approval",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "decisions": [{"source_reference": ref, "decision": decision} for ref in references(plan)],
            "guardrails_acknowledged": submit,
            "submit": submit,
        },
    )
    return plan, response.json()


async def generate(client, records, plan, approval, retry=False):
    return await client.post(
        generation_path(records),
        json={"approval_id": approval["approval_id"], "plan_fingerprint": plan["plan_fingerprint"], "retry_failed": retry},
    )


async def count(database, model):
    async with database._session() as session:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)


def passthrough_outputs(spec):
    outputs = {}
    for value in spec.llm_items:
        payload = value.binding.source_payload
        if value.section == "EXPERIENCE":
            payload = payload["description"]
        elif value.section == "ACHIEVEMENTS":
            payload = payload["content"]
        outputs[value.source_reference] = payload
    return outputs


async def test_01_missing_job_404(api_client, isolated_db, records):
    resume, _ = records
    response = await api_client.post(f"/api/v1/jobs/missing/resumes/{resume['resume_id']}/transformation-plan/generation", json={"approval_id": "missing", "plan_fingerprint": "a" * 64})
    assert response.status_code == 404


async def test_02_missing_resume_404(api_client, isolated_db, records):
    _, job = records
    response = await api_client.post(f"/api/v1/jobs/{job['job_id']}/resumes/missing/transformation-plan/generation", json={"approval_id": "missing", "plan_fingerprint": "a" * 64})
    assert response.status_code == 404


async def test_03_missing_approval_404(api_client, isolated_db, records):
    plan = (await api_client.get(plan_path(records))).json()
    response = await api_client.post(generation_path(records), json={"approval_id": "missing", "plan_fingerprint": plan["plan_fingerprint"]})
    assert response.status_code == 404


async def test_04_draft_422(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records, submit=False)
    assert (await generate(api_client, records, plan, approval)).status_code == 422


async def test_05_requires_review_422(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records, decision="REQUEST_REVIEW")
    assert (await generate(api_client, records, plan, approval)).status_code == 422


async def test_06_superseded_409(api_client, isolated_db, records):
    resume, _ = records
    plan, approval = await approve(api_client, records)
    changed = processed_resume(); changed["summary"] = "Changed summary"
    await isolated_db.update_resume(resume["resume_id"], {"processed_data": changed})
    response = await generate(api_client, records, plan, approval)
    assert response.status_code == 409


async def test_07_stale_fingerprint_409(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records)
    plan["plan_fingerprint"] = "f" * 64
    assert (await generate(api_client, records, plan, approval)).status_code == 409


async def test_08_approved_post_200(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records)
    assert (await generate(api_client, records, plan, approval)).status_code == 200


async def test_09_llm_not_configured_503(api_client, isolated_db, records, monkeypatch):
    monkeypatch.setattr("app.routers.career_positioning.get_llm_config", lambda: LLMConfig(provider="openai", model="example-model", api_key="", api_base=None))
    plan, approval = await approve(api_client, records)
    response = await generate(api_client, records, plan, approval)
    assert response.status_code == 503


async def test_10_generated_contract(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records)
    body = (await generate(api_client, records, plan, approval)).json()
    assert body["status"] == "GENERATED" and body["draft_resume"] and body["provenance"]


async def test_11_get_current(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    assert (await api_client.get(generation_path(records))).status_code == 200


async def test_12_get_by_id(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records)
    created = (await generate(api_client, records, plan, approval)).json()
    fetched = await api_client.get(f"{generation_path(records)}/{created['generation_id']}")
    assert fetched.status_code == 200 and fetched.json()["generation_id"] == created["generation_id"]


async def test_13_generation_not_found(api_client, isolated_db, records):
    assert (await api_client.get(f"{generation_path(records)}/missing")).status_code == 404


async def test_14_identical_post_reuses_success(api_client, isolated_db, records, monkeypatch):
    calls = 0
    async def counted(spec, config):
        nonlocal calls; calls += 1; return passthrough_outputs(spec)
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", counted)
    plan, approval = await approve(api_client, records)
    first = await generate(api_client, records, plan, approval); second = await generate(api_client, records, plan, approval)
    assert first.json()["generation_id"] == second.json()["generation_id"] and calls == 1


async def test_15_parallel_post_has_one_winner(api_client, isolated_db, records, monkeypatch):
    entered = asyncio.Event(); release = asyncio.Event(); calls = 0
    async def controlled(spec, config):
        nonlocal calls; calls += 1; entered.set(); await release.wait(); return passthrough_outputs(spec)
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", controlled)
    plan, approval = await approve(api_client, records)
    first = asyncio.create_task(generate(api_client, records, plan, approval)); await entered.wait()
    second = await generate(api_client, records, plan, approval); release.set(); completed = await first
    assert {second.status_code, completed.status_code} == {200, 409} and calls == 1


async def test_16_generation_in_progress_code(api_client, isolated_db, records, monkeypatch):
    entered = asyncio.Event(); release = asyncio.Event()
    async def controlled(spec, config): entered.set(); await release.wait(); return {}
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", controlled)
    plan, approval = await approve(api_client, records)
    task = asyncio.create_task(generate(api_client, records, plan, approval)); await entered.wait()
    response = await generate(api_client, records, plan, approval); release.set(); await task
    assert response.json()["detail"]["code"] == "GENERATION_IN_PROGRESS"


async def test_17_failed_has_no_partial_draft(api_client, isolated_db, records, monkeypatch):
    async def failed(spec, config): raise RuntimeError("provider secret detail")
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", failed)
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    row = await isolated_db.get_transformation_generation(job_id=records[1]["job_id"], resume_id=records[0]["resume_id"], plan_fingerprint=plan["plan_fingerprint"])
    assert row["status"] == "FAILED" and row["draft_resume"] is None and row["provenance"] == []


async def test_18_retry_failed(api_client, isolated_db, records, monkeypatch):
    calls = 0
    async def flaky(spec, config):
        nonlocal calls; calls += 1
        if calls == 1: raise RuntimeError("fail")
        return passthrough_outputs(spec)
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", flaky)
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    response = await generate(api_client, records, plan, approval, retry=True)
    assert response.status_code == 200 and response.json()["attempt_count"] == 2


async def test_19_parallel_retry_has_one_winner(api_client, isolated_db, records, monkeypatch):
    async def failed(spec, config): raise RuntimeError("fail")
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", failed)
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    entered = asyncio.Event(); release = asyncio.Event(); calls = 0
    async def controlled(spec, config):
        nonlocal calls; calls += 1; entered.set(); await release.wait(); return passthrough_outputs(spec)
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", controlled)
    first = asyncio.create_task(generate(api_client, records, plan, approval, retry=True)); await entered.wait()
    second = await generate(api_client, records, plan, approval, retry=True); release.set(); done = await first
    assert {second.status_code, done.status_code} == {200, 409} and calls == 1


async def test_20_malformed_model_response_502(api_client, isolated_db, records, monkeypatch):
    async def malformed(spec, config): raise GenerationValidationError("INVALID_LLM_RESPONSE", "bad")
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", malformed)
    plan, approval = await approve(api_client, records)
    assert (await generate(api_client, records, plan, approval)).status_code == 502


@pytest.mark.parametrize("error", [RuntimeError("api-key-secret"), TimeoutError("upstream timeout api-key-secret")], ids=["21-provider", "22-timeout"])
async def test_21_to_22_provider_failures_are_sanitized(error, api_client, isolated_db, records, monkeypatch):
    async def failed(spec, config): raise error
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", failed)
    plan, approval = await approve(api_client, records); response = await generate(api_client, records, plan, approval)
    assert response.status_code == 502 and "secret" not in response.text and "timeout" not in response.text


async def test_23_resume_is_unchanged(api_client, isolated_db, records):
    resume, _ = records; before = await isolated_db.get_resume(resume["resume_id"])
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    assert await isolated_db.get_resume(resume["resume_id"]) == before


async def test_24_no_new_resume(api_client, isolated_db, records):
    before = await count(isolated_db, Resume); plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    assert await count(isolated_db, Resume) == before


async def test_25_job_is_unchanged(api_client, isolated_db, records):
    _, job = records; before = await isolated_db.get_job(job["job_id"])
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    assert await isolated_db.get_job(job["job_id"]) == before


async def test_26_application_is_unchanged(api_client, isolated_db, records):
    resume, job = records; application = await isolated_db.create_application(job_id=job["job_id"], resume_id=resume["resume_id"]); before = await isolated_db.get_application(application["application_id"])
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    assert await isolated_db.get_application(application["application_id"]) == before


async def test_27_no_improvement(api_client, isolated_db, records):
    before = await count(isolated_db, Improvement); plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    assert await count(isolated_db, Improvement) == before


async def test_28_no_metric_event_from_generation(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records); before = await count(isolated_db, MetricEvent); await generate(api_client, records, plan, approval)
    assert await count(isolated_db, MetricEvent) == before


async def test_29_no_resume_generated_metric(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    async with isolated_db._session() as session:
        value = await session.scalar(select(func.count()).select_from(MetricEvent).where(MetricEvent.event_type == "RESUME_GENERATED"))
    assert value == 0


async def test_30_source_change_during_llm_409(api_client, isolated_db, records, monkeypatch):
    resume, _ = records
    async def changing(spec, config):
        changed = processed_resume(); changed["summary"] = "Changed during generation"
        await isolated_db.update_resume(resume["resume_id"], {"processed_data": changed}); return {}
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", changing)
    plan, approval = await approve(api_client, records); response = await generate(api_client, records, plan, approval)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SOURCE_CHANGED_DURING_GENERATION"


async def test_31_approval_change_during_llm_409(api_client, isolated_db, records, monkeypatch):
    plan, approval = await approve(api_client, records)
    async def changing(spec, config):
        await isolated_db.save_transformation_plan_approval(job_id=records[1]["job_id"], resume_id=records[0]["resume_id"], plan_version=plan["plan_version"], plan_fingerprint=plan["plan_fingerprint"], status="APPROVED", decisions=[{**item, "decision": "REJECT"} for item in approval["decisions"]], guardrails_acknowledged=True); return {}
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", changing)
    assert (await generate(api_client, records, plan, approval)).status_code == 409


async def test_32_toctou_row_is_superseded(api_client, isolated_db, records, monkeypatch):
    resume, _ = records
    async def changing(spec, config):
        changed = processed_resume(); changed["summary"] = "Changed during generation"
        await isolated_db.update_resume(resume["resume_id"], {"processed_data": changed}); return {}
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", changing)
    plan, approval = await approve(api_client, records); await generate(api_client, records, plan, approval)
    async with isolated_db._session() as session:
        row = (await session.execute(select(CVTransformationGeneration))).scalars().one()
    assert row.status == "SUPERSEDED" and row.draft_json is None


async def test_33_public_dto_is_private(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records); text = (await generate(api_client, records, plan, approval)).text
    for forbidden in ("private-example-id", "source_payload", "allowed_claim_codes", "api_key", "api_base", "system_prompt", "source_prompt"):
        assert forbidden not in text


async def test_34_provider_model_are_safe_metadata(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records); body = (await generate(api_client, records, plan, approval)).json()
    assert body["provider"] == "ollama" and body["model"] == "example-model"


async def test_35_truth_validation_is_not_run(api_client, isolated_db, records):
    plan, approval = await approve(api_client, records); body = (await generate(api_client, records, plan, approval)).json()
    assert body["requires_truth_validation"] is True and body["truth_validation_status"] == "NOT_RUN" and body["applied_to_resume"] is False


async def test_r1_approval_change_makes_old_generation_noncurrent_and_superseded(api_client, isolated_db, records):
    plan, approved = await approve(api_client, records)
    created = (await generate(api_client, records, plan, approved)).json()
    changed_response = await api_client.post(
        f"{plan_path(records)}/approval",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "decisions": [
                {"source_reference": ref, "decision": "REJECT"}
                for ref in references(plan)
            ],
            "guardrails_acknowledged": True,
            "submit": True,
        },
    )
    changed = changed_response.json()
    assert changed["approval_id"] == approved["approval_id"]
    assert (await api_client.get(generation_path(records))).status_code == 404
    stale = (await api_client.get(f"{generation_path(records)}/{created['generation_id']}")).json()
    assert stale["status"] == "SUPERSEDED"
    assert stale["draft_resume"] is None and stale["provenance"] == []
    replacement = (await generate(api_client, records, plan, changed)).json()
    assert replacement["generation_id"] != created["generation_id"]
    assert replacement["generation_input_fingerprint"] != created["generation_input_fingerprint"]


@pytest.mark.parametrize(
    ("provider", "model", "api_base"),
    [
        ("ollama", "other-model", "http://127.0.0.1:11434"),
        ("openai_compatible", "example-model", "http://127.0.0.1:1234"),
    ],
    ids=["model-change", "provider-change"],
)
async def test_r1_provider_or_model_change_makes_old_generation_noncurrent(
    provider, model, api_base, api_client, isolated_db, records, monkeypatch
):
    plan, approved = await approve(api_client, records)
    created = (await generate(api_client, records, plan, approved)).json()
    monkeypatch.setattr(
        "app.routers.career_positioning.get_llm_config",
        lambda: LLMConfig(provider=provider, model=model, api_key="", api_base=api_base),
    )
    assert (await api_client.get(generation_path(records))).status_code == 404
    stale = (await api_client.get(f"{generation_path(records)}/{created['generation_id']}")).json()
    assert stale["status"] == "SUPERSEDED" and stale["draft_resume"] is None
    replacement = (await generate(api_client, records, plan, approved)).json()
    assert replacement["generation_input_fingerprint"] != created["generation_input_fingerprint"]


async def test_r1_draft_approval_makes_old_generation_noncurrent(api_client, isolated_db, records):
    plan, approved = await approve(api_client, records)
    await generate(api_client, records, plan, approved)
    await api_client.post(
        f"{plan_path(records)}/approval",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "decisions": approved["decisions"],
            "guardrails_acknowledged": True,
            "submit": False,
        },
    )
    response = await api_client.get(generation_path(records))
    assert response.status_code == 404


async def test_r1_guardrail_acknowledgment_change_makes_old_generation_noncurrent(
    api_client, isolated_db, records
):
    plan, approved = await approve(api_client, records)
    await generate(api_client, records, plan, approved)
    await isolated_db.save_transformation_plan_approval(
        job_id=records[1]["job_id"], resume_id=records[0]["resume_id"],
        plan_version=plan["plan_version"], plan_fingerprint=plan["plan_fingerprint"],
        status="APPROVED", decisions=approved["decisions"],
        guardrails_acknowledged=False,
    )
    assert (await api_client.get(generation_path(records))).status_code == 404


async def test_r1_empty_plan_rejects_before_claim_or_llm(api_client, isolated_db, records, monkeypatch):
    resume_row, job_row = records
    empty = processed_resume()
    empty["summary"] = ""
    empty["workExperience"] = []
    empty["additional"]["technicalSkills"] = []
    await isolated_db.update_resume(resume_row["resume_id"], {"processed_data": empty})
    await isolated_db.update_job(job_row["job_id"], {"content": ""})
    plan = (await api_client.get(plan_path(records))).json()
    assert references(plan) == []
    approval_row, _ = await isolated_db.save_transformation_plan_approval(
        job_id=job_row["job_id"], resume_id=resume_row["resume_id"],
        plan_version=plan["plan_version"], plan_fingerprint=plan["plan_fingerprint"],
        status="APPROVED", decisions=[], guardrails_acknowledged=True,
    )
    calls = 0
    async def should_not_run(spec, config):
        nonlocal calls
        calls += 1
        return {}
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", should_not_run)
    response = await generate(api_client, records, plan, approval_row)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "NO_TRANSFORMATION_ITEMS"
    assert calls == 0 and await count(isolated_db, CVTransformationGeneration) == 0


async def test_r1_provider_exception_log_and_response_are_redacted(
    api_client, isolated_db, records, monkeypatch, caplog
):
    secrets = ["Private Example Company", "Private resume fragment", "sk-secret-value", "FULL PROMPT"]
    async def failed(spec, config):
        raise RuntimeError(" | ".join(secrets))
    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", failed)
    plan, approved = await approve(api_client, records)
    response = await generate(api_client, records, plan, approved)
    assert response.status_code == 502
    combined = caplog.text + response.text
    assert all(secret not in combined for secret in secrets)
    record = next(record for record in caplog.records if record.message == "Approved-plan generation provider failure")
    assert record.exception_class == "RuntimeError"
    assert record.failure_code == "LLM_PROVIDER_ERROR"


async def test_r1_final_post_assembly_toctou_supersedes_without_draft(
    api_client, isolated_db, records, monkeypatch
):
    import dataclasses
    from app.routers import career_positioning as router_module

    original = router_module._load_current_generation_context
    calls = 0
    async def changing_context(job_id, resume_id):
        nonlocal calls
        calls += 1
        context, resume_row, job_row, config = await original(job_id, resume_id)
        if calls == 2:
            context = dataclasses.replace(context, generation_input_fingerprint="f" * 64)
        return context, resume_row, job_row, config
    monkeypatch.setattr(router_module, "_load_current_generation_context", changing_context)
    plan, approved = await approve(api_client, records)
    response = await generate(api_client, records, plan, approved)
    assert response.status_code == 409
    row = await isolated_db.get_transformation_generation(
        job_id=records[1]["job_id"], resume_id=records[0]["resume_id"],
        generation_input_fingerprint=(
            await isolated_db.get_transformation_generation(
                job_id=records[1]["job_id"], resume_id=records[0]["resume_id"],
                plan_fingerprint=plan["plan_fingerprint"],
            )
        )["generation_input_fingerprint"],
    )
    assert row["status"] == "SUPERSEDED"
    assert row["draft_resume"] is None and row["provenance"] == []


@pytest.mark.parametrize("changed_source", ["resume", "job", "approval"])
async def test_r1_real_source_change_at_final_toctou_gate(
    changed_source, api_client, isolated_db, records, monkeypatch
):
    from app.routers import career_positioning as router_module

    plan, approved = await approve(api_client, records)
    original = router_module._load_current_generation_context
    calls = 0

    async def changing_context(job_id, resume_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            if changed_source == "resume":
                changed = processed_resume()
                changed["summary"] = "Changed only at final gate"
                await isolated_db.update_resume(resume_id, {"processed_data": changed})
            elif changed_source == "job":
                await isolated_db.update_job(job_id, {"content": "Changed only at final gate"})
            else:
                await isolated_db.save_transformation_plan_approval(
                    job_id=job_id, resume_id=resume_id,
                    plan_version=plan["plan_version"], plan_fingerprint=plan["plan_fingerprint"],
                    status="DRAFT", decisions=approved["decisions"],
                    guardrails_acknowledged=True,
                )
        return await original(job_id, resume_id)

    monkeypatch.setattr(router_module, "_load_current_generation_context", changing_context)
    response = await generate(api_client, records, plan, approved)
    assert response.status_code == 409
    async with isolated_db._session() as session:
        row = (await session.execute(select(CVTransformationGeneration))).scalars().one()
    assert row.status == "SUPERSEDED"
    assert row.draft_json is None and row.provenance_json is None


async def test_r1_finish_compare_and_set_rejects_wrong_attempt(isolated_db):
    fingerprint = "e" * 64
    row, claimed = await isolated_db.claim_transformation_generation(
        generation_input_fingerprint=fingerprint,
        values={
            "approval_id": "approval", "resume_id": "resume", "job_id": "job",
            "plan_version": "1.1", "plan_fingerprint": "a" * 64,
            "generation_version": "1.3", "prompt_version": "1.3",
            "provider": "ollama", "model": "model",
        },
    )
    assert claimed
    current, won = await isolated_db.finish_transformation_generation(
        row["generation_id"], status="GENERATED", draft_resume={"private": True}, provenance=[],
        expected_attempt_count=2,
        expected_generation_input_fingerprint=fingerprint,
    )
    assert not won and current["status"] == "GENERATING"
    assert current["draft_resume"] is None and current["provenance"] == []


async def test_r2_superseded_exact_input_recovers_once_with_one_row(
    api_client, isolated_db, records, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first_attempt(spec, config):
        entered.set()
        await release.wait()
        return passthrough_outputs(spec)

    monkeypatch.setattr(
        "app.routers.career_positioning.generate_item_outputs", first_attempt
    )
    plan, approved = await approve(api_client, records)
    first = asyncio.create_task(generate(api_client, records, plan, approved))
    await entered.wait()
    draft_response = await api_client.post(
        f"{plan_path(records)}/approval",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "decisions": approved["decisions"],
            "guardrails_acknowledged": True,
            "submit": False,
        },
    )
    assert draft_response.status_code == 200
    release.set()
    assert (await first).status_code == 409

    reapproved_response = await api_client.post(
        f"{plan_path(records)}/approval",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "decisions": approved["decisions"],
            "guardrails_acknowledged": True,
            "submit": True,
        },
    )
    reapproved = reapproved_response.json()
    assert reapproved["approval_id"] == approved["approval_id"]
    assert (await api_client.get(generation_path(records))).status_code == 404

    recovery_entered = asyncio.Event()
    recovery_release = asyncio.Event()
    calls = 0

    async def recovery(spec, config):
        nonlocal calls
        calls += 1
        recovery_entered.set()
        await recovery_release.wait()
        return passthrough_outputs(spec)

    monkeypatch.setattr("app.routers.career_positioning.generate_item_outputs", recovery)
    winner = asyncio.create_task(generate(api_client, records, plan, reapproved))
    await recovery_entered.wait()
    loser = await generate(api_client, records, plan, reapproved)
    recovery_release.set()
    completed = await winner

    assert completed.status_code == 200
    assert loser.status_code == 409
    assert loser.json()["detail"]["code"] == "GENERATION_IN_PROGRESS"
    assert calls == 1
    assert completed.json()["attempt_count"] == 2
    assert completed.json()["status"] == "GENERATED"
    assert await count(isolated_db, CVTransformationGeneration) == 1
