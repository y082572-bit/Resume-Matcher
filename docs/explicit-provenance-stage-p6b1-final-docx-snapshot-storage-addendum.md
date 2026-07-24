# Explicit Provenance Stage P6-B1: Final DOCX Snapshot SQL Storage Addendum

## Purpose

This addendum adds a production SQL storage implementation for the P6-A
`FinalDocxSnapshotRepository` Protocol (see
`docs/explicit-provenance-stage-p6a-final-snapshot-addendum.md`), the exact
same way P6-B1 itself added a SQL implementation for
`CvDocumentArtifactRepository` (see `docs/explicit-provenance-stage-p6b1.md`).
It ships:

1. one new SQL table (`cv_docx_final_snapshots`),
2. `FinalDocxSnapshotSqlRepository`,
3. reuse of the existing global `CvDocumentBlobStore`,
4. a shared owner/blob invariants module used by both SQL repositories,
5. an independent, hand-written schema manifest and fail-closed preflight,
6. a read-only, final-snapshot-scoped reconciliation pass,
7. an extension to the global reconciliation pass's referenced-blob set,
8. tests,
9. this document.

It does **not** implement a working copy, a template, a DOCX renderer, a PDF
converter, an API, a frontend, local-open, or any of P6-B2/P6-B3/P6-C. Those
all remain out of scope here.

## Why a separate table, not a new column on an existing one

`FinalDocxSnapshot` is a deliberately separate, additive domain contract from
`ValidatedCvDocxSnapshot` (see the P6-A addendum doc for the full rationale:
no validation semantics, no manual confirmation, the user owns responsibility
for Word edits). Its SQL storage mirrors that separation exactly:
`cv_docx_final_snapshots` is its own table, never a new column bolted onto
`cv_docx_validated_snapshots`, and `FinalDocxSnapshotSqlRepository` never
reuses or extends `CvDocumentArtifactSqlRepository`.

## The `cv_docx_final_snapshots` table

Exactly 10 columns:

| # | Column | Notes |
|---|--------|-------|
| 1 | `final_snapshot_fingerprint` | primary key |
| 2 | `owner_key_fingerprint` | FK → `cv_document_artifact_owners.owner_key_fingerprint` |
| 3 | `source_proposal_artifact_fingerprint` | FK → `cv_docx_proposal_artifacts.artifact_fingerprint` |
| 4 | `source_proposal_revision` | `>= 1` |
| 5 | `generated_proposal_sha256` | the pipeline's own generated-DOCX hash at proposal time |
| 6 | `final_docx_sha256` | FK → `cv_document_blobs.blob_sha256` |
| 7 | `edited_by_user` | `0`/`1`, always `generated_proposal_sha256 != final_docx_sha256` |
| 8 | `finalization_policy_version` | non-blank, `<= 64` chars |
| 9 | `snapshot_schema_version` | literal `cv-document-final-snapshot-schema-v1` |
| 10 | `created_at` | |

There is **no separate `blob_sha256` column** — `final_docx_sha256` itself is
the FK into `cv_document_blobs`, exactly like `exact_docx_sha256` doubles as
the FK column on `cv_docx_validated_snapshots`. `final_docx_sha256` carries
**no `UNIQUE` constraint**: many snapshot metadata rows across different
owner/proposal lineages may legitimately point at byte-identical final DOCX
content, and the repository must never treat "this hash is already claimed"
as a reason to block a second, independent lineage from saving.

Three non-unique indexes: `owner_key_fingerprint`,
`source_proposal_artifact_fingerprint`, `final_docx_sha256`. No slot table —
`FinalDocxSnapshot` has no "current" concept at all; every save is an
immutable, content-addressed record.

## Closed error channel (E1)

`FinalSnapshotSaveStatus` (in `cv_document_final_snapshot_repository.py`)
gained six new, additive members, each corresponding to a failure only a
real SQL-backed repository can produce (never
`InMemoryFinalDocxSnapshotRepository`):

- `SOURCE_PROPOSAL_NOT_FOUND`
- `SOURCE_PROPOSAL_OWNER_MISMATCH`
- `SOURCE_PROPOSAL_REVISION_MISMATCH`
- `SOURCE_PROPOSAL_HASH_MISMATCH`
- `STORAGE_METADATA_CONFLICT`
- `STORAGE_UNAVAILABLE`

