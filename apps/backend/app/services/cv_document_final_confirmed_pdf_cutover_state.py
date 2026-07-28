"""Explicit Provenance Stage P6-B3b-A: the resumable cutover-state machine
for ``cv_final_confirmed_pdf_artifacts``/``cv_confirmed_pdf_current_authority``.

``evaluate_cutover_state`` is strictly read-only -- it never issues a single
``CREATE``/``ALTER``/``DROP``/``INSERT``/``UPDATE`` statement, and calling it
any number of times never changes database state. ``run_cutover_installer``
is the only function in this module that mutates anything; it always calls
``evaluate_cutover_state`` first, only performs the exact next missing step
of the legal installation prefix, and always re-evaluates afterward -- a
caller that invokes it repeatedly (a crash-and-restart, or two callers
racing on the same idempotent DDL) always converges on the same ``READY``
end state without ever attempting to "repair" an object with the wrong
name-but-different schema/body (that is always ``INVALID``, fail-closed).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.engine import Engine

from app.services.cv_document_final_confirmed_pdf_schema_manifest import (
    ARTIFACT_TABLE_NAME,
    ARTIFACT_TRIGGER_NAMES,
    AUTHORITY_TABLE_NAME,
    AUTHORITY_VALIDATION_TRIGGER_NAMES,
    EXPECTED_TRIGGER_BODIES,
    LEGACY_FREEZE_TRIGGER_NAMES,
    LEGACY_PDF_SLOT_TABLE_NAME,
    TRIGGER_SQL_BY_NAME,
    preflight_final_confirmed_pdf_schema,
    table_matches_manifest,
)
from app.services.cv_document_models import (
    CvConfirmedPdfCurrentAuthorityRow,
    CvFinalConfirmedPdfArtifactRow,
)


class FinalConfirmedPdfCutoverState(StrEnum):
    ABSENT = "ABSENT"
    ARTIFACT_TABLE_TRIGGERS_PENDING = "ARTIFACT_TABLE_TRIGGERS_PENDING"
    AUTHORITY_TABLE_PENDING = "AUTHORITY_TABLE_PENDING"
    AUTHORITY_VALIDATION_TRIGGERS_PENDING = "AUTHORITY_VALIDATION_TRIGGERS_PENDING"
    LEGACY_FREEZE_PENDING = "LEGACY_FREEZE_PENDING"
    MIGRATION_PENDING = "MIGRATION_PENDING"
    READY = "READY"
    INVALID = "INVALID"


def _classify_triggers(existing: dict[str, str], expected_names: frozenset[str]) -> str:
    """Returns ``"COMPLETE"``/``"MISSING"``/``"INVALID"`` for one group of
    triggers. Any trigger present under an expected name but with a body
    that does not match ``EXPECTED_TRIGGER_BODIES`` is always ``INVALID`` --
    a partial-but-correct install (some but not all of the group present,
    all of those correct) is ``MISSING`` (resumable, install the rest)."""

    present = set(existing) & expected_names
    for name in present:
        if existing[name] != EXPECTED_TRIGGER_BODIES[name]:
            return "INVALID"
    if present == expected_names:
        return "COMPLETE"
    return "MISSING"


def _check_authority_integrity_and_migration(engine: Engine) -> str:
    """Returns ``"READY"``/``"MIGRATION_PENDING"``/``"INVALID"``. Always
    called only once every table/trigger phase above is already confirmed
    complete -- so any authority-row-level inconsistency found here is a
    genuine data problem, never a half-finished DDL install."""

    with engine.connect() as conn:
        invalid_final = conn.exec_driver_sql(
            f"""
            SELECT 1 FROM {AUTHORITY_TABLE_NAME} a
            WHERE a.lineage_kind = 'FINAL_DOCX_SNAPSHOT'
              AND NOT EXISTS (
                  SELECT 1 FROM {ARTIFACT_TABLE_NAME} f
                  WHERE f.artifact_fingerprint = a.current_artifact_fingerprint
                    AND f.owner_key_fingerprint = a.owner_key_fingerprint
              )
            LIMIT 1
            """
        ).first()
        if invalid_final is not None:
            return "INVALID"

        invalid_legacy = conn.exec_driver_sql(
            f"""
            SELECT 1 FROM {AUTHORITY_TABLE_NAME} a
            LEFT JOIN {LEGACY_PDF_SLOT_TABLE_NAME} s ON s.owner_key_fingerprint = a.owner_key_fingerprint
            LEFT JOIN cv_confirmed_pdf_artifacts c
              ON c.artifact_fingerprint = a.current_artifact_fingerprint
             AND c.owner_key_fingerprint = a.owner_key_fingerprint
            WHERE a.lineage_kind = 'LEGACY_VALIDATED_DOCX'
              AND (
                  c.artifact_fingerprint IS NULL
                  OR s.owner_key_fingerprint IS NULL
                  OR s.current_artifact_fingerprint IS NULL
                  OR s.current_artifact_fingerprint != a.current_artifact_fingerprint
              )
            LIMIT 1
            """
        ).first()
        if invalid_legacy is not None:
            return "INVALID"

        migration_pending = conn.exec_driver_sql(
            f"""
            SELECT 1 FROM {LEGACY_PDF_SLOT_TABLE_NAME} s
            LEFT JOIN {AUTHORITY_TABLE_NAME} a ON a.owner_key_fingerprint = s.owner_key_fingerprint
            WHERE s.current_artifact_fingerprint IS NOT NULL
              AND a.owner_key_fingerprint IS NULL
            LIMIT 1
            """
        ).first()
        if migration_pending is not None:
            return "MIGRATION_PENDING"

    return "READY"


def evaluate_cutover_state(engine: Engine) -> FinalConfirmedPdfCutoverState:
    """Read-only. Classifies exactly where the two-table/12-trigger/
    migration install currently stands, resuming from any legal partial
    prefix and fail-closed (``INVALID``) on anything else."""

    preflight = preflight_final_confirmed_pdf_schema(engine)

    artifact_exists = preflight["artifact_table_exists"]
    authority_exists = preflight["authority_table_exists"]

    if not artifact_exists and not authority_exists:
        if preflight["legacy_slot_triggers"] or preflight["artifact_triggers"] or preflight["authority_triggers"]:
            return FinalConfirmedPdfCutoverState.INVALID
        return FinalConfirmedPdfCutoverState.ABSENT

    if artifact_exists and not table_matches_manifest(preflight["artifact_schema"], table_name=ARTIFACT_TABLE_NAME):
        return FinalConfirmedPdfCutoverState.INVALID

    if authority_exists and not table_matches_manifest(
        preflight["authority_schema"], table_name=AUTHORITY_TABLE_NAME
    ):
        return FinalConfirmedPdfCutoverState.INVALID

    if not artifact_exists:
        # Authority table (or its triggers) exist without the artifact
        # table -- never a legal state under our fixed install ordering.
        return FinalConfirmedPdfCutoverState.INVALID

    artifact_trigger_state = _classify_triggers(preflight["artifact_triggers"], ARTIFACT_TRIGGER_NAMES)
    if artifact_trigger_state == "INVALID":
        return FinalConfirmedPdfCutoverState.INVALID
    if artifact_trigger_state == "MISSING":
        if authority_exists or preflight["authority_triggers"] or preflight["legacy_slot_triggers"]:
            return FinalConfirmedPdfCutoverState.INVALID
        return FinalConfirmedPdfCutoverState.ARTIFACT_TABLE_TRIGGERS_PENDING

    if not authority_exists:
        if preflight["authority_triggers"] or preflight["legacy_slot_triggers"]:
            return FinalConfirmedPdfCutoverState.INVALID
        return FinalConfirmedPdfCutoverState.AUTHORITY_TABLE_PENDING

    authority_trigger_state = _classify_triggers(
        preflight["authority_triggers"], AUTHORITY_VALIDATION_TRIGGER_NAMES
    )
    if authority_trigger_state == "INVALID":
        return FinalConfirmedPdfCutoverState.INVALID
    if authority_trigger_state == "MISSING":
        if preflight["legacy_slot_triggers"]:
            return FinalConfirmedPdfCutoverState.INVALID
        return FinalConfirmedPdfCutoverState.AUTHORITY_VALIDATION_TRIGGERS_PENDING

    legacy_freeze_state = _classify_triggers(preflight["legacy_slot_triggers"], LEGACY_FREEZE_TRIGGER_NAMES)
    if legacy_freeze_state == "INVALID":
        return FinalConfirmedPdfCutoverState.INVALID
    if legacy_freeze_state == "MISSING":
        return FinalConfirmedPdfCutoverState.LEGACY_FREEZE_PENDING

    integrity = _check_authority_integrity_and_migration(engine)
    if integrity == "INVALID":
        return FinalConfirmedPdfCutoverState.INVALID
    if integrity == "MIGRATION_PENDING":
        return FinalConfirmedPdfCutoverState.MIGRATION_PENDING
    return FinalConfirmedPdfCutoverState.READY


def _install_triggers(engine: Engine, names: frozenset[str]) -> None:
    with engine.begin() as conn:
        for name in names:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])


def _run_legacy_migration(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            INSERT INTO {AUTHORITY_TABLE_NAME} (
                owner_key_fingerprint, lineage_kind, current_artifact_fingerprint, slot_version, updated_at
            )
            SELECT
                s.owner_key_fingerprint,
                'LEGACY_VALIDATED_DOCX',
                s.current_artifact_fingerprint,
                1,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            FROM {LEGACY_PDF_SLOT_TABLE_NAME} s
            WHERE s.current_artifact_fingerprint IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {AUTHORITY_TABLE_NAME} a
                  WHERE a.owner_key_fingerprint = s.owner_key_fingerprint
              )
            """
        )


