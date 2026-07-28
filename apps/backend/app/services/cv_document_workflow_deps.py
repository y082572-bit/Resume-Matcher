"""Explicit Provenance Stage P6-C1a: FastAPI dependency providers for the
CV Document Workflow API.

This module is the first production composition root for the P6-A/P6-B
document-storage/working-copy/PDF-conversion stack -- every provider below
builds the exact production collaborators those stages already ship
(``CvDocumentArtifactSqlRepository``, ``FinalDocxSnapshotSqlRepository``,
``FinalConfirmedPdfSqlRepository``, ``WorkingCopyStore``,
``LibreOfficeDocxToPdfConverter``) from local, single-user-deployment
defaults. Every provider is a plain function (or a small ``Depends`` chain
of them) so ``app.dependency_overrides`` can replace any of them in tests
without touching the router.

Root-bound singletons (blob store, working copy store, converter) are
cached with ``functools.lru_cache`` -- they own real filesystem roots and a
cached converter runtime-identity observation, so they must not be rebuilt
per request. Everything else (repository wrappers, the owner resolver, the
query service) is cheap, stateless, and built fresh per request.

Working-copy/blob storage roots deliberately live under the user's home
directory, never inside a Git worktree: both ``WorkingCopyPaths`` and
``LibreOfficeDocxToPdfConverter`` fail-closed reject a root that lives
inside (or is itself) a Git-managed working tree / their own scratch root
(see ``cv_document_working_copy_paths.py``/``cv_document_libreoffice_adapter.py``).
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from app.database import db as _database
from app.services.candidate_identity_binding_sql_repository import (
    CandidateIdentityBindingSqlRepository,
)
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfRepository,
)
from app.services.cv_document_final_confirmed_pdf_sql_repository import (
    FinalConfirmedPdfSqlRepository,
)
from app.services.cv_document_final_docx_snapshot_pdf_source_reader import (
    FinalDocxSnapshotPdfSourceReader,
)
from app.services.cv_document_final_snapshot_repository import FinalDocxSnapshotRepository
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
from app.services.cv_document_final_source_filesystem_reader import FilesystemFinalDocxSourceReader
from app.services.cv_document_final_source_reader import FinalDocxSourceReader
from app.services.cv_document_libreoffice_adapter import LibreOfficeDocxToPdfConverter
from app.services.cv_document_pdf_converter_protocol import DocxToPdfConverter
from app.services.cv_document_process_runner import PosixProcessRunner
from app.services.cv_document_proposal_artifact_lineage_sql_reader import (
    ProposalArtifactLineageSqlReader,
)
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.schemas.cv_document_working_copy import WorkingCopyStableReadStatus
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_stable_read import posix_stable_read, windows_stable_read
from app.services.cv_document_working_copy_store import WorkingCopyStore
from app.services.cv_document_workflow_owner_resolution import CvDocumentWorkflowOwnerResolver
from app.services.cv_document_workflow_query import CvDocumentWorkflowQueryService

#: Server-side constants -- never accepted from a caller. Shared by every
#: provider/endpoint that needs them so the finalization/conversion policy
#: version used to build an artifact always matches the one used to
#: evaluate its freshness.
FINALIZATION_POLICY_VERSION = "cv-document-workflow-api-finalization-policy-v1"
CONVERSION_POLICY_VERSION = "cv-document-workflow-api-conversion-policy-v1"

#: Local, single-user-deployment storage roots -- deliberately outside any
#: Git worktree (see module docstring).
_CV_DOCUMENTS_ROOT = Path.home() / ".resume_matcher" / "cv_documents"
_BLOB_STORE_ROOT = _CV_DOCUMENTS_ROOT / "blobs"
_WORKING_COPY_ROOT = _CV_DOCUMENTS_ROOT / "working_copies"
_SCRATCH_ROOT = _CV_DOCUMENTS_ROOT / "scratch"
#: Purely a safety-check reference point for
#: ``LibreOfficeDocxToPdfConverter``'s scratch/repo ancestor-relationship
#: guard -- never itself read from or written to.
_APP_ROOT = Path(__file__).resolve().parents[1]

_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
_MAX_BINDING_SIZE_BYTES = 1_048_576
_LIBREOFFICE_TIMEOUT_SECONDS = 60.0
_FONT_ENVIRONMENT_ID = "resume-matcher-cv-document-workflow-fonts-v1"


def _default_soffice_path() -> Path:
    if sys.platform == "darwin":
        return Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if sys.platform == "win32":
        return Path("C:\\Program Files\\LibreOffice\\program\\soffice.exe")
    return Path("/usr/bin/soffice")


# -- root-bound singletons (cached across requests) --------------------------


def get_sync_sessionmaker() -> sessionmaker[Session]:
    """The production synchronous sessionmaker backing every CV Document
    Workflow read/write.

    ``app.database.Database`` exposes its sync engine's sessionmaker only
    as the private ``_sync`` property (built originally for the encrypted
    ``api_keys`` synchronous read path) -- there is no public accessor.
    This provider is the single sanctioned place that reaches into
    ``db._sync``; the router and every other collaborator depend on this
    provider instead of importing ``db`` directly.
    """

    return _database._sync


@lru_cache(maxsize=1)
def get_cv_document_blob_store() -> CvDocumentBlobStore:
    return CvDocumentBlobStore(_BLOB_STORE_ROOT, max_blob_size_bytes=_MAX_DOCUMENT_BYTES)


@lru_cache(maxsize=1)
def get_working_copy_paths() -> WorkingCopyPaths:
    """The validated Working Copy Root Contract. Not one of the 10
    explicitly named providers, but exposed (rather than kept module-
    private) so the GET working-copy download endpoint can derive
    ``current.docx``'s fixed path through the same contract
    ``WorkingCopyStore`` itself uses internally, without the router ever
    accepting or deriving an arbitrary filesystem path.
    """

    return WorkingCopyPaths(_WORKING_COPY_ROOT, _BLOB_STORE_ROOT)


@lru_cache(maxsize=1)
def get_working_copy_store() -> WorkingCopyStore:
    return WorkingCopyStore(get_working_copy_paths(), max_docx_size_bytes=_MAX_DOCUMENT_BYTES)


@lru_cache(maxsize=1)
def get_docx_to_pdf_converter() -> DocxToPdfConverter:
    process_runner = PosixProcessRunner(
        max_stdout_bytes=1_000_000,
        max_stderr_bytes=1_000_000,
        read_chunk_size=65_536,
        terminate_grace_seconds=5.0,
        final_wait_seconds=2.0,
        reader_join_timeout_seconds=5.0,
    )
    return LibreOfficeDocxToPdfConverter(
        soffice_path=_default_soffice_path(),
        scratch_root=_SCRATCH_ROOT,
        repo_root=_APP_ROOT,
        working_copy_root=_WORKING_COPY_ROOT,
        blob_root=_BLOB_STORE_ROOT,
        font_environment_id=_FONT_ENVIRONMENT_ID,
        max_docx_size_bytes=_MAX_DOCUMENT_BYTES,
        max_pdf_size_bytes=_MAX_DOCUMENT_BYTES,
        timeout_seconds=_LIBREOFFICE_TIMEOUT_SECONDS,
        process_runner=process_runner,
    )


# -- per-request, cheap collaborators -----------------------------------------


def get_cv_document_artifact_repository(
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    blob_store: Annotated[CvDocumentBlobStore, Depends(get_cv_document_blob_store)],
) -> CvDocumentArtifactRepository:
    return CvDocumentArtifactSqlRepository(session_factory, blob_store)


def get_final_docx_snapshot_repository(
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    blob_store: Annotated[CvDocumentBlobStore, Depends(get_cv_document_blob_store)],
) -> FinalDocxSnapshotRepository:
    return FinalDocxSnapshotSqlRepository(session_factory, blob_store)


def get_final_confirmed_pdf_repository(
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    blob_store: Annotated[CvDocumentBlobStore, Depends(get_cv_document_blob_store)],
) -> FinalConfirmedPdfRepository:
    return FinalConfirmedPdfSqlRepository(session_factory, blob_store)


def get_final_docx_snapshot_pdf_source_reader(
    repository: Annotated[FinalDocxSnapshotRepository, Depends(get_final_docx_snapshot_repository)],
) -> FinalDocxSnapshotPdfSourceReader:
    return FinalDocxSnapshotPdfSourceReader(repository)


def get_final_docx_source_reader(
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    blob_store: Annotated[CvDocumentBlobStore, Depends(get_cv_document_blob_store)],
) -> FinalDocxSourceReader:
    """The real, filesystem-backed working-copy-to-final-snapshot reader
    consumed by ``finalize_working_copy_to_final_docx_snapshot``. Not one
    of the 10 explicitly named providers, but required for the
    POST final-snapshot endpoint to function against real storage; kept
    here (rather than inline in the router) so it stays overridable via
    ``app.dependency_overrides`` exactly like every other collaborator.
    """

    lineage_reader = ProposalArtifactLineageSqlReader(session_factory, blob_store)
    return FilesystemFinalDocxSourceReader(
        get_working_copy_paths(),
        lineage_reader,
        max_docx_size_bytes=_MAX_DOCUMENT_BYTES,
        max_binding_size_bytes=_MAX_BINDING_SIZE_BYTES,
    )


def get_owner_resolver(
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
) -> CvDocumentWorkflowOwnerResolver:
    binding_repository = CandidateIdentityBindingSqlRepository(session_factory)
    return CvDocumentWorkflowOwnerResolver(database=_database, binding_repository=binding_repository)


def get_workflow_query_service(
    proposal_repository: Annotated[
        CvDocumentArtifactRepository, Depends(get_cv_document_artifact_repository)
    ],
    working_copy_store: Annotated[WorkingCopyStore, Depends(get_working_copy_store)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_sync_sessionmaker)],
    final_confirmed_pdf_repository: Annotated[
        FinalConfirmedPdfRepository, Depends(get_final_confirmed_pdf_repository)
    ],
    converter: Annotated[DocxToPdfConverter, Depends(get_docx_to_pdf_converter)],
) -> CvDocumentWorkflowQueryService:
    return CvDocumentWorkflowQueryService(
        proposal_repository=proposal_repository,
        working_copy_store=working_copy_store,
        final_snapshot_session_factory=session_factory,
        final_confirmed_pdf_repository=final_confirmed_pdf_repository,
        converter=converter,
        conversion_policy_version=CONVERSION_POLICY_VERSION,
    )


# -- plain (non-``Depends``) helper --------------------------------------------


def read_current_working_copy_bytes(paths: WorkingCopyPaths, owner_key_fingerprint: str) -> bytes | None:
    """Read one owner's exact ``current.docx`` bytes via the same platform
    stable-read primitive (``posix_stable_read``/``windows_stable_read``)
    ``WorkingCopyStore`` itself uses internally -- so the GET working-copy
    download endpoint never opens a path directly in the router.

    Returns ``None`` for any non-``STABLE_READ_OK`` outcome (missing,
    locked, unreadable, changed-during-read); the router derives the exact
    HTTP status from ``WorkingCopyStore.inspect_working_copy``'s own
    status instead of inspecting the cause here.
    """

    docx_path = paths.current_docx_path(owner_key_fingerprint)
    if sys.platform == "win32":
        result = windows_stable_read(docx_path, max_size_bytes=_MAX_DOCUMENT_BYTES)
    else:
        result = posix_stable_read(docx_path, max_size_bytes=_MAX_DOCUMENT_BYTES)
    if result.status != WorkingCopyStableReadStatus.STABLE_READ_OK:
        return None
    assert result.data is not None
    return result.data
