"""Pydantic contracts for the mandatory PRE-P4 -> P3 role-strategy
integration.

These models never widen what legacy P3 (``app.schemas.fact_selection``)
already decides: a strategy-aware decision always carries the original,
untouched ``FactSelectionDecision`` plus an explicit, closed-set record of
whether and why it was overridden. ``BLOCKED``/``EXCLUDED``/
``APPROVAL_REQUIRED`` can never be overridden; only an advisory
``NOT_RELEVANT`` outcome can be conditionally raised to ``SELECTED``, and
only for an exact, verified thesis-supporting fact.

Every model here is closed (``extra="forbid"``) and replayable: identity is
expressed only through content-derived SHA-256 fingerprints, never a random
UUID or an implicit ``datetime.now()``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from app.schemas.candidacy_thesis import (
    CandidacyThesisContext,
    CandidacyThesisReplayInput,
    CandidacyThesisResult,
    RoleEvidenceCategory,
    RoleStrategyContext,
)
from app.schemas.cv_content_generation import ApprovedFactPayloadSet
from app.schemas.fact_selection import (
    FactSelectionDecision,
    FactSelectionReasonCode,
    SelectionDecisionOutcome,
    TargetContext,
)
from app.schemas.job_posting_analysis import (
    JobAnalysisContext,
    JobAnalysisReplayInput,
    JobEvidenceSegment,
    JobPostingAnalysisResult,
    JobPostingSnapshot,
)
from app.schemas.truth_entity import SHA256_PATTERN
from app.schemas.truth_fact import FACT_TYPE_PATTERN, TransformationOperation, TruthFactRead


INTEGRATION_SCHEMA_VERSION = "role-strategy-fact-selection-integration-schema-v1"
INTEGRATION_POLICY_VERSION = "role-strategy-fact-selection-integration-policy-v1"
ROLE_EVIDENCE_CATEGORY_MAPPING_VERSION = "role-evidence-category-mapping-v1"


class RoleStrategyFactSelectionInput(BaseModel):
    """The complete, closed input to strategy-aware P3 fact selection.

    Every upstream PRE-P4 artifact is carried in full -- never a bare
    fingerprint, a bare ``ready_for_p4`` flag, or an arbitrary dict -- so
    that ``role_strategy_prep4_revalidation`` can independently re-derive
    and re-check every fingerprint instead of trusting the caller.
    """

    model_config = ConfigDict(extra="forbid")

    job_posting_snapshot: JobPostingSnapshot
    job_evidence_segments: tuple[JobEvidenceSegment, ...] = Field(min_length=1)
    job_analysis_context: JobAnalysisContext
    job_analysis_result: JobPostingAnalysisResult
    current_job_analysis_replay_input: JobAnalysisReplayInput
    candidacy_thesis_context: CandidacyThesisContext
    candidacy_thesis_result: CandidacyThesisResult
    current_candidacy_thesis_replay_input: CandidacyThesisReplayInput
    role_strategy_context: RoleStrategyContext
    approved_fact_payload_set: ApprovedFactPayloadSet
    target_context: TargetContext
    integration_schema_version: Literal["role-strategy-fact-selection-integration-schema-v1"] = (
        INTEGRATION_SCHEMA_VERSION
    )
    integration_policy_version: str = Field(min_length=1, max_length=64)
    integration_input_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _role_strategy_context_matches_thesis(self) -> "RoleStrategyFactSelectionInput":
        if self.role_strategy_context != self.candidacy_thesis_result.role_strategy_context:
            raise ValueError(
                "role_strategy_context must equal candidacy_thesis_result.role_strategy_context"
            )
        return self


class StrategyFactSelectionCandidate(BaseModel):
    """One candidate fact plus the baseline (non-strategy) request P3 would
    have used. ``fact`` is never a dict -- always a real ``TruthFactRead``.
    """

    model_config = ConfigDict(extra="forbid")

    fact: TruthFactRead
    baseline_requested_operation: TransformationOperation
    baseline_requested_target_scope: str = Field(min_length=1, max_length=512)


class StrategyOverrideOutcome(StrEnum):
    NOT_OVERRIDDEN = "NOT_OVERRIDDEN"
    OVERRIDDEN_TO_SELECTED = "OVERRIDDEN_TO_SELECTED"


class StrategyOverrideReasonCode(StrEnum):
    """Closed set of reasons a ``NOT_RELEVANT`` override was or was not
    applied. Never extended by text or similarity classification."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXACT_THESIS_SUPPORTING_FACT = "EXACT_THESIS_SUPPORTING_FACT"
    BASE_DECISION_NOT_NOT_RELEVANT = "BASE_DECISION_NOT_NOT_RELEVANT"
    PREP4_REVALIDATION_FAILED = "PREP4_REVALIDATION_FAILED"
    NO_EVIDENCE_MAPPING = "NO_EVIDENCE_MAPPING"
    MULTIPLE_EVIDENCE_MAPPINGS = "MULTIPLE_EVIDENCE_MAPPINGS"
    PAYLOAD_SNAPSHOT_MISSING = "PAYLOAD_SNAPSHOT_MISSING"
    PAYLOAD_IDENTITY_MISMATCH = "PAYLOAD_IDENTITY_MISMATCH"
    PAYLOAD_CONTENT_FINGERPRINT_MISMATCH = "PAYLOAD_CONTENT_FINGERPRINT_MISMATCH"
    MAPPING_PAYLOAD_FINGERPRINT_MISMATCH = "MAPPING_PAYLOAD_FINGERPRINT_MISMATCH"
    THESIS_NOT_FRESH = "THESIS_NOT_FRESH"


