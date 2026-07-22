# Explicit Provenance Stage P4: Deterministic CV Content Plan

## Purpose and boundary

Stage P4 turns an existing set of Stage P3 `FactSelectionDecision` records
into an explicit, deterministic, replayable `CvContentPlan`. It is the layer
that says *which approved fact goes into which CV section, in which order,
grouped under which employment entry* — nothing more.

P4 is a pure, in-memory Python library. It performs:

- fact-to-section assignment, via a closed `(fact_type, target_scope)` →
  `CvSection` mapping table,
- employment grouping, by the stable employment `entity_id` /
  `employment_scope_entity_id` UUID only,
- deterministic ordering of sections, employment entries, and facts within
  a bucket,
- tracking of pending approvals, structural omissions, and decision
  conflicts,
- content-derived SHA-256 fingerprints at every level,
- pure replay / stale-plan detection,
- fail-closed structural and freshness validation.

### What P4 explicitly does not do

- create a new `TruthFact` or `TruthEntity`,
- read `TruthFact`, `TruthEntity`, or `TruthPermission` from the database,
- call `select_fact()` or otherwise run Stage P3 decision logic,
- change a P3 decision's `outcome`,
- write to SQL,
- generate CV text, run an LLM rephrase, or apply a
  `TransformationOperation`,
- build a DOCX or PDF,
- modify the legacy `CVTransformationPlan` or the Master Resume,
- touch the frontend,
- start Stage P5.

These boundaries are enforced both by construction (P4's source files never
import `app.database`, `app.services.truth_repository`, LiteLLM, `docx`, or
Playwright) and by
`tests/integration/test_cv_content_plan_integration.py`, which greps the
five P4 source files for exactly those forbidden tokens.

## Inputs

`build_cv_content_plan()` takes only explicit, caller-supplied data:

| Parameter | Meaning |
|---|---|
| `target_context` | The same `TargetContext` P3 was given. |
| `decisions` | A sequence of `FactSelectionDecision` already produced by `select_fact()`. |
| `entity_types` | An explicit snapshot: `entity_id -> EntityType`. Never looked up from a database. |
| `employment_entry_order` | Optional explicit `employment_entity_id -> int` ordering hint. |
| `budget_profile` | Optional `CvContentBudgetProfile` (per-section item ceilings). |
| `career_positioning_snapshot_fingerprint` | Optional opaque fingerprint carried onto the plan, never interpreted. |

`entity_types` must contain an entry for every `decision.entity_id`, and for
every `decision.employment_scope_entity_id` that a responsibility/achievement
fact_type actually uses for grouping. A missing entry is a fail-closed
`INVALID_INPUT_SNAPSHOT` (`plan=None`), never a database lookup — P4 has no
database access to fall back on.

## No database I/O, no text generation

`build_cv_content_plan`, `validate_cv_content_plan`, and the replay helpers
never touch a clock, a database session, or an LLM. `PlannedFactUse` never
stores `generated_text`, `bullet`, `narrative`, `summary_text`,
`company_name`, `role_name`, `source_reference`, a legacy record key, or a
list index — only the decision-derived *placement* of an existing fact.

## Models

Defined in `app/schemas/cv_content_plan.py`, all closed (`extra="forbid"`):

- `CvContentPlan` — the top-level, replayable output.
- `CvExperienceSectionPlan` / `CvFlatSectionPlan` — a discriminated union
  (`kind: Literal["EXPERIENCE" | "FLAT"]`) so an EXPERIENCE section can never
  accidentally carry a flat `planned_fact_uses` list, and vice versa.
- `CvExperienceEntryPlan` — one employment entry, keyed only by its stable
  `employment_entity_id`.
- `PlannedFactUse` — one fact placed into one slot.
- `PendingFactApproval` — a P3 `APPROVAL_REQUIRED` decision, carried forward
  without placement.
- `OmittedFactDecision` — a decision structurally excluded by P4 policy.
- `CvContentPlanConflict` — two or more decisions P4 cannot resolve alone.
- `CvContentBudgetProfile` / `SectionBudgetLimit` — optional per-section
  ceilings.
- `CvContentPlanBuildResult` — `(outcome, plan, snapshot_violations)`.
- `CvContentPlanReplayInput` — the exact fields whose change makes a plan
  stale.
- `CvContentPlanValidationResult` / `CvContentPlanViolation` — validator
  output.

