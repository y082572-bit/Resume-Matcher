"""Explicit Provenance Stage P3.5 — employment_scope_entity_id compatibility backfill.

Historical facts created by the P2 legacy migrator before Stage P3.5 never
received ``TruthFact.employment_scope_entity_id`` (see
``truth_legacy_migrator.py``). This module is a deterministic, fail-closed
repair tool for that gap: it never creates a new fact, never mutates
``fact_id``/``entity_id``/``fact_type``/``status``/``use_in_cv``/
``requires_approval``/``transferability``/``value_json``/
``normalized_value_json``/``source_reference``, and never touches
``TruthLegacyMigrationMap``. It only ever sets
``employment_scope_entity_id = fact.entity_id`` on an existing, already
approved employment fact, and only through the existing, audited
``TruthRepository.update_fact`` (expected-revision + content-fingerprint
enforced).

See docs/explicit-provenance-stage-p2.md for the full compatibility design.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from app.models import TruthEntity, TruthFact
from app.schemas.truth_entity import EntityType
from app.schemas.truth_fact import Transferability, TruthFactUpdate
from app.services.truth_policy import EMPLOYMENT_FACT_TYPES


#: Present in every report so a consumer can tell which repair policy
#: version produced it. Does not require (and does not create) a new SQL
#: table or column: it is a constant carried on the report only.
EMPLOYMENT_SCOPE_COMPATIBILITY_VERSION = "employment-scope-compat-v1"


class TruthEmploymentScopeBackfillError(Exception):
    """Base error for the Stage P3.5 employment-scope backfill tool."""


class PersonEntityNotFoundError(TruthEmploymentScopeBackfillError):
    """Raised when ``person_entity_id`` does not resolve to any entity."""


class PersonEntityTypeMismatchError(TruthEmploymentScopeBackfillError):
    """Raised when ``person_entity_id`` resolves to a non-PERSON entity."""


class EmploymentScopeConflictError(TruthEmploymentScopeBackfillError):
    """Raised by ``apply()`` before any write when the batch contains a
    CONFLICT item, or a BLOCKED item whose reason is an inconsistency of an
    already-approved employment fact_type (owner missing, owner not
    EMPLOYMENT, or transferability mismatch). Fail-closed: zero mutations
    are applied. BLOCKED items caused by a fact simply belonging to a
    different person (``OWNER_NOT_CHILD_OF_PERSON``) or by an unrecognized
    fact_type (``UNKNOWN_EMPLOYMENT_FACT_TYPE``) do NOT raise this error:
    they are outside this run's scope and are excluded, not repaired.
    """


class Classification(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    ALREADY_CORRECT = "ALREADY_CORRECT"
    CONFLICT = "CONFLICT"
    BLOCKED = "BLOCKED"
    #: Only ever produced in an APPLY report, replacing ELIGIBLE for an item
    #: that was actually repaired this run.
    REPAIRED = "REPAIRED"


class ReasonCode(StrEnum):
    ELIGIBLE_SCOPE_MISSING = "ELIGIBLE_SCOPE_MISSING"
    ALREADY_CORRECT_SELF_SCOPE = "ALREADY_CORRECT_SELF_SCOPE"
    PERSON_ENTITY_NOT_FOUND = "PERSON_ENTITY_NOT_FOUND"
    PERSON_ENTITY_TYPE_MISMATCH = "PERSON_ENTITY_TYPE_MISMATCH"
    OWNER_ENTITY_NOT_FOUND = "OWNER_ENTITY_NOT_FOUND"
    OWNER_ENTITY_NOT_EMPLOYMENT = "OWNER_ENTITY_NOT_EMPLOYMENT"
    OWNER_NOT_CHILD_OF_PERSON = "OWNER_NOT_CHILD_OF_PERSON"
    TRANSFERABILITY_MISMATCH = "TRANSFERABILITY_MISMATCH"
    EXISTING_SCOPE_POINTS_TO_OTHER_EMPLOYMENT = "EXISTING_SCOPE_POINTS_TO_OTHER_EMPLOYMENT"
    EXISTING_SCOPE_POINTS_TO_NON_EMPLOYMENT = "EXISTING_SCOPE_POINTS_TO_NON_EMPLOYMENT"
    UNKNOWN_EMPLOYMENT_FACT_TYPE = "UNKNOWN_EMPLOYMENT_FACT_TYPE"


#: BLOCKED reasons that mean "this is a broken approved-employment-fact
#: invariant" -- these abort apply() entirely (fail-closed, no partial
#: writes). OWNER_NOT_CHILD_OF_PERSON and UNKNOWN_EMPLOYMENT_FACT_TYPE are
#: deliberately excluded: they mean "not this run's business", not "broken".
_APPLY_BLOCKING_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.OWNER_ENTITY_NOT_FOUND,
        ReasonCode.OWNER_ENTITY_NOT_EMPLOYMENT,
        ReasonCode.TRANSFERABILITY_MISMATCH,
    }
)


class EmploymentScopeBackfillItem(BaseModel):
    """One fact's classification for this backfill run."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    entity_id: str
    fact_type: str
    current_employment_scope_entity_id: str | None
    expected_employment_scope_entity_id: str | None
    revision: int = Field(ge=1)
    content_fingerprint: str
    classification: Classification
    reason_code: ReasonCode


