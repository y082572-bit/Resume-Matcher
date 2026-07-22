# Explicit Provenance -- Stage P3: Deterministic Fact Selection

Stage P3 adds a pure, deterministic **decision layer** over the existing
Explicit Provenance Truth model (P1) and the legacy migration bridge (P2).
It answers one question, repeatably: *given this existing fact, this
existing set of permissions, and this explicit target context, what is the
fact currently allowed to be used for?*

## What P3 is not

- P3 does not create facts. It only reads `TruthFactRead` / `TruthPermissionRead`
  records the caller already obtained from `TruthRepository`.
- P3 does not create a second Truth Library or a second Career Positioning
  Engine.
- P3 does not add a SQL table, an ORM model, a migration, a router, or any
  persistent decision log. A `FactSelectionDecision` is a plain, in-memory
  Pydantic value -- nothing about producing one writes to the database.
- P3 does not re-approve facts already `CONFIRMED`. A `CONFIRMED`,
  `use_in_cv=true`, `requires_approval=false` fact requested for
  `EXACT_COPY` is `SELECTED` without any additional human step.
- P3 does not read legacy Truth Library JSON, `legacy_record_key`,
  `legacy_source_path`/`legacy_source_id`, or any list index. P2 remains the
  only legacy bridge.

Source of truth remains: `TruthEntity`, `TruthFact`, `TruthPermission`,
`TruthTransformationPolicyRegistry`, `HardSafetyPolicy`, and the Career
Positioning Engine (CPE). P3 adds no new source of truth -- only a
decision function over the existing ones.

## Outcomes

`SelectionDecisionOutcome` is a closed set of five values:

| Outcome            | Meaning                                                                 |
|---------------------|--------------------------------------------------------------------------|
| `SELECTED`          | The fact may be used for the requested operation right now.             |
| `APPROVAL_REQUIRED` | The fact is true / potentially useful, but this context or operation needs explicit approval. |
| `BLOCKED`           | Status, transferability, or hard safety makes this use impossible.       |
| `EXCLUDED`          | The owner deliberately opted the fact out of CV use (`use_in_cv=false`). |
| `NOT_RELEVANT`      | The fact is allowed, but the Career Positioning Engine flagged it (by stable `fact_id`) as not matching this explicit target. |

`NOT_RELEVANT` is never a substitute for `EXCLUDED`: `use_in_cv=false` is
always `EXCLUDED`, never `NOT_RELEVANT`.

## Reason codes

`FactSelectionReasonCode` is a closed, versioned enum. Every decision
carries a non-empty, deterministically sorted tuple of the reasons that
produced it (`reason_codes`), plus a separate, deterministically sorted
tuple of non-decisive `advisory_flags` contributed only by the CPE signal.

## Pipeline

`select_fact()` runs five gates in a fixed order. Each gate can either
return a terminal decision (short-circuiting the rest) or pass through.
`TruthTransformationPolicyRegistry` (P1) is the sole, superordinate
authority on which `target_scope` and `requested_operation` a `fact_type`
may ever use. Nothing in P3 -- neither the base-operation shortcut nor an
active `TruthPermission` -- is a global default-allow, and neither can
widen what P1 already denies:

1. **Eligibility** (`evaluate_eligibility`) -- derived only from
   `TruthFact.status`, `use_in_cv`, `requires_approval`:
   - `DRAFT` / `REJECTED` / `ARCHIVED` -> `BLOCKED` (hard status blocks,
     checked first -- an unconfirmed or terminally-negative fact can never
     be softened into "just needs approval").
   - `REVIEW_REQUIRED` status, or `requires_approval=true` -> `APPROVAL_REQUIRED`.
   - `CONFIRMED` + `use_in_cv=true` + `requires_approval=false` -> candidate,
     continues to the transferability gate.
   - `use_in_cv=false` -> `EXCLUDED`.
   - Anything else (including a future/unknown status value) -> `BLOCKED`,
     fail-closed.
