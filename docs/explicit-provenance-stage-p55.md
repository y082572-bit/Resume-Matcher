# Explicit Provenance Stage P5.5: Semantic Validation, User Approval and Content Assembly

## Purpose

Stage P5.5 is the last stage before Stage P6. It takes the deterministic P5a
draft plus the controlled LLM proposals from P5b and turns them into a
single, deterministic, `ready_for_p6`-gated CV content result — through
exactly one semantic-validator call per P5b proposal, and exactly one
explicit user decision per proposal. It creates no DOCX, no PDF, modifies
no upstream stage, writes to no database, and never imports `app.llm`,
`litellm`, or Stage 10C.

## Two fully separated steps (Variant B)

| | Step 1 — Semantic Validation | Step 2 — Approval & Assembly |
|---|---|---|
| Module | `cv_content_semantic_validation_builder.py` | `cv_content_approval_builder.py` |
| Entrypoint | `validate_cv_content_proposals` | `build_approved_cv_content` |
| Result | `ProposalSemanticValidationResult` | `ApprovedCvContentResult` |
| Calls a provider? | Yes (the injected `ControlledProposalSemanticValidator`) | **Never** |
| Knows the user's decision? | **Never** | Yes (`ProposalApprovalDecision`) |
| Approves anything? | **Never** | Yes, deterministically |

Each step has its own fingerprint family, its own replay input/result
model, and its own freshness evaluation
(`ProposalSemanticValidationReplayInput`/`Result` vs.
`ApprovedCvContentReplayInput`/`Result`, both in
`cv_content_approval_replay.py`). Changing a user's approval decision never
re-triggers a semantic-validator call: Step 2 never imports or calls
`ControlledProposalSemanticValidator` — it only reuses pure, deterministic
fingerprint helpers exported by Step 1's builder module.

## Re-validation of P4 / P5a / P5b

Both steps always independently re-run:

- `validate_cv_content_plan(...)` — requires `plan_status == VALID`,
  `structural_status == VALID`, `freshness_status == FRESH`,
  `p5_ready == True`, zero conflicts;
- an independent recomputation of `p5a_result.result_fingerprint` via
  `compute_generation_result_fingerprint`, plus
  `evaluate_generation_freshness` against a caller-supplied
  `current_p5a_replay_input`;
- an independent recomputation of `p5b_result.result_fingerprint` via
  `compute_proposal_result_fingerprint`, plus `evaluate_proposal_freshness`
  against a caller-supplied `current_p5b_replay_input`.

Neither step ever trusts `plan.plan_status`, `p5a_result.status`,
`p5a_result.ready_for_p55`, `p5b_result.status`, or any caller-supplied
`p5_ready`/freshness/"trusted fingerprint" boolean. Any failure here is
fail-closed: zero semantic-validator calls (Step 1) or zero approvals
(Step 2).

## Deterministic guardrails (Step 1)

Before ever calling the semantic validator for one P5b proposal, Step 1
re-derives and re-checks (reusing P5b's own public functions — nothing is
re-implemented):

- payload identity + `compute_fact_payload_fingerprint`,
- the proposal's own `proposal_fingerprint` via
  `compute_generated_proposal_fingerprint`,
- `proposal_text_fingerprint` via `compute_proposal_text_fingerprint`,
- `source_text_fingerprint` via `render_generated_text` +
  `compute_source_text_fingerprint`,
- the immutable constraint set via `build_immutable_constraint_set` +
  `check_immutable_constraints`,
- the prompt fingerprint via `build_controlled_proposal_request` +
  `compute_prompt_fingerprint`,
- the model configuration fingerprint via
  `compute_model_configuration_fingerprint`,
- operation / employment scope / target section / target scope /
  placement order consistency against the P4 `PlannedFactUse`.

Any guardrail failure yields a `BlockedSemanticValidationItem`
(`SemanticGuardrailViolationCode`) and the semantic validator is **never**
called for that proposal. A semantic verdict can never override a
deterministic guardrail failure.

