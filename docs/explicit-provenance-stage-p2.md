# Explicit Provenance — Stage 10D-A-P2

## Purpose and scope

P2 is a controlled, idempotent migration bridge from the legacy Truth Library
JSON into the P1 Explicit Provenance foundation (`TruthEntity`, `TruthFact`)
plus a new audit ledger, `truth_legacy_migration_map`.

P2 does **not** connect `TruthFact` to CV generation, does not change any
active Stage 10A/10B/10C/10D flow, does not enable
`EXPLICIT_PROVENANCE_ENABLED`, does not run automatically at application
startup, and never modifies or deletes the legacy Truth Library JSON. It never
creates `TruthPermission`, `TruthEvidence`, or `TruthFactVariant` rows — see
"Why no Permission/Evidence/Variant" below.

## Configuration

Three new settings (`app/config.py`, `.env.example`):

- `TRUTH_LEGACY_MIGRATION_ENABLED` (bool, default `false`) — gates `--apply`
  only; `--dry-run` never requires it.
- `TRUTH_LIBRARY_PERSON_ENTITY_ID` (UUID4, optional) — the installation's
  single PERSON entity. The CLI's `--person-entity-id` takes precedence.
- `TRUTH_LIBRARY_SOURCE_ID` (str, default `truth_library_primary`) — a stable
  *logical* source id that participates in legacy identity and fingerprints.
  It is deliberately distinct from the physical file path, which varies
  across Mac/Docker/CI and must never change migrated identity.

The migrator never writes `TRUTH_LIBRARY_PERSON_ENTITY_ID` back to `.env` and
never derives a UUID from `meta.kandidat` or any other legacy text.

## Schema: `truth_legacy_migration_map`

One row per legacy record considered for migration, whether or not it
produced a `TruthEntity`/`TruthFact` — a REJECTED or UNSUPPORTED record still
gets a ledger row with `target_entity_id`/`target_fact_id` left `NULL`.
Columns, constraints, and indexes are listed in `app/models.py`
(`TruthLegacyMigrationMap`). Identity: `migration_map_id` (UUIDv4).
`legacy_source_path` is audit metadata only — excluded from identity and the
unique constraint, which is `(person_entity_id, legacy_source_id,
migration_schema_version, legacy_category, legacy_record_key)`.

## Preflight ordering (non-negotiable)

`app/db_engine.py::init_models_sync` runs, in this exact order, before any
DDL:

1. `preflight_explicit_provenance_p1_schema(engine)` — read-only; raises on
   an incompatible existing P1 schema.
2. `preflight_explicit_provenance_p2_schema(engine)` — read-only; raises on an
   incompatible existing P2 table or a case-mismatched table name
   (`ERROR_INCOMPATIBLE_P2_SCHEMA_NAME` / `ERROR_INCOMPATIBLE_P2_SCHEMA`).

Only once both preflights return `ABSENT_CREATE_REQUIRED` or
`P1_SCHEMA_READY`/`P2_SCHEMA_READY` does any `CREATE TABLE`/`CREATE
INDEX`/`CREATE TRIGGER` run. Either preflight raising aborts the whole call
before a single mutation — an incompatible P2 table can never let P1 get
created, and vice versa.

`truth_legacy_migration_map` is excluded from every generic/broad
`create_all` path (`Base.metadata.create_all(engine, tables=non_p2_tables)`
on first startup, and the generic additive loop that creates any
non-P1/non-P2 table when P1 is already ready). It is only ever created via
its own controlled branch, keyed off `preflight_explicit_provenance_p2_schema`
returning `ABSENT_CREATE_REQUIRED`. First startup on an empty database still
creates every legacy table and all five P1 tables via the broad path, then
creates the P2 table via its controlled path. Second and third startups are
read-only for both P1 and P2 managed schemas — see
`tests/integration/test_truth_legacy_migration_schema.py` and
`tests/integration/test_truth_legacy_migration_restart.py`.

