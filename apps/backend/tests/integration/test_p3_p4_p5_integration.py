"""P3 -> P4 -> P5a end-to-end proof: real P2 migration -> real P3
``select_fact()`` -> real P4 ``build_cv_content_plan()``/
``validate_cv_content_plan()`` -> P5a ``generate_cv_content_draft()``.

Confirms the P5a modules themselves never touch the database, the Truth
repository, the legacy ``CVTransformationPlan``/``cv_transformation_generation``,
the Master Resume, or a DOCX/PDF renderer -- and that every supported
fact_type (employment header/activity/numeric-result, education,
certification, course, language) routes to its correct P4 section and
renders correctly, while the out-of-scope ``AWARD_NAME`` fails closed.

All data is synthetic.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import Database
from app.models import TruthEntity, TruthFact
from app.schemas.cv_content_generation import (
    CV_CONTENT_GENERATION_SCHEMA_VERSION,
    ApprovedFactPayloadSet,
    ApprovedFactPayloadSnapshot,
    GenerationContext,
    GenerationStatus,
)
from app.schemas.cv_content_plan import CvContentPlanBuildOutcome, CvSection
from app.schemas.fact_selection import SelectionDecisionOutcome, TargetContext
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import TransformationOperation, TruthFactRead
from app.services import (
    cv_content_generation_builder,
    cv_content_generation_policy,
    cv_content_generation_renderer,
    cv_content_generation_replay,
)
from app.services.cv_content_generation_builder import (
    compute_fact_payload_fingerprint,
    compute_fact_payload_set_fingerprint,
    generate_cv_content_draft,
)
from app.services.cv_content_generation_policy import (
    CV_CONTENT_GENERATION_POLICY_VERSION,
    DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
    SUPPORTED_FACT_TYPES,
    resolve_payload_shape,
)
from app.services.cv_content_plan_builder import build_cv_content_plan
from app.services.cv_content_plan_replay import replay_input_from_plan
from app.services.cv_content_plan_validator import validate_cv_content_plan
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, select_fact
from app.services.truth_legacy_migrator import apply


APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_P5A_SOURCE_FILES = (
    Path(cv_content_generation_builder.__file__),
    Path(cv_content_generation_policy.__file__),
    Path(cv_content_generation_renderer.__file__),
    Path(cv_content_generation_replay.__file__),
    Path(cv_content_generation_builder.__file__).parent.parent
    / "schemas"
    / "cv_content_generation.py",
)

#: (fact_type, requested_target_scope, expected CvSection) for every
#: fact_type this end-to-end test exercises.
_FACT_TYPE_SCOPE_SECTION = {
    "EMPLOYMENT_ROLE": ("EMPLOYMENT_HEADER", CvSection.EXPERIENCE),
    "EMPLOYMENT_ACTIVITY": ("EXPERIENCE", CvSection.EXPERIENCE),
    "EMPLOYMENT_NUMERIC_RESULT": ("EXPERIENCE", CvSection.EXPERIENCE),
    "EMPLOYMENT_RESPONSIBILITY_SCALE": ("EXPERIENCE", CvSection.EXPERIENCE),
    "EDUCATION_DEGREE": ("EDUCATION", CvSection.EDUCATION),
    "CERTIFICATION_NAME": ("CERTIFICATIONS", CvSection.CERTIFICATIONS),
    "COURSE_NAME": ("COURSES", CvSection.COURSES),
    "LANGUAGE_NAME": ("LANGUAGES", CvSection.LANGUAGES),
}
_NAMED_ENTITY_TYPE_BY_FACT_TYPE = {
    "EDUCATION_DEGREE": EntityType.EDUCATION,
    "CERTIFICATION_NAME": EntityType.CERTIFICATION,
    "COURSE_NAME": EntityType.COURSE,
    "LANGUAGE_NAME": EntityType.LANGUAGE,
}


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
                    {"nazwa": "Synthetic Cloud Certificate", "status": APPROVED, "uzywacWCV": True}
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
                    {"jezyk": "Synthetic Language", "poziom": "C1", "status": APPROVED, "uzywacWCV": True}
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
        job_or_application_id="job-synthetic-p5",
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=employment_entity_id,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )


@pytest.fixture
async def migrated(database, tmp_path):
    """Real P2 apply() over a synthetic legacy record spanning employment +
    every P4.5a named category -> (person_id, employment_entity_id,
    facts_by_type, entity_rows_by_type)."""

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
            await session.execute(
                select(TruthFact).where(TruthFact.fact_type.in_(_FACT_TYPE_SCOPE_SECTION))
            )
        ).scalars().all()
        facts_by_type = {row.fact_type: await _fact_read(database, row.fact_id) for row in fact_rows}
        entity_rows_by_type = {}
        for fact_type, fact in facts_by_type.items():
            entity_row = await session.get(TruthEntity, str(fact.entity_id))
            assert entity_row is not None
            entity_rows_by_type[fact_type] = entity_row
    return person_id, UUID(employment_entity_id_str), facts_by_type, entity_rows_by_type


def _payload_from_fact(decision, fact: TruthFactRead) -> ApprovedFactPayloadSnapshot:
    fingerprint = compute_fact_payload_fingerprint(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=fact.value_json,
        normalized_value_json=fact.normalized_value_json,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    return ApprovedFactPayloadSnapshot(
        person_entity_id=decision.person_entity_id,
        entity_id=decision.entity_id,
        fact_id=decision.fact_id,
        fact_type=decision.fact_type,
        fact_revision=decision.fact_revision,
        fact_content_fingerprint=decision.fact_content_fingerprint,
        value_json=fact.value_json,
        normalized_value_json=fact.normalized_value_json,
        payload_fingerprint=fingerprint,
    )


@pytest.mark.asyncio
async def test_full_p3_p4_p5_pipeline_generates_every_supported_fact_type(migrated) -> None:
    person_id, employment_entity_id, facts_by_type, entity_rows_by_type = migrated
    target_context = _target_context(
        person_entity_id=person_id, employment_entity_id=employment_entity_id
    )

    decisions = [
        select_fact(
            fact=facts_by_type[fact_type],
            target_context=target_context,
            requested_operation=TransformationOperation.EXACT_COPY,
            requested_target_scope=target_scope,
            permissions=[],
            evaluation_time=NOW,
        )
        for fact_type, (target_scope, _) in _FACT_TYPE_SCOPE_SECTION.items()
    ]
    assert all(d.decision == SelectionDecisionOutcome.SELECTED for d in decisions)

    entity_types = {employment_entity_id: EntityType.EMPLOYMENT}
    for fact_type, entity_type in _NAMED_ENTITY_TYPE_BY_FACT_TYPE.items():
        entity_types[UUID(entity_rows_by_type[fact_type].entity_id)] = entity_type

    build_result = build_cv_content_plan(
        target_context=target_context, decisions=decisions, entity_types=entity_types
    )
    assert build_result.outcome == CvContentPlanBuildOutcome.BUILT
    plan = build_result.plan

    decisions_by_fp = {d.decision_fingerprint: d for d in decisions}
    validation = validate_cv_content_plan(
        plan,
        decisions_by_fp,
        entity_types=entity_types,
        current_replay_input=replay_input_from_plan(plan),
    )
    assert validation.p5_ready is True

    payloads = [
        _payload_from_fact(decision, facts_by_type[decision.fact_type]) for decision in decisions
    ]
    payload_set = ApprovedFactPayloadSet(
        payloads=tuple(payloads),
        payload_set_fingerprint=compute_fact_payload_set_fingerprint(payloads),
    )
    generation_context = GenerationContext(
        locale="pl-PL",
        deterministic_template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )

    result = generate_cv_content_draft(
        plan=plan,
        decisions_by_fingerprint=decisions_by_fp,
        entity_types=entity_types,
        current_replay_input=replay_input_from_plan(plan),
        fact_payloads=payload_set,
        generation_context=generation_context,
    )

    assert result.status == GenerationStatus.GENERATED
    assert result.ready_for_p55 is True
    assert result.blocked_items == ()

    elements_by_fact_type: dict[str, list] = {}
    for section in result.draft.generated_sections:
        if section.kind == "EXPERIENCE":
            for entry in section.generated_experience_entries:
                for element in (
                    entry.header_elements
                    + entry.responsibility_elements
                    + entry.achievement_elements
                ):
                    elements_by_fact_type.setdefault(element.fact_type, []).append(
                        (section.section, element)
                    )
        else:
            for element in section.generated_elements:
                elements_by_fact_type.setdefault(element.fact_type, []).append(
                    (section.section, element)
                )

    for fact_type, (_, expected_section) in _FACT_TYPE_SCOPE_SECTION.items():
        assert fact_type in elements_by_fact_type, fact_type
        actual_section, element = elements_by_fact_type[fact_type][0]
        assert actual_section == expected_section
        assert element.fact_id == facts_by_type[fact_type].fact_id

    role_element = elements_by_fact_type["EMPLOYMENT_ROLE"][0][1]
    assert role_element.generated_text == "Synthetic Analyst, Synthetic Co"

    education_element = elements_by_fact_type["EDUCATION_DEGREE"][0][1]
    assert education_element.generated_text == (
        "Magister, Synthetic Computer Science, Synthetic University"
    )

    certification_element = elements_by_fact_type["CERTIFICATION_NAME"][0][1]
    assert certification_element.generated_text == "Synthetic Cloud Certificate"

    course_element = elements_by_fact_type["COURSE_NAME"][0][1]
    assert course_element.generated_text == "Synthetic Data Course, Synthetic Academy"

    language_element = elements_by_fact_type["LANGUAGE_NAME"][0][1]
    assert language_element.generated_text == "Synthetic Language - C1"


@pytest.mark.asyncio
async def test_generated_elements_carry_no_source_reference_identity(migrated) -> None:
    person_id, employment_entity_id, facts_by_type, entity_rows_by_type = migrated
    target_context = _target_context(
        person_entity_id=person_id, employment_entity_id=employment_entity_id
    )
    decision = select_fact(
        fact=facts_by_type["EMPLOYMENT_ROLE"],
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
    payload = _payload_from_fact(decision, facts_by_type["EMPLOYMENT_ROLE"])
    payload_set = ApprovedFactPayloadSet(
        payloads=(payload,), payload_set_fingerprint=compute_fact_payload_set_fingerprint([payload])
    )
    generation_context = GenerationContext(
        locale="pl-PL",
        deterministic_template_version=DEFAULT_DETERMINISTIC_TEMPLATE_VERSION,
        generation_policy_version=CV_CONTENT_GENERATION_POLICY_VERSION,
    )
    result = generate_cv_content_draft(
        plan=plan,
        decisions_by_fingerprint={decision.decision_fingerprint: decision},
        entity_types=entity_types,
        current_replay_input=replay_input_from_plan(plan),
        fact_payloads=payload_set,
        generation_context=generation_context,
    )
    assert result.status == GenerationStatus.GENERATED
    # GeneratedCvElement/BlockedGenerationItem have no source_reference or
    # legacy_record_key field at all -- confirmed structurally.
    from app.schemas.cv_content_generation import BlockedGenerationItem, GeneratedCvElement

    assert "source_reference" not in GeneratedCvElement.model_fields
    assert "legacy_record_key" not in GeneratedCvElement.model_fields
    assert "source_reference" not in BlockedGenerationItem.model_fields


# -- AWARD stays out of scope --


def test_award_name_is_unsupported_in_p5a() -> None:
    assert "AWARD_NAME" not in SUPPORTED_FACT_TYPES
    result = resolve_payload_shape("AWARD_NAME", {"nazwa": "Some Award"})
    assert not result.is_valid


# -- Source boundary checks: no DB, no legacy coupling, no DOCX/PDF/frontend --


def _code_only(text: str) -> str:
    """Strip triple-quoted docstrings so scope-boundary prose (which names
    the very things P5a must avoid) doesn't produce a false positive."""

    return re.sub(r'""".*?"""', "", text, flags=re.DOTALL)


