"""Pure replay and stale-result detection for P5a CV content generation.

No function here reads a clock, performs a lookup, or touches the
database. Staleness is judged only by comparing a
``CvContentGenerationReplayInput`` derived from a previously computed
``CvContentGenerationResult`` against the caller's current values for those
same fields.
"""

from __future__ import annotations

from app.schemas.cv_content_generation import (
    ApprovedFactPayloadSet,
    CvContentGenerationReplayInput,
    CvContentGenerationReplayResult,
    CvContentGenerationResult,
    GenerationContext,
    GenerationFreshnessStatus,
)
from app.schemas.cv_content_plan import CvContentPlan
from app.services.cv_content_generation_builder import (
    compute_fact_payload_set_fingerprint,
    compute_generation_context_fingerprint,
)


def replay_input_from_generation_result(
    *,
    plan: CvContentPlan,
    payload_set: ApprovedFactPayloadSet,
    generation_context: GenerationContext,
    generation_schema_version: str,
    result: CvContentGenerationResult,
) -> CvContentGenerationReplayInput:
    """Extract the staleness-relevant fields of a previously computed result.

    ``plan``/``payload_set``/``generation_context`` are the exact inputs the
    result was computed from -- this function never re-derives them from a
    database or from ``result`` alone, since a result carries only
    fingerprints, never the full source objects.
    """

    planned_fact_use_fingerprints: list[str] = []
    for section in plan.sections:
        if section.kind == "EXPERIENCE":
            for entry in section.experience_entries:
                planned_fact_use_fingerprints.extend(
                    use.planned_fact_use_fingerprint
                    for use in (
                        entry.header_fact_uses
                        + entry.responsibility_fact_uses
                        + entry.achievement_fact_uses
                    )
                )
        else:
            planned_fact_use_fingerprints.extend(
                use.planned_fact_use_fingerprint for use in section.planned_fact_uses
            )

    generated_element_fingerprints: list[str] = []
    if result.draft is not None:
        for section in result.draft.generated_sections:
            if section.kind == "EXPERIENCE":
                for entry in section.generated_experience_entries:
                    generated_element_fingerprints.extend(
                        element.generated_element_fingerprint
                        for element in (
                            entry.header_elements
                            + entry.responsibility_elements
                            + entry.achievement_elements
                        )
                    )
            else:
                generated_element_fingerprints.extend(
                    element.generated_element_fingerprint
                    for element in section.generated_elements
                )

    return CvContentGenerationReplayInput(
        content_plan_fingerprint=plan.content_plan_fingerprint,
        content_plan_schema_version=plan.schema_version,
        content_policy_version=plan.content_policy_version,
        generation_schema_version=generation_schema_version,
        generation_policy_version=generation_context.generation_policy_version,
        fact_payload_set_fingerprint=compute_fact_payload_set_fingerprint(payload_set.payloads),
        planned_fact_use_fingerprints=tuple(sorted(planned_fact_use_fingerprints)),
        generation_context_fingerprint=compute_generation_context_fingerprint(
            generation_context
        ),
        deterministic_template_version=generation_context.deterministic_template_version,
        generated_element_fingerprints=tuple(sorted(generated_element_fingerprints)),
        blocked_item_fingerprints=tuple(
            sorted(item.blocked_item_fingerprint for item in result.blocked_items)
        ),
    )


def stale_fields(
    previous: CvContentGenerationReplayInput, current: CvContentGenerationReplayInput
) -> tuple[str, ...]:
    """Return the sorted names of fields that differ between two inputs."""

    differing = {
        name
        for name in type(previous).model_fields
        if getattr(previous, name) != getattr(current, name)
    }
    return tuple(sorted(differing))


def is_cv_content_generation_stale(
    previous: CvContentGenerationReplayInput, current: CvContentGenerationReplayInput
) -> bool:
    """A result is stale if any staleness-relevant field has changed.

    Takes no current time and performs no lookup: both arguments are
    supplied explicitly by the caller.
    """

    return previous != current


def evaluate_generation_freshness(
    previous: CvContentGenerationReplayInput,
    current: CvContentGenerationReplayInput | None,
) -> CvContentGenerationReplayResult:
    """Classify a previously computed replay input against the caller's current one.

    ``current=None`` means the caller supplied no current replay input at
    all -- freshness is then ``FRESHNESS_NOT_VERIFIED``, never guessed as
    ``FRESH``.
    """

    if current is None:
        return CvContentGenerationReplayResult(
            freshness_status=GenerationFreshnessStatus.FRESHNESS_NOT_VERIFIED, stale_fields=()
        )
    if previous == current:
        return CvContentGenerationReplayResult(
            freshness_status=GenerationFreshnessStatus.FRESH, stale_fields=()
        )
    return CvContentGenerationReplayResult(
        freshness_status=GenerationFreshnessStatus.STALE,
        stale_fields=stale_fields(previous, current),
    )