2. **Transferability** (`evaluate_transferability`) -- derived only from
   `TruthFact.transferability`, `TruthFact.employment_scope_entity_id`, and
   `TargetContext`:
   - `NON_TRANSFERABLE`: allowed only when the fact's `entity_id` equals
     `TargetContext.person_entity_id` or `.employment_entity_id`; otherwise
     `BLOCKED`. No permission can widen this.
   - `EMPLOYMENT_SCOPED`: requires `employment_scope_entity_id` to be set
     and equal to `TargetContext.employment_entity_id`; a missing or
     mismatched employment context is `BLOCKED` (never softened to
     approval -- a fact from one job can never be silently attributed to
     another).
   - `ENTITY_SCOPED`: same entity-id check as `NON_TRANSFERABLE`, but an
     unverifiable relation is `APPROVAL_REQUIRED` (P3 does no entity-graph
     traversal, so it asks a human rather than hard-blocking).
   - `ROLE_TRANSFERABLE` / `INDUSTRY_TRANSFERABLE`: a missing
     `target_role_family` / `target_organization_or_industry` on
     `TargetContext` is `APPROVAL_REQUIRED` (P3 never infers these values).
     When present, the permission gate is authoritative -- P1 carries no
     fact-level role/industry field for P3 to compare against, so P3 does
     not invent a mismatch rule beyond what P1 already defines.
   - `GLOBAL_TRANSFERABLE`: no extra restriction.
   - `EXACT_ONLY`: `CONTROLLED_REPHRASE` is always `BLOCKED` (mirrors the
     existing `HardSafetyPolicy` rule); every other operation proceeds to
     the fact-type policy gate.
3. **P1 fact-type policy** (`evaluate_fact_type_policy`) -- the existing,
   frozen `TruthTransformationPolicyRegistry` from `app.services.truth_policy`,
   read-only and never mutated by P3:
   - An unknown `fact_type` (no registered `TransformationPolicy`) ->
     `BLOCKED` (`FACT_TYPE_POLICY_NOT_FOUND`).
   - `requested_target_scope` outside that fact_type's
     `allowed_target_scopes` -> `BLOCKED` (`TARGET_SCOPE_NOT_ALLOWED_BY_POLICY`).
     This also covers a `target_scope` string the registry has never heard
     of for any fact_type -- there is no separate "unknown scope" category,
     since scope validity is always evaluated relative to a specific
     fact_type's allowed set.
   - `requested_operation` outside that fact_type's `allowed_operations` ->
     `BLOCKED` (`OPERATION_NOT_ALLOWED_BY_POLICY`).
   - This gate runs for **every** operation, including `EXACT_COPY` and
     `FORMAT_NORMALIZATION` -- the base-operation shortcut in the next gate
     is only reachable after P1 has already cleared this exact
     `fact_type`/`target_scope`/`requested_operation` combination. It is
     never a global default-allow.
   - `BLOCKED` is always used here, never `APPROVAL_REQUIRED`: a P1 policy
     denial is a hard rule, not a request for human review, and no
     `TruthPermission` -- however `ACTIVE` -- is consulted once this gate
     has denied.
4. **Permission** (`evaluate_permission_grant`) -- derived only from the
   caller-supplied list of `TruthPermissionRead` and `evaluation_time`,
   and only ever reached once gate 3 has already allowed this
   `fact_type`/`target_scope`/`requested_operation`:
   - `EXACT_COPY` and `FORMAT_NORMALIZATION` require no permission at all
     (but only because gate 3 already confirmed P1 allows them here).
   - Every other operation requires an applicable permission (same
     `fact_id`, same `target_scope` as the request) that is `ACTIVE`, whose
     `allowed_operations` contains the requested operation, whose
     `constraints_json` matches the `TargetContext`, and whose
     `[valid_from, valid_until]` window contains `evaluation_time`.
   - `REVIEW_REQUIRED`, `REVOKED`, and `ARCHIVED` permissions never grant.
     A permission with the wrong `fact_id`/`target_scope` is not
     "applicable" at all and is excluded from `permission_ids` /
     `permission_snapshot_fingerprint`.
   - No operation is ever silently downgraded (e.g. a `CONTROLLED_REPHRASE`
     request never falls back to `FORMAT_NORMALIZATION`).
   - A permission can only narrow what P1 already allows for this
     fact_type -- it can never grant a `target_scope` or `requested_operation`
     gate 3 has already denied. `TruthPermission` cannot extend
     `allowed_target_scopes`, `allowed_operations`, any fact_type hard
     constraint, or `HardSafetyPolicy`.
