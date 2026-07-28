"""Explicit Provenance Stage P6-C1a: read-only query services for the CV
Document Workflow API.

This module owns the only deterministic ``latest FinalDocxSnapshot`` lookup
(``owner_key_fingerprint`` scoped, ``created_at DESC, final_snapshot_fingerprint
DESC`` tie-broken) and the status-aggregate builder consumed by the router.
It may use synchronous SQLAlchemy internally -- the router itself never
imports SQLAlchemy and never sees an ORM row; every read here returns a
closed, typed result.

Nothing here mutates the database, computes a fingerprint, or trusts an ORM
row's shape without an independent Pydantic validation pass.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_document_artifact import JobArtifactOwnerKey
from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfLineageKind
from app.schemas.cv_document_final_confirmed_pdf_freshness import FinalConfirmedPdfFreshnessStatus
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot
from app.schemas.cv_document_working_copy import WorkingCopyInspectionStatus
from app.schemas.cv_document_workflow_api import (
    CvDocumentFinalPdfResponse,
    CvDocumentFinalSnapshotResponse,
    CvDocumentProposalResponse,
    CvDocumentWorkflowStatusResponse,
    CvDocumentWorkingCopyResponse,
)
from app.services.cv_document_blob_store import CvDocumentStorageError
from app.services.cv_document_final_confirmed_pdf_freshness import (
    evaluate_final_confirmed_pdf_freshness,
)
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfRepository,
)
from app.services.cv_document_models import CvDocumentArtifactOwner, CvDocxFinalSnapshotRow
from app.services.cv_document_pdf_converter_protocol import DocxToPdfConverter
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.services.cv_document_storage_shared import row_to_owner_key
from app.services.cv_document_working_copy_store import WorkingCopyStore


# -- latest FinalDocxSnapshot query -----------------------------------------


class LatestFinalSnapshotQueryStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STORAGE_METADATA_CONFLICT = "STORAGE_METADATA_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class LatestFinalSnapshotQueryResult(BaseModel):
    """The single closed return value of ``query_latest_final_docx_snapshot``.

    Only ``FOUND`` ever carries ``snapshot``/``created_at``. ``snapshot`` is
    the full internal domain object (its ``final_snapshot_fingerprint`` is
    used internally to call PDF finalization/DOCX retrieval) -- it is never
    serialized verbatim into a public JSON response by the router.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    status: LatestFinalSnapshotQueryStatus
    snapshot: FinalDocxSnapshot | None = None
    created_at: str | None = None


