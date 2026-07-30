# Explicit Provenance Stage P6-C1b-A: Approved Content Package Foundation

## Purpose

Stage P6-C1b-A gives Explicit Provenance a durable, content-addressed
snapshot of one already-validated `ApprovedContentDocumentInput`: an
`ApprovedContentPackage`. A package is built once, deterministically, from
an owner key plus a raw document-input envelope, and thereafter persisted
verbatim -- immutable, never repaired, never re-derived from a different
input and silently accepted as "the same" package.

This stage never implements the `ApprovedContentDocumentInput` resolver,
the POST Proposal API, router changes, the frontend, PRE-P4 -> P5.5
production orchestration, Proposal DOCX generation, the working-copy flow,
`FinalDocxSnapshot`, the PDF converter, or final PDF authority. It is a
storage/build foundation only.

## Domain model (`app/schemas/cv_approved_content_package.py`)

- **`ApprovedContentPackagePayload`** -- the flat, canonical-JSON-
  serializable content of a package. Every field the caller's
  `ApprovedContentDocumentInput` carried is flattened directly onto this
  model instead of embedding the envelope itself (`payload.document_input`
  is forbidden by construction -- the field does not exist).
- **`ApprovedContentPackageFingerprintInput`** -- the exact content that
  hashes into `package_fingerprint`. Never persisted separately: a reader
  always deterministically reconstructs an equivalent instance from a
  stored package's own columns plus its `payload`.
- **`ApprovedContentPackage`** -- the persisted, immutable unit. Carries
  `package_fingerprint`, `owner_key_fingerprint`, `owner_kind`,
  `document_input_fingerprint`, `approval_decisions_fingerprint`,
  `fact_selection_identity_fingerprint`, `payload_sha256`,
  `payload_byte_size`, `payload`, the three frozen version fields, and
  `created_at`.

Frozen version constants (never change the literal values):

```python
APPROVED_CONTENT_PACKAGE_FINGERPRINT_VERSION = "cv-approved-content-package-fingerprint-v1"
APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION = "cv-approved-content-package-schema-v1"
APPROVED_CONTENT_PACKAGE_POLICY_VERSION = "cv-approved-content-package-policy-v1"
APPROVAL_DECISIONS_FINGERPRINT_VERSION = "cv-approved-content-package-approval-decisions-v1"
FACT_SELECTION_IDENTITY_FINGERPRINT_VERSION = "cv-approved-content-package-fact-selection-v1"
PAYLOAD_LIMIT_BYTES = 1_048_576
```

`package_schema_version`/`package_policy_version` are the only two fields a
caller of the builder may vary (defaulting to the frozen v1 constants); the
package-DDL's own CHECK constraints hard-pin both to the exact v1 literal,
so only packages built under the currently recognized versions can ever be
persisted through the SQL repository.

## Builder (`app/services/cv_approved_content_package_builder.py`)

`build_approved_content_package(*, owner_key, document_input,
package_schema_version=..., package_policy_version=...)`:

1. Re-runs `validate_approved_content_document_input` and requires `VALID`
   -- never trusts the envelope's own claimed fingerprints.
2. Independently rebuilds the owner key from
   `source_content_plan.person_entity_id` /
   `document_input.owner_kind` / `source_content_plan.job_or_application_id`
   via the existing `build_owner_key`, and requires an exact match against
   the caller-supplied `owner_key` (`OWNER_MISMATCH` otherwise).
3. Requires an exact 1:1 coverage of every `PlannedFactUse` in the content
   plan by the result's `dispositions`, and requires every disposition's
   code to be one of the six accepted terminal codes
   (`INCLUDED_DETERMINISTIC`, `INCLUDED_APPROVED_PROPOSAL`,
   `REJECTED_BY_USER`, `SEMANTIC_VALIDATION_FAILED`, `INPUT_INVALID`,
   `NON_ELIGIBLE_BLOCKED`) -- any gap, overlap, or non-terminal code
   (`PENDING_USER_DECISION`, `SEMANTIC_VALIDATION_INCONCLUSIVE`,
   `SEMANTIC_VALIDATION_PROVIDER_ERROR`, `REQUIRES_REPLAN`) is
   `TERMINAL_DISPOSITION_SET_VIOLATION`. There is deliberately no rule that
   every `CvSection` must carry an approved output -- a section may be
   entirely, terminally omitted.
4. Deep-copies every nested value (`approved_content_result`,
   `source_content_plan`, both replay inputs, every approval decision) into
   the payload -- never embeds a caller-held reference a later mutation
   could leak through.
5. Serializes the payload via `canonical_json_bytes`; `PAYLOAD_TOO_LARGE`
   if the result exceeds `PAYLOAD_LIMIT_BYTES`.
6. Recomputes `document_input_fingerprint` via the existing
   `compute_document_input_fingerprint` helper and requires it to match the
   envelope's own field.
7. Computes `approval_decisions_fingerprint` (see below) and
   `fact_selection_identity_fingerprint` (see below); either returning
   `None` (an internal integrity failure, not a `DocumentInputViolation`)
   folds into `DOCUMENT_INPUT_INVALID` with a `diagnostics` entry.