5. **Career Positioning Engine advisory** (`evaluate_positioning_signal`) --
   only reached once the fact would otherwise be `SELECTED`:
   - `CareerPositioningSignal.not_relevant_fact_ids` is the *only* channel
     through which the CPE can affect a decision, and only by the fact's
     own stable `fact_id` -- never through title/company text matching.
   - `requires_human_review`, `flattening_required`, and
     `transferability_risk` only ever add an `advisory_flags` entry; they
     never change `status`, `use_in_cv`, `transferability`, or bypass a
     missing `TruthPermission`.

## Decision fingerprint

`decision_fingerprint` is a SHA-256 hex digest over a canonical JSON
projection of every input that can affect the decision:
`selection_policy_version`, `transformation_policy_version`, `fact_id`,
`entity_id`, `fact_type` (added in Stage P3.5), `fact_revision`,
`fact_content_fingerprint`, `target_context_fingerprint`,
`requested_operation`, `requested_target_scope`,
`permission_snapshot_fingerprint`, and the explicit `evaluation_time`.
There is no random UUID and no implicit `datetime.now()` anywhere in
`FactSelectionDecision` or in `select_fact()` -- `evaluation_time` is a
required, explicit parameter with no default.

`target_context_fingerprint` and `permission_snapshot_fingerprint` are
computed the same way: a canonical projection of the stable content fields
(never the opaque `target_context_id`, never raw job-description text),
hashed with the existing `canonical_json_bytes` helper from
`truth_fingerprint.py`.

`selection_policy_version` (module constant `SELECTION_POLICY_VERSION`,
currently `"fact-selection-policy-v3"`, bumped from `v2` in Stage P3.5 for the
new `fact_type` field) is a stable identifier for the exact
decision logic in `fact_selection_policy.py`, including the P1 fact-type
policy gate added in remediation R1. Any future change to *what* decides a
`FactSelectionDecision` -- including a change to how the P1 gate is
consulted -- must bump this constant so that fingerprints computed under
the old logic are distinguishable from ones computed under the new logic;
no random ID or current-time value is ever mixed into it.

`transformation_policy_version` (added in remediation R2) is a second,
independent version field on `FactSelectionDecision`, sourced directly from
`TruthTransformationPolicyRegistry.version` (`app/services/truth_policy.py`)
at the moment `select_fact()` runs -- never hardcoded, never a caller input.
It is included in the `decision_fingerprint` content and is a required
staleness field (see below). This exists because `selection_policy_version`
only tracks changes to P3's *own* decision logic, not to the *content* of
the P1 registry it delegates to: a future widening or narrowing of
`allowed_operations`/`allowed_target_scopes` for some `fact_type` changes
what a given input is allowed to decide to, without changing a single line
of `fact_selection_policy.py` itself. Without this field, that kind of
registry-only change would silently produce a different real-world decision
under the same `decision_fingerprint` -- P3 does not rely on a human
remembering to also bump `SELECTION_POLICY_VERSION` by hand whenever P1
changes.

## Replay and stale detection

