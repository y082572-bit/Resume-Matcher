"""Controlled Candidacy Thesis builder for Explicit Provenance Stage
PRE-P4, Step 2.

``build_candidacy_thesis`` never trusts a self-reported analysis result
fingerprint, posting fingerprint, hypothesis id, role objective fingerprint,
fact id, entity id, fact revision, payload fingerprint, evidence mapping,
or positioning level -- every one of those is independently
re-derived/re-checked here. It performs no database I/O, reads no
``source_reference`` as identity, creates no DOCX/PDF, and never imports
``app.llm``/``litellm``/Career Positioning/a P4 builder/a router. It never
scores, ranks, or evaluates a candidate, and it never promises a future
outcome.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from uuid import UUID

from app.schemas.candidacy_thesis import (
    CANDIDACY_THESIS_POLICY_VERSION,
    CANDIDACY_THESIS_RESPONSE_SCHEMA_VERSION,
    CANDIDACY_THESIS_SCHEMA_VERSION,
    ROLE_STRATEGY_CONTEXT_SCHEMA_VERSION,
    BlockedCandidacyThesisItem,
    CandidacyEvidenceMapping,
    CandidacyThesisAdapterResponse,
    CandidacyThesisAttempt,
    CandidacyThesisContext,
    CandidacyThesisItemKind,
    CandidacyThesisModelConfiguration,
    CandidacyThesisProvenance,
    CandidacyThesisProviderErrorCode,
    CandidacyThesisResult,
    CandidacyThesisResultViolation,
    CandidacyThesisStatement,
    CandidacyThesisStatus,
    CandidacyThesisViolationCode,
    ControlledCandidacyThesisResponse,
    PositioningLevel,
    RoleEvidenceCategory,
    RoleStrategyContext,
    StrategicContributionStatement,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSet, ApprovedFactPayloadSnapshot
from app.schemas.job_posting_analysis import (
    JobAnalysisContext,
    JobAnalysisFreshnessStatus,
    JobAnalysisReplayInput,
    JobEvidenceSegment,
    JobPostingAnalysisResult,
    JobPostingAnalysisStatus,
    JobPostingSnapshot,
)
from app.services.candidacy_thesis_llm import ControlledCandidacyThesisAdapter
from app.services.candidacy_thesis_policy import (
    MAXIMUM_TOTAL_ATTEMPTS,
    RETRYABLE_PROVIDER_ERROR_CODES,
    violates_forbidden_scoring_language,
    violates_future_outcome_guarantee,
    violates_perfect_candidate_claim,
)
from app.services.candidacy_thesis_prompt import (
    build_controlled_candidacy_thesis_request,
    compute_candidacy_thesis_prompt_fingerprint,
)
from app.services.candidacy_thesis_replay import (
    build_candidacy_thesis_replay_input,
    compute_candidacy_thesis_replay_input_fingerprint,
    compute_positioning_context_fingerprint,
)
from app.services.job_analysis_builder import compute_job_analysis_result_fingerprint
from app.services.job_analysis_replay import (
    compute_job_analysis_replay_input_fingerprint,
    evaluate_job_analysis_freshness,
    replay_input_from_job_analysis_result,
)
from app.services.truth_fingerprint import canonical_json_bytes


CONTEXT_FINGERPRINT_VERSION = "candidacy-thesis-context-v1"
MODEL_CONFIGURATION_FINGERPRINT_VERSION = "candidacy-thesis-model-configuration-v1"
ATTEMPT_FINGERPRINT_VERSION = "candidacy-thesis-attempt-v1"
PROVENANCE_FINGERPRINT_VERSION = "candidacy-thesis-provenance-v1"
EVIDENCE_MAPPING_FINGERPRINT_VERSION = "candidacy-evidence-mapping-v1"
STRATEGIC_CONTRIBUTION_FINGERPRINT_VERSION = "strategic-contribution-statement-v1"
THESIS_STATEMENT_FINGERPRINT_VERSION = "candidacy-thesis-statement-v1"
BLOCKED_ITEM_FINGERPRINT_VERSION = "candidacy-thesis-blocked-item-v1"
RESULT_FINGERPRINT_VERSION = "candidacy-thesis-result-v1"
ROLE_STRATEGY_CONTEXT_FINGERPRINT_VERSION = "role-strategy-context-v1"
RAW_RESPONSE_FINGERPRINT_VERSION = "candidacy-thesis-raw-response-v1"
STRUCTURED_RESPONSE_FINGERPRINT_VERSION = "candidacy-thesis-structured-response-v1"
PAYLOAD_SET_FINGERPRINT_VERSION = "candidacy-thesis-payload-set-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_candidacy_thesis_model_configuration_fingerprint(
    configuration: CandidacyThesisModelConfiguration,
) -> str:
    content = {
        "provider": configuration.provider,
        "model_identifier": configuration.model_identifier,
        "temperature": configuration.temperature,
        "top_p": configuration.top_p,
        "max_output_tokens": configuration.max_output_tokens,
        "timeout_seconds": configuration.timeout_seconds,
        "maximum_attempts": configuration.maximum_attempts,
        "response_schema_version": configuration.response_schema_version,
        "retry_policy_version": configuration.retry_policy_version,
    }
    return _fingerprint(MODEL_CONFIGURATION_FINGERPRINT_VERSION, content)


def compute_candidacy_thesis_context_fingerprint(context: CandidacyThesisContext) -> str:
    content = {
        "locale": context.locale,
        "thesis_prompt_version": context.thesis_prompt_version,
        "thesis_policy_version": context.thesis_policy_version,
        "retry_policy_version": context.retry_policy_version,
        "response_schema_version": context.response_schema_version,
        "requested_positioning_level": context.requested_positioning_level.value,
        "priority_evidence_categories": [c.value for c in context.priority_evidence_categories],
        "model_configuration_fingerprint": compute_candidacy_thesis_model_configuration_fingerprint(
            context.model_configuration
        ),
    }
    return _fingerprint(CONTEXT_FINGERPRINT_VERSION, content)


def compute_candidacy_thesis_request_fingerprint(request) -> str:  # noqa: ANN001
    return compute_candidacy_thesis_prompt_fingerprint(request)


def compute_candidacy_thesis_attempt_fingerprint(
    *,
    attempt_number: int,
    request_fingerprint: str,
    response_fingerprint: str | None,
    provider_error_code: CandidacyThesisProviderErrorCode | None,
    finish_reason: str | None,
) -> str:
    content = {
        "attempt_number": attempt_number,
        "request_fingerprint": request_fingerprint,
        "response_fingerprint": response_fingerprint,
        "provider_error_code": provider_error_code.value if provider_error_code else None,
        "finish_reason": finish_reason,
    }
    return _fingerprint(ATTEMPT_FINGERPRINT_VERSION, content)


def compute_candidacy_thesis_provenance_fingerprint(provenance: CandidacyThesisProvenance) -> str:
    return _fingerprint(PROVENANCE_FINGERPRINT_VERSION, provenance.model_dump(mode="json"))


def compute_candidacy_evidence_mapping_fingerprint(
    *,
    entity_id: UUID,
    fact_id: UUID,
    fact_revision: int,
    payload_fingerprint: str,
    linked_hypothesis_fingerprint: str | None,
    linked_role_objective_fingerprint: str | None,
    role_evidence_category: RoleEvidenceCategory,
    narrative_link: str,
) -> str:
    content = {
        "entity_id": str(entity_id),
        "fact_id": str(fact_id),
        "fact_revision": fact_revision,
        "payload_fingerprint": payload_fingerprint,
        "linked_hypothesis_fingerprint": linked_hypothesis_fingerprint,
        "linked_role_objective_fingerprint": linked_role_objective_fingerprint,
        "role_evidence_category": role_evidence_category.value,
        "narrative_link": narrative_link,
    }
    return _fingerprint(EVIDENCE_MAPPING_FINGERPRINT_VERSION, content)


def compute_strategic_contribution_statement_fingerprint(
    *, statement: str, supporting_evidence_mapping_fingerprints: Sequence[str]
) -> str:
    content = {
        "statement": statement,
        "supporting_evidence_mapping_fingerprints": sorted(
            supporting_evidence_mapping_fingerprints
        ),
    }
    return _fingerprint(STRATEGIC_CONTRIBUTION_FINGERPRINT_VERSION, content)


def compute_candidacy_thesis_statement_fingerprint(
    *,
    statement: str,
    role_objective_fingerprint: str,
    referenced_hypothesis_fingerprints: Sequence[str],
    supporting_evidence_mapping_fingerprints: Sequence[str],
) -> str:
    content = {
        "statement": statement,
        "role_objective_fingerprint": role_objective_fingerprint,
        "referenced_hypothesis_fingerprints": sorted(referenced_hypothesis_fingerprints),
        "supporting_evidence_mapping_fingerprints": sorted(
            supporting_evidence_mapping_fingerprints
        ),
    }
    return _fingerprint(THESIS_STATEMENT_FINGERPRINT_VERSION, content)


def compute_blocked_candidacy_thesis_item_fingerprint(
    *,
    item_kind: CandidacyThesisItemKind,
    source_response_item_id: str | None,
    violation_code: CandidacyThesisViolationCode,
    provider_error_code: CandidacyThesisProviderErrorCode | None = None,
) -> str:
    content = {
        "item_kind": item_kind.value,
        "source_response_item_id": source_response_item_id,
        "violation_code": violation_code.value,
        "provider_error_code": provider_error_code.value if provider_error_code else None,
    }
    return _fingerprint(BLOCKED_ITEM_FINGERPRINT_VERSION, content)


def compute_candidacy_thesis_result_fingerprint(
    *,
    status: CandidacyThesisStatus,
    posting_fingerprint: str,
    analysis_result_fingerprint: str,
    payload_set_fingerprint: str | None,
    thesis_statement_fingerprint: str | None,
    evidence_mapping_fingerprints: Sequence[str],
    strategic_contribution_fingerprints: Sequence[str],
    positioning_level: PositioningLevel | None,
    target_narrative_emphasis: Sequence[RoleEvidenceCategory],
    blocked_item_fingerprints: Sequence[str],
) -> str:
    content = {
        "status": status.value,
        "posting_fingerprint": posting_fingerprint,
        "analysis_result_fingerprint": analysis_result_fingerprint,
        "payload_set_fingerprint": payload_set_fingerprint,
        "thesis_statement_fingerprint": thesis_statement_fingerprint,
        "evidence_mapping_fingerprints": sorted(evidence_mapping_fingerprints),
        "strategic_contribution_fingerprints": sorted(strategic_contribution_fingerprints),
        "positioning_level": positioning_level.value if positioning_level else None,
        "target_narrative_emphasis": sorted(c.value for c in target_narrative_emphasis),
        "blocked_item_fingerprints": sorted(blocked_item_fingerprints),
    }
    return _fingerprint(RESULT_FINGERPRINT_VERSION, content)


def compute_role_strategy_context_fingerprint(
    *,
    job_analysis_result_fingerprint: str,
    job_analysis_replay_fingerprint: str,
    candidacy_thesis_result_fingerprint: str,
    candidacy_thesis_replay_fingerprint: str,
    posting_fingerprint: str,
    role_objective_fingerprint: str,
    employer_problem_hypothesis_fingerprints: Sequence[str],
    priority_evidence_categories: Sequence[RoleEvidenceCategory],
    positioning_level: PositioningLevel,
    target_narrative_emphasis: Sequence[RoleEvidenceCategory],
    approved_supporting_fact_fingerprints: Sequence[str],
) -> str:
    content = {
        "context_schema_version": ROLE_STRATEGY_CONTEXT_SCHEMA_VERSION,
        "job_analysis_result_fingerprint": job_analysis_result_fingerprint,
        "job_analysis_replay_fingerprint": job_analysis_replay_fingerprint,
        "candidacy_thesis_result_fingerprint": candidacy_thesis_result_fingerprint,
        "candidacy_thesis_replay_fingerprint": candidacy_thesis_replay_fingerprint,
        "posting_fingerprint": posting_fingerprint,
        "role_objective_fingerprint": role_objective_fingerprint,
        "employer_problem_hypothesis_fingerprints": sorted(
            employer_problem_hypothesis_fingerprints
        ),
        "priority_evidence_categories": sorted(c.value for c in priority_evidence_categories),
        "positioning_level": positioning_level.value,
        "target_narrative_emphasis": sorted(c.value for c in target_narrative_emphasis),
        "approved_supporting_fact_fingerprints": sorted(approved_supporting_fact_fingerprints),
    }
    return _fingerprint(ROLE_STRATEGY_CONTEXT_FINGERPRINT_VERSION, content)


def compute_approved_fact_payload_set_fingerprint(
    payloads: Sequence[ApprovedFactPayloadSnapshot],
) -> str:
    content = {"payload_fingerprints": sorted(p.payload_fingerprint for p in payloads)}
    return _fingerprint(PAYLOAD_SET_FINGERPRINT_VERSION, content)


def _compute_raw_response_fingerprint(raw_response_text: str) -> str:
    return _fingerprint(RAW_RESPONSE_FINGERPRINT_VERSION, {"raw_response_text": raw_response_text})


def _compute_structured_response_fingerprint(
    response: ControlledCandidacyThesisResponse | None,
) -> str:
    content = response.model_dump(mode="json") if response is not None else None
    return _fingerprint(STRUCTURED_RESPONSE_FINGERPRINT_VERSION, {"structured_response": content})


def _empty_result(
    *,
    status: CandidacyThesisStatus,
    posting_fingerprint: str,
    analysis_result_fingerprint: str,
    violations: Sequence[CandidacyThesisResultViolation],
) -> CandidacyThesisResult:
    result_fingerprint = compute_candidacy_thesis_result_fingerprint(
        status=status,
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=analysis_result_fingerprint,
        payload_set_fingerprint=None,
        thesis_statement_fingerprint=None,
        evidence_mapping_fingerprints=(),
        strategic_contribution_fingerprints=(),
        positioning_level=None,
        target_narrative_emphasis=(),
        blocked_item_fingerprints=(),
    )
    return CandidacyThesisResult(
        result_fingerprint=result_fingerprint,
        status=status,
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=analysis_result_fingerprint,
        violations=tuple(violations),
        ready_for_p4=False,
    )


async def _call_adapter_with_retry(
    *,
    request,  # noqa: ANN001
    model_configuration: CandidacyThesisModelConfiguration,
    llm_adapter: ControlledCandidacyThesisAdapter,
) -> tuple[CandidacyThesisAdapterResponse, list[CandidacyThesisAttempt]]:
    request_fingerprint = compute_candidacy_thesis_request_fingerprint(request)
    attempts: list[CandidacyThesisAttempt] = []
    max_attempts = min(model_configuration.maximum_attempts, MAXIMUM_TOTAL_ATTEMPTS)
    response: CandidacyThesisAdapterResponse | None = None
    for attempt_number in range(1, max_attempts + 1):
        response = await llm_adapter.synthesize_thesis(
            request=request, model_configuration=model_configuration
        )
        response_fingerprint = None
        if response.structured_response is not None or response.raw_response_text:
            response_fingerprint = _compute_raw_response_fingerprint(response.raw_response_text)
        attempt_fingerprint = compute_candidacy_thesis_attempt_fingerprint(
            attempt_number=attempt_number,
            request_fingerprint=request_fingerprint,
            response_fingerprint=response_fingerprint,
            provider_error_code=response.provider_error_code,
            finish_reason=response.finish_reason,
        )
        attempts.append(
            CandidacyThesisAttempt(
                attempt_number=attempt_number,
                request_fingerprint=request_fingerprint,
                response_fingerprint=response_fingerprint,
                provider_error_code=response.provider_error_code,
                finish_reason=response.finish_reason,
                attempt_fingerprint=attempt_fingerprint,
            )
        )
        if response.provider_error_code is None:
            break
        if response.provider_error_code not in RETRYABLE_PROVIDER_ERROR_CODES:
            break
        if attempt_number >= max_attempts:
            break
    assert response is not None
    return response, attempts


def _thesis_text_violation(text: str) -> CandidacyThesisViolationCode | None:
    if violates_future_outcome_guarantee(text):
        return CandidacyThesisViolationCode.FUTURE_OUTCOME_GUARANTEE
    if violates_perfect_candidate_claim(text) or violates_forbidden_scoring_language(text):
        return CandidacyThesisViolationCode.UNSUPPORTED_CLAIM
    return None


async def build_candidacy_thesis(
    *,
    job_analysis_result: JobPostingAnalysisResult,
    job_analysis_snapshot: JobPostingSnapshot,
    job_analysis_segments: tuple[JobEvidenceSegment, ...],
    job_analysis_context: JobAnalysisContext,
    current_job_analysis_replay_input: JobAnalysisReplayInput | None,
    fact_payloads: ApprovedFactPayloadSet,
    thesis_context: CandidacyThesisContext,
    llm_adapter: ControlledCandidacyThesisAdapter,
) -> CandidacyThesisResult:
    posting_fingerprint = job_analysis_result.posting_fingerprint

    # -- re-verify the Job Analysis result's own integrity --
    expected_analysis_fp = compute_job_analysis_result_fingerprint(
        status=job_analysis_result.status,
        posting_fingerprint=job_analysis_result.posting_fingerprint,
        analysis_context_fingerprint=job_analysis_result.analysis_context_fingerprint,
        objective_fingerprint=(
            job_analysis_result.role_objective.objective_fingerprint
            if job_analysis_result.role_objective
            else None
        ),
        responsibility_fingerprints=[
            r.responsibility_fingerprint for r in job_analysis_result.responsibilities
        ],
        outcome_fingerprints=[o.outcome_fingerprint for o in job_analysis_result.outcomes],
        competency_fingerprints=[c.competency_fingerprint for c in job_analysis_result.competencies],
        hypothesis_fingerprints=[h.hypothesis_fingerprint for h in job_analysis_result.hypotheses],
        success_indicator_fingerprints=[
            s.success_indicator_fingerprint for s in job_analysis_result.success_indicators
        ],
        operating_context_fingerprints=[
            o.operating_context_fingerprint for o in job_analysis_result.operating_context
        ],
        evidence_priority_fingerprints=[
            e.evidence_priority_fingerprint for e in job_analysis_result.evidence_priorities
        ],
        ambiguity_fingerprints=[a.ambiguity_fingerprint for a in job_analysis_result.ambiguities],
        blocked_item_fingerprints=[
            b.blocked_item_fingerprint for b in job_analysis_result.blocked_items
        ],
    )
    if expected_analysis_fp != job_analysis_result.result_fingerprint:
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH,
                    detail="job_analysis_result.result_fingerprint does not match its recomputed value",
                ),
            ),
        )

    if (
        not job_analysis_result.ready_for_candidacy_thesis
        or job_analysis_result.status != JobPostingAnalysisStatus.ANALYZED
        or job_analysis_result.role_objective is None
    ):
        return _empty_result(
            status=CandidacyThesisStatus.BLOCKED,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.JOB_ANALYSIS_NOT_READY,
                    detail="job_analysis_result is not ready_for_candidacy_thesis",
                ),
            ),
        )

    # -- Job Analysis freshness: never trusted as a caller-supplied boolean --
    stored_analysis_replay_input = replay_input_from_job_analysis_result(
        snapshot=job_analysis_snapshot,
        segments=job_analysis_segments,
        analysis_context=job_analysis_context,
        result=job_analysis_result,
    )
    analysis_freshness = evaluate_job_analysis_freshness(
        stored_analysis_replay_input, current_job_analysis_replay_input
    )
    if analysis_freshness.freshness_status == JobAnalysisFreshnessStatus.FRESHNESS_NOT_VERIFIED:
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.JOB_ANALYSIS_FRESHNESS_NOT_VERIFIED,
                    detail="no current_job_analysis_replay_input was supplied",
                ),
            ),
        )
    if analysis_freshness.freshness_status == JobAnalysisFreshnessStatus.STALE:
        return _empty_result(
            status=CandidacyThesisStatus.BLOCKED,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.JOB_ANALYSIS_STALE,
                    detail=f"job_analysis_result is stale: {analysis_freshness.stale_fields}",
                ),
            ),
        )
    job_analysis_replay_fingerprint = compute_job_analysis_replay_input_fingerprint(
        stored_analysis_replay_input
    )

    # -- approved facts only: no pending, no rejected, no duplicates --
    expected_payload_set_fp = compute_approved_fact_payload_set_fingerprint(fact_payloads.payloads)
    if expected_payload_set_fp != fact_payloads.payload_set_fingerprint:
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.PAYLOAD_SET_FINGERPRINT_MISMATCH,
                    detail="fact_payloads.payload_set_fingerprint does not match its recomputed value",
                ),
            ),
        )
    identity_seen: set[tuple[str, str]] = set()
    for payload in fact_payloads.payloads:
        key = (str(payload.entity_id), str(payload.fact_id))
        if key in identity_seen:
            return _empty_result(
                status=CandidacyThesisStatus.INVALID_INPUT,
                posting_fingerprint=posting_fingerprint,
                analysis_result_fingerprint=job_analysis_result.result_fingerprint,
                violations=(
                    CandidacyThesisResultViolation(
                        violation_code=CandidacyThesisViolationCode.DUPLICATE_FACT_IDENTITY,
                        detail=f"duplicate (entity_id, fact_id) in fact_payloads: {key}",
                    ),
                ),
            )
        identity_seen.add(key)

    facts_by_fact_id = {p.fact_id: p for p in fact_payloads.payloads}
    hypotheses_by_fp = {h.hypothesis_fingerprint: h for h in job_analysis_result.hypotheses}
    role_objective = job_analysis_result.role_objective
    role_objective_fp = role_objective.objective_fingerprint

    request = build_controlled_candidacy_thesis_request(
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=job_analysis_result.result_fingerprint,
        role_objective=role_objective,
        hypotheses=job_analysis_result.hypotheses,
        approved_facts=fact_payloads.payloads,
        requested_positioning_level=thesis_context.requested_positioning_level,
        priority_evidence_categories=thesis_context.priority_evidence_categories,
        prompt_template_version=thesis_context.thesis_prompt_version,
        response_schema_version=thesis_context.response_schema_version,
    )
    prompt_fingerprint = compute_candidacy_thesis_prompt_fingerprint(request)
    request_fingerprint = prompt_fingerprint
    model_configuration_fingerprint = compute_candidacy_thesis_model_configuration_fingerprint(
        thesis_context.model_configuration
    )

    response, attempts = await _call_adapter_with_retry(
        request=request,
        model_configuration=thesis_context.model_configuration,
        llm_adapter=llm_adapter,
    )

    if response.provider_error_code is not None:
        violation_code = (
            CandidacyThesisViolationCode.RETRY_EXHAUSTED
            if len(attempts) > 1
            else CandidacyThesisViolationCode.PROVIDER_ERROR
        )
        blocked = BlockedCandidacyThesisItem(
            blocked_item_fingerprint=compute_blocked_candidacy_thesis_item_fingerprint(
                item_kind=CandidacyThesisItemKind.PROVIDER_LEVEL,
                source_response_item_id=None,
                violation_code=violation_code,
                provider_error_code=response.provider_error_code,
            ),
            item_kind=CandidacyThesisItemKind.PROVIDER_LEVEL,
            violation_code=violation_code,
            provider_error_code=response.provider_error_code,
            detail=f"provider error: {response.provider_error_code.value}",
        )
        result_fingerprint = compute_candidacy_thesis_result_fingerprint(
            status=CandidacyThesisStatus.PROVIDER_ERROR,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
            thesis_statement_fingerprint=None,
            evidence_mapping_fingerprints=(),
            strategic_contribution_fingerprints=(),
            positioning_level=None,
            target_narrative_emphasis=(),
            blocked_item_fingerprints=(blocked.blocked_item_fingerprint,),
        )
        return CandidacyThesisResult(
            result_fingerprint=result_fingerprint,
            status=CandidacyThesisStatus.PROVIDER_ERROR,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
            blocked_items=(blocked,),
            ready_for_p4=False,
        )

    structured = response.structured_response
    if structured is None:
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.RESPONSE_SCHEMA_MISMATCH,
                    detail="adapter returned no structured_response",
                ),
            ),
        )

    if (
        structured.posting_fingerprint != posting_fingerprint
        or structured.analysis_result_fingerprint != job_analysis_result.result_fingerprint
    ):
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.RESPONSE_SCHEMA_MISMATCH,
                    detail="response posting_fingerprint/analysis_result_fingerprint does not match the request",
                ),
            ),
        )

    if structured.acknowledged_positioning_level != thesis_context.requested_positioning_level:
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.POSITIONING_LEVEL_MISMATCH,
                    detail="response.acknowledged_positioning_level does not match the requested level",
                ),
            ),
        )

    all_ids: list[str] = [m.response_item_id for m in structured.evidence_mappings]
    all_ids += [s.response_item_id for s in structured.strategic_contribution_statements]
    if structured.thesis_statement is not None:
        all_ids.append(structured.thesis_statement.response_item_id)
    if len(all_ids) != len(set(all_ids)):
        return _empty_result(
            status=CandidacyThesisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            violations=(
                CandidacyThesisResultViolation(
                    violation_code=CandidacyThesisViolationCode.DUPLICATE_RESPONSE_ITEM_ID,
                    detail="the response contains duplicate response_item_id values",
                ),
            ),
        )

    blocked_items: list[BlockedCandidacyThesisItem] = []

    def _block(
        *,
        kind: CandidacyThesisItemKind,
        response_item_id: str,
        code: CandidacyThesisViolationCode,
        detail: str,
    ) -> None:
        blocked_items.append(
            BlockedCandidacyThesisItem(
                blocked_item_fingerprint=compute_blocked_candidacy_thesis_item_fingerprint(
                    item_kind=kind, source_response_item_id=response_item_id, violation_code=code
                ),
                item_kind=kind,
                source_response_item_id=response_item_id,
                violation_code=code,
                detail=detail,
            )
        )

    # -- evidence mappings: every claim anchored to an approved fact --
    evidence_mappings: dict[str, CandidacyEvidenceMapping] = {}
    mapping_fp_by_response_id: dict[str, str] = {}
    seen_mapping_fingerprints: set[str] = set()
    for item in structured.evidence_mappings:
        payload = facts_by_fact_id.get(item.fact_id)
        if payload is None:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.UNKNOWN_FACT,
                detail="fact_id was not found in the supplied approved fact payloads",
            )
            continue
        if payload.entity_id != item.entity_id:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.MISSING_FACT,
                detail="fact_id/entity_id combination was not found in the supplied approved fact payloads",
            )
            continue
        if payload.fact_revision != item.fact_revision or payload.payload_fingerprint != item.payload_fingerprint:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.PAYLOAD_MISMATCH,
                detail="fact_revision/payload_fingerprint does not match the supplied approved fact payload",
            )
            continue

        has_hypothesis_link = item.linked_hypothesis_fingerprint is not None
        has_objective_link = item.linked_role_objective_fingerprint is not None
        if has_hypothesis_link == has_objective_link:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.CLAIM_WITHOUT_PROVENANCE,
                detail="an evidence mapping must link to exactly one of hypothesis/role objective",
            )
            continue
        if has_hypothesis_link and item.linked_hypothesis_fingerprint not in hypotheses_by_fp:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.UNKNOWN_HYPOTHESIS,
                detail="linked_hypothesis_fingerprint was not found in the fresh Job Analysis result",
            )
            continue
        if has_objective_link and item.linked_role_objective_fingerprint != role_objective_fp:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.UNKNOWN_ROLE_OBJECTIVE,
                detail="linked_role_objective_fingerprint does not match the fresh Job Analysis role objective",
            )
            continue

        fp = compute_candidacy_evidence_mapping_fingerprint(
            entity_id=item.entity_id,
            fact_id=item.fact_id,
            fact_revision=item.fact_revision,
            payload_fingerprint=item.payload_fingerprint,
            linked_hypothesis_fingerprint=item.linked_hypothesis_fingerprint,
            linked_role_objective_fingerprint=item.linked_role_objective_fingerprint,
            role_evidence_category=item.role_evidence_category,
            narrative_link=item.narrative_link,
        )
        if fp in seen_mapping_fingerprints:
            _block(
                kind=CandidacyThesisItemKind.EVIDENCE_MAPPING,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.DUPLICATE_EVIDENCE_MAPPING,
                detail="this evidence mapping duplicates an already-accepted mapping",
            )
            continue
        seen_mapping_fingerprints.add(fp)
        mapping = CandidacyEvidenceMapping(
            evidence_mapping_fingerprint=fp,
            person_entity_id=payload.person_entity_id,
            entity_id=item.entity_id,
            fact_id=item.fact_id,
            fact_type=payload.fact_type,
            fact_revision=item.fact_revision,
            payload_fingerprint=item.payload_fingerprint,
            linked_hypothesis_fingerprint=item.linked_hypothesis_fingerprint,
            linked_role_objective_fingerprint=item.linked_role_objective_fingerprint,
            role_evidence_category=item.role_evidence_category,
            narrative_link=item.narrative_link,
        )
        evidence_mappings[fp] = mapping
        mapping_fp_by_response_id[item.response_item_id] = fp

    # -- strategic contribution statements --
    strategic_contributions: list[StrategicContributionStatement] = []
    for item in structured.strategic_contribution_statements:
        if not item.statement.strip():
            _block(
                kind=CandidacyThesisItemKind.STRATEGIC_CONTRIBUTION,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.EMPTY_STATEMENT,
                detail="statement is blank",
            )
            continue
        violation = _thesis_text_violation(item.statement)
        if violation is not None:
            _block(
                kind=CandidacyThesisItemKind.STRATEGIC_CONTRIBUTION,
                response_item_id=item.response_item_id,
                code=violation,
                detail="statement violates the closed thesis-safety guard",
            )
            continue
        referenced_fps = [
            mapping_fp_by_response_id[rid]
            for rid in item.supporting_evidence_mapping_response_item_ids
            if rid in mapping_fp_by_response_id
        ]
        if len(referenced_fps) != len(item.supporting_evidence_mapping_response_item_ids):
            _block(
                kind=CandidacyThesisItemKind.STRATEGIC_CONTRIBUTION,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.CLAIM_WITHOUT_PROVENANCE,
                detail="references an evidence mapping that was not accepted",
            )
            continue
        fp = compute_strategic_contribution_statement_fingerprint(
            statement=item.statement, supporting_evidence_mapping_fingerprints=referenced_fps
        )
        strategic_contributions.append(
            StrategicContributionStatement(
                contribution_fingerprint=fp,
                statement=item.statement,
                supporting_evidence_mapping_fingerprints=tuple(referenced_fps),
            )
        )

    # -- thesis statement (0 or 1) --
    thesis_statement: CandidacyThesisStatement | None = None
    if structured.thesis_statement is None:
        blocked_items.append(
            BlockedCandidacyThesisItem(
                blocked_item_fingerprint=compute_blocked_candidacy_thesis_item_fingerprint(
                    item_kind=CandidacyThesisItemKind.THESIS_STATEMENT,
                    source_response_item_id=None,
                    violation_code=CandidacyThesisViolationCode.NO_THESIS_STATEMENT,
                ),
                item_kind=CandidacyThesisItemKind.THESIS_STATEMENT,
                source_response_item_id=None,
                violation_code=CandidacyThesisViolationCode.NO_THESIS_STATEMENT,
                detail="the response carries no thesis_statement",
            )
        )
    else:
        item = structured.thesis_statement
        violation = None
        if not item.statement.strip():
            violation = CandidacyThesisViolationCode.EMPTY_STATEMENT
        else:
            violation = _thesis_text_violation(item.statement)
        unknown_hyp = [
            fp for fp in item.referenced_hypothesis_fingerprints if fp not in hypotheses_by_fp
        ]
        referenced_mapping_fps = [
            mapping_fp_by_response_id[rid]
            for rid in item.supporting_evidence_mapping_response_item_ids
            if rid in mapping_fp_by_response_id
        ]
        if violation is not None:
            _block(
                kind=CandidacyThesisItemKind.THESIS_STATEMENT,
                response_item_id=item.response_item_id,
                code=violation,
                detail="statement violates the closed thesis-safety guard",
            )
        elif unknown_hyp:
            _block(
                kind=CandidacyThesisItemKind.THESIS_STATEMENT,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.UNKNOWN_HYPOTHESIS,
                detail="referenced_hypothesis_fingerprints contains an unknown hypothesis",
            )
        elif len(referenced_mapping_fps) != len(item.supporting_evidence_mapping_response_item_ids):
            _block(
                kind=CandidacyThesisItemKind.THESIS_STATEMENT,
                response_item_id=item.response_item_id,
                code=CandidacyThesisViolationCode.CLAIM_WITHOUT_PROVENANCE,
                detail="references an evidence mapping that was not accepted",
            )
        else:
            fp = compute_candidacy_thesis_statement_fingerprint(
                statement=item.statement,
                role_objective_fingerprint=role_objective_fp,
                referenced_hypothesis_fingerprints=item.referenced_hypothesis_fingerprints,
                supporting_evidence_mapping_fingerprints=referenced_mapping_fps,
            )
            thesis_statement = CandidacyThesisStatement(
                thesis_fingerprint=fp,
                statement=item.statement,
                role_objective_fingerprint=role_objective_fp,
                referenced_hypothesis_fingerprints=tuple(item.referenced_hypothesis_fingerprints),
                supporting_evidence_mapping_fingerprints=tuple(referenced_mapping_fps),
            )

    raw_response_fingerprint = _compute_raw_response_fingerprint(response.raw_response_text)
    structured_response_fingerprint = _compute_structured_response_fingerprint(structured)
    provenance = CandidacyThesisProvenance(
        schema_version=CANDIDACY_THESIS_SCHEMA_VERSION,
        policy_version=CANDIDACY_THESIS_POLICY_VERSION,
        prompt_version=request.prompt_template_version,
        response_schema_version=CANDIDACY_THESIS_RESPONSE_SCHEMA_VERSION,
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=job_analysis_result.result_fingerprint,
        analysis_replay_fingerprint=job_analysis_replay_fingerprint,
        payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
        provider=response.provider,
        model_identifier=response.model_identifier,
        model_configuration_fingerprint=model_configuration_fingerprint,
        request_fingerprint=request_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        attempt_count=len(attempts),
        attempt_fingerprints=tuple(a.attempt_fingerprint for a in attempts),
        raw_response_fingerprint=raw_response_fingerprint,
        structured_response_fingerprint=structured_response_fingerprint,
    )

    if blocked_items:
        status = CandidacyThesisStatus.BLOCKED
    elif thesis_statement is None:
        status = CandidacyThesisStatus.BLOCKED
    else:
        status = CandidacyThesisStatus.READY

    result_fingerprint = compute_candidacy_thesis_result_fingerprint(
        status=status,
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=job_analysis_result.result_fingerprint,
        payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
        thesis_statement_fingerprint=thesis_statement.thesis_fingerprint if thesis_statement else None,
        evidence_mapping_fingerprints=[m.evidence_mapping_fingerprint for m in evidence_mappings.values()],
        strategic_contribution_fingerprints=[s.contribution_fingerprint for s in strategic_contributions],
        positioning_level=structured.acknowledged_positioning_level if status == CandidacyThesisStatus.READY else None,
        target_narrative_emphasis=structured.target_narrative_emphasis if status == CandidacyThesisStatus.READY else (),
        blocked_item_fingerprints=[b.blocked_item_fingerprint for b in blocked_items],
    )

    ready_for_p4 = status == CandidacyThesisStatus.READY and thesis_statement is not None

    role_strategy_context: RoleStrategyContext | None = None
    approved_supporting_fact_fingerprints = tuple(
        sorted({m.payload_fingerprint for m in evidence_mappings.values()})
    )
    employer_hypothesis_fps = tuple(
        sorted(
            {
                m.linked_hypothesis_fingerprint
                for m in evidence_mappings.values()
                if m.linked_hypothesis_fingerprint is not None
            }
            | set(thesis_statement.referenced_hypothesis_fingerprints if thesis_statement else ())
        )
    )
    positioning_context_fingerprint = compute_positioning_context_fingerprint(
        requested_positioning_level=thesis_context.requested_positioning_level,
        priority_evidence_categories=thesis_context.priority_evidence_categories,
    )
    evidence_priority_fps = tuple(
        sorted(e.evidence_priority_fingerprint for e in job_analysis_result.evidence_priorities)
    )

    if ready_for_p4 and thesis_statement is not None:
        replay_input = build_candidacy_thesis_replay_input(
            job_analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
            posting_fingerprint=posting_fingerprint,
            approved_payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
            supporting_fact_payload_fingerprints=approved_supporting_fact_fingerprints,
            hypothesis_fingerprints=employer_hypothesis_fps,
            role_objective_fingerprint=role_objective_fp,
            positioning_context_fingerprint=positioning_context_fingerprint,
            evidence_priority_fingerprints=evidence_priority_fps,
            thesis_statement_fingerprint=thesis_statement.thesis_fingerprint,
            evidence_mapping_fingerprints=[m.evidence_mapping_fingerprint for m in evidence_mappings.values()],
            strategic_contribution_fingerprints=[s.contribution_fingerprint for s in strategic_contributions],
            model_configuration_fingerprint=model_configuration_fingerprint,
            prompt_version=request.prompt_template_version,
            blocked_item_fingerprints=[b.blocked_item_fingerprint for b in blocked_items],
            result_fingerprint=result_fingerprint,
        )
        candidacy_thesis_replay_fingerprint = compute_candidacy_thesis_replay_input_fingerprint(
            replay_input
        )
        role_strategy_context_fingerprint = compute_role_strategy_context_fingerprint(
            job_analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
            candidacy_thesis_result_fingerprint=result_fingerprint,
            candidacy_thesis_replay_fingerprint=candidacy_thesis_replay_fingerprint,
            posting_fingerprint=posting_fingerprint,
            role_objective_fingerprint=role_objective_fp,
            employer_problem_hypothesis_fingerprints=employer_hypothesis_fps,
            priority_evidence_categories=thesis_context.priority_evidence_categories,
            positioning_level=structured.acknowledged_positioning_level,
            target_narrative_emphasis=structured.target_narrative_emphasis,
            approved_supporting_fact_fingerprints=approved_supporting_fact_fingerprints,
        )
        role_strategy_context = RoleStrategyContext(
            job_analysis_result_fingerprint=job_analysis_result.result_fingerprint,
            job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
            candidacy_thesis_result_fingerprint=result_fingerprint,
            candidacy_thesis_replay_fingerprint=candidacy_thesis_replay_fingerprint,
            posting_fingerprint=posting_fingerprint,
            role_objective_fingerprint=role_objective_fp,
            employer_problem_hypothesis_fingerprints=employer_hypothesis_fps,
            priority_evidence_categories=thesis_context.priority_evidence_categories,
            positioning_level=structured.acknowledged_positioning_level,
            target_narrative_emphasis=structured.target_narrative_emphasis,
            approved_supporting_fact_fingerprints=approved_supporting_fact_fingerprints,
            context_fingerprint=role_strategy_context_fingerprint,
        )

    return CandidacyThesisResult(
        result_fingerprint=result_fingerprint,
        status=status,
        posting_fingerprint=posting_fingerprint,
        analysis_result_fingerprint=job_analysis_result.result_fingerprint,
        analysis_replay_fingerprint=job_analysis_replay_fingerprint,
        payload_set_fingerprint=fact_payloads.payload_set_fingerprint,
        thesis_statement=thesis_statement,
        evidence_mappings=tuple(evidence_mappings.values()),
        strategic_contribution_statements=tuple(strategic_contributions),
        employer_problem_hypothesis_fingerprints=employer_hypothesis_fps,
        role_objective_fingerprint=role_objective_fp,
        priority_evidence_categories=thesis_context.priority_evidence_categories,
        positioning_level=structured.acknowledged_positioning_level if status == CandidacyThesisStatus.READY else None,
        target_narrative_emphasis=structured.target_narrative_emphasis if status == CandidacyThesisStatus.READY else (),
        provenance=provenance,
        blocked_items=tuple(blocked_items),
        role_strategy_context=role_strategy_context,
        ready_for_p4=ready_for_p4,
    )
