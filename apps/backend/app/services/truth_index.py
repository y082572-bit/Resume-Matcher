"""Truth Index service.

Deterministic indexing of Truth Library facts for fast lookup and scope validation.
Maps employment entries, numeric achievements, and responsibility scales with
strict scoping rules: facts remain bound to their source employment.

Provides:
- TruthFact: Individual fact with metadata and source reference
- EmploymentTruthScope: Scope boundaries for a single employment entry
- TruthIndex: Complete indexed repository with fast lookups
- build_truth_index(): Factory function
- normalize_truth_text(): Unified text normalization for fact matching
"""

import copy
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TruthFact:
    """A single fact from the Truth Library.

    Attributes:
        fact_id: Unique identifier within its section (e.g, "dz-2:aktywnosci:0")
        section: Category (doswiadczenieZawodowe, osiagnieciaLiczbowe, etc.)
        employment_id: References employment entry id (e.g., "dz-2"), or None if not scoped
        company: Company name if employment-scoped
        role: Job title if employment-scoped
        fact_type: Type of fact (aktywnosci, wynikLiczbowy, responsibility_scale)
        original_text: The text as it appears in the library
        normalized_text: Lowercased, NFKC-normalized for matching
        status: Fact status (PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA, etc.)
        use_in_cv: Whether fact is approved for CV inclusion
        requires_approval: Whether fact needs explicit user approval
        source_reference: Path to fact origin (e.g., "dz-2:aktywnosci:3")
    """

    fact_id: str
    section: str
    employment_id: Optional[str]
    company: str
    role: str
    fact_type: str
    original_text: str
    normalized_text: str
    status: str
    use_in_cv: bool
    requires_approval: bool
    source_reference: str


@dataclass
class EmploymentTruthScope:
    """Scope boundaries for a single employment entry.

    Defines what facts are allowed within a specific employment context.
    Enforces strict scoping: facts from one employment cannot be used for another.

    Attributes:
        employment_id: Unique identifier (e.g., "dz-2")
        company: Normalized company name
        role: Normalized job title
        role_alt: Alternative role name if provided
        activities: List of activity facts scoped to this employment
        numeric_results: Numeric achievement facts (e.g., "116% output")
        responsibility_scale: Responsibility scope facts (budget ranges, team sizes)
        allowed_facts: All facts allowed in this employment context
    """

    employment_id: str
    company: str
    role: str
    role_alt: str
    activities: list[TruthFact] = field(default_factory=list)
    numeric_results: list[TruthFact] = field(default_factory=list)
    responsibility_scale: list[TruthFact] = field(default_factory=list)
    allowed_facts: list[TruthFact] = field(default_factory=list)


@dataclass
class TruthIndex:
    """Complete indexed Truth Library repository.

    Enables fast lookups, scope validation, and status-based filtering.

    Attributes:
        employment_by_id: Dict[employment_id, EmploymentTruthScope]
        employment_by_normalized_company: Dict[normalized_company_name, EmploymentTruthScope]
        globally_allowed_facts: Facts not scoped to employment (global achievements)
        blocked_rules: Global prohibition patterns
        exceptions: Explicit exceptions to rules (e.g., "CRM is allowed")
        auto_cv_statuses: Statuses automatically included in CV
        approval_statuses: Statuses requiring explicit approval
        blocked_statuses: Statuses that block CV inclusion
    """

    employment_by_id: dict[str, EmploymentTruthScope] = field(default_factory=dict)
    employment_by_normalized_company: dict[str, EmploymentTruthScope] = field(
        default_factory=dict
    )
    globally_allowed_facts: list[TruthFact] = field(default_factory=list)
    blocked_rules: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    auto_cv_statuses: list[str] = field(default_factory=list)
    approval_statuses: list[str] = field(default_factory=list)
    blocked_statuses: list[str] = field(default_factory=list)


def normalize_truth_text(value: str | None) -> str:
    """Normalize text for deterministic matching.

    Applies NFKC Unicode normalization, lowercases, normalizes dashes,
    removes excess whitespace and trailing punctuation.
    Preserves Polish diacritics, numbers, and percentages.

    Args:
        value: Raw text from Truth Library, or None

    Returns:
        Normalized text suitable for matching/indexing
    """
    if not isinstance(value, str):
        return ""

    # Apply NFKC normalization (compatibility decomposition + composition)
    normalized = unicodedata.normalize("NFKC", value)

    # Lowercase
    normalized = normalized.lower()

    # Normalize various dashes (en-dash, em-dash, etc.) to hyphen
    normalized = normalized.replace("–", "-").replace("—", "-").replace("−", "-")

    # Strip leading/trailing whitespace
    normalized = normalized.strip()

    # Remove trailing punctuation (. , ! ? ; :)
    while normalized and normalized[-1] in ".,:;!?":
        normalized = normalized[:-1]

    # Normalize multiple spaces to single space
    normalized = " ".join(normalized.split())

    return normalized


