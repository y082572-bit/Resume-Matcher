"""Career Positioning Engine v1.

Classifies job levels and evaluates candidate career positioning against target requirements.
"""

from dataclasses import dataclass
from enum import Enum, IntEnum
import math
import re
import unicodedata
from typing import Optional

# Named constants for weights and thresholds
WEIGHT_TITLE = 2.0
WEIGHT_DESCRIPTION = 1.0

# Base level weights
WEIGHT_EXECUTIVE = 7.0
WEIGHT_DIRECTOR = 6.0
WEIGHT_MANAGER = 5.0
WEIGHT_EXPERT = 4.0
WEIGHT_SPECIALIST = 1.0
WEIGHT_JUNIOR = 3.0

WEIGHT_AMBIGUOUS_MANAGER = 2.0
WEIGHT_AMBIGUOUS_LEAD = 2.0
WEIGHT_EXCLUDED_MANAGER = 1.0

CONF_BASE = 0.8
CONF_STRENGTH_THRESHOLD = 3.0
CONF_STRENGTH_BOOST = 1.1
CONF_STRENGTH_PENALTY = 0.8
CONF_MARGIN_TIGHT = 0.3
CONF_MARGIN_PENALTY = 0.8
CONF_BOTH_SOURCES_BOOST = 1.1
CONF_SINGLE_SOURCE_PENALTY = 0.9
CONF_EXTREME_CONFLICT_PENALTY = 0.6
CONF_AMBIGUOUS_MANAGER_PENALTY = 0.85
CONF_AMBIGUOUS_LEAD_PENALTY = 0.75

CONF_MAX = 1.0
CONF_MIN = 0.0

# Immutable list of negations (Rule 9)
_PEOPLE_MANAGEMENT_NEGATIONS = (
    "no direct reports",
    "no people management",
    "without people management",
    "individual contributor",
    "bez zarządzania zespołem",
    "brak podległych pracowników",
    "brak odpowiedzialności za pracowników",
    "rola indywidualna",
    "rola ekspercka bez zespołu"
)


class RoleLevel(IntEnum):
    UNKNOWN = 0
    JUNIOR = 1
    SPECIALIST = 2
    EXPERT = 3
    MANAGER = 4
    DIRECTOR = 5
    EXECUTIVE = 6


class RiskLevel(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PositioningStrategy(str, Enum):
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    JUNIOR_ENTRY = "JUNIOR_ENTRY"
    SPECIALIST_DELIVERY = "SPECIALIST_DELIVERY"
    EXPERT_AUTHORITY = "EXPERT_AUTHORITY"
    MANAGER_LEADERSHIP = "MANAGER_LEADERSHIP"
    DIRECTOR_SCALE = "DIRECTOR_SCALE"
    EXECUTIVE_ENTERPRISE = "EXECUTIVE_ENTERPRISE"
    CONTROLLED_FLATTENING = "CONTROLLED_FLATTENING"
    CONTROLLED_ELEVATION = "CONTROLLED_ELEVATION"
    BALANCED = "BALANCED"


class TitleTreatment(str, Enum):
    KEEP = "KEEP"
    CONTEXTUALIZE = "CONTEXTUALIZE"
    DEEMPHASIZE = "DEEMPHASIZE"
    REVIEW = "REVIEW"


def _is_finite_number(x) -> bool:
    """Helper to check if x is a float or int, and is finite (not nan/inf, not bool)."""
    if isinstance(x, bool):
        return False
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(x)


@dataclass(frozen=True)
class CandidatePositioningProfile:
    candidate_level: RoleLevel
    total_years: float
    management_years: float
    managed_people_max: int
    managed_units_max: int
    has_strategy_responsibility: bool
    has_budget_responsibility: bool
    has_sales_result_responsibility: bool
    evidence_references: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.candidate_level, RoleLevel):
            raise TypeError("candidate_level must be RoleLevel")
        if not _is_finite_number(self.total_years):
            raise TypeError("total_years must be a finite number")
        if not _is_finite_number(self.management_years):
            raise TypeError("management_years must be a finite number")
        if self.total_years < 0:
            raise ValueError("total_years cannot be negative")
        if self.management_years < 0:
            raise ValueError("management_years cannot be negative")
        if self.management_years > self.total_years:
            raise ValueError("management_years cannot be greater than total_years")

        # Check that max people and units are int and NOT bool
        if isinstance(self.managed_people_max, bool) or not isinstance(self.managed_people_max, int):
            raise TypeError("managed_people_max must be an int (not bool)")
        if isinstance(self.managed_units_max, bool) or not isinstance(self.managed_units_max, int):
            raise TypeError("managed_units_max must be an int (not bool)")

        if self.managed_people_max < 0:
            raise ValueError("managed_people_max cannot be negative")
        if self.managed_units_max < 0:
            raise ValueError("managed_units_max cannot be negative")

        # Explicit bool validations (Rule 8)
        if not isinstance(self.has_strategy_responsibility, bool):
            raise TypeError("has_strategy_responsibility must be a bool")
        if not isinstance(self.has_budget_responsibility, bool):
            raise TypeError("has_budget_responsibility must be a bool")
        if not isinstance(self.has_sales_result_responsibility, bool):
            raise TypeError("has_sales_result_responsibility must be a bool")

        if not isinstance(self.evidence_references, tuple):
            raise TypeError("evidence_references must be a tuple")
        for ref in self.evidence_references:
            if not isinstance(ref, str):
                raise TypeError("evidence_references must contain only strings")
            if not ref.strip():
                raise ValueError("evidence_references cannot contain empty or whitespace strings")


