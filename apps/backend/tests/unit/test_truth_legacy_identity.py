"""Unit tests for legacy identity/fingerprint construction (P2), pure JSON -> plan."""

from __future__ import annotations

from app.services.truth_legacy_migrator import build_plan

APPROVED = "PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA"


def _library(entries: list[dict]) -> dict:
    return {
        "meta": {"kandidat": "Osoba testowa", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {"wpisy": entries},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": [],
            },
        },
    }


def _keys_by_category(built, category: str) -> list[str]:
    return [r.record_key for r in built.records if r.category == category]


def test_stable_legacy_id_used_in_key() -> None:
    library = _library(
        [{"id": "dz-42", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}]
    )
    built = build_plan(library)
    root = next(r for r in built.records if r.category == "doswiadczenieZawodowe")
    assert "dz-42" in root.record_key
    assert root.has_stable_id is True


def test_reorder_employment_entries_does_not_change_identity() -> None:
    entry_a = {"id": "dz-a", "firma": "A", "stanowisko": "X", "status": APPROVED, "uzywacWCV": True}
    entry_b = {"id": "dz-b", "firma": "B", "stanowisko": "Y", "status": APPROVED, "uzywacWCV": True}
    keys_forward = sorted(_keys_by_category(build_plan(_library([entry_a, entry_b])), "doswiadczenieZawodowe"))
    keys_reversed = sorted(_keys_by_category(build_plan(_library([entry_b, entry_a])), "doswiadczenieZawodowe"))
    assert keys_forward == keys_reversed


def test_reorder_activities_does_not_change_identity() -> None:
    entry_forward = {
        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True,
        "aktywnosci": ["Did A", "Did B"],
    }
    entry_reversed = {
        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True,
        "aktywnosci": ["Did B", "Did A"],
    }
    keys_forward = sorted(_keys_by_category(build_plan(_library([entry_forward])), "doswiadczenieZawodowe"))
    keys_reversed = sorted(_keys_by_category(build_plan(_library([entry_reversed])), "doswiadczenieZawodowe"))
    assert keys_forward == keys_reversed


def test_fingerprint_changes_on_content_change() -> None:
    entry = {"id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}
    built_before = build_plan(_library([entry]))
    root_before = next(r for r in built_before.records if r.category == "doswiadczenieZawodowe")

    changed = {**entry, "stanowisko": "Senior Analyst"}
    built_after = build_plan(_library([changed]))
    root_after = next(r for r in built_after.records if r.category == "doswiadczenieZawodowe")

    assert root_before.record_key == root_after.record_key  # same identity
    assert root_before.fingerprint != root_after.fingerprint  # different content


def test_no_physical_path_in_fingerprint() -> None:
    """build_plan never receives or uses a filesystem path — fingerprints are
    purely a function of the JSON content."""

    entry = {"id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}
    built_a = build_plan(_library([entry]))
    built_b = build_plan(_library([entry]))
    root_a = next(r for r in built_a.records if r.category == "doswiadczenieZawodowe")
    root_b = next(r for r in built_b.records if r.category == "doswiadczenieZawodowe")
    assert root_a.fingerprint == root_b.fingerprint


def test_logical_source_id_not_baked_into_fingerprint_function() -> None:
    """legacy_source_id is a separate ledger/unique-constraint column (see
    apply()), not part of the pure content fingerprint computed here — so the
    same content always fingerprints identically regardless of which source id
    it is later filed under."""

    entry = {"id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}
    built = build_plan(_library([entry]))
    root = next(r for r in built.records if r.category == "doswiadczenieZawodowe")
    assert "truth_library_primary" not in root.fingerprint
    assert "truth_library_primary" not in root.record_key


def test_duplicate_activity_collision_collapses_to_one_record() -> None:
    entry = {
        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True,
        "aktywnosci": ["Same text", "Same text", "Different text"],
    }
    built = build_plan(_library([entry]))
    activity_keys = [
        r.record_key for r in built.records
        if r.category == "doswiadczenieZawodowe" and "aktywnosci" in r.record_key
    ]
    assert len(activity_keys) == 2  # "Same text" collapsed, "Different text" distinct
    assert len(built.duplicate_collisions) == 1
    collision = built.duplicate_collisions[0]
    assert collision.raw_item_count == 2
    assert collision.unique_identity_count == 1


def test_no_list_index_in_key() -> None:
    entry = {
        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True,
        "aktywnosci": ["First activity", "Second activity"],
    }
    built = build_plan(_library([entry]))
    activity_records = [
        r for r in built.records
        if r.category == "doswiadczenieZawodowe" and "aktywnosci" in r.record_key
    ]
    for record in activity_records:
        assert ":aktywnosci:0" not in record.record_key
        assert ":aktywnosci:1" not in record.record_key


def test_no_meta_kandidat_in_key_or_fingerprint() -> None:
    library_a = _library(
        [{"id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}]
    )
    library_a["meta"]["kandidat"] = "Jan Kowalski"
    library_b = _library(
        [{"id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}]
    )
    library_b["meta"]["kandidat"] = "Someone Else Entirely"

    root_a = next(r for r in build_plan(library_a).records if r.category == "doswiadczenieZawodowe")
    root_b = next(r for r in build_plan(library_b).records if r.category == "doswiadczenieZawodowe")
    assert root_a.record_key == root_b.record_key
    assert root_a.fingerprint == root_b.fingerprint


def test_no_zrodlo_used_as_identity() -> None:
    entry_a = {
        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True,
        "zrodlo": "CV wersja 1",
    }
    entry_b = {
        "id": "dz-1", "firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True,
        "zrodlo": "Zupełnie inne źródło",
    }
    root_a = next(r for r in build_plan(_library([entry_a])).records if r.category == "doswiadczenieZawodowe")
    root_b = next(r for r in build_plan(_library([entry_b])).records if r.category == "doswiadczenieZawodowe")
    # Same key (identity unaffected by zrodlo) but different fingerprint
    # (zrodlo is still part of the record's *content*, just not its identity).
    assert root_a.record_key == root_b.record_key
    assert root_a.fingerprint != root_b.fingerprint


def test_employment_without_stable_id_uses_content_fingerprint_and_forces_review() -> None:
    entry = {"firma": "Acme", "stanowisko": "Analyst", "status": APPROVED, "uzywacWCV": True}
    built = build_plan(_library([entry]))
    root = next(r for r in built.records if r.category == "doswiadczenieZawodowe")
    assert root.has_stable_id is False
    assert root.fingerprint in root.record_key
    from app.schemas.truth_legacy_migration import LegacyClassification
    assert root.classification == LegacyClassification.REQUIRES_REVIEW


def test_named_category_identity_uses_normalized_name_not_content() -> None:
    library = {
        "meta": {"kandidat": "X", "wersja": "1.0", "status": "AKTYWNA"},
        "kategorie": {
            "doswiadczenieZawodowe": {"wpisy": []},
            "kompetencje": {"wpisy": [{"nazwa": "  Python  ", "poziom": "expert", "status": APPROVED, "uzywacWCV": True}]},
            "zakazy": {"bezwzgledneZakazy": [], "wyjatki": []},
            "reguly": {
                "statusyDoAutomatycznegoCV": [APPROVED],
                "statusyWymagajaceAkceptacji": [],
                "statusyZablokowane": [],
            },
        },
    }
    built = build_plan(library)
    record = next(r for r in built.records if r.category == "kompetencje")
    assert "python" in record.record_key.lower()
    assert record.has_stable_id is True
