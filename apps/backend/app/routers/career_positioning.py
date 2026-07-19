"""Career Positioning Engine endpoints."""

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from app.database import db
from app.schemas.career_positioning import CareerPositioningResponse
from app.schemas.cv_transformation_approval import (
    CVTransformationPlanApproval,
    CVTransformationPlanApprovalRequest,
)
from app.schemas.cv_transformation_plan import CVTransformationPlan
from app.services.truth_library_loader import (
    get_truth_library,
    TruthLibraryNotFoundError,
    TruthLibraryInvalidJsonError,
    TruthLibraryStructureError,
)
from app.services.career_positioning_report import build_career_positioning_report
from app.services.cv_transformation_plan import build_cv_transformation_plan
from app.services.cv_transformation_approval import (
    ApprovalValidationError,
    validate_approval_request,
)

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Career Positioning"])


def _load_truth_library_or_http_error() -> dict:
    """Load approved source data while preserving the established API errors."""
    try:
        return get_truth_library()
    except TruthLibraryNotFoundError:
        logger.exception("Truth Library not found")
        raise HTTPException(
            status_code=404,
            detail={"code": "NO_TRUTH_LIBRARY", "message": "Truth Library file not found"},
        )
    except TruthLibraryInvalidJsonError:
        logger.exception("Truth Library JSON is invalid")
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_TRUTH_LIBRARY", "message": "Truth Library JSON is invalid"},
        )
    except TruthLibraryStructureError:
        logger.exception("Truth Library structure is invalid")
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TRUTH_LIBRARY", "message": "Invalid Truth Library structure"},
        )
    except Exception:
        logger.exception("Unexpected exception while loading Truth Library")
        raise HTTPException(
            status_code=500,
            detail={"code": "TRUTH_LIBRARY_READ_ERROR", "message": "Truth Library read error"},
        )


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
    truth_lib = _load_truth_library_or_http_error()

    # 3. Generate Report using Service
    now = datetime.now(timezone.utc)
    report = build_career_positioning_report(job, truth_lib, now)
    return report


@router.get(
    "/{job_id}/resumes/{resume_id}/transformation-plan",
    response_model=CVTransformationPlan,
)
async def get_cv_transformation_plan(job_id: str, resume_id: str) -> CVTransformationPlan:
    """Preview a deterministic plan without LLM calls, persistence, or metrics."""
    plan, _, _ = await _build_current_transformation_plan(job_id, resume_id)
    return plan


async def _build_current_transformation_plan(
    job_id: str, resume_id: str
) -> tuple[CVTransformationPlan, dict, dict]:
    """Regenerate the authoritative current plan for preview or approval."""

    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={"code": "JOB_NOT_FOUND", "message": "Job not found"},
        )

    resume = await db.get_resume(resume_id)
    if not resume:
        raise HTTPException(
            status_code=404,
            detail={"code": "RESUME_NOT_FOUND", "message": "Resume not found"},
        )

    if job.get("resume_id") != resume_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INCONSISTENT_JOB_RESUME_RELATION",
                "message": "Job is not linked to the requested source resume",
            },
        )
    if resume.get("processing_status") != "ready" or not isinstance(
        resume.get("processed_data"), dict
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RESUME_NOT_READY",
                "message": "Resume must be ready and contain structured data",
            },
        )

    truth_library = _load_truth_library_or_http_error()
    now = datetime.now(timezone.utc)
    career_report = build_career_positioning_report(job, truth_library, now)
    plan = build_cv_transformation_plan(
        resume_id=resume_id,
        job_id=job_id,
        resume=resume,
        job=job,
        truth_library=truth_library,
        career_report=career_report,
        now=now,
    )
    return plan, resume, job


@router.post(
    "/{job_id}/resumes/{resume_id}/transformation-plan/approval",
    response_model=CVTransformationPlanApproval,
)
async def save_cv_transformation_plan_approval(
    job_id: str,
    resume_id: str,
    request: CVTransformationPlanApprovalRequest,
) -> CVTransformationPlanApproval:
    """Persist explicit decisions for the exact current deterministic plan."""

    plan, _, _ = await _build_current_transformation_plan(job_id, resume_id)
    if request.plan_fingerprint != plan.plan_fingerprint:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_TRANSFORMATION_PLAN",
                "message": "Resume, Job, or source data changed; refresh the plan.",
            },
        )
    try:
        decisions, status = validate_approval_request(plan, request)
    except ApprovalValidationError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": error.message},
        ) from error

    row, _ = await db.save_transformation_plan_approval(
        job_id=job_id,
        resume_id=resume_id,
        plan_version=plan.plan_version,
        plan_fingerprint=plan.plan_fingerprint,
        status=status,
        decisions=[item.model_dump() for item in decisions],
        guardrails_acknowledged=request.guardrails_acknowledged,
    )
    return CVTransformationPlanApproval.model_validate(row)


@router.get(
    "/{job_id}/resumes/{resume_id}/transformation-plan/approval",
    response_model=CVTransformationPlanApproval,
)
async def get_cv_transformation_plan_approval(
    job_id: str, resume_id: str
) -> CVTransformationPlanApproval:
    """Return the approval for the current plan or expose the latest as superseded."""

    plan, _, _ = await _build_current_transformation_plan(job_id, resume_id)
    row = await db.get_transformation_plan_approval(
        job_id=job_id,
        resume_id=resume_id,
        plan_fingerprint=plan.plan_fingerprint,
    )
    if row is None:
        row = await db.get_transformation_plan_approval(
            job_id=job_id,
            resume_id=resume_id,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "TRANSFORMATION_PLAN_APPROVAL_NOT_FOUND",
                    "message": "Transformation plan approval not found",
                },
            )
        row = {**row, "status": "SUPERSEDED"}
    return CVTransformationPlanApproval.model_validate(row)
