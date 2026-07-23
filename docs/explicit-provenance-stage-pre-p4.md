# Explicit Provenance Stage PRE-P4: Job Posting Analysis and Candidacy Thesis

## Purpose

PRE-P4 answers two questions **before** P4 (CV content planning) begins,
about the job posting and about Marek's fit to it — narrowly defined:

1. **Job Posting Analysis** (Step 1): what does this role actually need?
2. **Candidacy Thesis** (Step 2): why should Marek specifically be invited
   to interview for it?

PRE-P4 never scores, ranks, or evaluates a candidate. It carries no
`match_score`, `fit_score`, `suitability_score`, `probability_of_hire`,
`application_recommendation`, `apply_or_skip`, `candidate_risk`,
`candidacy_risk`, `weaknesses`, `candidate_gaps`, `recruiter_concerns`,
`rejection_recommendation`, `overqualification_risk`, `missing_critical`,
or `excessive_for_role` field anywhere in its 13 core modules. It never
recommends applying or not applying, never compares Marek to other
candidates, and never blocks CV preparation on a low match. Source-level
isolation tests enforce this (see `tests/unit/test_job_analysis_policy.py`,
`tests/unit/test_candidacy_thesis_policy.py`, and
`tests/integration/test_prep4_job_analysis_thesis_integration.py`).

## Variant B: two fully separated steps

### Step 1 — Job Posting Analysis

Knows only the job posting. Never knows Marek, his CV, the Truth Library,
the Master Resume, prior applications, other postings, or the Career
Positioning Engine.

```
JobPostingSnapshot
+ deterministic JobEvidenceSegments
+ JobAnalysisContext
+ ControlledJobPostingAnalysisAdapter
→ JobPostingAnalysisResult
```

### Step 2 — Candidacy Thesis

Knows a **fresh** `JobPostingAnalysisResult` plus **only Marek's approved**
fact payloads. Never knows pending facts, rejected facts, the full Truth
Library, the Master Resume, other postings, scoring, or risk.

```
fresh JobPostingAnalysisResult
+ approved fact payloads only
+ CandidacyThesisContext
+ ControlledCandidacyThesisAdapter
→ CandidacyThesisResult
→ RoleStrategyContext
```

Each step has its own schemas, its own `Protocol`, its own model
configuration, its own prompt, its own builder, its own result fingerprint,
its own replay, and its own freshness evaluation. **Changing Marek's facts
never invalidates Job Analysis** (Job Analysis has no parameter through
which a fact could even be supplied — see
`test_changing_approved_facts_never_changes_job_analysis_fingerprint`).
**Changing the job posting invalidates both** Job Analysis and Candidacy
Thesis.

## Fact vs. inference

Every accepted analysis item and every thesis claim carries an
`AnalysisAssertionBasis`:

- `EXPLICIT_IN_POSTING` — stated directly in the posting text.
- `INFERRED_FROM_POSTING` — a labeled hypothesis, never presented as an
  internal fact about the employer.

`EmployerProblemHypothesis` is always `INFERRED_FROM_POSTING` and is
described as a hypothesis, never as a stated company fact.

## Step 1 in detail

### `JobPostingSnapshot`

A closed, content-addressed snapshot (`app/schemas/job_posting_analysis.py`,
`app/services/job_posting_snapshot.py`). `posting_id` (a correlation
reference to a legacy `Job.job_id`, if any) and `source_url` never
participate in `snapshot_fingerprint` — neither is a substitute for
content-derived identity or freshness. `canonical_text` is NFKC-normalized,
line-ending-unified, and whitespace-collapsed deterministically; an empty
result raises `InvalidJobPostingSnapshotError`.

### Deterministic evidence segmentation

`segment_job_posting` is a pure function of `canonical_text` — no LLM, no
clock, no Python `hash()`. Each `JobEvidenceSegment.segment_id` is derived
from `(posting_fingerprint, normalized_text, segment_type, duplicate_discriminator)`,
where the discriminator is an ordinal **only among identical normalized
segments** (never a global list index). `segment_fingerprint` covers the
full canonical projection (id, type, source/normalized text, posting
fingerprint, section hint, order). A duplicate `segment_id` or a
fingerprint mismatch is fail-closed (`INVALID_INPUT`).

