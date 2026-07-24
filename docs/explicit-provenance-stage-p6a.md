# Explicit Provenance Stage P6-A: Document Artifact Lifecycle Core

> **Addendum:** a separate, strictly additive `FinalDocxSnapshot` domain
> flow (no validation, no manual confirmation, trusted source binding) is
> documented in
> [`explicit-provenance-stage-p6a-final-snapshot-addendum.md`](explicit-provenance-stage-p6a-final-snapshot-addendum.md).

## Purpose

Stage P6-A is a pure, deterministic, fail-closed core that turns an
already-`READY`/fresh `ApprovedCvContentResult` (Stage P5.5) into a chain of
immutable, content-addressed document artifacts:

```
ApprovedCvContentResult
  -> CvDocxProposalArtifact   (current proposal slot, 0/1 per owner)
  -> ValidatedCvDocxSnapshot  (immutable, content-addressed)
  -> ConfirmedCvPdfArtifact   (current PDF slot, 0/1 per owner)
```

P6-A creates no new CV content, calls no LLM, performs no real DOCX
rendering, no real PDF conversion, no filesystem I/O, no database I/O, and
exposes no API. Every adapter is a pure Protocol; the only concrete
implementation P6-A ships is an in-memory repository for tests.

## P6-A / P6-B / P6-C split

| Stage | Scope |
|---|---|
| **P6-A** (this stage) | Closed domain models, fingerprints, Protocols, in-memory repository, proposal/snapshot/PDF-confirmation lifecycle, manual-edit detection, replay/freshness. |
| **P6-B** (future) | A real filesystem repository with atomic file operations, real DOCX rendering and PDF conversion adapters, a real local-open adapter, and the API routers. |
| **P6-C** (future) | The frontend. |

P6-A implements **no UI** of any kind, and defines no route, no endpoint,
and no request/response schema for an API.

## Owner identity: `JobArtifactOwnerKey`

A document artifact owner is one job or one application, named only by:

- `person_entity_id`,
- an explicit `ArtifactOwnerKind` (`JOB` or `APPLICATION`) — **always
  caller-supplied**, since a raw `job_or_application_id` string never by
  itself disambiguates the two namespaces,
- `owner_reference_id`, which must always be `CvContentPlan.job_or_application_id`
  (never an independently supplied string).

`owner_key_fingerprint` is `SHA-256(canonical_json_bytes({person_entity_id,
owner_kind, owner_reference_id, owner_key_schema_version}))` — never a URL,
employer name, job title, filename, filesystem path, or content-plan
fingerprint. A content-plan fingerprint changes between document versions
of the very same owner and must never leak into owner identity — see
`cv_document_owner_identity.py` and `tests/unit/test_cv_document_owner_identity.py`.

## `ApprovedContentDocumentInput` and its independent re-validation

`ApprovedContentDocumentInput` is a closed, `extra="forbid"` envelope the
caller hands to P6-A. Nothing on it — including its own
`document_input_fingerprint` — is trusted at face value.
`validate_approved_content_document_input` (in
`cv_document_input_validation.py`) independently:

1. Recomputes `ApprovedCvContentResult.result_fingerprint` via the same
   `compute_approved_content_result_fingerprint` P5.5 itself uses, from the
   result's own stored top-level fields.
2. Requires `status == READY` and `ready_for_p6 == True` — `ready_for_p6`
   is one invariant among several, never the sole basis for trust.
3. Requires zero `pending_approval_fingerprints` and no disposition still
   carrying an unresolved semantic-validation code
   (`PENDING_USER_DECISION`/`SEMANTIC_VALIDATION_INCONCLUSIVE`/
   `SEMANTIC_VALIDATION_PROVIDER_ERROR`).
4. Recomputes `CvContentPlan.content_plan_fingerprint` via
   `compute_content_plan_fingerprint`, fed by the plan's own stored
   section/pending/omission/conflict sub-fingerprints, and requires it to
   match both the plan's own stored value and
   `approved_content_result.content_plan_fingerprint`.
