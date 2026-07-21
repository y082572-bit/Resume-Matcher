"""Stage P3.5 end-to-end proof: synthetic legacy record -> P2 apply() ->
TruthEntity(EMPLOYMENT) -> TruthFact with a correct employment_scope_entity_id
-> P3 select_fact() -> SELECTED for an allowed operation.

Confirms the full chain uses only stable ids (fact_id/entity_id) and real P1
schemas -- no legacy Truth Library lookup or text matching happens anywhere
in the P3 selection step (that step never even sees the legacy JSON or
``truth_library_path``; see also ``test_fact_selection_no_legacy_coupling.py``).
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
from app.schemas.fact_selection import SelectionDecisionOutcome, TargetContext
from app.schemas.truth_fact import (
    PermissionStatus,
    TransformationOperation,
    TruthFactRead,
    TruthPermissionRead,
)
from app.services.fact_selection_policy import SELECTION_POLICY_VERSION, select_fact
from app.services.truth_legacy_migrator import apply

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"
SOURCE_ID = "truth_library_primary"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def _target_context(*, person_entity_id: str, employment_entity_id: str) -> TargetContext:
    return TargetContext(
        target_context_id=uuid4(),
        person_entity_id=person_entity_id,
        job_or_application_id=None,
        target_role_title=None,
        target_role_family=None,
        target_seniority=None,
        target_organization_or_industry=None,
        employment_entity_id=employment_entity_id,
        selection_policy_version=SELECTION_POLICY_VERSION,
    )


def _permission(*, fact_id, target_scope: str, operations: list[TransformationOperation]) -> TruthPermissionRead:
    return TruthPermissionRead(
        schema_version="1.0",
        revision=1,
        content_fingerprint="b" * 64,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
        permission_id=uuid4(),
        fact_id=fact_id,
        target_scope=target_scope,
        allowed_operations=operations,
        constraints_json={},
        status=PermissionStatus.ACTIVE,
        approved_by=None,
        approved_at=None,
        valid_from=None,
        valid_until=None,
    )


@pytest.fixture
async def migrated(database, tmp_path):
    """Run the real P2 apply() against a synthetic legacy record and return
    (person_entity_id, employment_entity_id, facts_by_type)."""

    person_id = str(uuid4())
    path = _write_library(tmp_path)
    await apply(
        database=database, truth_library_path=path,
        person_entity_id=person_id, legacy_source_id=SOURCE_ID,
    )
    async with database._session() as session:
        employment_entity = (
            await session.execute(
                select(TruthEntity).where(TruthEntity.entity_type == "EMPLOYMENT")
            )
        ).scalar_one()
        employment_entity_id_str = employment_entity.entity_id
        fact_rows = (
            await session.execute(
                select(TruthFact).where(TruthFact.entity_id == employment_entity_id_str)
            )
        ).scalars().all()
    facts_by_type = {row.fact_type: await _fact_read(database, row.fact_id) for row in fact_rows}
    return person_id, UUID(employment_entity_id_str), facts_by_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fact_type,target_scope",
    [
        ("EMPLOYMENT_ROLE", "EMPLOYMENT_HEADER"),
        ("EMPLOYMENT_ACTIVITY", "EXPERIENCE"),
        ("EMPLOYMENT_NUMERIC_RESULT", "EXPERIENCE"),
        ("EMPLOYMENT_RESPONSIBILITY_SCALE", "EXPERIENCE"),
    ],
)
async def test_migrated_employment_fact_is_selected_for_exact_copy(
    migrated, fact_type, target_scope
) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    fact = facts_by_type[fact_type]

    assert fact.entity_id == employment_entity_id
    assert fact.employment_scope_entity_id == employment_entity_id

    decision = select_fact(
        fact=fact,
        target_context=_target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id),
        requested_operation=TransformationOperation.EXACT_COPY,
        requested_target_scope=target_scope,
        permissions=[],
        evaluation_time=NOW,
    )

    assert decision.decision == SelectionDecisionOutcome.SELECTED
    assert decision.entity_id == employment_entity_id
    assert decision.employment_scope_entity_id == employment_entity_id
    assert decision.fact_type == fact_type


@pytest.mark.asyncio
async def test_numeric_result_flatten_without_permission_requires_approval(migrated) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    fact = facts_by_type["EMPLOYMENT_NUMERIC_RESULT"]

    decision = select_fact(
        fact=fact,
        target_context=_target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id),
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        requested_target_scope="EXPERIENCE",
        permissions=[],
        evaluation_time=NOW,
    )

    assert decision.decision == SelectionDecisionOutcome.APPROVAL_REQUIRED
    assert decision.requires_user_approval is True


@pytest.mark.asyncio
async def test_numeric_result_flatten_with_active_permission_is_selected(migrated) -> None:
    person_id, employment_entity_id, facts_by_type = migrated
    fact = facts_by_type["EMPLOYMENT_NUMERIC_RESULT"]
    permission = _permission(
        fact_id=fact.fact_id,
        target_scope="EXPERIENCE",
        operations=[TransformationOperation.FLATTEN_FOR_LOWER_ROLE],
    )

    decision = select_fact(
        fact=fact,
        target_context=_target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id),
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission],
        evaluation_time=NOW,
    )

    assert decision.decision == SelectionDecisionOutcome.SELECTED
    assert decision.allowed_operation == TransformationOperation.FLATTEN_FOR_LOWER_ROLE


@pytest.mark.asyncio
async def test_responsibility_scale_flatten_is_blocked_by_registry_even_with_permission(
    migrated,
) -> None:
    """EMPLOYMENT_RESPONSIBILITY_SCALE never gained FLATTEN_FOR_LOWER_ROLE in
    the registry -- an ACTIVE permission naming that exact operation must
    still be denied: permission can only narrow what the registry allows,
    never extend it."""

    person_id, employment_entity_id, facts_by_type = migrated
    fact = facts_by_type["EMPLOYMENT_RESPONSIBILITY_SCALE"]
    permission = _permission(
        fact_id=fact.fact_id,
        target_scope="EXPERIENCE",
        operations=[TransformationOperation.FLATTEN_FOR_LOWER_ROLE],
    )

    decision = select_fact(
        fact=fact,
        target_context=_target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id),
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission],
        evaluation_time=NOW,
    )

    assert decision.decision == SelectionDecisionOutcome.BLOCKED
    assert decision.allowed_operation is None


@pytest.mark.asyncio
async def test_permission_cannot_widen_registry_for_employment_activity(migrated) -> None:
    """Same narrowing-only guarantee for EMPLOYMENT_ACTIVITY, which also never
    gained FLATTEN_FOR_LOWER_ROLE."""

    person_id, employment_entity_id, facts_by_type = migrated
    fact = facts_by_type["EMPLOYMENT_ACTIVITY"]
    permission = _permission(
        fact_id=fact.fact_id,
        target_scope="EXPERIENCE",
        operations=[TransformationOperation.FLATTEN_FOR_LOWER_ROLE],
    )

    decision = select_fact(
        fact=fact,
        target_context=_target_context(person_entity_id=person_id, employment_entity_id=employment_entity_id),
        requested_operation=TransformationOperation.FLATTEN_FOR_LOWER_ROLE,
        requested_target_scope="EXPERIENCE",
        permissions=[permission],
        evaluation_time=NOW,
    )

    assert decision.decision == SelectionDecisionOutcome.BLOCKED