`FinalDocxSnapshotBuildStatus` (in `schemas/cv_document_final_snapshot.py`)
gained the same six members, 1:1. Extending this enum required modifying its
schema file — see the note below on scope.

`FinalDocxSnapshotSqlRepository` never raises `CvDocumentStorageError`,
`IntegrityError`, `OperationalError`, `OSError`, or any other
`SQLAlchemyError` to its caller; every normal SQL/storage failure is mapped
onto one of the closed `FinalSnapshotSaveStatus` values instead.
`finalize_current_docx_for_pdf` (the builder) maps exclusively on
`FinalSnapshotSaveStatus` — it imports no SQLAlchemy symbol, no
`CvDocumentStorageError`, and contains no `try`/`except` at all (verified by
an AST-level test), staying a pure Protocol client exactly as it was before
this addendum.

### Note on scope: the schema file had to be touched

The original touched-files plan called for exactly 17 files and explicitly
excluded `schemas/cv_document_final_snapshot.py`. That plan also required
extending `FinalDocxSnapshotBuildStatus`, which is defined *only* in that
same file — a direct contradiction, since the enum cannot be extended
without editing the file that defines it. This was flagged before writing
any code; the resolution (confirmed with the requester) was to treat the
"don't touch this file" instruction as the mistake and add it as an 18th
touched file. The final touched-file count is therefore 8 new + 10 modified
= 18, not 8 new + 9 modified = 17.

## Shared owner and blob invariants (H1)

`cv_document_storage_shared.py` is a new module holding `ensure_owner`,
`row_to_owner_key`, `require_owner_key`, and `ensure_blob_row` — extracted
verbatim from `CvDocumentArtifactSqlRepository`. Both
`CvDocumentArtifactSqlRepository` and `FinalDocxSnapshotSqlRepository` call
these same functions; the logic exists in exactly one place, not two
independently maintained copies. `CvDocumentArtifactSqlRepository`'s own
`_ensure_owner`/`_row_to_owner_key`/`_require_owner_key`/`_ensure_blob_row`
methods remain in place as thin delegating wrappers — same names, same
signatures, same semantics — purely so existing monkeypatch-based tests
(which patch e.g. `repo._ensure_owner`) keep working unmodified. Neither
P6-B1's public methods nor its semantics changed.

Blob metadata reuse compares `byte_size`, `storage_locator`, and
`media_type` exactly, for both repositories identically: an existing
`cv_document_blobs` row that disagrees on any of those three fails closed
with `STORAGE_METADATA_CONFLICT`, and the row is never updated in place.

## Source proposal lineage verification

At save time, `FinalDocxSnapshotSqlRepository` reads the live
`cv_docx_proposal_artifacts` row for
`snapshot.source_proposal_artifact_fingerprint` and checks, independently:

- the proposal row exists (`SOURCE_PROPOSAL_NOT_FOUND` if not),
- its `owner_key_fingerprint` matches the snapshot's own (recomputed) owner
  fingerprint (`SOURCE_PROPOSAL_OWNER_MISMATCH` if not),
- its `proposal_revision` matches `snapshot.source_proposal_revision`
  (`SOURCE_PROPOSAL_REVISION_MISMATCH` if not),
- its `generated_docx_content_hash` matches
  `snapshot.generated_proposal_sha256` (`SOURCE_PROPOSAL_HASH_MISMATCH` if
  not).

The FK on `source_proposal_artifact_fingerprint` guarantees referential
existence at the database level, but never substitutes for these four
independent comparisons — a dangling FK target and a live-but-mismatched row
are different failure modes, and each gets its own closed status.

## Save flow

1. Independently recompute `final_docx_sha256` from the caller's raw bytes.
2. Independently recompute the owner-key fingerprint from the snapshot's own
   `owner_key` constituent fields (never trusting
   `owner_key.owner_key_fingerprint` at face value).
3. Independently recompute `edited_by_user` and `final_snapshot_fingerprint`,
   the latter using the *recomputed* owner fingerprint and DOCX hash. Since
   `owner_key_fingerprint` is itself one of `final_snapshot_fingerprint`'s own
   inputs, this single recompute-and-compare blocks a tampered/rebuilt owner
   binding before any filesystem write — satisfying "block mismatches before
   filesystem mutation" without a separate, earlier owner check.