## One proposal, one fact

Every semantic-validator call concerns exactly one `GeneratedCvContentProposal`
against exactly one source fact/payload/section/scope. The
`ControlledSemanticValidationRequest` never carries another fact, another
proposal, the full plan, the full P5a draft, the Truth Library, the Master
Resume, omitted/pending facts, or the full job ad. There is no batch
validation call.

## The `ControlledProposalSemanticValidator` Protocol

`app/services/cv_content_semantic_validator.py` defines the sole boundary
between P5.5's core and any real semantic-check provider:

```python
class ControlledProposalSemanticValidator(Protocol):
    async def validate(
        self, *, request: ControlledSemanticValidationRequest,
        model_configuration: SemanticValidatorModelConfiguration,
    ) -> SemanticValidatorAdapterResponse: ...
```

There is no production adapter in P5.5's core: no import of `app.llm`,
`litellm`, `LLMConfig`, `get_llm_config`, the database, SQLAlchemy, or
Stage 10C anywhere in P5.5's modules. Tests use only deterministic fake
validators.

## Retry

Retry is owned entirely by Step 1's builder, never by the adapter: at most
2 total attempts (1 base + 1 retry), only for `TIMEOUT`/`TRANSPORT_ERROR`.
Every attempt reuses the same request fingerprint; each attempt gets its
own `attempt_fingerprint`. There is no model fallback.

## The semantic prompt and response

`cv_content_semantic_validation_prompt.py` builds a versioned,
one-proposal-one-fact `ControlledSemanticValidationRequest` carrying only
the source text, the proposal text, operation, fact type, target
section/scope, employment scope, the bounded `TargetRoleContext`, the
`ImmutableConstraintSet`, and a closed `forbidden_semantic_changes` list.

`ControlledSemanticValidationResponse` is a closed structured shape:
`verdict`, `source_fact_id`, `proposal_fingerprint`, `operation`,
`detected_violation_codes`, `source_claim_summary`,
`proposal_claim_summary`, `model_self_reported_warnings`. It never carries a
corrected proposal, a replacement text, or a user decision. Self-reported
`source_fact_id`/`proposal_fingerprint`/`operation` are always
independently re-verified against the request — a mismatch downgrades the
verdict to `INVALID_INPUT` regardless of what the provider claimed.

## Semantic verdict semantics

`PASS` means only that no forbidden semantic change was detected — it is a
**necessary, never sufficient**, condition for approval. It never implies
`APPROVED`, readiness for P6, or automatic acceptance.
`FAIL`/`INCONCLUSIVE`/`PROVIDER_ERROR`/`INVALID_INPUT` can never be
approved by Step 2, regardless of what decision a user submits.

### Claim violation taxonomy

`SemanticClaimViolationCode` is a closed, versioned set (new/removed
claims, responsibility scope increase/decrease, management scope,
decision authority, ownership, achievement/employment attribution,
company/role/date/number/percentage/currency change, negation, certainty,
causality, team size, portfolio/geographic/product/customer scope, and
`LOWER_ROLE_FLATTENING_BECAME_FALSE` for a dishonest flatten).

`CONTROLLED_REPHRASE` allows style changes only. `FLATTEN_FOR_LOWER_ROLE`
allows toning down managerial language but forbids a false lower title,
hidden scale, or swapped team/individual attribution.

## User approval decisions

`ProposalApprovalDecision` is explicit: `APPROVED` or `REJECTED`. A missing
decision is always interpreted as `PENDING` — Step 2 never synthesizes an
implicit approval. Every decision's `decision_context_fingerprint` and
`decision_fingerprint` are independently recalculated by Step 2
(`compute_decision_context_fingerprint`/
`compute_proposal_approval_decision_fingerprint`); a mismatch, a duplicate
decision, a decision for an unknown/guardrail-blocked proposal, or a
decision referencing a stale proposal/validation snapshot all make the
**entire** `ApprovedCvContentResult` `INVALID_INPUT`.

### Edited text is out of scope

