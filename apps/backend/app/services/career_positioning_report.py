"""Career Positioning Report Builder Service.

Generates the complete career positioning report based on candidate's truth library facts
and the target job description requirements.
"""

import re
from datetime import datetime, timezone
from typing import Any, Optional, Literal
from app.services.career_positioning import (
    RoleLevel,
    RiskLevel,
    PositioningStrategy,
    TitleTreatment,
    CandidatePositioningProfile,
    JobPositioningInput,
    classify_job_level,
    assess_positioning,
)
from app.services.truth_index import normalize_truth_text
from app.schemas.career_positioning import (
    CareerPositioningResponse,
    PositioningDetailsSchema,
    RisksSchema,
    RiskItemSchema,
    CompetencyItemSchema,
    CompetenciesSchema,
    StrategySchema,
    NarrativeVariantSchema,
    EvidenceSchema,
    LimitationsSchema,
)

COMPETENCY_KEYWORD_MAP = {
    "SQL": ["sql", "bazy danych", "postgres", "mysql", "oracle", "mssql", "baza danych sql"],
    "Python": ["python", "pyspark"],
    "Excel": ["excel", "arkusze kalkulacyjne", "spreadsheets", "arkusz kalkulacyjny"],
    "Zarządzanie budżetem": ["budżet", "budzet", "budgeting", "budget", "p&l", "p i l"],
    "Zarządzanie zespołem": ["zarządzanie zespołem", "kierowanie zespołem", "people management", "team leadership", "lider zespołu"],
    "Zarządzanie projektami": ["zarządzanie projektami", "project management", "pm", "pmp", "scrum", "agile", "jira"],
    "Analiza danych": ["analiza danych", "data analysis", "tableau", "power bi", "analytics"],
    "CRM": ["crm", "salesforce", "hubspot"],
    "Negocjacje": ["negocjacje", "negotiations", "omawianie warunków"],
    "Obsługa klienta": ["obsługa klienta", "customer service", "relacje z klientami"],
    "Marketing": ["marketing", "social media", "seo", "sem", "public relations", "pr"],
    "Chmura obliczeniowa": ["cloud", "aws", "azure", "gcp"],
    "Szkolenia": ["szkolenia", "training", "warsztaty", "mentoring", "coaching"],
    "Bancassurance": ["bancassurance", "banc-assurance"],
    "Pricing": ["pricing", "wycena"],
    "Regulatory compliance": ["regulatory compliance", "compliance", "zgodność z przepisami", "regulacje"]
}

ALLOWED_FIELDS_MAP = {
    "doswiadczenieZawodowe": ["stanowisko", "stanowiskoAlt", "firma", "wynikLiczbowy", "skalaOdpowiedzialnosci"],
    "kompetencje": ["nazwa", "nazwaAlt", "poziom"],
    "narzedzia": ["nazwa", "nazwaAlt"],
    "technologie": ["nazwa", "nazwaAlt"],
    "certyfikaty": ["nazwa"],
    "kursy": ["nazwa", "organizator"],
    "wyksztalcenie": ["kierunek", "uczelnia", "stopien"],
    "jezyki": ["jezyk", "poziom"],
    "osiagnieciaLiczbowe": ["opis", "wynik"],
    "skalaOdpowiedzialnosci": ["opis", "skala"]
}


def parse_date(date_str: str | None, now: datetime) -> Optional[tuple[int, int]]:
    """Parse date string into (year, month). Returns None if missing/invalid."""
    if not date_str:
        return None
    date_str = date_str.strip().lower()
    if date_str in ("obecnie", "present", "now", "current"):
        return now.year, now.month
    match = re.match(r"^(\d{4})-(\d{2})$", date_str)
    if match:
        y, m = int(match.group(1)), int(match.group(2))
        if 1 <= m <= 12:
            return y, m
    match = re.match(r"^(\d{4})$", date_str)
    if match:
        return int(match.group(1)), 1
    return None


def is_entry_allowed(entry: dict[str, Any], reguly: dict[str, Any]) -> bool:
    """Helper to check if a Truth Library entry is allowed for CV generation."""
    if not isinstance(entry, dict):
        return False

    status = entry.get("status")
    if not status or not isinstance(status, str) or not status.strip():
        return False

    wymaga_akceptacji = entry.get("wymagaAkceptacji", False)
    uzywac_w_cv = entry.get("uzywacWCV", True)

    if uzywac_w_cv is False:
        return False
    if wymaga_akceptacji is True:
        return False

    auto_cv = reguly.get("statusyDoAutomatycznegoCV", [])
    req_approval = reguly.get("statusyWymagajaceAkceptacji", [])
    blocked = reguly.get("statusyZablokowane", [])

    if status in blocked:
        return False
    if status in req_approval:
        return False
    if status not in auto_cv:
        return False

    # These lifecycle states are never trusted, even if a malformed ruleset
    # accidentally lists one as eligible for automatic CV generation.
    if status in ("NIEJASNE", "USUNIĘTE", "PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA"):
        return False

    return True


