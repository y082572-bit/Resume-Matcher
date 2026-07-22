# Explicit Provenance Stage P5a: Deterministic Approved CV Content Generation

## Purpose and boundary

Stage P5a turns an already `p5_ready` Stage P4 `CvContentPlan` plus a
caller-supplied snapshot of approved fact payloads into an explicit,
replayable `CvGeneratedContentDraft`. It is the layer that says *what exact
text goes into each already-planned CV slot* — nothing more.

P5a is a pure, in-memory Python library. It performs:

- mandatory re-validation of the P4 plan (structural + freshness + conflicts),
  never trusting a caller-supplied boolean or `plan.plan_status`,
- validation of the entire `ApprovedFactPayloadSet` snapshot before rendering
  anything,
- per-`PlannedFactUse` payload identity matching (never a text lookup),
- deterministic rendering for exactly three `TransformationOperation` values
  (`EXACT_COPY`, `FORMAT_NORMALIZATION`, `REORDER`),
- a closed result partition: every `PlannedFactUse` becomes exactly one
  generated element or one blocked item, never both, never neither,
- content-derived SHA-256 fingerprints at every level,
- pure replay / stale-result detection.

### What P5a explicitly does not do

- select new facts, or change which fact a `PlannedFactUse` refers to,
- change the P4 result partition (pending/omitted/conflict membership),
- read `TruthFact` or `TruthPermission` from the database,
- run SQL,
- read or modify the Master Resume,
- import the legacy `CVTransformationPlan` or `cv_transformation_generation`,
- use `source_reference`, a legacy record key, or a list index as identity,
- look up a fact by matching its text,
- invoke an LLM, apply `CONTROLLED_REPHRASE`/`FLATTEN_FOR_LOWER_ROLE`/
  `ELEVATE_PRESENTATION_FOR_SENIOR_ROLE`/`COMBINE_APPROVED_FACTS`/
  `SPLIT_APPROVED_FACT`/`SUMMARIZE_APPROVED_FACTS`, or render `OMIT` as
  content,
- build a DOCX or PDF,
- touch the frontend,
- start Stage P5b or P5.5.

These boundaries are enforced both by construction (P5a's source files never
import `app.database`, SQLAlchemy, `app.models`, `truth_legacy_migrator`,
`cv_transformation_plan`/`_approval`/`_generation`, `docx`, Playwright, or
LiteLLM) and by
`tests/integration/test_p3_p4_p5_integration.py`, which greps the five P5a
source files for exactly those forbidden tokens (after stripping
docstrings, so the scope-boundary prose you are reading now cannot produce
a false pass).

## Inputs

`generate_cv_content_draft()` takes only explicit, caller-supplied data:

| Parameter | Meaning |
|---|---|
| `plan` | The `CvContentPlan` P4 built. Always re-validated, never trusted as-is. |
| `decisions_by_fingerprint` | The same P3 decisions P4 was built from, keyed by `decision_fingerprint` — needed to re-run `validate_cv_content_plan`. |
| `entity_types` | The same `entity_id -> EntityType` snapshot P4 used. |
| `current_replay_input` | A `CvContentPlanReplayInput` computed by the caller *right now*. `None` means freshness cannot be verified, which fails the P4 gate (never assumed `FRESH`). |
| `fact_payloads` | An `ApprovedFactPayloadSet` — the approved `value_json`/`normalized_value_json` for every fact P5a might render. |
| `generation_context` | A `GenerationContext` — only the data that can influence a deterministic renderer. |

## Mandatory P4 re-validation

`generate_cv_content_draft` always calls `validate_cv_content_plan` itself
before doing anything else. It never reads `plan.plan_status`, a caller
boolean, or a previously stored `p5_ready` value. Generation proceeds only
when the validator reports:

```
plan.plan_status == VALID
structural_status == VALID
freshness_status == FRESH
p5_ready == True
conflicts empty
```

Otherwise the result is `GenerationStatus.BLOCKED` with `draft=None` and zero
generated elements, carrying one `CvContentGenerationViolation` classified
(in this priority order) as:

