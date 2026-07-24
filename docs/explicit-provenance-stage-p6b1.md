# Explicit Provenance Stage P6-B1: Document Storage Foundation

## Purpose

Stage P6-B1 is the production **storage foundation** for the P6-A Document
Artifact Lifecycle Core. It gives the exact P6-A
`CvDocumentArtifactRepository` Protocol a real, synchronous, SQL +
content-addressed-filesystem implementation, replacing
`InMemoryCvDocumentArtifactRepository` for production use while leaving the
Protocol itself, and every P6-A domain model, byte-for-byte unmodified.

P6-B1 adds no new domain semantics: no DOCX rendering, no DOCX structural
validation, no PDF conversion, no local-open adapter, no API router, and no
frontend. It only makes the P6-A chain

```
ApprovedCvContentResult
  -> CvDocxProposalArtifact   (current proposal slot, 0/1 per owner)
  -> ValidatedCvDocxSnapshot  (immutable, content-addressed)
  -> ConfirmedCvPdfArtifact   (current PDF slot, 0/1 per owner)
```

durable: SQL metadata rows for the domain artifacts, and a single global,
content-addressed blob store for their raw bytes.

## P6-B1 / P6-B2 / P6-B3 / P6-C split

| Stage | Scope |
|---|---|
| **P6-B1** (this stage) | SQL metadata, a content-addressed immutable blob store, the two current-slot tables, CAS + optimistic concurrency, owner/artifact/snapshot/PDF storage, global byte deduplication, filesystem containment/symlink protection, fail-closed blob write, reconciliation, schema preflight. |
| **P6-B2** (future) | A real DOCX template provider, a real DOCX renderer, and a real DOCX structural validator. |
| **P6-B3** (future) | A real PDF conversion adapter and the document-operation API routers. |
| **P6-C** (future) | The frontend. |

P6-B1 implements **no API router, no request/response schema, and no UI of
any kind**. It never imports FastAPI, Starlette, or anything under
`app/routers/`.

## Synchronous repository, and the future async boundary

`CvDocumentArtifactSqlRepository` (`cv_document_sql_repository.py`)
implements the P6-A `CvDocumentArtifactRepository` Protocol exactly:
every public method is a plain synchronous `def`. The class never uses
`async def`, `AsyncSession`, `asyncio.run`, `run_until_complete`, or a
nested event loop -- proven at the source level by
`tests/unit/test_cv_document_sql_repository.py` (an `ast`-based scan, not a
naive text grep, so a mention of these terms inside a docstring is never a
false positive).

It is constructed with a synchronous SQLAlchemy `sessionmaker[Session]`
(the same kind of sync session factory `app/database.py` already builds for
the encrypted `api_keys` table: `sessionmaker(sync_engine,
expire_on_commit=False)`) and a `CvDocumentBlobStore`. It opens its own
`Session`/transaction per mutating call; read methods may use their own
short-lived `Session`. It never constructs its own engine, never runs a
migration in its constructor, and never creates a second SQLite database --
all seven P6-B1 tables live in the same SQLite file as every other table.

**A future async router is expected to call every method on this
repository through a threadpool** (e.g. `anyio.to_thread.run_sync` /
`loop.run_in_executor`), the same way any other synchronous, blocking call
would be bridged into an async FastAPI handler. P6-B1 defines and proves
the synchronous contract only; it implements no router and no threadpool
wiring itself.

## The seven tables

1. `cv_document_artifact_owners` -- one row per `JobArtifactOwnerKey`,
   keyed by `owner_key_fingerprint`.
2. `cv_document_blobs` -- one row per **globally** deduplicated blob,
   keyed by `blob_sha256`.
3. `cv_docx_proposal_artifacts` -- one immutable row per generated
   proposal, keyed by `artifact_fingerprint`.
4. `cv_docx_validated_snapshots` -- one immutable row per validated
   snapshot, keyed by `snapshot_fingerprint`.
