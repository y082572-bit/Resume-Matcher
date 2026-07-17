"""Career Positioning Engine endpoints."""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from app.database import db
from app.schemas.career_positioning import CareerPositioningResponse
from app.services.truth_library_loader import (
    get_truth_library,
    TruthLibraryNotFoundError,
    TruthLibraryInvalidJsonError,
    TruthLibraryStructureError,
)
from app.services.career_positioning_report import build_career_positioning_report

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Career Positioning"])


@router.get("/{job_id}/career-positioning", response_model=CareerPositioningResponse)
async def get_career_positioning(job_id: str) -> CareerPositioningResponse:
    """Evaluate candidate career positioning against a job description.

    This is a read-only endpoint that does not modify the database
    nor emit metrics collector events.
    """
    # 1. Fetch Job
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={"code": "JOB_NOT_FOUND", "message": "Job not found"}
        )

    # 2. Fetch Truth Library with strict error differentiation
    try:
        truth_lib = get_truth_library()
    except TruthLibraryNotFoundError:
        logger.exception("Truth Library not found")
        raise HTTPException(
            status_code=404,
            detail={"code": "NO_TRUTH_LIBRARY", "message": "Truth Library file not found"}
        )
    except TruthLibraryInvalidJsonError:
        logger.exception("Truth Library JSON is invalid")
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_TRUTH_LIBRARY", "message": "Truth Library JSON is invalid"}
        )
    except TruthLibraryStructureError:
        logger.exception("Truth Library structure is invalid")
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TRUTH_LIBRARY", "message": "Invalid Truth Library structure"}
        )
    except Exception:
        logger.exception("Unexpected exception in get_career_positioning")
        raise HTTPException(
            status_code=500,
            detail={"code": "TRUTH_LIBRARY_READ_ERROR", "message": "Truth Library read error"}
        )

    # 3. Generate Report using Service
    now = datetime.now(timezone.utc)
    report = build_career_positioning_report(job, truth_lib, now)
    return report
