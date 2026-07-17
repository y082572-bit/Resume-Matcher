"""Metrics router implementing the read-only metrics report endpoint."""

import logging
from fastapi import APIRouter, HTTPException, status

from app.database import db
from app.services.project_metrics import (
    build_metrics_report,
    MetricsTrackingNotInitializedError,
)
from app.schemas.metrics import MetricsResponseSchema

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Metrics"])


@router.get("/metrics", response_model=MetricsResponseSchema)
async def get_metrics() -> MetricsResponseSchema:
    """Get summarized project metrics.

    Provides high-level aggregate data for saved and analyzed jobs, generated resumes,
    applications, and statuses. Excludes private details, IDs, and raw text.
    """
    async with db._session() as session:
        try:
            report = await build_metrics_report(session)
        except MetricsTrackingNotInitializedError as e:
            logger.warning(f"Metrics queried before initialization/bootstrap: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Metrics tracking has not been initialized. Please complete bootstrap.",
            )
        except Exception as e:
            logger.exception(f"Unexpected error building metrics report: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve metrics report.",
            )

        # Map capabilities dynamically from the report's metrics
        capabilities_map = {}
        metrics_list = []

        for mv in report.metrics:
            capabilities_map[mv.metric_name.value] = {
                "current": mv.current.availability.value,
                "lifetime": mv.lifetime.availability.value,
                "peak": mv.peak.availability.value,
                "archived": mv.archived.availability.value,
            }

            metrics_list.append({
                "metric_name": mv.metric_name.value,
                "current": {
                    "value": mv.current.value,
                    "availability": mv.current.availability.value,
                },
                "lifetime": {
                    "value": mv.lifetime.value,
                    "availability": mv.lifetime.availability.value,
                },
                "peak": {
                    "value": mv.peak.value,
                    "availability": mv.peak.availability.value,
                },
                "archived": {
                    "value": mv.archived.value,
                    "availability": mv.archived.availability.value,
                },
            })

        # Report has a validated history
        assert report.history is not None

        return MetricsResponseSchema(
            schema_version="1.0",
            generated_at=report.generated_at,
            history={
                "bootstrap_version": report.history.bootstrap_version,
                "baseline_at": report.history.baseline_at,
                "complete_since": report.history.complete_since,
                "completeness": report.history.completeness,
                "baseline_event_count": report.history.baseline_event_count,
            },
            capabilities=capabilities_map,
            metrics=metrics_list,
        )
