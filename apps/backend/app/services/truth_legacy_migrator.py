"""Explicit Provenance P2 — legacy Truth Library migration bridge.

Migrates existing legacy Truth Library JSON into the P1 Explicit Provenance
foundation (``TruthEntity`` / ``TruthFact``) plus an audit ledger
(``TruthLegacyMigrationMap``). See docs/explicit-provenance-stage-p2.md.

This module never touches active CV flows: it does not read
``EXPLICIT_PROVENANCE_ENABLED``, does not run automatically, and produces no
data that any generation/planning path currently reads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select

from app.models import TruthEntity, TruthLegacyMigrationMap
from app.schemas.truth_entity import EntityType, TruthEntityCreate
from app.schemas.truth_fact import TruthFactCreate
from app.schemas.truth_legacy_migration import (
    MIGRATION_SCHEMA_VERSION,
    CategoryDisposition,
    CategoryPlan,
    DuplicateCollision,
    FingerprintConflict,
    LegacyClassification,
    MigrationCounts,
    MigrationReport,
    RecordPlan,
)
from app.services.truth_fingerprint import canonical_json_bytes
from app.services.truth_index import normalize_truth_text
from app.services.truth_legacy_classification import (
    DEFERRED_POLICY_CATEGORIES,
    TRUTH_FACT_STATUS_BY_CLASSIFICATION,
    category_disposition,
    classify_category,
)
from app.services.truth_library_loader import load_truth_library


class TruthLegacyMigrationError(Exception):
    """Base error for the P2 legacy migration bridge."""


class PersonEntityIdRequiredError(TruthLegacyMigrationError):
    """Raised when ``--apply`` is attempted without a resolvable person_entity_id."""


class PersonEntityTypeMismatchError(TruthLegacyMigrationError):
    """Raised when person_entity_id already exists with entity_type != PERSON."""


class LegacyRecordChangedReviewRequiredError(TruthLegacyMigrationError):
    """Raised when a previously migrated legacy record's content changed."""


_NAMED_CATEGORY_IDENTITY_FIELD: dict[str, str] = {
    "kompetencje": "nazwa",
    "narzedzia": "nazwa",
    "technologie": "nazwa",
    "certyfikaty": "nazwa",
    "kursy": "nazwa",
    "wyksztalcenie": "kierunek",
    "jezyki": "jezyk",
}
_NAMED_CATEGORY_FIELDS: dict[str, tuple[str, ...]] = {
    "kompetencje": ("nazwa", "nazwaAlt", "poziom"),
    "narzedzia": ("nazwa", "nazwaAlt"),
    "technologie": ("nazwa", "nazwaAlt"),
    "certyfikaty": ("nazwa",),
    "kursy": ("nazwa", "organizator"),
    "wyksztalcenie": ("kierunek", "uczelnia", "stopien"),
    "jezyki": ("jezyk", "poziom"),
}
_NAMED_CATEGORY_ENTITY_TYPE: dict[str, str] = {
    "kompetencje": "SKILL",
    "narzedzia": "TOOL",
    "technologie": "TECHNOLOGY",
    "certyfikaty": "CERTIFICATION",
    "kursy": "COURSE",
    "wyksztalcenie": "EDUCATION",
    "jezyki": "LANGUAGE",
}
_NAMED_CATEGORY_FACT_TYPE: dict[str, str] = {
    "kompetencje": "SKILL_NAME",
    "narzedzia": "TOOL_NAME",
    "technologie": "TECHNOLOGY_NAME",
    "certyfikaty": "CERTIFICATION_NAME",
    "kursy": "COURSE_NAME",
    "wyksztalcenie": "EDUCATION_DEGREE",
    "jezyki": "LANGUAGE_NAME",
}
_GLOBAL_TEXT_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "osiagnieciaLiczbowe": ("opis", "tresc", "wynik"),
    "skalaOdpowiedzialnosci": ("opis", "skala"),
}
_GLOBAL_FACT_TYPE: dict[str, str] = {
    "osiagnieciaLiczbowe": "NUMERIC_ACHIEVEMENT",
    "skalaOdpowiedzialnosci": "RESPONSIBILITY_SCALE",
}
_UNSUPPORTED_CATEGORY = "daneOsobowe"
_FORCED_REVIEW_CATEGORY = "branze"
_EMPLOYMENT_CATEGORY = "doswiadczenieZawodowe"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_fingerprint(payload: Any) -> str:
    """Deterministic SHA-256 over canonical JSON. Never includes the physical path."""

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _record_fingerprint(entry: Any, *, schema_version: str) -> str:
    return compute_fingerprint({"migration_schema_version": schema_version, "record": entry})


