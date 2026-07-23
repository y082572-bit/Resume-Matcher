"""Full, independent PRE-P4 revalidation ahead of strategy-aware P3.

Nothing here trusts a caller-supplied ``ready_for_candidacy_thesis``,
``ready_for_p4``, or stored fingerprint: every fingerprint is re-derived
from the exact upstream inputs (``JobPostingSnapshot``, evidence segments,
``JobAnalysisContext``/``CandidacyThesisContext``) and independently
compared. This module is pure and in-memory: no clock, no lookup, no
database, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas.candidacy_thesis import (
    CandidacyThesisReplayInput,
    CandidacyThesisResult,
    CandidacyThesisStatus,
)
from app.schemas.job_posting_analysis import (
    JobAnalysisReplayInput,
    JobPostingAnalysisResult,
    JobPostingAnalysisStatus,
)
from app.schemas.strategy_fact_selection import RoleStrategyFactSelectionInput
from app.services.candidacy_thesis_builder import (
    compute_approved_fact_payload_set_fingerprint,
    compute_candidacy_evidence_mapping_fingerprint,
    compute_candidacy_thesis_model_configuration_fingerprint,
    compute_candidacy_thesis_result_fingerprint,
    compute_role_strategy_context_fingerprint,
)
from app.services.candidacy_thesis_replay import (
    compute_candidacy_thesis_replay_input_fingerprint,
    compute_positioning_context_fingerprint,
    evaluate_candidacy_thesis_freshness,
    replay_input_from_candidacy_thesis_result,
)
from app.services.job_analysis_builder import (
    compute_job_analysis_context_fingerprint,
    compute_job_analysis_result_fingerprint,
)
from app.services.job_analysis_replay import (
    compute_job_analysis_replay_input_fingerprint,
    evaluate_job_analysis_freshness,
    replay_input_from_job_analysis_result,
)
from app.services.job_posting_snapshot import recompute_job_posting_snapshot_fingerprint


class Prep4RevalidationViolationCode(StrEnum):
    JOB_ANALYSIS_SNAPSHOT_FINGERPRINT_MISMATCH = "JOB_ANALYSIS_SNAPSHOT_FINGERPRINT_MISMATCH"
    JOB_ANALYSIS_CONTEXT_FINGERPRINT_MISMATCH = "JOB_ANALYSIS_CONTEXT_FINGERPRINT_MISMATCH"
    JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH = "JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH"
    JOB_ANALYSIS_NOT_READY = "JOB_ANALYSIS_NOT_READY"
    JOB_ANALYSIS_NOT_FRESH = "JOB_ANALYSIS_NOT_FRESH"
    CANDIDACY_THESIS_CONTEXT_FINGERPRINT_MISMATCH = "CANDIDACY_THESIS_CONTEXT_FINGERPRINT_MISMATCH"
    CANDIDACY_THESIS_RESULT_FINGERPRINT_MISMATCH = "CANDIDACY_THESIS_RESULT_FINGERPRINT_MISMATCH"
    CANDIDACY_THESIS_EVIDENCE_MAPPING_FINGERPRINT_MISMATCH = (
        "CANDIDACY_THESIS_EVIDENCE_MAPPING_FINGERPRINT_MISMATCH"
    )
    CANDIDACY_THESIS_NOT_READY = "CANDIDACY_THESIS_NOT_READY"
    CANDIDACY_THESIS_NOT_FRESH = "CANDIDACY_THESIS_NOT_FRESH"
    APPROVED_PAYLOAD_SET_FINGERPRINT_MISMATCH = "APPROVED_PAYLOAD_SET_FINGERPRINT_MISMATCH"
    ROLE_STRATEGY_CONTEXT_FINGERPRINT_MISMATCH = "ROLE_STRATEGY_CONTEXT_FINGERPRINT_MISMATCH"
    ROLE_STRATEGY_CONTEXT_ANALYSIS_RESULT_MISMATCH = (
        "ROLE_STRATEGY_CONTEXT_ANALYSIS_RESULT_MISMATCH"
    )
    ROLE_STRATEGY_CONTEXT_THESIS_RESULT_MISMATCH = "ROLE_STRATEGY_CONTEXT_THESIS_RESULT_MISMATCH"
    ROLE_STRATEGY_CONTEXT_POSTING_FINGERPRINT_MISMATCH = (
        "ROLE_STRATEGY_CONTEXT_POSTING_FINGERPRINT_MISMATCH"
    )
    ROLE_STRATEGY_CONTEXT_ROLE_OBJECTIVE_FINGERPRINT_MISMATCH = (
        "ROLE_STRATEGY_CONTEXT_ROLE_OBJECTIVE_FINGERPRINT_MISMATCH"
    )
    ROLE_STRATEGY_CONTEXT_HYPOTHESIS_FINGERPRINT_UNKNOWN = (
        "ROLE_STRATEGY_CONTEXT_HYPOTHESIS_FINGERPRINT_UNKNOWN"
    )
    ROLE_STRATEGY_CONTEXT_POSITIONING_LEVEL_MISMATCH = (
        "ROLE_STRATEGY_CONTEXT_POSITIONING_LEVEL_MISMATCH"
    )
    ROLE_STRATEGY_CONTEXT_PRIORITY_CATEGORIES_MISMATCH = (
        "ROLE_STRATEGY_CONTEXT_PRIORITY_CATEGORIES_MISMATCH"
    )
    ROLE_STRATEGY_CONTEXT_NARRATIVE_EMPHASIS_MISMATCH = (
        "ROLE_STRATEGY_CONTEXT_NARRATIVE_EMPHASIS_MISMATCH"
    )


@dataclass(frozen=True)
class Prep4RevalidationViolation:
    code: Prep4RevalidationViolationCode
    detail: str


class Prep4RevalidationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Prep4RevalidationResult:
    status: Prep4RevalidationStatus
    violations: tuple[Prep4RevalidationViolation, ...]
    job_analysis_replay_fingerprint: str | None
    candidacy_thesis_replay_fingerprint: str | None
    role_strategy_context_fingerprint: str | None


def revalidate_prep4(
    integration_input: RoleStrategyFactSelectionInput,
) -> Prep4RevalidationResult:
    """Independently re-derive and re-check every PRE-P4 fingerprint.

    Never trusts ``job_analysis_result.ready_for_candidacy_thesis`` or
    ``candidacy_thesis_result.ready_for_p4`` as the sole basis for a
    decision -- both are re-derived here from their own closed
    status/content invariants, and freshness is independently
    recomputed via the existing PRE-P4 replay helpers.
    """

    violations: list[Prep4RevalidationViolation] = []

    snapshot = integration_input.job_posting_snapshot
    segments = integration_input.job_evidence_segments
    job_analysis_context = integration_input.job_analysis_context
    job_analysis_result = integration_input.job_analysis_result
    thesis_context = integration_input.candidacy_thesis_context
    thesis_result = integration_input.candidacy_thesis_result
    role_strategy_context = integration_input.role_strategy_context
    payload_set = integration_input.approved_fact_payload_set

    # -- Job Analysis --
    if recompute_job_posting_snapshot_fingerprint(snapshot) != snapshot.snapshot_fingerprint:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.JOB_ANALYSIS_SNAPSHOT_FINGERPRINT_MISMATCH,
                "job_posting_snapshot.snapshot_fingerprint does not match its recomputed value",
            )
        )

    expected_job_analysis_context_fp = compute_job_analysis_context_fingerprint(
        job_analysis_context
    )
    if expected_job_analysis_context_fp != job_analysis_result.analysis_context_fingerprint:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.JOB_ANALYSIS_CONTEXT_FINGERPRINT_MISMATCH,
                "job_analysis_result.analysis_context_fingerprint does not match the "
                "recomputed job_analysis_context fingerprint",
            )
        )

    expected_job_analysis_result_fp = compute_job_analysis_result_fingerprint(
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
    if expected_job_analysis_result_fp != job_analysis_result.result_fingerprint:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.JOB_ANALYSIS_RESULT_FINGERPRINT_MISMATCH,
                "job_analysis_result.result_fingerprint does not match its recomputed value",
            )
        )

    job_analysis_ready = (
        job_analysis_result.status == JobPostingAnalysisStatus.ANALYZED
        and job_analysis_result.role_objective is not None
    )
    if not job_analysis_ready:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.JOB_ANALYSIS_NOT_READY,
                "job_analysis_result is not ANALYZED with a role_objective",
            )
        )

    job_analysis_replay_fingerprint: str | None = None
    if job_analysis_ready:
        previous_job_analysis_replay = replay_input_from_job_analysis_result(
            snapshot=snapshot,
            segments=segments,
            analysis_context=job_analysis_context,
            result=job_analysis_result,
        )
        job_analysis_freshness = evaluate_job_analysis_freshness(
            previous_job_analysis_replay, integration_input.current_job_analysis_replay_input
        )
        job_analysis_replay_fingerprint = compute_job_analysis_replay_input_fingerprint(
            previous_job_analysis_replay
        )
        if job_analysis_freshness.freshness_status.value != "FRESH":
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.JOB_ANALYSIS_NOT_FRESH,
                    f"job analysis freshness is {job_analysis_freshness.freshness_status.value}, "
                    f"stale_fields={job_analysis_freshness.stale_fields}",
                )
            )

    # -- Candidacy Thesis --
    # CandidacyThesisProvenance carries no single context_fingerprint field,
    # so the caller's current CandidacyThesisContext is instead cross-checked
    # against the individual provenance fields it must have produced.
    if thesis_result.provenance is not None:
        expected_model_configuration_fp = compute_candidacy_thesis_model_configuration_fingerprint(
            thesis_context.model_configuration
        )
        context_mismatch = (
            thesis_context.thesis_prompt_version != thesis_result.provenance.prompt_version
            or thesis_context.response_schema_version != thesis_result.provenance.response_schema_version
            or thesis_context.thesis_policy_version != thesis_result.provenance.policy_version
            or expected_model_configuration_fp != thesis_result.provenance.model_configuration_fingerprint
        )
        if context_mismatch:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.CANDIDACY_THESIS_CONTEXT_FINGERPRINT_MISMATCH,
                    "candidacy_thesis_context does not match candidacy_thesis_result.provenance",
                )
            )

    expected_payload_set_fp = compute_approved_fact_payload_set_fingerprint(payload_set.payloads)
    if expected_payload_set_fp != payload_set.payload_set_fingerprint:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.APPROVED_PAYLOAD_SET_FINGERPRINT_MISMATCH,
                "approved_fact_payload_set.payload_set_fingerprint does not match its "
                "recomputed value",
            )
        )
    if thesis_result.payload_set_fingerprint is not None and (
        thesis_result.payload_set_fingerprint != payload_set.payload_set_fingerprint
    ):
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.APPROVED_PAYLOAD_SET_FINGERPRINT_MISMATCH,
                "approved_fact_payload_set does not match the payload set the thesis was built from",
            )
        )

    for mapping in thesis_result.evidence_mappings:
        expected_mapping_fp = compute_candidacy_evidence_mapping_fingerprint(
            entity_id=mapping.entity_id,
            fact_id=mapping.fact_id,
            fact_revision=mapping.fact_revision,
            payload_fingerprint=mapping.payload_fingerprint,
            linked_hypothesis_fingerprint=mapping.linked_hypothesis_fingerprint,
            linked_role_objective_fingerprint=mapping.linked_role_objective_fingerprint,
            role_evidence_category=mapping.role_evidence_category,
            narrative_link=mapping.narrative_link,
        )
        if expected_mapping_fp != mapping.evidence_mapping_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.CANDIDACY_THESIS_EVIDENCE_MAPPING_FINGERPRINT_MISMATCH,
                    f"evidence mapping for fact_id={mapping.fact_id} fingerprint mismatch",
                )
            )

    expected_thesis_result_fp = compute_candidacy_thesis_result_fingerprint(
        status=thesis_result.status,
        posting_fingerprint=thesis_result.posting_fingerprint,
        analysis_result_fingerprint=thesis_result.analysis_result_fingerprint,
        payload_set_fingerprint=thesis_result.payload_set_fingerprint,
        thesis_statement_fingerprint=(
            thesis_result.thesis_statement.thesis_fingerprint
            if thesis_result.thesis_statement
            else None
        ),
        evidence_mapping_fingerprints=[
            m.evidence_mapping_fingerprint for m in thesis_result.evidence_mappings
        ],
        strategic_contribution_fingerprints=[
            s.contribution_fingerprint for s in thesis_result.strategic_contribution_statements
        ],
        positioning_level=thesis_result.positioning_level,
        target_narrative_emphasis=thesis_result.target_narrative_emphasis,
        blocked_item_fingerprints=[b.blocked_item_fingerprint for b in thesis_result.blocked_items],
    )
    if expected_thesis_result_fp != thesis_result.result_fingerprint:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.CANDIDACY_THESIS_RESULT_FINGERPRINT_MISMATCH,
                "candidacy_thesis_result.result_fingerprint does not match its recomputed value",
            )
        )
    if thesis_result.analysis_result_fingerprint != job_analysis_result.result_fingerprint:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.CANDIDACY_THESIS_RESULT_FINGERPRINT_MISMATCH,
                "candidacy_thesis_result.analysis_result_fingerprint does not match "
                "job_analysis_result.result_fingerprint",
            )
        )

    thesis_ready = (
        thesis_result.status == CandidacyThesisStatus.READY
        and thesis_result.thesis_statement is not None
        and thesis_result.role_strategy_context is not None
    )
    if not thesis_ready:
        violations.append(
            Prep4RevalidationViolation(
                Prep4RevalidationViolationCode.CANDIDACY_THESIS_NOT_READY,
                "candidacy_thesis_result is not READY with a thesis_statement and "
                "role_strategy_context",
            )
        )

    candidacy_thesis_replay_fingerprint: str | None = None
    if thesis_ready and job_analysis_ready and job_analysis_replay_fingerprint is not None:
        positioning_context_fingerprint = compute_positioning_context_fingerprint(
            requested_positioning_level=thesis_context.requested_positioning_level,
            priority_evidence_categories=thesis_context.priority_evidence_categories,
        )
        model_configuration_fingerprint = compute_candidacy_thesis_model_configuration_fingerprint(
            thesis_context.model_configuration
        )
        previous_thesis_replay = replay_input_from_candidacy_thesis_result(
            job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
            approved_payload_set_fingerprint=payload_set.payload_set_fingerprint,
            positioning_context_fingerprint=positioning_context_fingerprint,
            evidence_priority_fingerprints=[
                e.evidence_priority_fingerprint for e in job_analysis_result.evidence_priorities
            ],
            model_configuration_fingerprint=model_configuration_fingerprint,
            prompt_version=thesis_context.thesis_prompt_version,
            result=thesis_result,
        )
        thesis_freshness = evaluate_candidacy_thesis_freshness(
            previous_thesis_replay, integration_input.current_candidacy_thesis_replay_input
        )
        candidacy_thesis_replay_fingerprint = compute_candidacy_thesis_replay_input_fingerprint(
            previous_thesis_replay
        )
        if thesis_freshness.freshness_status.value != "FRESH":
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.CANDIDACY_THESIS_NOT_FRESH,
                    f"candidacy thesis freshness is {thesis_freshness.freshness_status.value}, "
                    f"stale_fields={thesis_freshness.stale_fields}",
                )
            )

    # -- RoleStrategyContext --
    role_strategy_context_fingerprint: str | None = None
    if job_analysis_ready and thesis_ready and job_analysis_replay_fingerprint is not None and (
        candidacy_thesis_replay_fingerprint is not None
    ):
        expected_context_fp = compute_role_strategy_context_fingerprint(
            job_analysis_result_fingerprint=role_strategy_context.job_analysis_result_fingerprint,
            job_analysis_replay_fingerprint=role_strategy_context.job_analysis_replay_fingerprint,
            candidacy_thesis_result_fingerprint=role_strategy_context.candidacy_thesis_result_fingerprint,
            candidacy_thesis_replay_fingerprint=role_strategy_context.candidacy_thesis_replay_fingerprint,
            posting_fingerprint=role_strategy_context.posting_fingerprint,
            role_objective_fingerprint=role_strategy_context.role_objective_fingerprint,
            employer_problem_hypothesis_fingerprints=role_strategy_context.employer_problem_hypothesis_fingerprints,
            priority_evidence_categories=role_strategy_context.priority_evidence_categories,
            positioning_level=role_strategy_context.positioning_level,
            target_narrative_emphasis=role_strategy_context.target_narrative_emphasis,
            approved_supporting_fact_fingerprints=role_strategy_context.approved_supporting_fact_fingerprints,
        )
        if expected_context_fp != role_strategy_context.context_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_FINGERPRINT_MISMATCH,
                    "role_strategy_context.context_fingerprint does not match its recomputed value",
                )
            )
        else:
            role_strategy_context_fingerprint = expected_context_fp

        if role_strategy_context.job_analysis_result_fingerprint != job_analysis_result.result_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_ANALYSIS_RESULT_MISMATCH,
                    "role_strategy_context.job_analysis_result_fingerprint does not match "
                    "job_analysis_result.result_fingerprint",
                )
            )
        if role_strategy_context.job_analysis_replay_fingerprint != job_analysis_replay_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_ANALYSIS_RESULT_MISMATCH,
                    "role_strategy_context.job_analysis_replay_fingerprint does not match the "
                    "recomputed job analysis replay fingerprint",
                )
            )
        if role_strategy_context.candidacy_thesis_result_fingerprint != thesis_result.result_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_THESIS_RESULT_MISMATCH,
                    "role_strategy_context.candidacy_thesis_result_fingerprint does not match "
                    "candidacy_thesis_result.result_fingerprint",
                )
            )
        if role_strategy_context.candidacy_thesis_replay_fingerprint != candidacy_thesis_replay_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_THESIS_RESULT_MISMATCH,
                    "role_strategy_context.candidacy_thesis_replay_fingerprint does not match "
                    "the recomputed candidacy thesis replay fingerprint",
                )
            )
        if role_strategy_context.posting_fingerprint != snapshot.snapshot_fingerprint:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_POSTING_FINGERPRINT_MISMATCH,
                    "role_strategy_context.posting_fingerprint does not match "
                    "job_posting_snapshot.snapshot_fingerprint",
                )
            )
        if job_analysis_result.role_objective is not None and (
            role_strategy_context.role_objective_fingerprint
            != job_analysis_result.role_objective.objective_fingerprint
        ):
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_ROLE_OBJECTIVE_FINGERPRINT_MISMATCH,
                    "role_strategy_context.role_objective_fingerprint does not match "
                    "job_analysis_result.role_objective.objective_fingerprint",
                )
            )
        known_hypothesis_fps = {h.hypothesis_fingerprint for h in job_analysis_result.hypotheses}
        unknown_hypothesis_fps = set(
            role_strategy_context.employer_problem_hypothesis_fingerprints
        ) - known_hypothesis_fps
        if unknown_hypothesis_fps:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_HYPOTHESIS_FINGERPRINT_UNKNOWN,
                    f"role_strategy_context references unknown hypothesis fingerprints: "
                    f"{sorted(unknown_hypothesis_fps)}",
                )
            )
        if role_strategy_context.positioning_level != thesis_result.positioning_level:
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_POSITIONING_LEVEL_MISMATCH,
                    "role_strategy_context.positioning_level does not match "
                    "candidacy_thesis_result.positioning_level",
                )
            )
        if set(role_strategy_context.priority_evidence_categories) != set(
            thesis_context.priority_evidence_categories
        ):
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_PRIORITY_CATEGORIES_MISMATCH,
                    "role_strategy_context.priority_evidence_categories does not match "
                    "candidacy_thesis_context.priority_evidence_categories",
                )
            )
        if set(role_strategy_context.target_narrative_emphasis) != set(
            thesis_result.target_narrative_emphasis
        ):
            violations.append(
                Prep4RevalidationViolation(
                    Prep4RevalidationViolationCode.ROLE_STRATEGY_CONTEXT_NARRATIVE_EMPHASIS_MISMATCH,
                    "role_strategy_context.target_narrative_emphasis does not match "
                    "candidacy_thesis_result.target_narrative_emphasis",
                )
            )

    status = (
        Prep4RevalidationStatus.PASSED if not violations else Prep4RevalidationStatus.FAILED
    )
    return Prep4RevalidationResult(
        status=status,
        violations=tuple(violations),
        job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
        candidacy_thesis_replay_fingerprint=candidacy_thesis_replay_fingerprint,
        role_strategy_context_fingerprint=role_strategy_context_fingerprint,
    )
