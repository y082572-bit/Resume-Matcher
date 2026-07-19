# Stage 10C — controlled generation from an approved plan

Stage 10C creates a technical, read-only CV draft from one exact current
`APPROVED` transformation plan. It does not create or update a `Resume`,
`Improvement`, `Job`, `Application`, or `MetricEvent`. The result is not valid
for application until a later Truth Validator and acceptance stage completes.

## Architecture

The canonical draft schema is `app.schemas.models.ResumeData`. The planner now
builds a `CVTransformationPlanBundle`: the existing public plan and private
`TransformationPlanBinding` objects are materialized in the same sorted pass.
Bindings contain exact source payloads and fingerprints and never appear in an
API response, approval, log, database row, or audit fixture.

The legacy tailoring generator is intentionally bypassed. Stage 10C reuses only
the centralized LLM transport and configuration functions: `get_llm_config`,
`get_model_name`, `get_safe_max_tokens`, and `complete_json`. Each eligible
source reference gets a separate minimal call with `schema_type` set to
`cv_transformation_generation`, strict JSON envelope mode, and prompt version
`1.3`. Generation version is also `1.3`; the deterministic plan remains `1.1`.

Planner and generation validator import one pure shared classifier from
`cv_claim_boundaries.py`. It owns the prohibited-category regexes, exact claim
detection, and permission classification, and performs no I/O, database access,
or LLM work. Plan fingerprinting changes whenever exact scoped permissions or
their bound sources change.

## Generation gate

Before the first model call the backend rebuilds and validates:

1. the Job and ready structured Resume and their relationship;
2. Truth Library and Career Positioning inputs;
3. the current plan and private bindings;
4. an exact approval ID and fingerprint with status `APPROVED`;
5. complete decisions, acknowledged guardrails, and no `REQUEST_REVIEW`;
6. configured provider/model and required credentials or local endpoint.

Errors are safe structured 404, 409, 422, 502, or 503 responses. Provider
details, prompts, source payloads, raw Truth data, API keys, API bases, and
reasoning are never returned.

## Decision and action semantics

| Decision/action | Behavior | LLM | Provenance mode | Priority |
| --- | --- | --- | --- | --- |
| `REJECT` | exclude exact item | no | `EXCLUDED` | normal |
| `REQUEST_REVIEW` | block whole request | no | none | none |
| `ACCEPT + KEEP` | exact copy | no | `COPIED` | normal |
| `ACCEPT + EMPHASIZE`, narrative sections | item-scoped rewrite | yes | `GENERATED` | high |
| `ACCEPT + EMPHASIZE`, competencies/tools | exact name insert | no | `EXACT_INSERT` | high |
| `ACCEPT + REPHRASE`, narrative sections | item-scoped rewrite | yes | `GENERATED` | normal |
| `ACCEPT + DEEMPHASIZE` | exact copy | no | `COPIED_LOW_PRIORITY` | low |
| `ACCEPT + OMIT` | omit exact item | no | `OMITTED` | low |
| `ACCEPT + HUMAN_REVIEW`, Resume source | exact preservation | no | `PRESERVED_AFTER_REVIEW` | normal |
| `ACCEPT + HUMAN_REVIEW`, unresolved source | exclude | no | `EXCLUDED_UNRESOLVED` | normal |
| `ACCEPT + EMPHASIZE/REPHRASE`, empty experience | preserve immutable role | no | `COPIED_NO_MUTABLE_CONTENT` | action priority |
| `ACCEPT + EMPHASIZE/REPHRASE`, narrative source with an unpermitted protected claim | copy the exact source | no | `COPIED_PROTECTED_CONTENT` | action priority |
| accepted achievement with excluded parent | exclude by dependency | no | `EXCLUDED_BY_PARENT` | action priority |

Experience title, company, years, and location are immutable. Every private
EXPERIENCE binding also carries exact per-bullet bindings: index, source text,
source fingerprint, allowed claim codes, and allowed operation. These private
bindings are never exposed, persisted in approval, or logged, and are included
in the generation input fingerprint. Bindings also carry original source,
parent-experience, and description positions plus a parent revision fingerprint.
EXPERIENCE rewrites indexed bullet slots first;
ACHIEVEMENTS then make the final decision for their exact original slot without
matching modified text. Each prompt contains only one binding, its action,
`allowed_operation`, all ten public guardrails, and permissions whose
`source_reference` is identical.
No evidence can move between employments, items, or bullets. EXPERIENCE content
is an ordered array of exact `{index, source_fingerprint, text}` objects covering
every source bullet exactly once. Numeric and high-risk claim validation runs
against each exact bullet instead of an aggregate description. Model responses
must use the exact three-key JSON contract, preserve the reference and prompt
version, contain no Markdown/HTML, and preserve the exact multiset of numeric,
currency, percentage, and year tokens. The transport accepts exactly one bare
JSON object after optional thinking-tag removal; fences, surrounding prose,
HTML, and additional JSON values are rejected. A conservative
textual boundary validator rejects newly introduced P&L, budget, Board/C-level,
people-management, manager-management, team-size, technical-skill,
language-level, certification, and quantified-result claims. Existing high-risk
claims require the exact item-scoped permission; permissions never authorize a
new claim.