def build_truth_index(truth_library: dict[str, Any]) -> TruthIndex:
    """Build deterministic index from Truth Library.

    Indexes:
    - All employment entries (doswiadczenieZawodowe.wpisy)
    - All numeric achievements (osiagnieciaLiczbowe.wpisy)
    - Responsibility scales (skalaOdpowiedzialnosci.wpisy)
    - Global prohibitions (zakazy.bezwzgledneZakazy)
    - Exceptions (zakazy.wyjatki)
    - Auto-CV rules (reguly.statusyDoAutomatycznegoCV)
    - Approval rules (reguly.statusyWymagajaceAkceptacji)
    - Blocked rules (reguly.statusyZablokowane)

    Enforces strict employment scoping: facts from employment N stay bound to N,
    even if similar facts exist elsewhere.

    Args:
        truth_library: Loaded Truth Library dict

    Returns:
        TruthIndex with complete indexed repository

    Raises:
        KeyError: If required fields are missing
        ValueError: If fact structure is invalid
    """
    index = TruthIndex()

    # Extract rules
    try:
        reguly = truth_library["kategorie"]["reguly"]
        index.auto_cv_statuses = reguly.get("statusyDoAutomatycznegoCV", [])
        index.approval_statuses = reguly.get("statusyWymagajaceAkceptacji", [])
        index.blocked_statuses = reguly.get("statusyZablokowane", [])
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid reguly structure: {e}") from e

    # Extract zakazy (prohibitions)
    try:
        zakazy = truth_library["kategorie"].get("zakazy", {})
        index.blocked_rules = zakazy.get("bezwzgledneZakazy", [])
        index.exceptions = zakazy.get("wyjatki", [])
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid zakazy structure: {e}") from e

    # Index employment entries (doswiadczenieZawodowe)
    try:
        dz_wpisy = truth_library["kategorie"]["doswiadczenieZawodowe"].get("wpisy", [])
        for dz_idx, entry in enumerate(dz_wpisy):
            _index_employment_entry(index, entry, dz_idx)
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid doswiadczenieZawodowe structure: {e}") from e

    # Index numeric achievements (osiagnieciaLiczbowe)
    try:
        os_wpisy = truth_library["kategorie"].get("osiagnieciaLiczbowe", {}).get("wpisy", [])
        for os_idx, achievement in enumerate(os_wpisy):
            fact = _build_global_fact(achievement, os_idx, "osiagnieciaLiczbowe")
            if fact:
                index.globally_allowed_facts.append(fact)
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid osiagnieciaLiczbowe structure: {e}") from e

    # Index responsibility scales (skalaOdpowiedzialnosci)
    try:
        skala_wpisy = truth_library["kategorie"].get("skalaOdpowiedzialnosci", {}).get("wpisy", [])
        for skala_idx, scale in enumerate(skala_wpisy):
            fact = _build_global_fact(scale, skala_idx, "skalaOdpowiedzialnosci")
            if fact:
                index.globally_allowed_facts.append(fact)
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid skalaOdpowiedzialnosci structure: {e}") from e

    return index


