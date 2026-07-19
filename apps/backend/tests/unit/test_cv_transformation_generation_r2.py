"""Stage 10C-R2 per-bullet, hierarchy, DTO, collision and regex regressions."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.schemas.cv_transformation_generation import (
    CVTransformationGeneration,
    CVTransformationProvenance,
)
from app.schemas.cv_transformation_plan import CVTransformationPlanItem
from app.schemas.models import ResumeData
from app.services.cv_transformation_generation import (
    GENERATION_VERSION,
    PROMPT_VERSION,
    ApprovedGenerationItem,
    ApprovedGenerationSpec,
    GenerationValidationError,
    assemble_draft_resume,
    compile_approved_generation,
    detect_prohibited_claim_codes,
    generate_item_outputs,
    validate_item_response,
)
from app.services.cv_transformation_plan import (
    ExperienceBulletBinding,
    TransformationPlanBinding,
)
from tests.unit.test_cv_transformation_generation import approval, plan_for


def fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def bullet(index: int, text: str, *codes: str) -> ExperienceBulletBinding:
    return ExperienceBulletBinding(
        index=index,
        source_text=text,
        source_fingerprint=fingerprint(text),
        allowed_claim_codes=tuple(codes),
        allowed_operation="REPHRASE_EXISTING",
    )


def experience_binding(
    texts: list[str], codes: dict[int, tuple[str, ...]] | None = None
) -> TransformationPlanBinding:
    bullets = tuple(bullet(index, text, *(codes or {}).get(index, ())) for index, text in enumerate(texts))
    payload = {
        "id": 1,
        "title": "Role",
        "company": "Example",
        "location": None,
        "years": "2020-2024",
        "description": texts,
    }
    return TransformationPlanBinding(
        source_reference="resume:experience:0001",
        section="EXPERIENCE",
        namespace="resume",
        source_kind="resume_experience",
        source_payload=payload,
        source_fingerprint=fingerprint(payload),
        immutable_fields=("title", "company", "location", "years"),
        allowed_claim_codes=tuple(sorted({code for value in bullets for code in value.allowed_claim_codes})),
        allowed_operation="REPHRASE_EXISTING",
        source_position=0,
        parent_source_fingerprint=fingerprint(payload),
        bullet_bindings=bullets,
    )


def generated_experience(binding: TransformationPlanBinding) -> ApprovedGenerationItem:
    return ApprovedGenerationItem(
        binding.source_reference,
        "EXPERIENCE",
        "REPHRASE",
        "ACCEPT",
        "GENERATED",
        "NORMAL",
        True,
        binding,
    )


def response(binding: TransformationPlanBinding, texts: list[str]) -> dict:
    return {
        "source_reference": binding.source_reference,
        "prompt_version": "1.3",
        "content": [
            {
                "index": value.index,
                "source_fingerprint": value.source_fingerprint,
                "text": text,
            }
            for value, text in zip(binding.bullet_bindings, texts, strict=True)
        ],
    }


def assert_code(binding: TransformationPlanBinding, raw: dict, code: str) -> None:
    with pytest.raises(GenerationValidationError) as captured:
        validate_item_response(generated_experience(binding), raw)
    assert captured.value.code == code


def test_versions_are_exactly_13():
    assert PROMPT_VERSION == GENERATION_VERSION == "1.3"


def test_per_bullet_response_passes_and_returns_strings_for_assembly():
    binding = experience_binding(["Result 10%", "Cost 20%"], {0: ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE",), 1: ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE",)})
    raw = response(binding, ["Delivered result 10%", "Reduced cost 20%"])
    assert validate_item_response(generated_experience(binding), raw) == [
        "Delivered result 10%",
        "Reduced cost 20%",
    ]


def test_numbers_cannot_move_between_bullets():
    binding = experience_binding(["Result 10%", "Cost 20%"], {0: ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE",), 1: ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE",)})
    assert_code(binding, response(binding, ["Result 20%", "Cost 10%"]), "NUMERIC_BOUNDARY_VIOLATION")


@pytest.mark.parametrize(
    ("claim", "code"),
    [
        ("Współpraca z zarządem", "BOARD_WITHOUT_EVIDENCE"),
        ("Zarządzanie zespołem", "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE"),
        ("Odpowiedzialność P&L", "PNL_WITHOUT_EVIDENCE"),
    ],
)
def test_prohibited_claim_cannot_move_to_another_bullet(claim: str, code: str):
    binding = experience_binding([claim, "Delivery"], {0: (code,)})
    assert_code(
        binding,
        response(binding, ["Delivery", claim]),
        "PROHIBITED_CLAIM_BOUNDARY_VIOLATION",
    )


def test_two_bullets_cannot_be_merged():
    binding = experience_binding(["Built platform", "Led migration"])
    assert_code(
        binding,
        response(binding, ["Built platform and Led migration", "Led migration"]),
        "PROHIBITED_CLAIM_BOUNDARY_VIOLATION",
    )


@pytest.mark.parametrize("mutation", ["swapped", "duplicate", "missing", "bad-fingerprint", "extra"])
def test_bullet_index_and_fingerprint_invariants(mutation: str):
    binding = experience_binding(["First", "Second"])
    raw = response(binding, ["First revised", "Second revised"])
    if mutation == "swapped":
        raw["content"] = list(reversed(raw["content"]))
    elif mutation == "duplicate":
        raw["content"][1]["index"] = 0
    elif mutation == "missing":
        raw["content"] = raw["content"][:1]
    elif mutation == "bad-fingerprint":
        raw["content"][0]["source_fingerprint"] = "f" * 64
    else:
        raw["content"].append(
            {"index": 2, "source_fingerprint": "f" * 64, "text": "Extra"}
        )
    assert_code(binding, raw, "INVALID_LLM_RESPONSE")


@pytest.mark.parametrize("mutation", ["missing-field", "extra-field", "blank-text", "string-array"])
def test_bullet_object_is_strict(mutation: str):
    binding = experience_binding(["First"])
    raw = response(binding, ["First revised"])
    if mutation == "missing-field":
        del raw["content"][0]["source_fingerprint"]
    elif mutation == "extra-field":
        raw["content"][0]["extra"] = True
    elif mutation == "blank-text":
        raw["content"][0]["text"] = " "
    else:
        raw["content"] = ["First revised"]
    assert_code(binding, raw, "INVALID_LLM_RESPONSE")


def test_empty_experience_is_copied_without_llm():
    plan = plan_for("EMPHASIZE", "EXPERIENCE")
    binding = experience_binding([])
    item = compile_approved_generation(plan, approval(plan), (binding,)).items[0]
    assert (item.mode, item.priority, item.llm_used) == (
        "COPIED_NO_MUTABLE_CONTENT",
        "HIGH",
        False,
    )
    draft, provenance = assemble_draft_resume(
        ResumeData.model_validate({"workExperience": [binding.source_payload]}).model_dump(mode="json"),
        ApprovedGenerationSpec((item,)),
        {},
    )
    assert draft["workExperience"][0]["title"] == "Role"
    assert provenance[0]["output_fingerprint"] is not None
    assert provenance[0]["boundary_validation_status"] == "NOT_APPLICABLE"


def hierarchy_spec(parent_action: str, parent_decision: str = "ACCEPT"):
    plan = plan_for(parent_action, "EXPERIENCE")
    achievement = CVTransformationPlanItem(
        action="EMPHASIZE",
        section="ACHIEVEMENTS",
        source_reference="resume:achievements:0001",
        display_label="Result 20%",
        reason_code="TEST",
        reason="Test",
        evidence_strength="STRONG",
        human_review_required=False,
    )
    plan = plan.model_copy(update={"achievements_plan": [achievement]})
    parent = experience_binding(["Result 20%"])
    if parent_action == "HUMAN_REVIEW":
        parent = replace(
            parent,
            namespace="positioning",
            source_payload=None,
            source_fingerprint=fingerprint(None),
            bullet_bindings=(),
        )
    child_payload = {
        "content": "Result 20%",
        "title": "Role",
        "company": "Example",
        "years": "2020-2024",
        "location": None,
    }
    child = TransformationPlanBinding(
        source_reference=achievement.source_reference,
        section="ACHIEVEMENTS",
        namespace="resume",
        source_kind="resume_achievement",
        source_payload=child_payload,
        source_fingerprint=fingerprint(child_payload),
        immutable_fields=("title", "company", "years", "location"),
        allowed_claim_codes=("QUANTIFIED_RESULT_WITHOUT_EVIDENCE",),
        allowed_operation="EMPHASIZE_EXISTING",
        source_position=0,
        parent_experience_position=0,
        description_position=0,
        parent_source_fingerprint=parent.parent_source_fingerprint,
    )
    decisions = [
        {"source_reference": parent.source_reference, "decision": parent_decision},
        {"source_reference": child.source_reference, "decision": "ACCEPT"},
    ]
    approval_value = approval(plan)
    approval_value["decisions"] = sorted(decisions, key=lambda value: value["source_reference"])
    return compile_approved_generation(plan, approval_value, (parent, child))


@pytest.mark.parametrize(
    "action,decision",
    [("KEEP", "REJECT"), ("OMIT", "ACCEPT"), ("HUMAN_REVIEW", "ACCEPT")],
)
def test_excluded_parent_compiles_child_as_excluded_by_parent(action: str, decision: str):
    spec = hierarchy_spec(action, decision)
    child = next(item for item in spec.items if item.section == "ACHIEVEMENTS")
    assert child.mode == "EXCLUDED_BY_PARENT"
    assert child.llm_used is False


@pytest.mark.asyncio
async def test_excluded_parent_child_never_calls_llm():
    spec = hierarchy_spec("KEEP", "REJECT")
    calls = 0

    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("LLM must not run")

    class Config:
        provider = "ollama"
        model = "example"
        reasoning_effort = None

    assert await generate_item_outputs(spec, Config(), complete=complete) == {}
    assert calls == 0


def test_excluded_parent_child_is_absent_from_draft_and_has_provenance():
    spec = hierarchy_spec("KEEP", "REJECT")
    source = ResumeData.model_validate(
        {"workExperience": [experience_binding(["Result 20%"]).source_payload]}
    ).model_dump(mode="json")
    draft, provenance = assemble_draft_resume(source, spec, {})
    assert draft["workExperience"] == []
    child = next(item for item in provenance if item["section"] == "ACHIEVEMENTS")
    assert child["mode"] == "EXCLUDED_BY_PARENT"
    assert child["output_fingerprint"] is None
    assert child["boundary_validation_status"] == "NOT_APPLICABLE"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("zarząd", ("BOARD_WITHOUT_EVIDENCE",)),
        ("współpraca z zarządem", ("BOARD_WITHOUT_EVIDENCE",)),
        ("raportowanie zarządowi", ("BOARD_WITHOUT_EVIDENCE",)),
        ("zarządzanie", ()),
        ("zarządzanie zespołem", ("PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE",)),
        ("zarządzanie menedżerami", ("MANAGING_MANAGERS_WITHOUT_EVIDENCE",)),
    ],
)
def test_board_regex_exact_claim_codes(text: str, expected: tuple[str, ...]):
    detected = detect_prohibited_claim_codes(text)
    assert tuple(code for code in detected if code != "QUANTIFIED_RESULT_WITHOUT_EVIDENCE") == expected


def base_resume() -> dict:
    return ResumeData.model_validate({}).model_dump(mode="json")


def competency_item(decision="ACCEPT") -> ApprovedGenerationItem:
    binding = TransformationPlanBinding(
        source_reference="positioning:competencies:0001",
        section="COMPETENCIES",
        namespace="positioning",
        source_kind="positioning_competency",
        source_payload="Systems thinking",
        source_fingerprint=fingerprint("Systems thinking"),
        immutable_fields=(),
        allowed_claim_codes=(),
        allowed_operation="EMPHASIZE_EXISTING",
    )
    return ApprovedGenerationItem(
        binding.source_reference,
        "COMPETENCIES",
        "EMPHASIZE",
        decision,
        "EXACT_INSERT" if decision == "ACCEPT" else "EXCLUDED",
        "HIGH" if decision == "ACCEPT" else "NORMAL",
        False,
        binding,
    )


@pytest.mark.parametrize("collision", ["custom-string-list", "custom-other", "metadata-only"])
def test_competency_collision_preserves_source_and_uses_suffix(collision: str):
    source = base_resume()
    key = "careerPositioningCompetencies"
    if collision == "custom-string-list":
        source["customSections"][key] = {
            "sectionType": "stringList", "items": None,
            "strings": ["User content"], "text": None,
        }
    elif collision == "custom-other":
        source["customSections"][key] = {
            "sectionType": "text", "items": None, "strings": None, "text": "User text",
        }
    else:
        source["sectionMeta"].append({
            "id": "user", "key": key, "displayName": "User section",
            "sectionType": "stringList", "isDefault": False, "isVisible": True, "order": 99,
        })
    before = deepcopy(source)
    draft, _ = assemble_draft_resume(source, ApprovedGenerationSpec((competency_item(),)), {})
    assert draft["customSections"].get(key) == before["customSections"].get(key)
    assert draft["customSections"][f"{key}2"]["strings"] == ["Systems thinking"]
    metadata = next(item for item in draft["sectionMeta"] if item["key"] == f"{key}2")
    assert metadata["id"] == f"system:approved-plan-generation:{key}2"


def test_rejected_competency_does_not_change_user_section_or_add_system_section():
    source = base_resume()
    key = "careerPositioningCompetencies"
    source["customSections"][key] = {
        "sectionType": "stringList", "items": None,
        "strings": ["Systems thinking"], "text": None,
    }
    before = deepcopy(source)
    draft, _ = assemble_draft_resume(
        source, ApprovedGenerationSpec((competency_item("REJECT"),)), {}
    )
    assert draft["customSections"] == before["customSections"]
    assert f"{key}2" not in draft["customSections"]


def valid_generation_payload() -> dict:
    draft = base_resume()
    provenance = {
        "source_reference": "resume:summary:0001",
        "section": "SUMMARY",
        "action": "KEEP",
        "decision": "ACCEPT",
        "mode": "COPIED",
        "priority": "NORMAL",
        "source_fingerprint": "a" * 64,
        "output_fingerprint": "b" * 64,
        "llm_used": False,
        "prompt_version": None,
        "validation_status": "NOT_RUN",
        "boundary_validation_status": "NOT_APPLICABLE",
    }
    return {
        "generation_id": "g", "approval_id": "a", "resume_id": "r", "job_id": "j",
        "plan_version": "1.1", "plan_fingerprint": "a" * 64,
        "generation_version": "1.3", "prompt_version": "1.3",
        "generation_input_fingerprint": "b" * 64, "status": "GENERATED",
        "provider": "ollama", "model": "model", "draft_resume": draft,
        "provenance": [provenance], "failure_code": None, "attempt_count": 1,
        "created_at": "2026-07-19T00:00:00+00:00", "updated_at": "2026-07-19T00:00:00+00:00",
        "completed_at": "2026-07-19T00:00:00+00:00", "requires_truth_validation": True,
        "truth_validation_status": "NOT_RUN", "applied_to_resume": False,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["workExperience"].append({"id": {}, "title": "x", "company": "x", "location": None, "years": "x", "description": []}),
        lambda d: d["workExperience"].append({"id": 1, "title": [], "company": "x", "location": None, "years": "x", "description": []}),
        lambda d: d["workExperience"].append({"id": 1, "title": "x", "company": "x", "location": None, "years": "x", "description": "bad"}),
        lambda d: d["additional"]["technicalSkills"].append(7),
        lambda d: d["sectionMeta"].append({"id": "x", "key": "x", "displayName": "x", "sectionType": "text", "isDefault": False, "isVisible": True, "order": "1"}),
        lambda d: d["customSections"].update({"x": {"sectionType": "stringList", "items": None, "strings": "bad", "text": None}}),
        lambda d: d["customSections"].update({"x": {"sectionType": "itemList", "items": [{"id": 1, "title": "x", "subtitle": None, "location": None, "years": "", "description": {}}], "strings": None, "text": None}}),
    ],
    ids=["id-object", "title-array", "description-string", "technical-number", "meta-order-string", "strings-string", "custom-description-object"],
)
def test_backend_resume_dto_rejects_nested_coercions(mutation):
    payload = valid_generation_payload()
    mutation(payload["draft_resume"])
    with pytest.raises(ValidationError):
        CVTransformationGeneration.model_validate(payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"section": "EXPERIENCE", "action": "EMPHASIZE", "mode": "COPIED_NO_MUTABLE_CONTENT", "llm_used": False, "prompt_version": None, "boundary_validation_status": "NOT_APPLICABLE"},
        {"section": "ACHIEVEMENTS", "action": "EMPHASIZE", "mode": "EXCLUDED_BY_PARENT", "llm_used": False, "prompt_version": None, "output_fingerprint": None, "boundary_validation_status": "NOT_APPLICABLE"},
    ],
)
def test_new_provenance_modes_validate(updates):
    value = valid_generation_payload()["provenance"][0] | updates
    if value["mode"] == "COPIED_NO_MUTABLE_CONTENT":
        value["output_fingerprint"] = "b" * 64
    CVTransformationProvenance.model_validate(value)


def test_failed_dto_requires_empty_provenance():
    payload = valid_generation_payload()
    payload.update(
        status="FAILED",
        draft_resume=None,
        failure_code="LLM_PROVIDER_ERROR",
    )
    with pytest.raises(ValidationError, match="FAILED"):
        CVTransformationGeneration.model_validate(payload)
