"""Pure replay and stale-result detection for P5b controlled proposals.

No function here reads a clock, performs a lookup, or touches the
database. Staleness is judged only by comparing a
``CvContentProposalReplayInput`` derived from a previously computed
``CvContentProposalGenerationResult`` against the caller's current values
for those same fields.
"""

from __future__ import annotations

import hashlib

from app.schemas.cv_content_generation import CvContentGenerationReplayInput
from app.schemas.cv_content_plan import CvContentPlan
from app.schemas.cv_content_proposal import (
    CvContentProposalGenerationResult,
    CvContentProposalReplayInput,
    CvContentProposalReplayResult,
    ProposalFreshnessStatus,
    ProposalGenerationContext,
)
from app.services.cv_content_proposal_builder import compute_proposal_generation_context_fingerprint
from app.services.cv_content_proposal_prompt import compute_target_role_context_fingerprint
from app.services.truth_fingerprint import canonical_json_bytes


P5A_REPLAY_INPUT_FINGERPRINT_VERSION = "cv-content-proposal-p5a-replay-input-v1"


def compute_p5a_replay_input_fingerprint(replay_input: CvContentGenerationReplayInput) -> str:
    """Fold P5a's own replay input into a single fingerprint for embedding
    in P5b's replay input -- P5b never re-derives P5a's replay fields
    itself, only hashes the exact snapshot it was given."""

    content = {
        "fingerprint_version": P5A_REPLAY_INPUT_FINGERPRINT_VERSION,
        "content": replay_input.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def replay_input_from_proposal_result(
    *,
    plan: CvContentPlan,
    content_policy_version: str,
    p5a_generation_schema_version: str,
    p5a_generation_policy_version: str,
    p5a_replay_input: CvContentGenerationReplayInput,
    proposal_schema_version: str,
    proposal_policy_version: str,
    fact_payload_set_fingerprint: str,
    proposal_context: ProposalGenerationContext,
    result: CvContentProposalGenerationResult,
) -> CvContentProposalReplayInput:
    """Extract the staleness-relevant fields of a previously computed result.

    ``plan``/``p5a_replay_input``/``proposal_context`` are the exact inputs
    the result was computed from -- this function never re-derives them
    from a database or from ``result`` alone, since a result carries only
    fingerprints, never the full source objects.
    """

    eligible_blocked_item_fingerprints = sorted(
        {p.blocked_generation_item_fingerprint for p in result.proposals}
        | {b.blocked_generation_item_fingerprint for b in result.blocked_proposal_items}
    )
    immutable_constraint_set_fingerprints = sorted(
        {p.immutable_constraint_set_fingerprint for p in result.proposals}
    )
    prompt_template_versions = sorted({p.prompt_template_version for p in result.proposals})
    model_configuration_fingerprints = sorted(
        {p.model_configuration_fingerprint for p in result.proposals}
    )
    proposal_fingerprints = tuple(sorted(p.proposal_fingerprint for p in result.proposals))
    blocked_proposal_item_fingerprints = tuple(
        sorted(b.blocked_proposal_item_fingerprint for b in result.blocked_proposal_items)
    )

    return CvContentProposalReplayInput(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        content_plan_schema_version=plan.schema_version,
        content_policy_version=content_policy_version,
        p5a_generation_schema_version=p5a_generation_schema_version,
        p5a_generation_policy_version=p5a_generation_policy_version,
        p5a_result_fingerprint=result.p5a_result_fingerprint or "",
        p5a_replay_input_fingerprint=compute_p5a_replay_input_fingerprint(p5a_replay_input),
        proposal_schema_version=proposal_schema_version,
        proposal_policy_version=proposal_policy_version,
        fact_payload_set_fingerprint=fact_payload_set_fingerprint,
        eligible_blocked_item_fingerprints=tuple(eligible_blocked_item_fingerprints),
        proposal_context_fingerprint=compute_proposal_generation_context_fingerprint(
            proposal_context
        ),
        target_role_context_fingerprint=compute_target_role_context_fingerprint(
            proposal_context.target_role_context
        ),
        immutable_constraint_set_fingerprints=tuple(immutable_constraint_set_fingerprints),
        prompt_template_versions=tuple(prompt_template_versions),
        model_configuration_fingerprints=tuple(model_configuration_fingerprints),
        proposal_fingerprints=proposal_fingerprints,
        blocked_proposal_item_fingerprints=blocked_proposal_item_fingerprints,
    )


def stale_fields(
    previous: CvContentProposalReplayInput, current: CvContentProposalReplayInput
) -> tuple[str, ...]:
    """Return the sorted names of fields that differ between two inputs."""

    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_cv_content_proposal_stale(
    previous: CvContentProposalReplayInput, current: CvContentProposalReplayInput
) -> bool:
    """A result is stale if any staleness-relevant field has changed.

    Takes no current time and performs no lookup: both arguments are
    supplied explicitly by the caller.
    """

    return previous != current


def evaluate_proposal_freshness(
    previous: CvContentProposalReplayInput,
    current: CvContentProposalReplayInput | None,
) -> CvContentProposalReplayResult:
    """Classify a previously computed replay input against the caller's
    current one.

    ``current=None`` means the caller supplied no current replay input at
    all -- freshness is then ``FRESHNESS_NOT_VERIFIED``, never guessed as
    ``FRESH``.
    """

    if current is None:
        return CvContentProposalReplayResult(
            freshness_status=ProposalFreshnessStatus.FRESHNESS_NOT_VERIFIED, stale_fields=()
        )
    if previous == current:
        return CvContentProposalReplayResult(
            freshness_status=ProposalFreshnessStatus.FRESH, stale_fields=()
        )
    return CvContentProposalReplayResult(
        freshness_status=ProposalFreshnessStatus.STALE,
        stale_fields=stale_fields(previous, current),
    )