The P2 manifest collector (`_collect_p2_schema_manifest`) reuses the same
per-table introspection helper as P1 (`_collect_single_table_manifest`,
extracted from the P1 collector without changing its output) and the same
reference-database comparison technique, so the same structural checks apply:
table name (case-insensitive), full column set/order/types/nullability,
primary key, foreign keys and `ON DELETE`, CHECK/UNIQUE constraints, indexes,
and defaults.

## Legacy status classification (`app/services/truth_legacy_classification.py`)

Pure functions, no I/O. Legacy status → classification:

| Legacy status | Classification | TruthFact.status |
|---|---|---|
| `PRAWDA_ZATWIERDZONA_PRZEZ_UŻYTKOWNIKA`, `PRAWDA_BEZPOŚREDNIA` | MIGRATABLE (unless booleans inconsistent) | CONFIRMED |
| `TRANSFEROWALNE_RYZYKOWNE`, `PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA` | REQUIRES_REVIEW | REVIEW_REQUIRED |
| `NIEPOTWIERDZONE_ZABLOKOWAĆ`, `NIEJASNE`, `USUNIĘTE` | REJECTED | — |
| missing / unknown | REQUIRES_REVIEW | REVIEW_REQUIRED |

`uzywacWCV=false` or `wymagaAkceptacji=true` on an otherwise-MIGRATABLE record
downgrades it to REQUIRES_REVIEW — never to REJECTED, and never upgrades a
REQUIRES_REVIEW/REJECTED record. `PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA` can
never resolve to REJECTED.

Category overrides layered on top of status classification:

- `branze` (`zatwierdzoneBranze`): always REQUIRES_REVIEW; ledger row only,
  never an auto-confirmed fact (and, per P2 scope, never any fact at all).
- `daneOsobowe`: always UNSUPPORTED; ledger row only, never a `TruthFact`.
- `zakazy`, `reguly`, `tranferowalneKompetencje`: DEFERRED_POLICY — no ledger
  row, no `TruthFact`, no mutation of any kind.
- Any record without a stable legacy id (explicit for `osiagnieciaLiczbowe`
  and `skalaOdpowiedzialnosci`, applied uniformly to every category) can never
  resolve to MIGRATABLE — content-fingerprint-only identity is downgraded to
  REQUIRES_REVIEW.

## Legacy identity and fingerprints (`app/services/truth_legacy_migrator.py`)

Never used as identity: list index, `source_reference`/`zrodlo`, company,
role, `meta.kandidat`, the physical file path, or bare text without parent
context. `legacy_source_id` (the stable *logical* source, default
`truth_library_primary`) participates in identity/fingerprint/uniqueness;
`legacy_source_path` never does.

- **Named-identity categories** (`kompetencje`, `narzedzia`, `technologie`,
  `certyfikaty`, `kursy`, `wyksztalcenie`, `jezyki`): identity is the
  normalized name field (`nazwa`/`kierunek`/`jezyk`); the record's full
  content (all fields, including `nazwaAlt`) feeds the fingerprint, so a
  content-only change (e.g. `poziom`) is detected as a change against the
  same key without needing a separate `TruthFactVariant`.
- **`doswiadczenieZawodowe` (employment)**: identity is `entry["id"]` if
  present, otherwise a content fingerprint of the whole record (forcing
  REQUIRES_REVIEW, per the no-stable-id rule). Children (`aktywnosci`,
  `wynikLiczbowy`, `skalaOdpowiedzialnosci`) key off the *parent's* identity —
  never their own list index. `aktywnosci` entries additionally embed a
  content fingerprint in their key (no stable per-activity id exists), so two
  identical activity strings in the same employment collapse onto one ledger
  row and one `TruthFact` — reported as `duplicate_text_collisions` in the
  dry-run report (raw item count vs. unique identity count). Reordering
  employment entries or activities never changes any identity, because
  nothing derives from position.
- **Global `osiagnieciaLiczbowe`/`skalaOdpowiedzialnosci`**: identity is
  `entry["id"]` if present, else content fingerprint (forced
  REQUIRES_REVIEW).
- **`branze`/`daneOsobowe`**: identity is a content fingerprint of the item
  (no id concept applies).

### Parent employment conflict gate