### Content categories

`RoleObjective`, `RoleResponsibility`, `ExpectedBusinessOutcome`,
`RequiredRoleCompetency`, `EmployerProblemHypothesis`,
`RoleSuccessIndicator`, `RoleOperatingContext`, `RoleEvidencePriority`, and
`JobPostingAmbiguity`. Every accepted item requires ≥1 supporting evidence
segment that exists and belongs to the snapshot (`JobPostingAmbiguity` may
instead carry `explicit_absence_of_information=True`). `RoleObjective` is
rejected if its statement equals the role title after normalization, or if
it has no evidence. `ExpectedBusinessOutcome` is rejected
(`UNSUPPORTED_NUMERIC_OUTCOME`) if it carries a number not present in any
cited evidence segment — no invented KPI. `RequiredRoleCompetency` never
carries whether Marek has it. Duplicate accepted items (by canonical
content fingerprint) are silently deduplicated; a duplicate
`response_item_id` **within the raw response** invalidates the whole
response (`INVALID_INPUT`).

### The overclaim guard

`EmployerProblemHypothesis.problem_statement` is checked against a closed
set of absolute-claim keyword groups (crisis, poor performance, losing
customers, financial trouble, layoffs, conflict, ineffective team, in
Polish and English — `job_analysis_policy.OVERCLAIM_GUARD_KEYWORD_GROUPS`).
A matched claim must be independently supported by a keyword from the same
group in the cited evidence, or the hypothesis is blocked
(`OVERCLAIM_UNSUPPORTED_HYPOTHESIS`). This is a closed guardrail, not a
general NLP system.

### `ControlledJobPostingAnalysisAdapter`

`app/services/job_analysis_llm.py` defines the sole boundary to any real
LLM provider — a pure `Protocol`, no production implementation, no import
of `app.llm`, `litellm`, the database, SQLAlchemy, a router, Career
Positioning, a P4 builder, or DOCX/PDF code anywhere in Step 1's core.
Tests use only deterministic fake adapters.

### Prompt boundary

`ControlledJobAnalysisRequest` carries only the snapshot's public fields,
the deterministic evidence segments, and a closed, always-complete
`prohibitions` set (`JobAnalysisProhibitionCode`). It never carries Marek's
CV, facts, Truth Library, Master Resume, prior applications, other
postings, or a scoring instruction. `ControlledJobAnalysisResponse` is
`extra="forbid"` — a candidate score or apply/reject recommendation cannot
even be attached; it would raise a `ValidationError`.

### Validation and retry

Nothing self-reported is trusted: snapshot fingerprint, segment ids,
segment fingerprints, analysis item ids, and assertion basis are all
independently re-derived and re-checked (`job_analysis_builder.py`). Retry
is capped at 2 total attempts (1 retry), only for `TIMEOUT` /
`TRANSPORT_ERROR`, using the same provider/model/request/prompt/
configuration — `provider_request_id` never participates in identity or
any fingerprint.

### `ready_for_candidacy_thesis`

Computed internally, never caller-supplied: `True` only when
`status == ANALYZED` and a `role_objective` was accepted (which itself
requires the full partition/evidence/overclaim checks to have passed —
see `JobPostingAnalysisResult._ready_requires_analyzed_and_objective`).

## Step 2 in detail

### Approved facts only

Candidacy Thesis reuses the **real approved-payload contract already in
the repo**: `ApprovedFactPayloadSnapshot` / `ApprovedFactPayloadSet` from
`app/schemas/cv_content_generation.py` (P4/P5a's own contract — imported
as a schema only, never through a P4 builder). A fact referenced by the
model's response that isn't in the supplied set is `UNKNOWN_FACT`; a
`(entity_id, fact_id)` combination that doesn't match is `MISSING_FACT`; a
`fact_revision`/`payload_fingerprint` mismatch is `PAYLOAD_MISMATCH`; a
duplicate `(entity_id, fact_id)` in the supplied set itself is
`DUPLICATE_FACT_IDENTITY`. There is no parameter through which a pending
or rejected fact, the full Truth Library, or the Master Resume could be
supplied at all.

