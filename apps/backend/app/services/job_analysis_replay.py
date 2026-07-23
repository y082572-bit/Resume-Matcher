"""Pure replay and stale-result detection for Job Posting Analysis.

No function here reads a clock, performs a lookup, or touches the
database. Staleness is judged only by comparing a ``JobAnalysisReplayInput``
derived from a previously computed ``JobPostingAnalysisResult`` against the
caller's current values for those same fields.
"""

from __future__ import annotations

import hashlib

from app.schemas.job_posting_analysis import (
    JobAnalysisReplayInput,
    JobAnalysisReplayResult,
    JobAnalysisFreshnessStatus,
    JobEvidenceSegment,
    JobPostingAnalysisResult,
    JobPostingSnapshot,
)
from app.services.job_analysis_builder import (
    compute_job_analysis_context_fingerprint,
    compute_job_analysis_model_configuration_fingerprint,
)
from app.services.job_posting_snapshot import compute_canonical_text_fingerprint
from app.services.truth_fingerprint import canonical_json_bytes


REPLAY_INPUT_FINGERPRINT_VERSION = "job-analysis-replay-input-v1"


def compute_job_analysis_replay_input_fingerprint(replay_input: JobAnalysisReplayInput) -> str:
    projection = {
        "fingerprint_version": REPLAY_INPUT_FINGERPRINT_VERSION,
        "content": replay_input.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def replay_input_from_job_analysis_result(
    *,
    snapshot: JobPostingSnapshot,
    segments: tuple[JobEvidenceSegment, ...],
    analysis_context,  # noqa: ANN001
    result: JobPostingAnalysisResult,
) -> JobAnalysisReplayInput:
    """Extract the staleness-relevant fields of a previously computed
    result. ``snapshot``/``segments``/``analysis_context`` are the exact
    inputs the result was computed from -- never re-derived from a database
    or from ``result`` alone."""

    return JobAnalysisReplayInput(
        posting_fingerprint=result.posting_fingerprint,
        canonical_text_fingerprint=compute_canonical_text_fingerprint(snapshot.canonical_text),
        snapshot_schema_version=result.snapshot_schema_version,
        evidence_segment_fingerprints=tuple(
            sorted(segment.segment_fingerprint for segment in segments)
        ),
        analysis_context_fingerprint=compute_job_analysis_context_fingerprint(analysis_context),
        model_configuration_fingerprint=compute_job_analysis_model_configuration_fingerprint(
            analysis_context.model_configuration
        ),
        prompt_version=analysis_context.prompt_template_version,
        response_schema_version=analysis_context.response_schema_version,
        objective_fingerprint=result.role_objective.objective_fingerprint
        if result.role_objective
        else None,
        responsibility_fingerprints=tuple(
            sorted(r.responsibility_fingerprint for r in result.responsibilities)
        ),
        outcome_fingerprints=tuple(sorted(o.outcome_fingerprint for o in result.outcomes)),
        competency_fingerprints=tuple(sorted(c.competency_fingerprint for c in result.competencies)),
        hypothesis_fingerprints=tuple(sorted(h.hypothesis_fingerprint for h in result.hypotheses)),
        success_indicator_fingerprints=tuple(
            sorted(s.success_indicator_fingerprint for s in result.success_indicators)
        ),
        operating_context_fingerprints=tuple(
            sorted(o.operating_context_fingerprint for o in result.operating_context)
        ),
        evidence_priority_fingerprints=tuple(
            sorted(e.evidence_priority_fingerprint for e in result.evidence_priorities)
        ),
        ambiguity_fingerprints=tuple(sorted(a.ambiguity_fingerprint for a in result.ambiguities)),
        blocked_item_fingerprints=tuple(
            sorted(b.blocked_item_fingerprint for b in result.blocked_items)
        ),
        result_fingerprint=result.result_fingerprint,
    )


def stale_job_analysis_fields(
    previous: JobAnalysisReplayInput, current: JobAnalysisReplayInput
) -> tuple[str, ...]:
    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_job_analysis_stale(
    previous: JobAnalysisReplayInput, current: JobAnalysisReplayInput
) -> bool:
    return previous != current


def evaluate_job_analysis_freshness(
    previous: JobAnalysisReplayInput,
    current: JobAnalysisReplayInput | None,
) -> JobAnalysisReplayResult:
    """``current=None`` means no current replay input was supplied at all
    -- freshness is then ``FRESHNESS_NOT_VERIFIED``, never guessed as
    ``FRESH``."""

    if current is None:
        return JobAnalysisReplayResult(
            freshness_status=JobAnalysisFreshnessStatus.FRESHNESS_NOT_VERIFIED, stale_fields=()
        )
    if previous == current:
        return JobAnalysisReplayResult(
            freshness_status=JobAnalysisFreshnessStatus.FRESH, stale_fields=()
        )
    return JobAnalysisReplayResult(
        freshness_status=JobAnalysisFreshnessStatus.STALE,
        stale_fields=stale_job_analysis_fields(previous, current),
    )
