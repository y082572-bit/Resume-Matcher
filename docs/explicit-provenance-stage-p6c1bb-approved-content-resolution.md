# Explicit Provenance Stage P6-C1b-B: Approved Content Resolution Composition Root

## Purpose

Stage P6-C1b-B gives Explicit Provenance the single, synchronous
composition root that turns "the current Approved Content Package for this
owner" into "a re-validated `ApprovedContentDocumentInput` a caller can
trust" -- `resolve_current_approved_content_document_input` in
`app/services/cv_approved_content_resolution.py`. It is a pure, fail-closed
reader over the existing `ApprovedContentPackageRepository` Protocol
(P6-C1b-A): it never saves, promotes, or builds a package, never imports
FastAPI/Starlette/SQLAlchemy/a router/an HTTP schema, and never extends the
repository Protocol itself.

## Domain model (`app/schemas/cv_approved_content_resolution.py`)

- **`ApprovedContentResolutionStatus`** -- `FOUND`, `NOT_FOUND`,
  `STALE_AUTHORITY_OBSERVATION`, `OWNER_MISMATCH`,
  `DOCUMENT_INPUT_FINGERPRINT_MISMATCH`, `DOCUMENT_INPUT_INVALID`,
  `STORAGE_METADATA_CONFLICT`, `STORAGE_UNAVAILABLE`.
- **`ApprovedContentResolutionResult`** (frozen, `extra="forbid"`) --
  `status`, `document_input: ApprovedContentDocumentInput | None`,
  `observed_package_fingerprint: str | None` (SHA-256 pattern),
  `observed_authority_slot_version: int | None` (`ge=1`). A model
  validator enforces that `FOUND` carries all three of
  `document_input`/`observed_package_fingerprint`/
  `observed_authority_slot_version` and every other status carries none of
  them -- there is no partially reconstructed input and no raw `A1`/`A2`
  authority observation leaking out of a failure result. The result never
  embeds a full `ApprovedContentPackage`, a free-text diagnostic, exception
  text, proposal text, or approval-decision detail.

## Resolver (`app/services/cv_approved_content_resolution.py`)

```python
def resolve_current_approved_content_document_input(
    *,
    owner_key: JobArtifactOwnerKey,
    repository: ApprovedContentPackageRepository,
) -> ApprovedContentResolutionResult: ...
```

Synchronous. Zero retries anywhere: `repository.read_current_authority` is
called at most twice (the `A1`/`A2` pair below) and
`repository.get_package_by_fingerprint` at most once.

### Owner-key self-consistency (before any repository read)

The caller-supplied `owner_key` is never trusted at face value just because
it came from a server-side caller. It is independently rebuilt from its own
`person_entity_id`/`owner_kind`/`owner_reference_id` via the existing
`build_owner_key`, and the full recomputed key -- including
`owner_key_fingerprint` explicitly -- must match the input exactly.
Any mismatch is `OWNER_MISMATCH` with **zero** repository calls.

### Stable A1 -> package -> A2 observation

1. **A1**: first `read_current_authority(owner_key)`.
   `NOT_FOUND` -> `NOT_FOUND`; `STORAGE_METADATA_CONFLICT` ->
   `STORAGE_METADATA_CONFLICT`; `STORAGE_UNAVAILABLE` ->
   `STORAGE_UNAVAILABLE`. No further reads on any of these.
2. **Package read**: `get_package_by_fingerprint(A1.package_fingerprint)`,
   exactly once -- the package is immutable and content-addressed, so it is
   never read a second time regardless of how many times the authority
   pointer is observed. `NOT_FOUND` here (a promoted pointer with no
   backing row -- a dangling authority) maps to
   `STORAGE_METADATA_CONFLICT`, never an ordinary "not found".
   `STORAGE_METADATA_CONFLICT`/`STORAGE_UNAVAILABLE` pass through unchanged.
3. **Owner binding** -- every one of these must hold, independently, before
   `A2` is ever read:
   - `package.owner_key_fingerprint == owner_key.owner_key_fingerprint`
   - `package.owner_kind == owner_key.owner_kind`
   - `package.payload.owner_kind == owner_key.owner_kind`
   - `package.payload.source_content_plan.person_entity_id == owner_key.person_entity_id`
   - `package.payload.source_content_plan.job_or_application_id == owner_key.owner_reference_id`

   Any mismatch is `OWNER_MISMATCH`; `A2` is never read.
4. **A2** (the linearization point): second
   `read_current_authority(owner_key)`. `FOUND` is only ever reachable when
   `A2.package_fingerprint == A1.package_fingerprint` **and**
   `A2.slot_version == A1.slot_version` -- both compared, never just the
   fingerprint alone (an ABA authority move -- `v1(slot1) -> v2(slot2) ->
   v1(slot3)` -- would read as "unchanged" on fingerprint alone but is
   caught by the paired slot-version comparison).
   `NOT_FOUND`/any mismatch -> `STALE_AUTHORITY_OBSERVATION`;
   `STORAGE_METADATA_CONFLICT`/`STORAGE_UNAVAILABLE` pass through unchanged.

### Flat reconstruction and fingerprint recomputation