`CV_CONTENT_PLAN_SCHEMA_VERSION = "cv-content-plan-schema-v1"` and
`CV_CONTENT_POLICY_VERSION = "cv-content-policy-v1"` are P4's own versions,
distinct from `FACT_SELECTION_SCHEMA_VERSION` / `SELECTION_POLICY_VERSION`.
Both participate in the plan's fingerprints.

## Section mapping

`app/services/cv_content_plan_policy.py::FACT_TYPE_SECTION_MAP` is a closed
dict keyed by `(fact_type, requested_target_scope)`. There is no
`startswith`/substring matching and no default/fallback section: a
combination outside the table — including `SKILL`@`SUMMARY`, or any
`EDUCATION`/`CERTIFICATION`/`COURSE`/`LANGUAGE`/`AWARD` fact_type — is
`SECTION_NOT_ALLOWED_FOR_FACT_TYPE`, always.

`SECTION_ORDER` is an explicit, versioned tuple. `CvContentPlan.sections`
always contains exactly one entry per `CvSection`, in that order, even when
`skipped_empty=True`.

## Result partition

Every deduplicated input decision fingerprint lands in **exactly one** of
four buckets: `planned` (a `PlannedFactUse`), `pending`
(`PendingFactApproval`), `omitted` (`OmittedFactDecision`), or `conflict`
(a `CvContentPlanConflict` member). The union of all four equals
`plan.source_decision_fingerprints`; the four sets are pairwise disjoint.
An identical `decision_fingerprint` supplied more than once is deduplicated
before partitioning, never treated as a conflict.

`SelectionDecisionOutcome` → bucket is fixed: `APPROVAL_REQUIRED` is always
pending; `BLOCKED`/`EXCLUDED`/`NOT_RELEVANT` are always omitted with the
matching reason code; `SELECTED` can end up planned, a conflict member, or
omitted for a P4-only structural reason
(`SECTION_NOT_ALLOWED_FOR_FACT_TYPE`, `EMPLOYMENT_SCOPE_REQUIRED`, or
`BUDGET_NOT_AVAILABLE`). P4 never changes a decision's `outcome`.

## Employment grouping

A header fact_type (`EMPLOYMENT_ROLE`/`COMPANY`/`ROLE`/`EMPLOYMENT_PERIOD`)
is grouped into a `CvExperienceEntryPlan` only when
`entity_types[decision.entity_id] == EntityType.EMPLOYMENT`, keyed by
`decision.entity_id`.

A responsibility/achievement fact_type
(`EMPLOYMENT_ACTIVITY`/`EMPLOYMENT_RESPONSIBILITY_SCALE`/`RESPONSIBILITY`/
`EMPLOYMENT_NUMERIC_RESULT`/`ACHIEVEMENT`@`EXPERIENCE`) is grouped only when
`decision.employment_scope_entity_id` is set and
`entity_types[employment_scope_entity_id] == EntityType.EMPLOYMENT`, keyed
by `employment_scope_entity_id`. `ACHIEVEMENT`@`PROJECT` never groups into
an experience entry; it can only land in the flat `ACHIEVEMENTS` section.

Placement is never inferred from company name, role name, list order, or
`source_reference` — only from the stable UUID. A missing or
non-`EMPLOYMENT` identity fails closed to an `OmittedFactDecision` with
`EMPLOYMENT_SCOPE_REQUIRED`.

## PROFILE policy

`PROFILE` holds at most one `PlannedFactUse`, and only for
`fact_type == "SUMMARY"` with `target_scope == "SUMMARY"`.
`SKILL`@`SUMMARY` is rejected (`SECTION_NOT_ALLOWED_FOR_FACT_TYPE`), never
routed to `PROFILE`. When more than one distinct `fact_id` is independently
`SELECTED` as a `SUMMARY`, P4 never picks one arbitrarily: it raises a
`PROFILE_MULTIPLE_SUMMARY_CANDIDATES` conflict, `PROFILE` stays
`skipped_empty=True`, and `plan_status` becomes `INVALID`. No SUMMARY at all
is a normal, non-error `skipped_empty=True` PROFILE — never a pending
approval, and never a `COMBINE_APPROVED_FACTS` operation (P4 never invents
or requests a transformation).

## Duplicate/conflict policy

