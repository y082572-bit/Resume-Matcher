# Explicit Provenance Stage P6-B2A-I1: Candidate Identity Binding Foundation

## Purpose

This stage delivers an explicit, immutable, auditable binding:

```
TruthEntity(PERSON)
  ↔ Master Resume
  ↔ Resume.processed_data.personalInfo
  ↔ JobArtifactOwnerKey.person_entity_id
  ↔ (future) CvDocxHeaderBinding
```

It ships:

1. one new SQL table (`candidate_identity_bindings`),
2. four immutability/gating triggers,
3. closed, frozen Pydantic contracts (`schemas/candidate_identity_binding.py`),
4. a read-only repository Protocol + in-memory double,
5. the sole write authority, `CandidateIdentityBindingSqlService.create_binding`,
6. atomic header resolution, `CandidateIdentityHeaderSqlService.resolve_header_binding`,
7. an independent, hand-written schema manifest and fail-closed preflight,
8. a read-only reconciliation pass,
9. tests,
10. this document.

It does **not** implement the P6-B2A-R1 renderer remediation, the Proposal
DOCX composition root, a working copy, P6-B2b, a PDF converter, P6-B3, an
API, a frontend, or UI bootstrap. Those all remain out of scope here.

## The `candidate_identity_bindings` table

Exactly 5 columns:

| # | Column | Notes |
|---|--------|-------|
| 1 | `binding_fingerprint` | primary key, SHA-256 |
| 2 | `person_entity_id` | FK → `truth_entities.entity_id` (`ON DELETE RESTRICT`), UNIQUE |
| 3 | `master_resume_id` | FK → `resumes.resume_id` (`ON DELETE RESTRICT`), UNIQUE |
| 4 | `binding_schema_version` | literal `candidate-identity-binding-schema-v1` |
| 5 | `created_at` | audit metadata only — never part of identity |

`person_entity_id` and `master_resume_id` each carry their own single-column
`UNIQUE` constraint (not a composite) — one PERSON binds to exactly one
master Resume, and one master Resume binds to exactly one PERSON. There is
no `superseded`/`active`/`updated_at`/`personal_info_fingerprint` column, no
copy of personal data, no rebind revision, and no surrogate UUID primary
key.

## Immutability

The binding is completely immutable — no update, delete, rebind, supersede,
or replace exists anywhere in the public surface. Four triggers enforce
this at the SQLite layer (installed in `app/db_engine.py`, defined
independently — and duplicated intentionally — in
`candidate_identity_binding_schema_manifest.py` for preflight comparison):

- `trg_candidate_identity_bindings_person_type` — `BEFORE INSERT`, blocks
  unless a `TruthEntity` with `entity_type = 'PERSON'` exists for
  `NEW.person_entity_id`. `TruthEntity.status` is never a gate here.
- `trg_candidate_identity_bindings_resume_master` — `BEFORE INSERT`, blocks
  unless a `Resume` with `is_master = 1` exists for `NEW.master_resume_id`.
- `trg_candidate_identity_bindings_immutable` — `BEFORE UPDATE`, blocks
  every update unconditionally.
- `trg_candidate_identity_bindings_no_delete` — `BEFORE DELETE`, blocks
  every delete unconditionally.

## Sync-only persistence

Every new module uses exclusively `sqlalchemy.orm.Session`/`sessionmaker`,
synchronous `def`, `with session_factory() as session`, and
`with session.begin()`. Never `AsyncSession`, `async_sessionmaker`,
`async def`, an async engine, `await`, or a threadpool adapter. A
source/AST-level test
(`tests/unit/test_candidate_identity_binding_sql_service.py::test_new_modules_use_only_sync_sqlalchemy_persistence`)
confirms this across every new production module.

## Fingerprints

`binding_fingerprint` (in `candidate_identity_binding_repository.py`) is a
SHA-256 over canonical JSON of exactly `person_entity_id`, `master_resume_id`,
and `binding_schema_version` — never `created_at`, current time, a
filesystem path, or a random UUID.

