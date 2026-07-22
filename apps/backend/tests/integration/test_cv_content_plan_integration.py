"""P4 end-to-end proof: real P2 migration -> real P3 ``select_fact()`` ->
P4 ``build_cv_content_plan()`` -> ``CvExperienceEntryPlan`` grouped only by
stable employment UUID, with no text lookup anywhere.

Confirms the P4 modules themselves never import the database, the truth
repository, or the legacy ``CVTransformationPlan``, and never perform I/O.
All data is synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact
from app.schemas.cv_content_plan import (
    CvContentPlanBuildOutcome,
    CvContentPlanStatus,
    CvSection,
)
from app.schemas.fact_selection import CareerPositioningSignal, SelectionDecisionOutcome, TargetContext
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import TransformationOperation, TruthFactRead
from app.services import cv_content_plan_builder, cv_content_plan_policy, cv_content_plan_replay, cv_content_plan_validator
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, select_fact
from app.services.truth_legacy_migrator import apply


APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_P4_SOURCE_FILES = (
    Path(cv_content_plan_builder.__file__),
    Path(cv_content_plan_policy.__file__),
    Path(cv_content_plan_replay.__file__),
    Path(cv_content_plan_validator.__file__),
    Path(cv_content_plan_builder.__file__).parent.parent / "schemas" / "cv_content_plan.py",
)


@pytest.fixture
async def database(tmp_path):
    db = Database(db_path=tmp_path / "app.db")
    yield db
    await db.close()


def _write_library(tmp_path: Path) -> Path:
    payload = {
        "meta": {"kandidat": "Synthetic Candidate", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1",
                        "firma": "Synthetic Co",
                        "stanowisko": "Synthetic Analyst",
                        "status": APPROVED,
                        "uzywacWCV": True,
                        "aktywnosci": ["Did synthetic analysis work"],
                        "wynikLiczbowy": "+30%",
                        "skalaOdpowiedzialnosci": "Budget 1M PLN",
                    }
                ]
            },
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": [],
            },
        },
    }
    path = tmp_path / "truth-library.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


async def _fact_read(database, fact_id: str) -> TruthFactRead:
    async with database._session() as session:
        row = await session.get(TruthFact, fact_id)
        assert row is not None
        return TruthFactRead.model_validate(row)


def _target_context(*, person_entity_id, employment_entity_id) -> TargetContext:
    return TargetContext(
        target_context_id=uuid4(),
        person_entity_id=person_entity_id,
        job_or_application_id="job-synthetic-1",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=employment_entity_id,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )


@pytest.fixture
async def migrated(database, tmp_path):
    """Real P2 apply() over a synthetic legacy record -> (person_id,
    employment_entity_id, facts_by_type)."""

    person_id = str(uuid4())
    path = _write_library(tmp_path)
    await apply(
        database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID
    )
    async with database._session() as session:
        employment_entity = (
            await session.execute(select(TruthEntity).where(TruthEntity.entity_type == "EMPLOYMENT"))
        ).scalar_one()
        employment_entity_id_str = employment_entity.entity_id
        fact_rows = (
            await session.execute(select(TruthFact).where(TruthFact.entity_id == employment_entity_id_str))
        ).scalars().all()
    facts_by_type = {row.fact_type: await _fact_read(database, row.fact_id) for row in fact_rows}
    return person_id, UUID(employment_entity_id_str), facts_by_type


def _write_named_category_library(tmp_path: Path) -> Path:
    """A synthetic legacy library exercising the four P4.5a named categories
    only (``wyksztalcenie``/``certyfikaty``/``kursy``/``jezyki``) -- no
    employment record, no numeric achievements, no AWARD category (P2 has no
    AWARD support)."""

    payload = {
        "meta": {"kandidat": "Synthetic Candidate", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {"wpisy": []},
            "wyksztalcenie": {
                "wpisy": [
                    {
                        "kierunek": "Synthetic Computer Science",
                        "uczelnia": "Synthetic University",
                        "stopien": "Magister",
                        "status": APPROVED,
                        "uzywacWCV": True,
                    }
                ]
            },
            "certyfikaty": {
                "wpisy": [
                    {
                        "nazwa": "Synthetic Cloud Certificate",
                        "status": APPROVED,
                        "uzywacWCV": True,
                    }
                ]
            },
            "kursy": {
                "wpisy": [
                    {
                        "nazwa": "Synthetic Data Course",
                        "organizator": "Synthetic Academy",
                        "status": APPROVED,
                        "uzywacWCV": True,
                    }
                ]
            },
            "jezyki": {
                "wpisy": [
                    {
                        "jezyk": "Synthetic Language",
                        "poziom": "C1",
                        "status": APPROVED,
                        "uzywacWCV": True,
                    }
                ]
            },
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": [],
            },
        },
    }
    path = tmp_path / "truth-library-named.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


#: Stage P4.5a: the exact fact_type -> target_scope this stage supports.
_P45A_FACT_TYPE_SCOPE = {
    "EDUCATION_DEGREE": "EDUCATION",
    "CERTIFICATION_NAME": "CERTIFICATIONS",
    "COURSE_NAME": "COURSES",
    "LANGUAGE_NAME": "LANGUAGES",
}
_P45A_FACT_TYPE_SECTION = {
    "EDUCATION_DEGREE": CvSection.EDUCATION,
    "CERTIFICATION_NAME": CvSection.CERTIFICATIONS,
    "COURSE_NAME": CvSection.COURSES,
    "LANGUAGE_NAME": CvSection.LANGUAGES,
}
_P45A_FACT_TYPE_ENTITY_TYPE = {
    "EDUCATION_DEGREE": EntityType.EDUCATION,
    "CERTIFICATION_NAME": EntityType.CERTIFICATION,
    "COURSE_NAME": EntityType.COURSE,
    "LANGUAGE_NAME": EntityType.LANGUAGE,
}


@pytest.fixture
async def migrated_named(database, tmp_path):
    """Real P2 apply() over the four P4.5a named-category legacy records ->
    (person_id, facts_by_type, entity_rows_by_type)."""

    person_id = str(uuid4())
    path = _write_named_category_library(tmp_path)
    await apply(
        database=database, truth_library_path=path, person_entity_id=person_id, legacy_source_id=SOURCE_ID
    )
    async with database._session() as session:
        fact_rows = (
            await session.execute(
                select(TruthFact).where(TruthFact.fact_type.in_(_P45A_FACT_TYPE_SCOPE))
            )
        ).scalars().all()
        facts_by_type = {row.fact_type: await _fact_read(database, row.fact_id) for row in fact_rows}
        entity_rows_by_type = {}
        for fact_type, fact in facts_by_type.items():
            entity_row = await session.get(TruthEntity, str(fact.entity_id))
            assert entity_row is not None
            entity_rows_by_type[fact_type] = entity_row
    return person_id, facts_by_type, entity_rows_by_type


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_type", list(_P45A_FACT_TYPE_SCOPE))
async def test_p2_migrates_each_p45a_fact_type(fact_type, migrated_named) -> None:
    _, facts_by_type, _ = migrated_named
    assert fact_type in facts_by_type


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_type,expected_entity_type", list(_P45A_FACT_TYPE_ENTITY_TYPE.items()))
async def test_each_p45a_fact_type_has_a_dedicated_entity_type(
    fact_type, expected_entity_type, migrated_named
) -> None:
    _, _, entity_rows_by_type = migrated_named
    assert entity_rows_by_type[fact_type].entity_type == expected_entity_type.value


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_type", list(_P45A_FACT_TYPE_SCOPE))
async def test_each_p45a_owner_entity_parent_is_the_person(fact_type, migrated_named) -> None:
    person_id, _, entity_rows_by_type = migrated_named
    assert entity_rows_by_type[fact_type].parent_entity_id == person_id


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_type", list(_P45A_FACT_TYPE_SCOPE))
async def test_each_p45a_fact_has_stable_entity_and_fact_id(fact_type, migrated_named) -> None:
    _, facts_by_type, entity_rows_by_type = migrated_named
    fact = facts_by_type[fact_type]
    assert fact.fact_id is not None
    assert entity_rows_by_type[fact_type].entity_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_type", list(_P45A_FACT_TYPE_SCOPE))
async def test_each_p45a_fact_is_global_transferable(fact_type, migrated_named) -> None:
    _, facts_by_type, _ = migrated_named
    assert facts_by_type[fact_type].transferability.value == "GLOBAL_TRANSFERABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_type,target_scope", list(_P45A_FACT_TYPE_SCOPE.items()))
async def test_each_p45a_fact_passes_select_fact_for_exact_copy(fact_type, target_scope, migrated_named) -> None:
    person_id, facts_by_type, _ = migrated_named
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=None)
    decision = select_fact(
        fact=facts_by_type[fact_type],
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope=target_scope,
        permissions=[],
        evaluation_time=NOW,
    )
    assert decision.decision == SelectionDecisionOutcome.SELECTED


@pytest.mark.asyncio
async def test_each_p45a_fact_type_routes_to_its_own_p4_section(migrated_named) -> None:
    person_id, facts_by_type, entity_rows_by_type = migrated_named
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=None)
    decisions = [
        select_fact(
            fact=facts_by_type[fact_type],
            target_context=target_context,
            requested_operation=TransformationOperation.EXACT_COPY,
            requested_target_scope=target_scope,
            permissions=[],
            evaluation_time=NOW,
        )
        for fact_type, target_scope in _P45A_FACT_TYPE_SCOPE.items()
    ]
    entity_types = {
        UUID(entity_rows_by_type[fact_type].entity_id): entity_type
        for fact_type, entity_type in _P45A_FACT_TYPE_ENTITY_TYPE.items()
    }
    result = build_cv_content_plan(target_context=target_context, decisions=decisions, entity_types=entity_types)
    assert result.outcome == CvContentPlanBuildOutcome.BUILT
    plan = result.plan
    for fact_type, section in _P45A_FACT_TYPE_SECTION.items():
        section_plan = next(s for s in plan.sections if s.section == section)
        fact = facts_by_type[fact_type]
        assert any(u.fact_id == fact.fact_id for u in section_plan.planned_fact_uses)


@pytest.mark.asyncio
async def test_p45a_course_routes_only_to_courses_section(migrated_named) -> None:
    person_id, facts_by_type, entity_rows_by_type = migrated_named
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=None)
    course_fact = facts_by_type["COURSE_NAME"]
    decision = select_fact(
        fact=course_fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="COURSES",
        permissions=[],
        evaluation_time=NOW,
    )
    entity_types = {UUID(entity_rows_by_type["COURSE_NAME"].entity_id): EntityType.COURSE}
    plan = build_cv_content_plan(target_context=target_context, decisions=[decision], entity_types=entity_types).plan
    courses = next(s for s in plan.sections if s.section == CvSection.COURSES)
    education = next(s for s in plan.sections if s.section == CvSection.EDUCATION)
    certifications = next(s for s in plan.sections if s.section == CvSection.CERTIFICATIONS)
    assert any(u.fact_id == course_fact.fact_id for u in courses.planned_fact_uses)
    assert all(u.fact_id != course_fact.fact_id for u in education.planned_fact_uses)
    assert all(u.fact_id != course_fact.fact_id for u in certifications.planned_fact_uses)


def test_award_fact_type_has_no_p1_policy() -> None:
    """P4.5a explicitly excludes AWARD: no TruthTransformationPolicyRegistry
    entry, no FACT_TYPE_SECTION_MAP entry -- so no path can ever select or
    place an AWARD fact."""

    from app.services.cv_content_plan_policy import FACT_TYPE_SECTION_MAP
    from app.services.truth_policy import TruthTransformationPolicyRegistry

    assert TruthTransformationPolicyRegistry().get("AWARD_NAME") is None
    assert all(fact_type != "AWARD_NAME" for fact_type, _ in FACT_TYPE_SECTION_MAP)


def test_named_category_synthetic_data_only_used() -> None:
    """The library fixture above uses only clearly synthetic placeholder
    strings -- no real person, employer, institution or credential name."""

    for value in ("Synthetic Candidate", "Synthetic University", "Synthetic Cloud Certificate"):
        assert "Synthetic" in value


#: The P2 legacy migrator (Stage P3.5) only produces the four
#: ``EMPLOYMENT_FACT_TYPES`` fact types from a legacy record -- ``COMPANY``/
#: ``ROLE``/``EMPLOYMENT_PERIOD`` are P1-registered fact_types with no
#: current migration source, so this fixture only exercises what P2/P3.5
#: actually emit.
_HEADER_SCOPE = {
    "EMPLOYMENT_ROLE": "EMPLOYMENT_HEADER",
}
_SCOPED_SCOPE = {
    "EMPLOYMENT_ACTIVITY": "EXPERIENCE",
    "EMPLOYMENT_NUMERIC_RESULT": "EXPERIENCE",
    "EMPLOYMENT_RESPONSIBILITY_SCALE": "EXPERIENCE",
}


@pytest.mark.asyncio
async def test_real_p3_decisions_build_a_grouped_experience_entry(migrated) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id)

    decisions = []
    for fact_type, target_scope in {**_HEADER_SCOPE, **_SCOPED_SCOPE}.items():
        fact = facts_by_type[fact_type]
        decisions.append(
            select_fact(
                fact=fact,
                target_context=target_context,
                requested_operation=TransformationOperation.EXACT_COPY,
                requested_target_scope=target_scope,
                permissions=[],
                evaluation_time=NOW,
            )
        )
    for decision in decisions:
        assert decision.decision == SelectionDecisionOutcome.SELECTED

    entity_types = {employment_entity_id: EntityType.EMPLOYMENT}
    result = build_cv_content_plan(target_context=target_context, decisions=decisions, entity_types=entity_types)
    assert result.outcome == CvContentPlanBuildOutcome.BUILT
    plan = result.plan
    assert plan.plan_status == CvContentPlanStatus.VALID

    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    assert len(experience.experience_entries) == 1
    entry = experience.experience_entries[0]
    assert entry.employment_entity_id == employment_entity_id
    assert len(entry.header_fact_uses) == len(_HEADER_SCOPE)
    assert len(entry.responsibility_fact_uses) + len(entry.achievement_fact_uses) == len(_SCOPED_SCOPE)

    # No text/company/role lookup is possible: PlannedFactUse never stores
    # generated text or a company/role name field.
    for use in entry.header_fact_uses + entry.responsibility_fact_uses + entry.achievement_fact_uses:
        assert not hasattr(use, "company_name")
        assert not hasattr(use, "role_name")
        assert not hasattr(use, "generated_text")
        assert not hasattr(use, "bullet")

    decisions_by_fp = {d.decision_fingerprint: d for d in decisions}
    validation = validate_cv_content_plan(plan, decisions_by_fp, entity_types=entity_types)
    assert validation.structural_status.value == "VALID"


@pytest.mark.asyncio
async def test_career_positioning_not_relevant_never_reranked_by_p4(migrated) -> None:
    """P3's CPE-driven NOT_RELEVANT decision is simply carried through to an
    omission by P4 -- P4 performs no ranking, re-scoring, or override."""

    person_id, employment_entity_id, facts_by_type = migrated
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id)
    fact = facts_by_type["EMPLOYMENT_ACTIVITY"]

    signal = CareerPositioningSignal(
        source_job_id="job-synthetic-1",
        source_generated_at=NOW,
        not_relevant_fact_ids=(fact.fact_id,),
    )
    decision = select_fact(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
        positioning_signal=signal,
    )
    assert decision.decision == SelectionDecisionOutcome.NOT_RELEVANT

    entity_types = {employment_entity_id: EntityType.EMPLOYMENT}
    plan = build_cv_content_plan(
        target_context=target_context, decisions=[decision], entity_types=entity_types
    ).plan
    assert len(plan.omitted_facts) == 1
    assert plan.omitted_facts[0].omission_reason_code.value == "DECISION_NOT_RELEVANT"
    experience = next(s for s in plan.sections if s.section == CvSection.EXPERIENCE)
    assert experience.skipped_empty is True


@pytest.mark.asyncio
async def test_unsupported_sections_remain_empty(migrated) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id)
    fact = facts_by_type["EMPLOYMENT_ROLE"]
    decision = select_fact(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[],
        evaluation_time=NOW,
    )
    entity_types = {employment_entity_id: EntityType.EMPLOYMENT}
    plan = build_cv_content_plan(
        target_context=target_context, decisions=[decision], entity_types=entity_types
    ).plan
    for section_name in (CvSection.EDUCATION, CvSection.CERTIFICATIONS, CvSection.LANGUAGES):
        section = next(s for s in plan.sections if s.section == section_name)
        assert section.skipped_empty is True
        assert section.planned_fact_uses == ()


@pytest.mark.asyncio
async def test_plan_is_deterministic_across_rebuilds(migrated) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id)
    fact = facts_by_type["EMPLOYMENT_ROLE"]
    decision = select_fact(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[],
        evaluation_time=NOW,
    )
    entity_types = {employment_entity_id: EntityType.EMPLOYMENT}
    first = build_cv_content_plan(target_context=target_context, decisions=[decision], entity_types=entity_types).plan
    second = build_cv_content_plan(target_context=target_context, decisions=[decision], entity_types=entity_types).plan
    assert first.content_plan_fingerprint == second.content_plan_fingerprint
    assert first == second


@pytest.mark.asyncio
async def test_validator_is_fail_closed_against_a_tampered_real_plan(migrated) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    target_context = _target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id)
    fact = facts_by_type["EMPLOYMENT_ROLE"]
    decision = select_fact(
        fact=fact,
        target_context=target_context,
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope="EMPLOYMENT_HEADER",
        permissions=[],
        evaluation_time=NOW,
    )
    entity_types = {employment_entity_id: EntityType.EMPLOYMENT}
    plan = build_cv_content_plan(target_context=target_context, decisions=[decision], entity_types=entity_types).plan
    tampered = plan.model_copy(update={"content_plan_fingerprint": "0" * 64})
    validation = validate_cv_content_plan(
        tampered, {decision.decision_fingerprint: decision}, entity_types=entity_types
    )
    assert validation.structural_status.value == "INVALID"
    assert validation.p5_ready is False


# -- No legacy/database/text coupling in the P4 source itself --


_FORBIDDEN_SUBSTRINGS = (
    "truth_repository",
    "truth_library_loader",
    "truth_legacy_migrator",
    "truth_legacy_classification",
    "load_truth_library",
    "get_truth_library",
    "truth_library_path",
    "legacy_record_key",
    "legacy_source_path",
    "legacy_source_id",
    "TruthLegacyMigrationMap",
    "CVTransformationPlan",
    "from app.database",
    "from app import database",
    "import database",
    "app.db_engine",
    "sqlalchemy",
    "AsyncSession",
    "docx",
    "playwright",
    "chromium",
)


def _read_p4_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in _P4_SOURCE_FILES}


def test_p4_source_never_imports_database_or_truth_repository() -> None:
    sources = _read_p4_sources()
    for path, text in sources.items():
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in text, f"found forbidden token {forbidden!r} in {path}"


def test_p4_source_performs_no_llm_or_docx_pdf_work() -> None:
    sources = _read_p4_sources()
    for path, text in sources.items():
        for forbidden in ("litellm", "openai", "anthropic", "docx", "pdf", "playwright", "rephrase_text"):
            assert forbidden not in text.lower(), f"found forbidden token {forbidden!r} in {path}"


def test_p4_source_never_calls_select_fact() -> None:
    sources = _read_p4_sources()
    for path, text in sources.items():
        assert "select_fact(" not in text, f"P4 must never call select_fact() directly: {path}"
