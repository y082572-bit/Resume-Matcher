"""Deterministic, item-scoped Positioning-to-CV transformation planner.

The service is pure: it receives plain inputs, calls no LLM, performs no I/O,
and never exposes Truth Library identifiers or raw Truth content.
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from app.schemas.career_positioning import CareerPositioningResponse
from app.schemas.cv_transformation_plan import (
    CVTransformationPlan,
    CVTransformationPlanItem,
    EvidencePermission,
    PROHIBITED_CLAIM_CODES,
    ProhibitedClaim,
    TransformationSourceSummary,
)
from app.services.career_positioning_report import is_entry_allowed
from app.services.cv_claim_boundaries import classify_approved_fact
from app.services.truth_index import normalize_truth_text


_TOKEN_RE = re.compile(r"[\w+#.-]{2,}", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?\s*(?:%|pln|zł|eur|usd|gbp)?", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

_SUMMARY_ACTIONS: dict[str, tuple[str, str]] = {
    "REVIEW_REQUIRED": ("HUMAN_REVIEW", "POSITIONING_REVIEW_REQUIRED"),
    "JUNIOR_ENTRY": ("KEEP", "JUNIOR_FACTUAL_BASELINE"),
    "SPECIALIST_DELIVERY": ("EMPHASIZE", "SPECIALIST_DELIVERY_FOCUS"),
    "EXPERT_AUTHORITY": ("EMPHASIZE", "EXPERT_AUTHORITY_FOCUS"),
    "MANAGER_LEADERSHIP": ("REPHRASE", "MANAGER_EVIDENCE_LED_NARRATIVE"),
    "DIRECTOR_SCALE": ("REPHRASE", "DIRECTOR_SIGNAL_SEPARATION"),
    "EXECUTIVE_ENTERPRISE": ("HUMAN_REVIEW", "EXECUTIVE_PROFILE_MATERIAL_CHANGE"),
    "CONTROLLED_FLATTENING": ("REPHRASE", "CONTROLLED_EXPERT_FLATTENING"),
    "CONTROLLED_ELEVATION": ("HUMAN_REVIEW", "CONTROLLED_ELEVATION_REVIEW"),
    "BALANCED": ("KEEP", "BALANCED_POSITIONING"),
}

_REASONS = {
    "POSITIONING_REVIEW_REQUIRED": "Poziom lub strategia pozycjonowania wymaga ręcznej decyzji.",
    "JUNIOR_FACTUAL_BASELINE": "Zachowaj faktograficzny profil wejściowy bez podnoszenia seniority.",
    "SPECIALIST_DELIVERY_FOCUS": "Wyeksponuj zweryfikowane wykonawstwo istotne dla roli specjalistycznej.",
    "EXPERT_AUTHORITY_FOCUS": "Wyeksponuj potwierdzone kompetencje eksperckie i rezultaty.",
    "MANAGER_EVIDENCE_LED_NARRATIVE": "Narracja menedżerska może opierać się tylko na oddzielnie potwierdzonych dowodach.",
    "DIRECTOR_SIGNAL_SEPARATION": "Strategia, budżet, P&L, skala, menedżerowie i Board wymagają osobnych dowodów.",
    "EXECUTIVE_PROFILE_MATERIAL_CHANGE": "Pozycjonowanie executive może materialnie zmienić profil i wymaga akceptacji.",
    "CONTROLLED_EXPERT_FLATTENING": "Zmniejsz dominację hierarchii, zachowując prawdziwe tytuły i osiągnięcia.",
    "CONTROLLED_ELEVATION_REVIEW": "Podniesienie narracji może używać wyłącznie potwierdzonych faktów i wymaga akceptacji.",
    "BALANCED_POSITIONING": "Zachowaj równowagę między doświadczeniem a wymaganiami oferty.",
    "JOB_RELEVANT_EXPERIENCE": "Doświadczenie ma deterministyczne pokrycie z treścią oferty.",
    "EXPERIENCE_CONTEXT": "Prawdziwe doświadczenie pozostaje w CV, lecz nie jest głównym sygnałem tej oferty.",
    "DUPLICATE_RESUME_ITEM": "Powtarzalny element może zostać pominięty bez utraty faktu.",
    "DIRECT_COMPETENCY": "Career Positioning potwierdza bezpośrednie dopasowanie kompetencji.",
    "TRANSFERABLE_COMPETENCY": "Career Positioning potwierdza kompetencję transferowalną.",
    "MISSING_CRITICAL_COMPETENCY": "Brakującej kompetencji krytycznej nie wolno dopisywać bez dowodu.",
    "APPROVED_NUMERIC_RESULT": "Dokładny wynik ma zatwierdzony dowód we właściwym zakresie zatrudnienia.",
    "RESUME_ACHIEVEMENT_REVIEW": "Wynik z Resume nie ma dokładnego, zatwierdzonego dowodu w tym samym zakresie.",
    "JOB_RELEVANT_TOOL": "Narzędzie ma dokładny zatwierdzony dowód i występuje w ofercie.",
    "UNVERIFIED_RELEVANT_TOOL": "Narzędzie jest istotne dla oferty, lecz wymaga dokładnego zatwierdzonego dowodu.",
    "TOOL_CONTEXT": "Narzędzie pozostaje elementem Resume, ale ma niższy priorytet dla tej oferty.",
}

_GUARDRAIL_REASONS = {
    "PNL_WITHOUT_EVIDENCE": "Nie twórz nowej odpowiedzialności P&L bez dowodu dla konkretnego elementu.",
    "BUDGET_WITHOUT_EVIDENCE": "Nie twórz nowej odpowiedzialności budżetowej bez dowodu dla konkretnego elementu.",
    "BOARD_WITHOUT_EVIDENCE": "Nie twórz nowej relacji Board/C-level bez dowodu dla konkretnego elementu.",
    "PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE": "Nie twórz nowego people management bez dowodu dla konkretnego elementu.",
    "MANAGING_MANAGERS_WITHOUT_EVIDENCE": "Nie twórz zarządzania menedżerami bez dowodu dla konkretnego elementu.",
    "TEAM_SIZE_WITHOUT_EVIDENCE": "Nie twórz liczebności zespołu bez dowodu dla konkretnego elementu.",
    "TECHNICAL_SKILL_WITHOUT_EVIDENCE": "Nie dodawaj nowej umiejętności technicznej bez dokładnego dowodu.",
    "LANGUAGE_LEVEL_WITHOUT_EVIDENCE": "Nie dodawaj poziomu języka bez dokładnego dowodu.",
    "CERTIFICATION_WITHOUT_EVIDENCE": "Nie dodawaj certyfikacji bez dokładnego dowodu.",
    "QUANTIFIED_RESULT_WITHOUT_EVIDENCE": "Nie twórz nowego wyniku liczbowego bez dokładnego, scoped dowodu.",
}


@dataclass(frozen=True)
class _ApprovedFact:
    normalized_text: str
    fact_type: str


@dataclass(frozen=True)
class _EmploymentScope:
    company: str
    role: str
    start_year: str | None
    end_year: str | None
    facts: tuple[_ApprovedFact, ...]


@dataclass(frozen=True)
class _PermissionSpec:
    claim_code: str
    evidence_strength: str = "STRONG"


@dataclass
class _ItemCandidate:
    namespace: str
    section: str
    canonical_key: str
    display_label: str
    action: str
    reason_code: str
    evidence_strength: str
    human_review_required: bool = False
    permissions: list[_PermissionSpec] = field(default_factory=list)
    source_kind: str = "resume"
    source_payload: Any = None
    immutable_fields: tuple[str, ...] = ()
    source_position: int | None = None
    parent_experience_position: int | None = None
    description_position: int | None = None
    parent_source_fingerprint: str | None = None
    bullet_bindings: tuple["ExperienceBulletBinding", ...] = ()


@dataclass(frozen=True)
class ExperienceBulletBinding:
    """Private, exact fact boundary for one EXPERIENCE description bullet."""

    index: int
    source_text: str
    source_fingerprint: str
    allowed_claim_codes: tuple[str, ...]
    allowed_operation: str


@dataclass(frozen=True)
class TransformationPlanBinding:
    """Private source material for one public plan item."""

    source_reference: str
    section: str
    namespace: str
    source_kind: str
    source_payload: Any
    source_fingerprint: str
    immutable_fields: tuple[str, ...]
    allowed_claim_codes: tuple[str, ...]
    allowed_operation: str
    source_position: int | None = None
    parent_experience_position: int | None = None
    description_position: int | None = None
    parent_source_fingerprint: str | None = None
    bullet_bindings: tuple[ExperienceBulletBinding, ...] = ()


@dataclass(frozen=True)
class CVTransformationPlanBundle:
    plan: CVTransformationPlan
    bindings: tuple[TransformationPlanBinding, ...]


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(normalize_truth_text(value))}


def _overlaps_job(value: str, job_tokens: set[str]) -> bool:
    return bool(_tokens(value) & job_tokens)


def _display_label(value: str, fallback: str) -> str:
    label = " ".join(value.split()).strip() or fallback
    return label if len(label) <= 120 else f"{label[:117].rstrip()}..."


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _iter_category_entries(
    truth_library: dict[str, Any],
) -> Iterable[tuple[str, dict[str, Any]]]:
    categories = truth_library.get("kategorie", {})
    if not isinstance(categories, dict):
        return
    rules = categories.get("reguly", {})
    for category, payload in categories.items():
        if category in {"reguly", "zakazy", "tranferowalneKompetencje"}:
            continue
        if not isinstance(payload, dict):
            continue
        entries = payload.get("wpisy", [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and is_entry_allowed(entry, rules):
                yield category, entry


def _approved_employment_scopes(
    truth_library: dict[str, Any],
) -> tuple[_EmploymentScope, ...]:
    scopes = []
    for category, entry in _iter_category_entries(truth_library):
        if category != "doswiadczenieZawodowe":
            continue
        facts = []
        for activity in entry.get("aktywnosci", []):
            if isinstance(activity, str) and normalize_truth_text(activity):
                facts.append(_ApprovedFact(normalize_truth_text(activity), "activity"))
        numeric = entry.get("wynikLiczbowy")
        if isinstance(numeric, str) and normalize_truth_text(numeric):
            facts.append(_ApprovedFact(normalize_truth_text(numeric), "numeric_result"))
        scale = entry.get("skalaOdpowiedzialnosci")
        if isinstance(scale, str) and normalize_truth_text(scale):
            facts.append(_ApprovedFact(normalize_truth_text(scale), "responsibility_scale"))
        scopes.append(
            _EmploymentScope(
                company=normalize_truth_text(entry.get("firma")),
                role=normalize_truth_text(entry.get("stanowisko")),
                start_year=_first_year(entry.get("okresOd")),
                end_year=_first_year(entry.get("okresDo")),
                facts=tuple(sorted(set(facts), key=lambda fact: (fact.fact_type, fact.normalized_text))),
            )
        )
    return tuple(
        sorted(scopes, key=lambda scope: (scope.company, scope.role, scope.start_year or "", scope.end_year or ""))
    )


def _approved_exact_technical_values(truth_library: dict[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    fields_by_category = {
        "kompetencje": ("nazwa", "nazwaAlt"),
        "narzedzia": ("nazwa", "nazwaAlt"),
        "technologie": ("nazwa", "nazwaAlt"),
    }
    for category, entry in _iter_category_entries(truth_library):
        if category == "doswiadczenieZawodowe":
            for activity in entry.get("aktywnosci", []):
                if isinstance(activity, str):
                    normalized = normalize_truth_text(activity)
                    # Only a whole, single-token fact can approve a specific tool.
                    if normalized and len(_tokens(normalized)) == 1:
                        values.add(normalized)
            continue
        for field_name in fields_by_category.get(category, ()):
            value = entry.get(field_name)
            if isinstance(value, str) and normalize_truth_text(value):
                values.add(normalize_truth_text(value))
    return frozenset(values)


def _first_year(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _YEAR_RE.search(value)
    return match.group(0) if match else None


def _resume_year_bounds(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, None
    years = _YEAR_RE.findall(value)
    if not years:
        return None, None
    return years[0], years[-1]


def _scope_matches(experience: dict[str, Any], scope: _EmploymentScope) -> bool:
    company = normalize_truth_text(experience.get("company"))
    if not company or not scope.company or company != scope.company:
        return False
    role = normalize_truth_text(experience.get("title"))
    start_year, end_year = _resume_year_bounds(experience.get("years"))
    comparisons = []
    if role and scope.role:
        if role != scope.role:
            return False
        comparisons.append(True)
    if start_year and end_year and scope.start_year and scope.end_year:
        if start_year != scope.start_year or end_year != scope.end_year:
            return False
        comparisons.append(True)
    return bool(comparisons)


def _matching_scoped_facts(
    experience: dict[str, Any],
    claim: str,
    scopes: tuple[_EmploymentScope, ...],
) -> tuple[_ApprovedFact, ...]:
    normalized = normalize_truth_text(claim)
    if not normalized:
        return ()
    matches = {
        fact
        for scope in scopes
        if _scope_matches(experience, scope)
        for fact in scope.facts
        if fact.normalized_text == normalized
    }
    return tuple(sorted(matches, key=lambda fact: (fact.fact_type, fact.normalized_text)))


def _operation_for_action(action: str) -> str:
    if action == "EMPHASIZE":
        return "EMPHASIZE_EXISTING"
    if action == "REPHRASE":
        return "REPHRASE_EXISTING"
    return "KEEP_EXISTING"


def _materialize(
    candidates: list[_ItemCandidate],
) -> tuple[
    list[CVTransformationPlanItem],
    list[EvidencePermission],
    list[TransformationPlanBinding],
]:
    items = []
    permissions = []
    bindings = []
    for ordinal, candidate in enumerate(
        sorted(candidates, key=lambda item: (item.canonical_key, item.display_label)),
        start=1,
    ):
        source_reference = (
            f"{candidate.namespace}:{candidate.section.lower()}:{ordinal:04d}"
        )
        items.append(
            CVTransformationPlanItem(
                action=candidate.action,
                section=candidate.section,
                source_reference=source_reference,
                display_label=candidate.display_label,
                reason_code=candidate.reason_code,
                reason=_REASONS[candidate.reason_code],
                evidence_strength=candidate.evidence_strength,
                human_review_required=candidate.human_review_required,
            )
        )
        for permission in sorted(
            set(candidate.permissions), key=lambda item: item.claim_code
        ):
            permissions.append(
                EvidencePermission(
                    claim_code=permission.claim_code,
                    source_reference=source_reference,
                    evidence_strength=permission.evidence_strength,
                    allowed_operation=_operation_for_action(candidate.action),
                    human_review_required=False,
                )
            )
        bindings.append(
            TransformationPlanBinding(
                source_reference=source_reference,
                section=candidate.section,
                namespace=candidate.namespace,
                source_kind=candidate.source_kind,
                source_payload=candidate.source_payload,
                source_fingerprint=_sha256_canonical(candidate.source_payload),
                immutable_fields=candidate.immutable_fields,
                allowed_claim_codes=tuple(
                    sorted(permission.claim_code for permission in set(candidate.permissions))
                ),
                allowed_operation=_operation_for_action(candidate.action),
                source_position=candidate.source_position,
                parent_experience_position=candidate.parent_experience_position,
                description_position=candidate.description_position,
                parent_source_fingerprint=candidate.parent_source_fingerprint,
                bullet_bindings=candidate.bullet_bindings,
            )
        )
    return items, permissions, bindings


def _summary_candidates(
    resume: dict[str, Any], strategy: str, report_review: bool
) -> list[_ItemCandidate]:
    summary = resume.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return []
    action, reason_code = _SUMMARY_ACTIONS[strategy]
    review = action == "HUMAN_REVIEW" or report_review
    if report_review and action not in {"HUMAN_REVIEW", "KEEP"}:
        action, reason_code = "HUMAN_REVIEW", "POSITIONING_REVIEW_REQUIRED"
    return [
        _ItemCandidate(
            namespace="resume",
            section="SUMMARY",
            canonical_key=normalize_truth_text(summary),
            display_label="Podsumowanie zawodowe",
            action=action,
            reason_code=reason_code,
            evidence_strength="WEAK",
            human_review_required=review,
            source_kind="resume_summary",
            source_payload=summary,
        )
    ]


def _experience_candidates(
    resume: dict[str, Any],
    job_tokens: set[str],
    strategy: str,
    scopes: tuple[_EmploymentScope, ...],
) -> list[_ItemCandidate]:
    raw = resume.get("workExperience", [])
    if not isinstance(raw, list):
        return []
    semantic_items = []
    for source_position, experience in enumerate(raw):
        if not isinstance(experience, dict):
            continue
        semantic = {
            "title": experience.get("title", ""),
            "company": experience.get("company", ""),
            "years": experience.get("years", ""),
            "location": experience.get("location", ""),
            "description": sorted(
                value
                for value in experience.get("description", [])
                if isinstance(value, str)
            ),
        }
        semantic_items.append((semantic, experience, source_position))
    semantic_items.sort(key=lambda item: _canonical(item[0]))

    candidates = []
    occurrences: dict[str, int] = {}
    for semantic, experience, source_position in semantic_items:
        base_key = _canonical(semantic)
        duplicate_ordinal = occurrences.get(base_key, 0)
        occurrences[base_key] = duplicate_ordinal + 1
        text = " ".join(
            [
                str(semantic["title"]),
                str(semantic["company"]),
                " ".join(semantic["description"]),
            ]
        )
        relevant = _overlaps_job(text, job_tokens)
        action = "EMPHASIZE" if relevant else "KEEP"
        reason_code = "JOB_RELEVANT_EXPERIENCE" if relevant else "EXPERIENCE_CONTEXT"
        if strategy == "CONTROLLED_FLATTENING" and re.search(
            r"\b(?:director|dyrektor|executive|strateg\w*)\b",
            normalize_truth_text(text),
        ):
            action, reason_code = "DEEMPHASIZE", "EXPERIENCE_CONTEXT"

        permission_codes = set()
        for description in semantic["description"]:
            for fact in _matching_scoped_facts(experience, description, scopes):
                permission_codes.update(
                    classify_approved_fact(fact.normalized_text, fact.fact_type)
                )
        if duplicate_ordinal:
            action, reason_code = "OMIT", "DUPLICATE_RESUME_ITEM"
            permission_codes.clear()

        raw_descriptions = experience.get("description", [])
        bullet_bindings = tuple(
            ExperienceBulletBinding(
                index=index,
                source_text=description,
                source_fingerprint=_sha256_canonical(description),
                allowed_claim_codes=tuple(
                    sorted(
                        {
                            code
                            for fact in _matching_scoped_facts(
                                experience, description, scopes
                            )
                            for code in classify_approved_fact(
                                fact.normalized_text, fact.fact_type
                            )
                        }
                    )
                ),
                allowed_operation=_operation_for_action(action),
            )
            for index, description in enumerate(raw_descriptions)
            if isinstance(description, str)
        ) if isinstance(raw_descriptions, list) else ()

        title = str(semantic["title"]).strip()
        company = str(semantic["company"]).strip()
        label = " — ".join(part for part in (title, company) if part)
        candidates.append(
            _ItemCandidate(
                namespace="resume",
                section="EXPERIENCE",
                canonical_key=f"{base_key}|duplicate:{duplicate_ordinal:04d}",
                display_label=_display_label(label, "Doświadczenie zawodowe"),
                action=action,
                reason_code=reason_code,
                evidence_strength="STRONG" if permission_codes else "MODERATE",
                permissions=[_PermissionSpec(code) for code in sorted(permission_codes)],
                source_kind="resume_experience",
                source_payload=experience,
                immutable_fields=("title", "company", "years", "location"),
                source_position=source_position,
                parent_source_fingerprint=_sha256_canonical(experience),
                bullet_bindings=bullet_bindings,
            )
        )
    return candidates


def _competency_candidates(report: CareerPositioningResponse) -> list[_ItemCandidate]:
    candidates = []
    groups = (
        (report.competencies.direct_matches, "EMPHASIZE", "DIRECT_COMPETENCY", "STRONG", False),
        (report.competencies.transferable_matches, "EMPHASIZE", "TRANSFERABLE_COMPETENCY", "MODERATE", False),
        (report.competencies.missing_critical, "HUMAN_REVIEW", "MISSING_CRITICAL_COMPETENCY", "NONE", True),
    )
    for items, action, reason_code, strength, review in groups:
        for competency in items:
            name = normalize_truth_text(competency.name)
            permissions = (
                []
                if review
                else [_PermissionSpec("COMPETENCY_EVIDENCE", strength)]
            )
            candidates.append(
                _ItemCandidate(
                    namespace="positioning",
                    section="COMPETENCIES",
                    canonical_key=f"{reason_code}|{name}",
                    display_label=_display_label(competency.name, "Kompetencja"),
                    action=action,
                    reason_code=reason_code,
                    evidence_strength=strength,
                    human_review_required=review,
                    permissions=permissions,
                    source_kind="positioning_competency",
                    source_payload=competency.name,
                )
            )
    return candidates


def _achievement_candidates(
    resume: dict[str, Any], scopes: tuple[_EmploymentScope, ...]
) -> list[_ItemCandidate]:
    candidates: list[_ItemCandidate] = []
    experiences = resume.get("workExperience", [])
    if not isinstance(experiences, list):
        return []
    for parent_position, experience in enumerate(experiences):
        if not isinstance(experience, dict):
            continue
        descriptions = experience.get("description", [])
        if not isinstance(descriptions, list):
            continue
        parent_fingerprint = _sha256_canonical(experience)
        for description_position, claim in enumerate(descriptions):
            if not isinstance(claim, str) or not _NUMBER_RE.search(claim):
                continue
            matching = _matching_scoped_facts(experience, claim, scopes)
            approved = any(fact.fact_type == "numeric_result" for fact in matching)
            key = _canonical(
                {
                    "company": normalize_truth_text(experience.get("company")),
                    "role": normalize_truth_text(experience.get("title")),
                    "years": _resume_year_bounds(experience.get("years")),
                    "claim": normalize_truth_text(claim),
                    "parent_position": parent_position,
                    "description_position": description_position,
                }
            )
            candidates.append(_ItemCandidate(
                namespace="resume",
                section="ACHIEVEMENTS",
                canonical_key=key,
                display_label=_display_label(claim, "Osiągnięcie liczbowe"),
                action="EMPHASIZE" if approved else "HUMAN_REVIEW",
                reason_code=(
                    "APPROVED_NUMERIC_RESULT"
                    if approved
                    else "RESUME_ACHIEVEMENT_REVIEW"
                ),
                evidence_strength="STRONG" if approved else "WEAK",
                human_review_required=not approved,
                permissions=(
                    [_PermissionSpec("QUANTIFIED_RESULT_WITHOUT_EVIDENCE")]
                    if approved
                    else []
                ),
                source_kind="resume_achievement",
                source_payload={
                    "content": claim,
                    "title": experience.get("title", ""),
                    "company": experience.get("company", ""),
                    "years": experience.get("years", ""),
                    "location": experience.get("location", ""),
                },
                immutable_fields=("title", "company", "years", "location"),
                source_position=description_position,
                parent_experience_position=parent_position,
                description_position=description_position,
                parent_source_fingerprint=parent_fingerprint,
            ))
    return candidates


def _tool_candidates(
    resume: dict[str, Any],
    job_tokens: set[str],
    approved_tools: frozenset[str],
) -> list[_ItemCandidate]:
    additional = resume.get("additional", {})
    tools = additional.get("technicalSkills", []) if isinstance(additional, dict) else []
    if not isinstance(tools, list):
        return []
    candidates = []
    normalized_tools = {
        normalize_truth_text(tool): tool
        for tool in tools
        if isinstance(tool, str) and normalize_truth_text(tool)
    }
    for normalized, original in sorted(normalized_tools.items()):
        relevant = _overlaps_job(original, job_tokens)
        approved = normalized in approved_tools
        if relevant and approved:
            action, reason_code, review = "EMPHASIZE", "JOB_RELEVANT_TOOL", False
        elif relevant:
            action, reason_code, review = (
                "HUMAN_REVIEW",
                "UNVERIFIED_RELEVANT_TOOL",
                True,
            )
        else:
            action, reason_code, review = "KEEP", "TOOL_CONTEXT", False
        candidates.append(
            _ItemCandidate(
                namespace="resume",
                section="TOOLS",
                canonical_key=normalized,
                display_label=_display_label(original, "Narzędzie"),
                action=action,
                reason_code=reason_code,
                evidence_strength="STRONG" if approved else "WEAK",
                human_review_required=review,
                permissions=(
                    [_PermissionSpec("TECHNICAL_SKILL_WITHOUT_EVIDENCE")]
                    if approved
                    else []
                ),
                source_kind="resume_tool",
                source_payload=original,
            )
        )
    return candidates


def _universal_guardrails() -> list[ProhibitedClaim]:
    return [
        ProhibitedClaim(code=code, reason=_GUARDRAIL_REASONS[code])
        for code in PROHIBITED_CLAIM_CODES
    ]


def _job_requirements_count(job_content: str) -> int:
    lines = [line.strip() for line in job_content.splitlines() if line.strip()]
    return len(lines) if lines else (1 if job_content.strip() else 0)


def _sha256_canonical(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_source_value(value: Any) -> Any:
    """Normalize source collections whose ordering does not change plan semantics."""

    if isinstance(value, dict):
        return {
            key: _canonical_source_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        normalized = [_canonical_source_value(item) for item in value]
        return sorted(normalized, key=_canonical)
    return value


def compute_plan_fingerprint(
    plan: CVTransformationPlan,
    *,
    resume: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    truth_library: dict[str, Any] | None = None,
) -> str:
    """Hash the public plan and opaque source revisions, excluding generated_at."""

    public_plan = plan.model_dump(
        mode="json",
        exclude={"generated_at", "plan_fingerprint"},
    )
    source_revisions = {
        "resume": (
            _sha256_canonical(_canonical_source_value(resume))
            if resume is not None
            else None
        ),
        "job": (
            _sha256_canonical(_canonical_source_value(job))
            if job is not None
            else None
        ),
        "truth_library": (
            _sha256_canonical(_canonical_source_value(truth_library))
            if truth_library is not None
            else None
        ),
    }
    return _sha256_canonical(
        {"public_plan": public_plan, "source_revisions": source_revisions}
    )


def build_cv_transformation_plan_bundle(
    *,
    resume_id: str,
    job_id: str,
    resume: dict[str, Any],
    job: dict[str, Any],
    truth_library: dict[str, Any],
    career_report: CareerPositioningResponse,
    now: datetime,
) -> CVTransformationPlanBundle:
    """Build public plan and private bindings from the exact same candidates."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    processed = resume.get("processed_data") or {}
    job_content = job.get("content") if isinstance(job.get("content"), str) else ""
    job_tokens = _tokens(job_content)
    approved_entries = list(_iter_category_entries(truth_library))
    scopes = _approved_employment_scopes(truth_library)
    approved_tools = _approved_exact_technical_values(truth_library)
    strategy = career_report.positioning.positioning_strategy

    summary, summary_permissions, summary_bindings = _materialize(
        _summary_candidates(
            processed, strategy, career_report.positioning.requires_human_review
        )
    )
    experience, experience_permissions, experience_bindings = _materialize(
        _experience_candidates(processed, job_tokens, strategy, scopes)
    )
    competencies, competency_permissions, competency_bindings = _materialize(
        _competency_candidates(career_report)
    )
    achievements, achievement_permissions, achievement_bindings = _materialize(
        _achievement_candidates(processed, scopes)
    )
    tools, tool_permissions, tool_bindings = _materialize(
        _tool_candidates(processed, job_tokens, approved_tools)
    )
    permissions = sorted(
        summary_permissions
        + experience_permissions
        + competency_permissions
        + achievement_permissions
        + tool_permissions,
        key=lambda item: (
            item.claim_code,
            item.source_reference,
            item.allowed_operation,
        ),
    )

    limitations = sorted(set(career_report.limitations.messages))
    if not processed:
        limitations.append("Aktualne Resume nie zawiera ustrukturyzowanych danych.")
    limitations = sorted(set(limitations))
    plan_items = summary + experience + competencies + achievements + tools
    requires_review = (
        career_report.positioning.requires_human_review
        or any(item.human_review_required for item in plan_items)
        or strategy
        in {"REVIEW_REQUIRED", "CONTROLLED_ELEVATION", "EXECUTIVE_ENTERPRISE"}
    )
    plan = CVTransformationPlan(
        plan_fingerprint="0" * 64,
        resume_id=resume_id,
        job_id=job_id,
        generated_at=now,
        detected_job_level=career_report.positioning.detected_job_level,
        candidate_profile_level=career_report.positioning.candidate_profile_level,
        positioning_strategy=strategy,
        requires_human_review=requires_review,
        summary_plan=summary,
        experience_plan=experience,
        competencies_plan=competencies,
        achievements_plan=achievements,
        tools_plan=tools,
        evidence_permissions=permissions,
        prohibited_claims=_universal_guardrails(),
        limitations=limitations,
        source_summary=TransformationSourceSummary(
            approved_truth_items_count=len(approved_entries),
            resume_items_count=len(summary + experience + achievements + tools),
            job_requirements_count=_job_requirements_count(job_content),
        ),
    )
    final_plan = plan.model_copy(
        update={
            "plan_fingerprint": compute_plan_fingerprint(
                plan,
                resume=resume,
                job=job,
                truth_library=truth_library,
            )
        }
    )
    bindings = tuple(
        sorted(
            summary_bindings
            + experience_bindings
            + competency_bindings
            + achievement_bindings
            + tool_bindings,
            key=lambda item: item.source_reference,
        )
    )
    return CVTransformationPlanBundle(plan=final_plan, bindings=bindings)


def build_cv_transformation_plan(
    *,
    resume_id: str,
    job_id: str,
    resume: dict[str, Any],
    job: dict[str, Any],
    truth_library: dict[str, Any],
    career_report: CareerPositioningResponse,
    now: datetime,
) -> CVTransformationPlan:
    """Backward-compatible public-plan builder."""

    return build_cv_transformation_plan_bundle(
        resume_id=resume_id,
        job_id=job_id,
        resume=resume,
        job=job,
        truth_library=truth_library,
        career_report=career_report,
        now=now,
    ).plan