`ProposalApprovalDecision` has no `edited_text`/`replacement_text`/
`corrected_text` field — the schema is closed (`extra="forbid"`) and
rejects one. A future edited revision must go through P5b again and be
re-validated by Step 1 as a new proposal.

### REJECTED policy

`REJECTED` is a resolved, terminal decision. A rejected proposal is
excluded from the final draft, keeps rejection provenance
(`FinalDispositionCode.REJECTED_BY_USER`), and — on its own — never blocks
`ready_for_p6`. `REQUIRES_REPLAN` remains a defensive-only status: under
the current closed policy, header facts are always deterministic
(`EXACT_COPY`) and therefore always come from P5a, so a single P5b
rejection can never force a replan (see
`test_requires_replan_is_unreachable_for_current_closed_operations`).

## Approval safety

`APPROVED` is honored only when **all** of: semantic verdict `PASS`,
deterministic guardrails passed, P4/P5a/P5b/semantic-validation freshness
all `FRESH`, proposal/proposal-text/payload/semantic-item/decision
fingerprints all match their independently recomputed values. `APPROVED`
can never override `FAIL`/`INCONCLUSIVE`/`PROVIDER_ERROR`/`INVALID_INPUT`,
a stale snapshot, or an identity mismatch.

## Final partition

Every `PlannedFactUse` from the P4 plan maps to exactly one
`CvContentFinalDisposition` — complete and disjoint. Omitted/pending P4
decisions and P5b's non-eligible blocked items are never synthesized into a
`PlannedFactUse`/disposition; their fingerprints are carried forward only
as a source summary (`pending_approval_fingerprints`,
`omitted_fact_fingerprints`, `conflict_fingerprints`,
`non_eligible_blocked_item_fingerprints`). `FinalDispositionCode` is a
closed 10-member set: `INCLUDED_DETERMINISTIC`,
`INCLUDED_APPROVED_PROPOSAL`, `REJECTED_BY_USER`, `PENDING_USER_DECISION`,
`SEMANTIC_VALIDATION_FAILED`, `SEMANTIC_VALIDATION_INCONCLUSIVE`,
`SEMANTIC_VALIDATION_PROVIDER_ERROR`, `INPUT_INVALID`,
`NON_ELIGIBLE_BLOCKED`, `REQUIRES_REPLAN`.

## Final content assembly

`ApprovedCvContentDraft` mirrors the P4 plan's exact structure: section
order, experience-entry order, employment grouping, and bucket order
(header/responsibility/achievement) are all preserved by iterating the
plan's own structures and only *filtering* — never reordering, merging,
splitting, or rewriting. Every `ApprovedCvContentElement` carries an
`origin` (`DETERMINISTIC_P5A` or `APPROVED_PROPOSAL_P5B`) and the full
provenance chain for that origin (generated-element/generation-provenance
fingerprints for P5a; proposal/semantic-item/decision/provenance
fingerprints for P5b).

## Fingerprints

All fingerprints are `hashlib.sha256` over
`truth_fingerprint.canonical_json_bytes`, lowercase hex, with stable
sorting, no `uuid4()`, no `datetime.now()`, no `source_reference`/list-index
identity, and no provider request ID as identity. Step 1's builder exports
`compute_semantic_validation_context_fingerprint`,
`compute_semantic_validator_model_configuration_fingerprint`,
`compute_semantic_validation_request_fingerprint` (== the prompt
fingerprint from `cv_content_semantic_validation_prompt.py`),
`compute_semantic_validation_attempt_fingerprint`,
`compute_semantic_validation_provenance_fingerprint`,
`compute_semantic_validation_item_fingerprint`,
`compute_blocked_semantic_validation_item_fingerprint`, and
`compute_semantic_validation_result_fingerprint`. Step 2's builder exports
`compute_proposal_approval_decision_fingerprint`,
`compute_approved_content_element_fingerprint`,
`compute_final_disposition_fingerprint`,
`compute_approved_flat_section_fingerprint`,
`compute_approved_experience_entry_fingerprint`,
`compute_approved_experience_section_fingerprint`,
`compute_approved_content_draft_fingerprint`, and
`compute_approved_content_result_fingerprint`. Self-declared fingerprints
(e.g. on a caller-supplied `ProposalApprovalDecision`) are always
recalculated before use, never trusted as-is.