### `ControlledCandidacyThesisAdapter`

`app/services/candidacy_thesis_llm.py` — the same Protocol-only,
zero-production-implementation pattern as Step 1, with the same isolation
from `app.llm`/`litellm`/DB/Career Positioning/a P4 builder/a router.

### Evidence mappings and thesis safety

Every claim-bearing `CandidacyEvidenceMapping` links exactly one approved
fact to exactly one `EmployerProblemHypothesis` or the `RoleObjective`
(never neither, never both — `_exactly_one_link`). A
`StrategicContributionStatement` and the `CandidacyThesisStatement` must
reference only *accepted* evidence mappings
(`CLAIM_WITHOUT_PROVENANCE` otherwise) and must pass a closed
thesis-safety guard (`candidacy_thesis_policy.py`):

- no future-outcome guarantee ("will definitely improve", "gwarantuje",
  "is guaranteed to") → `FUTURE_OUTCOME_GUARANTEE`,
- no "perfect/ideal candidate" language → `UNSUPPORTED_CLAIM`,
- no scoring/application-recommendation language ("match score", "should
  apply") → `UNSUPPORTED_CLAIM`.

Allowed logic: *employer needs X* + *approved fact: Marek did Y* + *thesis:
Marek's experience is credible evidence of capability to help with X*.
Forbidden: *Marek will definitely achieve X*. The thesis never fabricates
a new fact, never edits an approved fact's content, and never changes
employment history — `PositioningLevel` only shapes narrative framing
downstream, never the underlying claim.

### Job Analysis freshness re-verified, never trusted

Before calling the adapter, the builder independently recomputes the Job
Analysis result's own fingerprint (rejecting a tampered
`job_analysis_result.result_fingerprint`), requires
`ready_for_candidacy_thesis`, and re-evaluates freshness by comparing a
replay input rebuilt from the **original** snapshot/segments/context
against a caller-supplied **current** replay input. `current=None` is
`JOB_ANALYSIS_FRESHNESS_NOT_VERIFIED` (never guessed fresh); any drift is
`JOB_ANALYSIS_STALE`. Both fail-closed **before the adapter is ever
called**.

### `PositioningLevel` and `RoleEvidenceCategory`

`PositioningLevel` (`EXECUTIVE`/`SENIOR_LEADERSHIP`/`MANAGEMENT`/`EXPERT`/
`SPECIALIST`/`OPERATIONAL`) and `RoleEvidenceCategory` are independent,
closed enums — PRE-P4 never imports the Career Positioning Engine's
`PositioningDetailsSchema`/`positioning_strategy` literals. The model's
`acknowledged_positioning_level` must echo the caller's
`requested_positioning_level` exactly, or the response is rejected
(`POSITIONING_LEVEL_MISMATCH`) — the level is never taken purely on the
model's word.

### `ready_for_p4`

Computed internally, never caller-supplied: `True` only when
`status == READY`, a `thesis_statement` was accepted, and a
`RoleStrategyContext` was successfully assembled.

## `RoleStrategyContext`

The single, closed output of PRE-P4 core (`extra="forbid"`, no arbitrary
dict, no free-text thesis field, no `match_score`, no `candidate_risk`).
It references, by fingerprint only: the Job Analysis result and its replay
fingerprint, the Candidacy Thesis result and its replay fingerprint, the
posting fingerprint, the role objective fingerprint, the employer problem
hypothesis fingerprints actually used, the priority evidence categories,
the positioning level, the target narrative emphasis, and the approved
supporting fact fingerprints actually used — each of these independently
changes `context_fingerprint`.

**`RoleStrategyContext` is not yet consumed by P4 in this stage.** No P4
file was modified, and no new P4 entrypoint was created.

### What `ready_for_p4` means in this stage