`fact_selection_replay.py` is a set of pure functions with no clock and no
lookup. A previously produced decision is **stale** if any of these ten
fields differs from the current values: `fact_id`, `entity_id`, `fact_type`
(added in Stage P3.5), `fact_revision`, `fact_content_fingerprint`,
`target_context_fingerprint`, `requested_operation`, `selection_policy_version`,
`transformation_policy_version` (added in remediation R2),
`permission_snapshot_fingerprint`. `evaluation_time` is deliberately not
part of staleness comparison -- see "MVP limitations" below.

`fact_type` is sourced directly from `TruthFactRead.fact_type` -- never
looked up or inferred -- and validated with the existing `FACT_TYPE_PATTERN`.
It is deliberately its own explicit replay/fingerprint field even though a
fact_type change also always changes `fact_content_fingerprint`: keeping it
separate makes a fact_type change individually diagnosable in
`stale_fields()` without decoding the content fingerprint.

Calling `select_fact()` twice with the exact same full input always
produces an identical `decision_fingerprint` and an identical
`FactSelectionDecision`.

## MVP limitations

- P3 does not detect a *content*-level mismatch for `ROLE_TRANSFERABLE` /
  `INDUSTRY_TRANSFERABLE` facts, because `TruthFact` carries no per-fact
  role-family/industry field today. When `TargetContext` supplies the
  relevant field, P3 defers entirely to the permission gate rather than
  inventing new denial semantics.
- `evaluate_permission_grant`'s `constraints_json` matching only recognizes
  keys that name a real `TargetContext` attribute; any other key fails
  closed (the permission is treated as not matching).
- Stale detection does not include `evaluation_time`: two decisions that
  differ only because a permission's validity window was crossed by the
  clock (not by any Truth or permission record changing) are not flagged
  stale by `is_decision_stale()`. The caller is expected to re-run
  `select_fact()` with a current `evaluation_time` whenever it wants a
  time-sensitive re-evaluation, rather than relying on staleness detection
  for that case.
- The P1 `TruthTransformationPolicyRegistry` (`app/services/truth_policy.py`)
  registers `FLATTEN_FOR_LOWER_ROLE` for exactly two `fact_type`s --
  `ACHIEVEMENT`, in `{EXPERIENCE, PROJECT}` scope (added in remediation R2),
  and `EMPLOYMENT_NUMERIC_RESULT`, in `{EXPERIENCE}` scope (added in Stage
  P3.5) -- for controlled presentation-level flattening of an
  achievement/numeric-result description when applying to a lower-seniority
  role. No other `fact_type` gains it: `COMPANY`, `ROLE`,
  `EMPLOYMENT_PERIOD`, and `EMPLOYMENT_ROLE` are identity/structural data (a
  company name, an employment period, a formal role title) and must never
  have their presentation level altered by flattening, so they stay
  copy-only. `RESPONSIBILITY`, `SKILL`, `TOOL`, `TECHNOLOGY`, `SUMMARY`,
  `EMPLOYMENT_ACTIVITY`, and `EMPLOYMENT_RESPONSIBILITY_SCALE` remain exactly
  as narrow as before. Even for `ACHIEVEMENT`/`EMPLOYMENT_NUMERIC_RESULT`,
  `FLATTEN_FOR_LOWER_ROLE` still requires an `ACTIVE` `TruthPermission` that
  passes every check in the permission gate (same `fact_id`, matching
  `target_scope`, `allowed_operations` containing the operation, matching
  `constraints_json`, and a validity window that contains
  `evaluation_time`) -- P1 registering an operation only makes it
  *eligible* to be granted; a `TruthPermission` still governs *whether* it
  is granted for one specific fact. `ELEVATE_PRESENTATION_FOR_SENIOR_ROLE`,
  `COMBINE_APPROVED_FACTS`, `SPLIT_APPROVED_FACT`, and
  `SUMMARIZE_APPROVED_FACTS` remain registered for no `fact_type` and are
  still unconditionally `BLOCKED` (`OPERATION_NOT_ALLOWED_BY_POLICY`).
  Widening P1 further is a P1-owned change (`truth_policy.py`), out of
  scope for P3.