def gather_candidate_texts(truth_lib: dict[str, Any], allowed_entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Gathers all texts from approved sections of the Truth Library, with public names."""
    texts = []
    reguly = truth_lib.get("kategorie", {}).get("reguly", {})

    for entry in allowed_entries:
        company = entry.get("firma") or "Pracodawca"
        role = entry.get("stanowisko") or "Stanowisko"
        source_name = f"Doświadczenie: {role} w {company}"

        allowed_fields = ALLOWED_FIELDS_MAP.get("doswiadczenieZawodowe", [])
        for field in allowed_fields:
            val = entry.get(field)
            if isinstance(val, str) and val.strip():
                texts.append((val, source_name))

        for act in entry.get("aktywnosci", []):
            if isinstance(act, str) and act.strip():
                texts.append((act, source_name))

    kategorie = truth_lib.get("kategorie", {})

    category_map = {
        "kompetencje": "Kompetencje i umiejętności",
        "narzedzia": "Narzędzia pracy",
        "technologie": "Technologie i języki",
        "certyfikaty": "Certyfikaty i licencje",
        "kursy": "Szkolenia i kursy",
        "wyksztalcenie": "Wykształcenie",
        "jezyki": "Języki obce",
        "osiagnieciaLiczbowe": "Osiągnięcia liczbowe",
        "skalaOdpowiedzialnosci": "Skala odpowiedzialności"
    }

    for cat_name, cat_data in kategorie.items():
        if cat_name in ("doswiadczenieZawodowe", "reguly", "zakazy"):
            continue
        if not isinstance(cat_data, dict):
            continue

        wpisy = cat_data.get("wpisy", [])
        if not isinstance(wpisy, list):
            continue

        public_name = category_map.get(cat_name, cat_name.capitalize())

        for item in wpisy:
            if isinstance(item, dict):
                if not is_entry_allowed(item, reguly):
                    continue
                allowed_fields = ALLOWED_FIELDS_MAP.get(cat_name, [])
                for field in allowed_fields:
                    val = item.get(field)
                    if isinstance(val, str) and val.strip():
                        texts.append((val, public_name))
            elif isinstance(item, str) and item.strip():
                texts.append((item, public_name))

    return texts


def build_career_positioning_report(
    job: dict[str, Any],
    truth_library: dict[str, Any],
    now: datetime,
) -> CareerPositioningResponse:
    """Builds a deterministic Career Positioning report."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    job_id = job.get("job_id") or job.get("id") or "unknown-job"
    job_content = job.get("content", "")
    metadata = job.get("metadata_json") or {}
    job_title = job.get("role") or metadata.get("role") or "Stanowisko"

    if not job_title or job_title == "Stanowisko":
        first_line = job_content.split("\n")[0].strip()
        if first_line and len(first_line) < 100:
            job_title = first_line
        else:
            job_title = "Oferta Pracy"

    reguly = truth_library.get("kategorie", {}).get("reguly", {})
    raw_wpisy = truth_library.get("kategorie", {}).get("doswiadczenieZawodowe", {}).get("wpisy", [])

    wpisy = [entry for entry in raw_wpisy if is_entry_allowed(entry, reguly)]

    limit_msgs = []

    max_level = RoleLevel.UNKNOWN
    total_months: set[tuple[int, int]] = set()
    mgmt_months: set[tuple[int, int]] = set()
    managed_people_max = 0
    managed_units_max = 0
    has_strategy_responsibility = False
    has_budget_responsibility = False
    has_sales_result_responsibility = False
    has_any_team_proof = False
    evidence_references = []

    for entry in wpisy:
        ent_id = entry.get("id")
        if ent_id:
            evidence_references.append(ent_id)

        title = entry.get("stanowisko", "")
        desc = (
            (entry.get("stanowiskoAlt") or "") + "\n" +
            "\n".join(entry.get("aktywnosci", [])) + "\n" +
            (entry.get("wynikLiczbowy") or "") + "\n" +
            (entry.get("skalaOdpowiedzialnosci") or "")
        )

        entry_level = RoleLevel.UNKNOWN
        if title:
            try:
                entry_level, _, _, _ = classify_job_level(JobPositioningInput(title=title, description=desc))
                if entry_level.value > max_level.value:
                    max_level = entry_level
            except Exception:
                pass

        # Managed resources matching
        text_to_scan = " ".join([
            entry.get("skalaOdpowiedzialnosci") or "",
            entry.get("wynikLiczbowy") or "",
            " ".join(entry.get("aktywnosci") or [])
        ])

        entry_people_max = 0
        people_matches = re.findall(r"(\d+)\s*(?:sprzedawców|pracowników|osób|podwładnych|ludzi|zespół|zespołem|team)", text_to_scan, re.IGNORECASE)
        for m in people_matches:
            try:
                val = int(m)
                if val > entry_people_max:
                    entry_people_max = val
            except ValueError:
                pass
        if entry_people_max > managed_people_max:
            managed_people_max = entry_people_max

        units_matches = re.findall(r"(\d+)\s*(?:agencji|oddziałów|placówek|partnerów|regionów|jednostek)", text_to_scan, re.IGNORECASE)
        for m in units_matches:
            try:
                val = int(m)
                if val > managed_units_max:
                    managed_units_max = val
            except ValueError:
                pass

        # Responsibilities
        text_lower = text_to_scan.lower()
        if any(kw in text_lower for kw in ["strateg", "strategic"]):
            has_strategy_responsibility = True
        if any(kw in text_lower for kw in ["budżet", "budzet", "p&l", "p i l"]):
            has_budget_responsibility = True
        sales_result_keywords = ["sprzedaż", "sprzedaz", "wolumen", "portfel"]
        sales_result_phrases = [
            "wynik sprzedażowy", "wyniki sprzedażowe", "wynik finansowy", "wyniki finansowe",
            "wynik biznesowy", "wyniki biznesowe", "dowożenie wyników", "realizacja wyników",
            "wyniki sprzedaży", "wynik sprzedaży", "wyniki roczne", "wyniki kwartalne",
            "realizacja celu", "realizacja celów"
        ]
        if any(kw in text_lower for kw in sales_result_keywords) or any(phrase in text_lower for phrase in sales_result_phrases):
            has_sales_result_responsibility = True

        entry_has_team_proof = (entry_people_max > 0) or any(kw in text_lower for kw in [
            "zarządzanie zespołem", "kierowanie zespołem", "zarządzanie ludźmi", "people management",
            "zarządzanie strukturą", "kierowanie ludźmi", "lider zespołu"
        ])
        if entry_has_team_proof:
            has_any_team_proof = True

        # Durations
        start_date = parse_date(entry.get("okresOd"), now)
        end_date = parse_date(entry.get("okresDo"), now)

        if not start_date or not end_date:
            limit_msgs.append(f"Wpis {ent_id or title or 'bez ID'} pominięty w obliczeniach stażu z powodu braku lub błędnego formatu dat.")
        else:
            sy, sm = start_date
            ey, em = end_date
            if (sy < ey) or (sy == ey and sm <= em):
                cy, cm = sy, sm
                iterations = 0
                while (cy < ey or (cy == ey and cm <= em)) and iterations < 1200:
                    total_months.add((cy, cm))

                    is_mgmt_role = False
                    if entry_level >= RoleLevel.MANAGER and entry_has_team_proof:
                        is_mgmt_role = True

                    if is_mgmt_role:
                        mgmt_months.add((cy, cm))

                    cm += 1
                    if cm > 12:
                        cm = 1
                        cy += 1
                    iterations += 1
            else:
                limit_msgs.append(f"Wpis {ent_id or title or 'bez ID'} pominięty w obliczeniach stażu: data początkowa jest późniejsza niż końcowa.")

    # Candidate Level Determination
    insufficient = (len(wpisy) == 0)
    if max_level == RoleLevel.UNKNOWN:
        candidate_level = RoleLevel.UNKNOWN
        insufficient = True
        limit_msgs.append("Nie udało się określić wiarygodnego poziomu stanowiska z dostępnych wpisów kandydata.")
    else:
        candidate_level = max_level

    total_years = len(total_months) / 12.0
    management_years = len(mgmt_months) / 12.0

    candidate_profile = CandidatePositioningProfile(
        candidate_level=candidate_level,
        total_years=float(total_years),
        management_years=float(management_years),
        managed_people_max=int(managed_people_max),
        managed_units_max=int(managed_units_max),
        has_strategy_responsibility=bool(has_strategy_responsibility),
        has_budget_responsibility=bool(has_budget_responsibility),
        has_sales_result_responsibility=bool(has_sales_result_responsibility),
        evidence_references=tuple(evidence_references),
    )

    # Job Input Parser
    req_years = None
    match = re.search(r"(\d+)\s*(?:lat|lata|years|yrs)\s*(?:doświadczenia|experience)", job_content, re.IGNORECASE)
    if match:
        try:
            req_years = float(match.group(1))
        except ValueError:
            pass

    explicit_mgmt = None
    if any(kw in job_content.lower() for kw in ["zarządzanie zespołem", "zarządzanie ludźmi", "people management", "direct reports"]):
        explicit_mgmt = True

    job_input = JobPositioningInput(
        title=job_title,
        description=job_content,
        required_years=req_years,
        explicit_people_management=explicit_mgmt,
    )

    # Engine Assessment
    res = assess_positioning(candidate_profile, job_input)

    # Fit Score calculation
    score_base = 100.0 - abs(res.level_gap) * 20.0
    score_base = max(0.0, min(100.0, score_base))
    fit_score = round(score_base * res.confidence, 1)

    if fit_score >= 80.0:
        fit_level = "STRONG"
    elif fit_score >= 60.0:
        fit_level = "GOOD"
    elif fit_score >= 40.0:
        fit_level = "MODERATE"
    else:
        fit_level = "LOW"

    # Sector Gap Evaluation
    job_desc_lower = job_content.lower()
    sectors = truth_library.get("kategorie", {}).get("branze", {}).get("zatwierdzoneBranze", [])
    has_sector_match = False
    if not sectors:
        sector_gap = RiskItemSchema(
            availability="UNSUPPORTED",
            level=None,
            rationale=[]
        )
    else:
        for sector in sectors:
            if sector.lower() in job_desc_lower:
                has_sector_match = True
                break
        sector_gap = RiskItemSchema(
            availability="AVAILABLE",
            level="NONE" if has_sector_match else "MEDIUM",
            rationale=["Wykryto dopasowanie branżowe."] if has_sector_match else ["Brak dopasowania branżowego z zatwierdzonymi branżami."]
        )

    # Functional Gap Evaluation
    has_detected_functional_requirements = False
    functional_rationale = []
    functional_level = "NONE"

    if "budżet" in job_desc_lower or "budget" in job_desc_lower:
        has_detected_functional_requirements = True
        if not candidate_profile.has_budget_responsibility:
            functional_level = "MEDIUM"
            functional_rationale.append("Oferta wymaga odpowiedzialności budżetowej, której brak w profilu kandydata.")

    if "strategi" in job_desc_lower or "strategy" in job_desc_lower:
        has_detected_functional_requirements = True
        if not candidate_profile.has_strategy_responsibility:
            functional_level = "MEDIUM"
            functional_rationale.append("Oferta wymaga odpowiedzialności strategicznej, której brak w profilu kandydata.")

    if has_detected_functional_requirements:
        functional_gap = RiskItemSchema(
            availability="AVAILABLE",
            level=functional_level,
            rationale=functional_rationale if functional_rationale else ["Zweryfikowano zgodność kluczowych wymagań funkcjonalnych."]
        )
    else:
        functional_gap = RiskItemSchema(
            availability="UNSUPPORTED",
            level=None,
            rationale=[]
        )

    # Overqualification and Underqualification Risks
    overqualification_risk = RiskItemSchema(
        availability="AVAILABLE",
        level=res.overqualification_risk.name,
        rationale=[r for r in res.reasons if "overqualification" in r.lower() or "zgodny" in r.lower()]
    )
    underqualification_risk = RiskItemSchema(
        availability="AVAILABLE",
        level=res.underqualification_risk.name,
        rationale=[r for r in res.reasons if "underqualification" in r.lower() or "zgodny" in r.lower()]
    )
    stability_concern = RiskItemSchema(
        availability="UNSUPPORTED",
        level=None,
        rationale=[]
    )

    # Competency requirements mechanism using metadata and job description
    direct_matches = []
    transferable_matches = []
    missing_critical = []
    excessive_for_role = []

    candidate_texts = gather_candidate_texts(truth_library, wpisy)

    seen_comp = set()
    mappings = truth_library.get("kategorie", {}).get("tranferowalneKompetencje", {}).get("mapowania", {})

    # 1. candidate_terms gather
    candidate_terms = set()
    for entry in wpisy:
        if entry.get("stanowisko"):
            candidate_terms.add(entry["stanowisko"].lower())
        for act in entry.get("aktywnosci", []):
            candidate_terms.add(act.lower())
    for raw_key, mapped_val in mappings.items():
        candidate_terms.add(raw_key.lower())
        candidate_terms.add(mapped_val.lower())

    # Compile target competencies to evaluate
    target_competencies = {}

    # Extract from metadata_json
    meta_keywords = []
    for key in ("job_keywords", "keywords", "required_skills", "competencies", "requirements"):
        val = metadata.get(key)
        if isinstance(val, list):
            meta_keywords.extend([str(item) for item in val if item])
        elif isinstance(val, str):
            meta_keywords.extend([item.strip() for item in val.split(",") if item.strip()])

    for kw in meta_keywords:
        norm_kw = kw.strip().lower()
        if norm_kw:
            syns = [norm_kw]
            for dict_name, dict_syns in COMPETENCY_KEYWORD_MAP.items():
                if norm_kw == dict_name.lower() or norm_kw in [s.lower() for s in dict_syns]:
                    syns = [s.lower() for s in dict_syns]
                    norm_kw = dict_name
                    break
            target_competencies[norm_kw] = syns

    # Extract from known dictionary terms
    for dict_name, dict_syns in COMPETENCY_KEYWORD_MAP.items():
        requires_comp = False
        for s in dict_syns:
            if s.lower() in job_desc_lower:
                requires_comp = True
                break
        if requires_comp:
            if dict_name not in target_competencies:
                target_competencies[dict_name] = [s.lower() for s in dict_syns]

    # Extract from candidate terms in job content
    for term in candidate_terms:
        if len(term) > 3 and term in job_desc_lower:
            title_term = term.capitalize()
            if title_term not in target_competencies:
                target_competencies[title_term] = [term]

    for comp_name, keywords in target_competencies.items():
        # Check direct match
        has_direct = False
        direct_src = ""
        for cand_text, src_name in candidate_texts:
            cand_text_norm = normalize_truth_text(cand_text)
            for kw in keywords:
                if kw in cand_text_norm:
                    has_direct = True
                    direct_src = src_name
                    break
            if has_direct:
                break

        if has_direct:
            norm_key = comp_name.strip().lower()
            if norm_key not in seen_comp:
                seen_comp.add(norm_key)
                direct_matches.append(CompetencyItemSchema(
                    name=comp_name,
                    reason="Potwierdzona kompetencja bezpośrednia w profilu.",
                    source=direct_src
                ))
            continue

        # Check transferable match
        has_transferable = False
        trans_src = ""
        mapped_key = ""
        for raw_key, mapped_val in mappings.items():
            if mapped_val.strip().lower() == comp_name.strip().lower():
                for cand_text, src_name in candidate_texts:
                    if raw_key.strip().lower() in cand_text.strip().lower():
                        has_transferable = True
                        trans_src = src_name
                        mapped_key = raw_key
                        break
            if has_transferable:
                break

        if has_transferable:
            norm_key = comp_name.strip().lower()
            if norm_key not in seen_comp:
                seen_comp.add(norm_key)
                transferable_matches.append(CompetencyItemSchema(
                    name=comp_name,
                    reason=f"Kompetencja transferowalna wykazana poprzez '{mapped_key}'.",
                    source=trans_src
                ))
            continue

        # Otherwise missing
        norm_key = comp_name.strip().lower()
        if norm_key not in seen_comp:
            seen_comp.add(norm_key)
            missing_critical.append(CompetencyItemSchema(
                name=comp_name,
                reason="Kompetencja wymagana przez ofertę, brak potwierdzenia w profilu.",
                source="Brakujące kompetencje"
            ))

    # Blocked rules
    blocked_rules = truth_library.get("kategorie", {}).get("zakazy", {}).get("bezwzgledneZakazy", [])
    for rule in blocked_rules:
        if rule.lower() in job_desc_lower:
            norm_key = rule.strip().lower()
            if norm_key not in seen_comp:
                seen_comp.add(norm_key)
                missing_critical.append(CompetencyItemSchema(
                    name=rule,
                    reason="Koliduje z zadeklarowanymi ograniczeniami kandydata.",
                    source="Zakazy i ograniczenia"
                ))

    if res.overqualification_risk in (RiskLevel.HIGH, RiskLevel.MEDIUM):
        if candidate_profile.managed_people_max > 0:
            excessive_for_role.append(CompetencyItemSchema(
                name=f"Skala kierowania zespołem ({candidate_profile.managed_people_max} osób)",
                reason="Skala zarządzania ludźmi wykracza poza zakres wymagań tej roli.",
                source="Skala odpowiedzialności"
            ))
        if candidate_profile.has_strategy_responsibility:
            excessive_for_role.append(CompetencyItemSchema(
                name="Kompetencje strategiczne",
                reason="Doświadczenie w kreowaniu strategii przewyższa zakres operacyjny roli.",
                source="Skala odpowiedzialności"
            ))

    # Evidence details
    board_patterns = [
        "współpraca z zarządem",
        "raportowanie do zarządu",
        "prezentowanie zarządowi",
        "board-level",
        "member of the board",
        "reporting to the board",
        "rada nadzorcza",
        "c-level",
    ]
    has_board_proof = any(
        any(pat in t.lower() for pat in board_patterns)
        for t, _ in candidate_texts
    )

    p_and_l_keywords = ["p&l", "p i l", "rachunek zysków"]
    p_and_l_phrases = [
        "odpowiedzialność za wynik",
        "odpowiedzialny za wynik",
        "ownership wyniku",
        "ownership of the financial",
        "ownership over financial",
        "managing profit and loss",
        "zarządzanie budżetem p&l",
        "odpowiedzialność za p&l",
    ]
    has_p_and_l_proof = any(
        any(kw in t.lower() for kw in p_and_l_keywords) or any(phrase in t.lower() for phrase in p_and_l_phrases)
        for t, _ in candidate_texts
    )

    has_managing_managers_proof = any(
        "zarządzanie menedżerami" in t.lower() or "zarządzanie kierownikami" in t.lower() or "wielopoziom" in t.lower()
        for t, _ in candidate_texts
    )

    # Filter technologies and tools using is_entry_allowed and allowed fields
    allowed_tech = [w for w in truth_library.get("kategorie", {}).get("technologie", {}).get("wpisy", []) if is_entry_allowed(w, reguly)]
    allowed_tools = [w for w in truth_library.get("kategorie", {}).get("narzedzia", {}).get("wpisy", []) if is_entry_allowed(w, reguly)]

    tech_has_text = False
    for item in allowed_tech:
        if isinstance(item, dict):
            for field in ALLOWED_FIELDS_MAP.get("technologie", []):
                val = item.get(field)
                if isinstance(val, str) and val.strip():
                    tech_has_text = True
                    break
        elif isinstance(item, str) and item.strip():
            tech_has_text = True

    tool_has_text = False
    for item in allowed_tools:
        if isinstance(item, dict):
            for field in ALLOWED_FIELDS_MAP.get("narzedzia", []):
                val = item.get(field)
                if isinstance(val, str) and val.strip():
                    tool_has_text = True
                    break
        elif isinstance(item, str) and item.strip():
            tool_has_text = True

    tech_patterns = ["programowanie", "kodowanie", "software development", "software engineering"]
    tech_phrases = [
        "architektura it", "architektura oprogramowania", "architektura systemowa", "architektura chmurowa", "architektura danych",
        "software architecture", "system architecture", "cloud architecture", "data architecture", "technical architect",
        "projektowanie architektury", "aspekt techniczny", "rozwiązanie techniczne", "wymagania techniczne", "dług techniczny",
        "wiedza techniczna", "zespół techniczny", "lider techniczny", "technical design", "technical debt",
        "technical leadership", "technical requirements", "technical specification", "specyfikacja techniczna", "rysunek techniczny",
        "dokumentacja techniczna", "technical documentation"
    ]
    has_technical_proof = (tech_has_text or tool_has_text) or any(
        any(p in t.lower() for p in tech_patterns) or any(p in t.lower() for p in tech_phrases)
        for t, _ in candidate_texts
    )

    # Narrative Variant Strategy Building and proof validation
    has_manager_proof = (
        candidate_profile.management_years > 0
        or candidate_profile.managed_people_max > 0
        or has_any_team_proof
    ) and (candidate_level != RoleLevel.UNKNOWN)

    director_evidence_points = 0
    if candidate_profile.candidate_level >= RoleLevel.DIRECTOR:
        director_evidence_points += 1
    if candidate_profile.has_strategy_responsibility:
        director_evidence_points += 1
    if has_budget_responsibility or has_p_and_l_proof:
        director_evidence_points += 1
    if has_managing_managers_proof:
        director_evidence_points += 1
    if has_board_proof:
        director_evidence_points += 1

    has_director_proof = (director_evidence_points >= 2) and (candidate_level != RoleLevel.UNKNOWN)

    rec_narrative = "EXPERT"
    if res.strategy == PositioningStrategy.CONTROLLED_FLATTENING:
        rec_narrative = "EXPERT"
    elif res.strategy == PositioningStrategy.CONTROLLED_ELEVATION:
        rec_narrative = "MANAGER"
    elif res.strategy == PositioningStrategy.MANAGER_LEADERSHIP:
        rec_narrative = "MANAGER"
    elif res.strategy in (PositioningStrategy.DIRECTOR_SCALE, PositioningStrategy.EXECUTIVE_ENTERPRISE):
        rec_narrative = "DIRECTOR"

    # Fallback to EXPERT narrative if the recommended variant is not supported
    if rec_narrative == "MANAGER" and not has_manager_proof:
        rec_narrative = "EXPERT"
    elif rec_narrative == "DIRECTOR" and not has_director_proof:
        rec_narrative = "EXPERT"

    # EXPERT VARIANT
    level_gap_expert = candidate_profile.candidate_level.value - RoleLevel.EXPERT.value
    expert_risk = "NONE"
    if level_gap_expert > 0:
        expert_risk = "MEDIUM"
    elif level_gap_expert < 0:
        expert_risk = "MEDIUM"

    has_problem_solving_proof = any(any(kw in t.lower() for kw in ["samodzieln", "skomplikowan", "problem", "optymalizac"]) for t, _ in candidate_texts)
    has_advisor_proof = any(any(kw in t.lower() for kw in ["doradc", "konsult", "advisor", "consultant"]) for t, _ in candidate_texts)
    has_kpi_proof = has_sales_result_responsibility or any(entry.get("wynikLiczbowy") for entry in wpisy)

    expert_emp = []
    for item in direct_matches:
        expert_emp.append(f"Potwierdzona kompetencja: {item.name}")
    for item in transferable_matches:
        expert_emp.append(f"Kompetencja transferowalna: {item.name}")

    if has_technical_proof:
        expert_emp.append("Zaawansowane kompetencje merytoryczne i techniczne")
    else:
        if len(direct_matches) > 0 or len(transferable_matches) > 0:
            expert_emp.append("Zaawansowane kompetencje merytoryczne")

    if has_problem_solving_proof:
        expert_emp.append("Samodzielne rozwiązywanie skomplikowanych problemów")
    if has_advisor_proof:
        expert_emp.append("Współpraca z biznesem jako doradca merytoryczny")
    if has_sales_result_responsibility or any(entry.get("wynikLiczbowy") for entry in wpisy):
        expert_emp.append("Konkretne rezultaty ilościowe i wolumeny (bez skali zespołu)")

    expert_flat = []
    if (managed_people_max > 0) or has_any_team_proof or (management_years > 0):
        expert_flat.append("Liczba bezpośrednich podwładnych (zarządzanie zespołem)")
    if has_budget_responsibility or has_p_and_l_proof:
        expert_flat.append("Zadania stricte administracyjne i budżetowanie struktury")
    if managed_units_max > 0:
        expert_flat.append("Zarządzanie strukturą rozproszoną lub wieloma jednostkami")
    if has_strategy_responsibility:
        expert_flat.append("Określanie kierunków strategicznych organiacji")

    expert_variant = NarrativeVariantSchema(
        type="EXPERT",
        availability="AVAILABLE" if candidate_level != RoleLevel.UNKNOWN else "UNSUPPORTED",
        recommended=(rec_narrative == "EXPERT" and candidate_level != RoleLevel.UNKNOWN),
        title_positioning="Dostosować do roli eksperckiej, unikać słów kluczowych związanych z zarządzaniem ludźmi." if candidate_level != RoleLevel.UNKNOWN else None,
        summary_positioning="Podsumowanie sformułowane w tonie wybitnego specjalisty, bez nacisku na hierarchię stanowisk." if candidate_level != RoleLevel.UNKNOWN else None,
        elements_to_emphasize=expert_emp if candidate_level != RoleLevel.UNKNOWN else [],
        elements_to_flatten=expert_flat if candidate_level != RoleLevel.UNKNOWN else [],
        elements_to_remove_or_deprioritize=[
            "Określenia sugerujące wyłączne delegowanie zadań innym",
        ] if candidate_level != RoleLevel.UNKNOWN else [],
        risk_level=expert_risk,
        limitations=[] if candidate_level != RoleLevel.UNKNOWN else ["Wariant niedostępny z powodu nieznanego poziomu kandydata."]
    )

    # MANAGER VARIANT
    if has_manager_proof:
        level_gap_mgr = candidate_profile.candidate_level.value - RoleLevel.MANAGER.value
        mgr_risk = "NONE"
        if level_gap_mgr > 0:
            mgr_risk = "MEDIUM"
        elif level_gap_mgr < 0:
            mgr_risk = "HIGH"

        mgr_emp = []
        if has_kpi_proof:
            mgr_emp.append("Efektywność operacyjna i realizacja celów biznesowych KPI")
        else:
            mgr_emp.append("Efektywność operacyjna i realizacja celów operacyjnych")

        if has_any_team_proof or managed_people_max > 0:
            mgr_emp.append("Zarządzanie zespołem (wielkość i struktura zespołu)")

        has_coaching_proof = any(any(kw in t.lower() for kw in ["mentor", "coach", "szkoleni", "training"]) for t, _ in candidate_texts)
        if has_coaching_proof:
            mgr_emp.append("Rozwój kompetencji pracowników (mentoring, coaching)")

        if has_budget_responsibility or has_p_and_l_proof:
            mgr_emp.append("Budżetowanie i optymalizacja zasobów")

        mgr_title_parts = ["Dostosować tytuł zawodowy kontekstowo do nazwy stanowiska w ofercie"]
        if has_any_team_proof or managed_people_max > 0:
            mgr_title_parts.append("z akcentem na kierowanie zespołem")
        manager_title = " ".join(mgr_title_parts) + "."

        mgr_summary_parts = ["Podsumowanie eksponujące operacje i realizację zadań"]
        if has_any_team_proof or managed_people_max > 0:
            mgr_summary_parts.append("zarządzanie ludźmi")
        if has_kpi_proof:
            mgr_summary_parts.append("realizację celów KPI")
        if has_budget_responsibility or has_p_and_l_proof:
            mgr_summary_parts.append("zarządzanie budżetem")

        if len(mgr_summary_parts) > 1:
            manager_summary = mgr_summary_parts[0] + ", w tym: " + ", ".join(mgr_summary_parts[1:]) + "."
        else:
            manager_summary = mgr_summary_parts[0] + "."

        manager_variant = NarrativeVariantSchema(
            type="MANAGER",
            availability="AVAILABLE",
            recommended=(rec_narrative == "MANAGER"),
            title_positioning=manager_title,
            summary_positioning=manager_summary,
            elements_to_emphasize=mgr_emp,
            elements_to_flatten=[
                "Detale operacyjne, zadania czysto techniczne/programistyczne",
                "Długofalowa wizja strategiczna (jeżeli rola ma charakter operacyjny)",
            ],
            elements_to_remove_or_deprioritize=[
                "Nadmierne skupienie na indywidualnym wykonawstwie zadań",
            ],
            risk_level=mgr_risk,
            limitations=[]
        )
    else:
        manager_variant = NarrativeVariantSchema(
            type="MANAGER",
            availability="UNSUPPORTED",
            recommended=False,
            title_positioning=None,
            summary_positioning=None,
            elements_to_emphasize=[],
            elements_to_flatten=[],
            elements_to_remove_or_deprioritize=[],
            risk_level="HIGH",
            limitations=["Brak udokumentowanego doświadczenia kierowniczego (zarządzanie zespołem lub lata menedżerskie)."]
        )

    # DIRECTOR VARIANT
    if has_director_proof:
        level_gap_dir = candidate_profile.candidate_level.value - RoleLevel.DIRECTOR.value
        dir_risk = "NONE"
        if level_gap_dir > 0:
            dir_risk = "MEDIUM"
        elif level_gap_dir < 0:
            dir_risk = "HIGH"

        dir_emphasize = []
        dir_limitations = []

        if candidate_profile.has_strategy_responsibility:
            dir_emphasize.append("Długofalowa strategia biznesowa i wizja")
        else:
            dir_limitations.append("Brak udokumentowanego dowodu odpowiedzialności strategicznej.")

        if has_p_and_l_proof:
            dir_emphasize.append("Pełna odpowiedzialność budżetowa P&L")
        elif has_budget_responsibility:
            dir_emphasize.append("Odpowiedzialność budżetowa")
        else:
            dir_limitations.append("Brak udokumentowanego dowodu odpowiedzialności budżetowej/P&L.")

        if has_managing_managers_proof:
            dir_emphasize.append("Zarządzanie strukturami wielopoziomowymi (zarządzanie menedżerami)")
        else:
            dir_limitations.append("Brak udokumentowanego dowodu zarządzania menedżerami.")

        if has_board_proof:
            dir_emphasize.append("Kluczowe relacje na poziomie Board / Enterprise partners")
        else:
            dir_limitations.append("Brak udokumentowanego dowodu relacji z zarządem/Board.")

        if managed_units_max > 0:
            dir_emphasize.append("Skala organizacyjna i zarządzanie jednostkami")
        else:
            dir_limitations.append("Brak udokumentowanej skali organizacyjnej (jednostki/oddziały).")

        dir_title_parts = ["Eksponować wpływ biznesowy"]
        if has_strategy_responsibility:
            dir_title_parts.append("wpływ strategiczny")
        if has_managing_managers_proof:
            dir_title_parts.append("zarządzanie strukturą wielopoziomową")
        if has_p_and_l_proof or has_budget_responsibility:
            dir_title_parts.append("skalę budżetową P&L")
        if has_board_proof:
            dir_title_parts.append("relacje z zarządem/Board")
        director_title = ", ".join(dir_title_parts) + "."

        dir_sum_parts = ["Podsumowanie skoncentrowane na skali biznesu"]
        if has_strategy_responsibility:
            dir_sum_parts.append("długofalowej strategii")
        if has_p_and_l_proof or has_budget_responsibility:
            dir_sum_parts.append("odpowiedzialności budżetowej")
        if has_managing_managers_proof:
            dir_sum_parts.append("zarządzaniu wieloma zespołami")
        if has_board_proof:
            dir_sum_parts.append("współpracy z zarządem")
        director_summary = ", ".join(dir_sum_parts) + "."

        director_variant = NarrativeVariantSchema(
            type="DIRECTOR",
            availability="AVAILABLE",
            recommended=(rec_narrative == "DIRECTOR"),
            title_positioning=director_title,
            summary_positioning=director_summary,
            elements_to_emphasize=dir_emphasize,
            elements_to_flatten=[
                "Szczegóły techniczne i operacyjne poszczególnych zadań",
                "Proste procesy wykonawcze i narzędzia pracy codziennej",
            ],
            elements_to_remove_or_deprioritize=[
                "Prace i opisy o niskiej wartości dodanej dla poziomu strategicznego",
            ],
            risk_level=dir_risk,
            limitations=dir_limitations
        )
    else:
        director_variant = NarrativeVariantSchema(
            type="DIRECTOR",
            availability="UNSUPPORTED",
            recommended=False,
            title_positioning=None,
            summary_positioning=None,
            elements_to_emphasize=[],
            elements_to_flatten=[],
            elements_to_remove_or_deprioritize=[],
            risk_level="HIGH",
            limitations=["Brak udokumentowanej skali dyrektorskiej (zarządzanie jednostkami, budżet P&L, strategia)."]
        )

    # Narrative recommendations rules when UNKNOWN candidate level or insufficient data
    if candidate_level == RoleLevel.UNKNOWN or insufficient:
        expert_variant.availability = "UNSUPPORTED"
        expert_variant.recommended = False
        expert_variant.title_positioning = None
        expert_variant.summary_positioning = None
        expert_variant.elements_to_emphasize = []
        expert_variant.elements_to_flatten = []
        expert_variant.elements_to_remove_or_deprioritize = []
        expert_variant.limitations = ["Brak wiarygodnego poziomu kandydata do pozycjonowania."]

        manager_variant.availability = "UNSUPPORTED"
        manager_variant.recommended = False
        manager_variant.title_positioning = None
        manager_variant.summary_positioning = None
        manager_variant.elements_to_emphasize = []
        manager_variant.elements_to_flatten = []
        manager_variant.elements_to_remove_or_deprioritize = []
        manager_variant.limitations = ["Brak wiarygodnego poziomu kandydata do pozycjonowania."]

        director_variant.availability = "UNSUPPORTED"
        director_variant.recommended = False
        director_variant.title_positioning = None
        director_variant.summary_positioning = None
        director_variant.elements_to_emphasize = []
        director_variant.elements_to_flatten = []
        director_variant.elements_to_remove_or_deprioritize = []
        director_variant.limitations = ["Brak wiarygodnego poziomu kandydata do pozycjonowania."]

        rec_narrative = None

    # Strategy fields setup
    elements_to_remove = []
    for rule in blocked_rules:
        if rule.lower() in job_desc_lower:
            elements_to_remove.append(rule)

    if rec_narrative is None:
        strategy_schema = StrategySchema(
            recommendation="Brak wiarygodnego poziomu kandydata do pozycjonowania.",
            recommended_narrative=None,
            elements_to_emphasize=[],
            elements_to_flatten=[],
            elements_to_remove_or_deprioritize=[],
            title_positioning=None,
            summary_positioning=None,
            narrative_variants=[expert_variant, manager_variant, director_variant]
        )
    else:
        chosen_variant = expert_variant
        if rec_narrative == "MANAGER" and manager_variant.availability == "AVAILABLE":
            chosen_variant = manager_variant
        elif rec_narrative == "DIRECTOR" and director_variant.availability == "AVAILABLE":
            chosen_variant = director_variant

        strategy_schema = StrategySchema(
            recommendation=" | ".join(res.reasons),
            recommended_narrative=rec_narrative,
            elements_to_emphasize=chosen_variant.elements_to_emphasize,
            elements_to_flatten=chosen_variant.elements_to_flatten,
            elements_to_remove_or_deprioritize=elements_to_remove,
            title_positioning=chosen_variant.title_positioning,
            summary_positioning=chosen_variant.summary_positioning,
            narrative_variants=[expert_variant, manager_variant, director_variant]
        )

    if len(wpisy) < 2:
        limit_msgs.append("Biblioteka Prawdy zawiera mało danych historycznych do pełnej analizy.")

    seen_limits = set()
    unique_limit_msgs = []
    for msg in limit_msgs:
        if msg.strip() and msg.strip() not in seen_limits:
            unique_limit_msgs.append(msg.strip())
            seen_limits.add(msg.strip())

    return CareerPositioningResponse(
        generated_at=now,
        job_id=job_id,
        job_title=job_title,
        positioning=PositioningDetailsSchema(
            detected_job_level=res.job_level.name,
            candidate_profile_level=candidate_profile.candidate_level.name,
            level_gap=res.level_gap,
            positioning_strategy=res.strategy.name,
            title_treatment=res.title_treatment.name,
            fit_level=fit_level,
            fit_score=float(fit_score),
            confidence=float(res.confidence),
            requires_human_review=res.requires_human_review or (candidate_level == RoleLevel.UNKNOWN),
        ),
        risks=RisksSchema(
            overqualification=overqualification_risk,
            underqualification=underqualification_risk,
            stability_concern=stability_concern,
            sector_gap=sector_gap,
            functional_gap=functional_gap,
        ),
        competencies=CompetenciesSchema(
            direct_matches=direct_matches,
            transferable_matches=transferable_matches,
            missing_critical=missing_critical,
            excessive_for_role=excessive_for_role,
        ),
        strategy=strategy_schema,
        evidence=EvidenceSchema(
            used_truth_items_count=len(wpisy) + len(direct_matches) + len(transferable_matches) + (1 if has_sector_match else 0),
            direct_evidence_count=len(direct_matches),
            transferable_evidence_count=len(transferable_matches),
            unresolved_items_count=len(missing_critical),
        ),
        limitations=LimitationsSchema(
            insufficient_data=insufficient,
            messages=unique_limit_msgs,
        ),
    )
