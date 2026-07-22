# Explicit Provenance Stage P5b: Controlled LLM Content Proposals

## Purpose

Stage P5b generates **controlled, human-reviewable LLM rewrite proposals**
for exactly the two non-deterministic `TransformationOperation` values P5a
cannot render on its own:

- `CONTROLLED_REPHRASE`
- `FLATTEN_FOR_LOWER_ROLE`

Every proposal P5b produces carries `approval_state=REQUIRES_USER_APPROVAL`
and the result always carries `ready_for_p55=False`. P5b never approves,
rejects, or merges a proposal into a CV draft — it only proposes.

## Isolation from P5a

P5b never modifies P5a's deterministic elements or draft. It always
**re-validates** the P4 plan (via `validate_cv_content_plan`) and
independently re-verifies the P5a result's fingerprints and freshness — it
never trusts `p5a_result.status`, `p5a_result.ready_for_p55`, a blocked
item's own `violation_code` message, or any caller-supplied
eligibility/trust boolean. P5a's own supported-operation set
(`EXACT_COPY`/`FORMAT_NORMALIZATION`/`REORDER`) and P5b's eligible set
(`CONTROLLED_REPHRASE`/`FLATTEN_FOR_LOWER_ROLE`) are disjoint by
construction (`cv_content_generation_policy.SUPPORTED_OPERATIONS` vs.
`cv_content_proposal_policy.ELIGIBLE_OPERATIONS`).

## Isolation from Stage 10C

P5b imports nothing from `cv_transformation_plan`, `cv_transformation_approval`,
`cv_transformation_generation`, or `truth_legacy_migrator`. It never reads a
`source_reference` or `legacy_record_key` as identity, and never performs a
text-similarity lookup.

## The `ControlledProposalLlmAdapter` Protocol

`app/services/cv_content_proposal_llm.py` defines the sole boundary between
P5b's core and any real LLM provider:

```python
class ControlledProposalLlmAdapter(Protocol):
    async def generate(
        self, *, request: ControlledProposalRequest, model_configuration: LlmModelConfiguration,
    ) -> LlmAdapterResponse: ...
```

There is **no production adapter in P5b core**: no import of `app.llm`,
`litellm`, `LLMConfig`, `get_llm_config`, the database, or SQLAlchemy
anywhere in P5b's modules. Tests use only deterministic fake adapters. An
adapter implementation must never change the model, temperature, top_p,
timeout, or prompt between calls, and must never retry on its own — retry
is owned entirely by P5b's builder.

## Inputs

`generate_controlled_cv_content_proposals` consumes:

- the approved P4 `CvContentPlan`,
- the current P5a `CvContentGenerationResult`,
- the `GenerationContext` P5a used,
- `decisions_by_fingerprint`, `entity_types` (needed to re-run P4 validation),
- `current_plan_replay_input` (P4 freshness snapshot),
- `current_p5a_replay_input` (P5a freshness snapshot),
- the `ApprovedFactPayloadSet`,
- a `ProposalGenerationContext` (bounded target/tone/limits/model config),
- an injected `ControlledProposalLlmAdapter`.

It never accepts a caller-supplied `p5_ready`, `ready_for_p55`, eligibility
boolean, or "trusted fingerprint" boolean — every one of those is always
recomputed.

## Mandatory P4 revalidation

The entrypoint always calls `validate_cv_content_plan(...)` and requires
`plan.plan_status == VALID`, `structural_status == VALID`,
`freshness_status == FRESH`, `p5_ready == True`, and zero conflicts. Any
other outcome returns `ProposalGenerationStatus.BLOCKED` with zero
proposals — the provider is never called. A `None`
`current_plan_replay_input` is treated as freshness-not-verified, which
blocks P5b.

## Mandatory P5a validation and freshness

P5b independently re-verifies, all by recomputation (never trusting a
self-declared field):

- `p5a_result.content_plan_fingerprint == plan.content_plan_fingerprint`,
- `fact_payloads.payload_set_fingerprint` (recomputed) matches both its own
  field and `p5a_result.payload_set_fingerprint`,
- `p5a_result.result_fingerprint` (recomputed via P5a's own
  `compute_generation_result_fingerprint`),
- P5a freshness via `replay_input_from_generation_result` +
  `evaluate_generation_freshness` against `current_p5a_replay_input`.

A missing `current_p5a_replay_input`, or a `STALE`/`FRESHNESS_NOT_VERIFIED`
outcome, always returns `ProposalGenerationStatus.INVALID_INPUT`. P5b never
requires P5a's own `status` to be `GENERATED` — P5a is normally `BLOCKED`
precisely because it carries the eligible items P5b consumes.

## Eligibility

Eligible means, for one `BlockedGenerationItem`:

1. `item.violation_code == GenerationViolationCode.OPERATION_NOT_SUPPORTED_IN_P5A`
   (never a text/status/fact_type/section heuristic), **and**
2. `classify_eligibility(item.allowed_operation) is None`, i.e. the
   operation is exactly `CONTROLLED_REPHRASE` or `FLATTEN_FOR_LOWER_ROLE`.

`OMIT` maps to `OMIT_NOT_GENERATABLE`; every other operation
(`EXACT_COPY`/`FORMAT_NORMALIZATION`/`REORDER`/`ELEVATE_PRESENTATION_FOR_SENIOR_ROLE`/
`COMBINE_APPROVED_FACTS`/`SPLIT_APPROVED_FACT`/`SUMMARIZE_APPROVED_FACTS`) maps to
`OPERATION_NOT_ELIGIBLE_FOR_P5B`. Non-eligible blocked items are recorded,
untouched, in `non_eligible_blocked_item_fingerprints` — they never reach
the adapter.