@dataclass(frozen=True)
class JobPositioningInput:
    title: str
    description: str
    required_years: Optional[float] = None
    explicit_people_management: Optional[bool] = None

    def __post_init__(self):
        if not isinstance(self.title, str):
            raise TypeError("title must be a string")
        if not isinstance(self.description, str):
            raise TypeError("description must be a string")
        if not self.title or not self.title.strip():
            raise ValueError("title cannot be empty or whitespace only")
        if self.required_years is not None:
            if not _is_finite_number(self.required_years):
                raise TypeError("required_years must be a finite number")
            if self.required_years < 0:
                raise ValueError("required_years cannot be negative")
        if self.explicit_people_management is not None:
            if not isinstance(self.explicit_people_management, bool):
                raise TypeError("explicit_people_management must be a bool or None")


@dataclass(frozen=True)
class RoleSignal:
    level: RoleLevel
    source: str
    phrase: str
    weight: float


@dataclass(frozen=True)
class PositioningResult:
    job_level: RoleLevel
    candidate_level: RoleLevel
    level_gap: int
    overqualification_risk: RiskLevel
    underqualification_risk: RiskLevel
    strategy: PositioningStrategy
    title_treatment: TitleTreatment
    confidence: float
    reasons: tuple[str, ...]
    emphasize: tuple[str, ...]
    deemphasize: tuple[str, ...]
    matched_signals: tuple[RoleSignal, ...]
    requires_human_review: bool


def _normalize_text(text: str) -> str:
    """Normalize letter case, whitespace, dashes, and punctuation."""
    if not isinstance(text, str):
        return ""
    # Normalize to Compatibility Decomposition
    normalized = unicodedata.normalize("NFKC", text)
    # Lowercase
    normalized = normalized.lower()
    # Replace all kinds of connectors and hyphens with space (Rule 5)
    normalized = normalized.replace("-", " ").replace("–", " ").replace("—", " ").replace("−", " ")
    # Clean non-alphanumeric, keep spaces
    cleaned = re.sub(r"[^\w\s]", " ", normalized)
    return " ".join(cleaned.split())


def _remove_others_roles(text: str) -> str:
    """Remove references to other people's roles to prevent accidental matching."""
    patterns = [
        r"\breports\s+to\s+(?:the\s+)?(?:ceo|director|manager|vice\s+president|president|executive|board)\b",
        r"\bsupports\s+(?:the\s+)?(?:director|ceo|manager|board|executive)\b",
        r"\bworks\s+with\s+(?:the\s+)?(?:vice\s+president|ceo|director|manager|prezes|zarząd|board)\b",
        r"\bwspółpraca\s+z\s+(?:prezesem|zarządem|dyrektorem|kierownikiem|zarządem)\b",
        r"\braportowanie\s+do\s+(?:dyrektora|prezesa|zarządu|kierownika)\b",
        r"\bwsparcie\s+(?:zarządu|prezesa|dyrektora|kierownika)\b"
    ]
    for pattern in patterns:
        text = re.sub(pattern, " ", text)
    return text


def _has_people_management_negation(description: str) -> bool:
    """Centralized check for negations in description."""
    norm_desc = _normalize_text(description)
    for neg in _PEOPLE_MANAGEMENT_NEGATIONS:
        if _normalize_text(neg) in norm_desc:
            return True
    return False


def _has_confirmed_people_management(matched_signals: list[RoleSignal], explicit_people_management: Optional[bool]) -> bool:
    """Centralized check for confirmed people management."""
    if explicit_people_management is True:
        return True
    for sig in matched_signals:
        if sig.source == "description" and sig.level == RoleLevel.MANAGER:
            return True
        if sig.source == "title" and sig.phrase in ["kierownik zespołu", "kierownik", "kierowniczka", "team leader", "lider zespołu", "lead_with_team", "manager_with_team"]:
            return True
        if sig.source == "explicit_people_management":
            return True
    return False


