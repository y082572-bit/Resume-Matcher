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
from app.schemas.cv_transformation_generation import (
    CVTransformationGeneration,
    CVTransformationGenerationRequest,
)
from app.services.truth_library_loader import (
    get_truth_library,
    TruthLibraryNotFoundError,
    TruthLibraryInvalidJsonError,
    TruthLibraryStructureError,
)
from app.services.career_positioning_report import build_career_positioning_report
from app.services.cv_transformation_plan import (
    CVTransformationPlanBundle,
    build_cv_transformation_plan_bundle,
)
from app.services.cv_transformation_approval import (
    ApprovalValidationError,
    plan_source_references,
    validate_approval_request,
)

import logging

from app.llm import get_llm_config
from app.services.cv_transformation_generation import (
    GENERATION_VERSION,
    PROMPT_VERSION,
    GenerationValidationError,
    assemble_draft_resume,
    build_current_generation_context,
    generate_item_outputs,
    validate_llm_configuration,
)

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

    bundle, resume, job = await _build_current_transformation_plan_bundle(job_id, resume_id)
    return bundle.plan, resume, job


async def _build_current_transformation_plan_bundle(
    job_id: str, resume_id: str
) -> tuple[CVTransformationPlanBundle, dict, dict]:
    """Build public plan and private bindings after all source gates pass."""

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
    bundle = build_cv_transformation_plan_bundle(
        resume_id=resume_id,
        job_id=job_id,
        resume=resume,
        job=job,
        truth_library=truth_library,
        career_report=career_report,
        now=now,
    )
    return bundle, resume, job


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


def _generation_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _bindings_revision(bundle: CVTransformationPlanBundle) -> tuple[tuple, ...]:
    return tuple(
        (
            binding.source_reference,
            binding.source_fingerprint,
            binding.section,
            binding.namespace,
            binding.source_kind,
            binding.immutable_fields,
            binding.allowed_operation,
            binding.allowed_claim_codes,
            binding.source_position,
            binding.parent_experience_position,
            binding.description_position,
            binding.parent_source_fingerprint,
            tuple(
                (
                    bullet.index,
                    bullet.source_text,
                    bullet.source_fingerprint,
                    bullet.allowed_claim_codes,
                    bullet.allowed_operation,
                )
                for bullet in binding.bullet_bindings
            ),
        )
        for binding in bundle.bindings
    )


async def _load_current_generation_context(job_id: str, resume_id: str):
    """Load and compile the exact current input without running the model."""

    bundle, resume, job = await _build_current_transformation_plan_bundle(job_id, resume_id)
    approval = await db.get_transformation_plan_approval(
        job_id=job_id,
        resume_id=resume_id,
        plan_fingerprint=bundle.plan.plan_fingerprint,
    )
    if approval is None:
        raise _generation_error(
            404,
            "TRANSFORMATION_PLAN_APPROVAL_NOT_FOUND",
            "Approval for the current plan was not found",
        )
    config = get_llm_config()
    try:
        context = build_current_generation_context(
            plan=bundle.plan,
            bindings=bundle.bindings,
            approval=approval,
            config=config,
            job_content=job.get("content", "") if isinstance(job.get("content"), str) else "",
        )
    except GenerationValidationError as error:
        status = 503 if error.code == "LLM_NOT_CONFIGURED" else 422
        raise _generation_error(status, error.code, error.message) from error
    return context, resume, job, config


async def _finish_generation_attempt(
    row: dict,
    *,
    status: str,
    draft_resume: dict | None = None,
    provenance: list[dict] | None = None,
    failure_code: str | None = None,
):
    return await db.finish_transformation_generation(
        row["generation_id"],
        status=status,
        draft_resume=draft_resume,
        provenance=provenance,
        failure_code=failure_code,
        expected_attempt_count=row["attempt_count"],
        expected_generation_input_fingerprint=row["generation_input_fingerprint"],
    )


def _same_generation_context(left, right) -> bool:
    return (
        left.generation_input_fingerprint == right.generation_input_fingerprint
        and left.plan.plan_fingerprint == right.plan.plan_fingerprint
        and left.approval == right.approval
        and _bindings_revision(
            CVTransformationPlanBundle(plan=left.plan, bindings=left.bindings)
        )
        == _bindings_revision(
            CVTransformationPlanBundle(plan=right.plan, bindings=right.bindings)
        )
    )