## Fail-closed reason codes (P1 fact-type policy gate)

Added in remediation R1, alongside the existing reason codes:

| Reason code                          | Meaning                                                              |
|---------------------------------------|-----------------------------------------------------------------------|
| `FACT_TYPE_POLICY_NOT_FOUND`          | `TruthTransformationPolicyRegistry.get(fact_type)` returned `None` -- no policy is registered for this `fact_type`. |
| `TARGET_SCOPE_NOT_ALLOWED_BY_POLICY`  | `requested_target_scope` is not in this fact_type's `allowed_target_scopes` (covers both a scope valid for a *different* fact_type and a scope the registry has never heard of). |
| `OPERATION_NOT_ALLOWED_BY_POLICY`     | `requested_operation` is not in this fact_type's `allowed_operations`. |

All three always produce `BLOCKED`, never `APPROVAL_REQUIRED` -- a P1
policy denial is a hard rule, not a request for human review, and no
`TruthPermission` is consulted once one of these has fired.

## Stage P3.5 — employment fact_type compatibility

Stage P3.5 (see `docs/explicit-provenance-stage-p2.md` for the P2 side) adds
four real employment fact_types to the P1
`TruthTransformationPolicyRegistry` (`app/services/truth_policy.py`,
`POLICY_REGISTRY_VERSION` bumped from `truth-transformation-policy-v2` to
`truth-transformation-policy-v3`):

| fact_type                          | `allowed_target_scopes` | `allowed_operations` |
|-------------------------------------|--------------------------|------------------------|
| `EMPLOYMENT_ROLE`                   | `EMPLOYMENT_HEADER`     | `EXACT_COPY`, `FORMAT_NORMALIZATION` |
| `EMPLOYMENT_ACTIVITY`               | `EXPERIENCE`            | `EXACT_COPY`, `CONTROLLED_REPHRASE`, `OMIT`, `REORDER` |
| `EMPLOYMENT_NUMERIC_RESULT`         | `EXPERIENCE`            | `EXACT_COPY`, `CONTROLLED_REPHRASE`, `OMIT`, `REORDER`, `FLATTEN_FOR_LOWER_ROLE` |
| `EMPLOYMENT_RESPONSIBILITY_SCALE`   | `EXPERIENCE`            | `EXACT_COPY`, `CONTROLLED_REPHRASE`, `OMIT`, `REORDER` |

Only `EMPLOYMENT_NUMERIC_RESULT` gains `FLATTEN_FOR_LOWER_ROLE` (alongside
the pre-existing `ACHIEVEMENT`), for the same reason `ACHIEVEMENT` gained it
in remediation R2: it is a narrative, free-text description whose
presentation level can be de-emphasized without altering the underlying
number. `EMPLOYMENT_ROLE`, `EMPLOYMENT_ACTIVITY`, and
`EMPLOYMENT_RESPONSIBILITY_SCALE` do not gain it. As with every other
`fact_type`, an `ACTIVE` `TruthPermission` naming `FLATTEN_FOR_LOWER_ROLE` for
`EMPLOYMENT_RESPONSIBILITY_SCALE` (or any fact_type the registry has not
granted it to) is still denied at gate 3 (`OPERATION_NOT_ALLOWED_BY_POLICY`)
-- permission narrows what the registry allows, it never extends it.

`FactSelectionDecision.fact_type` (new field, see "Decision fingerprint" and
"Replay and stale detection" above) means these four fact_types can now
actually reach `SELECTED`: previously, an employment fact could never pass
the `EMPLOYMENT_SCOPED` transferability gate because
`employment_scope_entity_id` was never populated (see
`docs/explicit-provenance-stage-p2.md`). Once P2's `apply()` or the P3.5
backfill sets that field correctly, `select_fact()` needs no P3 code change
at all to select an employment fact for an allowed operation -- the existing
pipeline (eligibility -> transferability -> P1 fact-type policy -> permission
-> CPE advisory) already handles it.