8. Assembles `ApprovedContentPackageFingerprintInput` and hashes it into
   `package_fingerprint`.
9. Returns `BUILT` with a complete package -- every other status carries
   none.

### Approval decisions fingerprint

Never hashed from the raw list of `decision_fingerprint` strings alone.
For every `ProposalApprovalDecision`:

1. Reject a duplicate `proposal_fingerprint` target.
2. Reject a decision whose `semantic_validation_result_fingerprint` does
   not match the current `ApprovedCvContentResult.semantic_validation_result_fingerprint`
   (lineage check).
3. Independently recompute `decision_context_fingerprint` (existing
   `compute_decision_context_fingerprint`) and `decision_fingerprint`
   (existing `compute_proposal_approval_decision_fingerprint`) and require
   an exact match.
4. Order the fully verified decisions by
   `(proposal_fingerprint, decision_fingerprint)`.
5. Hash `{"fingerprint_version": ..., "content": {"decisions": [full
   decision.model_dump(mode="json"), ...]}}`.

### Fact selection identity

Never derived from `dispositions`. The sole source is
`source_content_plan.source_decision_fingerprints`: every entry is
validated as a well-formed SHA-256 hex digest, duplicates are rejected, the
set is sorted, and hashed under `FACT_SELECTION_IDENTITY_FINGERPRINT_VERSION`.

## Repository Protocol (`app/services/cv_approved_content_package_repository_protocol.py`)

`ApprovedContentPackageRepository` (`runtime_checkable`, imports no
SQLAlchemy):

```python
save_package(*, owner_key, package) -> ApprovedContentPackageSaveResult
get_package_by_fingerprint(package_fingerprint) -> ApprovedContentPackageReadResult
read_current_authority(owner_key) -> ApprovedContentAuthorityReadResult
promote_current_authority(*, owner_key, package_fingerprint,
                           expected_previous_slot_version) -> ApprovedContentAuthorityPromotionResult
```

`InMemoryApprovedContentPackageRepository` implements the same contract
with plain dicts behind a `threading.Lock`: defensive copies on every
ingress/egress, `ALREADY_EXISTS_IDENTICAL` for a byte-identical replay,
`STORAGE_METADATA_CONFLICT` for a same-`package_fingerprint`-different-
content collision, `OWNER_NOT_FOUND`/`OWNER_MISMATCH` on save,
`NOT_FOUND` for an absent authority row, slot-version `1` on first
insert / `previous + 1` on update, `ALREADY_CURRENT` when the target
already is current, `STALE_REVISION` on any CAS precondition mismatch, and
`OWNER_PACKAGE_MISMATCH` when the target package belongs to a different
owner. Zero retries anywhere.

## SQL repository (`app/services/cv_approved_content_package_sql_repository.py`)

Never trusts a domain `ApprovedContentPackage` just because it was built by
`cv_approved_content_package_builder` -- every `save_package` and every read
independently re-derives the canonical payload bytes, `payload_sha256`,
`payload_byte_size`, `document_input_fingerprint`,
`approval_decisions_fingerprint`, `fact_selection_identity_fingerprint`,
and the final `package_fingerprint` before trusting anything. A same-PK
collision resolves to `ALREADY_EXISTS_IDENTICAL` (content compared
excluding `created_at`, which is never a fingerprint input) or
`STORAGE_METADATA_CONFLICT`.

**Fresh-session-after-race**: on `IntegrityError` (save) or a
zero-rowcount conditional `UPDATE` (`_ConcurrentSlotMutation`, authority
CAS), the failing `Session` is never reused -- a brand-new `Session` (via
the same `sessionmaker`) performs the re-read that produces the final
result, and the failing `Session` exits and closes without a retried
write.

**Exception channel** (most-specific-first): `IntegrityError` -> re-read
and report the winning row (save) / `STALE_REVISION` (authority CAS);
zero-rowcount CAS `UPDATE` -> `STALE_REVISION`; `OperationalError` ->
`STORAGE_UNAVAILABLE`; `DataError` -> `STORAGE_METADATA_CONFLICT`;
remaining `DatabaseError`/`DBAPIError` -> `STORAGE_UNAVAILABLE`;
`StatementError` (a superclass of `DatabaseError` -- only reached for a
non-`DatabaseError` statement-construction failure) ->
`STORAGE_METADATA_CONFLICT`. A Pydantic `ValidationError`, `TypeError`, or
other programmer error is never caught here and always propagates.

## Canonical JSON

Write: `payload.model_dump(mode="json")` -> `canonical_json_bytes` -> exact
UTF-8 bytes -> exact byte size -> SHA-256.

Read: stored `TEXT` -> UTF-8 bytes -> byte-size compare -> SHA compare ->
`json.loads` -> `ApprovedContentPackagePayload.model_validate` ->
`canonical_json_bytes(parsed.model_dump(mode="json"))` -> exact
stored-bytes comparison -> recompute every fingerprint -> return the
package. A semantically valid but non-canonical stored JSON text (same
parsed value, different bytes) is always `STORAGE_METADATA_CONFLICT` /
`PAYLOAD_NOT_CANONICAL`. No pickle anywhere.

