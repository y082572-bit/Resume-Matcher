"""Stage 10C compiler, binding, validation, fingerprint and assembly tests."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from app.llm import _extract_json
from app.schemas.cv_transformation_plan import (
    CVTransformationPlan,
    CVTransformationPlanItem,
    PROHIBITED_CLAIM_CODES,
    ProhibitedClaim,
    TransformationSourceSummary,
)
from app.services.cv_transformation_generation import (
    ApprovedGenerationItem,
    ApprovedGenerationSpec,
    GenerationValidationError,
    assemble_draft_resume,
    compile_approved_generation,
    compute_generation_input_fingerprint,
    validate_item_response,
)
from app.services.cv_transformation_plan import (
    ExperienceBulletBinding,
    TransformationPlanBinding,
    _ItemCandidate,
    _materialize,
)


def item(action="KEEP", section="SUMMARY", namespace="resume"):
    ref = f"{namespace}:{section.lower()}:0001"
    return CVTransformationPlanItem(
        action=action,
        section=section,
        source_reference=ref,
        display_label="Anonymous item",
        reason_code="TEST",
        reason="Anonymous deterministic test",
        evidence_strength="STRONG",
        human_review_required=action == "HUMAN_REVIEW",
    )


def binding(section="SUMMARY", namespace="resume", payload="Original summary", **updates):
    if section == "EXPERIENCE" and payload == "Original summary":
        payload = {"description": ["Built systems"]}
    values = {
        "source_reference": f"{namespace}:{section.lower()}:0001",
        "section": section,
        "namespace": namespace,
        "source_kind": f"{namespace}_{section.lower()}",
        "source_payload": payload,
        "source_fingerprint": "b" * 64,
        "immutable_fields": (),
        "allowed_claim_codes": (),
        "allowed_operation": "KEEP_EXISTING",
    }
    values.update(updates)
    if section == "EXPERIENCE" and "bullet_bindings" not in values:
        descriptions = payload.get("description", []) if isinstance(payload, dict) else []
        values["bullet_bindings"] = tuple(
            ExperienceBulletBinding(
                index=index,
                source_text=text,
                source_fingerprint=hashlib.sha256(
                    json.dumps(text, ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest(),
                allowed_claim_codes=values["allowed_claim_codes"],
                allowed_operation=values["allowed_operation"],
            )
            for index, text in enumerate(descriptions)
        )
    return TransformationPlanBinding(**values)


def plan_for(action="KEEP", section="SUMMARY", namespace="resume", fingerprint="a" * 64):
    value = item(action, section, namespace)
    section_fields = {
        "summary_plan": [], "experience_plan": [], "competencies_plan": [],
        "achievements_plan": [], "tools_plan": [],
    }
    key = {
        "SUMMARY": "summary_plan", "EXPERIENCE": "experience_plan",
        "COMPETENCIES": "competencies_plan", "ACHIEVEMENTS": "achievements_plan",
        "TOOLS": "tools_plan",
    }[section]
    section_fields[key] = [value]
    return CVTransformationPlan(
        plan_fingerprint=fingerprint,
        resume_id="resume-example",
        job_id="job-example",
        generated_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        detected_job_level="SPECIALIST",
        candidate_profile_level="SPECIALIST",
        positioning_strategy="BALANCED",
        requires_human_review=action == "HUMAN_REVIEW",
        evidence_permissions=[],
        prohibited_claims=[
            ProhibitedClaim(code=code, reason="Evidence required")
            for code in PROHIBITED_CLAIM_CODES
        ],
        limitations=[],
        source_summary=TransformationSourceSummary(
            approved_truth_items_count=0, resume_items_count=1, job_requirements_count=1
        ),
        **section_fields,
    )


def approval(plan, decision="ACCEPT", status="APPROVED"):
    return {
        "approval_id": "approval-example",
        "plan_version": plan.plan_version,
        "plan_fingerprint": plan.plan_fingerprint,
        "resume_id": plan.resume_id,
        "job_id": plan.job_id,
        "status": status,
        "decisions": [{"source_reference": plan_source(plan), "decision": decision}],
        "guardrails_acknowledged": True,
        "created_at": "2026-07-19T00:00:00+00:00",
        "updated_at": "2026-07-19T00:00:00+00:00",
    }


def plan_source(plan):
    for values in (
        plan.summary_plan, plan.experience_plan, plan.competencies_plan,
        plan.achievements_plan, plan.tools_plan,
    ):
        if values:
            return values[0].source_reference
    raise AssertionError("missing plan item")


@pytest.mark.parametrize(
    ("section", "namespace", "payload"),
    [
        ("SUMMARY", "resume", "Summary"),
        ("EXPERIENCE", "resume", {"title": "Role", "company": "Example Company", "years": "2020-2024", "description": ["Built systems"]}),
        ("COMPETENCIES", "positioning", "Systems design"),
        ("ACHIEVEMENTS", "resume", {"content": "Improved result by 20%", "title": "Role", "company": "Example Company", "years": "2020-2024"}),
        ("TOOLS", "resume", "Python"),
    ],
    ids=["01-summary-binding", "02-experience-binding", "03-competency-binding", "04-achievement-binding", "05-tool-binding"],
)
def test_01_to_05_binding_shapes(section, namespace, payload):
    items, _, bindings = _materialize([
        _ItemCandidate(
            namespace=namespace,
            section=section,
            canonical_key="anonymous-key",
            display_label="Anonymous item",
            action="KEEP",
            reason_code="EXPERIENCE_CONTEXT" if section == "EXPERIENCE" else "BALANCED_POSITIONING",
            evidence_strength="MODERATE",
            source_kind=f"{namespace}_{section.lower()}",
            source_payload=payload,
        )
    ])
    value = bindings[0]
    assert value.section == section
    assert value.source_payload == payload
    assert value.source_reference == items[0].source_reference


def test_06_source_reference_matches_plan():
    plan = plan_for()
    assert compile_approved_generation(plan, approval(plan), (binding(),)).items[0].source_reference == plan_source(plan)


def test_07_source_fingerprint_is_stable():
    assert binding().source_fingerprint == binding().source_fingerprint


def test_08_display_label_is_never_used_for_binding_resolution():
    plan = plan_for().model_copy(deep=True)
    plan.summary_plan[0].display_label = "Completely different public label"
    assert compile_approved_generation(plan, approval(plan), (binding(),)).items[0].binding.source_payload == "Original summary"


@pytest.mark.parametrize(
    ("action", "section", "namespace", "decision", "mode", "priority", "llm"),
    [
        ("KEEP", "SUMMARY", "resume", "ACCEPT", "COPIED", "NORMAL", False),
        ("EMPHASIZE", "SUMMARY", "resume", "ACCEPT", "GENERATED", "HIGH", True),
        ("EMPHASIZE", "EXPERIENCE", "resume", "ACCEPT", "GENERATED", "HIGH", True),
        ("EMPHASIZE", "COMPETENCIES", "positioning", "ACCEPT", "EXACT_INSERT", "HIGH", False),
        ("EMPHASIZE", "TOOLS", "resume", "ACCEPT", "EXACT_INSERT", "HIGH", False),
        ("REPHRASE", "SUMMARY", "resume", "ACCEPT", "GENERATED", "NORMAL", True),
        ("REPHRASE", "EXPERIENCE", "resume", "ACCEPT", "GENERATED", "NORMAL", True),
        ("REPHRASE", "ACHIEVEMENTS", "resume", "ACCEPT", "GENERATED", "NORMAL", True),
        ("DEEMPHASIZE", "EXPERIENCE", "resume", "ACCEPT", "COPIED_LOW_PRIORITY", "LOW", False),
        ("OMIT", "EXPERIENCE", "resume", "ACCEPT", "OMITTED", "LOW", False),
        ("HUMAN_REVIEW", "SUMMARY", "resume", "ACCEPT", "PRESERVED_AFTER_REVIEW", "NORMAL", False),
        ("KEEP", "SUMMARY", "resume", "REJECT", "EXCLUDED", "NORMAL", False),
    ],
    ids=[f"{number:02d}-{label}" for number, label in enumerate([
        "keep", "emphasize-summary", "emphasize-experience", "emphasize-competency",
        "emphasize-tool", "rephrase-summary", "rephrase-experience", "rephrase-achievement",
        "deemphasize", "omit", "human-review", "reject",
    ], start=9)],
)
def test_09_to_19_action_matrix(action, section, namespace, decision, mode, priority, llm):
    plan = plan_for(action, section, namespace)
    value = binding(section, namespace)
    compiled = compile_approved_generation(plan, approval(plan, decision), (value,)).items[0]
    assert (compiled.mode, compiled.priority, compiled.llm_used) == (mode, priority, llm)


def test_20_request_review_blocks_generation():
    plan = plan_for()
    with pytest.raises(GenerationValidationError, match="Review requests"):
        compile_approved_generation(plan, approval(plan, "REQUEST_REVIEW"), (binding(),))


@pytest.mark.parametrize("status", ["DRAFT", "REQUIRES_REVIEW", "SUPERSEDED"], ids=["21-draft", "22-requires-review", "23-superseded"])
def test_21_to_23_nonapproved_statuses_block(status):
    plan = plan_for()
    with pytest.raises(GenerationValidationError):
        compile_approved_generation(plan, approval(plan, status=status), (binding(),))


def test_24_binding_set_must_be_exact():
    plan = plan_for()
    with pytest.raises(GenerationValidationError, match="binding"):
        compile_approved_generation(plan, approval(plan), ())


def test_25_permissions_stay_on_the_same_reference():
    value = binding(allowed_claim_codes=("TEAM_SIZE_WITHOUT_EVIDENCE",))
    plan = plan_for()
    compiled = compile_approved_generation(plan, approval(plan), (value,))
    assert compiled.items[0].binding.allowed_claim_codes == ("TEAM_SIZE_WITHOUT_EVIDENCE",)


def test_26_experience_immutable_fields_are_explicit():
    value = binding("EXPERIENCE", payload={"description": ["Built systems"]}, immutable_fields=("title", "company", "years", "location"))
    assert value.immutable_fields == ("title", "company", "years", "location")


@pytest.mark.parametrize(
    ("section", "payload", "content"),
    [
        ("SUMMARY", "Led delivery", "Led reliable delivery"),
        ("EXPERIENCE", {"description": ["Improved 20%"]}, ["Improved delivery by 20%"]),
        ("ACHIEVEMENTS", {"content": "Improved 20%"}, "Improved outcome by 20%"),
    ],
    ids=["27-summary-valid", "28-experience-valid", "29-achievement-valid"],
)
def test_27_to_29_valid_llm_responses(section, payload, content):
    permissions = ("QUANTIFIED_RESULT_WITHOUT_EVIDENCE",) if "20%" in str(payload) else ()
    compiled = ApprovedGenerationItem("resume:test:0001", section, "REPHRASE", "ACCEPT", "GENERATED", "NORMAL", True, binding(section, payload=payload, source_reference="resume:test:0001", allowed_claim_codes=permissions, allowed_operation="REPHRASE_EXISTING"))
    response_content = content
    if section == "EXPERIENCE":
        response_content = [
            {
                "index": bullet.index,
                "source_fingerprint": bullet.source_fingerprint,
                "text": text,
            }
            for bullet, text in zip(compiled.binding.bullet_bindings, content, strict=True)
        ]
    raw = {"source_reference": "resume:test:0001", "prompt_version": "1.3", "content": response_content}
    assert validate_item_response(compiled, raw) == content


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"prompt_version": "1.3", "content": "Text"}, "INVALID_LLM_RESPONSE"),
        ({"source_reference": "other", "prompt_version": "1.3", "content": "Text"}, "SOURCE_REFERENCE_MISMATCH"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": ""}, "INVALID_LLM_RESPONSE"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": "```json"}, "INVALID_LLM_RESPONSE"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": "<b>Text</b>"}, "INVALID_LLM_RESPONSE"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": "Improved 30%"}, "NUMERIC_BOUNDARY_VIOLATION"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": "Saved USD 20"}, "NUMERIC_BOUNDARY_VIOLATION"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": "Text", "extra": True}, "INVALID_LLM_RESPONSE"),
        ({"source_reference": "ref", "prompt_version": "1.3", "content": ["Text"]}, "INVALID_LLM_RESPONSE"),
    ],
    ids=[f"{number:02d}-{label}" for number, label in enumerate([
        "missing-reference", "wrong-reference", "empty", "markdown", "html", "changed-percent",
        "new-currency-number", "extra-field", "wrong-shape",
    ], start=30)],
)
def test_30_to_38_invalid_llm_responses(raw, code):
    compiled = ApprovedGenerationItem("ref", "SUMMARY", "REPHRASE", "ACCEPT", "GENERATED", "NORMAL", True, binding(payload="Improved 20%", source_reference="ref"))
    with pytest.raises(GenerationValidationError) as captured:
        validate_item_response(compiled, raw)
    assert captured.value.code == code


def fingerprint(**updates):
    plan = plan_for()
    value = binding()
    spec = compile_approved_generation(plan, approval(plan), (value,))
    args = dict(plan=plan, approval=approval(plan), spec=spec, provider="test-provider", model="test-model", reasoning_effort="low")
    args.update(updates)
    return compute_generation_input_fingerprint(**args)


def test_39_generation_fingerprint_is_stable():
    assert fingerprint() == fingerprint()


@pytest.mark.parametrize(
    "change",
    ["prompt", "model", "provider", "plan", "approval", "decision", "source"],
    ids=["40-prompt", "41-model", "42-provider", "43-plan", "44-approval", "45-decision", "46-source"],
)
def test_40_to_46_semantic_inputs_change_fingerprint(change):
    baseline = fingerprint()
    plan = plan_for()
    appr = approval(plan)
    value = binding()
    if change == "plan": plan = plan_for(fingerprint="c" * 64); appr = approval(plan)
    if change == "approval": appr["approval_id"] = "approval-other"
    if change == "decision": appr["decisions"][0]["decision"] = "REJECT"
    if change == "source": value = binding(source_fingerprint="d" * 64)
    spec = compile_approved_generation(plan, appr, (value,))
    changed = compute_generation_input_fingerprint(
        plan=plan, approval=appr, spec=spec,
        provider="other" if change == "provider" else "test-provider",
        model="other" if change == "model" else "test-model",
        reasoning_effort="low",
        prompt_version="9.9" if change == "prompt" else "1.3",
    )
    assert changed != baseline


def test_47_api_key_is_not_a_fingerprint_input():
    assert "api_key" not in compute_generation_input_fingerprint.__annotations__


def resume_data():
    return {
        "personalInfo": {"name": "Example Candidate", "email": "candidate@example.test"},
        "summary": "Original summary",
        "workExperience": [{"id": 1, "title": "Engineer", "company": "Example Company", "years": "2020-2024", "description": ["Built systems"]}],
        "education": [{"id": 1, "institution": "Example University", "degree": "BSc", "years": "2016-2020"}],
        "personalProjects": [],
        "additional": {"technicalSkills": ["Python"], "languages": ["English"], "certificationsTraining": ["Example Certificate"], "awards": []},
        "sectionMeta": [], "customSections": {},
    }


@pytest.mark.parametrize("field", ["personalInfo", "education", "languages", "certificationsTraining"], ids=["48-contact", "49-education", "50-languages", "51-certificates"])
def test_48_to_51_unplanned_sections_are_preserved(field):
    original = resume_data()
    spec = ApprovedGenerationSpec((ApprovedGenerationItem("resume:summary:0001", "SUMMARY", "KEEP", "ACCEPT", "COPIED", "NORMAL", False, binding()),))
    draft, _ = assemble_draft_resume(deepcopy(original), spec, {})
    before = original[field] if field in original else original["additional"][field]
    after = draft[field] if field in draft else draft["additional"][field]
    if isinstance(before, dict):
        assert {key: after[key] for key in before} == before
    elif before and isinstance(before[0], dict):
        assert [{key: after[index][key] for key in value} for index, value in enumerate(before)] == before
    else:
        assert after == before


def test_52_summary_is_replaced_only_by_its_output():
    spec = ApprovedGenerationSpec((ApprovedGenerationItem("resume:summary:0001", "SUMMARY", "REPHRASE", "ACCEPT", "GENERATED", "NORMAL", True, binding()),))
    draft, _ = assemble_draft_resume(resume_data(), spec, {"resume:summary:0001": "Rephrased summary"})
    assert draft["summary"] == "Rephrased summary"


def test_53_experience_immutables_are_preserved():
    payload = resume_data()["workExperience"][0]
    value = binding("EXPERIENCE", payload=payload, immutable_fields=("title", "company", "years"))
    spec = ApprovedGenerationSpec((ApprovedGenerationItem(value.source_reference, "EXPERIENCE", "REPHRASE", "ACCEPT", "GENERATED", "NORMAL", True, value),))
    draft, _ = assemble_draft_resume(resume_data(), spec, {value.source_reference: ["Built reliable systems"]})
    assert {key: draft["workExperience"][0][key] for key in value.immutable_fields} == {key: payload[key] for key in value.immutable_fields}


def test_54_exact_tool_is_inserted_without_llm():
    value = binding("TOOLS", payload="PostgreSQL")
    spec = ApprovedGenerationSpec((ApprovedGenerationItem(value.source_reference, "TOOLS", "EMPHASIZE", "ACCEPT", "EXACT_INSERT", "HIGH", False, value),))
    draft, _ = assemble_draft_resume(resume_data(), spec, {})
    assert draft["additional"]["technicalSkills"] == ["Python", "PostgreSQL"]


def test_55_provenance_is_complete_and_contains_no_private_payload():
    value = binding(payload="Private source text")
    spec = ApprovedGenerationSpec((ApprovedGenerationItem(value.source_reference, "SUMMARY", "KEEP", "ACCEPT", "COPIED", "NORMAL", False, value),))
    _, provenance = assemble_draft_resume(resume_data(), spec, {})
    assert set(provenance[0]) == {"source_reference", "section", "action", "decision", "mode", "priority", "source_fingerprint", "output_fingerprint", "llm_used", "prompt_version", "validation_status", "boundary_validation_status"}
    assert provenance[0]["validation_status"] == "NOT_RUN"
    assert "Private source text" not in str(provenance)


def test_56_privacy_sensitive_json_errors_redact_provider_content(caplog):
    with pytest.raises(ValueError, match="privacy-sensitive"):
        _extract_json("Private Example Company provider response", redact_content=True)
    assert "Private Example Company" not in caplog.text