If more than one distinct `SELECTED` decision shares the same `fact_id`
(regardless of `target_scope`), that is always a single
`CONFLICTING_SELECTED_DECISIONS_FOR_SAME_FACT` conflict: every member
decision fingerprint becomes a conflict member — none of them becomes
planned, pending, or omitted — and `plan_status` becomes `INVALID`.

## CPE boundary

P4 never re-ranks, re-scores, or overrides a Career Positioning Engine
decision: a P3 `NOT_RELEVANT` decision (already resolved by P3's own
CPE-advisory step) is simply carried through to an
`OmittedFactDecision(DECISION_NOT_RELEVANT)`. P4 only ever copies
`advisory_flags` onto a `PlannedFactUse` and carries
`career_positioning_snapshot_fingerprint` opaquely onto the plan. It never
imports `CareerPositioningResponse`, never runs an LLM ranking, never
computes an `evidence_weight`, and never changes a `target_scope`.

## Deterministic sorting

Every ordering key is built only from: `SECTION_ORDER`, an explicit
`employment_entry_order` (or a UUID-string fallback with **no** business
meaning), a closed, versioned `FACT_TYPE_PRIORITY` dict, `entity_id`,
`fact_id`, and `decision_fingerprint`. Never `Transferability`, reason
codes, advisory flags, free text, or an LLM.

`entry_order_source` records, per employment entry, whether its position
came from the caller's `employment_entry_order` (`EXPLICIT_INPUT`) or from
sorting the bare UUID string (`UUID_FALLBACK`). Explicit entries are ordered
first (by their given integer), fallback entries after (by UUID string) —
the fallback ordering itself carries no semantic weight and must not be
treated as a business ranking.

## Budgets

`CvContentBudgetProfile` is entirely optional; P4 defines no default limits.
A section with no configured limit is always `NOT_EVALUATED`. A configured
`max_facts == 0` disables the section: every item that would have gone
there instead becomes an `OmittedFactDecision(BUDGET_NOT_AVAILABLE)`, and
the section reports `WITHIN_BUDGET` (0 items ≤ 0). A configured limit that
is exceeded by actual item count **never trims**: every item stays,
`SectionBudgetStatus.EXCEEDS_BUDGET` is reported, and `plan_status` becomes
at least `REQUIRES_REVIEW` (a conflict still takes priority and forces
`INVALID`).

## Fingerprints

Every fingerprint is `hashlib.sha256` over
`truth_fingerprint.canonical_json_bytes`, lowercase hex — no `uuid4()`, no
`datetime.now()`, no `source_reference`, no list index. `compute_*`
functions live in `app/services/cv_content_plan_builder.py`:
`compute_planned_fact_use_fingerprint`,
`compute_experience_entry_fingerprint`,
`compute_section_plan_fingerprint`, `compute_pending_approval_fingerprint`,
`compute_omission_fingerprint`, `compute_conflict_fingerprint`,
`compute_content_plan_fingerprint`, and
`compute_budget_profile_fingerprint`. `content_plan_fingerprint` folds in
the schema/policy/selection/transformation versions, the target context and
budget profile fingerprints, and every sorted collection of section /
pending / omission / conflict fingerprints plus the sorted set of source
decision fingerprints.

## Replay and freshness

`app/services/cv_content_plan_replay.py` mirrors the P3 replay contract:
`replay_input_from_plan()` extracts a `CvContentPlanReplayInput` purely from
the plan's own stored `PlannedFactUse` data (the only bucket carrying
`fact_revision`/`fact_content_fingerprint`/`permission_snapshot_fingerprint`
directly) plus the plan-level policy versions and the full set of source
decision fingerprints (which alone still covers pending/omitted facts at
decision granularity). `is_cv_content_plan_stale()` and `stale_fields()` are
pure equality comparisons — no clock, no lookup.

### Employment entry order is an explicit, separately tracked replay input

`employment_entry_order` (the caller-supplied `employment_entity_id -> int`
ordering hint passed to `build_cv_content_plan`) is an external, mutable
input: the caller can change it between builds without changing any
`FactSelectionDecision`. The *resulting* order is materialized on the plan
itself, once per employment entry, as
`CvExperienceEntryPlan.entry_order` / `entry_order_source` — P4 never
re-reads the caller's original mapping.