def _has_strong_classification_conflict(matched_signals: list[RoleSignal], job: JobPositioningInput) -> bool:
    """Centralized check for strong conflicts."""
    # Junior vs High conflict
    has_junior_desc = any(w in ["junior", "staż", "stażu", "praktyka", "praktykant", "asystent", "asystentka", "assistant", "młodszy", "młodsza", "asystencka"] for w in _normalize_text(job.description).split())
    has_junior = any(s.level == RoleLevel.JUNIOR for s in matched_signals) or has_junior_desc
    has_high = any(s.level in {RoleLevel.DIRECTOR, RoleLevel.EXECUTIVE} for s in matched_signals)
    if has_junior and has_high:
        return True

    # Explicit manager title or description + explicit_people_management=False or negation (Rule 2)
    has_negation = _has_people_management_negation(job.description)
    has_explicit_mgmt = False
    for s in matched_signals:
        if s.source == "title" and s.phrase in ["kierownik zespołu", "team leader", "lider zespołu"]:
            has_explicit_mgmt = True
        if s.source == "description" and s.level == RoleLevel.MANAGER:
            has_explicit_mgmt = True

    if has_explicit_mgmt and (has_negation or job.explicit_people_management is False):
        return True

    return False


def _check_desc_for_people_mgmt(description: str) -> bool:
    """Check description for confirmed management responsibility."""
    if _has_people_management_negation(description):
        return False
    norm_desc = _normalize_text(description)
    mgr_patterns = [
        r"\bzarządzani[ea]\s+(?:zespołem|ludźmi|pracownik(?:i|ów)?)\b",
        r"\bkierowani[ea]\s+zespołem\b",
        r"\bpeople\s+management\b",
        r"\bdirect\s+reports\b",
        r"\bodpowiedzialność\s+za\s+pracownik(?:i|ów)?\b",
        r"\bodpowiedzialności\s+za\s+pracownik(?:i|ów)?\b"
    ]
    for pattern in mgr_patterns:
        if re.search(pattern, norm_desc):
            return True
    return False