5. Rebuilds the "previous" `ApprovedCvContentReplayInput` from the stored
   result via P5.5's own `replay_input_from_approved_content_result` (fed
   by the caller-supplied `semantic_validation_replay_input` and
   `approval_decisions`), and calls P5.5's own
   `evaluate_approved_content_freshness` against the caller's
   independently-supplied `current_p55_replay_input` — **the caller's own
   freshness verdict, if any, is never trusted**; only `FRESH` passes.
6. Builds the owner key (see above) and independently recomputes
   `document_input_fingerprint`, requiring an exact match.

A P5a draft (`CvContentGenerationResult`) or a P5b proposal result
(`CvContentProposalGenerationResult`) or an arbitrary dict can never even
be constructed into this envelope: `approved_content_result` is strictly
typed as `ApprovedCvContentResult`, so Pydantic itself rejects any other
shape at construction time.

### Mandatory `ROLE_STRATEGY_INTEGRATED` content plan

**P6-A accepts only P5.5 content originating from a
`ROLE_STRATEGY_INTEGRATED` `CvContentPlan`.** A `LEGACY`
`source_content_plan.plan_mode` is always rejected fail-closed
(`LEGACY_CONTENT_PLAN_NOT_ALLOWED`) — there is no fallback path that
admits one, regardless of how otherwise valid/fresh/`READY` the P5.5
result is.

`validate_approved_content_document_input` checks this independently of
`CvContentPlan`'s own `_strategy_fields_match_mode` model validator, since
a plan constructed via `model_copy(update=...)` (or `model_construct(...)`)
never re-runs that validator and could otherwise carry
`plan_mode=ROLE_STRATEGY_INTEGRATED` alongside a `None` strategy field.
So on top of the plan-mode check, every one of
`role_strategy_context_fingerprint` / `strategy_selection_result_fingerprint`
/ `strategy_ranking_input_fingerprint` / `strategy_integration_policy_version`
is independently re-checked for non-`None`
(`CONTENT_PLAN_STRATEGY_FIELDS_INCOMPLETE`) — not merely inferred from
`plan_mode`. All four strategy fields (plus `plan_mode` itself) already
participate in the existing `content_plan_fingerprint` recompute, so a
tampered field is normally also caught by
`CONTENT_PLAN_FINGERPRINT_MISMATCH`; the explicit field check exists as an
independent invariant for the case where a `model_copy`-crafted plan is
internally self-consistent (its own fingerprint recomputed against its own
tampered fields) and would otherwise slip past a recompute-only check. See
`tests/unit/test_cv_document_input_validation.py` for the LEGACY-rejection,
missing-field, tampered-fingerprint, and model_copy-bypass proofs.

## DOCX rendering and template Protocols

`CvDocxRenderingAdapter` (Protocol, in `cv_document_adapters.py`) may only
place already-approved content into template bytes — it can never generate
new content, paraphrase, or call an LLM, and its signature carries no
target path (Master Resume or otherwise): it always returns fresh bytes,
never writes in-place. `DocxTemplateProvider` returns an immutable
`DocxTemplateHandle` (plain `bytes`, never `bytearray`) plus its own
fingerprint and adapter identity — never a domain identity based on a
path. P6-A ships neither adapter for real; tests use deterministic fakes.

Both the proposal builder and the renderer's own claimed `output_sha256`
are independently re-verified: the builder hashes the template bytes
before *and* after the renderer call and requires an unchanged
fingerprint, and recomputes `hashlib.sha256(docx_bytes)` against the
adapter's claimed `output_sha256` — a mismatch fails closed
(`RENDERER_OUTPUT_HASH_MISMATCH`) without ever creating a proposal.

## `CvDocxProposalArtifact` and the proposal builder

`CvDocxProposalArtifact` never carries a filesystem path — `artifact_id`
is a repository locator only and never participates in
`artifact_fingerprint`. `generation_input_fingerprint` folds in the owner
key, the approved-content and content-plan fingerprints, the role-strategy
context fingerprint, the template fingerprint, the renderer adapter
identity, the rendering policy version, and the document schema version —
so changing any one of these changes the fingerprint (proven by
`tests/unit/test_cv_document_proposal_builder.py`). `generated_docx_content_hash`
is the raw SHA-256 of the renderer's exact output bytes; two renderings of
identical inputs are **not** required to produce byte-identical DOCX ZIPs
— only *input* identity is deterministic, never the output ZIP bytes.