5. `cv_confirmed_pdf_artifacts` -- one immutable row per confirmed PDF,
   keyed by `artifact_fingerprint`.
6. `cv_document_proposal_slots` -- the current-proposal pointer, one row
   per owner (`owner_key_fingerprint` is both PK and FK).
7. `cv_document_pdf_slots` -- the current-PDF pointer, one row per owner.

All seven are declared in `cv_document_models.py` on the existing
`app.models.Base` -- no second `Base`, no second engine.

## Blob identity vs. domain artifact identity

`blob_sha256` is the identity of **exact bytes**, shared globally: the same
blob may be pointed at by many proposal/snapshot/PDF rows across many
owners (deduplication -- see below). It is never conflated with a domain
artifact's own identity:

- `CvDocxProposalArtifact.artifact_fingerprint`
- `ValidatedCvDocxSnapshot.snapshot_fingerprint`
- `ConfirmedCvPdfArtifact.artifact_fingerprint`

are each computed by the exact same pure fingerprint functions P6-A's own
builders use (`compute_proposal_artifact_fingerprint`,
`compute_snapshot_fingerprint`, `compute_pdf_artifact_fingerprint`), fed by
owner/content/template/policy fingerprints -- never by `blob_sha256` alone,
and never by a path. A same-row `CHECK` constraint
(`generated_docx_content_hash = blob_sha256`, `exact_docx_sha256 =
blob_sha256`, `pdf_sha256 = blob_sha256`) still ties each domain row to
*its own* declared blob, so the two identities can never silently drift
apart within one row.

The repository never trusts a stored fingerprint at face value on ingress:
`replace_current_proposal`/`replace_current_pdf`/`save_validated_snapshot`
all independently recompute the owner-key fingerprint and the
artifact/snapshot fingerprint from the incoming domain object's own
constituent fields, and independently recompute a fresh SHA-256 of the
incoming bytes, before ever writing anything. A mismatch raises a closed
`CvDocumentStorageError` (see below) -- it is never silently accepted or
"fixed".

## Two independent current slots

`cv_document_proposal_slots` and `cv_document_pdf_slots` are physically
separate tables. "Current" status is exclusively the slot's
`current_artifact_fingerprint`/`current_revision` pointer -- there is no
`is_current` boolean column on any artifact table, and no caller-supplied
flag is ever trusted as current-status authority. `replace_current_proposal`
never reads or writes the PDF slot; `replace_current_pdf` never reads or
writes the proposal slot -- proven by
`test_proposal_replace_does_not_touch_pdf_slot` /
`test_pdf_replace_does_not_touch_proposal_slot`.

## CAS and optimistic concurrency

Every slot replace binds two independent checks:

1. **Caller-facing CAS**: the slot's current `current_revision` must equal
   the caller's `expected_previous_revision` (`None` for first generation).
   A mismatch returns `CasReplaceStatus.STALE_REVISION` with the real
   current artifact, before any row is written.
