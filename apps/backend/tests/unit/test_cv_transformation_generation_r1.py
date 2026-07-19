"""Stage 10C-R1 semantic, boundary, provenance and DTO regression tests."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.llm import LLMConfig
from app.schemas.cv_transformation_generation import (
    CVTransformationGeneration,
    CVTransformationProvenance,
)
from app.schemas.cv_transformation_plan import ProhibitedClaim
from app.services.cv_transformation_generation import (
    PROMPT_VERSION,
    ApprovedGenerationItem,
    ApprovedGenerationSpec,
    GenerationValidationError,
    assemble_draft_resume,
    build_current_generation_context,
    build_item_prompt,
    validate_item_response,
)
from app.services.cv_transformation_plan import ExperienceBulletBinding, TransformationPlanBinding
from tests.unit.test_cv_transformation_generation import approval, plan_for


def fp(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def resume(experiences=None):
    return {
        "personalInfo": {"name": "Example"},
        "summary": "Original",
        "workExperience": experiences or [],
        "education": [],
        "personalProjects": [],
        "additional": {
            "technicalSkills": ["Python"],
            "languages": [],
            "certificationsTraining": [],
            "awards": [],
        },
        "sectionMeta": [{
            "id": "other", "key": "other", "displayName": "Other",
            "sectionType": "stringList", "isDefault": False,
            "isVisible": True, "order": 0,
        }],
        "customSections": {
            "other": {"sectionType": "stringList", "strings": ["Preserved"]}
        },
    }


def binding(ref, section, payload, *, position=None, parent=None, bullet=None, **updates):
    values = dict(
        source_reference=ref,
        section=section,
        namespace="positioning" if section == "COMPETENCIES" else "resume",
        source_kind=f"r1_{section.lower()}",
        source_payload=payload,
        source_fingerprint=fp(payload),
        immutable_fields=("title", "company", "years") if section in {"EXPERIENCE", "ACHIEVEMENTS"} else (),
        allowed_claim_codes=(),
        allowed_operation="REPHRASE_EXISTING",
        source_position=position,
        parent_experience_position=parent,
        description_position=bullet,
        parent_source_fingerprint=None,
    )
    values.update(updates)
    if section == "EXPERIENCE" and "bullet_bindings" not in values:
        descriptions = payload.get("description", []) if isinstance(payload, dict) else []
        values["bullet_bindings"] = tuple(
            ExperienceBulletBinding(
                index=index,
                source_text=text,
                source_fingerprint=fp(text),
                allowed_claim_codes=values["allowed_claim_codes"],
                allowed_operation=values["allowed_operation"],
            )
            for index, text in enumerate(descriptions)
        )
    return TransformationPlanBinding(**values)


def generated(binding_value, action="REPHRASE", priority="NORMAL"):
    return ApprovedGenerationItem(
        binding_value.source_reference, binding_value.section, action, "ACCEPT",
        "GENERATED", priority, True, binding_value,
    )


def copied(binding_value, action="KEEP", mode="COPIED", decision="ACCEPT", priority="NORMAL"):
    return ApprovedGenerationItem(
        binding_value.source_reference, binding_value.section, action, decision,
        mode, priority, False, binding_value,
    )


def overlap_spec(achievement_action, achievement_mode, *, decision="ACCEPT", llm=False):
    experience = {
        "id": 1, "title": "Role", "company": "First", "years": "2020-2024",
        "description": ["Result 20%", "Second bullet"],
    }
    parent_fp = fp(experience)
    experience_binding = binding(
        "resume:experience:0001", "EXPERIENCE", experience,
        position=0, parent_source_fingerprint=parent_fp,
    )
    achievement_payload = {
        "content": "Result 20%", "title": "Role", "company": "First", "years": "2020-2024",
    }
    achievement_binding = binding(
        "resume:achievements:0001", "ACHIEVEMENTS", achievement_payload,
        position=0, parent=0, bullet=0, parent_source_fingerprint=parent_fp,
    )
    achievement = ApprovedGenerationItem(
        achievement_binding.source_reference, "ACHIEVEMENTS", achievement_action,
        decision, achievement_mode, "NORMAL", llm, achievement_binding,
    )
    return resume([experience]), ApprovedGenerationSpec((generated(experience_binding), achievement))


@pytest.mark.parametrize(
    ("action", "mode", "decision", "llm", "achievement_output", "expected"),
    [
        ("EMPHASIZE", "EXCLUDED", "REJECT", False, None, ["Experience second"]),
        ("REPHRASE", "GENERATED", "ACCEPT", True, "Final result 20%", ["Final result 20%", "Experience second"]),
        ("KEEP", "COPIED", "ACCEPT", False, None, ["Result 20%", "Experience second"]),
        ("HUMAN_REVIEW", "PRESERVED_AFTER_REVIEW", "ACCEPT", False, None, ["Result 20%", "Experience second"]),
        ("OMIT", "OMITTED", "ACCEPT", False, None, ["Experience second"]),
    ],
)
def test_overlap_matrix(action, mode, decision, llm, achievement_output, expected):
    source, spec = overlap_spec(action, mode, decision=decision, llm=llm)
    outputs = {"resume:experience:0001": ["Experience result 20%", "Experience second"]}
    if achievement_output:
        outputs["resume:achievements:0001"] = achievement_output
    draft, _ = assemble_draft_resume(source, spec, outputs)
    assert draft["workExperience"][0]["description"] == expected


@pytest.mark.parametrize(
    (
        "experience_action", "experience_llm", "achievement_action",
        "achievement_mode", "achievement_decision", "achievement_llm",
        "achievement_output", "expected_first",
    ),
    [
        ("EMPHASIZE", True, "EMPHASIZE", "EXCLUDED", "REJECT", False, None, None),
        ("EMPHASIZE", True, "REPHRASE", "GENERATED", "ACCEPT", True, "Achievement final 20%", "Achievement final 20%"),
        ("REPHRASE", True, "KEEP", "COPIED", "ACCEPT", False, None, "Result 20%"),
        ("REPHRASE", True, "HUMAN_REVIEW", "PRESERVED_AFTER_REVIEW", "ACCEPT", False, None, "Result 20%"),
        ("KEEP", False, "EMPHASIZE", "GENERATED", "ACCEPT", True, "Achievement final 20%", "Achievement final 20%"),
    ],
    ids=[
        "experience-emphasize-achievement-reject",
        "experience-emphasize-achievement-rephrase",
        "experience-rephrase-achievement-keep",
        "experience-rephrase-achievement-human-review",
        "experience-keep-achievement-emphasize",
    ],
)
def test_required_experience_achievement_action_combinations(
    experience_action, experience_llm, achievement_action, achievement_mode,
    achievement_decision, achievement_llm, achievement_output, expected_first,
):
    source, base_spec = overlap_spec(
        achievement_action,
        achievement_mode,
        decision=achievement_decision,
        llm=achievement_llm,
    )
    experience_binding = base_spec.items[0].binding
    experience_item = (
        generated(
            experience_binding,
            action=experience_action,
            priority="HIGH" if experience_action == "EMPHASIZE" else "NORMAL",
        )
        if experience_llm
        else copied(experience_binding, action="KEEP")
    )
    spec = ApprovedGenerationSpec((experience_item, base_spec.items[1]))
    outputs = {}
    if experience_llm:
        outputs[experience_binding.source_reference] = [
            "Experience transformed 20%", "Experience transformed second",
        ]
    if achievement_output is not None:
        outputs[base_spec.items[1].source_reference] = achievement_output
    draft, _ = assemble_draft_resume(source, spec, outputs)
    descriptions = draft["workExperience"][0]["description"]
    if expected_first is None:
        assert descriptions == ["Experience transformed second"]
    else:
        assert descriptions[0] == expected_first


def test_two_achievements_use_original_indexes_after_first_removal():
    experience = {"id": 1, "title": "Role", "company": "One", "years": "2020", "description": ["10%", "20%"]}
    parent_fp = fp(experience)
    exp = binding("resume:experience:0001", "EXPERIENCE", experience, position=0, parent_source_fingerprint=parent_fp)
    achievements = []
    for index, content in enumerate(experience["description"]):
        value = binding(
            f"resume:achievements:{index + 1:04d}", "ACHIEVEMENTS",
            {"content": content, "title": "Role", "company": "One", "years": "2020"},
            parent=0, bullet=index, parent_source_fingerprint=parent_fp,
        )
        achievements.append(copied(value, action="OMIT", mode="OMITTED"))
    draft, _ = assemble_draft_resume(resume([experience]), ApprovedGenerationSpec((copied(exp), *achievements)), {})
    assert draft["workExperience"][0]["description"] == []


def test_duplicate_bullets_and_identical_bullets_across_companies_are_scoped():
    experiences = [
        {"id": 1, "title": "Role", "company": "One", "years": "2020", "description": ["20%", "20%"]},
        {"id": 2, "title": "Role", "company": "Two", "years": "2021", "description": ["20%"]},
    ]
    items = []
    for position, value in enumerate(experiences):
        items.append(copied(binding(
            f"resume:experience:{position + 1:04d}", "EXPERIENCE", value,
            position=position, parent_source_fingerprint=fp(value),
        )))
    target = binding(
        "resume:achievements:0001", "ACHIEVEMENTS",
        {"content": "20%", "title": "Role", "company": "One", "years": "2020"},
        parent=0, bullet=1, parent_source_fingerprint=fp(experiences[0]),
    )
    items.append(copied(target, action="OMIT", mode="OMITTED"))
    draft, _ = assemble_draft_resume(resume(experiences), ApprovedGenerationSpec(tuple(items)), {})
    assert draft["workExperience"][0]["description"] == ["20%"]
    assert draft["workExperience"][1]["description"] == ["20%"]


@pytest.mark.parametrize(
    "output",
    [
        "Odpowiedzialność P&L", "Współpraca z zarządem", "Zarządzanie zespołem",
        "Zarządzanie menedżerami", "Zespół 12 osób", "Nowy certyfikat",
        "Język angielski C1", "Odpowiedzialność za budżet", "People management",
    ],
)
def test_new_prohibited_claims_are_rejected(output):
    value = binding("resume:summary:0001", "SUMMARY", "Original factual summary")
    item = generated(value)
    with pytest.raises(GenerationValidationError) as error:
        validate_item_response(item, {
            "source_reference": item.source_reference,
            "prompt_version": "1.3",
            "content": output,
        })
    assert error.value.code == "PROHIBITED_CLAIM_BOUNDARY_VIOLATION"


def test_existing_prohibited_claim_requires_exact_item_permission():
    value = binding("resume:summary:0001", "SUMMARY", "Współpraca z zarządem")
    item = generated(value)
    raw = {
        "source_reference": item.source_reference,
        "prompt_version": "1.3",
        "content": "Współpraca z zarządem",
    }
    with pytest.raises(GenerationValidationError) as error:
        validate_item_response(item, raw)
    assert error.value.code == "PROHIBITED_CLAIM_BOUNDARY_VIOLATION"
    permitted = replace(
        item,
        binding=replace(
            value,
            allowed_claim_codes=("BOARD_WITHOUT_EVIDENCE",),
            allowed_operation="REPHRASE_EXISTING",
        ),
    )
    assert validate_item_response(permitted, raw) == "Współpraca z zarządem"


def test_item_prompt_contains_all_guardrails_and_item_scoped_context():
    value = binding(
        "resume:summary:0001", "SUMMARY", "Python delivery",
        allowed_claim_codes=("TECHNICAL_SKILL_WITHOUT_EVIDENCE",),
        allowed_operation="REPHRASE_EXISTING",
    )
    item = generated(value)
    claims = tuple(
        {"code": f"CLAIM_{index}", "reason": "Do not add"} for index in range(10)
    )
    spec = ApprovedGenerationSpec((item,), claims, "Python delivery focus\nBoard ownership")
    prompt, system = build_item_prompt(item, spec)
    payload = json.loads(prompt)
    assert payload["allowed_operation"] == "REPHRASE_EXISTING"
    assert payload["allowed_claim_codes"] == ["TECHNICAL_SKILL_WITHOUT_EVIDENCE"]
    assert len(payload["prohibited_claims"]) == 10
    assert payload["job_style_context"] == {
        "classification": "STYLE_ONLY_NOT_FACT_SOURCE",
        "fragments": ["Python delivery focus"],
    }
    assert payload["prompt_version"] == "1.3"
    for phrase in ("P&L", "budget", "Board/C-level", "people management", "team size", "certification"):
        assert phrase.casefold() in system.casefold()


def test_current_context_is_pure_and_provider_model_are_fingerprinted():
    plan = plan_for("KEEP")
    value = binding("resume:summary:0001", "SUMMARY", "Original")
    approved = approval(plan)
    first = build_current_generation_context(
        plan=plan, bindings=(value,), approval=approved,
        config=LLMConfig(provider="ollama", model="one", api_key="", api_base="http://local"),
    )
    second = build_current_generation_context(
        plan=plan, bindings=(value,), approval=approved,
        config=LLMConfig(provider="ollama", model="two", api_key="", api_base="http://local"),
    )
    assert first.model == "one" and first.generation_input_fingerprint != second.generation_input_fingerprint


@pytest.mark.parametrize(
    "field",
    [
        "section", "namespace", "source_kind", "immutable_fields", "allowed_operation",
        "allowed_claim_codes", "source_position", "parent_experience_position",
        "description_position", "parent_source_fingerprint",
    ],
)
def test_binding_semantics_change_generation_fingerprint(field):
    plan = plan_for("KEEP")
    approved = approval(plan)
    value = binding("resume:summary:0001", "SUMMARY", "Original")
    baseline = build_current_generation_context(
        plan=plan, bindings=(value,), approval=approved,
        config=LLMConfig(provider="ollama", model="one", api_key="", api_base="http://local"),
    )
    changes = {
        "section": "TOOLS", "namespace": "positioning", "source_kind": "changed",
        "immutable_fields": ("title",), "allowed_operation": "EMPHASIZE_EXISTING",
        "allowed_claim_codes": ("BOARD_WITHOUT_EVIDENCE",), "source_position": 1,
        "parent_experience_position": 1, "description_position": 1,
        "parent_source_fingerprint": "f" * 64,
    }
    changed_binding = replace(value, **{field: changes[field]})
    # Fingerprinting is intentionally testable independent of public section consistency.
    changed_spec = ApprovedGenerationSpec((replace(baseline.spec.items[0], binding=changed_binding),), baseline.spec.prohibited_claims)
    from app.services.cv_transformation_generation import compute_generation_input_fingerprint
    changed = compute_generation_input_fingerprint(
        plan=plan, approval=approved, spec=changed_spec,
        provider="ollama", model="one", reasoning_effort=None,
    )
    assert changed != baseline.generation_input_fingerprint


def test_competencies_and_tools_use_separate_canonical_sections_without_overwrite():
    source = resume([])
    before_meta = deepcopy(source["sectionMeta"])
    before_other = deepcopy(source["customSections"]["other"])
    competency = binding("positioning:competencies:0001", "COMPETENCIES", "Systems thinking")
    tool = binding("resume:tools:0001", "TOOLS", "PostgreSQL")
    spec = ApprovedGenerationSpec((
        copied(competency, action="EMPHASIZE", mode="EXACT_INSERT", priority="HIGH"),
        copied(tool, action="EMPHASIZE", mode="EXACT_INSERT", priority="HIGH"),
    ))
    draft, _ = assemble_draft_resume(source, spec, {})
    assert draft["customSections"]["careerPositioningCompetencies"]["strings"] == ["Systems thinking"]
    assert draft["additional"]["technicalSkills"] == ["Python", "PostgreSQL"]
    assert {
        key: draft["customSections"]["other"][key] for key in before_other
    } == before_other
    assert draft["sectionMeta"][: len(before_meta)] == before_meta


def test_rejecting_competency_does_not_remove_tool_and_vice_versa():
    source = resume([])
    source["customSections"]["careerPositioningCompetencies"] = {
        "sectionType": "stringList", "strings": ["Systems thinking"]
    }
    competency = binding("positioning:competencies:0001", "COMPETENCIES", "Systems thinking")
    tool = binding("resume:tools:0001", "TOOLS", "Python")
    draft, _ = assemble_draft_resume(source, ApprovedGenerationSpec((
        copied(competency, mode="EXCLUDED", decision="REJECT"), copied(tool),
    )), {})
    assert draft["additional"]["technicalSkills"] == ["Python"]
    assert draft["customSections"]["careerPositioningCompetencies"]["strings"] == ["Systems thinking"]


def test_experience_order_is_priority_then_original_position_not_alphabetical():
    experiences = [
        {"id": 1, "title": "Role", "company": "Zulu", "years": "2020", "description": ["A"]},
        {"id": 2, "title": "Role", "company": "Alpha", "years": "2021", "description": ["B"]},
        {"id": 3, "title": "Role", "company": "Beta", "years": "2022", "description": ["C"]},
        {"id": 4, "title": "Role", "company": "Aardvark", "years": "2023", "description": ["D"]},
    ]
    priorities = ["NORMAL", "HIGH", "NORMAL", "LOW"]
    items = []
    for index, value in enumerate(experiences):
        b = binding(
            f"resume:experience:{index + 1:04d}", "EXPERIENCE", value,
            position=index, parent_source_fingerprint=fp(value),
        )
        items.append(copied(b, priority=priorities[index]))
    draft, _ = assemble_draft_resume(resume(experiences), ApprovedGenerationSpec(tuple(items)), {})
    assert [item["company"] for item in draft["workExperience"]] == ["Alpha", "Zulu", "Beta", "Aardvark"]


def valid_provenance(**updates):
    values = dict(
        source_reference="resume:summary:0001", section="SUMMARY", action="REPHRASE",
        decision="ACCEPT", mode="GENERATED", priority="NORMAL",
        source_fingerprint="a" * 64, output_fingerprint="b" * 64,
        llm_used=True, prompt_version="1.3", validation_status="NOT_RUN",
        boundary_validation_status="PASSED",
    )
    values.update(updates)
    return values


@pytest.mark.parametrize(
    "updates",
    [
        {"validation_status": "PASSED"}, {"boundary_validation_status": "NOT_APPLICABLE"},
        {"prompt_version": "1.0"}, {"llm_used": False}, {"output_fingerprint": None},
        {"decision": "REJECT"}, {"source_reference": "invalid"}, {"section": "EDUCATION"},
        {"action": "CREATE"}, {"mode": "UNKNOWN"},
    ],
)
def test_strict_provenance_rejects_invalid_contract(updates):
    with pytest.raises(ValidationError):
        CVTransformationProvenance.model_validate(valid_provenance(**updates))


def test_generation_dto_rejects_naive_dates_and_unsorted_provenance():
    base = {
        "generation_id": "g", "approval_id": "a", "resume_id": "r", "job_id": "j",
        "plan_version": "1.1", "plan_fingerprint": "a" * 64,
        "generation_version": "1.3", "prompt_version": "1.3",
        "generation_input_fingerprint": "b" * 64, "status": "SUPERSEDED",
        "provider": "ollama", "model": "model", "draft_resume": None, "provenance": [],
        "failure_code": "SOURCE_CHANGED_DURING_GENERATION", "attempt_count": 1,
        "created_at": datetime(2026, 1, 1), "updated_at": datetime.now(timezone.utc),
        "completed_at": datetime.now(timezone.utc), "requires_truth_validation": True,
        "truth_validation_status": "NOT_RUN", "applied_to_resume": False,
    }
    with pytest.raises(ValidationError, match="timezone-aware"):
        CVTransformationGeneration.model_validate(base)


def test_prompt_version_constant_is_12():
    assert PROMPT_VERSION == "1.3"
