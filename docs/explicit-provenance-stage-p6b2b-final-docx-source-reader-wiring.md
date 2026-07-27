# Explicit Provenance Stage P6-B2b-B: Filesystem Final DOCX Source Reader and Snapshot Wiring

## Purpose

This stage wires the P6-B2b-A Working Copy Foundation
(`docs/explicit-provenance-stage-p6b2b-working-copy-foundation.md`) to the
existing P6-A finalization builder
(`docs/explicit-provenance-stage-p6a-final-snapshot-addendum.md`) and its
P6-B1 SQL storage (`docs/explicit-provenance-stage-p6b1.md`):

```
Proposal DOCX
  -> working copy current.docx
  -> user edits in Word
  -> secure observation of current.docx + binding.json
  -> owner-bound revalidation of the historical Proposal artifact
  -> verified FinalDocxSourceCapture
  -> existing finalize_current_docx_for_pdf
  -> immutable FinalDocxSnapshot
  -> existing FinalDocxSnapshotSqlRepository
```

It implements no PDF conversion, no API, no router, and no frontend, and it
changes no SQL schema — see the manifest in the implementation task for the
exact, closed set of new/modified files.

## Two clearly separated reader phases

`FilesystemFinalDocxSourceReader` (`cv_document_final_source_filesystem_reader.py`)
splits its work into two phases that never overlap:

1. **Filesystem observation, under exactly one `PlatformOwnerLock`
   acquisition.** Re-derive `owner_key_fingerprint` via
   `compute_owner_key_fingerprint` and reject a mismatch *before* the lock
   is ever touched. Acquire the lock exactly once; stably read
   `current.docx` (exact bytes) and `binding.json` (sidecar) using the
   *existing* `posix_stable_read`/`windows_stable_read` primitives — never
   a second POSIX/WinAPI implementation, and never `Path.read_bytes()` for
   the sidecar. Release the lock in every path (`finally`), including every
   early-failure branch. The result is a private, immutable
   `_FilesystemFinalizationObservation` — never exported, never visible to
   the finalization builder or any SQL/lineage reader, and never itself a
   `FinalDocxSourceCapture`.

2. **Owner-bound Proposal lineage re-verification, after the lock is
   released.** The Proposal artifact row and its immutable blob are the
   *only* lineage authority (rule 9 of the implementation task) —
   `binding.json`'s `bound_proposal_artifact_fingerprint`/
   `bound_proposal_revision`/`bound_generated_proposal_sha256` are passed
   to `ProposalArtifactLineageReader.read_verified_proposal_artifact_lineage`
   purely as *hints*, never as authority. A `VERIFIED` result is itself
   never trusted at face value either: this reader defensively re-compares
   the returned `ProposalArtifactLineage` against the exact four values it
   queried the lineage reader with before ever assembling a
   `FinalDocxSourceCapture` — see "`FinalDocxSourceCapture` construction"
   below.

## The owner-lock boundary

Filesystem observation happens under exactly one `PlatformOwnerLock`
acquisition per `read_finalization_source` call: one `acquire`, one
`release` in a `finally`, and `validate_active_token` gates every real read
that follows. The lineage lookup always happens *after* that lock is
released — a Proposal artifact row/blob read never blocks a concurrent
Working Copy writer, and vice versa.

Three distinct lock-related failures are never conflated:

- `OWNER_LOCK_TIMEOUT` → `WORKING_COPY_LOCKED`
- `LOCK_PATH_IDENTITY_MISMATCH` → `WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH`
- an invalid/failed `validate_active_token` → `LOCK_CAPABILITY_INVALID`

