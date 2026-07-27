# Explicit Provenance Stage P6-B2b-A: Working Copy Foundation

## Purpose

Stage P6-B2b-A gives a document owner a locally editable **working copy**:
a per-owner `current.docx` living outside Git under a caller-supplied
`working_copy_root`, paired with a `binding.json` sidecar that records
which current Proposal DOCX it was last published from.

The sidecar is **cache and binding metadata only**. Authority for a
Proposal's content always remains:

- the current Proposal DOCX slot,
- the Proposal artifact metadata (`CvDocxProposalArtifact`),
- the Proposal repository bytes,

all exposed through the existing P6-A `CvDocumentArtifactRepository`
Protocol (`app/services/cv_document_repository_protocol.py`). P6-B2b-A
introduces no second source of truth for a Proposal's content, no new SQL
table, and no migration.

## Scope

P6-B2b-A implements:

1. a validated working-copy root (`WorkingCopyPaths`),
2. a per-owner cross-process lock (`PlatformOwnerLock`),
3. a POSIX stable read of `current.docx`,
4. a Windows stable read via `CreateFileW`/`ReadFile`,
5. `WorkingCopyObservedState` (a closed snapshot of one owner's pair),
6. `create_or_refresh_working_copy`,
7. `reset_working_copy_to_current_proposal`,
8. optimistic CAS (proposal-before/proposal-after freshness checks),
9. atomic DOCX + sidecar publication,
10. read-only reconciliation.

It implements **no** concrete `FinalDocxSourceReader`, no finalization
wiring, no PDF conversion, no local file opener, no API, no router, no
frontend, no SQL, no migration, and no `config.py` change.

## Root Contract

`WorkingCopyPaths(working_copy_root, blob_store_root)` fail-closed rejects:

| Violation | Code |
|---|---|
| relative root | `RELATIVE_ROOT` |
| root inside a Git worktree | `ROOT_INSIDE_GIT_WORKTREE` |
| `working_copy_root == blob_store_root` | `ROOT_EQUALS_BLOB_ROOT` |
| root inside the blob root | `ROOT_INSIDE_BLOB_ROOT` |
| blob root inside the working-copy root | `BLOB_ROOT_INSIDE_WORKING_COPY_ROOT` |
| a symlink/reparse point at a path this stage manages | `SYMLINK_OR_REPARSE_POINT` |
| a derived path resolving outside the root | `PATH_ESCAPE` |
| a malformed `owner_key_fingerprint` | `INVALID_OWNER_KEY_FINGERPRINT` |

Layout under `working_copy_root`:

```
<working_copy_root>/
  locks/
    <owner_key_fingerprint>.lock
  owners/
    <owner_key_fingerprint>/
      current.docx
      binding.json
      tmp/
```

`owner_key_fingerprint` (exactly 64 lowercase hex characters) is the
**only** dynamic filesystem path segment. No method on `WorkingCopyPaths`
accepts a caller-supplied path, and no public `WorkingCopyStore` operation
accepts a `current.docx`/`binding.json` path either -- every path is
derived internally from the caller's `JobArtifactOwnerKey`.

An ordinary OS-level symlink *outside* this stage's own managed tree
(e.g. macOS's `/var` -> `/private/var`) is not a Root Contract violation --
only a symlink at or under a path P6-B2b-A itself manages is rejected.

## `WorkingCopyBinding` (the sidecar)

```python
class WorkingCopyBinding:
    owner_key_fingerprint: str
    bound_proposal_artifact_fingerprint: str
    bound_proposal_revision: int
    bound_generated_proposal_sha256: str
    last_known_tool_written_hash: str
    binding_schema_version: str
```

Serialized deterministically (`serialize_working_copy_binding`) so an
unchanged binding always reserializes to identical bytes. Never trusted at
face value: every operation that reads it re-derives
`WorkingCopyBindingState` (`VALID`/`MISSING`/`INVALID`) from a fresh parse
and an owner-fingerprint cross-check against the containing directory.

## `WorkingCopyObservedState` and `binding_state_fingerprint`

A closed snapshot built only after a **successful stable read** of
`current.docx` -- a missing/unreadable/torn read is reported by the
wrapping operation's own status instead of a partially-populated
`WorkingCopyObservedState`.

`binding_state_fingerprint` is the SHA-256 of a canonical payload over: the
observed-state schema version, the owner fingerprint, the working-copy
SHA-256, the binding state, the raw sidecar SHA-256 (or `null`), the
binding schema version (or `null`), and the bound proposal fingerprint/
revision (or `null`). Any change to the raw sidecar bytes changes this
fingerprint, even if `WorkingCopyBinding.model_validate` would still parse
the new bytes into an equal-looking object -- reset confirmation is always
checked against the fingerprint of the exact bytes, not a re-parsed value.

`inspect_working_copy(owner_key)` is the read-only public operation that
returns a `WorkingCopyInspectionResult` wrapping this state.

## Per-owner cross-process lock

`PlatformOwnerLock` never depends on `filelock`/`portalocker`:

- **POSIX**: `os.open(..., O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)` +
  `fcntl.flock(LOCK_EX | LOCK_NB)` in a bounded poll loop, with
  pre-open/post-open/post-lock `lstat`-vs-`fstat` identity checks
  (device/inode match, regular-file check, hardlink (`st_nlink > 1`)
  rejection).
- **Windows**: `CreateFileW` (rejecting a reparse point) +
  `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY)` on a
  1-byte region at offset 0, in the same bounded poll loop; `UnlockFileEx`
  + exactly one `CloseHandle` in `finally`.

Timeout -> `OWNER_LOCK_TIMEOUT`. Any identity check failure ->
`LOCK_PATH_IDENTITY_MISMATCH`.

### Lock token capability

`acquire()` returns an `OwnerLockToken` -- an opaque object with no
`__eq__`/`__hash__` override, so only object identity ever counts as a
match. Each `PlatformOwnerLock` instance keeps its own in-memory registry
keyed by a monotonically increasing acquisition ID; `validate_active_token`
requires the *exact* registered object, for the *exact* instance, for the
*exact* owner. A forged token, a `copy.copy()` of a live token, an
already-released token, and a token issued by a different lock instance
(different owner) are all rejected. One `PlatformOwnerLock` instance
supports exactly one live acquisition -- a second `acquire()` call while
one is outstanding raises `RuntimeError` (no nested acquisition).

Every public `WorkingCopyStore` operation acquires its owner's lock exactly
once, validates the token, and delegates to a private
`_..._under_lock` method; none of those private methods ever re-acquire
the lock.

## Stable read

**POSIX** (`posix_stable_read`): up to 5 attempts. Each attempt
`lstat`s before opening (rejecting a symlink), opens
`O_RDONLY | O_NOFOLLOW`, confirms path-vs-fd identity, reads the exact
byte count from the pre-read `fstat`, then re-`fstat`/re-`lstat`s and
requires `st_dev`/`st_ino`/`st_size`/`st_mtime_ns`/`st_ctime_ns` (and the
actual byte count) to still agree before trusting the read. Any mismatch
retries; exhausting 5 attempts reports
`WORKING_COPY_CHANGED_DURING_READ`.

**Windows** (`windows_stable_read`): `CreateFileW` with `FILE_SHARE_READ`
only -- excluding `FILE_SHARE_WRITE`/`FILE_SHARE_DELETE`, so the OS itself
refuses a concurrent writer rather than this code racing one. A sharing
violation (WinError 32/33) maps to `WORKING_COPY_LOCKED`. After opening:
reject a reparse point, resolve the file's `FileId` (via
`GetFileInformationByHandleEx(FileIdInfo)`, falling back to
`BY_HANDLE_FILE_INFORMATION` when unavailable), `ReadFile` the exact byte
count in a bounded loop, re-check size and `FileId` identity, then open a
second, independent path-probe handle and require its identity to match
too. Exactly one `CreateFileW` -> `ReadFile` -> `CloseHandle` sequence per
handle -- never `msvcrt.open_osfhandle`/`os.fdopen` on the raw `HANDLE`.

## Lifecycle matrix (`create_or_refresh_working_copy`)

Under the owner lock: fetch `proposal_before`, verify the repository's own
bytes hash against `generated_docx_content_hash`, then observe
`current.docx` and `binding.json` together and apply exactly one of:

| # | `current.docx` | sidecar | condition | outcome |
|---|---|---|---|---|
| A | missing | missing | -- | create pair -> `WORKING_COPY_CREATED` |
| B | present | missing | `docx_sha256 == proposal.hash` | rebuild sidecar only -> `RECOVERED_UNEDITED_HALF_STATE` |
| B | present | missing | otherwise | preserve, no-op -> `HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT` |
| C | missing | present (valid or invalid) | -- | recreate pair -> `WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL` |
| D | present | valid | `docx_sha256 == last_known_tool_written_hash` and binding names the current revision | no-op -> `WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED` |
| D | present | valid | `docx_sha256 == last_known_tool_written_hash` but binding is stale | refresh pair -> `WORKING_COPY_REFRESHED` |
| D | present | valid | `docx_sha256 != last_known_tool_written_hash` | preserve, no-op -> `HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT` |
| E | present | invalid | `docx_sha256 == proposal.hash` | rebuild sidecar only -> `RECOVERED_UNEDITED_HALF_STATE` |
| E | present | invalid | otherwise | preserve, no-op -> `HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT` |

**An edited (or possibly edited) `current.docx` is never automatically
overwritten.** Immediately before any publish, `proposal_after` is
re-fetched and compared (`artifact_fingerprint`/`proposal_revision`/
`generated_docx_content_hash`) against `proposal_before`; a mismatch aborts
with `CURRENT_PROPOSAL_CHANGED_DURING_OPERATION` and publishes nothing.

## `reset_working_copy_to_current_proposal` (CAS)

Never accepts caller-supplied DOCX bytes -- always publishes exact
repository proposal bytes. The caller presents a `WorkingCopyResetConfirmation`
(`expected_working_copy_sha256`, `expected_binding_state_fingerprint`,
`expected_target_proposal_artifact_fingerprint`,
`expected_target_proposal_revision`) captured from a prior
`inspect_working_copy` call. Under the owner lock: stable-read
`current.docx`, rebuild the observed state, and compare against every
expected field before fetching `proposal_before`, re-verifying repository
bytes, re-fetching `proposal_after`, and only then publishing:

`RESET_TO_CURRENT_PROPOSAL` |
`WORKING_COPY_CHANGED_SINCE_CONFIRMATION` |
`WORKING_COPY_BINDING_CHANGED_SINCE_CONFIRMATION` |
`CURRENT_PROPOSAL_CHANGED_SINCE_CONFIRMATION` |
`CURRENT_PROPOSAL_CHANGED_DURING_OPERATION` |
`REPOSITORY_BYTES_HASH_MISMATCH` | `NO_CURRENT_PROPOSAL`.

Reset works uniformly whether the sidecar was observed `VALID`, `MISSING`,
or `INVALID`.

## Atomic publication

Under the single owner lock already held: temp-write (when the DOCX is
being rewritten) -> flush -> `fsync` -> rehash, temp-write the sidecar ->
flush -> `fsync` -> rehash, a final `proposal_after` freshness check, then
`os.replace` the DOCX (if rewritten) and the sidecar, a best-effort
directory `fsync`, and a post-write re-read verification of both final
files. A crash between the two `os.replace` calls can leave a half-state on
disk -- the lifecycle matrix (branches B/C/E) is exactly what recovers from
that on the next `create_or_refresh_working_copy` call. P6-B2b-A makes no
promise of a single cross-store transaction spanning the Proposal SQL slot
and this filesystem tree.

### Residual race: the Proposal SQL slot and the filesystem are not one transaction

The `proposal_after` freshness check (re-fetching the current Proposal
immediately before `os.replace` and comparing it against `proposal_before`)
closes the *largest* window for publishing against a proposal this
operation never actually observed, but it does not -- and cannot, without a
distributed transaction this stage deliberately does not build -- close the
window completely. The Proposal SQL slot (`CvDocumentArtifactRepository`)
and this filesystem tree are two independent stores with no shared commit:

1. `_publish_and_finalize` re-fetches `proposal_after` and confirms its
   identity still matches `proposal_before`.
2. Immediately after that check returns, but *before* `_atomic_publish`'s
   `os.replace` calls land, a concurrent writer can advance the current
   Proposal to a new revision.
3. This operation's `os.replace` still lands, publishing bytes and a
   binding that were correct for the proposal identity it verified in step
   1 -- but that proposal identity is no longer the *current* one by the
   time the bytes are actually live on disk.

The result is a **stale-but-consistent binding**: `current.docx` and
`binding.json` agree with each other (the sidecar's
`last_known_tool_written_hash` matches the DOCX bytes actually on disk, and
`bound_proposal_artifact_fingerprint`/`bound_proposal_revision` correctly
name the proposal this operation actually published from) -- they are
simply no longer bound to *the* current Proposal, because the Proposal
moved on again after this operation's last freshness check. This is not
data corruption and it is never silently masked: it surfaces the next time
anything re-observes the pair.

**Detection.** The very next `create_or_refresh_working_copy` call for the
same owner re-fetches the (now further-advanced) `proposal_before`, finds
`docx_sha256 == last_known_tool_written_hash` (branch D, sidecar `VALID`)
but the binding no longer names the current revision, and reports
`WORKING_COPY_REFRESHED` -- exactly the same status an ordinary,
non-racy stale binding produces. `run_working_copy_reconciliation`
independently flags the same stale-but-consistent binding as
`STALE_BINDING`/`BINDING_MISMATCH` whenever it is run with
`owner_keys_by_fingerprint` + `repository` supplied. Neither path requires
any change to this stage's status enums or lifecycle matrix -- a
stale-but-consistent binding left by this race is, by construction,
indistinguishable from (and recovered by exactly the same mechanism as) an
ordinary stale binding produced by any other sequence of events.

This residual race is accepted, not a defect this stage silently hides:
closing it fully would require either a distributed transaction across the
Proposal SQL slot and the filesystem, or re-taking a filesystem-visible
lock that spans the SQL write path itself -- both out of scope for
P6-B2b-A, which only ever guards the working-copy *filesystem* tree.

## Reconciliation

`run_working_copy_reconciliation` is a pure, read-only observation pass
over `owners/` and `locks/`. It never moves, deletes, quarantines, or
rewrites anything -- calling it twice on unchanged storage always yields an
identical report. It detects: `DOCX_WITHOUT_SIDECAR`,
`SIDECAR_WITHOUT_DOCX`, `INVALID_SIDECAR`, `OWNER_MISMATCH`,
`BINDING_MISMATCH`, `STALE_BINDING`, `SYMLINK_OR_REPARSE_POINT`,
`PATH_ESCAPE`, `EMPTY_FILE`, `UNREADABLE_FILE`, `UNEXPECTED_FILE`, and
`LOCK_PATH_ANOMALY`.

`STALE_BINDING`/`BINDING_MISMATCH`'s current-revision-hash check are only
evaluated when the caller supplies `owner_keys_by_fingerprint` +
`repository` (both optional) -- reconciliation never depends on SQL or an
API to report every purely structural issue.

**An edited working copy is never one of these issue codes.** A
`current.docx`/`last_known_tool_written_hash` mismatch is exactly the
signal `create_or_refresh_working_copy` treats as
`HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT`, not a reconciliation finding.
This module never emits a global blob-orphan code -- that remains
exclusively `cv_document_reconciliation.py`'s P6-B1 blob-store concern.

## Closed error channel

All three public `WorkingCopyStore` operations
(`create_or_refresh_working_copy`, `reset_working_copy_to_current_proposal`,
`inspect_working_copy`) are closed error channels: every internal exception
this class or its collaborators can raise is caught and mapped to a status
on the operation's own closed enum, never re-raised to the caller. Only
`WorkingCopyPublishError` (an internal post-write verification failure --
see Atomic publication above), `RuntimeError` (an internal owner-lock
contract violation, e.g. the no-nested-acquisition guard), and
`OSError`/`json.JSONDecodeError` (any unclassifiable filesystem/storage
problem -- `PermissionError` is included as an `OSError` subclass) are ever
caught this way; `BaseException`/`KeyboardInterrupt`/`SystemExit` are never
caught. `WorkingCopyPublishError` itself remains a private implementation
detail of `cv_document_working_copy_store.py` -- it is never part of any
public contract and is never expected to be caught by a caller of
`WorkingCopyStore`.

Mapping:

| Internal condition | Closed status |
|---|---|
| post-write pair verification failed (`WorkingCopyPublishError`) | `POST_WRITE_VERIFICATION_FAILED` |
| invalid internal owner-lock capability (`RuntimeError`) | `LOCK_CAPABILITY_INVALID` |
| unclassifiable storage/filesystem problem (`OSError`/`PermissionError`/`JSONDecodeError`) | `STORAGE_UNAVAILABLE` |

`POST_WRITE_VERIFICATION_FAILED` and `STORAGE_UNAVAILABLE` are added to
both `WorkingCopyCreateRefreshStatus` and `WorkingCopyResetStatus` (the two
statuses that publish); `WorkingCopyInspectionStatus` (read-only, never
publishes) only gains `STORAGE_UNAVAILABLE`, since it has no post-write
step to fail. All three join the existing "must carry no published pair"
branch of each result model's `model_validator` -- a non-`GENERATED`/
non-success status can never carry `published_working_copy_sha256` or
`published_binding`, exactly like every other closed-failure status this
stage already defines.

## Windows implementation: contract-tested, not runtime-verified

The Windows backends in `cv_document_owner_lock.py`
(`_windows_acquire`/`_windows_release`) and
`cv_document_working_copy_stable_read.py` (`windows_stable_read`) never call
`ctypes.WinDLL`/`kernel32` directly. Each goes through a small internal
Protocol (`_Win32LockApi`, `_Win32ReadApi`) with exactly one production
implementation (`_CtypesWin32LockApi`, `_CtypesWin32ReadApi`), which is only
ever constructed on `sys.platform == "win32"`. On every other platform,
calling `_windows_acquire`/`_windows_release`/`windows_stable_read` without
an explicit `api=` raises `RuntimeError` rather than silently doing nothing
or attempting a real Win32 call.

`tests/unit/test_cv_document_owner_lock.py` and
`tests/unit/test_cv_document_working_copy_stable_read.py` each carry a
Windows *contract* suite that calls these functions directly with a fake
implementing the same Protocol, exercising the real handle-open /
reparse-point / identity-resolution (`FileIdInfo`, with the
`BY_HANDLE_FILE_INFORMATION` fallback exercised separately) /
locking / handle-bookkeeping logic in `_windows_acquire`/
`windows_stable_read` themselves -- only the underlying syscalls are faked.
These tests are unconditional (no `skipif`) and run as ordinary `passed`
results in every `pytest` run on this repository, including on Darwin/CI,
specifically so a regression in this logic is caught without ever touching
a Windows machine.

**What this does and does not prove.** These contract tests prove the
Python-level control flow -- which flags are passed to which call, in what
order, how identity is compared, how every handle is closed on every
branch -- is correct against the fake's stand-in for the Win32 ABI. They do
**not** exercise the real `kernel32.dll` ABI, real ctypes struct layout
against the actual OS, real `WinError` behavior under real contention, or
any real filesystem/locking semantics on an actual Windows kernel.
`_CtypesWin32LockApi`/`_CtypesWin32ReadApi` themselves -- the only code
paths that touch real ctypes bindings -- have never been executed, on any
platform, as part of this stage's test suite; they can only run on
`win32`, and no Windows runtime execution has occurred. Real Windows
verification (running this code, under load, on an actual Windows host)
remains a separate, future gate this stage does not claim to satisfy.

## Files

| File | Contents |
|---|---|
| `app/schemas/cv_document_working_copy.py` | `WorkingCopyBinding`, `WorkingCopyObservedState`, every operation's closed status enum + result model, reconciliation contracts |
| `app/services/cv_document_working_copy_paths.py` | `WorkingCopyPaths`, the Root Contract |
| `app/services/cv_document_owner_lock.py` | `PlatformOwnerLock`, `OwnerLockToken` |
| `app/services/cv_document_working_copy_stable_read.py` | `posix_stable_read`, `windows_stable_read` |
| `app/services/cv_document_working_copy_store.py` | `WorkingCopyStore` (the three public operations) |
| `app/services/cv_document_working_copy_reconciliation.py` | `run_working_copy_reconciliation` |

## What P6-B2b-A does not do

No concrete `FinalDocxSourceReader`, no finalization wiring, no PDF
conversion, no local file opener, no API router, no request/response
schema, no frontend, no SQL table, no migration, and no `config.py`
change. A future stage is expected to wire a router/service on top of
`WorkingCopyStore` through a threadpool, the same way P6-B1's synchronous
repository documents its own async boundary.

It also does not claim real Windows runtime verification -- see
"Windows implementation: contract-tested, not runtime-verified" above --
and it does not claim to close the Proposal-SQL-slot-vs-filesystem residual
race described under "Atomic publication" above; both remain explicit,
accepted, documented gaps rather than silently assumed to be solved.
