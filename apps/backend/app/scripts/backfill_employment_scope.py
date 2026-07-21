"""Explicit Provenance P3.5 CLI — employment_scope_entity_id compatibility backfill.

Usage::

    uv run python -m app.scripts.backfill_employment_scope --dry-run \\
        --person-entity-id <uuid4>
    uv run python -m app.scripts.backfill_employment_scope --apply \\
        --person-entity-id <uuid4>

``--dry-run`` and ``--apply`` are mutually exclusive and one is required.
``--person-entity-id`` is required for both modes: this tool never selects a
person on its own and never scans every person in the database. See
docs/explicit-provenance-stage-p2.md for the full compatibility design.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from app.services.truth_employment_scope_backfill import (
    EmploymentScopeBackfillReport,
    EmploymentScopeConflictError,
    PersonEntityNotFoundError,
    PersonEntityTypeMismatchError,
    apply as apply_backfill,
    dry_run as dry_run_backfill,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_employment_scope",
        description=(
            "Explicit Provenance P3.5 — deterministically backfill "
            "employment_scope_entity_id on pre-existing employment facts "
            "for exactly one PERSON entity. Read-only in --dry-run; "
            "--apply writes in exactly one all-or-nothing transaction."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Plan only; no writes.")
    mode.add_argument("--apply", action="store_true", help="Repair for real.")
    parser.add_argument(
        "--person-entity-id",
        type=str,
        required=True,
        help="UUID4 identity of the PERSON entity whose employment facts to backfill.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print the full EmploymentScopeBackfillReport as JSON to stdout.",
    )
    return parser


def _print_report(report: EmploymentScopeBackfillReport) -> None:
    print(f"Explicit Provenance P3.5 — {report.mode}")
    print(f"  compatibility_version: {report.compatibility_version}")
    print(f"  person_entity_id:      {report.person_entity_id}")
    counts: dict[str, int] = {}
    for item in report.items:
        counts[item.classification.value] = counts.get(item.classification.value, 0) + 1
    print(
        "  counts: "
        + " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    )
    print("  items:")
    for item in report.items:
        print(
            f"    - fact_id={item.fact_id} fact_type={item.fact_type:<32} "
            f"classification={item.classification.value:<12} reason={item.reason_code.value}"
        )
    if report.mode == "APPLY":
        print(f"  repaired: {len(report.repaired_fact_ids)}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        UUID(args.person_entity_id)
    except ValueError:
        print("ERROR: --person-entity-id must be a valid UUID4.", file=sys.stderr)
        return 2

    from app.database import db

    try:
        if args.dry_run:
            report = asyncio.run(
                dry_run_backfill(database=db, person_entity_id=args.person_entity_id)
            )
        else:
            report = asyncio.run(
                apply_backfill(database=db, person_entity_id=args.person_entity_id)
            )
    except PersonEntityNotFoundError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3
    except PersonEntityTypeMismatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3
    except EmploymentScopeConflictError as error:
        print(
            f"ERROR: {error} — full rollback performed, zero mutations applied.",
            file=sys.stderr,
        )
        return 4

    _print_report(report)
    if args.json:
        print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