P3.5 adds no new SQL table, ORM model, router, or persistent decision log.
`FactSelectionDecision` remains a plain, in-memory Pydantic value exactly as
before; only its field set and the two version constants changed.

## Boundary with P4

P3 stops at producing a `FactSelectionDecision`. It does not decide *how*
a `SELECTED` fact is phrased, does not generate CV text, DOCX, or PDF
output, and does not touch the CV transformation plan/generation/approval
services. Turning a set of `FactSelectionDecision`s into actual generated
content is out of scope for P3 and belongs to a later stage.

## Stage P4.5a — non-employment CV section fact_types

Stage P4.5a adds exactly four real, non-employment fact_types to the P1
`TruthTransformationPolicyRegistry` (`app/services/truth_policy.py`,
`POLICY_REGISTRY_VERSION` bumped from `truth-transformation-policy-v3` to
`truth-transformation-policy-v4`):

| fact_type              | `allowed_target_scopes` | `allowed_operations`                 |
|------------------------|--------------------------|----------------------------------------|
| `EDUCATION_DEGREE`     | `EDUCATION`             | `EXACT_COPY`, `FORMAT_NORMALIZATION`   |
| `CERTIFICATION_NAME`   | `CERTIFICATIONS`        | `EXACT_COPY`, `FORMAT_NORMALIZATION`   |
| `COURSE_NAME`          | `COURSES`               | `EXACT_COPY`, `FORMAT_NORMALIZATION`   |
| `LANGUAGE_NAME`        | `LANGUAGES`             | `EXACT_COPY`, `FORMAT_NORMALIZATION`   |

All four are copy-only: unlike the Stage P3.5 `EMPLOYMENT_*` fact_types,
none of these four ever gains `CONTROLLED_REPHRASE`, `FLATTEN_FOR_LOWER_ROLE`,
`ELEVATE_PRESENTATION_FOR_SENIOR_ROLE`, `COMBINE_APPROVED_FACTS`,
`SPLIT_APPROVED_FACT`, or `SUMMARIZE_APPROVED_FACTS` -- a degree, a
certification name, a course name, or a language name is presented exactly
as recorded, never rephrased or restructured by the CV pipeline. No new
`TruthPermission` requirement was added for these operations: exactly like
every other `fact_type`, `EXACT_COPY`/`FORMAT_NORMALIZATION` remain
base-operation-shortcut eligible (gate 4 grants them without an explicit
permission once gate 3 has already cleared the fact_type/target_scope/
operation combination).

`SELECTION_POLICY_VERSION` (P3's own decision-logic version) is
**unchanged** at `"fact-selection-policy-v3"` -- Stage P4.5a is a P1 registry
content change only (new `TransformationPolicy` entries plus a
`POLICY_REGISTRY_VERSION` bump), consumed automatically through the existing
`transformation_policy_version` field on every decision (see "Decision
fingerprint" above). No change was made to `fact_selection_policy.py`
itself, so there is no reason to bump P3's own version.

P2's legacy migrator (`truth_legacy_migrator.py`) already produced these
four fact_types before Stage P4.5a existed (see the `_NAMED_CATEGORY_*`
tables for `wyksztalcenie`/`certyfikaty`/`kursy`/`jezyki`); Stage P4.5a adds
no new migrator code, no new backfill, and no new legacy category. It only
teaches the two downstream layers that previously had no policy or section
for these fact_types (P1's registry here, and P4's content policy) how to
handle them.

`AWARD` remains completely out of scope for Stage P4.5a: no
`TransformationPolicy` entry, no P4 section mapping, no owner `EntityType`
policy entry. An `AWARD`/`AWARD_NAME` fact_type still fails closed to
`BLOCKED` (`FACT_TYPE_POLICY_NOT_FOUND`) at gate 3, exactly as any other
unregistered fact_type would.
