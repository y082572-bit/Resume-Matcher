"""Explicit Provenance Stage P6-C1b-A: the resumable cutover-state machine
for ``cv_approved_content_packages``/``cv_approved_content_current_authority``.

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

from app.services.cv_approved_content_package_schema_manifest import (
    AUTHORITY_TABLE_NAME,
    AUTHORITY_TRIGGER_NAMES,
    PACKAGE_TABLE_NAME,
    PACKAGE_TRIGGER_NAMES,
    EXPECTED_TRIGGER_BODIES,
    TRIGGER_SQL_BY_NAME,
    preflight_approved_content_package_schema,
    table_matches_manifest,
)
from app.services.cv_document_models import (
    CvApprovedContentCurrentAuthorityRow,
    CvApprovedContentPackageRow,
)


class ApprovedContentPackageCutoverState(StrEnum):
    ABSENT = "ABSENT"
    PACKAGE_TABLE_TRIGGERS_PENDING = "PACKAGE_TABLE_TRIGGERS_PENDING"
    AUTHORITY_TABLE_PENDING = "AUTHORITY_TABLE_PENDING"
    AUTHORITY_TRIGGERS_PENDING = "AUTHORITY_TRIGGERS_PENDING"
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


def evaluate_cutover_state(engine: Engine) -> ApprovedContentPackageCutoverState:
    """Read-only. Classifies exactly where the two-table/six-trigger install
    currently stands, resuming from any legal partial prefix and fail-closed
    (``INVALID``) on anything else."""

    preflight = preflight_approved_content_package_schema(engine)

    package_exists = preflight["package_table_exists"]
    authority_exists = preflight["authority_table_exists"]

    if not package_exists and not authority_exists:
        if preflight["package_triggers"] or preflight["authority_triggers"]:
            return ApprovedContentPackageCutoverState.INVALID
        return ApprovedContentPackageCutoverState.ABSENT

    if package_exists and not table_matches_manifest(preflight["package_schema"], table_name=PACKAGE_TABLE_NAME):
        return ApprovedContentPackageCutoverState.INVALID

    if authority_exists and not table_matches_manifest(
        preflight["authority_schema"], table_name=AUTHORITY_TABLE_NAME
    ):
        return ApprovedContentPackageCutoverState.INVALID

    if not package_exists:
        # Authority table (or its triggers) exist without the package table
        # -- never a legal state under our fixed install ordering.
        return ApprovedContentPackageCutoverState.INVALID

    package_trigger_state = _classify_triggers(preflight["package_triggers"], PACKAGE_TRIGGER_NAMES)
    if package_trigger_state == "INVALID":
        return ApprovedContentPackageCutoverState.INVALID
    if package_trigger_state == "MISSING":
        if authority_exists or preflight["authority_triggers"]:
            return ApprovedContentPackageCutoverState.INVALID
        return ApprovedContentPackageCutoverState.PACKAGE_TABLE_TRIGGERS_PENDING

    if not authority_exists:
        if preflight["authority_triggers"]:
            return ApprovedContentPackageCutoverState.INVALID
        return ApprovedContentPackageCutoverState.AUTHORITY_TABLE_PENDING

    authority_trigger_state = _classify_triggers(preflight["authority_triggers"], AUTHORITY_TRIGGER_NAMES)
    if authority_trigger_state == "INVALID":
        return ApprovedContentPackageCutoverState.INVALID
    if authority_trigger_state == "MISSING":
        return ApprovedContentPackageCutoverState.AUTHORITY_TRIGGERS_PENDING

    return ApprovedContentPackageCutoverState.READY


def _install_triggers(engine: Engine, names: frozenset[str]) -> None:
    with engine.begin() as conn:
        for name in names:
            conn.exec_driver_sql(TRIGGER_SQL_BY_NAME[name])


def run_cutover_installer(engine: Engine) -> ApprovedContentPackageCutoverState:
    """Idempotent, resumable, ordered installer. Always fail-closed: raises
    ``RuntimeError`` immediately if the starting state (or any state reached
    mid-way) is ``INVALID``, and raises if the final state after every legal
    step is anything other than ``READY``. Never attempts an auto-repair of
    an object with a wrong schema/body."""

    state = evaluate_cutover_state(engine)
    if state == ApprovedContentPackageCutoverState.INVALID:
        raise RuntimeError("ERROR_INCOMPATIBLE_APPROVED_CONTENT_PACKAGE_SCHEMA:state=INVALID")
    if state == ApprovedContentPackageCutoverState.READY:
        return state

    if state == ApprovedContentPackageCutoverState.ABSENT:
        CvApprovedContentPackageRow.__table__.create(engine, checkfirst=True)
        state = evaluate_cutover_state(engine)
        if state == ApprovedContentPackageCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_APPROVED_CONTENT_PACKAGE_SCHEMA:state=INVALID:after=create_package_table"
            )

    if state == ApprovedContentPackageCutoverState.PACKAGE_TABLE_TRIGGERS_PENDING:
        _install_triggers(engine, PACKAGE_TRIGGER_NAMES)
        state = evaluate_cutover_state(engine)
        if state == ApprovedContentPackageCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_APPROVED_CONTENT_PACKAGE_SCHEMA:state=INVALID:after=install_package_triggers"
            )

    if state == ApprovedContentPackageCutoverState.AUTHORITY_TABLE_PENDING:
        CvApprovedContentCurrentAuthorityRow.__table__.create(engine, checkfirst=True)
        state = evaluate_cutover_state(engine)
        if state == ApprovedContentPackageCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_APPROVED_CONTENT_PACKAGE_SCHEMA:state=INVALID:after=create_authority_table"
            )

    if state == ApprovedContentPackageCutoverState.AUTHORITY_TRIGGERS_PENDING:
        _install_triggers(engine, AUTHORITY_TRIGGER_NAMES)
        state = evaluate_cutover_state(engine)
        if state == ApprovedContentPackageCutoverState.INVALID:
            raise RuntimeError(
                "ERROR_INCOMPATIBLE_APPROVED_CONTENT_PACKAGE_SCHEMA:state=INVALID:after=install_authority_triggers"
            )

    if state != ApprovedContentPackageCutoverState.READY:
        raise RuntimeError(f"ERROR_INCOMPATIBLE_APPROVED_CONTENT_PACKAGE_SCHEMA:state={state}")
    return state