`ready_for_p4 = True` means only: *PRE-P4 core is complete and would be
ready for a future, mandatory integration with P4.* It does **not** mean:

- that P4 already consumes `RoleStrategyContext`,
- that P4 already verifies the analysis or thesis fingerprint,
- that P4's own replay already includes the analysis or thesis,
- that the existing legacy P4 entrypoint cannot still be reached without
  going through PRE-P4 at all.

## Deferred mandatory PRE-P4 → P4 integration gate

A separate, future, mandatory stage — **PRE-P4 TO P4 MANDATORY INTEGRATION
GATE** — is required before P4 may be modified or before a new,
mandatory P4 v2 entrypoint may be added. That future stage must add tests
(none of which exist yet, and none of which are simulated here) proving
that the existing P4:

- requires a `RoleStrategyContext` as input,
- does not accept an arbitrary PRE-P4-shaped dict as a substitute,
- verifies the Job Analysis result fingerprint,
- verifies the Candidacy Thesis result fingerprint,
- changes P4's own replay/staleness outcome when the Job Analysis changes,
- changes P4's own replay/staleness outcome when the Candidacy Thesis
  changes,
- changes P4's own replay/staleness outcome when the priority evidence
  categories change,
- cannot be bypassed by a new production flow that skips PRE-P4.

This stage deliberately implements none of the above.

## Fingerprints

All fingerprints are SHA-256 lowercase hex over `canonical_json_bytes`
(from `app.services.truth_fingerprint`, reused — never a bespoke JSON
encoder), with an explicit `fingerprint_version` string, never involving
`datetime.now()`, Python's `hash()`, a `source_reference`, a URL, or a
bare list index as identity. See `job_analysis_builder.py` and
`candidacy_thesis_builder.py` for the full `compute_*` function set (role
objective, responsibility, outcome, competency, hypothesis, success
indicator, operating context, evidence priority, ambiguity, context, model
configuration, request/prompt, attempt, provenance, blocked item, result —
and, in Step 2, evidence mapping, strategic contribution, thesis
statement, and `RoleStrategyContext` itself).

## Replay and freshness

`job_analysis_replay.py` and `candidacy_thesis_replay.py` are both pure —
no clock, no lookup, no database. `current=None` is always
`FRESHNESS_NOT_VERIFIED`; an identical replay input is `FRESH`; any
differing field is `STALE`, with `stale_fields` naming exactly which ones.
A changed approved fact leaves Job Analysis `FRESH` but makes Candidacy
Thesis `STALE`; a changed posting makes both `STALE`.

## Fail-closed matrix

Empty posting text, an invalid or fingerprint-mismatched snapshot, a
duplicate or fingerprint-mismatched segment, an out-of-snapshot segment
reference, a provider timeout/transport error after retry, a rejected
provider call, invalid JSON, a schema mismatch, an empty response, content
filtering, a token limit, an unsupported finish reason, any forbidden
candidate-scoring field, a missing role objective, missing evidence for
any item or hypothesis, an unsupported employer claim, a stale Job
Analysis, a missing/pending/rejected/unknown/mismatched fact, an unknown
hypothesis or role objective, a thesis claim without provenance, an
unsupported claim, a future-outcome guarantee, a stale Candidacy Thesis,
and an invalid `RoleStrategyContext` are all fail-closed — never silently
omitted, never falling back to the job title alone, never falling back to
a match score, never an arbitrary free-text context, never a
`source_reference` identity, never a Career Positioning fallback.

## Isolation

None of PRE-P4's 13 core modules import `app.llm`, `litellm`, the
database, SQLAlchemy, a router, the frontend, DOCX/PDF rendering, or the
Career Positioning Engine (`app/services/career_positioning.py`,
`app/services/career_positioning_report.py`,
`app/schemas/career_positioning.py`). No P4 file was modified. No new P4
entrypoint was created. `career_positioning_snapshot_fingerprint` is never
used as a substitute for `RoleStrategyContext`, and this stage never
declares that P4 already requires PRE-P4.