`build_current_docx_proposal` (`cv_document_proposal_builder.py`)
independently re-validates the document input first, re-verifies the
template hash before/after rendering, re-verifies the renderer output
hash, then atomically replaces the repository's current-proposal slot via
its `expected_previous_revision` CAS contract. First generation requires
`expected_previous_revision=None`; regeneration requires the exact current
revision. A renderer failure, a tampered hash, or a stale CAS revision
never creates a proposal, never touches the existing current proposal, and
never touches the PDF slot — proven by
**exactly one proposal is ever current** and **regeneration never removes
an existing PDF**.

## Manual-edit detection

`cv_document_manual_edit_detector.py` compares raw SHA-256 bytes only —
never mtime, filesize, filename, path, or Word metadata. `detect_manual_edit`
always recomputes a fresh hash from whatever bytes are handed to *this*
call; it never accepts or trusts a previously stored
`current_file_hash`/`generated_docx_content_hash` as if it were the
current observation. A single-byte difference (even one caused only by
ZIP metadata Word itself rewrites on open/save) is reported as
`USER_EDITED` — an accepted, safe, fail-closed false positive.

## Manual document snapshot confirmation

`ManualDocumentSnapshotConfirmation` is the **only** way a user-edited
proposal can ever become a validated snapshot. For
`confirmation_mode=USER_CONFIRMED_MANUAL_DOCUMENT` a literal `CONFIRMED`
decision is required; the confirmation must name the exact
`exact_docx_sha256` and `proposal_revision` it applies to, and
`build_validated_docx_snapshot` rejects a confirmation for a different
hash or an older revision. The system never infers user consent, and the
resulting snapshot never claims semantic equivalence with
`ApprovedCvContent` — it only carries a fingerprint linkage, not an
"equivalent" flag.

## `ValidatedCvDocxSnapshot` and TOCTOU protection

`build_validated_docx_snapshot` (`cv_document_snapshot_builder.py`) never
builds a snapshot from a live proposal path. Its sequence: read the
current proposal artifact, re-read its exact bytes from the repository,
hash them, compare against the artifact's own generated hash (via the
manual-edit detector), branch on unmodified-vs-edited provenance, run the
structural validator, **re-hash the bytes again after validation** and
require the validator's own `validated_sha256` to still match, then save
into the content-addressed repository and **re-read the saved bytes one
more time** to verify integrity before returning. A hash that changes at
any of these checkpoints fails closed
(`HASH_CHANGED_DURING_VALIDATION`/`REPOSITORY_INTEGRITY_FAILURE`) rather
than silently trusting an earlier observation. Once saved, a snapshot's
bytes are immutable under later proposal regeneration — proven by
`test_snapshot_bytes_are_immutable_after_later_proposal_change`.

## `DocxStructuralValidator` Protocol

Validates exact bytes + an expected SHA-256 against a validation policy,
returning a closed `DocxStructuralValidationResult` (status, the hash it
actually validated, and a closed violation-code tuple). P6-A implements no
real python-docx/OOXML validator; tests use a deterministic fake.

## PDF conversion attempt vs. confirmed PDF

`CvPdfConversionAdapter.convert` accepts only exact immutable snapshot
bytes plus their fingerprint — never a live proposal path, a filename as
identity, or a shell command string — and returns a
`CvPdfConversionAttemptResult`. A `FAILED` attempt is **never** promoted
into a `ConfirmedCvPdfArtifact`: `build_and_confirm_pdf`
(`cv_document_pdf_confirmation_builder.py`) returns the failed attempt
as-is, creates no artifact, does not touch an existing current PDF, and
does not bump the PDF revision. A `SUCCEEDED` attempt's own claimed
`pdf_sha256` is still independently recomputed and compared before any
artifact is ever built (`TAMPERED_PDF_HASH` fails closed otherwise).

## 0/1 invariants and independence of the two slots

