"""Explicit Provenance Stage P6-C1a: public HTTP DTOs for the CV Document
Workflow API.

Every model here is a wholly independent, closed public contract -- never a
re-export or subclass of an internal domain model (``CvDocxProposalArtifact``,
``ValidatedCvDocxSnapshot``, ``FinalDocxSnapshot``, ``FinalConfirmedPdfArtifact``,
``JobArtifactOwnerKey``, ...). No field here ever carries a fingerprint, a
SHA-256 hash, a storage key, a filesystem path, an executable path, a runtime
identity, or a schema-version string -- those all remain internal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiErrorResponse(BaseModel):
    """The single closed public error envelope for this router.

    ``details`` may only ever contain explicitly approved, closed fields --
    never raw exception text, a stack trace, or an internal identifier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    retryable: bool
    request_id: str | None = None
    details: dict[str, str] | None = None


class CvDocumentProposalResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exists: bool
    revision: int | None = None
    download_available: bool
    updated_at: datetime | None = None


class CvDocumentWorkingCopyResponse(BaseModel):
    """This backend runs in local single-user mode. The working copy is
    always a server-managed local file -- a downloaded working-copy DOCX is
    an export snapshot, never a live edit-save-finalize roundtrip. Saving
    the downloaded file elsewhere (for example, to a Downloads folder) does
    not update the server-managed working copy, and uploading an edited
    DOCX back to the server is not part of this API stage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exists: bool
    edited_by_user: bool | None = None
    download_available: bool
    finalization_available: bool
    editing_mode: Literal["local_managed_file"] = Field(
        default="local_managed_file",
        description=(
            "This backend runs in local single-user mode. The working copy "
            "is always a server-managed local file, not a synced or "
            "cloud-tracked document."
        ),
    )
    download_is_authoritative_edit_source: Literal[False] = Field(
        default=False,
        description=(
            "The downloaded working-copy DOCX is an export snapshot, not "
            "the authoritative edit source. Saving the downloaded file "
            "(for example, to a Downloads folder) does not automatically "
            "update the server-managed working copy. Uploading an edited "
            "DOCX back to the server is not part of this API stage."
        ),
    )


class CvDocumentFinalSnapshotResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exists: bool
    edited_by_user: bool | None = None
    download_available: bool
    created_at: datetime | None = None


class CvDocumentFinalPdfResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exists: bool
    is_current: bool
    state: Literal[
        "missing",
        "created",
        "reused",
        "already_current",
        "current_conflict",
    ]
    freshness: Literal["fresh", "stale", "missing", "unavailable"]
    download_available: bool
    edited_by_user: bool | None = None
    updated_at: datetime | None = None


class CvDocumentWorkflowStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal: CvDocumentProposalResponse
    working_copy: CvDocumentWorkingCopyResponse
    final_snapshot: CvDocumentFinalSnapshotResponse
    final_pdf: CvDocumentFinalPdfResponse
    available_actions: tuple[
        Literal[
            "download_proposal",
            "create_working_copy",
            "download_working_copy",
            "finalize_working_copy",
            "generate_final_pdf",
            "download_final_pdf",
            "download_final_docx",
        ],
        ...,
    ] = ()
