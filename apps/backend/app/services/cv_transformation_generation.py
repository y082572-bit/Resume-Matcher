"""Controlled, item-scoped generation from one exact approved plan."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from app.llm import (
    LLMResponseContentError,
    complete_json,
    get_model_name,
    get_safe_max_tokens,
)
from app.schemas.cv_transformation_generation import GenerationLLMItemResponse
from app.schemas.cv_transformation_plan import CVTransformationPlan
from app.schemas.models import ResumeData, normalize_resume_data
from app.services.cv_transformation_approval import plan_source_references
from app.services.cv_claim_boundaries import (
    NUMBER_PATTERN,
    PROHIBITED_CLAIM_BOUNDARIES,
    detect_claim_codes,
    detect_claim_values,
)
from app.services.cv_transformation_plan import (
    ExperienceBulletBinding,
    TransformationPlanBinding,
)

GENERATION_VERSION = "1.3"
PROMPT_VERSION = "1.3"
_NUMBER_RE = NUMBER_PATTERN
_HTML_RE = re.compile(r"<[^>]+>")


@dataclass
class GenerationValidationError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class ApprovedGenerationItem:
    source_reference: str
    section: str
    action: str
    decision: str
    mode: str
    priority: str
    llm_used: bool
    binding: TransformationPlanBinding


@dataclass(frozen=True)
class ApprovedGenerationSpec:
    items: tuple[ApprovedGenerationItem, ...]
    prohibited_claims: tuple[dict[str, str], ...] = ()
    job_content: str = ""

    @property
    def llm_items(self) -> tuple[ApprovedGenerationItem, ...]:
        return tuple(item for item in self.items if item.llm_used)


@dataclass(frozen=True)
class CurrentGenerationContext:
    plan: CVTransformationPlan
    bindings: tuple[TransformationPlanBinding, ...]
    approval: dict[str, Any]
    spec: ApprovedGenerationSpec
    provider: str
    model: str
    generation_input_fingerprint: str


def _contains_unpermitted_source_claim(
    section: str, binding: TransformationPlanBinding
) -> bool:
    """Fail closed before transport when an exact source boundary lacks permission."""

    if section == "EXPERIENCE":
        return any(
            set(detect_claim_codes(bullet.source_text))
            - set(bullet.allowed_claim_codes)
            for bullet in binding.bullet_bindings
        )
    payload = binding.source_payload
    if section == "ACHIEVEMENTS" and isinstance(payload, dict):
        payload = payload.get("content", "")
    if not isinstance(payload, str):
        return False
    return bool(set(detect_claim_codes(payload)) - set(binding.allowed_claim_codes))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def compile_approved_generation(
    plan: CVTransformationPlan,
    approval: dict[str, Any],
    bindings: tuple[TransformationPlanBinding, ...],
    *,
    job_content: str = "",
) -> ApprovedGenerationSpec:
    """Compile user decisions into backend-owned, deterministic behavior."""

    if approval.get("status") != "APPROVED":
        code = {
            "DRAFT": "APPROVAL_NOT_SUBMITTED",
            "REQUIRES_REVIEW": "APPROVAL_REQUIRES_REVIEW",
            "SUPERSEDED": "APPROVAL_SUPERSEDED",
        }.get(approval.get("status"), "APPROVAL_NOT_APPROVED")
        raise GenerationValidationError(code, "An exact APPROVED plan is required")
    if not approval.get("guardrails_acknowledged"):
        raise GenerationValidationError("GUARDRAILS_NOT_ACKNOWLEDGED", "Guardrails are required")

    refs = plan_source_references(plan)
    decisions = approval.get("decisions") or []
    decision_map = {item.get("source_reference"): item.get("decision") for item in decisions}
    if set(decision_map) != set(refs):
        raise GenerationValidationError("INCOMPLETE_DECISIONS", "Every current item needs a decision")
    if any(value == "REQUEST_REVIEW" for value in decision_map.values()):
        raise GenerationValidationError("APPROVAL_REQUIRES_REVIEW", "Review requests block generation")
    if any(value not in {"ACCEPT", "REJECT"} for value in decision_map.values()):
        raise GenerationValidationError("INVALID_DECISION", "Unsupported approval decision")

    binding_map = {item.source_reference: item for item in bindings}
    if set(binding_map) != set(refs):
        raise GenerationValidationError("BINDING_MISMATCH", "Private binding set does not match plan")
    public_items = {
        item.source_reference: item
        for item in (
            plan.summary_plan
            + plan.experience_plan
            + plan.competencies_plan
            + plan.achievements_plan
            + plan.tools_plan
        )
    }
    compiled: list[ApprovedGenerationItem] = []
    for ref in refs:
        plan_item = public_items[ref]
        binding = binding_map[ref]
        decision = decision_map[ref]
        if decision == "REJECT":
            mode, priority, llm_used = "EXCLUDED", "NORMAL", False
        elif plan_item.action == "KEEP":
            mode, priority, llm_used = "COPIED", "NORMAL", False
        elif plan_item.action == "DEEMPHASIZE":
            mode, priority, llm_used = "COPIED_LOW_PRIORITY", "LOW", False
        elif plan_item.action == "OMIT":
            mode, priority, llm_used = "OMITTED", "LOW", False
        elif plan_item.action == "HUMAN_REVIEW":
            resolved = binding.namespace == "resume" and bool(binding.source_payload)
            mode = "PRESERVED_AFTER_REVIEW" if resolved else "EXCLUDED_UNRESOLVED"
            priority, llm_used = "NORMAL", False
        elif plan_item.section in {"COMPETENCIES", "TOOLS"}:
            mode, priority, llm_used = "EXACT_INSERT", (
                "HIGH" if plan_item.action == "EMPHASIZE" else "NORMAL"
            ), False
        elif plan_item.action in {"EMPHASIZE", "REPHRASE"}:
            priority = "HIGH" if plan_item.action == "EMPHASIZE" else "NORMAL"
            if (
                plan_item.section in {"SUMMARY", "EXPERIENCE", "ACHIEVEMENTS"}
                and _contains_unpermitted_source_claim(plan_item.section, binding)
            ):
                mode, llm_used = "COPIED_PROTECTED_CONTENT", False
            elif plan_item.section == "EXPERIENCE" and not binding.bullet_bindings:
                mode, llm_used = "COPIED_NO_MUTABLE_CONTENT", False
            else:
                mode, llm_used = "GENERATED", True
        else:
            raise GenerationValidationError("UNSUPPORTED_ACTION", "Unsupported plan action")
        compiled.append(
            ApprovedGenerationItem(
                source_reference=ref,
                section=plan_item.section,
                action=plan_item.action,
                decision=decision,
                mode=mode,
                priority=priority,
                llm_used=llm_used,
                binding=binding,
            )
        )
    parent_modes = {
        item.binding.source_position: item.mode
        for item in compiled
        if item.section == "EXPERIENCE" and item.binding.source_position is not None
    }
    parent_excluded = {"EXCLUDED", "OMITTED", "EXCLUDED_UNRESOLVED", "EXCLUDED_BY_PARENT"}
    dependency_compiled = []
    for item in compiled:
        parent_position = item.binding.parent_experience_position
        if (
            item.section == "ACHIEVEMENTS"
            and item.decision == "ACCEPT"
            and parent_position is not None
            and parent_modes.get(parent_position) in parent_excluded
        ):
            item = ApprovedGenerationItem(
                source_reference=item.source_reference,
                section=item.section,
                action=item.action,
                decision=item.decision,
                mode="EXCLUDED_BY_PARENT",
                priority=item.priority,
                llm_used=False,
                binding=item.binding,
            )
        dependency_compiled.append(item)
    return ApprovedGenerationSpec(
        items=tuple(dependency_compiled),
        prohibited_claims=tuple(
            claim.model_dump(mode="json") for claim in plan.prohibited_claims
        ),
        job_content=job_content,
    )


def compute_generation_input_fingerprint(
    *,
    plan: CVTransformationPlan,
    approval: dict[str, Any],
    spec: ApprovedGenerationSpec,
    provider: str,
    model: str,
    reasoning_effort: str | None,
    prompt_version: str | None = None,
) -> str:
    """Hash all semantic inputs and no secret/config transport values."""

    effective_prompt_version = prompt_version or PROMPT_VERSION
    return _fingerprint(
        {
            "generation_version": GENERATION_VERSION,
            "prompt_version": effective_prompt_version,
            "plan_version": plan.plan_version,
            "plan_fingerprint": plan.plan_fingerprint,
            "approval_id": approval["approval_id"],
            "approval_status": approval.get("status"),
            "guardrails_acknowledged": approval.get("guardrails_acknowledged"),
            "decisions": sorted(approval["decisions"], key=lambda item: item["source_reference"]),
            "compiled": [
                {
                    "source_reference": item.source_reference,
                    "section": item.section,
                    "action": item.action,
                    "decision": item.decision,
                    "mode": item.mode,
                    "priority": item.priority,
                    "llm_used": item.llm_used,
                    "namespace": item.binding.namespace,
                    "binding_section": item.binding.section,
                    "source_kind": item.binding.source_kind,
                    "source_fingerprint": item.binding.source_fingerprint,
                    "immutable_fields": item.binding.immutable_fields,
                    "allowed_operation": item.binding.allowed_operation,
                    "allowed_claim_codes": item.binding.allowed_claim_codes,
                    "source_position": item.binding.source_position,
                    "parent_experience_position": item.binding.parent_experience_position,
                    "description_position": item.binding.description_position,
                    "parent_source_fingerprint": item.binding.parent_source_fingerprint,
                    "bullet_bindings": [
                        {
                            "index": bullet.index,
                            "source_text": bullet.source_text,
                            "source_fingerprint": bullet.source_fingerprint,
                            "allowed_claim_codes": bullet.allowed_claim_codes,
                            "allowed_operation": bullet.allowed_operation,
                        }
                        for bullet in item.binding.bullet_bindings
                    ],
                }
                for item in spec.items
            ],
            "prohibited_claims": spec.prohibited_claims,
            "provider": provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
    )


def build_current_generation_context(
    *,
    plan: CVTransformationPlan,
    bindings: tuple[TransformationPlanBinding, ...],
    approval: dict[str, Any],
    config: Any,
    job_content: str = "",
) -> CurrentGenerationContext:
    """Build the exact current generation input without invoking an LLM."""

    validate_generation_context_configuration(config)
    spec = compile_approved_generation(
        plan, approval, bindings, job_content=job_content
    )
    provider = str(config.provider)
    model = str(config.model)
    fingerprint = compute_generation_input_fingerprint(
        plan=plan,
        approval=approval,
        spec=spec,
        provider=provider,
        model=model,
        reasoning_effort=config.reasoning_effort,
    )
    return CurrentGenerationContext(
        plan=plan,
        bindings=bindings,
        approval=approval,
        spec=spec,
        provider=provider,
        model=model,
        generation_input_fingerprint=fingerprint,
    )


def validate_generation_context_configuration(config: Any) -> None:
    """Validate only non-secret fields used by the semantic current fingerprint."""

    if not config.provider or not config.model:
        raise GenerationValidationError(
            "LLM_NOT_CONFIGURED", "LLM provider and model are required"
        )


def validate_llm_configuration(config: Any) -> None:
    validate_generation_context_configuration(config)
    if config.provider not in {"ollama", "openai_compatible"} and not config.api_key:
        raise GenerationValidationError("LLM_NOT_CONFIGURED", "LLM credentials are required")
    if config.provider in {"ollama", "openai_compatible"} and not config.api_base:
        raise GenerationValidationError("LLM_NOT_CONFIGURED", "Local LLM endpoint is required")


def _text_payload(item: ApprovedGenerationItem) -> Any:
    payload = item.binding.source_payload
    if item.section == "EXPERIENCE" and isinstance(payload, dict):
        return payload.get("description", [])
    if item.section == "ACHIEVEMENTS" and isinstance(payload, dict):
        return payload.get("content", "")
    return payload


def build_job_style_context(
    item: ApprovedGenerationItem,
    job_content: str,
    all_items: tuple[ApprovedGenerationItem, ...] = (),
) -> dict[str, Any]:
    """Select a short, deterministic, source-related style hint from the Job."""

    source_tokens = {
        token.casefold()
        for token in re.findall(r"[\w+#.-]{2,}", _content_text(_text_payload(item)))
    }
    fragments = [
        fragment.strip()
        for fragment in re.split(r"[\n\r]+|(?<=[.!?;])\s+", job_content)
        if fragment.strip()
    ]
    selected: list[str] = []
    other_payloads = {
        " ".join(_content_text(_text_payload(other)).casefold().split())
        for other in all_items
        if other.source_reference != item.source_reference
        and _content_text(_text_payload(other)).strip()
    }
    total = 0
    for fragment in fragments:
        tokens = {token.casefold() for token in re.findall(r"[\w+#.-]{2,}", fragment)}
        if not (tokens & source_tokens):
            continue
        value = " ".join(fragment.split())[:300]
        normalized_value = value.casefold()
        if any(other in normalized_value for other in other_payloads):
            continue
        if total + len(value) > 1000:
            value = value[: max(0, 1000 - total)].rstrip()
        if not value:
            break
        selected.append(value)
        total += len(value)
        if len(selected) == 5 or total >= 1000:
            break
    return {
        "classification": "STYLE_ONLY_NOT_FACT_SOURCE",
        "fragments": selected,
    }


def build_item_prompt(
    item: ApprovedGenerationItem,
    spec: ApprovedGenerationSpec | None = None,
) -> tuple[str, str]:
    """Build a minimal prompt containing only this binding and its permissions."""

    prohibited_claims = spec.prohibited_claims if spec else ()
    job_content = spec.job_content if spec else ""
    system = (
        "You rewrite one approved CV source item. Preserve every fact. Never infer, "
        "add, merge, quantify, or transfer facts. The job context affects style and "
        "priority only and is never a source of facts. Never create: P&L responsibility; "
        "budget responsibility; Board/C-level relations; people management; managing "
        "managers; team size; a new technical skill; a language level; a certification; "
        "or a new quantified result. EXPERIENCE must return every bullet separately "
        "with its exact index and source_fingerprint. Return exactly one JSON object "
        "with only these three top-level keys: source_reference, prompt_version, content. Do not echo "
        "the request, contract description, guardrails, or any input metadata."
    )
    payload = {
        "response_contract": {
            "only_top_level_keys": ["source_reference", "prompt_version", "content"],
            "source_reference_must_equal": item.source_reference,
            "prompt_version_must_equal": PROMPT_VERSION,
            "content_shape": (
                [
                    {
                        "index": bullet.index,
                        "source_fingerprint": bullet.source_fingerprint,
                        "text": "non_empty_rewrite_of_only_this_source_bullet",
                    }
                    for bullet in item.binding.bullet_bindings
                ]
                if item.section == "EXPERIENCE"
                else "string"
            ),
            "do_not_echo_request": True,
        },
        "action": item.action,
        "section": item.section,
        "source_reference": item.source_reference,
        "allowed_operation": item.binding.allowed_operation,
        "source_payload": (
            [
                {
                    "index": bullet.index,
                    "source_fingerprint": bullet.source_fingerprint,
                    "text": bullet.source_text,
                    "allowed_claim_codes": list(bullet.allowed_claim_codes),
                    "allowed_operation": bullet.allowed_operation,
                }
                for bullet in item.binding.bullet_bindings
            ]
            if item.section == "EXPERIENCE"
            else _text_payload(item)
        ),
        "immutable_fields": list(item.binding.immutable_fields),
        "allowed_claim_codes": list(item.binding.allowed_claim_codes),
        "prohibited_claims": list(prohibited_claims),
        "prompt_version": PROMPT_VERSION,
        "job_style_context": build_job_style_context(
            item, job_content, spec.items if spec else ()
        ),
        "job_context_rule": "STYLE_ONLY_NOT_FACT_SOURCE",
    }
    return _canonical(payload), system


def _content_text(value: str | list[str]) -> str:
    return "\n".join(value) if isinstance(value, list) else value


def detect_prohibited_claim_codes(text: str) -> tuple[str, ...]:
    """Return exact high-risk claim categories detected in one text boundary."""

    return detect_claim_codes(text)


def _validate_one_claim_boundary(
    *,
    source_text: str,
    output_text: str,
    allowed_claim_codes: tuple[str, ...],
    allowed_operation: str,
) -> None:
    for claim_code, pattern in PROHIBITED_CLAIM_BOUNDARIES:
        if not pattern.search(output_text):
            continue
        source_has_claim = bool(pattern.search(source_text))
        if claim_code == "TECHNICAL_SKILL_WITHOUT_EVIDENCE" and (
            detect_claim_values(output_text, claim_code)
            - detect_claim_values(source_text, claim_code)
        ):
            raise GenerationValidationError(
                "PROHIBITED_CLAIM_BOUNDARY_VIOLATION",
                "Output introduced a new technical claim boundary",
            )
        permission_allows_existing = (
            claim_code in allowed_claim_codes
            and allowed_operation
            in {"KEEP_EXISTING", "REPHRASE_EXISTING", "EMPHASIZE_EXISTING"}
        )
        if not source_has_claim or not permission_allows_existing:
            raise GenerationValidationError(
                "PROHIBITED_CLAIM_BOUNDARY_VIOLATION",
                f"Output introduced a prohibited claim boundary: {claim_code}",
            )


def validate_prohibited_claim_boundaries(
    item: ApprovedGenerationItem,
    output: str | list[str],
) -> None:
    """Reject newly introduced high-risk claim categories, without rewriting output."""

    _validate_one_claim_boundary(
        source_text=_content_text(_text_payload(item)),
        output_text=_content_text(output),
        allowed_claim_codes=item.binding.allowed_claim_codes,
        allowed_operation=item.binding.allowed_operation,
    )


def _normalize_transfer_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_experience_bullet(
    bullet: ExperienceBulletBinding,
    text: str,
    all_bullets: tuple[ExperienceBulletBinding, ...],
) -> None:
    source_numbers = sorted(
        match.group(0).casefold() for match in _NUMBER_RE.finditer(bullet.source_text)
    )
    output_numbers = sorted(match.group(0).casefold() for match in _NUMBER_RE.finditer(text))
    if source_numbers != output_numbers:
        raise GenerationValidationError(
            "NUMERIC_BOUNDARY_VIOLATION",
            "Numeric tokens must remain exact within each source bullet",
        )
    _validate_one_claim_boundary(
        source_text=bullet.source_text,
        output_text=text,
        allowed_claim_codes=bullet.allowed_claim_codes,
        allowed_operation=bullet.allowed_operation,
    )
    own = _normalize_transfer_text(bullet.source_text)
    output = _normalize_transfer_text(text)
    for other in all_bullets:
        if other.index == bullet.index:
            continue
        other_text = _normalize_transfer_text(other.source_text)
        if len(other_text) >= 8 and other_text not in own and other_text in output:
            raise GenerationValidationError(
                "PROHIBITED_CLAIM_BOUNDARY_VIOLATION",
                "Output merged content from a different source bullet",
            )


def validate_item_response(
    item: ApprovedGenerationItem, raw: dict[str, Any]
) -> str | list[str]:
    try:
        response = GenerationLLMItemResponse.model_validate(raw)
    except ValidationError as exc:
        raise GenerationValidationError("INVALID_LLM_RESPONSE", "LLM response contract failed") from exc
    if response.source_reference != item.source_reference:
        raise GenerationValidationError("SOURCE_REFERENCE_MISMATCH", "LLM returned a different source reference")
    if item.section == "EXPERIENCE":
        if not isinstance(response.content, list):
            raise GenerationValidationError("INVALID_LLM_RESPONSE", "EXPERIENCE content must be a bullet array")
        bullets = item.binding.bullet_bindings
        if len(response.content) != len(bullets) or len(bullets) > 40:
            raise GenerationValidationError("INVALID_LLM_RESPONSE", "EXPERIENCE bullet count changed")
        indexes = [value.index for value in response.content]
        if indexes != list(range(len(bullets))):
            raise GenerationValidationError("INVALID_LLM_RESPONSE", "EXPERIENCE indexes must be complete, unique and ordered")
        output: list[str] = []
        for value, bullet in zip(response.content, bullets, strict=True):
            if value.source_fingerprint != bullet.source_fingerprint:
                raise GenerationValidationError("INVALID_LLM_RESPONSE", "EXPERIENCE source fingerprint mismatch")
            if not value.text.strip() or len(value.text) > 1500:
                raise GenerationValidationError("INVALID_LLM_RESPONSE", "EXPERIENCE bullet text is invalid")
            if _HTML_RE.search(value.text) or "```" in value.text:
                raise GenerationValidationError("INVALID_LLM_RESPONSE", "Markup is not allowed")
            _validate_experience_bullet(bullet, value.text, bullets)
            output.append(value.text)
        return output
    if not isinstance(response.content, str):
        raise GenerationValidationError("INVALID_LLM_RESPONSE", "LLM content shape changed")
    if not response.content.strip() or len(response.content) > 5000:
        raise GenerationValidationError("INVALID_LLM_RESPONSE", "LLM content is empty or too long")
    text = response.content
    if _HTML_RE.search(text) or "```" in text:
        raise GenerationValidationError("INVALID_LLM_RESPONSE", "Markup is not allowed")
    source_numbers = sorted(match.group(0).casefold() for match in _NUMBER_RE.finditer(_content_text(_text_payload(item))))
    output_numbers = sorted(match.group(0).casefold() for match in _NUMBER_RE.finditer(text))
    if source_numbers and source_numbers != output_numbers:
        raise GenerationValidationError("NUMERIC_BOUNDARY_VIOLATION", "Numeric tokens must remain exact")
    validate_prohibited_claim_boundaries(item, response.content)
    return response.content


async def generate_item_outputs(
    spec: ApprovedGenerationSpec,
    config: Any,
    complete: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, str | list[str]]:
    outputs: dict[str, str | list[str]] = {}
    if not spec.llm_items:
        return outputs
    model_name = get_model_name(config)
    max_tokens = get_safe_max_tokens(model_name, requested=2048)
    for item in spec.llm_items:
        prompt, system = build_item_prompt(item, spec)
        complete_fn = complete or complete_json
        try:
            raw = await complete_fn(
                prompt,
                system_prompt=system,
                config=config,
                max_tokens=max_tokens,
                retries=1,
                schema_type="cv_transformation_generation",
                strict_json_envelope=True,
            )
        except LLMResponseContentError as error:
            raise GenerationValidationError(
                "INVALID_LLM_RESPONSE", "LLM response content is invalid"
            ) from error
        outputs[item.source_reference] = validate_item_response(item, raw)
    return outputs


def _experience_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("title", ""),
        value.get("company", ""),
        value.get("years", ""),
        value.get("location", ""),
        tuple(value.get("description", []) if isinstance(value.get("description"), list) else []),
    )


def assemble_draft_resume(
    processed_data: dict[str, Any],
    spec: ApprovedGenerationSpec,
    outputs: dict[str, str | list[str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply compiled items to a canonical deep copy, never to the source Resume."""

    raw_experiences = copy.deepcopy(processed_data.get("workExperience", []))
    canonical = ResumeData.model_validate(
        normalize_resume_data(copy.deepcopy(processed_data))
    ).model_dump(mode="json")
    provenance = []
    original_experiences = canonical["workExperience"]
    original_experience_fingerprints = {
        index: _fingerprint(value) for index, value in enumerate(raw_experiences)
        if isinstance(value, dict)
    }
    experience_state: dict[int, dict[str, Any] | None] = {
        index: value for index, value in enumerate(original_experiences)
    }
    experience_priorities: dict[int, str] = {
        index: "NORMAL" for index in experience_state
    }

    def experience_position(item: ApprovedGenerationItem) -> int | None:
        if item.binding.source_position is not None:
            return item.binding.source_position
        payload = item.binding.source_payload
        if isinstance(payload, dict):
            return next(
                (
                    index
                    for index, value in enumerate(original_experiences)
                    if _experience_key(value) == _experience_key(payload)
                ),
                None,
            )
        return None

    # EXPERIENCE establishes indexed bullet slots first. Immutable fields are never changed.
    for item in (value for value in spec.items if value.section == "EXPERIENCE"):
        position = experience_position(item)
        if position is None or position not in experience_state:
            continue
        if (
            item.binding.parent_source_fingerprint is not None
            and item.binding.parent_source_fingerprint
            != original_experience_fingerprints[position]
        ):
            raise GenerationValidationError("BINDING_MISMATCH", "Experience binding revision changed")
        include = item.mode not in {
            "EXCLUDED", "EXCLUDED_UNRESOLVED", "EXCLUDED_BY_PARENT", "OMITTED"
        }
        if not include:
            experience_state[position] = None
            continue
        experience = experience_state[position]
        if experience is None:
            continue
        if item.llm_used:
            content = outputs.get(item.source_reference)
            if not isinstance(content, list) or len(content) != len(experience.get("description", [])):
                raise GenerationValidationError(
                    "INVALID_LLM_RESPONSE", "Experience bullets must preserve source indexes"
                )
            experience["description"] = list(content)
        experience_priorities[position] = item.priority

    # ACHIEVEMENTS are final per-bullet overrides addressed by parent and original index.
    for item in (value for value in spec.items if value.section == "ACHIEVEMENTS"):
        payload = item.binding.source_payload
        if not isinstance(payload, dict):
            continue
        parent = item.binding.parent_experience_position
        bullet = item.binding.description_position
        if parent is None or bullet is None:
            for candidate_position, original in enumerate(original_experiences):
                if all(original.get(key, "") == payload.get(key, "") for key in ("title", "company", "years")):
                    descriptions = original.get("description", [])
                    try:
                        bullet = descriptions.index(payload.get("content", ""))
                    except ValueError:
                        continue
                    parent = candidate_position
                    break
        if parent is None or bullet is None or experience_state.get(parent) is None:
            continue
        if (
            item.binding.parent_source_fingerprint is not None
            and item.binding.parent_source_fingerprint
            != original_experience_fingerprints.get(parent)
        ):
            raise GenerationValidationError("BINDING_MISMATCH", "Achievement parent revision changed")
        experience = experience_state[parent]
        assert experience is not None
        descriptions = experience.get("description", [])
        if bullet < 0 or bullet >= len(descriptions):
            continue
        include = item.mode not in {
            "EXCLUDED", "EXCLUDED_UNRESOLVED", "EXCLUDED_BY_PARENT", "OMITTED"
        }
        if not include:
            descriptions[bullet] = None
        elif item.action in {"KEEP", "HUMAN_REVIEW"}:
            descriptions[bullet] = payload.get("content", "")
        elif item.llm_used:
            descriptions[bullet] = outputs[item.source_reference]

    for experience in experience_state.values():
        if experience is not None:
            experience["description"] = [
                value for value in experience.get("description", []) if isinstance(value, str)
            ]

    for item in (
        value for value in spec.items if value.section not in {"EXPERIENCE", "ACHIEVEMENTS"}
    ):
        payload = item.binding.source_payload
        content: Any = outputs.get(item.source_reference, _text_payload(item))
        include = item.mode not in {
            "EXCLUDED", "EXCLUDED_UNRESOLVED", "EXCLUDED_BY_PARENT", "OMITTED"
        }
        if item.section == "SUMMARY":
            if include:
                canonical["summary"] = content
            elif canonical.get("summary") == payload:
                canonical["summary"] = ""
        elif item.section == "TOOLS" and isinstance(payload, str):
            skills = canonical["additional"]["technicalSkills"]
            if include and payload not in skills:
                skills.append(payload)
            elif not include:
                canonical["additional"]["technicalSkills"] = [v for v in skills if v != payload]

    competency_items = [item for item in spec.items if item.section == "COMPETENCIES"]
    if competency_items:
        base_key = "careerPositioningCompetencies"
        key = base_key
        suffix = 2
        existing_keys = set(canonical["customSections"])
        existing_keys.update(
            meta.get("key")
            for meta in canonical["sectionMeta"]
            if isinstance(meta.get("key"), str)
        )
        existing_ids = {
            meta.get("id")
            for meta in canonical["sectionMeta"]
            if isinstance(meta.get("id"), str)
        }
        system_id = f"system:approved-plan-generation:{key}"
        while key in existing_keys or system_id in existing_ids:
            key = f"{base_key}{suffix}"
            suffix += 1
            system_id = f"system:approved-plan-generation:{key}"
        strings: list[str] = []
        for item in competency_items:
            payload = item.binding.source_payload
            if not isinstance(payload, str):
                continue
            include = item.decision == "ACCEPT" and item.mode == "EXACT_INSERT"
            if include and payload not in strings:
                strings.append(payload)
        strings = list(dict.fromkeys(strings))
        if strings:
            canonical["customSections"][key] = {
                "sectionType": "stringList",
                "items": None,
                "strings": strings,
                "text": None,
            }
            next_order = max(
                (int(meta.get("order", 0)) for meta in canonical["sectionMeta"]),
                default=-1,
            ) + 1
            canonical["sectionMeta"].append(
                {
                    "id": system_id,
                    "key": key,
                    "displayName": "Kluczowe kompetencje",
                    "sectionType": "stringList",
                    "isDefault": False,
                    "isVisible": True,
                    "order": next_order,
                }
            )

    order = {"HIGH": 0, "NORMAL": 1, "LOW": 2}
    canonical["workExperience"] = [
        experience
        for _, experience in sorted(
            (
                (position, experience)
                for position, experience in experience_state.items()
                if experience is not None
            ),
            key=lambda pair: (order[experience_priorities[pair[0]]], pair[0]),
        )
    ]
    canonical["additional"]["technicalSkills"] = list(
        dict.fromkeys(canonical["additional"]["technicalSkills"])
    )

    for item in spec.items:
        include = item.mode not in {
            "EXCLUDED", "EXCLUDED_UNRESOLVED", "EXCLUDED_BY_PARENT", "OMITTED"
        }
        content: Any = outputs.get(item.source_reference, _text_payload(item))
        if item.section == "ACHIEVEMENTS" and include and item.action in {"KEEP", "HUMAN_REVIEW"}:
            content = item.binding.source_payload.get("content", "")
        output_value = content if include else None
        provenance.append(
            {
                "source_reference": item.source_reference,
                "section": item.section,
                "action": item.action,
                "decision": item.decision,
                "mode": item.mode,
                "priority": item.priority,
                "source_fingerprint": item.binding.source_fingerprint,
                "output_fingerprint": _fingerprint(output_value) if output_value is not None else None,
                "llm_used": item.llm_used,
                "prompt_version": PROMPT_VERSION if item.llm_used else None,
                "validation_status": "NOT_RUN",
                "boundary_validation_status": "PASSED" if item.llm_used else "NOT_APPLICABLE",
            }
        )
    validated = ResumeData.model_validate(canonical).model_dump(mode="json")
    return validated, sorted(provenance, key=lambda item: item["source_reference"])