`LOCK_PATH_IDENTITY_MISMATCH` (a symlink/hardlink/identity anomaly on the
lock file itself — a potential attack surface) is never folded into the
more generic `LOCK_CAPABILITY_INVALID` (an internal owner-lock contract
violation, e.g. token forgery). Both the filesystem reader and the
`finalize_working_copy_to_final_docx_snapshot` composition root (via
`WorkingCopyStore.inspect_working_copy`'s own `WorkingCopyInspectionStatus`)
keep these separate end-to-end.

## `binding.json` is a hint and a cache, never authority

Every value the sidecar carries — `bound_proposal_artifact_fingerprint`,
`bound_proposal_revision`, `bound_generated_proposal_sha256` — is used only
to *look up* a Proposal artifact lineage candidate. None of it is ever
copied into a `FinalDocxSourceCapture` directly. What ends up in the
capture always comes from the `ProposalArtifactLineageReader`'s own
`VERIFIED` result, which is itself derived exclusively from the
authoritative `cv_docx_proposal_artifacts` row and its blob.

If the sidecar is stale, corrupt, or lies, the lineage reader's own
metadata/hash checks catch it — the reader never trusts a hint far enough
to skip re-verification.

## Owner-bound Proposal lineage: metadata before bytes

`ProposalArtifactLineageSqlReader` (`cv_document_proposal_artifact_lineage_sql_reader.py`)
enforces a strict ordering: a closed precondition check on the caller's own
arguments (`INVALID_REQUEST` on failure — no SQL or blob touched at all);
`session.get` the row by `artifact_fingerprint` (`NOT_FOUND` if absent);
owner check; revision check; generated-metadata-hash check. **Only after
every one of those passes** is the Proposal's own blob ever read from the
blob store, and only then is that blob's SHA-256 re-hashed and compared
against the row's own `generated_docx_content_hash` (`BYTES_HASH_MISMATCH`
on divergence). A mismatch at any earlier step means the blob is never
touched — proven directly by the SQL reader's own integration tests, which
monkeypatch `read_blob` to raise `AssertionError` and confirm it is never
called for `OWNER_MISMATCH`/`REVISION_MISMATCH`/`GENERATED_HASH_MISMATCH`.

`INVALID_REQUEST` is a deliberate, implementation-mandated addition: a
malformed argument (non-hex `artifact_fingerprint`, `proposal_revision`
below 1, wrong argument types) must never surface as a raw `ValueError`,
Pydantic `ValidationError`, or a `TypeError` from bad values — every
`ProposalArtifactLineageReader` implementation runs the same shared
`validate_lineage_request` precondition check first.

### Closed error channel

The SQL reader catches `SQLAlchemyError` (the general base class, not just
`OperationalError`), `CvDocumentStorageError`, and a controlled `OSError`
from the blob layer — every one of those maps to `STORAGE_UNAVAILABLE`.
It never catches `BaseException`, `KeyboardInterrupt`, or `SystemExit`, and
never uses a bare `except:`.

## Secure `current.docx`/`binding.json` observation

Both files are read via the same platform stable-read dispatch
(`posix_stable_read`/`windows_stable_read`) under the single owner-lock
acquisition described above — `O_NOFOLLOW`, symlink/reparse rejection,
stable pre/post identity checks, and an exact, defensively-copied byte
result. `binding.json` gets its own `max_binding_size_bytes` bound,
independent of `max_docx_size_bytes`.