4. Publish the blob via `CvDocumentBlobStore.write_blob` — before opening any
   DB transaction, so a subsequent DB failure can only ever leave an orphan
   filesystem blob, never a DB row pointing at bytes that were never durably
   written.
5. Open one short `Session`. In one transaction: `ensure_owner`, verify
   source proposal lineage, `ensure_blob_row`, check for an existing snapshot
   row by `final_snapshot_fingerprint`, then insert.
6. Commit (the `with session.begin():` context manager's normal exit).
7. Close the `Session`.

A DB failure strictly after blob publish may leave an orphan filesystem
blob; this repository never deletes it automatically — only the read-only
reconciliation module ever reports it (as a `cv_document_reconciliation.py`
`ORPHAN_FILESYSTEM_BLOB`, unaffected by this addendum).

Neither the proposal slot, the PDF slot, validated snapshots, nor any
proposal/PDF artifact's storage status is ever read for CAS purposes or
written by this addendum.

## Concurrency

Two identical first-writers can race on the same
`final_snapshot_fingerprint` primary key. On `IntegrityError`:

1. the transaction is rolled back (the `with session.begin():` context
   manager's own exception handling) and the `Session` that raised is closed
   (the enclosing `with self._session_factory() as session:` block exits
   normally once the `IntegrityError` is caught inside it),
2. a **literally new** `Session` is opened,
3. the snapshot row is re-read by `final_snapshot_fingerprint` through that
   new `Session`,
4. its stored fields (including the blob hash) are compared against what
   this call was about to write,
5. the result is `ALREADY_EXISTS_IDENTICAL` if everything matches, or
   `SNAPSHOT_METADATA_CONFLICT` otherwise.

The `Session` that raised `IntegrityError` is never reused for the re-read —
this is a stricter contract than base P6-B1's CAS-slot IntegrityError
handling (which does reuse the same `Session` after a rollback), deliberately
chosen for this addendum. A raw `IntegrityError` never propagates to a
caller of `save_final_docx_snapshot`.

## Repository retrieval

- `get_final_docx_snapshot(final_snapshot_fingerprint)` — metadata lookup,
  always by fingerprint.
- `retrieve_final_docx_bytes(final_docx_sha256)` — bytes lookup, always by
  hash, re-verified against the blob store's own re-hash on read.

There is no method that looks up metadata by a bytes hash — given only
`final_docx_sha256`, the repository never picks an arbitrary lineage record
on the caller's behalf, exactly mirroring
`InMemoryFinalDocxSnapshotRepository`'s existing contract.

## Independent schema manifest and preflight

`cv_document_final_snapshot_schema_manifest.py` defines the expected
`cv_docx_final_snapshots` schema — 10 columns (name/type/nullability/PK
position), 3 foreign keys, 9 CHECK constraint bodies, and the expected
non-unique/unique index sets — as literal, hand-written Python values. It
never imports `Base.metadata`, `cv_document_models`, or any ORM table
definition, unlike every other schema preflight in this codebase (P1, P2,
and base P6-B1 all build their "expected" schema by running
`Base.metadata.create_all` against an in-memory reference engine and
introspecting *that*). This addendum's manifest is independent on purpose:
a bug in the ORM model (`CvDocxFinalSnapshotRow`) can never silently pass its
own preflight by being compared against its own reflection.

The actual schema is read exclusively via `sqlite_master`,
`PRAGMA table_info`, `PRAGMA foreign_key_list`, `PRAGMA index_list`, and
`PRAGMA index_xinfo` — the same raw introspection primitives the rest of
`db_engine.py` already uses, applied independently here (this module's CHECK-
constraint extractor is a small, self-contained, quote-aware paren scanner,
duplicated in spirit from — never imported from — `db_engine.py`'s
identical-purpose helper).

`preflight_explicit_provenance_final_docx_snapshot_schema` (in
`db_engine.py`, delegating straight to the manifest module) returns:

- `ABSENT_CREATE_REQUIRED` if the table does not exist at all,
- `FINAL_DOCX_SNAPSHOT_SCHEMA_READY` if it exists and matches exactly,
- otherwise raises `RuntimeError` before a single mutation.

`init_models_sync` runs this preflight — read-only — alongside the P1/P2/
P6-B1 preflights, all before any DDL. `cv_docx_final_snapshots` FKs into
three base P6-B1 tables, so its own create-or-validate step always runs
strictly after the P6-B1 block; the table is excluded from every other
generic/broad `create_all`/`checkfirst` path in `init_models_sync` so it is
only ever created via this one controlled branch. A partial or incompatible
existing table is never auto-repaired.

## Global orphan authority unchanged (R1)

`cv_document_reconciliation.run_reconciliation` remains the single authority
for `ORPHAN_BLOB_ROW` and `ORPHAN_FILESYSTEM_BLOB`. It now additionally reads
`cv_docx_final_snapshots.final_docx_sha256` into its referenced-blob set, so
a blob referenced only by a final snapshot is never misreported as an
orphan. No other behavior of that module changed.

## Final snapshot reconciliation (new, separate module)

`cv_document_final_snapshot_reconciliation.run_final_docx_snapshot_reconciliation`
is a new, read-only pass scoped exclusively to `cv_docx_final_snapshots`'s
own invariants:

- missing owner / missing source proposal,
- source-proposal owner/revision/generated-hash mismatch,
- missing blob row / missing blob file / blob hash mismatch / blob size
  mismatch / blob media-type mismatch,
- an invalid (tampered) `final_snapshot_fingerprint`,
- an `edited_by_user` that disagrees with
  `generated_proposal_sha256 != final_docx_sha256`,
- a symlink anywhere in the managed blob-store tree,
- a stale `*.tmp` file left under the managed tmp directory.

It never scans for, and never emits, `ORPHAN_BLOB_ROW` or
`ORPHAN_FILESYSTEM_BLOB` — those remain exclusively
`run_reconciliation`'s to report. Standard mode never mutates SQL rows or
the filesystem, and repeated calls on unchanged storage are idempotent.

## Testing

- `tests/unit/test_cv_document_final_snapshot_sql_repository.py` — Protocol
  conformance, save/idempotent-resave, lineage sharing across
  owners/revisions, retrieval, lineage mismatches, hash/fingerprint
  mismatches, media-type conflict, corrupted-blob non-overwrite, orphan-blob-
  on-DB-failure, `OperationalError` mapping, concurrency (converging
  writers, a genuine metadata conflict, and a synthetic raw `IntegrityError`
  that must never leak), and a check that no public method ever raises
  `CvDocumentStorageError`.
- `tests/integration/test_final_docx_snapshot_schema_preflight.py` — mirrors
  the base P6-B1 preflight test conventions against the independent
  manifest: absent → created, ready idempotently, missing column/wrong
  type/wrong nullability/wrong PK/missing FK/wrong FK target/missing CHECK/
  wrong `edited_by_user` CHECK/accidental `UNIQUE(final_docx_sha256)`/
  missing index all block, a failed preflight mutates nothing, and the
  manifest module itself never imports the ORM.
- `tests/integration/test_cv_document_final_snapshot_reconciliation.py` —
  every listed inconsistency, that the module never emits the two global
  orphan codes, and that standard mode is read-only and idempotent.
- `tests/unit/test_cv_document_final_snapshot_builder.py` (modified) — the
  six new save statuses map to their corresponding build statuses, the
  builder never imports SQLAlchemy or `CvDocumentStorageError`, and contains
  no `try`/`except` at all.
- `tests/unit/test_cv_document_sql_repository.py` (modified) — both
  repositories delegate to the literal same shared helper functions, and
  both block owner rebinding, a media-type conflict, a corrupted-locator
  conflict, and a corrupted-`byte_size` conflict identically.
- `tests/integration/test_cv_document_reconciliation.py` (modified) — a blob
  referenced only by a final snapshot (or only by a proposal, or only by a
  PDF) is never orphan, and a real orphan is still reported exactly once.

## What is explicitly NOT implemented here

Working copy storage, a template, a DOCX renderer, a PDF converter, an API,
a frontend, local-open, or any of P6-B2/P6-B3/P6-C.
