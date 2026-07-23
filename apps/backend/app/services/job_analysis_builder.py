"""Controlled Job Posting Analysis builder for Explicit Provenance Stage
PRE-P4, Step 1.

``build_job_posting_analysis`` never trusts a self-reported snapshot
fingerprint, segment id, segment fingerprint, analysis item id, assertion
basis, or hypothesis id -- every one of those is independently
re-derived/re-checked here. It performs no database I/O, reads no
``source_reference`` as identity, creates no DOCX/PDF, and never imports
``app.llm``/``litellm``/Career Positioning/a P4 builder/a router. It never
scores, ranks, or evaluates a candidate -- it establishes facts about the
*role* only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.schemas.job_posting_analysis import (
    JOB_ANALYSIS_POLICY_VERSION,
    JOB_ANALYSIS_RESPONSE_SCHEMA_VERSION,
    JOB_ANALYSIS_SCHEMA_VERSION,
    AnalysisAssertionBasis,
    BlockedJobAnalysisItem,
    ControlledJobAnalysisResponse,
    EmployerProblemHypothesis,
    ExpectedBusinessOutcome,
    JobAnalysisAdapterResponse,
    JobAnalysisAttempt,
    JobAnalysisContext,
    JobAnalysisItemKind,
    JobAnalysisModelConfiguration,
    JobAnalysisProvenance,
    JobAnalysisProviderErrorCode,
    JobAnalysisResultViolation,
    JobAnalysisViolationCode,
    JobEvidenceSegment,
    JobPostingAmbiguity,
    JobPostingAnalysisResult,
    JobPostingAnalysisStatus,
    JobPostingSnapshot,
    OverclaimGuardStatus,
    RequiredRoleCompetency,
    RoleEvidencePriority,
    RoleObjective,
    RoleOperatingContext,
    RoleResponsibility,
    RoleSuccessIndicator,
)
from app.services.job_analysis_llm import ControlledJobPostingAnalysisAdapter
from app.services.job_analysis_policy import (
    MAXIMUM_TOTAL_ATTEMPTS,
    RETRYABLE_PROVIDER_ERROR_CODES,
    claim_is_supported_by_evidence,
    explicit_claim_is_supported_by_cited_evidence,
    numeric_claim_is_supported,
    statement_equals_role_title,
)
from app.services.job_analysis_prompt import (
    build_controlled_job_analysis_request,
    compute_job_analysis_prompt_fingerprint,
)
from app.services.job_posting_snapshot import (
    find_duplicate_segment_ids,
    recompute_job_evidence_segment_fingerprint,
    recompute_job_posting_snapshot_fingerprint,
)
from app.services.truth_fingerprint import canonical_json_bytes


CONTEXT_FINGERPRINT_VERSION = "job-analysis-context-v1"
MODEL_CONFIGURATION_FINGERPRINT_VERSION = "job-analysis-model-configuration-v1"
ATTEMPT_FINGERPRINT_VERSION = "job-analysis-attempt-v1"
PROVENANCE_FINGERPRINT_VERSION = "job-analysis-provenance-v1"
OBJECTIVE_FINGERPRINT_VERSION = "job-analysis-role-objective-v1"
RESPONSIBILITY_FINGERPRINT_VERSION = "job-analysis-responsibility-v1"
OUTCOME_FINGERPRINT_VERSION = "job-analysis-outcome-v1"
COMPETENCY_FINGERPRINT_VERSION = "job-analysis-competency-v1"
HYPOTHESIS_FINGERPRINT_VERSION = "job-analysis-hypothesis-v1"
SUCCESS_INDICATOR_FINGERPRINT_VERSION = "job-analysis-success-indicator-v1"
OPERATING_CONTEXT_FINGERPRINT_VERSION = "job-analysis-operating-context-v1"
EVIDENCE_PRIORITY_FINGERPRINT_VERSION = "job-analysis-evidence-priority-v1"
AMBIGUITY_FINGERPRINT_VERSION = "job-analysis-ambiguity-v1"
BLOCKED_ITEM_FINGERPRINT_VERSION = "job-analysis-blocked-item-v1"
RESULT_FINGERPRINT_VERSION = "job-analysis-result-v1"
RAW_RESPONSE_FINGERPRINT_VERSION = "job-analysis-raw-response-v1"
STRUCTURED_RESPONSE_FINGERPRINT_VERSION = "job-analysis-structured-response-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_job_analysis_model_configuration_fingerprint(
    configuration: JobAnalysisModelConfiguration,
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


def compute_job_analysis_context_fingerprint(context: JobAnalysisContext) -> str:
    content = {
        "analysis_policy_version": context.analysis_policy_version,
        "prompt_template_version": context.prompt_template_version,
        "retry_policy_version": context.retry_policy_version,
        "response_schema_version": context.response_schema_version,
        "model_configuration_fingerprint": compute_job_analysis_model_configuration_fingerprint(
            context.model_configuration
        ),
    }
    return _fingerprint(CONTEXT_FINGERPRINT_VERSION, content)


def compute_job_analysis_request_fingerprint(request) -> str:  # noqa: ANN001
    return compute_job_analysis_prompt_fingerprint(request)


def compute_job_analysis_attempt_fingerprint(
    *,
    attempt_number: int,
    request_fingerprint: str,
    response_fingerprint: str | None,
    provider_error_code: JobAnalysisProviderErrorCode | None,
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


def compute_job_analysis_provenance_fingerprint(provenance: JobAnalysisProvenance) -> str:
    return _fingerprint(PROVENANCE_FINGERPRINT_VERSION, provenance.model_dump(mode="json"))


def compute_role_objective_fingerprint(
    *,
    posting_fingerprint: str,
    concise_statement: str,
    assertion_basis: AnalysisAssertionBasis,
    supporting_evidence_segment_ids: Sequence[str],
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "concise_statement": concise_statement,
        "assertion_basis": assertion_basis.value,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
    }
    return _fingerprint(OBJECTIVE_FINGERPRINT_VERSION, content)


def compute_role_responsibility_fingerprint(
    *,
    posting_fingerprint: str,
    statement: str,
    assertion_basis: AnalysisAssertionBasis,
    supporting_evidence_segment_ids: Sequence[str],
    priority_order: int,
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "statement": statement,
        "assertion_basis": assertion_basis.value,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
        "priority_order": priority_order,
    }
    return _fingerprint(RESPONSIBILITY_FINGERPRINT_VERSION, content)


def compute_expected_outcome_fingerprint(
    *,
    posting_fingerprint: str,
    expected_change: str,
    affected_business_area: str,
    assertion_basis: AnalysisAssertionBasis,
    supporting_evidence_segment_ids: Sequence[str],
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "expected_change": expected_change,
        "affected_business_area": affected_business_area,
        "assertion_basis": assertion_basis.value,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
    }
    return _fingerprint(OUTCOME_FINGERPRINT_VERSION, content)


def compute_required_competency_fingerprint(
    *,
    posting_fingerprint: str,
    competency_name: str,
    competency_class: str,
    assertion_basis: AnalysisAssertionBasis,
    supporting_evidence_segment_ids: Sequence[str],
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "competency_name": competency_name,
        "competency_class": competency_class,
        "assertion_basis": assertion_basis.value,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
    }
    return _fingerprint(COMPETENCY_FINGERPRINT_VERSION, content)


def compute_employer_problem_hypothesis_fingerprint(
    *,
    posting_fingerprint: str,
    problem_statement: str,
    affected_business_area: str,
    observable_signals_in_posting: Sequence[str],
    supporting_evidence_segment_ids: Sequence[str],
    overclaim_guard_status: OverclaimGuardStatus,
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "problem_statement": problem_statement,
        "affected_business_area": affected_business_area,
        "observable_signals_in_posting": sorted(observable_signals_in_posting),
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
        "overclaim_guard_status": overclaim_guard_status.value,
    }
    return _fingerprint(HYPOTHESIS_FINGERPRINT_VERSION, content)


def compute_role_success_indicator_fingerprint(
    *,
    posting_fingerprint: str,
    indicator_statement: str,
    assertion_basis: AnalysisAssertionBasis,
    supporting_evidence_segment_ids: Sequence[str],
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "indicator_statement": indicator_statement,
        "assertion_basis": assertion_basis.value,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
    }
    return _fingerprint(SUCCESS_INDICATOR_FINGERPRINT_VERSION, content)


def compute_role_operating_context_fingerprint(
    *,
    posting_fingerprint: str,
    context_statement: str,
    assertion_basis: AnalysisAssertionBasis,
    supporting_evidence_segment_ids: Sequence[str],
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "context_statement": context_statement,
        "assertion_basis": assertion_basis.value,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
    }
    return _fingerprint(OPERATING_CONTEXT_FINGERPRINT_VERSION, content)


def compute_role_evidence_priority_fingerprint(
    *,
    posting_fingerprint: str,
    priority_theme: str,
    rationale: str,
    supporting_evidence_segment_ids: Sequence[str],
    priority_rank: int,
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "priority_theme": priority_theme,
        "rationale": rationale,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
        "priority_rank": priority_rank,
    }
    return _fingerprint(EVIDENCE_PRIORITY_FINGERPRINT_VERSION, content)


def compute_job_posting_ambiguity_fingerprint(
    *,
    posting_fingerprint: str,
    ambiguity_type: str,
    description: str,
    supporting_evidence_segment_ids: Sequence[str],
    explicit_absence_of_information: bool,
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "ambiguity_type": ambiguity_type,
        "description": description,
        "supporting_evidence_segment_ids": sorted(supporting_evidence_segment_ids),
        "explicit_absence_of_information": explicit_absence_of_information,
    }
    return _fingerprint(AMBIGUITY_FINGERPRINT_VERSION, content)


def compute_blocked_job_analysis_item_fingerprint(
    *,
    item_kind: JobAnalysisItemKind,
    source_response_item_id: str | None,
    violation_code: JobAnalysisViolationCode,
    provider_error_code: JobAnalysisProviderErrorCode | None = None,
) -> str:
    content = {
        "item_kind": item_kind.value,
        "source_response_item_id": source_response_item_id,
        "violation_code": violation_code.value,
        "provider_error_code": provider_error_code.value if provider_error_code else None,
    }
    return _fingerprint(BLOCKED_ITEM_FINGERPRINT_VERSION, content)


def compute_job_analysis_result_fingerprint(
    *,
    status: JobPostingAnalysisStatus,
    posting_fingerprint: str,
    analysis_context_fingerprint: str | None,
    objective_fingerprint: str | None,
    responsibility_fingerprints: Sequence[str],
    outcome_fingerprints: Sequence[str],
    competency_fingerprints: Sequence[str],
    hypothesis_fingerprints: Sequence[str],
    success_indicator_fingerprints: Sequence[str],
    operating_context_fingerprints: Sequence[str],
    evidence_priority_fingerprints: Sequence[str],
    ambiguity_fingerprints: Sequence[str],
    blocked_item_fingerprints: Sequence[str],
) -> str:
    content = {
        "status": status.value,
        "posting_fingerprint": posting_fingerprint,
        "analysis_context_fingerprint": analysis_context_fingerprint,
        "objective_fingerprint": objective_fingerprint,
        "responsibility_fingerprints": sorted(responsibility_fingerprints),
        "outcome_fingerprints": sorted(outcome_fingerprints),
        "competency_fingerprints": sorted(competency_fingerprints),
        "hypothesis_fingerprints": sorted(hypothesis_fingerprints),
        "success_indicator_fingerprints": sorted(success_indicator_fingerprints),
        "operating_context_fingerprints": sorted(operating_context_fingerprints),
        "evidence_priority_fingerprints": sorted(evidence_priority_fingerprints),
        "ambiguity_fingerprints": sorted(ambiguity_fingerprints),
        "blocked_item_fingerprints": sorted(blocked_item_fingerprints),
    }
    return _fingerprint(RESULT_FINGERPRINT_VERSION, content)


def _compute_raw_response_fingerprint(raw_response_text: str) -> str:
    return _fingerprint(RAW_RESPONSE_FINGERPRINT_VERSION, {"raw_response_text": raw_response_text})


def _compute_structured_response_fingerprint(
    response: ControlledJobAnalysisResponse | None,
) -> str:
    content = response.model_dump(mode="json") if response is not None else None
    return _fingerprint(STRUCTURED_RESPONSE_FINGERPRINT_VERSION, {"structured_response": content})


def _empty_result(
    *,
    status: JobPostingAnalysisStatus,
    posting_fingerprint: str,
    snapshot_schema_version: str,
    violations: Sequence[JobAnalysisResultViolation],
    analysis_context_fingerprint: str | None = None,
) -> JobPostingAnalysisResult:
    result_fingerprint = compute_job_analysis_result_fingerprint(
        status=status,
        posting_fingerprint=posting_fingerprint,
        analysis_context_fingerprint=analysis_context_fingerprint,
        objective_fingerprint=None,
        responsibility_fingerprints=(),
        outcome_fingerprints=(),
        competency_fingerprints=(),
        hypothesis_fingerprints=(),
        success_indicator_fingerprints=(),
        operating_context_fingerprints=(),
        evidence_priority_fingerprints=(),
        ambiguity_fingerprints=(),
        blocked_item_fingerprints=(),
    )
    return JobPostingAnalysisResult(
        result_fingerprint=result_fingerprint,
        status=status,
        posting_fingerprint=posting_fingerprint,
        snapshot_schema_version=snapshot_schema_version,
        analysis_context_fingerprint=analysis_context_fingerprint,
        violations=tuple(violations),
        ready_for_candidacy_thesis=False,
    )


async def _call_adapter_with_retry(
    *,
    request,  # noqa: ANN001
    model_configuration: JobAnalysisModelConfiguration,
    llm_adapter: ControlledJobPostingAnalysisAdapter,
) -> tuple[JobAnalysisAdapterResponse, list[JobAnalysisAttempt]]:
    request_fingerprint = compute_job_analysis_request_fingerprint(request)
    attempts: list[JobAnalysisAttempt] = []
    max_attempts = min(model_configuration.maximum_attempts, MAXIMUM_TOTAL_ATTEMPTS)
    response: JobAnalysisAdapterResponse | None = None
    for attempt_number in range(1, max_attempts + 1):
        response = await llm_adapter.analyze(
            request=request, model_configuration=model_configuration
        )
        response_fingerprint = None
        if response.structured_response is not None or response.raw_response_text:
            response_fingerprint = _compute_raw_response_fingerprint(response.raw_response_text)
        attempt_fingerprint = compute_job_analysis_attempt_fingerprint(
            attempt_number=attempt_number,
            request_fingerprint=request_fingerprint,
            response_fingerprint=response_fingerprint,
            provider_error_code=response.provider_error_code,
            finish_reason=response.finish_reason,
        )
        attempts.append(
            JobAnalysisAttempt(
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


def _evidence_texts(segments_by_id: dict[str, JobEvidenceSegment], ids: Sequence[str]) -> list[str]:
    return [segments_by_id[i].source_text for i in ids if i in segments_by_id]


async def build_job_posting_analysis(
    *,
    snapshot: JobPostingSnapshot,
    segments: tuple[JobEvidenceSegment, ...],
    analysis_context: JobAnalysisContext,
    llm_adapter: ControlledJobPostingAnalysisAdapter,
) -> JobPostingAnalysisResult:
    expected_snapshot_fp = recompute_job_posting_snapshot_fingerprint(snapshot)
    if expected_snapshot_fp != snapshot.snapshot_fingerprint:
        return _empty_result(
            status=JobPostingAnalysisStatus.INVALID_INPUT,
            posting_fingerprint=snapshot.snapshot_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            violations=(
                JobAnalysisResultViolation(
                    violation_code=JobAnalysisViolationCode.SNAPSHOT_FINGERPRINT_MISMATCH,
                    detail="snapshot_fingerprint does not match its recomputed value",
                ),
            ),
        )

    posting_fingerprint = snapshot.snapshot_fingerprint

    if not segments:
        return _empty_result(
            status=JobPostingAnalysisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            violations=(
                JobAnalysisResultViolation(
                    violation_code=JobAnalysisViolationCode.EMPTY_POSTING_TEXT,
                    detail="no evidence segments were supplied for this posting",
                ),
            ),
        )

    duplicate_ids = find_duplicate_segment_ids(segments)
    if duplicate_ids:
        return _empty_result(
            status=JobPostingAnalysisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            violations=(
                JobAnalysisResultViolation(
                    violation_code=JobAnalysisViolationCode.DUPLICATE_SEGMENT_ID,
                    detail=f"duplicate segment_id values: {duplicate_ids}",
                ),
            ),
        )

    for segment in segments:
        if segment.posting_fingerprint != posting_fingerprint:
            return _empty_result(
                status=JobPostingAnalysisStatus.INVALID_INPUT,
                posting_fingerprint=posting_fingerprint,
                snapshot_schema_version=snapshot.snapshot_schema_version,
                violations=(
                    JobAnalysisResultViolation(
                        violation_code=JobAnalysisViolationCode.SEGMENT_NOT_OWNED_BY_SNAPSHOT,
                        detail=f"segment {segment.segment_id} does not belong to this snapshot",
                    ),
                ),
            )
        if recompute_job_evidence_segment_fingerprint(segment) != segment.segment_fingerprint:
            return _empty_result(
                status=JobPostingAnalysisStatus.INVALID_INPUT,
                posting_fingerprint=posting_fingerprint,
                snapshot_schema_version=snapshot.snapshot_schema_version,
                violations=(
                    JobAnalysisResultViolation(
                        violation_code=JobAnalysisViolationCode.SEGMENT_FINGERPRINT_MISMATCH,
                        detail=f"segment {segment.segment_id} fingerprint does not match its recomputed value",
                    ),
                ),
            )

    segments_by_id = {s.segment_id: s for s in segments}
    analysis_context_fingerprint = compute_job_analysis_context_fingerprint(analysis_context)

    request = build_controlled_job_analysis_request(
        snapshot=snapshot,
        segments=segments,
        prompt_template_version=analysis_context.prompt_template_version,
        response_schema_version=analysis_context.response_schema_version,
    )
    prompt_fingerprint = compute_job_analysis_prompt_fingerprint(request)
    request_fingerprint = prompt_fingerprint
    model_configuration_fingerprint = compute_job_analysis_model_configuration_fingerprint(
        analysis_context.model_configuration
    )

    response, attempts = await _call_adapter_with_retry(
        request=request,
        model_configuration=analysis_context.model_configuration,
        llm_adapter=llm_adapter,
    )

    if response.provider_error_code is not None:
        violation_code = (
            JobAnalysisViolationCode.RETRY_EXHAUSTED
            if len(attempts) > 1
            else JobAnalysisViolationCode.PROVIDER_ERROR
        )
        blocked = BlockedJobAnalysisItem(
            blocked_item_fingerprint=compute_blocked_job_analysis_item_fingerprint(
                item_kind=JobAnalysisItemKind.PROVIDER_LEVEL,
                source_response_item_id=None,
                violation_code=violation_code,
                provider_error_code=response.provider_error_code,
            ),
            item_kind=JobAnalysisItemKind.PROVIDER_LEVEL,
            violation_code=violation_code,
            provider_error_code=response.provider_error_code,
            detail=f"provider error: {response.provider_error_code.value}",
        )
        result_fingerprint = compute_job_analysis_result_fingerprint(
            status=JobPostingAnalysisStatus.PROVIDER_ERROR,
            posting_fingerprint=posting_fingerprint,
            analysis_context_fingerprint=analysis_context_fingerprint,
            objective_fingerprint=None,
            responsibility_fingerprints=(),
            outcome_fingerprints=(),
            competency_fingerprints=(),
            hypothesis_fingerprints=(),
            success_indicator_fingerprints=(),
            operating_context_fingerprints=(),
            evidence_priority_fingerprints=(),
            ambiguity_fingerprints=(),
            blocked_item_fingerprints=(blocked.blocked_item_fingerprint,),
        )
        return JobPostingAnalysisResult(
            result_fingerprint=result_fingerprint,
            status=JobPostingAnalysisStatus.PROVIDER_ERROR,
            posting_fingerprint=posting_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            analysis_context_fingerprint=analysis_context_fingerprint,
            blocked_items=(blocked,),
            ready_for_candidacy_thesis=False,
        )

    structured = response.structured_response
    if structured is None:
        return _empty_result(
            status=JobPostingAnalysisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            analysis_context_fingerprint=analysis_context_fingerprint,
            violations=(
                JobAnalysisResultViolation(
                    violation_code=JobAnalysisViolationCode.RESPONSE_SCHEMA_MISMATCH,
                    detail="adapter returned no structured_response",
                ),
            ),
        )

    if structured.posting_fingerprint != posting_fingerprint:
        return _empty_result(
            status=JobPostingAnalysisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            analysis_context_fingerprint=analysis_context_fingerprint,
            violations=(
                JobAnalysisResultViolation(
                    violation_code=JobAnalysisViolationCode.RESPONSE_SCHEMA_MISMATCH,
                    detail="response.posting_fingerprint does not match the request's posting_fingerprint",
                ),
            ),
        )

    # Partition: every response_item_id must be globally unique across the
    # whole response -- a duplicate is a structural input failure, not a
    # per-item concern.
    all_ids: list[str] = []
    if structured.role_objective is not None:
        all_ids.append(structured.role_objective.response_item_id)
    for bucket in (
        structured.responsibilities,
        structured.outcomes,
        structured.competencies,
        structured.hypotheses,
        structured.success_indicators,
        structured.operating_context,
        structured.evidence_priorities,
        structured.ambiguities,
    ):
        all_ids.extend(item.response_item_id for item in bucket)
    if len(all_ids) != len(set(all_ids)):
        return _empty_result(
            status=JobPostingAnalysisStatus.INVALID_INPUT,
            posting_fingerprint=posting_fingerprint,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            analysis_context_fingerprint=analysis_context_fingerprint,
            violations=(
                JobAnalysisResultViolation(
                    violation_code=JobAnalysisViolationCode.DUPLICATE_RESPONSE_ITEM_ID,
                    detail="the response contains duplicate response_item_id values",
                ),
            ),
        )

    def _valid_segment_ids(ids: Sequence[str]) -> bool:
        return all(i in segments_by_id for i in ids)

    blocked_items: list[BlockedJobAnalysisItem] = []

    def _block(
        *, kind: JobAnalysisItemKind, response_item_id: str, code: JobAnalysisViolationCode, detail: str
    ) -> None:
        blocked_items.append(
            BlockedJobAnalysisItem(
                blocked_item_fingerprint=compute_blocked_job_analysis_item_fingerprint(
                    item_kind=kind, source_response_item_id=response_item_id, violation_code=code
                ),
                item_kind=kind,
                source_response_item_id=response_item_id,
                violation_code=code,
                detail=detail,
            )
        )

    # -- role objective (0 or 1) --
    role_objective: RoleObjective | None = None
    if structured.role_objective is None:
        blocked_items.append(
            BlockedJobAnalysisItem(
                blocked_item_fingerprint=compute_blocked_job_analysis_item_fingerprint(
                    item_kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                    source_response_item_id=None,
                    violation_code=JobAnalysisViolationCode.NO_ROLE_OBJECTIVE,
                ),
                item_kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                source_response_item_id=None,
                violation_code=JobAnalysisViolationCode.NO_ROLE_OBJECTIVE,
                detail="the response carries no role_objective",
            )
        )
    else:
        item = structured.role_objective
        if not item.concise_statement.strip():
            _block(
                kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.EMPTY_STATEMENT,
                detail="concise_statement is blank",
            )
        elif not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="role_objective carries no supporting evidence",
            )
        elif not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="role_objective references an unknown segment id",
            )
        elif statement_equals_role_title(item.concise_statement, snapshot.role_title):
            _block(
                kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.OBJECTIVE_EQUALS_ROLE_TITLE,
                detail="concise_statement is identical to role_title after normalization",
            )
        elif item.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING and not (
            explicit_claim_is_supported_by_cited_evidence(
                item.concise_statement,
                _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids),
            )
        ):
            _block(
                kind=JobAnalysisItemKind.ROLE_OBJECTIVE,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail=(
                    "assertion_basis=EXPLICIT_IN_POSTING but concise_statement is not "
                    "directly supported by its own cited evidence segments"
                ),
            )
        else:
            fp = compute_role_objective_fingerprint(
                posting_fingerprint=posting_fingerprint,
                concise_statement=item.concise_statement,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
            )
            role_objective = RoleObjective(
                objective_id=fp,
                concise_statement=item.concise_statement,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                objective_fingerprint=fp,
            )

    # -- responsibilities (dedupe by content fingerprint) --
    responsibilities: dict[str, RoleResponsibility] = {}
    for item in structured.responsibilities:
        if not item.statement.strip():
            _block(
                kind=JobAnalysisItemKind.RESPONSIBILITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.EMPTY_STATEMENT,
                detail="statement is blank",
            )
            continue
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.RESPONSIBILITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="responsibility carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.RESPONSIBILITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="responsibility references an unknown segment id",
            )
            continue
        if item.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING and not (
            explicit_claim_is_supported_by_cited_evidence(
                item.statement, _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids)
            )
        ):
            _block(
                kind=JobAnalysisItemKind.RESPONSIBILITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail=(
                    "assertion_basis=EXPLICIT_IN_POSTING but statement is not directly "
                    "supported by its own cited evidence segments"
                ),
            )
            continue
        fp = compute_role_responsibility_fingerprint(
            posting_fingerprint=posting_fingerprint,
            statement=item.statement,
            assertion_basis=item.assertion_basis,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
            priority_order=item.priority_order,
        )
        if fp not in responsibilities:
            responsibilities[fp] = RoleResponsibility(
                responsibility_id=fp,
                statement=item.statement,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                priority_order=item.priority_order,
                responsibility_fingerprint=fp,
            )

    # -- expected business outcomes --
    outcomes: dict[str, ExpectedBusinessOutcome] = {}
    for item in structured.outcomes:
        if not item.expected_change.strip():
            _block(
                kind=JobAnalysisItemKind.OUTCOME,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.EMPTY_STATEMENT,
                detail="expected_change is blank",
            )
            continue
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.OUTCOME,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="outcome carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.OUTCOME,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="outcome references an unknown segment id",
            )
            continue
        evidence_texts = _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids)
        if item.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING and not (
            explicit_claim_is_supported_by_cited_evidence(item.expected_change, evidence_texts)
        ):
            _block(
                kind=JobAnalysisItemKind.OUTCOME,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail=(
                    "assertion_basis=EXPLICIT_IN_POSTING but expected_change is not "
                    "directly supported by its own cited evidence segments"
                ),
            )
            continue
        if not numeric_claim_is_supported(item.expected_change, evidence_texts):
            _block(
                kind=JobAnalysisItemKind.OUTCOME,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNSUPPORTED_NUMERIC_OUTCOME,
                detail="expected_change carries a number not present in any cited evidence segment",
            )
            continue
        fp = compute_expected_outcome_fingerprint(
            posting_fingerprint=posting_fingerprint,
            expected_change=item.expected_change,
            affected_business_area=item.affected_business_area,
            assertion_basis=item.assertion_basis,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
        )
        if fp not in outcomes:
            outcomes[fp] = ExpectedBusinessOutcome(
                outcome_id=fp,
                expected_change=item.expected_change,
                affected_business_area=item.affected_business_area,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                outcome_fingerprint=fp,
            )

    # -- required competencies --
    competencies: dict[str, RequiredRoleCompetency] = {}
    for item in structured.competencies:
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.COMPETENCY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="competency carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.COMPETENCY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="competency references an unknown segment id",
            )
            continue
        if item.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING and not (
            explicit_claim_is_supported_by_cited_evidence(
                item.competency_name,
                _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids),
            )
        ):
            _block(
                kind=JobAnalysisItemKind.COMPETENCY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail=(
                    "assertion_basis=EXPLICIT_IN_POSTING but competency_name is not "
                    "directly supported by its own cited evidence segments"
                ),
            )
            continue
        fp = compute_required_competency_fingerprint(
            posting_fingerprint=posting_fingerprint,
            competency_name=item.competency_name,
            competency_class=item.competency_class.value,
            assertion_basis=item.assertion_basis,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
        )
        if fp not in competencies:
            competencies[fp] = RequiredRoleCompetency(
                competency_id=fp,
                competency_name=item.competency_name,
                competency_class=item.competency_class,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                competency_fingerprint=fp,
            )

    # -- employer problem hypotheses --
    hypotheses: dict[str, EmployerProblemHypothesis] = {}
    for item in structured.hypotheses:
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.HYPOTHESIS,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="hypothesis carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.HYPOTHESIS,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="hypothesis references an unknown segment id",
            )
            continue
        evidence_texts = _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids)
        if not claim_is_supported_by_evidence(item.problem_statement, evidence_texts):
            _block(
                kind=JobAnalysisItemKind.HYPOTHESIS,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.OVERCLAIM_UNSUPPORTED_HYPOTHESIS,
                detail="problem_statement makes an unsupported absolute employer claim",
            )
            continue
        guard_status = OverclaimGuardStatus.PASSED
        fp = compute_employer_problem_hypothesis_fingerprint(
            posting_fingerprint=posting_fingerprint,
            problem_statement=item.problem_statement,
            affected_business_area=item.affected_business_area,
            observable_signals_in_posting=item.observable_signals_in_posting,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
            overclaim_guard_status=guard_status,
        )
        if fp not in hypotheses:
            hypotheses[fp] = EmployerProblemHypothesis(
                hypothesis_id=fp,
                problem_statement=item.problem_statement,
                affected_business_area=item.affected_business_area,
                observable_signals_in_posting=item.observable_signals_in_posting,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                overclaim_guard_status=guard_status,
                hypothesis_fingerprint=fp,
            )

    # -- success indicators --
    success_indicators: dict[str, RoleSuccessIndicator] = {}
    for item in structured.success_indicators:
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.SUCCESS_INDICATOR,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="success indicator carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.SUCCESS_INDICATOR,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="success indicator references an unknown segment id",
            )
            continue
        if item.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING and not (
            explicit_claim_is_supported_by_cited_evidence(
                item.indicator_statement,
                _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids),
            )
        ):
            _block(
                kind=JobAnalysisItemKind.SUCCESS_INDICATOR,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail=(
                    "assertion_basis=EXPLICIT_IN_POSTING but indicator_statement is not "
                    "directly supported by its own cited evidence segments"
                ),
            )
            continue
        fp = compute_role_success_indicator_fingerprint(
            posting_fingerprint=posting_fingerprint,
            indicator_statement=item.indicator_statement,
            assertion_basis=item.assertion_basis,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
        )
        if fp not in success_indicators:
            success_indicators[fp] = RoleSuccessIndicator(
                success_indicator_id=fp,
                indicator_statement=item.indicator_statement,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                success_indicator_fingerprint=fp,
            )

    # -- operating context --
    operating_context: dict[str, RoleOperatingContext] = {}
    for item in structured.operating_context:
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.OPERATING_CONTEXT,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="operating context carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.OPERATING_CONTEXT,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="operating context references an unknown segment id",
            )
            continue
        if item.assertion_basis == AnalysisAssertionBasis.EXPLICIT_IN_POSTING and not (
            explicit_claim_is_supported_by_cited_evidence(
                item.context_statement,
                _evidence_texts(segments_by_id, item.supporting_evidence_segment_ids),
            )
        ):
            _block(
                kind=JobAnalysisItemKind.OPERATING_CONTEXT,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail=(
                    "assertion_basis=EXPLICIT_IN_POSTING but context_statement is not "
                    "directly supported by its own cited evidence segments"
                ),
            )
            continue
        fp = compute_role_operating_context_fingerprint(
            posting_fingerprint=posting_fingerprint,
            context_statement=item.context_statement,
            assertion_basis=item.assertion_basis,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
        )
        if fp not in operating_context:
            operating_context[fp] = RoleOperatingContext(
                operating_context_id=fp,
                context_statement=item.context_statement,
                assertion_basis=item.assertion_basis,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                operating_context_fingerprint=fp,
            )

    # -- evidence priorities --
    evidence_priorities: dict[str, RoleEvidencePriority] = {}
    for item in structured.evidence_priorities:
        if not item.supporting_evidence_segment_ids:
            _block(
                kind=JobAnalysisItemKind.EVIDENCE_PRIORITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="evidence priority carries no supporting evidence",
            )
            continue
        if not _valid_segment_ids(item.supporting_evidence_segment_ids):
            _block(
                kind=JobAnalysisItemKind.EVIDENCE_PRIORITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="evidence priority references an unknown segment id",
            )
            continue
        fp = compute_role_evidence_priority_fingerprint(
            posting_fingerprint=posting_fingerprint,
            priority_theme=item.priority_theme,
            rationale=item.rationale,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
            priority_rank=item.priority_rank,
        )
        if fp not in evidence_priorities:
            evidence_priorities[fp] = RoleEvidencePriority(
                evidence_priority_id=fp,
                priority_theme=item.priority_theme,
                rationale=item.rationale,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                priority_rank=item.priority_rank,
                evidence_priority_fingerprint=fp,
            )

    # -- ambiguities --
    ambiguities: dict[str, JobPostingAmbiguity] = {}
    for item in structured.ambiguities:
        if not item.supporting_evidence_segment_ids and not item.explicit_absence_of_information:
            _block(
                kind=JobAnalysisItemKind.AMBIGUITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.MISSING_EVIDENCE,
                detail="ambiguity carries neither evidence nor explicit_absence_of_information",
            )
            continue
        if item.supporting_evidence_segment_ids and not _valid_segment_ids(
            item.supporting_evidence_segment_ids
        ):
            _block(
                kind=JobAnalysisItemKind.AMBIGUITY,
                response_item_id=item.response_item_id,
                code=JobAnalysisViolationCode.UNKNOWN_SEGMENT,
                detail="ambiguity references an unknown segment id",
            )
            continue
        fp = compute_job_posting_ambiguity_fingerprint(
            posting_fingerprint=posting_fingerprint,
            ambiguity_type=item.ambiguity_type.value,
            description=item.description,
            supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
            explicit_absence_of_information=item.explicit_absence_of_information,
        )
        if fp not in ambiguities:
            ambiguities[fp] = JobPostingAmbiguity(
                ambiguity_id=fp,
                ambiguity_type=item.ambiguity_type,
                description=item.description,
                supporting_evidence_segment_ids=item.supporting_evidence_segment_ids,
                explicit_absence_of_information=item.explicit_absence_of_information,
                ambiguity_fingerprint=fp,
            )

    raw_response_fingerprint = _compute_raw_response_fingerprint(response.raw_response_text)
    structured_response_fingerprint = _compute_structured_response_fingerprint(structured)
    provenance = JobAnalysisProvenance(
        schema_version=JOB_ANALYSIS_SCHEMA_VERSION,
        policy_version=JOB_ANALYSIS_POLICY_VERSION,
        prompt_version=request.prompt_template_version,
        response_schema_version=JOB_ANALYSIS_RESPONSE_SCHEMA_VERSION,
        context_fingerprint=analysis_context_fingerprint,
        posting_fingerprint=posting_fingerprint,
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
        status = JobPostingAnalysisStatus.BLOCKED
    elif role_objective is None:
        status = JobPostingAnalysisStatus.BLOCKED
    else:
        status = JobPostingAnalysisStatus.ANALYZED

    result_fingerprint = compute_job_analysis_result_fingerprint(
        status=status,
        posting_fingerprint=posting_fingerprint,
        analysis_context_fingerprint=analysis_context_fingerprint,
        objective_fingerprint=role_objective.objective_fingerprint if role_objective else None,
        responsibility_fingerprints=[r.responsibility_fingerprint for r in responsibilities.values()],
        outcome_fingerprints=[o.outcome_fingerprint for o in outcomes.values()],
        competency_fingerprints=[c.competency_fingerprint for c in competencies.values()],
        hypothesis_fingerprints=[h.hypothesis_fingerprint for h in hypotheses.values()],
        success_indicator_fingerprints=[s.success_indicator_fingerprint for s in success_indicators.values()],
        operating_context_fingerprints=[o.operating_context_fingerprint for o in operating_context.values()],
        evidence_priority_fingerprints=[e.evidence_priority_fingerprint for e in evidence_priorities.values()],
        ambiguity_fingerprints=[a.ambiguity_fingerprint for a in ambiguities.values()],
        blocked_item_fingerprints=[b.blocked_item_fingerprint for b in blocked_items],
    )

    ready_for_candidacy_thesis = (
        status == JobPostingAnalysisStatus.ANALYZED and role_objective is not None
    )

    return JobPostingAnalysisResult(
        result_fingerprint=result_fingerprint,
        status=status,
        posting_fingerprint=posting_fingerprint,
        snapshot_schema_version=snapshot.snapshot_schema_version,
        analysis_context_fingerprint=analysis_context_fingerprint,
        role_objective=role_objective,
        responsibilities=tuple(responsibilities.values()),
        outcomes=tuple(outcomes.values()),
        competencies=tuple(competencies.values()),
        hypotheses=tuple(hypotheses.values()),
        success_indicators=tuple(success_indicators.values()),
        operating_context=tuple(operating_context.values()),
        evidence_priorities=tuple(evidence_priorities.values()),
        ambiguities=tuple(ambiguities.values()),
        blocked_items=tuple(blocked_items),
        provenance=provenance,
        ready_for_candidacy_thesis=ready_for_candidacy_thesis,
    )