1. `INPUT_PLAN_STRUCTURALLY_INVALID` — `structural_status != VALID`,
2. `INPUT_PLAN_HAS_CONFLICTS` — `plan.conflicts` is non-empty,
3. `INPUT_PLAN_STALE` — `freshness_status == STALE`,
4. `INPUT_PLAN_NOT_READY` — any other reason `p5_ready` is `False` (e.g. a
   `REQUIRES_REVIEW` plan status, or `current_replay_input=None` leaving
   freshness `FRESHNESS_NOT_VERIFIED`).

## Approved fact payload snapshot

`ApprovedFactPayloadSnapshot` carries, for one fact, only:
`person_entity_id`, `entity_id`, `fact_id`, `fact_type`, `fact_revision`,
`fact_content_fingerprint`, `value_json`, `normalized_value_json`, and its
own `payload_fingerprint`. It has no `source_reference` and no legacy record
key field at all.

`ApprovedFactPayloadSet` wraps a tuple of these plus a
`payload_set_fingerprint` computed by
`compute_fact_payload_set_fingerprint()` — a sorted set of member
`payload_fingerprint`s, so payload order never changes the set's
fingerprint.

Before rendering anything, the builder validates the *whole* snapshot and
fails closed to `GenerationStatus.INVALID_INPUT` (`draft=None`, zero
generated elements) for any of:

- `PAYLOAD_SET_FINGERPRINT_MISMATCH` — the set's stated fingerprint doesn't
  match its recomputed value,
- `PAYLOAD_DUPLICATE_FACT_ID` — the same `fact_id` appears twice,
- `PAYLOAD_DUPLICATE_FINGERPRINT` — the same `payload_fingerprint` appears
  twice,
- `PAYLOAD_EXTRA_FACT` — a payload's `fact_id` matches no `PlannedFactUse`
  in the plan.

Every other identity or shape problem is scoped to the single affected
`PlannedFactUse` and becomes a `BlockedGenerationItem`, never a global
failure:

- `PAYLOAD_MISSING` — no payload exists for this `fact_id`,
- `PAYLOAD_PERSON_MISMATCH` / `PAYLOAD_ENTITY_MISMATCH` /
  `PAYLOAD_FACT_TYPE_MISMATCH` / `PAYLOAD_REVISION_MISMATCH` /
  `PAYLOAD_CONTENT_FINGERPRINT_MISMATCH` — the matched payload's identity
  fields disagree with the `PlannedFactUse` (checked in that order, first
  mismatch wins),
- `PAYLOAD_FINGERPRINT_MISMATCH` — this specific payload's own
  `payload_fingerprint` doesn't match its recomputed value,
- `PAYLOAD_SHAPE_NOT_ALLOWED` / `PAYLOAD_REQUIRED_FIELD_MISSING` — the
  payload contract for its `fact_type` rejects `value_json` (see below).

A blocked use never causes a different payload to be substituted, and never
triggers a text-similarity lookup.

## Payload contracts (`cv_content_generation_policy.py`)

Every fact_type's payload shape is a plain, closed, versioned table — never
`startswith`, substring matching, or a fallback shape:

- **Narrative scalar/text** (`SUMMARY`, `SKILL`, `TOOL`, `TECHNOLOGY`,
  `COMPANY`, `ROLE`, `EMPLOYMENT_PERIOD`, `RESPONSIBILITY`,
  `EMPLOYMENT_ACTIVITY`, `EMPLOYMENT_RESPONSIBILITY_SCALE`, `ACHIEVEMENT`,
  `EMPLOYMENT_NUMERIC_RESULT`): `value_json` must be a non-blank string, or
  an object with a non-blank string `text` key. Never both at once.
- **`EMPLOYMENT_ROLE`**: required `firma`, `stanowisko`; optional
  `stanowiskoAlt`, `okresOd`, `okresDo`, `legacy_source_label`.
- **`EDUCATION_DEGREE`**: required `kierunek`; optional `uczelnia`,
  `stopien`, `legacy_source_label`.
- **`CERTIFICATION_NAME`**: required `nazwa`.
- **`COURSE_NAME`**: required `nazwa`; optional `organizator`.
- **`LANGUAGE_NAME`**: required `jezyk`; optional `poziom`.