`source_personal_info_fingerprint` (in `cv_docx_header_binding_builder.py`)
is a SHA-256 over canonical JSON of exactly the normalized `full_name`,
`email`, `phone`, `location`, and `linkedin` — never `website`, `github`,
`title`, a Resume id, or a timestamp. A website-only change leaves it
unchanged; a change to any of the five covered fields changes it.

## `CandidateIdentityBindingSqlService.create_binding`

The sole write authority over `candidate_identity_bindings`. Receives a
`sessionmaker`, never an open `Session`. The first attempt runs entirely
inside one `Session` and one transaction:

1. load `TruthEntity` → `PERSON_ENTITY_NOT_FOUND` / `ENTITY_IS_NOT_PERSON`,
2. load `Resume` → `MASTER_RESUME_NOT_FOUND` / `RESUME_IS_NOT_MASTER`,
3. validate `PersonalInfo` exists/parses/has a non-blank name →
   `PERSONAL_INFO_MISSING` / `PERSONAL_INFO_INVALID` / `FULL_NAME_MISSING`,
4. compute `binding_fingerprint`,
5. one combined pre-check `SELECT` (fingerprint OR person OR resume — never
   three separate statements, which would leave a window for an
   interleaving concurrent commit to be seen by only *some* of the checks)
   → `BINDING_ALREADY_EXISTS_IDENTICAL` / `PERSON_ALREADY_BOUND` /
   `MASTER_RESUME_ALREADY_BOUND`,
6. insert, flush, commit → `CREATED`.

A concurrent `IntegrityError` (two identical first-writers racing on the
same primary key) is never resolved by reusing that `Session` — it is
closed, and a **literally new** `Session` re-reads and classifies the
outcome (`BINDING_ALREADY_EXISTS_IDENTICAL` / `PERSON_ALREADY_BOUND` /
`MASTER_RESUME_ALREADY_BOUND` / `STORAGE_CONFLICT`). A session-factory or
`OperationalError` failure at either stage maps to `STORAGE_UNAVAILABLE`.
No raw `IntegrityError`/`OperationalError`/`SQLAlchemyError` ever leaves
`create_binding`.

Two identical concurrent requests always converge to `CREATED` for the
winner and `BINDING_ALREADY_EXISTS_IDENTICAL` for the loser — never a
spurious `PERSON_ALREADY_BOUND`/`MASTER_RESUME_ALREADY_BOUND`, regardless
of any `created_at` divergence between the two local computations.

Creation statuses (13, closed): `CREATED`, `PERSON_ENTITY_NOT_FOUND`,
`ENTITY_IS_NOT_PERSON`, `MASTER_RESUME_NOT_FOUND`, `RESUME_IS_NOT_MASTER`,
`PERSONAL_INFO_MISSING`, `PERSONAL_INFO_INVALID`, `FULL_NAME_MISSING`,
`BINDING_ALREADY_EXISTS_IDENTICAL`, `PERSON_ALREADY_BOUND`,
`MASTER_RESUME_ALREADY_BOUND`, `STORAGE_CONFLICT`, `STORAGE_UNAVAILABLE`.

## Read-only repository

`CandidateIdentityBindingRepository` (Protocol) and
`CandidateIdentityBindingSqlRepository`/`InMemoryCandidateIdentityBindingRepository`
each expose exactly three methods: `get_binding_by_person`,
`get_binding_by_master_resume`, `list_all_bindings`. None has a
`create_binding` — write authority belongs exclusively to
`CandidateIdentityBindingSqlService`, which never calls into these
repositories (never opens a second `Session` mid-transaction).

## `CandidateIdentityHeaderSqlService.resolve_header_binding`

Before opening any `Session`, independently recomputes
`owner_key_fingerprint` via the existing `compute_owner_key_fingerprint`
and compares it to the caller-supplied `JobArtifactOwnerKey`. A mismatch
(including one manufactured via `model_copy(update=...)` on any of
`person_entity_id`/`owner_kind`/`owner_reference_id`/`owner_key_schema_version`)
returns `OWNER_KEY_FINGERPRINT_MISMATCH` — no `Session` is ever opened.