## Payload boundary

P5b reuses P5a's exact `ApprovedFactPayloadSnapshot`/`ApprovedFactPayloadSet`
and fingerprint functions (`compute_fact_payload_fingerprint`,
`compute_fact_payload_set_fingerprint`) — it never defines a competing
payload model. For each eligible item it matches by `fact_id`, verifies
identity fields, and recomputes `payload_fingerprint`. A missing or
mismatched payload becomes a `BlockedProposalItem` **without any adapter
call** and without a database or text lookup.

## One-fact-one-proposal

Every adapter call is scoped to exactly one `PlannedFactUse`: one fact, one
operation, one `target_section`, one `target_scope`, one
`employment_scope_entity_id`, one `placement_order`, one
`ProposalGenerationContext`. No other fact, no neighboring element, no full
section, no full P5a draft, no other employment entry, no omitted/pending
fact, no full job ad, and no Master Resume is ever passed to the adapter.
There is no batch prompt.

## Operations

- **CONTROLLED_REPHRASE**: a wording change only — no new claim, no scope
  expansion, no changed number/date/name/negation.
- **FLATTEN_FOR_LOWER_ROLE**: may change presentation from managerial to
  expert/execution framing, but must never change the actual job title,
  company, material responsibility, or drop a number/range/result, and must
  never fabricate a false lower title.

Both always yield `approval_state=REQUIRES_USER_APPROVAL`. A
`TruthPermission` allowing the operation is not an approval of the
resulting text.

## Prompt contract

`cv_content_proposal_prompt.build_controlled_proposal_request` builds a
versioned `ControlledProposalRequest` containing only: operation,
fact_type, target_section, target_scope, employment_scope_entity_id, one
fact's source payload/text, immutable constraints, a bounded
`TargetRoleContext`, tone profile, length limits, an explicit closed
forbidden-transformations list, and the response schema version. The
`ControlledProposalResponse` schema is closed
(`proposal_text`/`operation`/`source_fact_id`/`preserved_tokens`/
`model_self_reported_warnings`) and validated by Pydantic — there is no
fallback to free text, and `preserved_tokens` is never trusted as
validation (it is model self-reported metadata only).

## Immutable constraints

`cv_content_proposal_constraints.py` deterministically extracts (never full
NLP/NER): integers, decimals, percentages, currency amounts, numeric
ranges, dates, periods, inequality signs, units directly attached to a
number, and a closed negation-word list from free text — plus
company/role identity **only** from the structured payload fields already
validated by P5a's payload-shape contract (never mined from prose).
Validation compares a fresh extraction over the proposal text against the
source's `ImmutableConstraintSet`: any missing, changed, or newly-added
token in any of these categories blocks the proposal. Any other proper
noun outside the structured payload remains P5.5's semantic-validation
responsibility.

## Retry policy

Retry is owned by P5b's builder — never the adapter. At most 2 total
attempts (1 base + 1 retry), and retry is allowed **only** for `TIMEOUT` and
`TRANSPORT_ERROR`. Every retry reuses the identical provider, model,
prompt, configuration, and request fingerprint. There is no fallback to a
different model.

## Fail-closed provider matrix

Every one of: timeout, transport error, provider rejection, invalid JSON,
response-schema mismatch, empty response, missing `proposal_text`, token
limit, content filtering, unsupported finish reason, retry exhaustion,
provider/model mismatch, operation mismatch, fact_id mismatch, length-limit
overrun, and any immutable-constraint violation produces a
`BlockedProposalItem` — never an `EXACT_COPY` fallback, never an empty
proposal, never a partial/raw-text fallback.

## Result partition

Every eligible `BlockedGenerationItem` maps to **exactly one** of a
`GeneratedCvContentProposal` or a `BlockedProposalItem` — complete and
disjoint, one proposal per fact_id. Non-eligible blocked items and P5a's
deterministic `GeneratedCvElement`s are referenced only by fingerprint in
the result's summary fields; P5b never builds a full CV draft.

## Provenance and fingerprints

`LlmGenerationProvenance` records schema/policy/prompt/response versions,
every relevant fingerprint (proposal context, target role context,
immutable constraint set, prompt, model configuration), attempt count and
attempt fingerprints, and fingerprints of the raw/structured response — but
never the raw prompt or response text itself, and never a provider request
ID as identity. `GeneratedCvContentProposal.proposal_fingerprint` folds in
the proposal text, source fact identity, operation, placement provenance,
prompt fingerprint, model configuration fingerprint, immutable constraint
set fingerprint, and the LLM provenance fingerprint — the same prompt does
not guarantee the same proposal fingerprint; the fingerprint identifies the
concrete result.

## Replay

`cv_content_proposal_replay.py` mirrors P4/P5a's pattern exactly: a missing
current replay input is `FRESHNESS_NOT_VERIFIED`, an identical replay input
is `FRESH`, and any drift (model, provider, temperature, timeout, retry
policy, prompt version, target context, immutable constraints, payload set,
P5a result, or proposal text) is `STALE`. No function reads a clock or
performs a lookup.

## `ready_for_p55`

Always `False` on every `CvContentProposalGenerationResult`. Stage P5.5 is
the only stage that performs claim-level semantic validation and approves
or rejects a proposal.

## Boundaries P5b never crosses

No database I/O, no SQL, no Master Resume read, no Truth Library read, no
DOCX/PDF generation, no router, no frontend wiring. `AWARD`/`AWARD_NAME` is
explicitly out of scope: no `AWARD` prompt, renderer, or fallback to
`ACHIEVEMENTS` — an `AWARD_NAME` fact fails closed
(`ProposalViolationCode.AWARD_NOT_SUPPORTED`) before any adapter call.