def run_cutover_installer(engine: Engine) -> FinalConfirmedPdfCutoverState:
    """Idempotent, resumable, ordered installer. Always fail-closed: raises
    ``RuntimeError`` immediately if the starting state (or any state
    reached mid-way) is ``INVALID``, and raises if the final state after
    every legal step is anything other than ``READY``. Never attempts an
    auto-repair of an object with a wrong schema/body."""

    state = evaluate_cutover_state(engine)
    if state == FinalConfirmedPdfCutoverState.INVALID:
        raise RuntimeError("ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state=INVALID")
    if state == FinalConfirmedPdfCutoverState.READY:
        return state

    if state == FinalConfirmedPdfCutoverState.ABSENT:
        CvFinalConfirmedPdfArtifactRow.__table__.create(engine, checkfirst=True)
        state = evaluate_cutover_state(engine)
        if state == FinalConfirmedPdfCutoverState.INVALID:
            raise RuntimeError("ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state=INVALID:after=create_artifact_table")

    if state == FinalConfirmedPdfCutoverState.ARTIFACT_TABLE_TRIGGERS_PENDING:
        _install_triggers(engine, ARTIFACT_TRIGGER_NAMES)
        state = evaluate_cutover_state(engine)
        if state == FinalConfirmedPdfCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state=INVALID:after=install_artifact_triggers"
            )

    if state == FinalConfirmedPdfCutoverState.AUTHORITY_TABLE_PENDING:
        CvConfirmedPdfCurrentAuthorityRow.__table__.create(engine, checkfirst=True)
        state = evaluate_cutover_state(engine)
        if state == FinalConfirmedPdfCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state=INVALID:after=create_authority_table"
            )

    if state == FinalConfirmedPdfCutoverState.AUTHORITY_VALIDATION_TRIGGERS_PENDING:
        _install_triggers(engine, AUTHORITY_VALIDATION_TRIGGER_NAMES)
        state = evaluate_cutover_state(engine)
        if state == FinalConfirmedPdfCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state=INVALID:after=install_authority_triggers"
            )

    if state == FinalConfirmedPdfCutoverState.LEGACY_FREEZE_PENDING:
        _install_triggers(engine, LEGACY_FREEZE_TRIGGER_NAMES)
        state = evaluate_cutover_state(engine)
        if state == FinalConfirmedPdfCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state=INVALID:after=install_legacy_freeze_triggers"
            )

    if state == FinalConfirmedPdfCutoverState.MIGRATION_PENDING:
        _run_legacy_migration(engine)
        state = evaluate_cutover_state(engine)

    if state != FinalConfirmedPdfCutoverState.READY:
        raise RuntimeError(f"ERROR_INCOMPATIBLE_FINAL_CONFIRMED_PDF_SCHEMA:state={state}")
    return state