@router.post(
    "/{job_id}/resumes/{resume_id}/transformation-plan/generation",
    response_model=CVTransformationGeneration,
)
async def generate_cv_from_approved_plan(
    job_id: str,
    resume_id: str,
    request: CVTransformationGenerationRequest,
) -> CVTransformationGeneration:
    """Generate one technical draft from the exact current approved plan."""

    bundle, resume, job = await _build_current_transformation_plan_bundle(job_id, resume_id)
    plan = bundle.plan
    if request.plan_fingerprint != plan.plan_fingerprint:
        raise _generation_error(409, "STALE_TRANSFORMATION_PLAN", "Refresh the plan before generation")
    approval = await db.get_transformation_plan_approval(
        job_id=job_id, resume_id=resume_id, plan_fingerprint=plan.plan_fingerprint
    )
    if approval is None or approval.get("approval_id") != request.approval_id:
        raise _generation_error(404, "TRANSFORMATION_PLAN_APPROVAL_NOT_FOUND", "Approval not found")
    if not plan_source_references(plan):
        raise _generation_error(
            422,
            "NO_TRANSFORMATION_ITEMS",
            "The current plan contains no transformation items",
        )
    try:
        config = get_llm_config()
        validate_llm_configuration(config)
        context = build_current_generation_context(
            plan=plan,
            bindings=bundle.bindings,
            approval=approval,
            config=config,
            job_content=job.get("content", "") if isinstance(job.get("content"), str) else "",
        )
    except GenerationValidationError as error:
        status = 409 if error.code == "APPROVAL_SUPERSEDED" else 503 if error.code == "LLM_NOT_CONFIGURED" else 422
        raise _generation_error(status, error.code, error.message) from error

    spec = context.spec
    input_fingerprint = context.generation_input_fingerprint
    row, claimed = await db.claim_transformation_generation(
        generation_input_fingerprint=input_fingerprint,
        values={
            "approval_id": approval["approval_id"],
            "resume_id": resume_id,
            "job_id": job_id,
            "plan_version": plan.plan_version,
            "plan_fingerprint": plan.plan_fingerprint,
            "generation_version": GENERATION_VERSION,
            "prompt_version": PROMPT_VERSION,
            "provider": config.provider,
            "model": config.model,
        },
    )
    if not claimed:
        if row["status"] == "GENERATED":
            return CVTransformationGeneration.model_validate(row)
        if row["status"] == "GENERATING":
            raise _generation_error(409, "GENERATION_IN_PROGRESS", "Generation is already in progress")
        if row["status"] == "FAILED":
            if not request.retry_failed:
                raise _generation_error(409, "GENERATION_FAILED_RETRY_REQUIRED", "Explicit retry is required")
            row, claimed = await db.retry_failed_transformation_generation(row["generation_id"])
            if not claimed:
                raise _generation_error(409, "GENERATION_IN_PROGRESS", "A retry is already in progress")
        elif row["status"] == "SUPERSEDED":
            row, claimed = await db.recover_superseded_transformation_generation(
                row["generation_id"]
            )
            if not claimed:
                raise _generation_error(
                    409, "GENERATION_IN_PROGRESS", "Generation recovery is already in progress"
                )
        else:
            raise _generation_error(409, "GENERATION_IN_PROGRESS", "Generation is unavailable")

    try:
        outputs = await generate_item_outputs(spec, config)
    except GenerationValidationError as error:
        await _finish_generation_attempt(
            row, status="FAILED", failure_code=error.code
        )
        raise _generation_error(502, error.code, "The model returned an unsafe response") from error
    except Exception as error:
        logger.error(
            "Approved-plan generation provider failure",
            extra={
                "generation_id": row["generation_id"],
                "failure_code": "LLM_PROVIDER_ERROR",
                "provider": context.provider,
                "model": context.model,
                "exception_class": error.__class__.__name__,
            },
        )
        await _finish_generation_attempt(
            row, status="FAILED", failure_code="LLM_PROVIDER_ERROR"
        )
        raise _generation_error(502, "LLM_PROVIDER_ERROR", "Generation provider failed") from error

    try:
        current_context, current_resume, _, _ = await _load_current_generation_context(
            job_id, resume_id
        )
        source_changed = not _same_generation_context(context, current_context)
    except HTTPException:
        source_changed = True
        current_resume = resume
    if source_changed:
        await _finish_generation_attempt(
            row,
            status="SUPERSEDED",
            failure_code="SOURCE_CHANGED_DURING_GENERATION",
        )
        raise _generation_error(
            409,
            "SOURCE_CHANGED_DURING_GENERATION",
            "Source or approval changed during generation",
        )

    try:
        draft, provenance = assemble_draft_resume(
            current_resume["processed_data"], spec, outputs
        )
    except Exception as error:
        logger.error(
            "Approved-plan deterministic draft assembly failed",
            extra={
                "generation_id": row["generation_id"],
                "failure_code": "DRAFT_ASSEMBLY_FAILED",
                "exception_class": error.__class__.__name__,
            },
        )
        await _finish_generation_attempt(
            row, status="FAILED", failure_code="DRAFT_ASSEMBLY_FAILED"
        )
        raise _generation_error(502, "DRAFT_ASSEMBLY_FAILED", "Draft assembly failed") from error

    # Last TOCTOU gate: rebuild every semantic input after assembly and before CAS.
    try:
        final_context, _, _, _ = await _load_current_generation_context(job_id, resume_id)
        source_changed = not _same_generation_context(context, final_context)
    except HTTPException:
        source_changed = True
    if source_changed:
        await _finish_generation_attempt(
            row,
            status="SUPERSEDED",
            failure_code="SOURCE_CHANGED_DURING_GENERATION",
        )
        raise _generation_error(
            409,
            "SOURCE_CHANGED_DURING_GENERATION",
            "Source or approval changed during generation",
        )

    finished, won = await _finish_generation_attempt(
        row,
        status="GENERATED",
        draft_resume=draft,
        provenance=provenance,
    )
    if not won:
        raise _generation_error(
            409,
            "SOURCE_CHANGED_DURING_GENERATION",
            "Generation finalization lost its compare-and-set",
        )
    return CVTransformationGeneration.model_validate(finished)