## Replay

`cv_content_approval_replay.py` keeps the two replay families fully
separate:

- **Semantic validation replay**: `replay_input_from_semantic_validation_result`,
  `evaluate_semantic_validation_freshness`, `is_semantic_validation_stale`,
  `stale_semantic_validation_fields`,
  `compute_semantic_validation_replay_input_fingerprint`.
- **Approved content replay**: `replay_input_from_approved_content_result`,
  `evaluate_approved_content_freshness`, `is_approved_content_stale`,
  `stale_approved_content_fields`,
  `compute_approved_content_replay_input_fingerprint`.

No current replay input → `FRESHNESS_NOT_VERIFIED`. An identical replay
snapshot → `FRESH`. Any changed field → `STALE`, with the exact changed
field names reported. No function here reads a clock.

## `ready_for_p6`

Computed internally by Step 2, never caller-supplied. `True` only when
`status == READY`: P4/P5a/P5b/semantic-validation all valid and fresh,
every semantic-PASS proposal has an explicit resolved decision (no
`PENDING_USER_DECISION` anywhere), no provider errors, no inconclusive
validations, no invalid inputs, no unresolved (non-terminal) blocked items,
no `REQUIRES_REPLAN`, and the final partition is complete and disjoint. A
`REJECTED` proposal is resolved and never blocks `ready_for_p6` by itself.

## Fail-closed matrix

Every one of the following yields a fail-closed result (zero semantic
calls in Step 1, or an `INVALID_INPUT`/non-`READY` result in Step 2, never
a default-approved or silently-omitted outcome): invalid/stale P4,
invalid/stale P5a, invalid/stale P5b, missing/mismatched payload, proposal
or proposal-text fingerprint mismatch, immutable-constraint failure,
provider timeout/transport error/rejection, invalid JSON, schema mismatch,
empty response, content filtering, token limit, unsupported finish reason,
operation/fact-ID/proposal-fingerprint mismatch in the semantic response,
semantic `FAIL`/`INCONCLUSIVE`, a missing decision, approval attempted for
a stale proposal or a failed validation, duplicate/extra/unknown-proposal
decisions, a decision-fingerprint mismatch, a final-partition gap or
overlap, `REQUIRES_REPLAN`, and `AWARD_NAME`. There is no `EXACT_COPY`
fallback, no automatic/default approval, no silent omission, and no raw
response fallback anywhere in P5.5.

## Isolation from Stage 10C, the database, and the Master Resume

P5.5 imports nothing from `cv_transformation_plan`,
`cv_transformation_approval`, `cv_transformation_generation`, or
`truth_legacy_migrator`. It performs no database I/O (no `app.database`,
no SQLAlchemy, no `app.models`), never reads a `source_reference` or
`legacy_record_key` as identity, and never references the Master Resume.
It creates no DOCX/PDF, no FastAPI router, and no frontend code — see the
integration test's source-boundary checks
(`test_p4_p5a_p5b_p55_integration.py`).

## AWARD boundary

`AWARD_NAME` stays unsupported end-to-end: P4 never maps it to a section
(structurally omitted), so it never reaches P5a, P5b, or P5.5 in a
`PlannedFactUse` at all. P5.5 adds no `AWARD` semantic prompt, no
`CvSection.AWARDS`, and no fallback to `ACHIEVEMENTS`. This never blocks
P5.5's implementation for every other supported fact type.

## Boundary with Stage P6

P5.5 hands Stage P6 a single `ApprovedCvContentResult` with `ready_for_p6`
already computed. P5.5 never creates a DOCX/PDF, never renders a template,
and never writes to a database — that is entirely P6's (and later stages')
responsibility.
