"""Explicit Provenance P2 CLI — legacy Truth Library migration bridge.

Usage::

    uv run python -m app.scripts.migrate_truth_library_to_provenance --dry-run
    uv run python -m app.scripts.migrate_truth_library_to_provenance --apply \\
        --person-entity-id <uuid4>

``--dry-run`` and ``--apply`` are mutually exclusive and one is required.
``--dry-run`` never touches the database or the legacy JSON file.
``--apply`` requires ``TRUTH_LEGACY_MIGRATION_ENABLED=true`` and a resolvable
``person_entity_id`` (``--person-entity-id`` takes precedence over
``TRUTH_LIBRARY_PERSON_ENTITY_ID``); it never runs automatically at app
startup. See docs/explicit-provenance-stage-p2.md for the full design.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.schemas.truth_legacy_migration import MigrationReport
from app.services.truth_legacy_migrator import (
    LegacyRecordChangedReviewRequiredError,
    PersonEntityIdRequiredError,
    PersonEntityTypeMismatchError,
    apply as apply_migration,
    dry_run as dry_run_migration,
)

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_truth_library_to_provenance",
        description=(
            "Explicit Provenance P2 — migrate legacy Truth Library JSON into "
            "the P1 foundation (TruthEntity/TruthFact) plus an audit ledger. "
            "Read-only for the legacy file in both modes; --apply writes to "
            "the database in exactly one transaction."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Plan only; no writes.")
    mode.add_argument("--apply", action="store_true", help="Migrate for real.")
    parser.add_argument(
        "--person-entity-id",
        type=str,
        default=None,
        help=(
            "UUID4 identity of the PERSON entity that owns this installation's "
            "Truth Library. Takes precedence over TRUTH_LIBRARY_PERSON_ENTITY_ID. "
            "Required for --apply; optional for --dry-run."
        ),
    )
    parser.add_argument(
        "--truth-library-path",
        type=str,
        default=None,
        help="Path to the legacy Truth Library JSON. Defaults to settings.truth_library_path.",
    )
    parser.add_argument(
        "--legacy-source-id",
        type=str,
        default=None,
        help=(
            "Stable logical source id (NOT a filesystem path) used in legacy "
            "identity/fingerprints. Defaults to settings.truth_library_source_id."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print the full MigrationReport as JSON to stdout.",
    )
    return parser


def _resolve_person_entity_id(cli_value: str | None) -> str | None:
    if cli_value is not None:
        UUID(cli_value)  # raises ValueError on malformed input; let argparse surface it
        return cli_value
    if settings.truth_library_person_entity_id is not None:
        return str(settings.truth_library_person_entity_id)
    return None


def _print_report(report: MigrationReport) -> None:
    print(f"Explicit Provenance P2 — {report.mode}")
    print(f"  legacy_source_id:   {report.legacy_source_id}")
    print(f"  legacy_source_path: {report.legacy_source_path}")
    print(f"  person_entity_id:   {report.person_entity_id or '(none)'}")
    if report.person_entity_id_required_for_apply:
        print("  NOTICE: PERSON_ENTITY_ID_REQUIRED_FOR_APPLY")
    print(
        "  counts: "
        f"MIGRATABLE={report.counts.MIGRATABLE} "
        f"REQUIRES_REVIEW={report.counts.REQUIRES_REVIEW} "
        f"REJECTED={report.counts.REJECTED} "
        f"UNSUPPORTED={report.counts.UNSUPPORTED}"
    )
    print("  categories:")
    for category_plan in report.category_plans:
        print(
            f"    - {category_plan.category:<24} {category_plan.disposition.value:<16} "
            f"records={category_plan.record_count:<4} reason={category_plan.reason}"
        )
    if report.duplicate_text_collisions:
        print("  duplicate_text_collisions:")
        for collision in report.duplicate_text_collisions:
            print(
                f"    - {collision.category}:{collision.legacy_record_key} "
                f"raw={collision.raw_item_count} unique={collision.unique_identity_count}"
            )
    if report.fingerprint_conflicts:
        print("  fingerprint_conflicts (LEGACY_RECORD_CHANGED_REVIEW_REQUIRED):")
        for conflict in report.fingerprint_conflicts:
            print(f"    - {conflict.category}:{conflict.legacy_record_key}")
    if report.mode == "APPLY":
        print(
            f"  created: entities={len(report.created_entity_ids)} "
            f"facts={len(report.created_fact_ids)} "
            f"map_rows={len(report.created_map_row_ids)} "
            f"reused_map_rows={report.reused_map_row_count}"
        )
    if report.aborted:
        print(f"  ABORTED: {report.abort_reason}")


async def _run_apply(
    *, person_entity_id: str, truth_library_path: Path, legacy_source_id: str
) -> MigrationReport:
    if not settings.truth_legacy_migration_enabled:
        raise PersonEntityIdRequiredError(
            "TRUTH_LEGACY_MIGRATION_ENABLED_REQUIRED_FOR_APPLY"
        )
    from app.database import db

    return await apply_migration(
        database=db,
        truth_library_path=truth_library_path,
        person_entity_id=person_entity_id,
        legacy_source_id=legacy_source_id,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    truth_library_path = (
        Path(args.truth_library_path) if args.truth_library_path else settings.truth_library_path
    )
    legacy_source_id = args.legacy_source_id or settings.truth_library_source_id

    try:
        person_entity_id = _resolve_person_entity_id(args.person_entity_id)
    except ValueError:
        print("ERROR: --person-entity-id must be a valid UUID4.", file=sys.stderr)
        return 2

    if args.dry_run:
        report = dry_run_migration(
            truth_library_path=truth_library_path,
            legacy_source_id=legacy_source_id,
            person_entity_id=person_entity_id,
        )
        _print_report(report)
        if args.json:
            print(report.model_dump_json(indent=2))
        return 0

    # --apply
    if person_entity_id is None:
        print(
            "ERROR: PERSON_ENTITY_ID_REQUIRED_FOR_APPLY "
            "(pass --person-entity-id or set TRUTH_LIBRARY_PERSON_ENTITY_ID).",
            file=sys.stderr,
        )
        return 2

    try:
        report = asyncio.run(
            _run_apply(
                person_entity_id=person_entity_id,
                truth_library_path=truth_library_path,
                legacy_source_id=legacy_source_id,
            )
        )
    except PersonEntityIdRequiredError:
        print(
            "ERROR: TRUTH_LEGACY_MIGRATION_ENABLED must be true to run --apply.",
            file=sys.stderr,
        )
        return 2
    except PersonEntityTypeMismatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3
    except LegacyRecordChangedReviewRequiredError as error:
        print(f"ERROR: {error} — full rollback performed, zero mutations applied.", file=sys.stderr)
        return 4

    _print_report(report)
    if args.json:
        print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