def _find_title_signals(title: str, has_confirmed_people_mgmt: bool) -> list[RoleSignal]:
    signals = []
    norm_title = _normalize_text(title)
    if not norm_title:
        return signals

    # Helper function to match and consume phrases
    def consume(phrase_pattern, level, weight, phrase_name):
        nonlocal norm_title
        matches = re.findall(phrase_pattern, norm_title)
        if matches:
            for _ in matches:
                signals.append(RoleSignal(level=level, source="title", phrase=phrase_name, weight=weight))
            # Remove from norm_title to avoid matching sub-tokens
            norm_title = re.sub(phrase_pattern, " ", norm_title)
            norm_title = " ".join(norm_title.split())

    # 1. Exclusions (Assistants to executives) - MUST BE FIRST (Rule 5)
    consume(r"\bexecutive assistant to the ceo\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "executive assistant to ceo")
    consume(r"\bexecutive assistant to ceo\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "executive assistant to ceo")
    consume(r"\bassistant to the ceo\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "assistant to ceo")
    consume(r"\bassistant to ceo\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "assistant to ceo")
    consume(r"\bassistant to the board\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "assistant to board")
    consume(r"\bexecutive assistant\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "executive assistant")
    consume(r"\bceo assistant\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "ceo assistant")
    consume(r"\basystentka ceo\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystentka ceo")
    consume(r"\basystent ceo\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystent ceo")
    consume(r"\basystentka prezesa\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystentka prezesa")
    consume(r"\basystent prezesa\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystent prezesa")
    consume(r"\basystentka zarządu\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystentka zarządu")
    consume(r"\basystent zarządu\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystent zarządu")
    consume(r"\basystentka dyrektora\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystentka dyrektora")
    consume(r"\basystent dyrektora\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "asystent dyrektora")

    # 2. Excluded generic managers (Account, PM, KAM, etc.)
    consume(r"\bsenior key account manager\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "senior key account manager")
    consume(r"\bkey account manager\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "key account manager")
    consume(r"\bsenior account manager\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "senior account manager")
    consume(r"\baccount manager\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "account manager")
    consume(r"\bproduct manager\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "product manager")
    consume(r"\bbrand manager\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "brand manager")
    consume(r"\brelationship manager\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "relationship manager")

    # Head Hunter / Headhunter (Rule 4)
    consume(r"\bhead hunter\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "head hunter")
    consume(r"\bheadhunter\b", RoleLevel.SPECIALIST, WEIGHT_SPECIALIST, "headhunter")

    # 3. Ambiguous manager titles & Project/Program Managers (Rule 7)
    if has_confirmed_people_mgmt:
        consume(r"\bstrategic partnership manager\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "strategic partnership manager")
        consume(r"\bbusiness development manager\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "business development manager")
        consume(r"\bchannel manager\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "channel manager")
        consume(r"\bsales manager\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "sales manager")
        consume(r"\bkierownik projektu\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "kierownik projektu")
        consume(r"\bproject manager\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "project manager")
        consume(r"\bprogram manager\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "program manager")
    else:
        consume(r"\bstrategic partnership manager\b", RoleLevel.MANAGER, WEIGHT_AMBIGUOUS_MANAGER, "strategic partnership manager")
        consume(r"\bbusiness development manager\b", RoleLevel.MANAGER, WEIGHT_AMBIGUOUS_MANAGER, "business development manager")
        consume(r"\bchannel manager\b", RoleLevel.MANAGER, WEIGHT_AMBIGUOUS_MANAGER, "channel manager")
        consume(r"\bsales manager\b", RoleLevel.MANAGER, WEIGHT_AMBIGUOUS_MANAGER, "sales manager")
        consume(r"\bkierownik projektu\b", RoleLevel.EXPERT, 2.0, "project_manager_unresolved")
        consume(r"\bproject manager\b", RoleLevel.EXPERT, 2.0, "project_manager_unresolved")
        consume(r"\bprogram manager\b", RoleLevel.EXPERT, 2.0, "project_manager_unresolved")

    # 4. Multi-word titles
    consume(r"\bhead of sales\b", RoleLevel.DIRECTOR, WEIGHT_DIRECTOR, "head of sales")
    consume(r"\bhead of\b", RoleLevel.DIRECTOR, WEIGHT_DIRECTOR, "head of")

    # VP Sales/Marketing/Operations (Rule 6)
    consume(r"\bvp sales\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "vp sales")
    consume(r"\bvp marketing\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "vp marketing")
    consume(r"\bvp operations\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "vp operations")
    consume(r"\bvp\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "vp")

    consume(r"\bvice president\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "vice president")
    consume(r"\bczłonek zarządu\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "członek zarządu")
    consume(r"\bboard member\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "board member")
    consume(r"\bkierownik zespołu\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "kierownik zespołu")

    # Team Leader / Lider zespołu (Rule 6)
    consume(r"\bteam leader\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "team leader")
    consume(r"\blider zespołu\b", RoleLevel.MANAGER, WEIGHT_MANAGER, "lider zespołu")

    consume(r"\bmłodszy specjalista\b", RoleLevel.JUNIOR, WEIGHT_JUNIOR, "młodszy specjalista")
    consume(r"\btrener sprzedaży\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "trener sprzedaży")
    consume(r"\bsales trainer\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "sales trainer")
    consume(r"\bc level\b", RoleLevel.EXECUTIVE, WEIGHT_EXECUTIVE, "c-level")

    # Główny specjalista / Chief Specialist (Rule 6)
    consume(r"\bgłówny specjalista\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "główny specjalista")
    consume(r"\bchief specialist\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "chief specialist")
    consume(r"\bkonsultant biznesowy\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "konsultant biznesowy")
    consume(r"\bbusiness consultant\b", RoleLevel.EXPERT, WEIGHT_EXPERT, "business consultant")

    # 5. Token-based matching
    words = norm_title.split()

    # Rule 1: Expert alone is NOT ignored anymore (only Senior, Starszy, Starsza)
    is_senior_alone = len(words) == 1 and words[0] in {"senior", "starszy", "starsza"}

    for w in words:
        # EXECUTIVE
        if w in ["prezes", "wiceprezes", "vicepresident", "president", "ceo", "cco", "cso", "cfo", "cto", "coo", "zarząd"]:
            signals.append(RoleSignal(level=RoleLevel.EXECUTIVE, source="title", phrase=w, weight=WEIGHT_EXECUTIVE))
        # DIRECTOR (samotne "head" usunięte z DIRECTOR - Rule 4)
        elif w in ["dyrektor", "director", "szef", "szefowa"]:
            signals.append(RoleSignal(level=RoleLevel.DIRECTOR, source="title", phrase=w, weight=WEIGHT_DIRECTOR))
        # MANAGER (ambiguous unless confirmed)
        elif w in ["manager", "menedżer", "menadżer"]:
            signals.append(RoleSignal(level=RoleLevel.MANAGER, source="title", phrase=w, weight=WEIGHT_AMBIGUOUS_MANAGER))
        elif w in ["kierownik", "kierowniczka"]:
            signals.append(RoleSignal(level=RoleLevel.MANAGER, source="title", phrase=w, weight=WEIGHT_MANAGER))
        # LEAD (ambiguous lead)
        elif w in ["lead", "lider", "liderka"]:
            signals.append(RoleSignal(level=RoleLevel.EXPERT, source="title", phrase="ambiguous_lead", weight=WEIGHT_AMBIGUOUS_LEAD))
        # EXPERT (trener, trainer, konsultant, consultant - Rule 6)
        elif w in ["senior", "ekspert", "expert", "architekt", "architect", "principal", "starszy", "starsza", "trener", "trainer", "konsultant", "consultant"]:
            if not is_senior_alone:
                signals.append(RoleSignal(level=RoleLevel.EXPERT, source="title", phrase=w, weight=WEIGHT_EXPERT))
        # SPECIALIST
        elif w in ["specjalista", "specjalistka", "specialist", "developer", "engineer", "inżynier", "analyst", "analityk", "programista", "programistka", "projektant", "designer", "kodowanie", "wdrożeniowiec", "doradca", "advisor", "koordynator", "coordinator", "am", "kam"]:
            signals.append(RoleSignal(level=RoleLevel.SPECIALIST, source="title", phrase=w, weight=WEIGHT_SPECIALIST))
        # JUNIOR
        elif w in ["młodszy", "młodsza", "junior", "stażysta", "stażystka", "staż", "praktykant", "praktykantka", "praktyka", "intern", "asystent", "asystentka", "assistant"]:
            signals.append(RoleSignal(level=RoleLevel.JUNIOR, source="title", phrase=w, weight=WEIGHT_JUNIOR))

    return list(dict.fromkeys(signals))


def _find_description_signals(description: str) -> list[RoleSignal]:
    signals = []
    norm_desc = _normalize_text(description)
    norm_desc = _remove_others_roles(norm_desc)

    has_negation = _has_people_management_negation(description)

    # Description only processes responsibility signals
    # MANAGER (only if not negated)
    if not has_negation:
        mgr_patterns = [
            (r"\bzarządzani[ea]\s+(?:zespołem|ludźmi|pracownik(?:i|ów)?)\b", "zarządzanie zespołem/ludźmi/pracownikami"),
            (r"\bkierowani[ea]\s+zespołem\b", "kierowanie zespołem"),
            (r"\bpeople\s+management\b", "people management"),
            (r"\bdirect\s+reports\b", "direct reports"),
            (r"\bodpowiedzialność\s+za\s+pracownik(?:i|ów)?\b", "odpowiedzialność za pracowników"),
            (r"\bodpowiedzialności\s+za\s+pracownik(?:i|ów)?\b", "odpowiedzialność za pracowników")
        ]
        for pattern, phrase in mgr_patterns:
            if re.search(pattern, norm_desc):
                signals.append(RoleSignal(level=RoleLevel.MANAGER, source="description", phrase=phrase, weight=WEIGHT_MANAGER))

    # DIRECTOR
    dir_patterns = [
        (r"\btworzeni[ea]\s+strategi[aę]\b", "tworzenie strategii"),
        (r"\bodpowiedzialność\s+strategiczna\b", "odpowiedzialność strategiczna"),
        (r"\btworzenie\s+planów\s+strategicznych\b", "tworzenie planów strategicznych"),
        (r"\bodpowiedzialność\s+za\s+p\s*&\s*l\b", "odpowiedzialność za P&L"),
        (r"\bodpowiedzialność\s+za\s+p\s+i\s+l\b", "odpowiedzialność za P&L"),
        (r"\bodpowiedzialność\s+za\s+p\s+l\b", "odpowiedzialność za P&L"),
        (r"\bodpowiedzialność\s+za\s+budżet(?:owa|ową|owej)?\b", "odpowiedzialność za budżet"),
        (r"\bodpowiedzialności\s+za\s+budżet(?:owa|ową|owej)?\b", "odpowiedzialność za budżet"),
        (r"\bzarządzani[ea]\s+regionem\b", "zarządzanie regionem"),
        (r"\bzarządzani[ea]\s+siecią\b", "zarządzanie siecią"),
        (r"\bodpowiedzialność\s+za\s+kanał(?:em)?\b", "odpowiedzialność za kanał"),
        (r"\bodpowiedzialności\s+za\s+kanał(?:em)?\b", "odpowiedzialność za kanał"),
        (r"\bodpowiedzialność\s+za\s+wynik(?:i)?\s+biznesow(?:y|e|ych)?\b", "odpowiedzialność za wynik biznesowy"),
        (r"\bodpowiedzialności\s+za\s+wynik(?:i)?\s+biznesow(?:y|e|ych)?\b", "odpowiedzialność za wynik biznesowy")
    ]
    for pattern, phrase in dir_patterns:
        if re.search(pattern, norm_desc):
            signals.append(RoleSignal(level=RoleLevel.DIRECTOR, source="description", phrase=phrase, weight=WEIGHT_DIRECTOR))

    return signals


def classify_job_level(
    job: JobPositioningInput,
) -> tuple[
    RoleLevel,
    float,
    tuple[RoleSignal, ...],
    tuple[str, ...],
]:
    """Classifies job level based on job title and description."""
    # Pre-check description management
    has_confirmed_people_mgmt = (job.explicit_people_management is True) or _check_desc_for_people_mgmt(job.description)

    # Matched signals
    title_signals = _find_title_signals(job.title, has_confirmed_people_mgmt)
    desc_signals = _find_description_signals(job.description)
    matched_signals = title_signals + desc_signals

    # Rule 14: explicit_people_management = True is a strong MANAGER signal
    if job.explicit_people_management is True:
        matched_signals.append(RoleSignal(level=RoleLevel.MANAGER, source="explicit_people_management", phrase="explicit_people_management=True", weight=6.0))

    if not matched_signals:
        return RoleLevel.UNKNOWN, 0.0, (), ("Brak wystarczających sygnałów do klasyfikacji poziomu stanowiska.",)

    # Sum weights per level
    level_weights = {lvl: 0.0 for lvl in RoleLevel}
    for sig in matched_signals:
        multiplier = WEIGHT_TITLE if sig.source == "title" else WEIGHT_DESCRIPTION
        level_weights[sig.level] += sig.weight * multiplier

    # Determine winning level
    winning_level = RoleLevel.UNKNOWN
    max_weight = 0.0
    for lvl, w in level_weights.items():
        if lvl == RoleLevel.UNKNOWN:
            continue
        if w > max_weight:
            max_weight = w
            winning_level = lvl

    if winning_level == RoleLevel.UNKNOWN or max_weight == 0.0:
        return RoleLevel.UNKNOWN, 0.0, (), ("Brak jednoznacznego zwycięskiego poziomu stanowiska.",)

    # Check for confirmed people management (Rule 9 helper)
    has_confirmed_mgr_signal = _has_confirmed_people_management(matched_signals, job.explicit_people_management)

    # Check unresolved manager (manager keyword without team context)
    has_unresolved_manager = False
    for sig in matched_signals:
        if sig.phrase == "project_manager_unresolved":
            has_unresolved_manager = True
        elif sig.level == RoleLevel.MANAGER and sig.phrase in ["manager", "menedżer", "menadżer", "sales manager", "channel manager", "business development manager", "strategic partnership manager"]:
            if not has_confirmed_mgr_signal:
                has_unresolved_manager = True

    # Check unresolved lead (Lead/Team Lead without team context)
    has_unresolved_lead = False
    for sig in matched_signals:
        if "lead" in sig.phrase:
            if not has_confirmed_mgr_signal:
                has_unresolved_lead = True

    # Rule 1: Generic Manager without confirmed management becomes UNKNOWN
    if winning_level == RoleLevel.MANAGER and not has_confirmed_mgr_signal:
        winning_level = RoleLevel.UNKNOWN

    # Rule 2: Strong conflict also forces UNKNOWN (Rule 9 helper used)
    has_strong_conflict = _has_strong_classification_conflict(matched_signals, job)
    if has_strong_conflict:
        winning_level = RoleLevel.UNKNOWN

    # Second highest level weight
    second_weight = 0.0
    second_level = RoleLevel.UNKNOWN
    for lvl, w in level_weights.items():
        if lvl != winning_level and lvl != RoleLevel.UNKNOWN:
            if w > second_weight:
                second_weight = w
                second_level = lvl

    # Confidence calculation
    confidence = CONF_BASE

    # 1. Strength of title signals
    title_weights = sum(sig.weight for sig in matched_signals if sig.source == "title")
    if title_weights >= 3.0:
        confidence *= 1.1
    else:
        confidence *= 0.9

    # 2. Confirmation in description responsibilities
    has_desc_confirm = any(sig.source == "description" and sig.level == winning_level for sig in matched_signals)
    if has_desc_confirm:
        confidence *= 1.1
    else:
        confidence *= 0.85

    # 3. Margin of victory
    if max_weight > 0:
        margin = (max_weight - second_weight) / max_weight
        if margin < CONF_MARGIN_TIGHT:
            confidence *= CONF_MARGIN_PENALTY

    # 4. Consistency & Conflicts
    title_levels = {s.level for s in matched_signals if s.source == "title" if s.level != RoleLevel.UNKNOWN}
    desc_levels = {s.level for s in matched_signals if s.source == "description" if s.level != RoleLevel.UNKNOWN}

    if has_strong_conflict:
        confidence *= 0.5
    elif title_levels and desc_levels:
        if title_levels != desc_levels:
            confidence *= 0.7

    # Apply limits on confidence (Rule 9)
    if has_strong_conflict:
        confidence = min(0.49, confidence)
    elif has_unresolved_manager or has_unresolved_lead:
        confidence = min(0.59, confidence)

    # Clamp confidence
    confidence = max(CONF_MIN, min(CONF_MAX, confidence))

    # Reasons generation
    reasons = []
    if winning_level == RoleLevel.UNKNOWN:
        reasons.append("Poziom stanowiska jest nieznany (brak sygnałów lub nierozstrzygnięty manager/lead).")
    else:
        reasons.append(f"Stanowisko sklasyfikowane jako {winning_level.name} z wagą sygnałów {max_weight:.1f}.")

    # Add consistency reason ONLY if no strong conflict and title and desc agree
    if not has_strong_conflict and title_levels == desc_levels and len(title_levels) > 0:
        reasons.append("Sygnały poziomu są spójne między tytułem a opisem.")

    if has_unresolved_lead:
        reasons.append("Wykryto niejednoznaczny sygnał 'lead' bez potwierdzenia zarządzania ludźmi.")
    if has_unresolved_manager:
        reasons.append("Wykryto słowo 'manager' bez potwierdzonego zarządzania ludźmi.")
    if has_strong_conflict:
        reasons.append("Wykryto silny konflikt między sygnałami poziomu lub obecnością negacji.")

    return winning_level, confidence, tuple(matched_signals), tuple(reasons)


def assess_positioning(
    candidate: CandidatePositioningProfile,
    job: JobPositioningInput,
) -> PositioningResult:
    """Evaluates candidate positioning, risks, title treatment, and presentation strategy."""
    job_level, confidence, matched_signals, classify_reasons = classify_job_level(job)
    candidate_level = candidate.candidate_level

    # Level gap: candidate level - job level
    level_gap = candidate_level.value - job_level.value

    # Default result values
    overqualification_risk = RiskLevel.NONE
    underqualification_risk = RiskLevel.NONE
    strategy = PositioningStrategy.BALANCED
    title_treatment = TitleTreatment.KEEP
    reasons = list(classify_reasons)
    emphasize = ()
    deemphasize = ()
    requires_human_review = False

    # Centralized conflict check (Rule 9)
    has_strong_conflict = _has_strong_classification_conflict(matched_signals, job)

    # Rule 3: Low confidence (<= 0.49) or strong conflict blocks validation scoring
    if confidence <= 0.49 or has_strong_conflict or job_level == RoleLevel.UNKNOWN or candidate_level == RoleLevel.UNKNOWN:
        strategy = PositioningStrategy.REVIEW_REQUIRED
        title_treatment = TitleTreatment.REVIEW
        overqualification_risk = RiskLevel.NONE
        underqualification_risk = RiskLevel.NONE
        requires_human_review = True
        reasons.append("Brak możliwości wiarygodnej oceny pozycjonowania z powodu niskiej pewności lub konfliktu.")
        return PositioningResult(
            job_level=job_level,
            candidate_level=candidate_level,
            level_gap=level_gap,
            overqualification_risk=overqualification_risk,
            underqualification_risk=underqualification_risk,
            strategy=strategy,
            title_treatment=title_treatment,
            confidence=confidence,
            reasons=tuple(reasons),
            emphasize=(),
            deemphasize=(),
            matched_signals=matched_signals,
            requires_human_review=requires_human_review,
        )

    # Evaluates Candidate Scale
    is_large_scale = (
        candidate.managed_people_max >= 50 or
        candidate.managed_units_max >= 20 or
        candidate.management_years >= 8 or
        candidate.has_strategy_responsibility or
        candidate.has_budget_responsibility or
        candidate.has_sales_result_responsibility
    )

    # 1. Risk Evaluation
    if level_gap == 0:
        overqualification_risk = RiskLevel.NONE
        underqualification_risk = RiskLevel.NONE
        reasons.append("Poziom kandydata jest w pełni zgodny z poziomem stanowiska.")
    elif level_gap == 1:
        underqualification_risk = RiskLevel.NONE
        if is_large_scale:
            overqualification_risk = RiskLevel.MEDIUM
            reasons.append("Wykryto ryzyko overqualification na poziomie MEDIUM z powodu dużej skali doświadczenia kandydata.")
        else:
            overqualification_risk = RiskLevel.LOW
            reasons.append("Wykryto ryzyko overqualification na poziomie LOW (kandydat o 1 poziom powyżej stanowiska).")
    elif level_gap == 2:
        overqualification_risk = RiskLevel.MEDIUM
        underqualification_risk = RiskLevel.NONE
        reasons.append("Wykryto ryzyko overqualification na poziomie MEDIUM (kandydat o 2 poziomy powyżej stanowiska).")
    elif level_gap >= 3:
        overqualification_risk = RiskLevel.HIGH
        underqualification_risk = RiskLevel.NONE
        reasons.append("Wykryto ryzyko overqualification na poziomie HIGH (kandydat o 3 lub więcej poziomów powyżej stanowiska).")
    elif level_gap == -1:
        overqualification_risk = RiskLevel.NONE
        req_years = job.required_years or 0.0
        if candidate.total_years >= req_years:
            underqualification_risk = RiskLevel.LOW
            reasons.append("Wykryto ryzyko underqualification na poziomie LOW (staż pracy kandydata jest wystarczający).")
        else:
            underqualification_risk = RiskLevel.MEDIUM
            reasons.append("Wykryto ryzyko underqualification na poziomie MEDIUM z powodu niewystarczającego stażu pracy kandydata.")
    elif level_gap == -2:
        overqualification_risk = RiskLevel.NONE
        underqualification_risk = RiskLevel.MEDIUM
        reasons.append("Wykryto ryzyko underqualification na poziomie MEDIUM (kandydat o 2 poziomy poniżej stanowiska).")
    elif level_gap <= -3:
        overqualification_risk = RiskLevel.NONE
        underqualification_risk = RiskLevel.HIGH
        reasons.append("Wykryto ryzyko underqualification na poziomie HIGH (kandydat o 3 lub więcej poziomów poniżej stanowiska).")

    # 2. Strategy Selection (Rule 10: emphasize / deemphasize for gap 0 stabilized)
    if level_gap >= 2:
        strategy = PositioningStrategy.CONTROLLED_FLATTENING
    elif level_gap == 1:
        if is_large_scale:
            strategy = PositioningStrategy.CONTROLLED_FLATTENING
        else:
            strategy = PositioningStrategy.BALANCED
    elif level_gap < 0:
        strategy = PositioningStrategy.CONTROLLED_ELEVATION
    else:  # level_gap == 0
        if job_level == RoleLevel.JUNIOR:
            strategy = PositioningStrategy.JUNIOR_ENTRY
        elif job_level == RoleLevel.SPECIALIST:
            strategy = PositioningStrategy.SPECIALIST_DELIVERY
        elif job_level == RoleLevel.EXPERT:
            strategy = PositioningStrategy.EXPERT_AUTHORITY
            emphasize = (
                "wiedza ekspercka",
                "samodzielność",
                "wdrożenia",
                "analiza",
                "szkolenia i rozwój kompetencji"
            )
        elif job_level == RoleLevel.MANAGER:
            strategy = PositioningStrategy.MANAGER_LEADERSHIP
            emphasize = (
                "zarządzanie zespołem",
                "rozwój pracowników",
                "realizacja celów zespołu",
                "koordynacja działań",
                "odpowiedzialność operacyjna"
            )
        elif job_level == RoleLevel.DIRECTOR:
            strategy = PositioningStrategy.DIRECTOR_SCALE
            emphasize = (
                "strategia",
                "wynik biznesowy",
                "skala odpowiedzialności",
                "zarządzanie strukturą",
                "odpowiedzialność za budżet"
            )
        elif job_level == RoleLevel.EXECUTIVE:
            strategy = PositioningStrategy.EXECUTIVE_ENTERPRISE
            emphasize = (
                "kierunek organizacji",
                "odpowiedzialność zarządcza",
                "wynik przedsiębiorstwa",
                "ład organizacyjny",
                "decyzje strategiczne"
            )

    # 3. Title Treatment
    has_extreme_conflict = any(s.level == RoleLevel.JUNIOR for s in matched_signals) and any(s.level in {RoleLevel.DIRECTOR, RoleLevel.EXECUTIVE} for s in matched_signals)

    if confidence < 0.4 or has_extreme_conflict or has_strong_conflict:
        title_treatment = TitleTreatment.REVIEW
        reasons.append("Rekomendacja weryfikacji tytułu (REVIEW) ze względu na niski poziom pewności lub silne konflikty.")
    elif level_gap == 0:
        title_treatment = TitleTreatment.KEEP
    elif level_gap == 1:
        title_treatment = TitleTreatment.CONTEXTUALIZE
    elif level_gap >= 2:
        title_treatment = TitleTreatment.DEEMPHASIZE
    else:  # level_gap < 0
        title_treatment = TitleTreatment.KEEP

    # 4. Emphasize and Deemphasize details for CONTROLLED_FLATTENING
    if strategy == PositioningStrategy.CONTROLLED_FLATTENING:
        emphasize = (
            "praca operacyjna i własny wkład",
            "wdrożenia i realizacja",
            "kompetencje eksperckie",
            "współpraca międzyfunkcyjna",
            "rezultaty istotne dla ogłoszenia"
        )
        deemphasize = (
            "rozbudowana hierarchia organizacyjna",
            "dominacja liczby podległych osób",
            "język transformacji całej organizacji",
            "nadmierne eksponowanie odpowiedzialności strategicznej",
            "nieistotne dla roli elementy skali"
        )

    # Human review conditions
    has_confirmed_people_mgmt = _has_confirmed_people_management(
        list(matched_signals),
        job.explicit_people_management,
    )

    has_unresolved_manager = False
    for sig in matched_signals:
        if sig.level == RoleLevel.MANAGER and sig.phrase in ["manager", "menedżer", "menadżer", "sales manager", "channel manager", "business development manager", "strategic partnership manager", "project_manager_unresolved"]:
            if not has_confirmed_people_mgmt:
                has_unresolved_manager = True

    has_unresolved_lead = any("ambiguous_lead" in s.phrase for s in matched_signals) and not has_confirmed_people_mgmt

    has_unresolved_project_manager = any(
        signal.phrase == "project_manager_unresolved"
        for signal in matched_signals
    )

    if confidence < 0.5 or has_unresolved_lead or has_unresolved_manager or title_treatment == TitleTreatment.REVIEW or has_strong_conflict or has_unresolved_project_manager:
        requires_human_review = True
        reasons.append("Wymagana ręczna weryfikacja dopasowania stanowiska.")

    return PositioningResult(
        job_level=job_level,
        candidate_level=candidate_level,
        level_gap=level_gap,
        overqualification_risk=overqualification_risk,
        underqualification_risk=underqualification_risk,
        strategy=strategy,
        title_treatment=title_treatment,
        confidence=confidence,
        reasons=tuple(reasons),
        emphasize=emphasize,
        deemphasize=deemphasize,
        matched_signals=matched_signals,
        requires_human_review=requires_human_review,
    )