`CvContentPlanReplayInput.employment_entry_orders` is a snapshot of exactly
that materialized result: a tuple of
`(employment_entity_id, entry_order, entry_order_source)`, one triple per
`CvExperienceEntryPlan` in the plan, sorted deterministically by
`str(employment_entity_id)` (never by tuple/section iteration order).
`replay_input_from_plan()` builds it by walking `CvExperienceSectionPlan.experience_entries`
directly — no lookup, no re-derivation from a caller-supplied
`employment_entry_order` object. A plan with no experience entries yields an
empty tuple.

Because `stale_fields()` compares every field on `CvContentPlanReplayInput`
generically, a change to any element of `employment_entry_orders` — an
added or removed entry, a changed `employment_entity_id`, a changed
`entry_order`, or a changed `entry_order_source` (including an
`EXPLICIT_INPUT` ⇄ `UUID_FALLBACK` transition) — is reported as
`stale_fields == ("employment_entry_orders",)` and flips
`freshness_status` to `STALE` (and therefore `p5_ready` to `False`), even
when every fact-level field is unchanged. `UUID_FALLBACK` positions are
still tracked and still participate in staleness detection like any other
element, even though (per **Deterministic sorting** above) the fallback
ordering itself carries no business meaning.

This is a replay-input addition only: `experience_entry_fingerprint` already
folds in `employment_entity_id`/`entry_order`/`entry_order_source` (see
**Fingerprints** above), so `content_plan_fingerprint` already changes
whenever the built order changes. `employment_entry_orders` closes a
different gap — it makes that order change visible to staleness detection
*before* a plan is rebuilt, by giving the caller's *current* employment
ordering something explicit to be compared against.

### EntityType is immutable and out of scope for this snapshot

`entity_types` (the `entity_id -> EntityType` snapshot passed to
`build_cv_content_plan`) has no separate replay snapshot and does not need
one: the current truth-layer contract makes `EntityType` immutable after
creation — `TruthEntityUpdate` carries no `entity_type` field, and
`TruthRepository.update_entity` accepts only a `TruthEntityUpdate`. There is
no code path that can change an entity's type in place, so unlike
employment order there is nothing for a replay snapshot to detect changing.

## Structural validation

`app/services/cv_content_plan_validator.py::validate_cv_content_plan()` is
fail-closed: it recomputes every fingerprint from the plan's own stored data
plus the caller-supplied `decisions_by_fingerprint`, and reports every
mismatch as a `CvContentPlanViolation`. `structural_status` is `INVALID` if
any violation is found, except `SECTION_BUDGET_EXCEEDED`, which is
informational only (the budget-exceeded condition is already reflected in
`plan.plan_status`).

`freshness_status` is `FRESHNESS_NOT_VERIFIED` when no
`current_replay_input` is supplied, `FRESH`/`STALE` otherwise, by comparing
against a freshly recomputed `replay_input_from_plan(plan)`.

`p5_ready` is `True` only when **all** of: `plan.plan_status == VALID`,
`structural_status == VALID`, `freshness_status == FRESH`, and
`plan.conflicts` is empty. P4 never starts Stage P5 itself — `p5_ready`
is only a signal for a future caller.

## Unsupported sections (by design, for now)

`EDUCATION`, `CERTIFICATIONS`, and `LANGUAGES` — and the corresponding
`EDUCATION`/`CERTIFICATION`/`COURSE`/`LANGUAGE`/`AWARD` fact_types — have no
P4 policy yet. They stay `skipped_empty=True` with no lookup and no
`FACT_TYPE_POLICY_NOT_FOUND`-style workaround. A complete CV requires a
separate P1/P3 extension stage (new `TransformationPolicy` entries plus new
P3 gate coverage) before P5 can rely on those sections being populated. This
does not block P4 itself, which is deliberately scoped to the fact_types P1
already recognizes.

## No new table, no router, no feature flag, no legacy coupling

P4 adds no SQLAlchemy model, no Alembic migration, no FastAPI router, and no
feature flag. It has no dependency on `app.services.truth_legacy_migrator`,
`app.services.truth_repository`, or the legacy `CVTransformationPlan` —
enforced both by never importing them and by a source-grep integration
test.

## Boundary with P5

P4 stops at a validated, replayable `CvContentPlan`. It never generates CV
text, never invokes an LLM rephrase, never builds a DOCX/PDF, and never
writes to the Master Resume. A future Stage P5 is expected to consume a
`p5_ready == True` plan and perform content generation from it — that stage
is explicitly out of scope here and is not started by this change.
