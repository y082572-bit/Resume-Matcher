"""Service-level tests for the P2 dry-run planner (no database, no I/O beyond
reading the legacy JSON fixture)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.schemas.truth_legacy_migration import CategoryDisposition, LegacyClassification
from app.services.truth_legacy_migrator import dry_run

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def _write_library(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "truth-library.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _rich_library() -> dict:
    return {
        "meta": {"kandidat": "Osoba testowa", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1",
                        "firma": "Firma testowa",
                        "stanowisko": "Analityk",
                        "status": APPROVED,
                        "uzywacWCV": True,
                        "aktywnosci": ["Zrobił X", "Zrobił X", "Zrobił Y"],
                    },
                    {
                        "id": "dz-2",
                        "firma": "Inna firma",
                        "stanowisko": "Specjalista",
                        "status": "NIEJASNE",
                        "uzywacWCV": False,
                    },
                ]
            },
            "kompetencje": {"wpisy": [{"nazwa": "SQL", "status": APPROVED, "uzywacWCV": True}]},
            "osiagnieciaLiczbowe": {"wpisy": [{"opis": "wzrost 20%", "status": APPROVED, "uzywacWCV": True}]},
            "branze": {"zatwierdzoneBranze": ["Finanse"]},
            "daneOsobowe": {"wpisy": [{"pole": "wartość"}]},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": ["NIEJASNE"],
            },
            "tranferowalneKompetencje": {"mapowania": {}},
        },
    }


def test_dry_run_makes_no_writes(tmp_path: Path) -> None:
    payload = _rich_library()
    path = _write_library(tmp_path, payload)
    before_bytes = path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = path.stat().st_mtime

    dry_run(truth_library_path=path, legacy_source_id="truth_library_primary")

    after_bytes = path.read_bytes()
    assert hashlib.sha256(after_bytes).hexdigest() == before_hash
    assert after_bytes == before_bytes
    assert path.stat().st_mtime == before_mtime


def test_dry_run_complete_category_report(tmp_path: Path) -> None:
    path = _write_library(tmp_path, _rich_library())
    report = dry_run(truth_library_path=path, legacy_source_id="truth_library_primary")

    categories = {plan.category: plan for plan in report.category_plans}
    expected_categories = {
        "doswiadczenieZawodowe", "osiagnieciaLiczbowe", "skalaOdpowiedzialnosci",
        "kompetencje", "narzedzia", "technologie", "certyfikaty", "kursy",
        "wyksztalcenie", "jezyki", "branze", "daneOsobowe",
        "zakazy", "reguly", "tranferowalneKompetencje",
    }
    assert set(categories) == expected_categories
    assert categories["zakazy"].disposition == CategoryDisposition.DEFERRED_POLICY
    assert categories["reguly"].disposition == CategoryDisposition.DEFERRED_POLICY
    assert categories["tranferowalneKompetencje"].disposition == CategoryDisposition.DEFERRED_POLICY
    for category in expected_categories - {"zakazy", "reguly", "tranferowalneKompetencje"}:
        assert categories[category].disposition == CategoryDisposition.PROCESSED


def test_dry_run_counts_by_classification(tmp_path: Path) -> None:
    path = _write_library(tmp_path, _rich_library())
    report = dry_run(truth_library_path=path, legacy_source_id="truth_library_primary")

    # dz-1 root (MIGRATABLE) + 2 unique activities (MIGRATABLE) = 3
    # dz-2 root (REJECTED, NIEJASNE)
    # kompetencje SQL (MIGRATABLE)
    # osiagnieciaLiczbowe (REQUIRES_REVIEW: no stable id)
    # branze (REQUIRES_REVIEW)
    # daneOsobowe (UNSUPPORTED)
    assert report.counts.MIGRATABLE == 4
    assert report.counts.REQUIRES_REVIEW == 2
    assert report.counts.REJECTED == 1
    assert report.counts.UNSUPPORTED == 1


def test_dry_run_skipped_category_reasons(tmp_path: Path) -> None:
    path = _write_library(tmp_path, _rich_library())
    report = dry_run(truth_library_path=path, legacy_source_id="truth_library_primary")
    deferred = {p.category: p.reason for p in report.category_plans if p.disposition == CategoryDisposition.DEFERRED_POLICY}
    assert "zakazy" in deferred and deferred["zakazy"]
    assert "reguly" in deferred and deferred["reguly"]
    assert "tranferowalneKompetencje" in deferred and deferred["tranferowalneKompetencje"]
    for reason in deferred.values():
        assert "no ledger row" in reason


def test_dry_run_reports_missing_person_id() -> None:
    payload = _rich_library()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_library(Path(tmp), payload)
        report = dry_run(truth_library_path=path, legacy_source_id="truth_library_primary")
        assert report.person_entity_id is None
        assert report.person_entity_id_required_for_apply is True


def test_dry_run_with_person_id_does_not_flag_missing(tmp_path: Path) -> None:
    path = _write_library(tmp_path, _rich_library())
    person_id = str(uuid4())
    report = dry_run(
        truth_library_path=path, legacy_source_id="truth_library_primary", person_entity_id=person_id
    )
    assert report.person_entity_id_required_for_apply is False
    assert str(report.person_entity_id) == person_id


def test_dry_run_reports_duplicate_collisions(tmp_path: Path) -> None:
    path = _write_library(tmp_path, _rich_library())
    report = dry_run(truth_library_path=path, legacy_source_id="truth_library_primary")
    assert len(report.duplicate_text_collisions) == 1
    collision = report.duplicate_text_collisions[0]
    assert collision.category == "doswiadczenieZawodowe"
    assert collision.raw_item_count == 2
    assert collision.unique_identity_count == 1


def test_employment_root_planned_before_its_children() -> None:
    """apply() relies on root-before-children ordering for the parent
    employment conflict gate to actually gate anything."""

    from app.services.truth_legacy_migrator import build_plan

    library = {
        "meta": {"kandidat": "X", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {
                "wpisy": [
                    {
                        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst",
                        "status": APPROVED, "uzywacWCV": True,
                        "aktywnosci": ["Did A"], "wynikLiczbowy": "+20%",
                    }
                ]
            },
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [], "statusyZablokowane": [],
            },
        },
    }
    built = build_plan(library)
    employment_records = [r for r in built.records if r.category == "doswiadczenieZawodowe"]
    root = next(r for r in employment_records if r.owner == "SELF")
    root_index = employment_records.index(root)
    for index, record in enumerate(employment_records):
        if record.owner == "PARENT":
            assert index > root_index
            assert record.classification == LegacyClassification.MIGRATABLE