The in-memory repository (`InMemoryCvDocumentArtifactRepository`)
maintains exactly one current-proposal slot and exactly one current-PDF
slot per owner, each with its own CAS `expected_previous_revision`
contract. Replacing one slot never touches the other — proven by
`test_regeneration_does_not_remove_existing_pdf` and
`test_pdf_replace_does_not_change_proposal`. The previous current artifact
becomes `superseded=True` history only *after* a successful replace; a
failed replace (stale revision) leaves the previous current artifact
completely untouched.

## Repository Protocol and CAS/atomicity

`CvDocumentArtifactRepository` (`cv_document_repository_protocol.py`) is
the exact contract a future P6-B filesystem repository must satisfy:
`get_current_proposal`/`read_current_proposal_bytes`/`replace_current_proposal`,
the PDF-slot mirror of the same three, `save_validated_snapshot`/
`retrieve_snapshot` (content-addressed by `exact_docx_sha256`), and
`mark_proposal_superseded`/`mark_pdf_superseded`. The in-memory
implementation enforces: first generation requires
`expected_previous_revision=None`; a stale revision is rejected without
mutating the current slot; two sequential stale replace attempts never
produce two current artifacts; and a different content payload offered
under the same `exact_docx_sha256` locator is rejected
(`LOCATOR_CONTENT_MISMATCH`). No fsync, no atomic rename — explicitly out
of scope for P6-A.

### Immutability and defensive copying

Every domain record a repository stores or returns
(`JobArtifactOwnerKey`, `CvDocxProposalArtifact`, `ValidatedCvDocxSnapshot`,
`ConfirmedCvPdfArtifact`, `ManualDocumentSnapshotConfirmation`,
`CvPdfConversionAttemptResult`, `DocumentLifecycleView`) is
`ConfigDict(extra="forbid", frozen=True)` — once built, a normal field
assignment on it always raises. `frozen=True` alone is not treated as
sufficient, though: `InMemoryCvDocumentArtifactRepository` additionally
never stores or returns a caller's own object reference.

- **Ingress** (`replace_current_proposal`/`replace_current_pdf`/
  `save_validated_snapshot`): the repository takes its own
  `artifact.model_copy()` before storing, and normalizes byte-like input
  via `bytes(...)` (never keeping a caller's own `bytearray`/`memoryview`
  around) — mutating the object the caller originally passed to a
  `replace_*`/`save_*` call can never change repository state afterward.
- **Egress** (`get_current_proposal`/`get_current_pdf`/`retrieve_snapshot`,
  and the `current_artifact` on both CAS replace results): every getter
  returns a fresh `.model_copy()` of the stored artifact, never the
  internally stored instance, so `returned_object is not
  internally_stored_object` always holds and two separate calls never
  return the same object identity either. A caller's `model_copy(update=...)`
  on what it was handed back always produces a genuinely new local object,
  never a repository mutation.
- **CAS ordering**: `replace_current_proposal`/`replace_current_pdf` first
  compare `expected_previous_revision` against the current slot — on a
  mismatch, nothing is mutated, and the (possibly `None`) current artifact
  is returned as its own defensive copy. Only once the CAS check passes are
  the incoming artifact/bytes copied and, if a current artifact already
  exists, its `superseded=True` copy built (via `model_copy`, never an
  in-place mutation of the previous artifact) — only then does the current
  slot get atomically swapped and the superseded copy appended to history.

See `tests/unit/test_cv_document_proposal_builder.py`,
`tests/unit/test_cv_document_snapshot_builder.py`, and
`tests/unit/test_cv_document_pdf_confirmation_builder.py` for the direct
object-identity, frozen-assignment, `model_copy`-isolation, and
post-replace-mutation proofs.

## Local document open Protocol

`LocalDocumentOpenAdapter` defines only the Protocol and its result shape
(`LocalDocumentOpenResult`). P6-A never calls `open`, `subprocess`,
AppleScript, `os.startfile`, or `xdg-open` — a real implementation is
deferred to P6-B. Neither a repository locator nor a suggested filename
ever participates in artifact identity or any fingerprint.

## Lifecycle status and `DocumentLifecycleView`