def _index_employment_entry(
    index: TruthIndex, entry: dict[str, Any], dz_idx: int
) -> None:
    """Index a single employment entry with strict scoping.

    Extracts activities, numeric results, and responsibility scales,
    binding them permanently to this employment via employment_id + company + role.

    Args:
        index: Target TruthIndex to populate
        entry: Single entry from doswiadczenieZawodowe.wpisy
        dz_idx: 0-based index (used for fact_id generation)

    Raises:
        ValueError: If entry structure is invalid
    """
    employment_id = entry.get("id", f"dz-{dz_idx + 1}")
    company = entry.get("firma", "")
    role = entry.get("stanowisko", "")
    role_alt = entry.get("stanowiskoAlt", "")
    status = entry.get("status", "")
    use_in_cv = entry.get("uzywacWCV", False)
    requires_approval = entry.get("wymagaAkceptacji", False)

    scope = EmploymentTruthScope(
        employment_id=employment_id,
        company=company,
        role=role,
        role_alt=role_alt,
    )

    # Index activities (aktywnosci)
    activities = entry.get("aktywnosci", [])
    if isinstance(activities, list):
        for act_idx, activity_text in enumerate(activities):
            if isinstance(activity_text, str) and activity_text.strip():
                fact = TruthFact(
                    fact_id=f"{employment_id}:aktywnosci:{act_idx}",
                    section="doswiadczenieZawodowe",
                    employment_id=employment_id,
                    company=company,
                    role=role,
                    fact_type="aktywnosci",
                    original_text=activity_text,
                    normalized_text=normalize_truth_text(activity_text),
                    status=status,
                    use_in_cv=use_in_cv,
                    requires_approval=requires_approval,
                    source_reference=f"{employment_id}:aktywnosci:{act_idx}",
                )
                scope.activities.append(fact)
                scope.allowed_facts.append(fact)

    # Index numeric result (wynikLiczbowy)
    numeric_result = entry.get("wynikLiczbowy", "")
    if isinstance(numeric_result, str) and numeric_result.strip():
        fact = TruthFact(
            fact_id=f"{employment_id}:wynikLiczbowy",
            section="doswiadczenieZawodowe",
            employment_id=employment_id,
            company=company,
            role=role,
            fact_type="wynikLiczbowy",
            original_text=numeric_result,
            normalized_text=normalize_truth_text(numeric_result),
            status=status,
            use_in_cv=use_in_cv,
            requires_approval=requires_approval,
            source_reference=f"{employment_id}:wynikLiczbowy",
        )
        scope.numeric_results.append(fact)
        scope.allowed_facts.append(fact)

    # Index responsibility scale references (skalaOdpowiedzialnosci)
    responsibility_scale = entry.get("skalaOdpowiedzialnosci", "")
    if isinstance(responsibility_scale, str) and responsibility_scale.strip():
        fact = TruthFact(
            fact_id=f"{employment_id}:skalaOdpowiedzialnosci",
            section="doswiadczenieZawodowe",
            employment_id=employment_id,
            company=company,
            role=role,
            fact_type="skalaOdpowiedzialnosci",
            original_text=responsibility_scale,
            normalized_text=normalize_truth_text(responsibility_scale),
            status=status,
            use_in_cv=use_in_cv,
            requires_approval=requires_approval,
            source_reference=f"{employment_id}:skalaOdpowiedzialnosci",
        )
        scope.responsibility_scale.append(fact)
        scope.allowed_facts.append(fact)

    # Register scope in index with deep copies to prevent mutation
    index.employment_by_id[employment_id] = _deep_copy_scope(scope)
    # Also index by normalized company for quick lookup
    company_normalized = normalize_truth_text(company)
    if company_normalized:
        index.employment_by_normalized_company[company_normalized] = _deep_copy_scope(scope)


def _deep_copy_scope(scope: EmploymentTruthScope) -> EmploymentTruthScope:
    """Create a deep copy of an EmploymentTruthScope.
    
    Protects against mutations of returned copies.
    """
    return EmploymentTruthScope(
        employment_id=scope.employment_id,
        company=scope.company,
        role=scope.role,
        role_alt=scope.role_alt,
        activities=[copy.deepcopy(f) for f in scope.activities],
        numeric_results=[copy.deepcopy(f) for f in scope.numeric_results],
        responsibility_scale=[copy.deepcopy(f) for f in scope.responsibility_scale],
        allowed_facts=[copy.deepcopy(f) for f in scope.allowed_facts],
    )


def _build_global_fact(
    entry: dict[str, Any], idx: int, section: str
) -> Optional[TruthFact]:
    """Build a global (non-employment-scoped) fact.

    Used for achievements and responsibility scales that are not bound
    to a specific employment entry.

    Args:
        entry: Entry from osiagnieciaLiczbowe or skalaOdpowiedzialnosci
        idx: 0-based index in the section
        section: Section name

    Returns:
        TruthFact if entry is valid, None otherwise
    """
    # Determine the text field name for this section
    text_field = "tresc" if section == "osiagnieciaLiczbowe" else "opis"

    text = entry.get(text_field, "")
    if not isinstance(text, str) or not text.strip():
        return None

    status = entry.get("status", "")
    use_in_cv = entry.get("uzywacWCV", False)
    requires_approval = entry.get("wymagaAkceptacji", False)

    fact = TruthFact(
        fact_id=f"{section}:wpisy:{idx}",
        section=section,
        employment_id=None,  # Global facts are not employment-scoped
        company="",
        role="",
        fact_type=section,
        original_text=text,
        normalized_text=normalize_truth_text(text),
        status=status,
        use_in_cv=use_in_cv,
        requires_approval=requires_approval,
        source_reference=f"{section}:wpisy:{idx}",
    )
    return fact