def query_latest_final_docx_snapshot(
    session_factory: sessionmaker[Session], owner_key_fingerprint: str
) -> LatestFinalSnapshotQueryResult:
    """Deterministic latest-snapshot lookup for one owner.

    ``ORDER BY created_at DESC, final_snapshot_fingerprint DESC LIMIT 1`` --
    the fingerprint tiebreaker is mandatory so two rows sharing the same
    ``created_at`` string never make this query's result depend on
    unspecified storage-engine row order. Read-only: opens no write
    transaction and never mutates ``cv_docx_final_snapshots``.
    """

    try:
        with session_factory() as session:
            row = (
                session.execute(
                    select(CvDocxFinalSnapshotRow)
                    .where(CvDocxFinalSnapshotRow.owner_key_fingerprint == owner_key_fingerprint)
                    .order_by(
                        CvDocxFinalSnapshotRow.created_at.desc(),
                        CvDocxFinalSnapshotRow.final_snapshot_fingerprint.desc(),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if row is None:
                return LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND)

            owner_row = session.get(CvDocumentArtifactOwner, owner_key_fingerprint)
            if owner_row is None or owner_row.owner_key_fingerprint != row.owner_key_fingerprint:
                return LatestFinalSnapshotQueryResult(
                    status=LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT
                )
            owner_key = row_to_owner_key(owner_row)

            try:
                snapshot = FinalDocxSnapshot(
                    owner_key=owner_key,
                    source_proposal_artifact_fingerprint=row.source_proposal_artifact_fingerprint,
                    source_proposal_revision=row.source_proposal_revision,
                    generated_proposal_sha256=row.generated_proposal_sha256,
                    final_docx_sha256=row.final_docx_sha256,
                    edited_by_user=bool(row.edited_by_user),
                    finalization_policy_version=row.finalization_policy_version,
                    snapshot_schema_version=row.snapshot_schema_version,
                    final_snapshot_fingerprint=row.final_snapshot_fingerprint,
                )
            except ValidationError:
                return LatestFinalSnapshotQueryResult(
                    status=LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT
                )

            return LatestFinalSnapshotQueryResult(
                status=LatestFinalSnapshotQueryStatus.FOUND,
                snapshot=snapshot,
                created_at=row.created_at,
            )
    except OperationalError:
        return LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.STORAGE_UNAVAILABLE)


# -- status aggregate ---------------------------------------------------------


class WorkflowStatusQueryStatus(StrEnum):
    FOUND = "FOUND"
    UNAVAILABLE = "UNAVAILABLE"


class WorkflowStatusQueryResult(BaseModel):
    """The single closed return value of
    ``CvDocumentWorkflowQueryService.get_workflow_status``. Only ``FOUND``
    ever carries ``response`` -- ``UNAVAILABLE`` always means a critical
    source (proposal/working-copy/final-snapshot/final-pdf authority) hit a
    hard storage failure, never a fabricated ``exists=False``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: WorkflowStatusQueryStatus
    response: CvDocumentWorkflowStatusResponse | None = None


_UNAVAILABLE = WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.UNAVAILABLE)


class CvDocumentWorkflowQueryService:
    """Builds the CV Document Workflow status aggregate from existing,
    already-productionized read paths -- never from filesystem presence
    guesses, and never by opening a blob path directly."""

    def __init__(
        self,
        *,
        proposal_repository: CvDocumentArtifactRepository,
        working_copy_store: WorkingCopyStore,
        final_snapshot_session_factory: sessionmaker[Session],
        final_confirmed_pdf_repository: FinalConfirmedPdfRepository,
        converter: DocxToPdfConverter,
        conversion_policy_version: str,
    ) -> None:
        self._proposal_repository = proposal_repository
        self._working_copy_store = working_copy_store
        self._final_snapshot_session_factory = final_snapshot_session_factory
        self._final_confirmed_pdf_repository = final_confirmed_pdf_repository
        self._converter = converter
        self._conversion_policy_version = conversion_policy_version

    def get_workflow_status(self, owner_key: JobArtifactOwnerKey) -> WorkflowStatusQueryResult:
        try:
            current_proposal = self._proposal_repository.get_current_proposal(owner_key)
        except (CvDocumentStorageError, OperationalError):
            return _UNAVAILABLE

        proposal_response = CvDocumentProposalResponse(
            exists=current_proposal is not None,
            revision=current_proposal.proposal_revision if current_proposal is not None else None,
            download_available=current_proposal is not None,
            updated_at=None,
        )

        try:
            inspection = self._working_copy_store.inspect_working_copy(owner_key=owner_key)
        except Exception:  # pragma: no cover - store's own public methods never raise
            return _UNAVAILABLE

        if inspection.status in (
            WorkingCopyInspectionStatus.WORKING_COPY_UNREADABLE,
            WorkingCopyInspectionStatus.WORKING_COPY_CHANGED_DURING_READ,
            WorkingCopyInspectionStatus.OWNER_LOCK_TIMEOUT,
            WorkingCopyInspectionStatus.LOCK_PATH_IDENTITY_MISMATCH,
            WorkingCopyInspectionStatus.LOCK_CAPABILITY_INVALID,
            WorkingCopyInspectionStatus.STORAGE_UNAVAILABLE,
        ):
            return _UNAVAILABLE

        if inspection.status == WorkingCopyInspectionStatus.WORKING_COPY_MISSING:
            working_copy_response = CvDocumentWorkingCopyResponse(
                exists=False,
                edited_by_user=None,
                download_available=False,
                finalization_available=False,
            )
        else:
            observed_state = inspection.observed_state
            assert observed_state is not None
            edited_by_user: bool | None = None
            if (
                current_proposal is not None
                and observed_state.bound_proposal_artifact_fingerprint
                == current_proposal.artifact_fingerprint
                and observed_state.bound_proposal_revision == current_proposal.proposal_revision
            ):
                edited_by_user = (
                    observed_state.working_copy_sha256 != current_proposal.generated_docx_content_hash
                )
            working_copy_response = CvDocumentWorkingCopyResponse(
                exists=True,
                edited_by_user=edited_by_user,
                download_available=True,
                finalization_available=True,
            )

        snapshot_query = query_latest_final_docx_snapshot(
            self._final_snapshot_session_factory, owner_key.owner_key_fingerprint
        )
        if snapshot_query.status in (
            LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT,
            LatestFinalSnapshotQueryStatus.STORAGE_UNAVAILABLE,
        ):
            return _UNAVAILABLE

        latest_snapshot = snapshot_query.snapshot
        final_snapshot_response = CvDocumentFinalSnapshotResponse(
            exists=latest_snapshot is not None,
            edited_by_user=latest_snapshot.edited_by_user if latest_snapshot is not None else None,
            download_available=latest_snapshot is not None,
            created_at=snapshot_query.created_at,
        )

        try:
            authority_read = self._final_confirmed_pdf_repository.read_current_authority(owner_key)
        except Exception:  # pragma: no cover - Protocol methods never raise
            return _UNAVAILABLE

        if authority_read.status in (
            FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT,
            FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE,
        ):
            return _UNAVAILABLE

        observation = authority_read.observation
        assert observation is not None
        is_final_lineage = observation.exists and observation.lineage_kind == ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT

        pdf_edited_by_user: bool | None = None
        if is_final_lineage and latest_snapshot is not None:
            assert observation.current_artifact_fingerprint is not None
            try:
                artifact_read = self._final_confirmed_pdf_repository.read_artifact(
                    observation.current_artifact_fingerprint
                )
            except Exception:  # pragma: no cover - Protocol methods never raise
                artifact_read = None
            if (
                artifact_read is not None
                and artifact_read.status == FinalConfirmedPdfArtifactReadStatus.FOUND
                and artifact_read.artifact is not None
                and artifact_read.artifact.source_final_snapshot_fingerprint
                == latest_snapshot.final_snapshot_fingerprint
            ):
                pdf_edited_by_user = latest_snapshot.edited_by_user

        final_pdf_response = CvDocumentFinalPdfResponse(
            exists=is_final_lineage,
            is_current=is_final_lineage,
            state="already_current" if is_final_lineage else "missing",
            freshness=self._evaluate_freshness_label(
                owner_key=owner_key, latest_snapshot=latest_snapshot, has_current_pdf=is_final_lineage
            ),
            download_available=is_final_lineage,
            edited_by_user=pdf_edited_by_user,
            updated_at=None,
        )

        available_actions: list[str] = []
        if proposal_response.exists:
            available_actions.append("download_proposal")
            available_actions.append("create_working_copy")
        if working_copy_response.exists:
            available_actions.append("download_working_copy")
            available_actions.append("finalize_working_copy")
        if final_snapshot_response.exists:
            available_actions.append("generate_final_pdf")
            available_actions.append("download_final_docx")
        if final_pdf_response.exists and final_pdf_response.is_current:
            available_actions.append("download_final_pdf")

        response = CvDocumentWorkflowStatusResponse(
            proposal=proposal_response,
            working_copy=working_copy_response,
            final_snapshot=final_snapshot_response,
            final_pdf=final_pdf_response,
            available_actions=tuple(available_actions),  # type: ignore[arg-type]
        )
        return WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.FOUND, response=response)

    def _evaluate_freshness_label(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        latest_snapshot: FinalDocxSnapshot | None,
        has_current_pdf: bool,
    ) -> str:
        if not has_current_pdf or latest_snapshot is None:
            return "missing"

        try:
            runtime_identity = self._converter.runtime_identity
        except Exception:  # pragma: no cover - converter contract violation
            return "unavailable"
        if runtime_identity is None:
            return "unavailable"

        freshness_result = evaluate_final_confirmed_pdf_freshness(
            owner_key=owner_key,
            current_final_snapshot_fingerprint=latest_snapshot.final_snapshot_fingerprint,
            current_final_docx_sha256=latest_snapshot.final_docx_sha256,
            current_runtime_identity_fingerprint=runtime_identity.runtime_identity_fingerprint,
            current_conversion_policy_version=self._conversion_policy_version,
            repository=self._final_confirmed_pdf_repository,
        )
        if freshness_result.status == FinalConfirmedPdfFreshnessStatus.FRESH:
            return "fresh"
        if freshness_result.status in (
            FinalConfirmedPdfFreshnessStatus.STALE_SOURCE_SNAPSHOT,
            FinalConfirmedPdfFreshnessStatus.STALE_RUNTIME_IDENTITY,
            FinalConfirmedPdfFreshnessStatus.STALE_CONVERSION_POLICY,
        ):
            return "stale"
        if freshness_result.status == FinalConfirmedPdfFreshnessStatus.NOT_FOUND:
            return "missing"
        return "unavailable"
