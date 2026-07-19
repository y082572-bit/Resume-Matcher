"""Stage 10C-R3 real planner-to-validator claim permission regressions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.career_positioning_report import build_career_positioning_report
from app.services.cv_transformation_approval import plan_source_references
from app.services.cv_transformation_generation import (
    ApprovedGenerationSpec,
    GenerationValidationError,
    compile_approved_generation,
    generate_item_outputs,
    validate_item_response,
)
from app.services.cv_transformation_plan import build_cv_transformation_plan_bundle


APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _truth(*, approved: bool = True) -> dict:
    return {
        "meta": {"wersja": "stage10c-r3"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "private-r3-scope",
                        "firma": "Example Company",
                        "stanowisko": "Engineering Manager",
                        "okresOd": "2020-01",
                        "okresDo": "2025-12",
                        "aktywnosci": ["Python delivery"],
                        "wynikLiczbowy": "Improved result by 25%",
                        "status": APPROVED if approved else "DO_AKCEPTACJI",
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


def _processed(summary: str = "Manager with 20 years of experience") -> dict:
    return {
        "personalInfo": {"name": "Example Candidate", "email": "candidate@example.test"},
        "summary": summary,
        "workExperience": [
            {
                "id": 1,
                "title": "Engineering Manager",
                "company": "Example Company",
                "years": "2020-2025",
                "description": ["Improved result by 25%", "Python delivery"],
            },
            {
                "id": 2,
                "title": "Engineering Manager",
                "company": "Other Company",
                "years": "2020-2025",
                "description": ["Improved result by 25%", "Python delivery"],
            },
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


def _bundle(*, approved: bool = True, summary: str = "Manager with 20 years of experience"):
    truth = _truth(approved=approved)
    resume = {"resume_id": "resume-r3", "processed_data": _processed(summary)}
    job = {
        "job_id": "job-r3",
        "resume_id": "resume-r3",
        "role": "Engineering Manager",
        "content": "Engineering Manager Python delivery 25% results",
    }
    report = build_career_positioning_report(job, truth, NOW)
    report.positioning.positioning_strategy = "MANAGER_LEADERSHIP"
    report.positioning.requires_human_review = False
    return build_cv_transformation_plan_bundle(
        resume_id="resume-r3",
        job_id="job-r3",
        resume=resume,
        job=job,
        truth_library=truth,
        career_report=report,
        now=NOW,
    )


def _compiled(*, approved: bool = True, summary: str = "Manager with 20 years of experience"):
    bundle = _bundle(approved=approved, summary=summary)
    approval = {
        "approval_id": "approval-r3",
        "status": "APPROVED",
        "guardrails_acknowledged": True,
        "decisions": [
            {"source_reference": reference, "decision": "ACCEPT"}
            for reference in plan_source_references(bundle.plan)
        ],
    }
    return bundle, compile_approved_generation(bundle.plan, approval, bundle.bindings)


def _experience(spec, position: int):
    return next(
        item
        for item in spec.items
        if item.section == "EXPERIENCE" and item.binding.source_position == position
    )


def _response(item, texts: list[str]) -> dict:
    return {
        "source_reference": item.source_reference,
        "prompt_version": "1.3",
        "content": [
            {
                "index": bullet.index,
                "source_fingerprint": bullet.source_fingerprint,
                "text": text,
            }
            for bullet, text in zip(item.binding.bullet_bindings, texts, strict=True)
        ],
    }


def test_real_planner_permissions_are_exact_scoped_and_pass_validator():
    _, spec = _compiled()
    first = _experience(spec, 0)
    second = _experience(spec, 1)

    assert first.binding.bullet_bindings[0].allowed_claim_codes == (
        "QUANTIFIED_RESULT_WITHOUT_EVIDENCE",
    )
    assert first.binding.bullet_bindings[1].allowed_claim_codes == (
        "TECHNICAL_SKILL_WITHOUT_EVIDENCE",
    )
    assert set(first.binding.allowed_claim_codes) == {
        "QUANTIFIED_RESULT_WITHOUT_EVIDENCE",
        "TECHNICAL_SKILL_WITHOUT_EVIDENCE",
    }
    assert all(not bullet.allowed_claim_codes for bullet in second.binding.bullet_bindings)
    assert validate_item_response(
        first, _response(first, ["Improved result by 25%", "Python delivery"])
    ) == ["Improved result by 25%", "Python delivery"]


def test_changed_number_and_new_technical_skill_are_rejected():
    _, spec = _compiled()
    item = _experience(spec, 0)
    with pytest.raises(GenerationValidationError, match="Numeric tokens") as numeric:
        validate_item_response(item, _response(item, ["Improved result by 30%", "Python delivery"]))
    assert numeric.value.code == "NUMERIC_BOUNDARY_VIOLATION"
    with pytest.raises(GenerationValidationError) as technical:
        validate_item_response(item, _response(item, ["Improved result by 25%", "Kubernetes delivery"]))
    assert technical.value.code == "PROHIBITED_CLAIM_BOUNDARY_VIOLATION"


def test_unapproved_fact_creates_no_permission_and_protects_whole_experience():
    _, spec = _compiled(approved=False)
    item = _experience(spec, 0)
    assert all(not bullet.allowed_claim_codes for bullet in item.binding.bullet_bindings)
    assert item.mode == "COPIED_PROTECTED_CONTENT"
    assert item.llm_used is False


@pytest.mark.asyncio
async def test_unpermitted_summary_is_copied_without_llm_call():
    _, spec = _compiled()
    summary = next(item for item in spec.items if item.section == "SUMMARY")
    assert summary.mode == "COPIED_PROTECTED_CONTENT"
    assert summary.llm_used is False

    async def forbidden(*args, **kwargs):
        raise AssertionError("protected content must not call the LLM")

    outputs = await generate_item_outputs(
        ApprovedGenerationSpec(items=(summary,)), object(), complete=forbidden
    )
    assert outputs == {}


def test_board_source_is_protected_but_new_board_claim_is_rejected():
    _, protected_spec = _compiled(summary="Worked with Board")
    protected = next(item for item in protected_spec.items if item.section == "SUMMARY")
    assert protected.mode == "COPIED_PROTECTED_CONTENT"

    _, ordinary_spec = _compiled(summary="Experienced engineering leader")
    ordinary = next(item for item in ordinary_spec.items if item.section == "SUMMARY")
    assert ordinary.mode == "GENERATED"
    with pytest.raises(GenerationValidationError) as captured:
        validate_item_response(
            ordinary,
            {
                "source_reference": ordinary.source_reference,
                "prompt_version": "1.3",
                "content": "Experienced engineering leader working with Board",
            },
        )
    assert captured.value.code == "PROHIBITED_CLAIM_BOUNDARY_VIOLATION"