class StrategyRankTier(StrEnum):
    """Closed priority tiers used only to order already-SELECTED facts."""

    TIER_0_EXACT_THESIS_SUPPORTING_FACT = "TIER_0_EXACT_THESIS_SUPPORTING_FACT"
    TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH = "TIER_1_PRIORITY_EVIDENCE_CATEGORY_MATCH"
    TIER_2_LEGACY_ONLY = "TIER_2_LEGACY_ONLY"


class StrategyRequestedOperationSource(StrEnum):
    LEGACY_DEFAULT = "LEGACY_DEFAULT"
    POSITIONING_FLATTEN_REQUESTED = "POSITIONING_FLATTEN_REQUESTED"


class StrategyAwareFactSelectionStatus(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class StrategyAwareFactSelectionFreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    FRESHNESS_NOT_VERIFIED = "FRESHNESS_NOT_VERIFIED"


class StrategyAwareFactSelectionDecision(BaseModel):
    """One P3 decision wrapped with its strategy-aware provenance.

    ``base_decision`` is always the untouched, legacy ``select_fact``
    output -- its ``decision_fingerprint`` never changes here. Only
    ``final_decision`` may differ from ``base_decision.decision``, and only
    for the single, closed ``NOT_RELEVANT`` -> ``SELECTED`` override path.
    """

    model_config = ConfigDict(extra="forbid")

    base_decision: FactSelectionDecision
    base_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    base_decision_outcome: SelectionDecisionOutcome
    final_decision: SelectionDecisionOutcome
    override_outcome: StrategyOverrideOutcome
    override_reason_code: StrategyOverrideReasonCode
    supporting_evidence_mapping_fingerprint: str | None = Field(default=None, pattern=SHA256_PATTERN)
    role_evidence_category: RoleEvidenceCategory | None = None
    strategy_rank_tier: StrategyRankTier
    requested_operation_source: StrategyRequestedOperationSource
    strategy_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _base_decision_preserved(self) -> "StrategyAwareFactSelectionDecision":
        if self.base_decision_fingerprint != self.base_decision.decision_fingerprint:
            raise ValueError(
                "base_decision_fingerprint must equal base_decision.decision_fingerprint"
            )
        if self.base_decision_outcome != self.base_decision.decision:
            raise ValueError("base_decision_outcome must equal base_decision.decision")
        return self

    @model_validator(mode="after")
    def _final_decision_respects_hard_gates(self) -> "StrategyAwareFactSelectionDecision":
        hard_gated = {
            SelectionDecisionOutcome.BLOCKED,
            SelectionDecisionOutcome.EXCLUDED,
            SelectionDecisionOutcome.APPROVAL_REQUIRED,
        }
        if self.base_decision_outcome in hard_gated:
            if self.final_decision != self.base_decision_outcome:
                raise ValueError(
                    "BLOCKED/EXCLUDED/APPROVAL_REQUIRED can never be overridden by strategy"
                )
            if self.override_outcome != StrategyOverrideOutcome.NOT_OVERRIDDEN:
                raise ValueError(
                    "BLOCKED/EXCLUDED/APPROVAL_REQUIRED can never carry an override outcome"
                )
        elif self.base_decision_outcome == SelectionDecisionOutcome.SELECTED:
            if self.final_decision != SelectionDecisionOutcome.SELECTED:
                raise ValueError("a base SELECTED decision must stay SELECTED")
            if self.override_outcome != StrategyOverrideOutcome.NOT_OVERRIDDEN:
                raise ValueError("a base SELECTED decision never carries an override outcome")
        elif self.base_decision_outcome == SelectionDecisionOutcome.NOT_RELEVANT:
            if self.final_decision not in (
                SelectionDecisionOutcome.NOT_RELEVANT,
                SelectionDecisionOutcome.SELECTED,
            ):
                raise ValueError(
                    "a base NOT_RELEVANT decision may only stay NOT_RELEVANT or become SELECTED"
                )
            if self.final_decision == SelectionDecisionOutcome.SELECTED:
                if self.override_outcome != StrategyOverrideOutcome.OVERRIDDEN_TO_SELECTED:
                    raise ValueError(
                        "final_decision=SELECTED from NOT_RELEVANT requires "
                        "override_outcome=OVERRIDDEN_TO_SELECTED"
                    )
                if self.override_reason_code != StrategyOverrideReasonCode.EXACT_THESIS_SUPPORTING_FACT:
                    raise ValueError(
                        "an OVERRIDDEN_TO_SELECTED decision must carry "
                        "override_reason_code=EXACT_THESIS_SUPPORTING_FACT"
                    )
                if self.supporting_evidence_mapping_fingerprint is None:
                    raise ValueError(
                        "an OVERRIDDEN_TO_SELECTED decision must carry a "
                        "supporting_evidence_mapping_fingerprint"
                    )
            else:
                if self.override_outcome != StrategyOverrideOutcome.NOT_OVERRIDDEN:
                    raise ValueError(
                        "a decision that stays NOT_RELEVANT must carry override_outcome=NOT_OVERRIDDEN"
                    )
        return self


class StrategyFactRankingEntry(BaseModel):
    """One fact's position in the deterministic strategy ranking."""

    model_config = ConfigDict(extra="forbid")

    fact_id: UUID4
    entity_id: UUID4
    fact_type: str = Field(pattern=FACT_TYPE_PATTERN)
    strategy_rank_tier: StrategyRankTier
    fact_type_priority: int = Field(ge=0)
    strategy_decision_fingerprint: str = Field(pattern=SHA256_PATTERN)
    ranking_entry_fingerprint: str = Field(pattern=SHA256_PATTERN)


class StrategyFactRankingInput(BaseModel):
    """The complete, deterministic ranking assignment for every SELECTED
    (post-override) fact in one ``StrategyAwareFactSelectionResult``."""

    model_config = ConfigDict(extra="forbid")

    ranking_policy_version: str = Field(min_length=1, max_length=64)
    entries: tuple[StrategyFactRankingEntry, ...]
    strategy_ranking_input_fingerprint: str = Field(pattern=SHA256_PATTERN)


class StrategyAwareFactSelectionResult(BaseModel):
    """The single return value of ``build_fact_selection_with_role_strategy``."""

    model_config = ConfigDict(extra="forbid")

    result_fingerprint: str = Field(pattern=SHA256_PATTERN)
    status: StrategyAwareFactSelectionStatus
    integration_input_fingerprint: str = Field(pattern=SHA256_PATTERN)
    role_strategy_context_fingerprint: str = Field(pattern=SHA256_PATTERN)
    decisions: tuple[StrategyAwareFactSelectionDecision, ...] = Field(default_factory=tuple)
    ranking: StrategyFactRankingInput | None = None
    evidence_category_mapping_version: str = Field(min_length=1, max_length=64)
    integration_policy_version: str = Field(min_length=1, max_length=64)
    ready_for_integrated_p4: bool

    @model_validator(mode="after")
    def _ready_requires_decisions_and_ranking(self) -> "StrategyAwareFactSelectionResult":
        if self.ready_for_integrated_p4:
            if self.status != StrategyAwareFactSelectionStatus.READY:
                raise ValueError("ready_for_integrated_p4 requires status=READY")
            if not self.decisions or self.ranking is None:
                raise ValueError(
                    "ready_for_integrated_p4 requires non-empty decisions and a ranking"
                )
        return self


class StrategyAwareFactSelectionReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freshness_status: StrategyAwareFactSelectionFreshnessStatus
    stale_fields: tuple[str, ...] = Field(default_factory=tuple)