Permissions are derived only from an allowed, approved Truth Library fact whose
normalized text exactly equals the source bullet and whose company, role, and
available employment dates match that Experience. An exact numeric result grants
`QUANTIFIED_RESULT_WITHOUT_EVIDENCE`; exact approved activities and responsibility
facts grant only categories actually detected in that same fact, including
technical skills and the remaining high-risk claim codes. Parent Experience
permissions are only the union of its per-bullet permissions. Job text, text
similarity, another company/role/bullet, unapproved Truth entries, and claims
merely present in Resume never grant permission. Exact technical values cannot
be replaced by a different technology under the same category.

If an accepted narrative `EMPHASIZE` or `REPHRASE` source contains any protected
claim without its exact permission, compilation fails closed before transport:
`COPIED_PROTECTED_CONTENT` copies the exact source, uses no LLM, keeps immutable
fields unchanged, records an output fingerprint and action-derived priority,
sets `prompt_version=null`, `validation_status=NOT_RUN`, and
`boundary_validation_status=NOT_APPLICABLE`. For Experience the entire original
description is preserved rather than partially rewriting or deleting claims.

## Draft and provenance

The service validates and deep-copies the existing `ResumeData`, then applies
compiled items deterministically. Contact data, education, projects, languages,
certifications, awards, section metadata, custom sections, and other canonical
content are preserved without sending the full Resume to the model.
Competencies use a collision-safe deterministic key beginning with
`careerPositioningCompetencies` and system metadata ID beginning with
`system:approved-plan-generation:`. Existing custom sections and metadata are
never reused or changed; suffixes `2`, `3`, and so on select the first free key.
The system section starts empty and contains only accepted competency items.
Tools alone use
`additional.technicalSkills`. Experience ordering is priority first and original
Resume position second.

Each plan item receives provenance containing only reference, section, action,
decision, mode, priority, source/output fingerprints, LLM usage, prompt version,
and validation statuses. `validation_status` is always `NOT_RUN` because the
Truth Validator belongs to Stage 10D. `boundary_validation_status` is `PASSED`
for generated items and `NOT_APPLICABLE` otherwise. `requires_truth_validation` is always `true`,
`truth_validation_status` is always `NOT_RUN`, and `applied_to_resume` is always
`false`.

## Persistence, idempotency, retry, and TOCTOU

`cv_transformation_generations` stores minimal metadata, final draft/provenance,
safe failure code, attempts, and timestamps. A unique
`generation_input_fingerprint` covers generation/prompt/plan versions, approval
ID, approval status, acknowledgment and sorted decisions, every compiled and
private binding semantic, all guardrails, and safe provider/model/reasoning
metadata. It excludes API key, API base, timestamps,
raw errors, prompts, and retry count.

SQLite `INSERT ... ON CONFLICT DO NOTHING` elects one initial caller. A second
caller reuses `GENERATED`, receives 409 for `GENERATING`, or may atomically claim
one explicit `FAILED` retry. An exact `SUPERSEDED` row is not current for GET,
but one POST caller can atomically reclaim it as `GENERATING`, increment its
attempt count, and clear all prior result/failure fields; concurrent losers get
409 and no duplicate row is created. Failed and superseded rows never contain a
partial draft or provenance.

After all model calls and again after assembly, the backend rebuilds the exact
current generation context (plan, bindings, approval, provider, model and input
fingerprint). Final persistence is a compare-and-set over generation ID,
`GENERATING`, attempt count, and input fingerprint. Any difference or lost CAS
marks or leaves the row non-current, stores no draft, and returns 409
`SOURCE_CHANGED_DURING_GENERATION`. Current GET resolves only the exact current
`generation_input_fingerprint`; scoped GET-by-ID projects mismatches as
`SUPERSEDED` and redacts draft and provenance.

An empty plan returns 422 `NO_TRANSFORMATION_ITEMS` before claim or LLM use.
Provider failures are logged only with generation ID, safe failure code,
provider, model, and exception class; exception messages, prompts, payloads, raw
responses, and credentials are excluded.

Strict-envelope/content failures after a provider response—fences, surrounding
prose, extra JSON, malformed or absent JSON, and empty content—are normalized to
`INVALID_LLM_RESPONSE`. Timeouts, connection failures, provider HTTP failures,
and LiteLLM transport exceptions remain `LLM_PROVIDER_ERROR`. Neither path stores
or returns the model response, prompt, or raw exception message.

The two read-only GET endpoints rebuild semantic current context using only safe
provider, model, and reasoning metadata. They do not validate an API key, probe a
provider, or require a live local endpoint. POST still validates all credentials
and endpoint requirements before claiming a generation or starting any LLM call.
Changing provider or model still changes the input fingerprint and projects the
old result as `SUPERSEDED`; removing only a credential does not.

## API and Stage 10D handoff

- `POST /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan/generation`
- `GET /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan/generation`
- `GET /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan/generation/{generation_id}`

POST accepts only `approval_id`, `plan_fingerprint`, and optional
`retry_failed`. Stage 10D will use the returned `generation_id` and scoped GET
endpoint to validate the exact technical draft. The Stage 10C UI is intentionally
read-only: it clears any old draft before refresh/generation and keeps it hidden
after errors. Generate/retry remain disabled while loading, generating, or in an
error state. It can generate, retry, refresh, and navigate back, but cannot edit,
save, export, apply, or create a Resume.

The full Truth Validator and any validation/application transition remain
explicitly out of scope until Stage 10D.
