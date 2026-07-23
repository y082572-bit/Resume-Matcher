"""Pure replay and stale-result detection for Candidacy Thesis.

No function here reads a clock, performs a lookup, or touches the
database. Staleness is judged only by comparing a
``CandidacyThesisReplayInput`` derived from a previously computed
``CandidacyThesisResult`` against the caller's current values for those
same fields.

A change to an approved fact's payload makes the *thesis* stale but never
retroactively invalidates the underlying Job Posting Analysis -- Job
Analysis freshness depends only on the posting, never on Marek's facts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.schemas.candidacy_thesis import (
    CandidacyThesisFreshnessStatus,
    CandidacyThesisReplayInput,
    CandidacyThesisReplayResult,
    CandidacyThesisResult,
    PositioningLevel,
    RoleEvidenceCategory,
)
from app.services.truth_fingerprint import canonical_json_bytes


REPLAY_INPUT_FINGERPRINT_VERSION = "candidacy-thesis-replay-input-v1"
POSITIONING_CONTEXT_FINGERPRINT_VERSION = "candidacy-thesis-positioning-context-v1"


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def compute_positioning_context_fingerprint(
    *,
    requested_positioning_level: PositioningLevel,
    priority_evidence_categories: Sequence[RoleEvidenceCategory],
) -> str:
    content = {
        "requested_positioning_level": requested_positioning_level.value,
        "priority_evidence_categories": [c.value for c in priority_evidence_categories],
    }
    return _fingerprint(POSITIONING_CONTEXT_FINGERPRINT_VERSION, content)


def compute_candidacy_thesis_replay_input_fingerprint(
    replay_input: CandidacyThesisReplayInput,
) -> str:
    return _fingerprint(REPLAY_INPUT_FINGERPRINT_VERSION, replay_input.model_dump(mode="json"))


def build_candidacy_thesis_replay_input(
    *,
    job_analysis_result_fingerprint: str,
    job_analysis_replay_fingerprint: str,
    posting_fingerprint: str,
    approved_payload_set_fingerprint: str,
    supporting_fact_payload_fingerprints: Sequence[str],
    hypothesis_fingerprints: Sequence[str],
    role_objective_fingerprint: str,
    positioning_context_fingerprint: str,
    evidence_priority_fingerprints: Sequence[str],
    thesis_statement_fingerprint: str | None,
    evidence_mapping_fingerprints: Sequence[str],
    strategic_contribution_fingerprints: Sequence[str],
    model_configuration_fingerprint: str,
    prompt_version: str,
    blocked_item_fingerprints: Sequence[str],
    result_fingerprint: str,
) -> CandidacyThesisReplayInput:
    return CandidacyThesisReplayInput(
        job_analysis_result_fingerprint=job_analysis_result_fingerprint,
        job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
        posting_fingerprint=posting_fingerprint,
        approved_payload_set_fingerprint=approved_payload_set_fingerprint,
        supporting_fact_payload_fingerprints=tuple(sorted(supporting_fact_payload_fingerprints)),
        hypothesis_fingerprints=tuple(sorted(hypothesis_fingerprints)),
        role_objective_fingerprint=role_objective_fingerprint,
        positioning_context_fingerprint=positioning_context_fingerprint,
        evidence_priority_fingerprints=tuple(sorted(evidence_priority_fingerprints)),
        thesis_statement_fingerprint=thesis_statement_fingerprint,
        evidence_mapping_fingerprints=tuple(sorted(evidence_mapping_fingerprints)),
        strategic_contribution_fingerprints=tuple(sorted(strategic_contribution_fingerprints)),
        model_configuration_fingerprint=model_configuration_fingerprint,
        prompt_version=prompt_version,
        blocked_item_fingerprints=tuple(sorted(blocked_item_fingerprints)),
        result_fingerprint=result_fingerprint,
    )


def replay_input_from_candidacy_thesis_result(
    *,
    job_analysis_replay_fingerprint: str,
    approved_payload_set_fingerprint: str,
    positioning_context_fingerprint: str,
    evidence_priority_fingerprints: Sequence[str],
    model_configuration_fingerprint: str,
    prompt_version: str,
    result: CandidacyThesisResult,
) -> CandidacyThesisReplayInput:
    """Reconstruct the staleness-relevant fields from an already-built
    ``CandidacyThesisResult`` plus the exact external inputs it was
    computed from. Never re-derives those external inputs from a database
    or from ``result`` alone."""

    return build_candidacy_thesis_replay_input(
        job_analysis_result_fingerprint=result.analysis_result_fingerprint,
        job_analysis_replay_fingerprint=job_analysis_replay_fingerprint,
        posting_fingerprint=result.posting_fingerprint,
        approved_payload_set_fingerprint=approved_payload_set_fingerprint,
        supporting_fact_payload_fingerprints=[
            m.payload_fingerprint for m in result.evidence_mappings
        ],
        hypothesis_fingerprints=result.employer_problem_hypothesis_fingerprints,
        role_objective_fingerprint=result.role_objective_fingerprint or "",
        positioning_context_fingerprint=positioning_context_fingerprint,
        evidence_priority_fingerprints=evidence_priority_fingerprints,
        thesis_statement_fingerprint=(
            result.thesis_statement.thesis_fingerprint if result.thesis_statement else None
        ),
        evidence_mapping_fingerprints=[
            m.evidence_mapping_fingerprint for m in result.evidence_mappings
        ],
        strategic_contribution_fingerprints=[
            s.contribution_fingerprint for s in result.strategic_contribution_statements
        ],
        model_configuration_fingerprint=model_configuration_fingerprint,
        prompt_version=prompt_version,
        blocked_item_fingerprints=[b.blocked_item_fingerprint for b in result.blocked_items],
        result_fingerprint=result.result_fingerprint,
    )


def stale_candidacy_thesis_fields(
    previous: CandidacyThesisReplayInput, current: CandidacyThesisReplayInput
) -> tuple[str, ...]:
    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_candidacy_thesis_stale(
    previous: CandidacyThesisReplayInput, current: CandidacyThesisReplayInput
) -> bool:
    return previous != current


def evaluate_candidacy_thesis_freshness(
    previous: CandidacyThesisReplayInput,
    current: CandidacyThesisReplayInput | None,
) -> CandidacyThesisReplayResult:
    if current is None:
        return CandidacyThesisReplayResult(
            freshness_status=CandidacyThesisFreshnessStatus.FRESHNESS_NOT_VERIFIED, stale_fields=()
        )
    if previous == current:
        return CandidacyThesisReplayResult(
            freshness_status=CandidacyThesisFreshnessStatus.FRESH, stale_fields=()
        )
    return CandidacyThesisReplayResult(
        freshness_status=CandidacyThesisFreshnessStatus.STALE,
        stale_fields=stale_candidacy_thesis_fields(previous, current),
    )