Past that gate, exactly one `Session` and one read transaction: look up the
binding by `person_entity_id`, re-verify the `TruthEntity`/`Resume`/
`PersonalInfo` chain fresh (never trusting the stored binding row alone —
a `Resume` that is no longer master, or a `PersonalInfo` that has since
become invalid, fails closed even for a previously-valid binding),
canonicalize fields, compute `source_personal_info_fingerprint`, and
return a frozen `CvDocxHeaderBinding`. This module never uses the
read-only repository as a composition mechanism, precisely so the whole
read stays atomic in one `Session`.

`CvDocxHeaderBinding` has no `website`, `github`, or `title` field —
`website` is never read, copied, or allowed to influence the fingerprint.
`linkedin` is trimmed, rejected on internal whitespace, capped at 512
chars, and a bare host/path is given an `https://` prefix without
disturbing any slug it contains.

Header resolution statuses (12, closed): `RESOLVED`,
`OWNER_KEY_FINGERPRINT_MISMATCH`, `IDENTITY_BINDING_NOT_FOUND`,
`BINDING_OWNER_MISMATCH`, `PERSON_ENTITY_NOT_FOUND`, `ENTITY_IS_NOT_PERSON`,
`MASTER_RESUME_NOT_FOUND`, `RESUME_IS_NOT_MASTER`, `PERSONAL_INFO_MISSING`,
`FULL_NAME_MISSING`, `PERSONAL_INFO_INVALID`, `STORAGE_UNAVAILABLE`.

## Independent schema manifest and preflight

`candidate_identity_binding_schema_manifest.py` never imports
`Base.metadata`, the `CandidateIdentityBinding` ORM model, or
`app.models`'s table definition — every expected column/type/nullability/
PK/FK/CHECK/UNIQUE/trigger/index is a literal, hand-written Python value.
The actual schema is read exclusively via `sqlite_master`/`PRAGMA
table_info`/`PRAGMA foreign_key_list`/`PRAGMA index_list`/`PRAGMA
index_xinfo`.

Preflight is fail-closed: absent → `ABSENT_CREATE_REQUIRED` (the only state
a caller may act on); exact match → `CANDIDATE_IDENTITY_BINDING_SCHEMA_READY`;
any drift (missing/extra column, wrong type/nullability/PK, missing/wrong
FK, missing/altered CHECK, missing UNIQUE, missing/extra/drifted trigger,
extra manual index) raises `RuntimeError` before a single mutation.

## Startup ordering (`app/db_engine.py`)

1. existing P1/P2/P6-B1/final-DOCX-snapshot preflights (read-only, unchanged),
2. candidate-identity-binding preflight (read-only),
3. create-if-completely-absent (only after every preflight above passed),
4. install the four triggers,
5. post-create verification.

`candidate_identity_bindings` is excluded from every generic/broad
`create_all` path, exactly like the P2/P6-B1/final-snapshot tables — it is
only ever created via this one controlled branch.

## Reconciliation

`run_candidate_identity_binding_reconciliation` is read-only in standard
mode: it never repairs, updates, deletes, rebinds, or copies `PersonalInfo`.
It detects: missing `TruthEntity`, non-`PERSON` entity, non-`ACTIVE` status
(informational only — never a gate), missing `Resume`, non-master `Resume`,
missing/invalid `PersonalInfo`, blank `full_name`, duplicate person/resume
bindings, a `binding_fingerprint` that no longer matches a recompute, and a
stale `binding_schema_version`.

## Tests

- `tests/unit/test_candidate_identity_binding_schema_manifest.py`
- `tests/unit/test_candidate_identity_binding_repository.py`
- `tests/unit/test_candidate_identity_binding_sql_service.py`
- `tests/unit/test_cv_docx_header_binding_builder.py`
- `tests/unit/test_candidate_identity_binding_reconciliation.py`
- `tests/integration/test_candidate_identity_binding_schema_preflight.py`