## Exact DDL (`app/services/cv_approved_content_package_schema_manifest.py`)

Two new, additive tables -- `cv_approved_content_packages` (13 columns,
composite `UNIQUE(owner_key_fingerprint, package_fingerprint)`, 10 CHECK
constraints, a single-column FK to `cv_document_artifact_owners`, one
non-unique index on `owner_key_fingerprint`) and
`cv_approved_content_current_authority` (4 columns, a single-column FK to
owners *and* a composite FK
`(owner_key_fingerprint, current_package_fingerprint) ->
cv_approved_content_packages(owner_key_fingerprint, package_fingerprint)`,
one non-unique index on `current_package_fingerprint`) -- with six
triggers:

1. `trg_cv_approved_content_packages_no_update` (`CV_APPROVED_CONTENT_PACKAGE_IMMUTABLE`)
2. `trg_cv_approved_content_packages_no_delete` (`CV_APPROVED_CONTENT_PACKAGE_NO_DELETE`)
3. `trg_cv_approved_content_authority_insert_package_match` (`CV_APPROVED_CONTENT_AUTHORITY_PACKAGE_OWNER_MISMATCH`)
4. `trg_cv_approved_content_authority_update_package_match` (same token)
5. `trg_cv_approved_content_authority_owner_immutable` (`CV_APPROVED_CONTENT_AUTHORITY_OWNER_IMMUTABLE`)
6. `trg_cv_approved_content_authority_slot_version_increment` (`CV_APPROVED_CONTENT_AUTHORITY_SLOT_VERSION_INVALID_INCREMENT`)

The manifest module is hand-written and never imports
`cv_document_models`/`Base.metadata` -- every expected column, CHECK,
FK (including the composite as a single grouped unit, never two
independent single-column FKs), UNIQUE body, index, and trigger body is a
literal Python value, read back exclusively via `sqlite_master`/`PRAGMA`.

## Cutover (`app/services/cv_approved_content_package_cutover_state.py`)

States: `ABSENT` -> `PACKAGE_TABLE_TRIGGERS_PENDING` ->
`AUTHORITY_TABLE_PENDING` -> `AUTHORITY_TRIGGERS_PENDING` -> `READY`, or
`INVALID` (fail-closed, never auto-repaired) at any point a table/FK/CHECK/
index/trigger doesn't match the manifest. `run_cutover_installer` performs
only the next legal step and re-evaluates after each; a `READY` restart
issues zero DDL.

The two ORM classes in `cv_document_models.py`
(`CvApprovedContentPackageRow`/`CvApprovedContentCurrentAuthorityRow`)
exist only so the installer can call `Table.create(engine,
checkfirst=True)` -- they are never the schema's source of truth (the
hand-written manifest is), and both tables are excluded from every earlier
`Base.metadata.create_all` branch in `db_engine.py`. `db_engine.py`'s
`init_models_sync` runs this cutover's preflight/installer strictly after
every prior schema stage (P1/P2/P6-B1/final-DOCX-snapshot/
candidate-identity-binding/final-confirmed-PDF), fail-closed on `INVALID`.

## Reconciliation (`app/services/cv_approved_content_package_reconciliation.py`)

`run_reconciliation(engine)` is read-only: it never issues an `INSERT`,
`UPDATE`, `DELETE`, promotion, or repair. It reads every package row, every
current-authority row, every owner row, and the live schema/indexes/
constraints/triggers of both tables, and reports a closed set of issue
codes (`OWNER_MISSING`, `OWNER_PAYLOAD_MISMATCH`,
`PACKAGE_FINGERPRINT_MISMATCH`, `DOCUMENT_INPUT_FINGERPRINT_MISMATCH`,
`PAYLOAD_SHA256_MISMATCH`, `PAYLOAD_SIZE_MISMATCH`,
`PAYLOAD_NOT_CANONICAL`, `PAYLOAD_RECONSTRUCTION_FAILED`,
`APPROVAL_DECISIONS_FINGERPRINT_MISMATCH`,
`FACT_SELECTION_IDENTITY_FINGERPRINT_MISMATCH`,
`AUTHORITY_MISSING_PACKAGE`, `AUTHORITY_OWNER_MISMATCH`,
`INVALID_SLOT_VERSION`, `IMMUTABILITY_TRIGGER_MISSING`,
`AUTHORITY_TRIGGER_MISSING`, `SCHEMA_MANIFEST_MISMATCH`,
`UNSUPPORTED_PACKAGE_SCHEMA_VERSION`,
`UNSUPPORTED_PACKAGE_POLICY_VERSION`). There is no separate blob store for
this line (the payload is stored inline as `payload_json`), so there is
deliberately no orphan-blob-style issue code.

## Out of scope

Not implemented by this stage: the `ApprovedContentDocumentInput` resolver,
the POST Proposal API, router changes, the frontend, PRE-P4 -> P5.5
production orchestration, Proposal DOCX generation, the working-copy flow,
`FinalDocxSnapshot`, the PDF converter, and final PDF authority.
