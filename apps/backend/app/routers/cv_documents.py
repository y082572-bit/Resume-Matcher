"""Explicit Provenance Stage P6-C1a: Document Workflow API Foundation.

Exposes a stable HTTP API over the existing local, single-user CV document
workflow: download the current Proposal DOCX, create/refresh/download the
server-managed working copy, finalize it into an immutable FinalDocxSnapshot,
generate/replay the final confirmed PDF, download the current final PDF/DOCX,
and read an aggregate status. Every mutating collaborator call already
exists in P6-A/P6-B/P6-B2b/P6-B3 -- this router only resolves the caller-
facing owner identity, maps closed domain statuses onto closed public HTTP
responses, and moves every synchronous SQLAlchemy/filesystem/subprocess call
off the event loop via ``starlette.concurrency.run_in_threadpool``.

Deliberately out of scope (see the stage's own design doc): generating a new
Proposal DOCX, ``ApprovedContentDocumentInput`` resolution, auth, tenants,
and any background/queued execution. Downloading the working copy is an
export, never a live edit-save-finalize roundtrip -- editing the downloaded
file does not update the server-managed ``current.docx``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfLineageKind
from app.schemas.cv_document_final_confirmed_pdf_orchestration import (
    FinalConfirmedPdfGenerationStatus,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshotBuildStatus
from app.schemas.cv_document_working_copy import (
    WorkingCopyCreateRefreshStatus,
    WorkingCopyInspectionStatus,
)
from app.schemas.cv_document_workflow_api import (
    ApiErrorResponse,
    CvDocumentFinalPdfResponse,
    CvDocumentFinalSnapshotResponse,
    CvDocumentWorkflowStatusResponse,
    CvDocumentWorkingCopyResponse,
)
from app.services.cv_document_blob_store import CvDocumentStorageError
from app.services.cv_document_final_confirmed_pdf_orchestrator import (
    generate_final_confirmed_pdf,
)
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfRepository,
    FinalConfirmedPdfRetrievalStatus,
)
from app.services.cv_document_final_docx_snapshot_pdf_source_reader import (
    FinalDocxSnapshotPdfSourceReader,
)
from app.services.cv_document_final_snapshot_repository import FinalDocxSnapshotRepository
from app.services.cv_document_final_source_reader import FinalDocxSourceReader
from app.services.cv_document_finalize_working_copy import (
    finalize_working_copy_to_final_docx_snapshot,
)
from app.services.cv_document_pdf_converter_protocol import DocxToPdfConverter
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_workflow_binary_response import (
    BinaryDocumentKind,
    build_binary_document_response,
)
from app.services.cv_document_workflow_deps import (
    CONVERSION_POLICY_VERSION,
    FINALIZATION_POLICY_VERSION,
    get_cv_document_artifact_repository,
    get_docx_to_pdf_converter,
    get_final_confirmed_pdf_repository,
    get_final_docx_snapshot_pdf_source_reader,
    get_final_docx_snapshot_repository,
    get_final_docx_source_reader,
    get_owner_resolver,
    get_sync_sessionmaker,
    get_working_copy_paths,
    get_working_copy_store,
    get_workflow_query_service,
    read_current_working_copy_bytes,
)
from app.services.cv_document_workflow_errors import (
    DOCUMENT_WORKFLOW_CONFLICT,
    DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    DOCUMENT_WORKFLOW_LOCKED,
    DOCUMENT_WORKFLOW_NOT_FOUND,
    DOCUMENT_WORKFLOW_UNAVAILABLE,
    ErrorSpec,
    build_error_response,
    internal_error_response,
)
from app.services.cv_document_workflow_owner_resolution import (
    CvDocumentWorkflowOwnerResolver,
    OwnerResolutionStatus,
)
from app.services.cv_document_workflow_query import (
    CvDocumentWorkflowQueryService,
    LatestFinalSnapshotQueryStatus,
    WorkflowStatusQueryStatus,
    query_latest_final_docx_snapshot,
)
from app.services.cv_document_working_copy_store import WorkingCopyStore

router = APIRouter(prefix="/jobs/{job_id}/cv-documents", tags=["CV Documents"])

_ERROR_RESPONSES = {
    404: {"model": ApiErrorResponse, "description": "Document workflow not found"},
    409: {"model": ApiErrorResponse, "description": "Conflict"},
    423: {"model": ApiErrorResponse, "description": "Locked"},
    500: {"model": ApiErrorResponse, "description": "Internal error"},
    503: {"model": ApiErrorResponse, "description": "Storage or converter unavailable"},
}


async def _resolve_owner(
    job_id: str, owner_resolver: CvDocumentWorkflowOwnerResolver
) -> object:
    """Returns a ``JobArtifactOwnerKey`` on success, or a ready-to-return
    error ``Response`` on failure -- callers check ``isinstance(result, Response)``."""

    result = await owner_resolver.resolve_owner_for_job(job_id)
    if result.status != OwnerResolutionStatus.FOUND:
        return build_error_response(DOCUMENT_WORKFLOW_NOT_FOUND)
    return result.owner_key


class _StorageUnavailable(Exception):
    """Internal-only signal from a threadpool worker back to its caller."""


class _InspectionFailure(Exception):
    """Internal-only signal carrying a non-``OBSERVED`` ``WorkingCopyInspectionStatus``
    from a threadpool worker back to its caller."""

    def __init__(self, status: WorkingCopyInspectionStatus) -> None:
        super().__init__(status.value)
        self.status = status


# ---------------------------------------------------------------------------
# GET status
# ---------------------------------------------------------------------------


@router.get(
    "",
    operation_id="get_cv_document_workflow_status",
    summary="Read the aggregate CV document workflow status",
    response_model=CvDocumentWorkflowStatusResponse,
    responses=_ERROR_RESPONSES,
)
async def get_cv_document_workflow_status(
    job_id: str,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    query_service: Annotated[CvDocumentWorkflowQueryService, Depends(get_workflow_query_service)],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        result = await run_in_threadpool(query_service.get_workflow_status, owner_key)
        if result.status != WorkflowStatusQueryStatus.FOUND:
            return build_error_response(DOCUMENT_WORKFLOW_UNAVAILABLE)
        assert result.response is not None
        return Response(
            content=result.response.model_dump_json(),
            media_type="application/json",
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001 - sole HTTP-boundary catch, see module docstring
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# GET proposal
# ---------------------------------------------------------------------------


@router.get(
    "/proposal",
    operation_id="download_cv_proposal",
    summary="Download the current Proposal DOCX",
    responses={
        200: {"content": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}}},
        304: {"description": "Not Modified"},
        **_ERROR_RESPONSES,
    },
)
async def download_cv_proposal(
    job_id: str,
    request: Request,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    artifact_repository: Annotated[
        CvDocumentArtifactRepository, Depends(get_cv_document_artifact_repository)
    ],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        def _read() -> bytes | None:
            artifact = artifact_repository.get_current_proposal(owner_key)
            if artifact is None:
                return None
            return artifact_repository.read_current_proposal_bytes(owner_key)

        try:
            content = await run_in_threadpool(_read)
        except (CvDocumentStorageError, OperationalError):
            return build_error_response(DOCUMENT_WORKFLOW_UNAVAILABLE)

        if content is None:
            return build_error_response(DOCUMENT_WORKFLOW_NOT_FOUND)

        return build_binary_document_response(
            kind=BinaryDocumentKind.PROPOSAL_DOCX,
            content=content,
            if_none_match=request.headers.get("if-none-match"),
            include_etag=True,
        )
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# POST working copy
# ---------------------------------------------------------------------------

_CREATE_REFRESH_SUCCESS_STATUSES = frozenset(
    {
        WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED,
        WorkingCopyCreateRefreshStatus.WORKING_COPY_REFRESHED,
        WorkingCopyCreateRefreshStatus.WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL,
        WorkingCopyCreateRefreshStatus.RECOVERED_UNEDITED_HALF_STATE,
        WorkingCopyCreateRefreshStatus.WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED,
    }
)

_CREATE_REFRESH_ERROR_MAP: dict[WorkingCopyCreateRefreshStatus, ErrorSpec] = {
    WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT: DOCUMENT_WORKFLOW_CONFLICT,
    WorkingCopyCreateRefreshStatus.NO_CURRENT_PROPOSAL: DOCUMENT_WORKFLOW_NOT_FOUND,
    WorkingCopyCreateRefreshStatus.REPOSITORY_BYTES_HASH_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyCreateRefreshStatus.CURRENT_PROPOSAL_CHANGED_DURING_OPERATION: DOCUMENT_WORKFLOW_CONFLICT,
    WorkingCopyCreateRefreshStatus.WORKING_COPY_CHANGED_DURING_READ: DOCUMENT_WORKFLOW_CONFLICT,
    WorkingCopyCreateRefreshStatus.WORKING_COPY_UNREADABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyCreateRefreshStatus.OWNER_LOCK_TIMEOUT: DOCUMENT_WORKFLOW_LOCKED,
    WorkingCopyCreateRefreshStatus.LOCK_PATH_IDENTITY_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyCreateRefreshStatus.LOCK_CAPABILITY_INVALID: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyCreateRefreshStatus.POST_WRITE_VERIFICATION_FAILED: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyCreateRefreshStatus.STORAGE_UNAVAILABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
}


@router.post(
    "/working-copy",
    operation_id="create_cv_working_copy",
    summary="Create or refresh the server-managed working copy",
    description=(
        "Creates or refreshes the working copy as a server-managed local "
        "file. This backend runs in local single-user mode -- the working "
        "copy is never a synced or cloud-tracked document. Editing happens "
        "against this server-managed local file; downloading it later "
        "produces an export, not a live edit-save-finalize roundtrip."
    ),
    response_model=CvDocumentWorkingCopyResponse,
    responses=_ERROR_RESPONSES,
)
async def create_cv_working_copy(
    job_id: str,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    artifact_repository: Annotated[
        CvDocumentArtifactRepository, Depends(get_cv_document_artifact_repository)
    ],
    working_copy_store: Annotated[WorkingCopyStore, Depends(get_working_copy_store)],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        def _op():
            return working_copy_store.create_or_refresh_working_copy(
                owner_key=owner_key, repository=artifact_repository
            )

        result = await run_in_threadpool(_op)

        if result.status in _CREATE_REFRESH_SUCCESS_STATUSES:
            body = CvDocumentWorkingCopyResponse(
                exists=True,
                edited_by_user=False,
                download_available=True,
                finalization_available=True,
            )
            status_code = (
                201 if result.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED else 200
            )
            return Response(
                content=body.model_dump_json(), media_type="application/json", status_code=status_code
            )

        spec = _CREATE_REFRESH_ERROR_MAP.get(result.status, DOCUMENT_WORKFLOW_UNAVAILABLE)
        return build_error_response(spec)
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# GET working copy
# ---------------------------------------------------------------------------

_INSPECT_ERROR_MAP: dict[WorkingCopyInspectionStatus, ErrorSpec] = {
    WorkingCopyInspectionStatus.WORKING_COPY_MISSING: DOCUMENT_WORKFLOW_NOT_FOUND,
    WorkingCopyInspectionStatus.WORKING_COPY_UNREADABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyInspectionStatus.WORKING_COPY_CHANGED_DURING_READ: DOCUMENT_WORKFLOW_CONFLICT,
    WorkingCopyInspectionStatus.OWNER_LOCK_TIMEOUT: DOCUMENT_WORKFLOW_LOCKED,
    WorkingCopyInspectionStatus.LOCK_PATH_IDENTITY_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyInspectionStatus.LOCK_CAPABILITY_INVALID: DOCUMENT_WORKFLOW_UNAVAILABLE,
    WorkingCopyInspectionStatus.STORAGE_UNAVAILABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
}


@router.get(
    "/working-copy",
    operation_id="download_cv_working_copy",
    summary="Download the current server-managed working copy DOCX",
    description=(
        "Downloads an export of the server-managed working copy DOCX. This "
        "backend runs in local single-user mode; the working copy itself "
        "always remains a server-managed local file. The downloaded file "
        "is not the authoritative edit source -- saving it elsewhere (for "
        "example, to a Downloads folder) does not automatically update the "
        "server-managed working copy, and uploading an edited DOCX back to "
        "the server is not part of this API stage."
    ),
    responses={
        200: {"content": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}}},
        **_ERROR_RESPONSES,
    },
)
async def download_cv_working_copy(
    job_id: str,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    working_copy_store: Annotated[WorkingCopyStore, Depends(get_working_copy_store)],
    working_copy_paths: Annotated[WorkingCopyPaths, Depends(get_working_copy_paths)],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        def _op() -> bytes | None:
            inspection = working_copy_store.inspect_working_copy(owner_key=owner_key)
            if inspection.status != WorkingCopyInspectionStatus.OBSERVED:
                raise _InspectionFailure(inspection.status)
            return read_current_working_copy_bytes(working_copy_paths, owner_key.owner_key_fingerprint)

        try:
            content = await run_in_threadpool(_op)
        except _InspectionFailure as failure:
            spec = _INSPECT_ERROR_MAP.get(failure.status, DOCUMENT_WORKFLOW_UNAVAILABLE)
            return build_error_response(spec)

        if content is None:
            return build_error_response(DOCUMENT_WORKFLOW_NOT_FOUND)

        return build_binary_document_response(
            kind=BinaryDocumentKind.WORKING_COPY_DOCX,
            content=content,
            include_etag=False,
        )
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# POST final snapshot
# ---------------------------------------------------------------------------

_FINALIZE_ERROR_MAP: dict[FinalDocxSnapshotBuildStatus, ErrorSpec] = {
    FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING: DOCUMENT_WORKFLOW_NOT_FOUND,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_UNREADABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCKED: DOCUMENT_WORKFLOW_LOCKED,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_CHANGED_DURING_READ: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_TOO_LARGE: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.FINAL_DOCX_HASH_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_FINGERPRINT_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_NOT_FOUND: DOCUMENT_WORKFLOW_NOT_FOUND,
    FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_OWNER_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_REVISION_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_HASH_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.STORAGE_METADATA_CONFLICT: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.STORAGE_UNAVAILABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.OWNER_KEY_FINGERPRINT_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISSING: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_INVALID: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_OWNER_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_TOO_LARGE: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.LOCK_CAPABILITY_INVALID: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
}


@router.post(
    "/final-snapshot",
    operation_id="finalize_cv_working_copy",
    summary="Finalize the working copy into an immutable FinalDocxSnapshot",
    response_model=CvDocumentFinalSnapshotResponse,
    responses=_ERROR_RESPONSES,
)
async def finalize_cv_working_copy(
    job_id: str,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    working_copy_store: Annotated[WorkingCopyStore, Depends(get_working_copy_store)],
    source_reader: Annotated[FinalDocxSourceReader, Depends(get_final_docx_source_reader)],
    snapshot_repository: Annotated[
        FinalDocxSnapshotRepository, Depends(get_final_docx_snapshot_repository)
    ],
    artifact_repository: Annotated[
        CvDocumentArtifactRepository, Depends(get_cv_document_artifact_repository)
    ],
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        previous_query = await run_in_threadpool(
            query_latest_final_docx_snapshot, session_factory, owner_key.owner_key_fingerprint
        )
        previous_fingerprint = (
            previous_query.snapshot.final_snapshot_fingerprint
            if previous_query.status == LatestFinalSnapshotQueryStatus.FOUND
            else None
        )

        def _op():
            return finalize_working_copy_to_final_docx_snapshot(
                owner_key=owner_key,
                working_copy_store=working_copy_store,
                source_reader=source_reader,
                snapshot_repository=snapshot_repository,
                artifact_repository=artifact_repository,
                finalization_policy_version=FINALIZATION_POLICY_VERSION,
            )

        result = await run_in_threadpool(_op)

        if result.status != FinalDocxSnapshotBuildStatus.FINALIZED:
            spec = _FINALIZE_ERROR_MAP.get(result.status, DOCUMENT_WORKFLOW_UNAVAILABLE)
            return build_error_response(spec)

        assert result.snapshot is not None
        is_new = result.snapshot.final_snapshot_fingerprint != previous_fingerprint
        body = CvDocumentFinalSnapshotResponse(
            exists=True,
            edited_by_user=result.snapshot.edited_by_user,
            download_available=True,
            created_at=None,
        )
        return Response(
            content=body.model_dump_json(),
            media_type="application/json",
            status_code=201 if is_new else 200,
        )
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# POST final PDF
# ---------------------------------------------------------------------------

_FINAL_PDF_PAYLOAD_STATUSES = frozenset(
    {
        FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED,
        FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED,
        FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT,
        FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT,
    }
)

_FINAL_PDF_STATUS_HTTP: dict[FinalConfirmedPdfGenerationStatus, int] = {
    FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED: 201,
    FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED: 200,
    FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT: 200,
    FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT: 409,
}

_FINAL_PDF_STATUS_STATE: dict[FinalConfirmedPdfGenerationStatus, str] = {
    FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED: "created",
    FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED: "reused",
    FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT: "already_current",
    FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT: "current_conflict",
}

_FINAL_PDF_ERROR_MAP: dict[FinalConfirmedPdfGenerationStatus, ErrorSpec] = {
    FinalConfirmedPdfGenerationStatus.OWNER_KEY_FINGERPRINT_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.FINAL_SNAPSHOT_NOT_FOUND: DOCUMENT_WORKFLOW_NOT_FOUND,
    FinalConfirmedPdfGenerationStatus.FINAL_SNAPSHOT_OWNER_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalConfirmedPdfGenerationStatus.FINAL_DOCX_BYTES_NOT_FOUND: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.FINAL_DOCX_HASH_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.CONVERTER_UNAVAILABLE: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.INVALID_REQUEST: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PROCESS_START_FAILED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PROCESS_OUTPUT_LIMIT_EXCEEDED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.CONVERSION_TIMEOUT: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PROCESS_TERMINATION_FAILED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.CONVERSION_FAILED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_MISSING: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_LOCKED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_UNREADABLE: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_CHANGED_DURING_READ: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_TOO_LARGE: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_INVALID: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.WORKSPACE_SETUP_FAILED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.WORKSPACE_CLEANUP_FAILED: DOCUMENT_WORKFLOW_CONVERTER_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_ARTIFACT_NOT_FOUND: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_BLOB_MISSING: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_BLOB_HASH_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_BLOB_SIZE_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.PDF_BLOB_MEDIA_TYPE_MISMATCH: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND: DOCUMENT_WORKFLOW_NOT_FOUND,
    FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH: DOCUMENT_WORKFLOW_CONFLICT,
    FinalConfirmedPdfGenerationStatus.OWNER_NOT_FOUND: DOCUMENT_WORKFLOW_NOT_FOUND,
    FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT: DOCUMENT_WORKFLOW_UNAVAILABLE,
    FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE: DOCUMENT_WORKFLOW_UNAVAILABLE,
}


@router.post(
    "/final-pdf",
    operation_id="generate_cv_final_pdf",
    summary="Generate (or replay) the final confirmed PDF for the latest FinalDocxSnapshot",
    response_model=CvDocumentFinalPdfResponse,
    responses=_ERROR_RESPONSES,
)
async def generate_cv_final_pdf(
    job_id: str,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    source_reader: Annotated[
        FinalDocxSnapshotPdfSourceReader, Depends(get_final_docx_snapshot_pdf_source_reader)
    ],
    converter: Annotated[DocxToPdfConverter, Depends(get_docx_to_pdf_converter)],
    repository: Annotated[FinalConfirmedPdfRepository, Depends(get_final_confirmed_pdf_repository)],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        snapshot_query = await run_in_threadpool(
            query_latest_final_docx_snapshot, session_factory, owner_key.owner_key_fingerprint
        )
        if snapshot_query.status == LatestFinalSnapshotQueryStatus.NOT_FOUND:
            return build_error_response(DOCUMENT_WORKFLOW_NOT_FOUND)
        if snapshot_query.status in (
            LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT,
            LatestFinalSnapshotQueryStatus.STORAGE_UNAVAILABLE,
        ):
            return build_error_response(DOCUMENT_WORKFLOW_UNAVAILABLE)

        assert snapshot_query.snapshot is not None
        final_snapshot_fingerprint = snapshot_query.snapshot.final_snapshot_fingerprint

        def _op():
            return generate_final_confirmed_pdf(
                owner_key=owner_key,
                final_snapshot_fingerprint=final_snapshot_fingerprint,
                conversion_policy_version=CONVERSION_POLICY_VERSION,
                source_reader=source_reader,
                converter=converter,
                repository=repository,
            )

        result = await run_in_threadpool(_op)

        if result.status not in _FINAL_PDF_PAYLOAD_STATUSES:
            spec = _FINAL_PDF_ERROR_MAP.get(result.status, DOCUMENT_WORKFLOW_UNAVAILABLE)
            return build_error_response(spec)

        assert result.artifact is not None and result.source_final_snapshot is not None
        is_current = result.status != FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT
        body = CvDocumentFinalPdfResponse(
            exists=True,
            is_current=is_current,
            state=_FINAL_PDF_STATUS_STATE[result.status],
            freshness="fresh" if is_current else "unavailable",
            download_available=is_current,
            edited_by_user=result.source_final_snapshot.edited_by_user,
            updated_at=None,
        )
        return Response(
            content=body.model_dump_json(),
            media_type="application/json",
            status_code=_FINAL_PDF_STATUS_HTTP[result.status],
        )
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# GET final PDF
# ---------------------------------------------------------------------------


@router.get(
    "/final-pdf",
    operation_id="download_cv_final_pdf",
    summary="Download the current final confirmed PDF",
    responses={
        200: {"content": {"application/pdf": {}}},
        304: {"description": "Not Modified"},
        **_ERROR_RESPONSES,
    },
)
async def download_cv_final_pdf(
    job_id: str,
    request: Request,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    repository: Annotated[FinalConfirmedPdfRepository, Depends(get_final_confirmed_pdf_repository)],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        def _op() -> bytes | None:
            authority = repository.read_current_authority(owner_key)
            if authority.status in (
                FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT,
                FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE,
            ):
                raise _StorageUnavailable()
            observation = authority.observation
            if observation is None or not observation.exists:
                return None
            if observation.lineage_kind != ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT:
                return None
            assert observation.current_artifact_fingerprint is not None
            retrieval = repository.retrieve_pdf_bytes_by_artifact(observation.current_artifact_fingerprint)
            if retrieval.status in (
                FinalConfirmedPdfRetrievalStatus.STORAGE_METADATA_CONFLICT,
                FinalConfirmedPdfRetrievalStatus.STORAGE_UNAVAILABLE,
                FinalConfirmedPdfRetrievalStatus.BLOB_MISSING,
                FinalConfirmedPdfRetrievalStatus.BLOB_HASH_MISMATCH,
                FinalConfirmedPdfRetrievalStatus.BLOB_SIZE_MISMATCH,
                FinalConfirmedPdfRetrievalStatus.BLOB_MEDIA_TYPE_MISMATCH,
            ):
                raise _StorageUnavailable()
            if retrieval.status == FinalConfirmedPdfRetrievalStatus.ARTIFACT_NOT_FOUND:
                return None
            assert retrieval.pdf_bytes is not None
            return retrieval.pdf_bytes

        try:
            content = await run_in_threadpool(_op)
        except _StorageUnavailable:
            return build_error_response(DOCUMENT_WORKFLOW_UNAVAILABLE)

        if content is None:
            return build_error_response(DOCUMENT_WORKFLOW_NOT_FOUND)

        return build_binary_document_response(
            kind=BinaryDocumentKind.FINAL_PDF,
            content=content,
            if_none_match=request.headers.get("if-none-match"),
            include_etag=True,
        )
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)


# ---------------------------------------------------------------------------
# GET final DOCX
# ---------------------------------------------------------------------------


@router.get(
    "/final-docx",
    operation_id="download_cv_final_docx",
    summary="Download the current final DOCX (the latest FinalDocxSnapshot's exact bytes)",
    responses={
        200: {"content": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}}},
        304: {"description": "Not Modified"},
        **_ERROR_RESPONSES,
    },
)
async def download_cv_final_docx(
    job_id: str,
    request: Request,
    owner_resolver: Annotated[CvDocumentWorkflowOwnerResolver, Depends(get_owner_resolver)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    snapshot_repository: Annotated[
        FinalDocxSnapshotRepository, Depends(get_final_docx_snapshot_repository)
    ],
) -> Response:
    try:
        owner_key = await _resolve_owner(job_id, owner_resolver)
        if isinstance(owner_key, Response):
            return owner_key

        def _op() -> bytes | None:
            snapshot_query = query_latest_final_docx_snapshot(
                session_factory, owner_key.owner_key_fingerprint
            )
            if snapshot_query.status in (
                LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT,
                LatestFinalSnapshotQueryStatus.STORAGE_UNAVAILABLE,
            ):
                raise _StorageUnavailable()
            if snapshot_query.status == LatestFinalSnapshotQueryStatus.NOT_FOUND:
                return None
            assert snapshot_query.snapshot is not None
            return snapshot_repository.retrieve_final_docx_bytes(snapshot_query.snapshot.final_docx_sha256)

        try:
            content = await run_in_threadpool(_op)
        except _StorageUnavailable:
            return build_error_response(DOCUMENT_WORKFLOW_UNAVAILABLE)

        if content is None:
            return build_error_response(DOCUMENT_WORKFLOW_NOT_FOUND)

        return build_binary_document_response(
            kind=BinaryDocumentKind.FINAL_DOCX,
            content=content,
            if_none_match=request.headers.get("if-none-match"),
            include_etag=True,
        )
    except Exception as exc:  # noqa: BLE001
        return internal_error_response(exc)