@router.get(
    "/{job_id}/resumes/{resume_id}/transformation-plan/generation",
    response_model=CVTransformationGeneration,
)
async def get_current_cv_transformation_generation(
    job_id: str, resume_id: str
) -> CVTransformationGeneration:
    try:
        context, _, _, _ = await _load_current_generation_context(job_id, resume_id)
    except HTTPException as error:
        code = error.detail.get("code") if isinstance(error.detail, dict) else None
        if code in {
            "APPROVAL_NOT_SUBMITTED",
            "APPROVAL_REQUIRES_REVIEW",
            "APPROVAL_SUPERSEDED",
            "APPROVAL_NOT_APPROVED",
            "GUARDRAILS_NOT_ACKNOWLEDGED",
            "INCOMPLETE_DECISIONS",
            "INVALID_DECISION",
        }:
            raise _generation_error(
                404,
                "TRANSFORMATION_GENERATION_NOT_FOUND",
                "The current generation input has not been generated",
            ) from error
        raise
    row = await db.get_transformation_generation(
        job_id=job_id,
        resume_id=resume_id,
        generation_input_fingerprint=context.generation_input_fingerprint,
    )
    if row is None or row["status"] == "SUPERSEDED":
        raise _generation_error(404, "TRANSFORMATION_GENERATION_NOT_FOUND", "Generation not found")
    return CVTransformationGeneration.model_validate(row)


@router.get(
    "/{job_id}/resumes/{resume_id}/transformation-plan/generation/{generation_id}",
    response_model=CVTransformationGeneration,
)
async def get_cv_transformation_generation_by_id(
    job_id: str, resume_id: str, generation_id: str
) -> CVTransformationGeneration:
    row = await db.get_transformation_generation(
        generation_id=generation_id, job_id=job_id, resume_id=resume_id
    )
    if row is None:
        raise _generation_error(404, "TRANSFORMATION_GENERATION_NOT_FOUND", "Generation not found")
    try:
        context, _, _, _ = await _load_current_generation_context(job_id, resume_id)
        is_current = (
            row["generation_input_fingerprint"]
            == context.generation_input_fingerprint
        )
    except HTTPException:
        is_current = False
    if not is_current:
        row = {
            **row,
            "status": "SUPERSEDED",
            "draft_resume": None,
            "provenance": [],
            "failure_code": "SOURCE_CHANGED_DURING_GENERATION",
            "applied_to_resume": False,
        }
    return CVTransformationGeneration.model_validate(row)