`posix_stable_read`/`windows_stable_read` themselves collapse an oversize
file into `WORKING_COPY_UNREADABLE` (see that module's own docstring) — we
never modify that module to carve out a distinct oversize status. Instead,
`FilesystemFinalDocxSourceReader` performs its own best-effort size
classification (an `lstat` immediately before, and again immediately
after, a failed stable read) so `WORKING_COPY_TOO_LARGE`/
`WORKING_COPY_BINDING_TOO_LARGE` can still be reported distinctly. This
classification is inherently racy — a best-effort diagnostic refinement,
never a security boundary; the stable read itself remains the real safety
guarantee.

`WORKING_COPY_BINDING_INVALID` means exactly one thing: `binding.json` was
stably, successfully read and then failed to parse into a
`WorkingCopyBinding` — a *content* failure after a *successful* I/O. It is
never used to report an I/O failure on the sidecar. A stable-read `LOCKED`
outcome on `binding.json` maps to the same `WORKING_COPY_LOCKED` a locked
`current.docx` would report; a stable-read `UNREADABLE` outcome maps to the
same `WORKING_COPY_UNREADABLE`. Both are reported *before* the lineage
lookup is ever attempted — a binding.json I/O failure never reaches the
`ProposalArtifactLineageReader` at all. A `WORKING_COPY_CHANGED_DURING_READ`
observed on either file reuses the one existing, generic status of that
name.

## Owner identity revalidation

Before any lock is acquired, `read_finalization_source` independently
recomputes `owner_key_fingerprint` via the existing
`compute_owner_key_fingerprint` and compares it against
`owner_key.owner_key_fingerprint`. A mismatch returns
`OWNER_KEY_FINGERPRINT_MISMATCH` immediately — `PlatformOwnerLock` is never
even constructed in that case.

## `FinalDocxSourceCapture` construction

A capture is only ever built after a `VERIFIED` lineage result **and** after
that result passes a defensive coherence check against the exact four
values this reader queried the lineage reader with:

1. `lineage.artifact_fingerprint` == the queried `artifact_fingerprint`
   (the sidecar's `bound_proposal_artifact_fingerprint` hint) — a mismatch
   is a `ProposalArtifactLineageReader` *contract violation* (a conforming
   implementation can never return this), not a legitimate Proposal
   outcome, so it maps to `STORAGE_UNAVAILABLE` rather than a new public
   status, with a `VERIFIED_LINEAGE_CONTRACT_VIOLATION` diagnostic.
2. `lineage.owner_key_fingerprint` == the recomputed owner identity —
   mismatch → `SOURCE_PROPOSAL_OWNER_MISMATCH`.
3. `lineage.proposal_revision` == the queried `proposal_revision` (the
   sidecar's `bound_proposal_revision` hint) — mismatch →
   `SOURCE_PROPOSAL_REVISION_MISMATCH`.
4. `lineage.generated_docx_content_hash` == the queried
   `generated_docx_sha256` (the sidecar's `bound_generated_proposal_sha256`
   hint) — mismatch → `SOURCE_PROPOSAL_HASH_MISMATCH`.

A `VERIFIED` result — from any `ProposalArtifactLineageReader`
implementation, not only the one SQL-backed implementation this stage
ships — is never trusted at face value: an incoherent `VERIFIED` result
(exercised directly in the unit tests via a fake lineage reader) can never
produce a `FinalDocxSourceCapture`. Only once all four checks pass is the
capture assembled:

- `owner_key_fingerprint` — the recomputed owner identity (never the
  caller's own, unverified field).
- `source_proposal_artifact_fingerprint`, `source_proposal_revision`,
  `generated_proposal_sha256` — all three from the verified, now
  coherence-checked SQL lineage, never from the sidecar hint.
- `docx_bytes` — from the phase-1 filesystem observation (the actual
  working copy the user may have edited), **never** from
  `ProposalArtifactLineage.docx_bytes` (the Proposal's own historical
  bytes, which exist solely to verify the sidecar's declared hash — they
  are a different document revision entirely and must never leak into the
  capture).

## Stale-but-valid Proposal N

A working copy may still be bound to a historical Proposal N while the
current slot has already moved to N+1. Finalization succeeds as long as
Proposal N's row still exists, its owner/revision/generated-hash all check
out, and its blob's exact bytes still hash to that declared value —
finalization never requires the bound Proposal to still be the *current*
one, and it never silently rewrites the sidecar's lineage to N+1.
`test_historical_proposal_n_verified_while_current_is_n_plus_1` and the
end-to-end `test_stale_but_valid_proposal_n_finalizes_while_current_is_n_plus_1`
cover this directly.

## `source_proposal_is_current`: best-effort, informational, never blocking

After a `FinalDocxSnapshot` has already been durably saved,
`finalize_working_copy_to_final_docx_snapshot` performs one best-effort
`CvDocumentArtifactRepository.get_current_proposal` lookup purely to report
whether the snapshot's source Proposal is still current:

- current proposal's fingerprint matches → `True`
- current proposal's fingerprint differs → `False`
- no current proposal at all → `False`
- the lookup itself raises → `None`

This is the *only* place in the whole flow that catches a broad
`Exception` (never `BaseException`/`KeyboardInterrupt`/`SystemExit`) — a
lookup failure can never turn an already-`FINALIZED` result into a
failure, and it never removes the snapshot that was already saved.

## `edited_by_user` stays purely informational

Nothing in this stage changes the existing P6-A policy: `edited_by_user`
is exactly `generated_proposal_sha256 != final_docx_sha256`, computed and
enforced entirely inside `finalize_current_docx_for_pdf` (unchanged by this
stage). It never gates finalization, and this stage performs **no**
semantic validation, Truth Library check, claim/fact verification, or any
structural assessment of what the user changed.

## Totalized status mappings — no fallback, anywhere

Mapping dictionaries in this stage are complete, fallback-free 1:1
mappings, each proven complete by a dedicated test:

- `_BINDING_STATUS_TO_READ_STATUS` (filesystem reader): every
  `binding.json` stable-read I/O outcome short of `STABLE_READ_OK` → a
  `WorkingCopyReadStatus` — `WORKING_COPY_MISSING` is the only
  binding-specific member (`WORKING_COPY_BINDING_MISSING`); `LOCKED`/
  `UNREADABLE`/`CHANGED_DURING_READ` reuse the same generic statuses
  `current.docx` would report for the identical I/O outcome. Never
  `WORKING_COPY_BINDING_INVALID`, which this dict does not contain at all.
- `_LINEAGE_STATUS_TO_READ_STATUS` (filesystem reader): every non-`VERIFIED`
  `ProposalArtifactLineageStatus` → a `WorkingCopyReadStatus`.
- `_READ_FAILURE_TO_BUILD_STATUS` (existing P6-A builder, now extended):
  every non-`SUCCESS` `WorkingCopyReadStatus` → a `FinalDocxSnapshotBuildStatus`.
  The builder now indexes this dict directly (`mapping[key]`) instead of
  `.get(key, default)` — an unmapped future status raises loudly instead of
  silently collapsing onto a default.
- `_INSPECTION_FAILURE_TO_BUILD_STATUS` (composition root): every
  non-`OBSERVED` `WorkingCopyInspectionStatus` → a `FinalDocxSnapshotBuildStatus`.

## Closed error channels, everywhere

- `ProposalArtifactLineageSqlReader`: `SQLAlchemyError` / `CvDocumentStorageError`
  / `OSError` → `STORAGE_UNAVAILABLE`. Never `BaseException`.
- `FilesystemFinalDocxSourceReader`: every internal collaborator it calls
  (`PlatformOwnerLock`, `posix_stable_read`/`windows_stable_read`,
  `parse_working_copy_binding`) is already a closed error channel; this
  reader adds no new `try`/`except` of its own beyond the owner-lock
  `finally` release.
- `finalize_working_copy_to_final_docx_snapshot`: the only broad
  `except Exception` in this stage's own new code, scoped exclusively
  around the best-effort `source_proposal_is_current` lookup.

## No mutable finalization slot

Finalization never mutates `current.docx`, `binding.json`, the current
Proposal slot, the working-copy directory, the Proposal artifact, or a
`FinalDocxSnapshot` once saved. `finalize_working_copy_to_final_docx_snapshot`
only ever *reads* through `WorkingCopyStore.inspect_working_copy` and the
filesystem source reader, and only ever *writes* through the existing,
append-only `FinalDocxSnapshotSqlRepository`.

## Out of scope

This stage implements no PDF conversion, no local file opener, no API, no
router, no frontend, and no SQL schema/migration change. It never modifies
`FinalDocxSnapshotSqlRepository`, `CvDocumentArtifactRepository`, or any
existing test.