`DocxProposalStatus` and `PdfConfirmationStatus` are two independent
enums — there is deliberately no single collapsed `is_confirmed` boolean.
`compute_document_lifecycle_view` (`cv_document_replay.py`) combines a
current-proposal observation (via the manual-edit detector),
whether a validated snapshot exists for it, and whether the current PDF's
source proposal revision is older than the current proposal revision
(`PDF_CURRENT_BASED_ON_OLDER_PROPOSAL`) — proven by
`test_lifecycle_regeneration_with_existing_pdf_keeps_pdf_current` and
`test_lifecycle_pdf_older_than_proposal`. `CONVERSION_FAILED` is
deliberately **not** a member of `PdfConfirmationStatus`: a failed
conversion is only ever an attempt result, never a current-PDF state.

## Fingerprint chain

```
ApprovedCvContentResult.result_fingerprint
  -> ApprovedContentDocumentInput.document_input_fingerprint
  -> JobArtifactOwnerKey.owner_key_fingerprint
  -> CvDocxProposalArtifact.generation_input_fingerprint
  -> CvDocxProposalArtifact.generated_docx_content_hash   (raw bytes SHA-256)
  -> (current observed DOCX raw hash, via manual-edit detection)
  -> ManualDocumentSnapshotConfirmation.confirmation_fingerprint (if manual)
  -> ValidatedCvDocxSnapshot.snapshot_fingerprint
  -> CvPdfConversionAttemptResult.attempt_fingerprint
  -> (PDF raw bytes SHA-256)
  -> ConfirmedCvPdfArtifact.artifact_fingerprint
```

Every model fingerprint is `SHA-256(canonical_json_bytes(...))` over a
versioned, sorted projection; every raw-bytes hash is
`hashlib.sha256(bytes).hexdigest()`. No fingerprint or identity anywhere in
P6-A is derived from a path, filename, URL, employer name, job title,
`datetime.now()`, `uuid4()` (used only as an opaque `artifact_id`
locator), or Python's built-in `hash()`.

## Replay and freshness

`cv_document_replay.py` defines six independent replay families —
approved document input, proposal generation, current proposal
observation, validated snapshot, PDF conversion, and confirmed PDF — each
with its own `previous`/`current` comparison. `DocumentReplayFreshnessStatus`
covers `FRESH`/`STALE`/`FRESHNESS_NOT_VERIFIED` plus three
byte-level-specific outcomes: `FILE_MISSING`, `FILE_CHANGED` (current
proposal observation), `SNAPSHOT_MISMATCH` (PDF conversion, when the
snapshot bytes handed to conversion no longer hash to the recorded
`exact_docx_sha256`), and `CONVERSION_MISMATCH` (confirmed PDF, when the
current PDF bytes no longer hash to the recorded `pdf_sha256`). No
function here performs I/O or reads a clock; `current` is always supplied
by the caller, recomputed from the caller's actual current state — a
"current" replay input is never built by copying a stored value it is
supposed to be checked against (`test_current_replay_never_reused_from_stored_input`).

## Isolation

Every P6-A core module is proven, at the source level
(`tests/integration/test_p6a_document_core_integration.py`), to never
import `app.llm`/`litellm`, `sqlalchemy`/`app.database`/`app.db_engine`/
`app.models`, `app.routers`/`fastapi`, the frontend, a real `docx`
(python-docx) or `playwright` library, or `pathlib`; and to never call
`subprocess`, `os.startfile`, AppleScript (`osascript`), or `xdg-open`; and
to never use `datetime.now()` or Python's `hash()` inside fingerprint
computation.

## What is explicitly NOT implemented in P6-A

- A real filesystem repository, atomic file writes, or persistence of any
  kind beyond the in-memory test repository.
- A real DOCX renderer or a real PDF conversion adapter (no Word
  automation, no LibreOffice/`soffice`).
- A real local-document-open adapter.
- Any API router, request/response schema, or endpoint.
- Any frontend code.
- Any database or SQL migration.
- Production orchestration of any kind.

These are explicitly deferred: filesystem repository, real adapters, and
the API to **P6-B**; the frontend to **P6-C**.