Reconstruction reads only `package.payload` -- never
`package.document_input_fingerprint` itself, which is instead the
comparison target. The current `current_p55_replay_input` fingerprint is
recomputed via the existing `compute_approved_content_replay_input_fingerprint`,
then the document-input fingerprint via the existing
`compute_document_input_fingerprint` (never a hand-rolled copy of either
formula). A mismatch against `package.document_input_fingerprint` is
`DOCUMENT_INPUT_FINGERPRINT_MISMATCH`.

`ApprovedContentDocumentInput` is then built via a normal (non-`model_copy`)
constructor call from deep copies of every nested payload field
(`approved_content_result`, `source_content_plan`,
`current_p55_replay_input`, `semantic_validation_replay_input`, every
`approval_decisions` entry), `payload.owner_kind`, both schema/policy
version literals, and the freshly recomputed fingerprint. Pydantic
re-validates nested model instances on a normal constructor call (unlike
`model_copy`), so a payload whose embedded `CvContentPlan` fails its own
`ROLE_STRATEGY_INTEGRATED` strategy-field invariant raises a
`pydantic.ValidationError` here -- caught and mapped to
`DOCUMENT_INPUT_INVALID`, never left to escape as a raw exception.

### Mandatory document-input validation

The reconstructed input is always re-run through the existing, independent
`validate_approved_content_document_input`. `FOUND` requires
`status == VALID`; every other status or violation code is
`DOCUMENT_INPUT_INVALID` -- the specific violation code is never surfaced in
the result. If validation returns an `owner_key` (only possible when
`VALID`), it is compared in full against the caller's own `owner_key`;
any difference is `OWNER_MISMATCH`, even though `VALID` was reported.

### Success result

`FOUND` carries a defensive deep copy of the reconstructed
`document_input`, `observed_package_fingerprint = A2.package_fingerprint`,
and `observed_authority_slot_version = A2.slot_version` -- the two scalars
observed at the linearization point, never the full authority object and
never the full package.

## Status mapping (summary)

| Signal | Status |
|---|---|
| Owner-key self-inconsistency | `OWNER_MISMATCH` (zero repository calls) |
| A1 `NOT_FOUND` | `NOT_FOUND` |
| A1 `STORAGE_METADATA_CONFLICT` / package `STORAGE_METADATA_CONFLICT` / A2 `STORAGE_METADATA_CONFLICT` | `STORAGE_METADATA_CONFLICT` |
| A1 `STORAGE_UNAVAILABLE` / package `STORAGE_UNAVAILABLE` / A2 `STORAGE_UNAVAILABLE` | `STORAGE_UNAVAILABLE` |
| Package `NOT_FOUND` after A1 `FOUND` (dangling authority) | `STORAGE_METADATA_CONFLICT` |
| Owner/package binding mismatch | `OWNER_MISMATCH` |
| A2 `NOT_FOUND` / A2 fingerprint or slot-version mismatch (incl. ABA) | `STALE_AUTHORITY_OBSERVATION` |
| Recomputed `document_input_fingerprint` mismatch | `DOCUMENT_INPUT_FINGERPRINT_MISMATCH` |
| Reconstruction `ValidationError` / validation not `VALID` | `DOCUMENT_INPUT_INVALID` |
| Validation-returned `owner_key` mismatch | `OWNER_MISMATCH` |
| A1/package/A2 all consistent and validation `VALID` | `FOUND` |

## Tests (`apps/backend/tests/unit/test_cv_approved_content_resolution.py`)

Covers the schema's closed status/result consistency; owner-key
self-inconsistency (forged fingerprint, owner-kind/person/reference
tamper) rejected with zero repository calls; the exact A1/package/A2
status mapping via a scripted repository stub; every owner-binding
violation in isolation; document-input fingerprint mismatch and two
distinct `DOCUMENT_INPUT_INVALID` violation families (stale freshness,
incomplete strategy fields), plus a defense-in-depth check for a
validation-returned owner-key mismatch; concurrency/ABA fail-closed
behavior (a real authority promotion injected between A1 and the package
read, and between the package read and A2, via a hooked wrapper around a
real `InMemoryApprovedContentPackageRepository`); a real
`InMemoryApprovedContentPackageRepository` happy path and absent-authority
case; and a real SQLite-backed `ApprovedContentPackageSqlRepository`
happy path, restart persistence, and two fail-closed reads against a
store forced into a corrupt state that the schema's own triggers/foreign
keys make unreachable through any conforming write path (proving the
resolver's read-side defenses hold independently of any one repository's
own write-time guarantees).

## No HTTP/router scope

This stage implements no FastAPI router, no HTTP endpoint, no dependency
provider for a router, and no request/response schema. The resolver is a
plain Python function over the existing repository Protocol; nothing here
is reachable from an HTTP client.

## No Proposal generation scope

This stage never generates a Proposal DOCX, never calls
`generate_current_deterministic_docx_proposal` or any renderer, and never
touches the working-copy flow, `FinalDocxSnapshot`, or PDF generation.

## Production source gap

**P6-C1b-B does not create, save, or promote Approved Content Packages.**
The current production pipeline still does not build or persist a package
outside of tests -- every package used in this stage's own tests is
constructed directly via `build_approved_content_package` and saved/
promoted directly against a repository, never through a real PRE-P4 ->
P5.5 production call path. After P6-C1b-B, the next required stage before
a real POST Proposal flow can resolve anything is:

**Approved Content Package Production Orchestration.**