Unknown extra keys on a structured payload are tolerated (they may remain
part of the source payload fingerprint) but are never read, rendered, or
used to order content. A missing required key is
`PAYLOAD_REQUIRED_FIELD_MISSING`; any other invalid shape (wrong type, not
an object, blank scalar) is `PAYLOAD_SHAPE_NOT_ALLOWED`. A fact_type outside
this table — including the out-of-scope `AWARD_NAME` — fails closed to
`PAYLOAD_SHAPE_NOT_ALLOWED` with no default contract.

## Supported operations

P5a renders exactly three `TransformationOperation` values:
`EXACT_COPY`, `FORMAT_NORMALIZATION`, `REORDER` (`REORDER`'s renderer is
identical to `EXACT_COPY` — the ordering itself came from P4's
`placement_order`, which P5a never changes).

Every other operation is unsupported and produces a `BlockedGenerationItem`:
`CONTROLLED_REPHRASE`, `FLATTEN_FOR_LOWER_ROLE`,
`ELEVATE_PRESENTATION_FOR_SENIOR_ROLE`, `COMBINE_APPROVED_FACTS`,
`SPLIT_APPROVED_FACT`, `SUMMARIZE_APPROVED_FACTS` all become
`OPERATION_NOT_SUPPORTED_IN_P5A`; `OMIT` becomes its own
`OMIT_NOT_GENERATABLE` — P5a never fabricates an empty `GeneratedCvElement`
to represent an omission.

## Deterministic renderers (`cv_content_generation_renderer.py`)

`EXACT_COPY`/`REORDER` return the exact source text (or the exact composed
structured text) with no whitespace or case change. `FORMAT_NORMALIZATION`
applies only Unicode NFKC and whitespace-run collapsing — nothing else, so
Polish diacritics, digits, percentages, currency symbols, dates, proper
names, and negations are always preserved unchanged.

Structured composition reads named keys only, in a fixed order, never
`str(dict)`/`repr(dict)`/`json.dumps` for display, and never
`dict.items()` order:

- `EMPLOYMENT_ROLE`: `"{stanowisko}, {firma}"`, optionally followed by
  `" | {okresOd} - {okresDo}"` (or just one side if only one bound is
  present) — a missing date is never invented and "obecnie"/"present" is
  never inserted without a source value. `stanowiskoAlt` is never
  auto-appended.
- `EDUCATION_DEGREE`: `", ".join([stopien, kierunek, uczelnia])`, skipping
  missing optional fields.
- `CERTIFICATION_NAME`: `nazwa`.
- `COURSE_NAME`: `nazwa`, optionally `", {organizator}"`.
- `LANGUAGE_NAME`: `jezyk`, optionally `" - {poziom}"`.

## Result partition

Every `planned_fact_use_fingerprint` in the plan belongs to exactly one of
two buckets: a `GeneratedCvElement` or a `BlockedGenerationItem`. P5a has no
proposal bucket. `PendingFactApproval`, `OmittedFactDecision`, and
`CvContentPlanConflict` from P4 generate no content and never appear in
draft sections — their fingerprints and counts remain visible on
`CvContentGenerationResult` as a source summary
(`pending_approval_fingerprints`, `omitted_fact_fingerprints`,
`conflict_fingerprints`).

## Provenance

Every `GeneratedCvElement` carries, among other fields:
`generated_element_fingerprint`, `content_plan_fingerprint`,
`section_plan_fingerprint`, `planned_fact_use_fingerprint`,
`fact_selection_decision_fingerprint`, the fact's stable identity fields,
`payload_fingerprint`, `target_section`/`target_scope`/`allowed_operation`/
`placement_order`/`employment_scope_entity_id` (mirrored unchanged from the
source `PlannedFactUse`), `generation_mode=DETERMINISTIC`, `generated_text`,
`approval_state=NOT_REQUIRED`, and a nested `GenerationProvenance`
(`generation_schema_version`, `generation_policy_version`,
`deterministic_template_version`, `generation_context_fingerprint`,
`payload_set_fingerprint`, `payload_fingerprint`, `renderer_contract_version`,
`renderer_name`). No `generated_element_id` is ever random.

## Structural mirror of the P4 plan