class EmploymentScopeBackfillReport(BaseModel):
    """The complete, serializable result of one dry-run or apply invocation."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["DRY_RUN", "APPLY"]
    compatibility_version: str = EMPLOYMENT_SCOPE_COMPATIBILITY_VERSION
    person_entity_id: str
    items: tuple[EmploymentScopeBackfillItem, ...] = Field(default_factory=tuple)
    repaired_fact_ids: tuple[str, ...] = Field(default_factory=tuple)


async def _preflight_person(session: Any, person_entity_id: str) -> None:
    """Read-only preflight. Must run before any classification or write."""

    entity = await session.get(TruthEntity, person_entity_id)
    if entity is None:
        raise PersonEntityNotFoundError(f"PERSON_ENTITY_NOT_FOUND:{person_entity_id}")
    if entity.entity_type != EntityType.PERSON.value:
        raise PersonEntityTypeMismatchError(
            f"PERSON_ENTITY_TYPE_MISMATCH:{person_entity_id}"
        )


def _classify_fact(
    fact: TruthFact,
    *,
    person_entity_id: str,
    owner: TruthEntity | None,
    existing_scope_entity: TruthEntity | None,
) -> EmploymentScopeBackfillItem:
    base: dict[str, Any] = dict(
        fact_id=fact.fact_id,
        entity_id=fact.entity_id,
        fact_type=fact.fact_type,
        current_employment_scope_entity_id=fact.employment_scope_entity_id,
        revision=fact.revision,
        content_fingerprint=fact.content_fingerprint,
    )

    if fact.fact_type not in EMPLOYMENT_FACT_TYPES:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=None,
            classification=Classification.BLOCKED,
            reason_code=ReasonCode.UNKNOWN_EMPLOYMENT_FACT_TYPE,
        )
    if owner is None:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=None,
            classification=Classification.BLOCKED,
            reason_code=ReasonCode.OWNER_ENTITY_NOT_FOUND,
        )
    if owner.entity_type != EntityType.EMPLOYMENT.value:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=None,
            classification=Classification.BLOCKED,
            reason_code=ReasonCode.OWNER_ENTITY_NOT_EMPLOYMENT,
        )
    if fact.transferability != Transferability.EMPLOYMENT_SCOPED.value:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=None,
            classification=Classification.BLOCKED,
            reason_code=ReasonCode.TRANSFERABILITY_MISMATCH,
        )
    if owner.parent_entity_id != person_entity_id:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=None,
            classification=Classification.BLOCKED,
            reason_code=ReasonCode.OWNER_NOT_CHILD_OF_PERSON,
        )

    expected = fact.entity_id
    if fact.employment_scope_entity_id is None:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=expected,
            classification=Classification.ELIGIBLE,
            reason_code=ReasonCode.ELIGIBLE_SCOPE_MISSING,
        )
    if fact.employment_scope_entity_id == fact.entity_id:
        return EmploymentScopeBackfillItem(
            **base,
            expected_employment_scope_entity_id=expected,
            classification=Classification.ALREADY_CORRECT,
            reason_code=ReasonCode.ALREADY_CORRECT_SELF_SCOPE,
        )

    if existing_scope_entity is not None and existing_scope_entity.entity_type == (
        EntityType.EMPLOYMENT.value
    ):
        reason = ReasonCode.EXISTING_SCOPE_POINTS_TO_OTHER_EMPLOYMENT
    else:
        reason = ReasonCode.EXISTING_SCOPE_POINTS_TO_NON_EMPLOYMENT
    return EmploymentScopeBackfillItem(
        **base,
        expected_employment_scope_entity_id=expected,
        classification=Classification.CONFLICT,
        reason_code=reason,
    )


async def _classify_all(
    session: Any, *, person_entity_id: str
) -> tuple[EmploymentScopeBackfillItem, ...]:
    """Read-only classification of every fact potentially in scope.

    The candidate query is intentionally broad -- any fact whose
    transferability is EMPLOYMENT_SCOPED, or whose fact_type is one of the
    approved ``EMPLOYMENT_FACT_TYPES`` -- so that both an unrecognized
    employment-shaped fact_type and a structurally wrong owner entity are
    visible in the report (as BLOCKED) rather than silently skipped. Person
    scoping is enforced per-item, not by the query, so a fact belonging to a
    different person's employment entity is still classified (BLOCKED /
    OWNER_NOT_CHILD_OF_PERSON) but never eligible and never mutated.
    """

    stmt = (
        select(TruthFact)
        .where(
            or_(
                TruthFact.transferability == Transferability.EMPLOYMENT_SCOPED.value,
                TruthFact.fact_type.in_(sorted(EMPLOYMENT_FACT_TYPES)),
            )
        )
        .order_by(TruthFact.fact_id)
    )
    facts = (await session.scalars(stmt)).all()

    entity_cache: dict[str, TruthEntity | None] = {}

    async def _get_entity(entity_id: str | None) -> TruthEntity | None:
        if entity_id is None:
            return None
        if entity_id not in entity_cache:
            entity_cache[entity_id] = await session.get(TruthEntity, entity_id)
        return entity_cache[entity_id]

    items: list[EmploymentScopeBackfillItem] = []
    for fact in facts:
        owner = await _get_entity(fact.entity_id)
        existing_scope_entity = await _get_entity(fact.employment_scope_entity_id)
        items.append(
            _classify_fact(
                fact,
                person_entity_id=person_entity_id,
                owner=owner,
                existing_scope_entity=existing_scope_entity,
            )
        )
    return tuple(sorted(items, key=lambda item: item.fact_id))


def _assert_apply_allowed(items: tuple[EmploymentScopeBackfillItem, ...]) -> None:
    conflicts = [item for item in items if item.classification == Classification.CONFLICT]
    if conflicts:
        raise EmploymentScopeConflictError(
            "EMPLOYMENT_SCOPE_CONFLICT:"
            f"{','.join(sorted(item.fact_id for item in conflicts))}"
        )
    structural = [
        item
        for item in items
        if item.classification == Classification.BLOCKED
        and item.reason_code in _APPLY_BLOCKING_REASONS
    ]
    if structural:
        raise EmploymentScopeConflictError(
            "EMPLOYMENT_SCOPE_INVARIANT_VIOLATION:"
            f"{','.join(sorted(item.fact_id for item in structural))}"
        )


async def dry_run(*, database: Any, person_entity_id: str) -> EmploymentScopeBackfillReport:
    """Read-only plan: full preflight + full classification, zero writes."""

    async with database._session() as session:
        await _preflight_person(session, person_entity_id)
        items = await _classify_all(session, person_entity_id=person_entity_id)
    return EmploymentScopeBackfillReport(
        mode="DRY_RUN", person_entity_id=person_entity_id, items=items
    )


async def apply(*, database: Any, person_entity_id: str) -> EmploymentScopeBackfillReport:
    """Repair every ELIGIBLE fact in exactly one all-or-nothing transaction.

    Preflight and classification run first and are identical to ``dry_run``.
    A CONFLICT or a structural BLOCKED item aborts before the first write
    (``EmploymentScopeConflictError``). Each repair goes through the
    existing, audited ``TruthRepository.update_fact`` (expected-revision
    checked, content_fingerprint recomputed) -- never a raw UPDATE.
    """

    async with database.truth_service.transaction() as repo:
        session = repo.session
        await _preflight_person(session, person_entity_id)
        items = await _classify_all(session, person_entity_id=person_entity_id)
        _assert_apply_allowed(items)

        repaired_fact_ids: list[str] = []
        final_items: list[EmploymentScopeBackfillItem] = []
        for item in items:
            if item.classification != Classification.ELIGIBLE:
                final_items.append(item)
                continue
            updated = await repo.update_fact(
                item.fact_id,
                TruthFactUpdate(
                    expected_revision=item.revision,
                    employment_scope_entity_id=item.entity_id,
                ),
            )
            repaired_fact_ids.append(str(updated.fact_id))
            final_items.append(
                item.model_copy(
                    update={
                        "current_employment_scope_entity_id": str(
                            updated.employment_scope_entity_id
                        ),
                        "revision": updated.revision,
                        "content_fingerprint": updated.content_fingerprint,
                        "classification": Classification.REPAIRED,
                    }
                )
            )

    return EmploymentScopeBackfillReport(
        mode="APPLY",
        person_entity_id=person_entity_id,
        items=tuple(final_items),
        repaired_fact_ids=tuple(repaired_fact_ids),
    )
