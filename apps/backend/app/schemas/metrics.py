"""Pydantic schemas for the read-only Metrics API."""

from datetime import datetime
from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, Field, field_validator


class MetricMeasureSchema(BaseModel):
    """Value and availability for a single metric dimension (current, lifetime, etc.)."""

    value: Optional[int] = Field(None, ge=0)
    availability: Literal["AVAILABLE", "UNSUPPORTED"]

    model_config = {
        "extra": "forbid"
    }


class MetricValueSchema(BaseModel):
    """Aggregate measurements for a single metric type."""

    metric_name: Literal[
        "job_offers_saved",
        "job_offers_analyzed",
        "resumes_generated",
        "applications_created",
        "applications_submitted",
        "employer_responses",
        "interviews",
        "rejections",
        "applications_accepted",
        "status_changes",
    ]
    current: MetricMeasureSchema
    lifetime: MetricMeasureSchema
    peak: MetricMeasureSchema
    archived: MetricMeasureSchema

    model_config = {
        "extra": "forbid"
    }


class HistoryInfoSchema(BaseModel):
    """Bootstrap metadata and history completeness indicators."""

    bootstrap_version: int = Field(..., ge=0)
    baseline_at: datetime
    complete_since: datetime
    completeness: Literal["COMPLETE", "PARTIAL_BASELINE"]
    baseline_event_count: int = Field(..., ge=0)

    model_config = {
        "extra": "forbid"
    }

    @field_validator("baseline_at", "complete_since", mode="after")
    @classmethod
    def validate_utc_datetime(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        if v.utcoffset() is None or v.utcoffset().total_seconds() != 0:
            raise ValueError("datetime must be in UTC timezone")
        return v


class MetricCapabilitiesSchema(BaseModel):
    """Availabilities of current, lifetime, peak, and archived dimensions for a metric."""

    current: Literal["AVAILABLE", "UNSUPPORTED"]
    lifetime: Literal["AVAILABLE", "UNSUPPORTED"]
    peak: Literal["AVAILABLE", "UNSUPPORTED"]
    archived: Literal["AVAILABLE", "UNSUPPORTED"]

    model_config = {
        "extra": "forbid"
    }


class MetricsResponseSchema(BaseModel):
    """The complete response payload for GET /api/v1/metrics."""

    schema_version: Literal["1.0"]
    generated_at: datetime
    history: HistoryInfoSchema
    capabilities: Dict[str, MetricCapabilitiesSchema]
    metrics: List[MetricValueSchema]

    model_config = {
        "extra": "forbid"
    }

    @field_validator("generated_at", mode="after")
    @classmethod
    def validate_utc_datetime(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        if v.utcoffset() is None or v.utcoffset().total_seconds() != 0:
            raise ValueError("datetime must be in UTC timezone")
        return v