`CvGeneratedContentDraft` preserves the plan's structure exactly: one
generated section per `CvSection`, in the same order; `CvExperienceEntryPlan`
→ `CvGeneratedExperienceEntry` with the same `employment_entity_id`,
`entry_order`, `entry_order_source`, and header/responsibility/achievement
grouping. P5a never moves an element between sections or employment
entries, never changes `placement_order`, never creates a new employment
entry, never merges or splits a `fact_id`, and never re-sorts by
`generated_text`. Blocked items never appear in `generated_sections`; a
section may be empty if every one of its uses was blocked.

## Fingerprints

All computed in `cv_content_generation_builder.py` as `hashlib.sha256` over
`truth_fingerprint.canonical_json_bytes`, lowercase hex, with explicitly
sorted collections where order is not semantically meaningful, no
`uuid4()`, no `datetime.now()`, and no `source_reference`/legacy identity
folded in:

`compute_fact_payload_fingerprint`, `compute_fact_payload_set_fingerprint`,
`compute_generation_context_fingerprint`,
`compute_generation_provenance_fingerprint`,
`compute_generated_element_fingerprint`,
`compute_generated_section_fingerprint`,
`compute_generated_experience_entry_fingerprint`,
`compute_generated_content_draft_fingerprint`,
`compute_generation_result_fingerprint`, plus
`compute_blocked_generation_item_fingerprint` for a blocked item's own
identity. Changing a payload's content, the plan, the generation policy
version, or the deterministic template version changes the corresponding
element/draft/result fingerprint.

## Replay (`cv_content_generation_replay.py`)

`replay_input_from_generation_result()` extracts the staleness-relevant
fields of a previously computed result into a `CvContentGenerationReplayInput`
(plan fingerprint/schema/policy versions, generation schema/policy versions,
payload set fingerprint, sorted planned-fact-use fingerprints, generation
context fingerprint, deterministic template version, sorted generated
element fingerprints, sorted blocked item fingerprints).
`evaluate_generation_freshness(previous, current)` classifies:

- `current=None` → `FRESHNESS_NOT_VERIFIED`,
- `previous == current` → `FRESH`,
- otherwise → `STALE`, with `stale_fields()` reporting exactly which fields
  differ (deterministic, sorted).

No replay function reads a clock or performs a lookup.

## Status and `ready_for_p55`

- `GenerationStatus.GENERATED` — the plan was ready, every `PlannedFactUse`
  generated, zero blocked items, a draft exists.
- `GenerationStatus.BLOCKED` — either the P4 gate failed (`draft=None`) or
  at least one payload-level use was blocked (`draft` may still contain the
  successfully generated elements). Never ready for the next stage.
- `GenerationStatus.INVALID_INPUT` — the `ApprovedFactPayloadSet` snapshot
  itself failed a set-level check. `draft=None`, zero generated elements.

`ready_for_p55` is `True` only when `status == GENERATED`, a draft exists,
`blocked_items` is empty, and no fatal violation was recorded. P5a never
sets readiness for Stage P6.

## Stage 10C / DB / Master Resume / DOCX / PDF boundary

P5a is a clean-room implementation: it does not import or reuse the legacy
`CVTransformationPlan`, `cv_transformation_plan`, `cv_transformation_approval`,
`cv_transformation_generation`, or `truth_legacy_migrator` modules. It
performs no database I/O (no `app.database`, no SQLAlchemy, no
`app.models`), reads and writes nothing to the Master Resume, and produces
no DOCX or PDF output. These are enforced by
`tests/integration/test_p3_p4_p5_integration.py`'s source-scan tests.

## `AWARD` boundary

`AWARD` remains entirely out of scope for P5a: there is no `AWARD_NAME`
payload contract, no `CvSection.AWARDS`, no fallback to `ACHIEVEMENTS`, and
no renderer. Any unknown fact_type — including `AWARD_NAME` — fails closed
to `PAYLOAD_SHAPE_NOT_ALLOWED`.

## What comes after P5a

Stage P5b/P5.5 (not started here) would consume a `GENERATED`,
`ready_for_p55` result to assemble a full CV document. P5a itself never
reaches that boundary: it has no concept of a finished document, a
template layout, or an export format.