2. **Low-level optimistic lock**: the actual slot `UPDATE` is conditioned
   on the slot's own `slot_version` (`UPDATE ... WHERE slot_version =
   <value just read>`); a concurrent mutation that slipped in between the
   read and the write makes this `UPDATE` affect zero rows, which the
   repository treats exactly like a stale-CAS loss.

For the very first artifact of an owner (`expected_previous_revision=None`,
no slot row exists yet), the race between two literal first-writers is
resolved by the **database's own** `UNIQUE`/`PRIMARY KEY` constraints on
`cv_document_proposal_slots.owner_key_fingerprint` and
`cv_docx_proposal_artifacts (owner_key_fingerprint, proposal_revision)`
(and their PDF-table mirrors): the loser's `IntegrityError` is caught, its
whole transaction is already rolled back by the `Session.begin()` context
manager, and the loser is reported `STALE_REVISION` with the winner's
now-current artifact. Exactly one writer ever ends up current -- proven by
`test_two_first_writers_never_produce_two_current_proposals` (concurrent
threads, a `threading.Barrier`).

## DB / filesystem boundary and orphan-blob behavior

Every mutating repository method writes (or reuses) the blob **before**
opening any DB transaction. This means:

- A failed DB transaction can only ever leave behind an **orphan blob on
  disk** -- it can never leave a DB row pointing at bytes that were never
  durably written.
- The reverse can never happen: there is no code path where a DB artifact
  row is committed before its blob is confirmed durable.

Reconciliation (below) can report an orphan blob; it never invents a DB row
to "fix" it, and it never deletes the orphan blob itself in standard mode.

## Atomic no-replace publish (no blind `os.replace`)

`CvDocumentBlobStore._publish` never does a blind `os.replace(temp, final)`.
Publish is a strict *create-if-absent* operation via `os.link(temp,
final)`:

- If the link succeeds, the final blob was created atomically; the temp
  file is removed and the directory is best-effort `fsync`'d.
- If it raises `FileExistsError`, the existing final blob is re-read and
  re-hashed (never trusted from its filename or from `mtime`): identical
  bytes/size means the temp file is discarded and the write is reported as
  a reuse; a mismatch means `EXISTING_BLOB_CORRUPTED` -- the temp file is
  discarded, but **the existing final blob is left completely untouched**
  (never overwritten, never moved). Quarantining a corrupted blob is
  deliberately left to a future, explicit, controlled reconciliation mode.

If `os.link` itself is unavailable, the write fails closed
(`TEMP_WRITE_FAILED`) rather than silently falling back to an overwriting
`os.replace`.

## Filesystem containment and symlink policy

A blob path is always derived exclusively from a regex-validated, 64-char
lowercase-hex SHA-256 -- `root/blobs/<sha[0:2]>/<sha[2:4]>/<sha>.blob` --
never from caller input. Containment is checked with `Path.resolve()` +
`is_relative_to()`, never a textual `str.startswith`, which would
incorrectly accept a sibling directory sharing a name prefix (proven by
`test_prefix_collision_path_fails_containment`). Every ancestor directory
component is checked for a symlink before being trusted or created, and the
final blob path itself is also checked -- a symlink anywhere in that chain
fails closed (`SYMLINK_ESCAPE`), whether encountered during a write publish
or a read.

## Global deduplication

Blob identity is global and content-addressed: two different owners, or
two different proposal revisions of the *same* owner, that happen to
produce byte-identical output converge on exactly one row in
`cv_document_blobs` and one file on disk. `cv_docx_proposal_artifacts`,
`cv_docx_validated_snapshots`, and `cv_confirmed_pdf_artifacts` each keep
their **own** domain-fingerprint identity regardless of this sharing --
many artifact/snapshot rows may legitimately point at the same
`blob_sha256` (proven by
`test_two_owners_can_share_the_same_blob` and
`test_two_snapshot_fingerprints_can_share_the_same_blob`).

## Proposal / snapshot / PDF lineage

- **Proposal replace** (`replace_current_proposal`): recompute owner/
  artifact fingerprints and the bytes hash; write/reuse the blob; open a
  transaction; ensure the owner row; CAS-check the proposal slot; mark the
  previous current artifact `SUPERSEDED` (immutable history, never mutated
  otherwise); insert the new immutable artifact row; update the slot.
- **Snapshot save** (`save_validated_snapshot`): recompute the snapshot
  fingerprint; require the structural-validation result already be `VALID`
  (P6-A's own builder never calls this otherwise); write/reuse the blob;
  require the referenced proposal artifact to exist and match the owner;
  insert the immutable snapshot row keyed by `snapshot_fingerprint`
  (**not** by `blob_sha256`) -- so two independent manual confirmations of
  byte-identical content keep two separate snapshot rows/lineages sharing
  one blob (proven by
  `test_two_manual_confirmations_keep_separate_snapshot_lineage`). An
  identical resave (same fingerprint, same fields) is idempotent
  (`ALREADY_EXISTS_IDENTICAL`); a same-fingerprint row with *different*
  fields is `STORAGE_METADATA_CONFLICT`
  (`LOCATOR_CONTENT_MISMATCH` at the Protocol level).
- **PDF replace** (`replace_current_pdf`): recompute owner/artifact
  fingerprints and the bytes hash; require the referenced validated
  snapshot to exist with a matching `source_docx_sha256`; otherwise the
  same write-blob-first, CAS, mark-superseded, insert, slot-update sequence
  as proposal replace, on its own independent PDF slot.

`ValidatedCvDocxSnapshot.structural_validation_result` is never persisted
as its own columns: only a structurally `VALID` snapshot (validated hash
== exact hash, no violations) is ever allowed to reach
`save_validated_snapshot` in the first place, so the repository safely and
exactly rebuilds that closed sub-result deterministically on read.

## Reconciliation

`cv_document_reconciliation.py::run_reconciliation` is read-only by
default: it never changes a current slot, never deletes a current
confirmed PDF or any domain history row, and never quarantines or
overwrites a blob. It reports (at least): dangling proposal/PDF slots;
artifact/snapshot rows referencing a missing blob row; a blob row with a
missing/wrong-hash/wrong-size file; an orphan filesystem blob with no
metadata row; an orphan blob row with no artifact/snapshot/PDF reference;
mismatches between a declared artifact hash and its `blob_sha256`; a
`QUARANTINED` blob row; a stale leftover file under the managed `tmp/`
directory; and a symlink anywhere in the managed tree. `run_reconciliation`
is idempotent (two consecutive calls on unchanged storage produce an
identical report) and its standard mode leaves both the database and the
filesystem byte-for-byte unchanged. The **only** mutation this module ever
performs is `cleanup_stale_temp_files`, and only when a caller explicitly
invokes that separate function -- it removes leftover `*.tmp` files from
`tmp/` only, and is never called as a side effect of
`run_reconciliation`.

## Schema preflight

`db_engine.py` adds `preflight_explicit_provenance_p6b1_schema` following
the exact P1/P2 pattern: a reference manifest is built once in an isolated
`:memory:` SQLite database via `Base.metadata.create_all`, and the real
target schema is fully compared against it -- columns, types, nullability,
primary keys, foreign keys, `CHECK`/`UNIQUE` constraints, and indexes.
Absent schema returns `ABSENT_CREATE_REQUIRED` (safe to create); a
partially-present table set raises `ERROR_PARTIAL_P6B1_SCHEMA`; any other
divergence raises `ERROR_INCOMPATIBLE_P6B1_SCHEMA` -- there is no
auto-repair path. `init_models_sync` now runs all three preflights (P1, P2,
P6-B1) read-only before any DDL for any of them, and the seven P6-B1 tables
are created strictly in their FK-dependency order (via
`Base.metadata.sorted_tables`, never an alphabetical or hand-written
order) only when fully absent. Existing P1/P2 preflight behavior is
otherwise unchanged (see `tests/integration/test_p6b1_schema_preflight.py`
and the pre-existing `test_truth_legacy_migration_schema.py`/
`test_truth_identity_migration.py`).

## What is explicitly NOT implemented in P6-B1

- A real DOCX template provider or a real DOCX renderer (P6-B2).
- A real DOCX structural validator (P6-B2).
- A real PDF conversion adapter (P6-B3).
- A real local-document-open adapter (deferred beyond P6-B1).
- Any API router, request/response schema, or endpoint (P6-B3/P6-C).
- Any frontend code (P6-C).
- Any change to the P6-A Protocol or P6-A domain model semantics.

These are explicitly deferred: DOCX rendering/validation to **P6-B2**; PDF
conversion and the document-operation API to **P6-B3**; the frontend to
**P6-C**.