def _text_fingerprint(text: str, *, schema_version: str) -> str:
    return compute_fingerprint({"migration_schema_version": schema_version, "text": text})


def _with_source_label(value: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    zrodlo = entry.get("zrodlo")
    if isinstance(zrodlo, str) and zrodlo.strip():
        value = {**value, "legacy_source_label": zrodlo.strip()}
    return value


def _display_label(*parts: Any, fallback: str) -> str:
    cleaned = [str(part).strip() for part in parts if isinstance(part, str) and part.strip()]
    label = " — ".join(cleaned) if cleaned else fallback
    return label[:500]


@dataclass(frozen=True)
class PlannedRecord:
    """One legacy record fully resolved: identity, fingerprint, and (if any)
    the entity/fact it should materialize into. Shared by dry-run and apply so
    both use exactly the same classification and identity logic.
    """

    category: str
    record_key: str
    fingerprint: str
    classification: LegacyClassification
    reason: str
    has_stable_id: bool
    owner: Literal["PERSON", "SELF", "PARENT"]
    parent_record_key: str | None = None
    entity_type: str | None = None
    entity_display_name: str | None = None
    fact_type: str | None = None
    fact_value: dict[str, Any] | None = None
    transferability: str | None = None


@dataclass
class _BuildResult:
    records: list[PlannedRecord] = field(default_factory=list)
    category_plans: list[CategoryPlan] = field(default_factory=list)
    duplicate_collisions: list[DuplicateCollision] = field(default_factory=list)


def _employment_identity(
    entry: dict[str, Any], *, schema_version: str
) -> tuple[str, bool, str]:
    fingerprint = _record_fingerprint(entry, schema_version=schema_version)
    stable_id = entry.get("id")
    if isinstance(stable_id, str) and stable_id.strip():
        return stable_id.strip(), True, fingerprint
    return fingerprint, False, fingerprint


def _plan_employment(
    entries: list[Any], *, schema_version: str, result: _BuildResult
) -> None:
    counts = MigrationCounts()
    record_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record_count += 1
        identity, has_stable_id, record_fp = _employment_identity(
            entry, schema_version=schema_version
        )
        root_key = f"{_EMPLOYMENT_CATEGORY}:{identity}:{schema_version}"
        decision = classify_category(
            _EMPLOYMENT_CATEGORY,
            status=entry.get("status"),
            use_in_cv=entry.get("uzywacWCV"),
            requires_approval=entry.get("wymagaAkceptacji"),
            has_stable_id=has_stable_id,
        )
        counts.add(decision.classification)
        has_fact = decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
        display_name = _display_label(
            entry.get("stanowisko"), entry.get("firma"), fallback="Legacy Employment Record"
        )
        role_value = None
        if has_fact:
            role_value = {}
            for field_name in ("firma", "stanowisko", "stanowiskoAlt", "okresOd", "okresDo"):
                value = entry.get(field_name)
                if isinstance(value, str) and value.strip():
                    role_value[field_name] = value.strip()
            role_value = _with_source_label(role_value, entry)
        result.records.append(
            PlannedRecord(
                category=_EMPLOYMENT_CATEGORY,
                record_key=root_key,
                fingerprint=record_fp,
                classification=decision.classification,
                reason=decision.reason,
                has_stable_id=has_stable_id,
                owner="SELF",
                entity_type="EMPLOYMENT" if has_fact else None,
                entity_display_name=display_name if has_fact else None,
                fact_type="EMPLOYMENT_ROLE" if has_fact else None,
                fact_value=role_value,
                transferability="EMPLOYMENT_SCOPED" if has_fact else None,
            )
        )

        # Children share the parent's status/booleans verbatim (the legacy
        # schema has no per-activity status), so their classification is
        # always identical to the root's — see PARENT EMPLOYMENT CONFLICT
        # GATE in docs/explicit-provenance-stage-p2.md.
        child_decision = decision

        activities = entry.get("aktywnosci", [])
        if isinstance(activities, list):
            seen_keys: dict[str, int] = {}
            raw_count = 0
            for activity in activities:
                if not (isinstance(activity, str) and activity.strip()):
                    continue
                raw_count += 1
                child_fp = _text_fingerprint(activity, schema_version=schema_version)
                child_key = f"{_EMPLOYMENT_CATEGORY}:{identity}:aktywnosci:{child_fp}:{schema_version}"
                seen_keys[child_key] = seen_keys.get(child_key, 0) + 1
                if seen_keys[child_key] > 1:
                    continue  # duplicate content collapses onto the same key
                result.records.append(
                    PlannedRecord(
                        category=_EMPLOYMENT_CATEGORY,
                        record_key=child_key,
                        fingerprint=child_fp,
                        classification=child_decision.classification,
                        reason=child_decision.reason,
                        has_stable_id=has_stable_id,
                        owner="PARENT",
                        parent_record_key=root_key,
                        fact_type="EMPLOYMENT_ACTIVITY"
                        if child_decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
                        else None,
                        fact_value={"text": activity.strip()}
                        if child_decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
                        else None,
                        transferability="EMPLOYMENT_SCOPED"
                        if child_decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
                        else None,
                    )
                )
            for child_key, raw in seen_keys.items():
                if raw > 1:
                    result.duplicate_collisions.append(
                        DuplicateCollision(
                            category=_EMPLOYMENT_CATEGORY,
                            legacy_record_key=child_key,
                            raw_item_count=raw,
                            unique_identity_count=1,
                        )
                    )

        for field_name, fact_type in (
            ("wynikLiczbowy", "EMPLOYMENT_NUMERIC_RESULT"),
            ("skalaOdpowiedzialnosci", "EMPLOYMENT_RESPONSIBILITY_SCALE"),
        ):
            value = entry.get(field_name)
            if not (isinstance(value, str) and value.strip()):
                continue
            child_fp = _text_fingerprint(value, schema_version=schema_version)
            child_key = f"{_EMPLOYMENT_CATEGORY}:{identity}:{field_name}:{schema_version}"
            result.records.append(
                PlannedRecord(
                    category=_EMPLOYMENT_CATEGORY,
                    record_key=child_key,
                    fingerprint=child_fp,
                    classification=child_decision.classification,
                    reason=child_decision.reason,
                    has_stable_id=has_stable_id,
                    owner="PARENT",
                    parent_record_key=root_key,
                    fact_type=fact_type
                    if child_decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
                    else None,
                    fact_value={"text": value.strip()}
                    if child_decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
                    else None,
                    transferability="EMPLOYMENT_SCOPED"
                    if child_decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
                    else None,
                )
            )
    result.category_plans.append(
        CategoryPlan(
            category=_EMPLOYMENT_CATEGORY,
            disposition=CategoryDisposition.PROCESSED,
            reason="per-record classification; parent employment gates its children",
            record_count=record_count,
            counts=counts,
        )
    )


def _plan_named_category(
    category: str, entries: list[Any], *, schema_version: str, result: _BuildResult
) -> None:
    identity_field = _NAMED_CATEGORY_IDENTITY_FIELD[category]
    counts = MigrationCounts()
    record_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record_count += 1
        record_fp = _record_fingerprint(entry, schema_version=schema_version)
        name_value = entry.get(identity_field)
        has_stable_id = isinstance(name_value, str) and bool(name_value.strip())
        identity = normalize_truth_text(name_value) if has_stable_id else record_fp
        key = f"{category}:{identity}:{schema_version}"
        decision = classify_category(
            category,
            status=entry.get("status"),
            use_in_cv=entry.get("uzywacWCV"),
            requires_approval=entry.get("wymagaAkceptacji"),
            has_stable_id=has_stable_id,
        )
        counts.add(decision.classification)
        has_fact = decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
        fact_value = None
        if has_fact:
            fact_value = {}
            for field_name in _NAMED_CATEGORY_FIELDS[category]:
                value = entry.get(field_name)
                if isinstance(value, str) and value.strip():
                    fact_value[field_name] = value.strip()
            fact_value = _with_source_label(fact_value, entry)
        display_name = _display_label(
            name_value, fallback=f"Legacy {category} record"
        )
        result.records.append(
            PlannedRecord(
                category=category,
                record_key=key,
                fingerprint=record_fp,
                classification=decision.classification,
                reason=decision.reason,
                has_stable_id=has_stable_id,
                owner="SELF",
                entity_type=_NAMED_CATEGORY_ENTITY_TYPE[category] if has_fact else None,
                entity_display_name=display_name if has_fact else None,
                fact_type=_NAMED_CATEGORY_FACT_TYPE[category] if has_fact else None,
                fact_value=fact_value,
                transferability="GLOBAL_TRANSFERABLE" if has_fact else None,
            )
        )
    result.category_plans.append(
        CategoryPlan(
            category=category,
            disposition=CategoryDisposition.PROCESSED,
            reason="per-record classification",
            record_count=record_count,
            counts=counts,
        )
    )


def _plan_global_achievement_category(
    category: str, entries: list[Any], *, schema_version: str, result: _BuildResult
) -> None:
    counts = MigrationCounts()
    record_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record_count += 1
        record_fp = _record_fingerprint(entry, schema_version=schema_version)
        stable_id = entry.get("id")
        has_stable_id = isinstance(stable_id, str) and bool(stable_id.strip())
        identity = stable_id.strip() if has_stable_id else record_fp
        key = f"{category}:{identity}:{schema_version}"
        decision = classify_category(
            category,
            status=entry.get("status"),
            use_in_cv=entry.get("uzywacWCV"),
            requires_approval=entry.get("wymagaAkceptacji"),
            has_stable_id=has_stable_id,
        )
        counts.add(decision.classification)
        has_fact = decision.classification in TRUTH_FACT_STATUS_BY_CLASSIFICATION
        fact_value = None
        if has_fact:
            text_value = ""
            for field_name in _GLOBAL_TEXT_FIELD_CANDIDATES[category]:
                candidate = entry.get(field_name)
                if isinstance(candidate, str) and candidate.strip():
                    text_value = candidate.strip()
                    break
            fact_value = _with_source_label({"text": text_value}, entry)
        result.records.append(
            PlannedRecord(
                category=category,
                record_key=key,
                fingerprint=record_fp,
                classification=decision.classification,
                reason=decision.reason,
                has_stable_id=has_stable_id,
                owner="PERSON",
                fact_type=_GLOBAL_FACT_TYPE[category] if has_fact else None,
                fact_value=fact_value,
                transferability="GLOBAL_TRANSFERABLE" if has_fact else None,
            )
        )
    result.category_plans.append(
        CategoryPlan(
            category=category,
            disposition=CategoryDisposition.PROCESSED,
            reason="REQUIRES_REVIEW when no stable legacy id, otherwise per-record classification",
            record_count=record_count,
            counts=counts,
        )
    )


def _plan_branze(kategorie: dict[str, Any], *, schema_version: str, result: _BuildResult) -> None:
    names = kategorie.get(_FORCED_REVIEW_CATEGORY, {}).get("zatwierdzoneBranze", [])
    counts = MigrationCounts()
    record_count = 0
    if isinstance(names, list):
        for name in names:
            if not (isinstance(name, str) and name.strip()):
                continue
            record_count += 1
            fp = _text_fingerprint(name, schema_version=schema_version)
            key = f"{_FORCED_REVIEW_CATEGORY}:{fp}:{schema_version}"
            decision = classify_category(
                _FORCED_REVIEW_CATEGORY,
                status=None,
                use_in_cv=None,
                requires_approval=None,
                has_stable_id=False,
            )
            counts.add(decision.classification)
            result.records.append(
                PlannedRecord(
                    category=_FORCED_REVIEW_CATEGORY,
                    record_key=key,
                    fingerprint=fp,
                    classification=decision.classification,
                    reason=decision.reason,
                    has_stable_id=False,
                    owner="PERSON",
                )
            )
    result.category_plans.append(
        CategoryPlan(
            category=_FORCED_REVIEW_CATEGORY,
            disposition=CategoryDisposition.PROCESSED,
            reason="always REQUIRES_REVIEW; ledger row only, never an auto-confirmed fact",
            record_count=record_count,
            counts=counts,
        )
    )


def _plan_dane_osobowe(
    kategorie: dict[str, Any], *, schema_version: str, result: _BuildResult
) -> None:
    entries = kategorie.get(_UNSUPPORTED_CATEGORY, {}).get("wpisy", [])
    counts = MigrationCounts()
    record_count = 0
    if isinstance(entries, list):
        for entry in entries:
            record_count += 1
            fp = _record_fingerprint(entry, schema_version=schema_version)
            key = f"{_UNSUPPORTED_CATEGORY}:{fp}:{schema_version}"
            decision = classify_category(
                _UNSUPPORTED_CATEGORY,
                status=None,
                use_in_cv=None,
                requires_approval=None,
                has_stable_id=False,
            )
            counts.add(decision.classification)
            result.records.append(
                PlannedRecord(
                    category=_UNSUPPORTED_CATEGORY,
                    record_key=key,
                    fingerprint=fp,
                    classification=decision.classification,
                    reason=decision.reason,
                    has_stable_id=False,
                    owner="PERSON",
                )
            )
    result.category_plans.append(
        CategoryPlan(
            category=_UNSUPPORTED_CATEGORY,
            disposition=CategoryDisposition.PROCESSED,
            reason="UNSUPPORTED; ledger row only, never a TruthFact",
            record_count=record_count,
            counts=counts,
        )
    )


def build_plan(
    library: dict[str, Any], *, schema_version: str = MIGRATION_SCHEMA_VERSION
) -> _BuildResult:
    """Pure function: legacy JSON -> planned records + category report.

    Never touches the database or the legacy file. Shared by both dry-run and
    apply so their classification/identity decisions can never drift apart.
    """

    result = _BuildResult()
    kategorie = library.get("kategorie", {})
    if not isinstance(kategorie, dict):
        kategorie = {}

    employment = kategorie.get(_EMPLOYMENT_CATEGORY, {}).get("wpisy", [])
    _plan_employment(
        employment if isinstance(employment, list) else [],
        schema_version=schema_version,
        result=result,
    )

    for category in ("osiagnieciaLiczbowe", "skalaOdpowiedzialnosci"):
        entries = kategorie.get(category, {}).get("wpisy", [])
        _plan_global_achievement_category(
            category, entries if isinstance(entries, list) else [],
            schema_version=schema_version, result=result,
        )

    for category in _NAMED_CATEGORY_IDENTITY_FIELD:
        entries = kategorie.get(category, {}).get("wpisy", [])
        _plan_named_category(
            category, entries if isinstance(entries, list) else [],
            schema_version=schema_version, result=result,
        )

    _plan_branze(kategorie, schema_version=schema_version, result=result)
    _plan_dane_osobowe(kategorie, schema_version=schema_version, result=result)

    for category in DEFERRED_POLICY_CATEGORIES:
        if category not in kategorie:
            continue
        result.category_plans.append(
            CategoryPlan(
                category=category,
                disposition=category_disposition(category),
                reason="policy decision deferred; no ledger row, no TruthFact, no mutation",
            )
        )

    return result


def _base_report(
    *, mode: Literal["DRY_RUN", "APPLY"], legacy_source_id: str, legacy_source_path: str,
    person_entity_id: str | None, schema_version: str,
) -> MigrationReport:
    return MigrationReport(
        mode=mode,
        legacy_source_id=legacy_source_id,
        legacy_source_path=str(legacy_source_path),
        migration_schema_version=schema_version,
        person_entity_id=person_entity_id,
    )


def _fill_report_from_plan(report: MigrationReport, built: _BuildResult) -> None:
    report.category_plans = built.category_plans
    report.duplicate_text_collisions = built.duplicate_collisions
    for planned in built.records:
        report.counts.add(planned.classification)
        report.record_plans.append(
            RecordPlan(
                category=planned.category,
                legacy_record_key=planned.record_key,
                legacy_record_fingerprint=planned.fingerprint,
                classification=planned.classification,
                reason=planned.reason,
                has_stable_id=planned.has_stable_id,
                entity_type=planned.entity_type,
                fact_type=planned.fact_type,
                employment_legacy_record_key=planned.parent_record_key,
            )
        )


def dry_run(
    *,
    truth_library_path: Path,
    legacy_source_id: str,
    person_entity_id: str | None = None,
    schema_version: str = MIGRATION_SCHEMA_VERSION,
) -> MigrationReport:
    """Read-only plan: no database access, no writes to the legacy JSON."""

    library = load_truth_library(truth_library_path)
    report = _base_report(
        mode="DRY_RUN",
        legacy_source_id=legacy_source_id,
        legacy_source_path=str(truth_library_path),
        person_entity_id=person_entity_id,
        schema_version=schema_version,
    )
    if person_entity_id is None:
        report.person_entity_id_required_for_apply = True
    built = build_plan(library, schema_version=schema_version)
    _fill_report_from_plan(report, built)
    return report


async def apply(
    *,
    database: Any,
    truth_library_path: Path,
    person_entity_id: str,
    legacy_source_id: str,
    schema_version: str = MIGRATION_SCHEMA_VERSION,
) -> MigrationReport:
    """Migrate legacy JSON into P1 in exactly one transaction.

    Any error raised here rolls back every write made during this call (entity
    creation, fact creation, and ledger rows alike) because
    ``TruthService.transaction()`` wraps everything in ``session.begin()``.
    """

    library = load_truth_library(truth_library_path)
    built = build_plan(library, schema_version=schema_version)

    report = _base_report(
        mode="APPLY",
        legacy_source_id=legacy_source_id,
        legacy_source_path=str(truth_library_path),
        person_entity_id=person_entity_id,
        schema_version=schema_version,
    )
    report.category_plans = built.category_plans
    report.duplicate_text_collisions = built.duplicate_collisions

    async with database.truth_service.transaction() as repo:
        session = repo.session

        # Person existence/type check happens first and is the only thing
        # that runs before any migration write (rule: a mismatch must be
        # detected before the first migration write).
        existing_person = await session.get(TruthEntity, person_entity_id)
        if existing_person is not None and existing_person.entity_type != EntityType.PERSON.value:
            raise PersonEntityTypeMismatchError(
                f"PERSON_ENTITY_TYPE_MISMATCH:{person_entity_id}"
            )
        if existing_person is None:
            created_person = await repo.create_entity(
                TruthEntityCreate(
                    entity_id=person_entity_id,
                    entity_type=EntityType.PERSON,
                    display_name="Truth Library Owner",
                )
            )
            report.created_entity_ids.append(created_person.entity_id)

        entity_by_record_key: dict[str, str] = {}

        for planned in built.records:
            existing_map = (
                await session.execute(
                    select(TruthLegacyMigrationMap).where(
                        TruthLegacyMigrationMap.person_entity_id == person_entity_id,
                        TruthLegacyMigrationMap.legacy_source_id == legacy_source_id,
                        TruthLegacyMigrationMap.migration_schema_version == schema_version,
                        TruthLegacyMigrationMap.legacy_category == planned.category,
                        TruthLegacyMigrationMap.legacy_record_key == planned.record_key,
                    )
                )
            ).scalar_one_or_none()

            if existing_map is not None:
                if existing_map.legacy_record_fingerprint != planned.fingerprint:
                    report.fingerprint_conflicts.append(
                        FingerprintConflict(
                            category=planned.category,
                            legacy_record_key=planned.record_key,
                            stored_fingerprint=existing_map.legacy_record_fingerprint,
                            current_fingerprint=planned.fingerprint,
                        )
                    )
                    raise LegacyRecordChangedReviewRequiredError(
                        "LEGACY_RECORD_CHANGED_REVIEW_REQUIRED:"
                        f"{planned.category}:{planned.record_key}"
                    )
                # Idempotent replay: reuse the previously assigned target ids.
                report.reused_map_row_count += 1
                report.counts.add(planned.classification)
                if planned.owner == "SELF" and existing_map.target_entity_id:
                    entity_by_record_key[planned.record_key] = existing_map.target_entity_id
                continue

            target_entity_id: str | None = None
            target_fact_id: str | None = None

            if planned.fact_type is not None and planned.fact_value is not None:
                if planned.owner == "SELF":
                    assert planned.entity_type is not None
                    assert planned.entity_display_name is not None
                    created_entity = await repo.create_entity(
                        TruthEntityCreate(
                            entity_type=EntityType(planned.entity_type),
                            parent_entity_id=person_entity_id,
                            display_name=planned.entity_display_name,
                            source_reference=f"truth_library:{planned.record_key}",
                        )
                    )
                    target_entity_id = str(created_entity.entity_id)
                    entity_by_record_key[planned.record_key] = target_entity_id
                    report.created_entity_ids.append(created_entity.entity_id)
                    fact_owner_entity_id = target_entity_id
                elif planned.owner == "PARENT":
                    assert planned.parent_record_key is not None
                    fact_owner_entity_id = entity_by_record_key.get(planned.parent_record_key)
                    if fact_owner_entity_id is None:
                        # Parent had no materialized entity (its own record
                        # resolved to REJECTED); a child cannot outrank its
                        # parent, so it is silently skipped — ledger row still
                        # records the classification decision.
                        fact_owner_entity_id = None
                else:  # "PERSON"
                    fact_owner_entity_id = person_entity_id

                if fact_owner_entity_id is not None:
                    fact_status = TRUTH_FACT_STATUS_BY_CLASSIFICATION[planned.classification]
                    normalized_value = {
                        key: normalize_truth_text(value) if isinstance(value, str) else value
                        for key, value in planned.fact_value.items()
                    }
                    created_fact = await repo.create_fact(
                        TruthFactCreate(
                            entity_id=fact_owner_entity_id,
                            fact_type=planned.fact_type,
                            value_json=planned.fact_value,
                            normalized_value_json=normalized_value,
                            status=fact_status,
                            use_in_cv=fact_status == "CONFIRMED",
                            requires_approval=fact_status != "CONFIRMED",
                            transferability=planned.transferability,
                            source_reference=f"truth_library:{planned.record_key}",
                        )
                    )
                    target_fact_id = str(created_fact.fact_id)
                    report.created_fact_ids.append(created_fact.fact_id)

            now = _utcnow_iso()
            map_row = TruthLegacyMigrationMap(
                migration_map_id=str(uuid4()),
                person_entity_id=person_entity_id,
                legacy_source_id=legacy_source_id,
                legacy_source_path=str(truth_library_path),
                legacy_category=planned.category,
                legacy_record_key=planned.record_key,
                legacy_record_fingerprint=planned.fingerprint,
                target_entity_id=target_entity_id,
                target_fact_id=target_fact_id,
                classification=planned.classification.value,
                migration_schema_version=schema_version,
                created_at=now,
                updated_at=now,
            )
            session.add(map_row)
            await session.flush()
            report.created_map_row_ids.append(map_row.migration_map_id)
            report.counts.add(planned.classification)

    return report