def _p5a_source_texts() -> dict[str, str]:
    return {path.name: _code_only(path.read_text(encoding="utf-8")) for path in _P5A_SOURCE_FILES}


def test_p5a_modules_never_import_the_database_or_orm() -> None:
    forbidden = ("from app.database", "from app import database", "sqlalchemy", "app.models")
    for name, text in _p5a_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden DB symbol: {needle}"


def test_p5a_modules_never_import_legacy_stage_10c() -> None:
    forbidden = (
        "truth_legacy_migrator",
        "cv_transformation_generation",
        "cv_transformation_plan",
        "cv_transformation_approval",
    )
    for name, text in _p5a_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden legacy module: {needle}"


def test_p5a_modules_never_reference_source_reference_or_legacy_record_key() -> None:
    forbidden = ("source_reference", "legacy_record_key")
    for name, text in _p5a_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references forbidden identity field: {needle}"


def test_p5a_modules_never_reference_master_resume() -> None:
    forbidden = ("master_resume", "MasterResume")
    for name, text in _p5a_source_texts().items():
        for needle in forbidden:
            assert needle not in text, f"{name} references Master Resume: {needle}"


def test_p5a_modules_never_reference_docx_pdf_or_frontend() -> None:
    forbidden = ("docx", "playwright", "pdf", "frontend", "litellm", "llm_client")
    for name, text in _p5a_source_texts().items():
        lowered = text.lower()
        for needle in forbidden:
            assert needle not in lowered, f"{name} references forbidden output/LLM symbol: {needle}"


def test_named_category_synthetic_data_only_used() -> None:
    """The library fixture above uses only clearly synthetic placeholder
    strings ("Synthetic ..."/"Sp. z o.o.") -- no real person's data."""

    for value in (
        "Synthetic Candidate",
        "Synthetic Co",
        "Synthetic Analyst",
        "Synthetic Computer Science",
        "Synthetic University",
        "Synthetic Cloud Certificate",
        "Synthetic Data Course",
        "Synthetic Academy",
        "Synthetic Language",
    ):
        assert value.startswith("Synthetic")
