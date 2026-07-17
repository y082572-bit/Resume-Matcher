"""Pydantic models for Career Positioning Engine responses."""

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator


class PositioningDetailsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detected_job_level: Literal["UNKNOWN", "JUNIOR", "SPECIALIST", "EXPERT", "MANAGER", "DIRECTOR", "EXECUTIVE"]
    candidate_profile_level: Literal["UNKNOWN", "JUNIOR", "SPECIALIST", "EXPERT", "MANAGER", "DIRECTOR", "EXECUTIVE"]
    level_gap: int
    positioning_strategy: Literal[
        "REVIEW_REQUIRED",
        "JUNIOR_ENTRY",
        "SPECIALIST_DELIVERY",
        "EXPERT_AUTHORITY",
        "MANAGER_LEADERSHIP",
        "DIRECTOR_SCALE",
        "EXECUTIVE_ENTERPRISE",
        "CONTROLLED_FLATTENING",
        "CONTROLLED_ELEVATION",
        "BALANCED"
    ]
    title_treatment: Literal["KEEP", "CONTEXTUALIZE", "DEEMPHASIZE", "REVIEW"]
    fit_level: Literal["LOW", "MODERATE", "GOOD", "STRONG"]
    fit_score: float = Field(..., ge=0.0, le=100.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    requires_human_review: bool


class RiskItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    availability: Literal["AVAILABLE", "UNSUPPORTED"]
    level: Optional[Literal["NONE", "LOW", "MEDIUM", "HIGH"]] = None
    rationale: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_risk_level(self) -> 'RiskItemSchema':
        if self.availability == "AVAILABLE":
            if self.level is None:
                raise ValueError("AVAILABLE risk requires a non-null level")
        else:
            if self.level is not None:
                raise ValueError("UNSUPPORTED risk requires level to be null")

        seen = set()
        for r in self.rationale:
            if not isinstance(r, str) or not r.strip():
                raise ValueError("Rationale elements must be non-empty strings")
            if r.strip() in seen:
                raise ValueError(f"Duplicate rationale found: {r}")
            seen.add(r.strip())
        return self


class RisksSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overqualification: RiskItemSchema
    underqualification: RiskItemSchema
    stability_concern: RiskItemSchema
    sector_gap: RiskItemSchema
    functional_gap: RiskItemSchema


class CompetencyItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_non_whitespace(self) -> 'CompetencyItemSchema':
        if not self.name.strip():
            raise ValueError("name cannot be whitespace-only")
        if not self.reason.strip():
            raise ValueError("reason cannot be whitespace-only")
        if not self.source.strip():
            raise ValueError("source cannot be whitespace-only")
        return self


class CompetenciesSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direct_matches: list[CompetencyItemSchema] = Field(default_factory=list)
    transferable_matches: list[CompetencyItemSchema] = Field(default_factory=list)
    missing_critical: list[CompetencyItemSchema] = Field(default_factory=list)
    excessive_for_role: list[CompetencyItemSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_no_duplicates(self) -> 'CompetenciesSchema':
        seen = set()
        for list_name in ("direct_matches", "transferable_matches", "missing_critical", "excessive_for_role"):
            items = getattr(self, list_name)
            for item in items:
                norm_name = item.name.strip().lower()
                if norm_name in seen:
                    raise ValueError(f"Duplicate competency name found: {item.name}")
                seen.add(norm_name)
        return self


class NarrativeVariantSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["EXPERT", "MANAGER", "DIRECTOR"]
    availability: Literal["AVAILABLE", "UNSUPPORTED"]
    recommended: bool
    title_positioning: Optional[str] = None
    summary_positioning: Optional[str] = None
    elements_to_emphasize: list[str] = Field(default_factory=list)
    elements_to_flatten: list[str] = Field(default_factory=list)
    elements_to_remove_or_deprioritize: list[str] = Field(default_factory=list)
    risk_level: Literal["NONE", "LOW", "MEDIUM", "HIGH"]
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_variant_rules(self) -> 'NarrativeVariantSchema':
        if self.availability == "UNSUPPORTED":
            if self.recommended:
                raise ValueError("UNSUPPORTED narrative variant cannot be recommended")
            if self.title_positioning is not None or self.summary_positioning is not None:
                raise ValueError("UNSUPPORTED narrative variant must have null title_positioning and summary_positioning")
        else:
            if self.title_positioning is None or not self.title_positioning.strip():
                raise ValueError("AVAILABLE narrative variant must have non-empty title_positioning")
            if self.summary_positioning is None or not self.summary_positioning.strip():
                raise ValueError("AVAILABLE narrative variant must have non-empty summary_positioning")

        for list_name in ("elements_to_emphasize", "elements_to_flatten", "elements_to_remove_or_deprioritize", "limitations"):
            lst = getattr(self, list_name)
            seen = set()
            for item in lst:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"List {list_name} in narrative variant contains empty or non-string elements")
                if item.strip() in seen:
                    raise ValueError(f"Duplicate element found in variant list {list_name}: {item}")
                seen.add(item.strip())
        return self


class StrategySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recommendation: str = Field(..., min_length=1)
    recommended_narrative: Optional[Literal["EXPERT", "MANAGER", "DIRECTOR"]] = None
    elements_to_emphasize: list[str] = Field(default_factory=list)
    elements_to_flatten: list[str] = Field(default_factory=list)
    elements_to_remove_or_deprioritize: list[str] = Field(default_factory=list)
    title_positioning: Optional[str] = None
    summary_positioning: Optional[str] = None
    narrative_variants: list[NarrativeVariantSchema] = Field(..., min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_strategy_rules(self) -> 'StrategySchema':
        types = [v.type for v in self.narrative_variants]
        if set(types) != {"EXPERT", "MANAGER", "DIRECTOR"}:
            raise ValueError("Narrative variants must consist of exactly EXPERT, MANAGER, and DIRECTOR")

        recommended_variants = [v for v in self.narrative_variants if v.recommended]

        if self.recommended_narrative is None:
            if len(recommended_variants) != 0:
                raise ValueError("No narrative variants can be recommended when recommended_narrative is null")
            if self.title_positioning is not None or self.summary_positioning is not None:
                raise ValueError("title_positioning and summary_positioning must be null when recommended_narrative is null")
        else:
            if len(recommended_variants) != 1:
                raise ValueError("Exactly one narrative variant must be marked as recommended")
            if recommended_variants[0].type != self.recommended_narrative:
                raise ValueError("Recommended variant type does not match recommended_narrative")
            if self.title_positioning is None or not self.title_positioning.strip():
                raise ValueError("title_positioning cannot be null or empty when recommended_narrative is provided")
            if self.summary_positioning is None or not self.summary_positioning.strip():
                raise ValueError("summary_positioning cannot be null or empty when recommended_narrative is provided")

        for list_name in ("elements_to_emphasize", "elements_to_flatten", "elements_to_remove_or_deprioritize"):
            lst = getattr(self, list_name)
            seen = set()
            for item in lst:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"List {list_name} contains empty or non-string elements")
                if item.strip() in seen:
                    raise ValueError(f"Duplicate element found in {list_name}: {item}")
                seen.add(item.strip())

        if not self.recommendation.strip():
            raise ValueError("recommendation cannot be whitespace-only")

        return self


class EvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    used_truth_items_count: int = Field(..., ge=0)
    direct_evidence_count: int = Field(..., ge=0)
    transferable_evidence_count: int = Field(..., ge=0)
    unresolved_items_count: int = Field(..., ge=0)


class LimitationsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    insufficient_data: bool
    messages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_limitations(self) -> 'LimitationsSchema':
        seen = set()
        for msg in self.messages:
            if not isinstance(msg, str) or not msg.strip():
                raise ValueError("Limitations messages must be non-empty strings")
            if msg.strip() in seen:
                raise ValueError(f"Duplicate limitation message: {msg}")
            seen.add(msg.strip())
        return self


class CareerPositioningResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    job_id: str = Field(..., min_length=1)
    job_title: str = Field(..., min_length=1)
    positioning: PositioningDetailsSchema
    risks: RisksSchema
    competencies: CompetenciesSchema
    strategy: StrategySchema
    evidence: EvidenceSchema
    limitations: LimitationsSchema

    @model_validator(mode="after")
    def validate_cross_rules(self) -> 'CareerPositioningResponse':
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if not self.job_id.strip():
            raise ValueError("job_id cannot be whitespace-only")
        if not self.job_title.strip():
            raise ValueError("job_title cannot be whitespace-only")

        c_level = self.positioning.candidate_profile_level
        insufficient = self.limitations.insufficient_data
        requires_review = self.positioning.requires_human_review

        # A. Gdy candidate_profile_level != UNKNOWN i limitations.insufficient_data = false
        if c_level != "UNKNOWN" and not insufficient:
            if self.strategy.recommended_narrative is None:
                raise ValueError("recommended_narrative cannot be null when data is sufficient")
            recommended_variants = [v for v in self.strategy.narrative_variants if v.recommended]
            if len(recommended_variants) != 1:
                raise ValueError("Exactly one narrative variant must be recommended when data is sufficient")
            if recommended_variants[0].type != self.strategy.recommended_narrative:
                raise ValueError("Recommended variant type does not match recommended_narrative")
            if self.strategy.title_positioning is None or not self.strategy.title_positioning.strip():
                raise ValueError("title_positioning must be provided when data is sufficient")
            if self.strategy.summary_positioning is None or not self.strategy.summary_positioning.strip():
                raise ValueError("summary_positioning must be provided when data is sufficient")

        # B. Gdy candidate_profile_level = UNKNOWN lub limitations.insufficient_data = true
        if c_level == "UNKNOWN" or insufficient:
            if not requires_review:
                raise ValueError("requires_human_review must be true when level is UNKNOWN or data is insufficient")
            if not insufficient:
                raise ValueError("insufficient_data must be true when level is UNKNOWN")
            if self.strategy.recommended_narrative is not None:
                raise ValueError("recommended_narrative must be null when data is insufficient")
            recommended_variants = [v for v in self.strategy.narrative_variants if v.recommended]
            if len(recommended_variants) != 0:
                raise ValueError("Zero narrative variants must be recommended when data is insufficient")

            for v in self.strategy.narrative_variants:
                if v.availability != "UNSUPPORTED":
                    raise ValueError(f"Variant {v.type} must be UNSUPPORTED when data is insufficient")

            if self.strategy.title_positioning is not None:
                raise ValueError("title_positioning must be null when data is insufficient")
            if self.strategy.summary_positioning is not None:
                raise ValueError("summary_positioning must be null when data is insufficient")
            if len(self.strategy.elements_to_emphasize) > 0:
                raise ValueError("elements_to_emphasize must be empty when data is insufficient")
            if len(self.strategy.elements_to_flatten) > 0:
                raise ValueError("elements_to_flatten must be empty when data is insufficient")
            if len(self.strategy.elements_to_remove_or_deprioritize) > 0:
                raise ValueError("elements_to_remove_or_deprioritize must be empty when data is insufficient")

        return self