Before any child of an employment record (`aktywnosci`, `wynikLiczbowy`,
`skalaOdpowiedzialnosci`) is processed, the employment root's own ledger
upsert runs first. If a ledger row already exists for that root's
`legacy_record_key` with a **different** content fingerprint,
`LegacyRecordChangedReviewRequiredError`
(`LEGACY_RECORD_CHANGED_REVIEW_REQUIRED`) is raised immediately — before any
child is read — aborting the whole `apply()` transaction. Changed child text
alone can never bypass this: children share the parent's status/booleans
verbatim (the legacy schema has no per-activity status), so the parent's own
fingerprint check is the actual gate.

This is one instance of the general **change policy**, applied uniformly to
every ledger row, not just employment: same `legacy_record_key` + same
fingerprint is an idempotent replay (zero new records); same key + different
fingerprint raises `LEGACY_RECORD_CHANGED_REVIEW_REQUIRED` and aborts with a
full rollback. P2 never auto-creates a new revision, archives the old fact, or
merges changes.

## Dry-run vs. apply

Both call the same pure `build_plan(library)` (identity + classification), so
their decisions can never drift apart.

- **Dry-run**: no database engine involved at all; reads only the legacy JSON
  (via the existing read-only `load_truth_library`). Reports counts by
  classification, a per-category breakdown (`PROCESSED` with counts, or
  `DEFERRED_POLICY` with a reason), planned identities/fingerprints,
  duplicate-text collisions, and `person_entity_id_required_for_apply` when no
  person id is resolvable. Never requires
  `TRUTH_LEGACY_MIGRATION_ENABLED=true`.
- **Apply**: requires `TRUTH_LEGACY_MIGRATION_ENABLED=true` and a resolvable
  `person_entity_id`. Runs inside exactly one `TruthService.transaction()` —
  `TruthEntity`, `TruthFact`, and `TruthLegacyMigrationMap` rows all commit or
  roll back together. The migrator's own repository flushes; it never calls
  `session.commit()` and never opens a second session. A `PERSON` entity is
  created with the given UUID only if it does not already exist; if it exists
  with `entity_type != PERSON`, `PersonEntityTypeMismatchError`
  (`PERSON_ENTITY_TYPE_MISMATCH`) is raised **before** any migration write.

## Entity/fact mapping

`PERSON` is the one explicit owner entity (no `owner_profile_id` is ever
created). `EMPLOYMENT`, `SKILL`, `TOOL`, `TECHNOLOGY`, `CERTIFICATION`,
`COURSE`, `EDUCATION`, `LANGUAGE` all get `parent_entity_id = person_entity_id`.
Global (non-employment) achievements/scale facts attach directly to `PERSON`.
No entity is ever created for a fully REJECTED record with no materialized
child fact. `zrodlo`, when present, is preserved as
`value_json.legacy_source_label`; `TruthFact.source_reference` is always
`truth_library:<legacy_record_key>`. Alt fields (`stanowiskoAlt`, `nazwaAlt`)
feed the record's content fingerprint but never produce a
`TruthFactVariant`.

## Why no `TruthPermission` / `TruthEvidence` / `TruthFactVariant`

- **`TruthPermission`**: the legacy library has no `target_scope` or
  `allowed_operations` — inventing them would fabricate policy P2 has no
  authority to decide.
- **`TruthEvidence`**: legacy `zrodlo` is free text, not a real document with
  `content_hash`/`captured_at`.
- **`TruthFactVariant`**: `stanowiskoAlt`/`nazwaAlt` have no unambiguous base
  `fact_id` to attach a variant to within P2's approved scope; they are
  embedded content instead.

## CLI

```bash
uv run python -m app.scripts.migrate_truth_library_to_provenance --dry-run
uv run python -m app.scripts.migrate_truth_library_to_provenance --apply \
    --person-entity-id <uuid4>
```

`--dry-run` and `--apply` are mutually exclusive; one is required.
`--person-entity-id`, `--truth-library-path`, and `--legacy-source-id` are all
optional overrides of the corresponding settings. No API route or router is
added — this is a CLI-only bridge.
