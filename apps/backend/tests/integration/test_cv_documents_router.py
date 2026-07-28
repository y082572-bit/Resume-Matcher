"""Integration tests for the P6-C1a CV Document Workflow API router.

Every collaborator (owner resolver, artifact repository, working copy
store, final snapshot/final confirmed PDF repositories, converter, query
service) is faked via ``app.dependency_overrides`` or a targeted
``monkeypatch`` of the composition-root functions the router imports by
name (``finalize_working_copy_to_final_docx_snapshot``,
``generate_final_confirmed_pdf``, ``query_latest_final_docx_snapshot``,
``read_current_working_copy_bytes``) -- no real filesystem/SQLite/subprocess
access is exercised here.
"""

from __future__ import annotations

import hashlib
import threading
import types
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

import app.routers.cv_documents as cvdocs
from app.main import app
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfLineageKind
from app.schemas.cv_document_final_confirmed_pdf_orchestration import (
    FinalConfirmedPdfGenerationResult,
    FinalConfirmedPdfGenerationStatus,
)
from app.schemas.cv_document_final_snapshot import (
    FINAL_SNAPSHOT_SCHEMA_VERSION,
    FinalDocxSnapshot,
    FinalDocxSnapshotBuildStatus,
)
from app.schemas.cv_document_finalize_working_copy import FinalizeWorkingCopyResult
from app.schemas.cv_document_working_copy import (
    WorkingCopyBinding,
    WorkingCopyCreateRefreshResult,
    WorkingCopyCreateRefreshStatus,
    WorkingCopyInspectionResult,
    WorkingCopyInspectionStatus,
    WorkingCopyObservedState,
    WorkingCopyBindingState,
    compute_binding_state_fingerprint,
)
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadResult,
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfRetrievalResult,
    FinalConfirmedPdfRetrievalStatus,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_workflow_deps import (
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
)
from app.services.cv_document_workflow_owner_resolution import (
    OwnerResolutionResult,
    OwnerResolutionStatus,
)
from app.services.cv_document_workflow_query import (
    LatestFinalSnapshotQueryResult,
    LatestFinalSnapshotQueryStatus,
    WorkflowStatusQueryResult,
    WorkflowStatusQueryStatus,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class FakeOwnerResolver:
    def __init__(self, *, owner_key=None, found: bool = True):
        self.owner_key = owner_key or _owner_key()
        self.found = found
        self.calls: list[str] = []

    async def resolve_owner_for_job(self, job_id: str) -> OwnerResolutionResult:
        self.calls.append(job_id)
        if not self.found:
            return OwnerResolutionResult(status=OwnerResolutionStatus.NOT_FOUND)
        return OwnerResolutionResult(status=OwnerResolutionStatus.FOUND, owner_key=self.owner_key)


@pytest.fixture(autouse=True)
def _clear_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def _override(dep, value):
    app.dependency_overrides[dep] = lambda: value


def _found_owner_override(owner_key=None):
    resolver = FakeOwnerResolver(owner_key=owner_key)
    _override(get_owner_resolver, resolver)
    return resolver


def _not_found_owner_override():
    resolver = FakeOwnerResolver(found=False)
    _override(get_owner_resolver, resolver)
    return resolver


# ---------------------------------------------------------------------------
# OWNER
# ---------------------------------------------------------------------------


class TestOwnerResolution:
    async def test_job_missing_is_unified_404(self) -> None:
        _not_found_owner_override()
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/missing-job/cv-documents")
        assert resp.status_code == 404
        assert resp.json()["code"] == "DOCUMENT_WORKFLOW_NOT_FOUND"

    async def test_identity_binding_missing_is_same_unified_404(self) -> None:
        # From the router's point of view, "job missing" and "binding
        # missing" are indistinguishable -- both surface via
        # OwnerResolutionStatus.NOT_FOUND.
        _not_found_owner_override()
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-with-no-binding/cv-documents")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "DOCUMENT_WORKFLOW_NOT_FOUND"
        assert body["message"] == "Document workflow was not found."

    async def test_caller_cannot_supply_person_entity_id(self) -> None:
        resolver = _found_owner_override()
        async with _client() as client:
            resp = await client.post(
                "/api/v1/jobs/job-1/cv-documents/working-copy",
                json={"person_entity_id": str(uuid4())},
            )
        assert resolver.calls == ["job-1"]
        assert resp.status_code in (200, 201, 404, 409, 423, 500, 503)

    async def test_caller_cannot_supply_master_resume_id(self) -> None:
        resolver = _found_owner_override()
        async with _client() as client:
            await client.post(
                "/api/v1/jobs/job-1/cv-documents/working-copy",
                json={"master_resume_id": "some-resume-id"},
            )
        assert resolver.calls == ["job-1"]

    async def test_caller_cannot_supply_fingerprint(self) -> None:
        resolver = _found_owner_override()
        async with _client() as client:
            await client.post(
                "/api/v1/jobs/job-1/cv-documents/final-snapshot",
                json={"owner_key_fingerprint": "a" * 64},
            )
        assert resolver.calls == ["job-1"]

    async def test_caller_cannot_supply_path(self) -> None:
        resolver = _found_owner_override()
        async with _client() as client:
            await client.post(
                "/api/v1/jobs/job-1/cv-documents/working-copy",
                json={"path": "/etc/passwd"},
            )
        assert resolver.calls == ["job-1"]


# ---------------------------------------------------------------------------
# THREADPOOL
# ---------------------------------------------------------------------------


class TestThreadpoolBoundary:
    async def test_sync_owner_binding_lookup_not_on_event_loop_thread(self) -> None:
        from app.services.cv_document_workflow_owner_resolution import CvDocumentWorkflowOwnerResolver

        class _FakeAsyncDatabase:
            async def get_job(self, job_id):
                return {"job_id": job_id}

            async def get_master_resume(self):
                return {"resume_id": "master-1"}

        class _ThreadRecordingBindingRepo:
            def __init__(self):
                self.recorded_ident: int | None = None

            def get_binding_by_master_resume(self, master_resume_id):
                self.recorded_ident = threading.get_ident()
                return types.SimpleNamespace(person_entity_id=uuid4())

        binding_repo = _ThreadRecordingBindingRepo()
        resolver = CvDocumentWorkflowOwnerResolver(
            database=_FakeAsyncDatabase(), binding_repository=binding_repo
        )
        _override(get_owner_resolver, resolver)
        _override(
            get_workflow_query_service,
            _StubQueryService(WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.FOUND, response=_status_response())),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert resp.status_code == 200
        assert binding_repo.recorded_ident is not None
        assert binding_repo.recorded_ident != threading.main_thread().ident

    async def test_query_service_called_through_threadpool(self) -> None:
        _found_owner_override()
        stub = _StubQueryService(
            WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.FOUND, response=_status_response())
        )
        _override(get_workflow_query_service, stub)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert resp.status_code == 200
        assert stub.recorded_ident is not None
        assert stub.recorded_ident != threading.main_thread().ident
        assert stub.call_count == 1

    async def test_working_copy_service_called_through_threadpool(self) -> None:
        _found_owner_override()
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED, **_publish_kwargs())
        )
        _override(get_working_copy_store, store)
        _override(get_cv_document_artifact_repository, object())
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 201
        assert store.recorded_ident != threading.main_thread().ident
        assert store.create_call_count == 1

    async def test_final_snapshot_composition_root_called_through_threadpool(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_working_copy_store, object())
        _override(get_final_docx_source_reader, object())
        _override(get_final_docx_snapshot_repository, object())
        _override(get_cv_document_artifact_repository, object())

        snapshot = _make_final_docx_snapshot()
        recorder = _CallRecorder(
            FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.FINALIZED, snapshot=snapshot)
        )
        monkeypatch.setattr(cvdocs, "finalize_working_copy_to_final_docx_snapshot", recorder)
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )

        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 201
        assert recorder.call_count == 1
        assert recorder.recorded_ident != threading.main_thread().ident

    async def test_final_pdf_composition_root_called_through_threadpool(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_final_docx_snapshot_pdf_source_reader, object())
        _override(get_docx_to_pdf_converter, object())
        _override(get_final_confirmed_pdf_repository, object())

        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot),
        )
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED,
            **_pdf_success_kwargs(snapshot),
        )
        recorder = _CallRecorder(result)
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", recorder)

        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 201
        assert recorder.call_count == 1
        assert recorder.recorded_ident != threading.main_thread().ident

    async def test_composition_root_called_exactly_once(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_final_docx_snapshot_pdf_source_reader, object())
        _override(get_docx_to_pdf_converter, object())
        _override(get_final_confirmed_pdf_repository, object())
        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot),
        )
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED, **_pdf_success_kwargs(snapshot)
        )
        recorder = _CallRecorder(result)
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", recorder)
        async with _client() as client:
            await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert recorder.call_count == 1

    async def test_no_router_level_retry_on_failure(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_final_docx_snapshot_pdf_source_reader, object())
        _override(get_docx_to_pdf_converter, object())
        _override(get_final_confirmed_pdf_repository, object())
        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot),
        )
        result = FinalConfirmedPdfGenerationResult(status=FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE)
        recorder = _CallRecorder(result)
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", recorder)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 503
        assert recorder.call_count == 1


class _CallRecorder:
    def __init__(self, result):
        self.result = result
        self.call_count = 0
        self.recorded_ident: int | None = None

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        self.recorded_ident = threading.get_ident()
        return self.result


class _StubQueryService:
    def __init__(self, result: WorkflowStatusQueryResult):
        self.result = result
        self.call_count = 0
        self.recorded_ident: int | None = None

    def get_workflow_status(self, owner_key):
        self.call_count += 1
        self.recorded_ident = threading.get_ident()
        return self.result


class _StubWorkingCopyStore:
    def __init__(self, create_result=None, inspect_result=None):
        self.create_result = create_result
        self.inspect_result = inspect_result
        self.create_call_count = 0
        self.recorded_ident: int | None = None

    def create_or_refresh_working_copy(self, *, owner_key, repository):
        self.create_call_count += 1
        self.recorded_ident = threading.get_ident()
        return self.create_result

    def inspect_working_copy(self, *, owner_key):
        self.recorded_ident = threading.get_ident()
        return self.inspect_result


def _status_response():
    from app.schemas.cv_document_workflow_api import (
        CvDocumentFinalPdfResponse,
        CvDocumentFinalSnapshotResponse,
        CvDocumentProposalResponse,
        CvDocumentWorkflowStatusResponse,
        CvDocumentWorkingCopyResponse,
    )

    return CvDocumentWorkflowStatusResponse(
        proposal=CvDocumentProposalResponse(exists=False, download_available=False),
        working_copy=CvDocumentWorkingCopyResponse(
            exists=False, download_available=False, finalization_available=False
        ),
        final_snapshot=CvDocumentFinalSnapshotResponse(exists=False, download_available=False),
        final_pdf=CvDocumentFinalPdfResponse(
            exists=False, is_current=False, state="missing", freshness="missing", download_available=False
        ),
        available_actions=(),
    )


def _publish_kwargs():
    owner_fp = "a" * 64
    binding = WorkingCopyBinding(
        owner_key_fingerprint=owner_fp,
        bound_proposal_artifact_fingerprint="b" * 64,
        bound_proposal_revision=1,
        bound_generated_proposal_sha256="c" * 64,
        last_known_tool_written_hash="c" * 64,
    )
    return {"published_working_copy_sha256": "d" * 64, "published_binding": binding}


def _make_final_docx_snapshot() -> FinalDocxSnapshot:
    owner_key = _owner_key()
    final_docx_sha = _sha256(b"final docx bytes")
    from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint

    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint="1" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha,
        final_docx_sha256=final_docx_sha,
        finalization_policy_version="policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="1" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha,
        final_docx_sha256=final_docx_sha,
        edited_by_user=False,
        finalization_policy_version="policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )


def _pdf_success_kwargs(snapshot: FinalDocxSnapshot):
    from app.schemas.cv_document_final_confirmed_pdf import (
        ConfirmedPdfCurrentAuthorityObservation,
        build_final_confirmed_pdf_artifact,
    )
    from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity

    runtime_identity = build_converter_runtime_identity(
        implementation_id="fake",
        implementation_version="1.0",
        executable_canonical_path="/usr/bin/fake",
        executable_file_identity="dev:1,inode:1",
        executable_sha256="a" * 64,
        platform_identity="test",
        font_environment_id="fonts-v1",
        runtime_identity_schema_version="cv-document-pdf-conversion-runtime-schema-v1",
    )
    pdf_bytes = b"%PDF-1.4 fake"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=snapshot.owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=runtime_identity,
        conversion_policy_version="conversion-policy-v1",
        pdf_sha256=_sha256(pdf_bytes),
    )
    observation = ConfirmedPdfCurrentAuthorityObservation(
        exists=True,
        lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
        current_artifact_fingerprint=artifact.artifact_fingerprint,
        slot_version=1,
    )
    return {
        "artifact": artifact,
        "pdf_bytes": pdf_bytes,
        "source_final_snapshot": snapshot,
        "current_authority_promoted": True,
        "final_authority_observation": observation,
    }


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    async def test_happy_path(self) -> None:
        _found_owner_override()
        _override(
            get_workflow_query_service,
            _StubQueryService(WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.FOUND, response=_status_response())),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available_actions"] == []

    async def test_503_on_hard_storage_failure(self) -> None:
        _found_owner_override()
        _override(
            get_workflow_query_service,
            _StubQueryService(WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.UNAVAILABLE)),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert resp.status_code == 503
        assert resp.json()["code"] == "DOCUMENT_WORKFLOW_UNAVAILABLE"

    async def test_no_internal_identifiers_leaked(self) -> None:
        _found_owner_override()
        _override(
            get_workflow_query_service,
            _StubQueryService(WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.FOUND, response=_status_response())),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        text = resp.text
        for forbidden in ("fingerprint", "sha256", "owner_key", "person_entity_id"):
            assert forbidden not in text

    async def test_available_actions_present(self) -> None:
        _found_owner_override()
        _override(
            get_workflow_query_service,
            _StubQueryService(WorkflowStatusQueryResult(status=WorkflowStatusQueryStatus.FOUND, response=_status_response())),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert "available_actions" in resp.json()


# ---------------------------------------------------------------------------
# PROPOSAL DOWNLOAD
# ---------------------------------------------------------------------------


class _FakeProposalArtifact:
    def __init__(self, revision: int = 1):
        self.proposal_revision = revision


class _FakeArtifactRepository:
    def __init__(self, artifact=None, content: bytes | None = None, raise_error: Exception | None = None):
        self.artifact = artifact
        self.content = content
        self.raise_error = raise_error

    def get_current_proposal(self, owner_key):
        if self.raise_error:
            raise self.raise_error
        return self.artifact

    def read_current_proposal_bytes(self, owner_key):
        if self.raise_error:
            raise self.raise_error
        return self.content


class TestProposalDownload:
    async def test_exact_bytes(self) -> None:
        _found_owner_override()
        content = b"exact proposal bytes"
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=content),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.status_code == 200
        assert resp.content == content

    async def test_docx_media_type(self) -> None:
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=b"x"),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    async def test_fixed_filename(self) -> None:
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=b"x"),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/some-job-id-here/cv-documents/proposal")
        assert 'filename="cv-proposal.docx"' in resp.headers["content-disposition"]
        assert "some-job-id-here" not in resp.headers["content-disposition"]

    async def test_content_length(self) -> None:
        _found_owner_override()
        content = b"twelve bytes"
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=content),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.headers["content-length"] == str(len(content))

    async def test_nosniff(self) -> None:
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=b"x"),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.headers["x-content-type-options"] == "nosniff"

    async def test_no_store(self) -> None:
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=b"x"),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.headers["cache-control"] == "no-store"

    async def test_etag_present(self) -> None:
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=b"x"),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert "etag" in resp.headers

    async def test_if_none_match_returns_304(self) -> None:
        _found_owner_override()
        content = b"same content"
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=content),
        )
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": etag}
            )
        assert second.status_code == 304

    async def test_304_empty_body(self) -> None:
        _found_owner_override()
        content = b"proposal 304 empty body test"
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=content),
        )
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": etag}
            )
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert second.headers["cache-control"] == "no-store"
        assert "content-disposition" not in second.headers
        assert "content-type" not in second.headers
        assert "x-content-type-options" not in second.headers
        assert "content-length" not in second.headers

    async def test_404_when_no_proposal(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, _FakeArtifactRepository(artifact=None))
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.status_code == 404

    async def test_503_on_storage_error(self) -> None:
        from app.services.cv_document_blob_store import CvDocumentStorageError
        from app.schemas.cv_document_storage import CvDocumentStorageErrorCode

        _found_owner_override()
        error = CvDocumentStorageError.of(CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT, "boom")
        _override(get_cv_document_artifact_repository, _FakeArtifactRepository(raise_error=error))
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# IF-NONE-MATCH HANDLING
# ---------------------------------------------------------------------------


class TestIfNoneMatchHandling:
    """Exercises the shared If-None-Match helper through a real HTTP
    request/response roundtrip against the proposal download endpoint --
    every download endpoint shares the same helper."""

    def _override_content(self, content: bytes):
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=content),
        )

    async def test_exact_quoted_validator_matches(self) -> None:
        self._override_content(b"exact quoted validator test")
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": etag}
            )
        assert second.status_code == 304

    async def test_list_of_validators_matches(self) -> None:
        self._override_content(b"list of validators test")
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
            etag = first.headers["etag"]
            header_value = f'"unrelated-etag-value", {etag}, "another-unrelated-one"'
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": header_value}
            )
        assert second.status_code == 304

    async def test_non_matching_list_falls_through_to_200(self) -> None:
        content = b"non matching list test"
        self._override_content(content)
        async with _client() as client:
            resp = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal",
                headers={"if-none-match": '"unrelated-one", "unrelated-two"'},
            )
        assert resp.status_code == 200
        assert resp.content == content

    async def test_wildcard_matches(self) -> None:
        self._override_content(b"wildcard test")
        async with _client() as client:
            resp = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": "*"}
            )
        assert resp.status_code == 304

    async def test_weak_validator_matches_via_weak_comparison(self) -> None:
        self._override_content(b"weak validator test")
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
            etag = first.headers["etag"]
            weak_header = f"W/{etag}"
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": weak_header}
            )
        assert second.status_code == 304

    async def test_whitespace_around_validators_is_tolerated(self) -> None:
        self._override_content(b"whitespace tolerance test")
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/proposal")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal",
                headers={"if-none-match": f"   {etag}   "},
            )
        assert second.status_code == 304

    async def test_malformed_input_never_500_and_falls_back_to_200(self) -> None:
        content = b"malformed if-none-match test"
        self._override_content(content)
        async with _client() as client:
            resp = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal",
                headers={"if-none-match": 'W/*, "unterminated, ,,, W/, """'},
            )
        assert resp.status_code == 200
        assert resp.content == content

    async def test_malformed_wildcard_weak_prefix_does_not_cause_false_304(self) -> None:
        content = b"weak wildcard is not a real wildcard"
        self._override_content(content)
        async with _client() as client:
            resp = await client.get(
                "/api/v1/jobs/job-1/cv-documents/proposal", headers={"if-none-match": "W/*"}
            )
        assert resp.status_code == 200
        assert resp.content == content


# ---------------------------------------------------------------------------
# WORKING COPY POST
# ---------------------------------------------------------------------------


class TestCreateWorkingCopy:
    async def test_happy_path_created(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, object())
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(
                status=WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED, **_publish_kwargs()
            )
        )
        _override(get_working_copy_store, store)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 201
        body = resp.json()
        assert body["exists"] is True

    async def test_409_conflict(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, object())
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(
                status=WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT
            )
        )
        _override(get_working_copy_store, store)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 409

    async def test_423_lock(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, object())
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.OWNER_LOCK_TIMEOUT)
        )
        _override(get_working_copy_store, store)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 423

    async def test_404_missing_proposal(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, object())
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.NO_CURRENT_PROPOSAL)
        )
        _override(get_working_copy_store, store)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 404

    async def test_503_storage(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, object())
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(status=WorkingCopyCreateRefreshStatus.STORAGE_UNAVAILABLE)
        )
        _override(get_working_copy_store, store)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 503

    async def test_response_download_is_authoritative_edit_source_false(self) -> None:
        _found_owner_override()
        _override(get_cv_document_artifact_repository, object())
        store = _StubWorkingCopyStore(
            create_result=WorkingCopyCreateRefreshResult(
                status=WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED, **_publish_kwargs()
            )
        )
        _override(get_working_copy_store, store)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.json()["download_is_authoritative_edit_source"] is False


# ---------------------------------------------------------------------------
# WORKING COPY GET
# ---------------------------------------------------------------------------


def _observed_state(owner_fp: str) -> WorkingCopyObservedState:
    fp = compute_binding_state_fingerprint(
        owner_key_fingerprint=owner_fp,
        working_copy_sha256="a" * 64,
        binding_state=WorkingCopyBindingState.MISSING,
        sidecar_raw_sha256=None,
        binding_schema_version=None,
        bound_proposal_artifact_fingerprint=None,
        bound_proposal_revision=None,
    )
    return WorkingCopyObservedState(
        owner_key_fingerprint=owner_fp,
        working_copy_sha256="a" * 64,
        binding_state=WorkingCopyBindingState.MISSING,
        binding_state_fingerprint=fp,
    )


class TestDownloadWorkingCopy:
    async def test_exact_bytes(self, monkeypatch) -> None:
        owner_key = _owner_key()
        _found_owner_override(owner_key)
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(
                status=WorkingCopyInspectionStatus.OBSERVED,
                observed_state=_observed_state(owner_key.owner_key_fingerprint),
            )
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        content = b"working copy exact bytes"
        monkeypatch.setattr(cvdocs, "read_current_working_copy_bytes", lambda *a, **k: content)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 200
        assert resp.content == content

    async def test_media_type(self, monkeypatch) -> None:
        owner_key = _owner_key()
        _found_owner_override(owner_key)
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(
                status=WorkingCopyInspectionStatus.OBSERVED,
                observed_state=_observed_state(owner_key.owner_key_fingerprint),
            )
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        monkeypatch.setattr(cvdocs, "read_current_working_copy_bytes", lambda *a, **k: b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    async def test_fixed_filename(self, monkeypatch) -> None:
        owner_key = _owner_key()
        _found_owner_override(owner_key)
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(
                status=WorkingCopyInspectionStatus.OBSERVED,
                observed_state=_observed_state(owner_key.owner_key_fingerprint),
            )
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        monkeypatch.setattr(cvdocs, "read_current_working_copy_bytes", lambda *a, **k: b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert 'filename="cv-working-copy.docx"' in resp.headers["content-disposition"]

    async def test_no_etag_required(self, monkeypatch) -> None:
        owner_key = _owner_key()
        _found_owner_override(owner_key)
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(
                status=WorkingCopyInspectionStatus.OBSERVED,
                observed_state=_observed_state(owner_key.owner_key_fingerprint),
            )
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        monkeypatch.setattr(cvdocs, "read_current_working_copy_bytes", lambda *a, **k: b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert "etag" not in resp.headers

    async def test_404_missing(self) -> None:
        _found_owner_override()
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.WORKING_COPY_MISSING)
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 404

    async def test_409_unstable(self) -> None:
        _found_owner_override()
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(
                status=WorkingCopyInspectionStatus.WORKING_COPY_CHANGED_DURING_READ
            )
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 409

    async def test_423_lock(self) -> None:
        _found_owner_override()
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.OWNER_LOCK_TIMEOUT)
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 423

    async def test_503_storage(self) -> None:
        _found_owner_override()
        store = _StubWorkingCopyStore(
            inspect_result=WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.STORAGE_UNAVAILABLE)
        )
        _override(get_working_copy_store, store)
        _override(get_working_copy_paths, object())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/working-copy")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# FINAL SNAPSHOT POST
# ---------------------------------------------------------------------------


class TestFinalizeCvWorkingCopy:
    def _override_common(self):
        _override(get_working_copy_store, object())
        _override(get_final_docx_source_reader, object())
        _override(get_final_docx_snapshot_repository, object())
        _override(get_cv_document_artifact_repository, object())

    async def test_created_returns_201(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.FINALIZED, snapshot=snapshot),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 201

    async def test_identical_replay_returns_200(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(
                status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot
            ),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.FINALIZED, snapshot=snapshot),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 200

    async def test_conflict_returns_409(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 409

    async def test_lock_returns_423(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCKED),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 423

    async def test_missing_returns_404(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 404

    async def test_storage_returns_503(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.STORAGE_UNAVAILABLE),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert resp.status_code == 503

    async def test_no_fingerprints_or_hashes_in_json(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )
        monkeypatch.setattr(
            cvdocs,
            "finalize_working_copy_to_final_docx_snapshot",
            lambda **k: FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.FINALIZED, snapshot=snapshot),
        )
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-snapshot")
        assert set(resp.json().keys()) == {"exists", "edited_by_user", "download_available", "created_at"}


# ---------------------------------------------------------------------------
# FINAL PDF POST
# ---------------------------------------------------------------------------


class TestGenerateCvFinalPdf:
    def _override_common(self):
        _override(get_final_docx_snapshot_pdf_source_reader, object())
        _override(get_docx_to_pdf_converter, object())
        _override(get_final_confirmed_pdf_repository, object())

    def _with_snapshot(self, monkeypatch, snapshot):
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(
                status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot
            ),
        )

    async def test_created_returns_201(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED, **_pdf_success_kwargs(snapshot)
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 201
        assert resp.json()["state"] == "created"

    async def test_replayed_returns_200(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED, **_pdf_success_kwargs(snapshot)
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 200
        assert resp.json()["state"] == "reused"

    async def test_already_current_returns_200(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        kwargs = _pdf_success_kwargs(snapshot)
        kwargs["current_authority_promoted"] = False
        result = FinalConfirmedPdfGenerationResult(status=FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT, **kwargs)
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 200
        assert resp.json()["state"] == "already_current"

    async def test_current_conflict_returns_409(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        kwargs = _pdf_success_kwargs(snapshot)
        kwargs["current_authority_promoted"] = False
        # CURRENT_PDF_SLOT_CONFLICT requires the observation to point at a
        # *different* (already-current, winning) artifact than the one this
        # call produced.
        from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfCurrentAuthorityObservation

        kwargs["final_authority_observation"] = ConfirmedPdfCurrentAuthorityObservation(
            exists=True,
            lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
            current_artifact_fingerprint="f" * 64,
            slot_version=2,
        )
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT, **kwargs
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 409
        body = resp.json()
        assert body["state"] == "current_conflict"
        assert body["is_current"] is False

    async def test_converter_unavailable_returns_503(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(status=FinalConfirmedPdfGenerationStatus.CONVERTER_UNAVAILABLE)
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 503

    async def test_storage_unavailable_returns_503(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(status=FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE)
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 503

    async def test_source_conflict_maps_to_409_or_404(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 409

        result2 = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result2)
        async with _client() as client:
            resp2 = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp2.status_code == 404

    async def test_no_diagnostics_leakage(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONVERSION_FAILED,
            diagnostics=("secret diagnostic detail",),
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert "secret diagnostic detail" not in resp.text

    async def test_no_runtime_identity_leakage(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED, **_pdf_success_kwargs(snapshot)
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert "executable" not in resp.text and "runtime_identity" not in resp.text

    async def test_no_fingerprints_leaked(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED, **_pdf_success_kwargs(snapshot)
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert "fingerprint" not in resp.text

    async def test_latest_snapshot_resolved_server_side(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        called_with = {}

        def _fake_query(session_factory, owner_key_fingerprint):
            called_with["owner_key_fingerprint"] = owner_key_fingerprint
            return LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND)

        monkeypatch.setattr(cvdocs, "query_latest_final_docx_snapshot", _fake_query)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 404
        assert "owner_key_fingerprint" in called_with

    async def test_caller_cannot_supply_conversion_policy(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common()
        snapshot = _make_final_docx_snapshot()
        self._with_snapshot(monkeypatch, snapshot)
        received = {}

        def _fake_generate(**kwargs):
            received.update(kwargs)
            return FinalConfirmedPdfGenerationResult(
                status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED, **_pdf_success_kwargs(snapshot)
            )

        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", _fake_generate)
        async with _client() as client:
            await client.post(
                "/api/v1/jobs/job-1/cv-documents/final-pdf",
                json={"conversion_policy_version": "client-supplied-version"},
            )
        assert received["conversion_policy_version"] == cvdocs.CONVERSION_POLICY_VERSION
        assert received["conversion_policy_version"] != "client-supplied-version"


# ---------------------------------------------------------------------------
# FINAL PDF GET
# ---------------------------------------------------------------------------


class _FakeFinalConfirmedPdfRepository:
    def __init__(self, authority_result, retrieval_result=None):
        self.authority_result = authority_result
        self.retrieval_result = retrieval_result

    def read_current_authority(self, owner_key):
        return self.authority_result

    def retrieve_pdf_bytes_by_artifact(self, artifact_fingerprint):
        return self.retrieval_result


def _authority_result(status, *, lineage=None, artifact_fp=None):
    from app.schemas.cv_document_final_confirmed_pdf import ConfirmedPdfCurrentAuthorityObservation
    from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
        FinalConfirmedPdfAuthorityReadResult,
    )

    if status == FinalConfirmedPdfAuthorityReadStatus.ABSENT:
        return FinalConfirmedPdfAuthorityReadResult(
            status=status, observation=ConfirmedPdfCurrentAuthorityObservation(exists=False)
        )
    if status == FinalConfirmedPdfAuthorityReadStatus.FOUND:
        return FinalConfirmedPdfAuthorityReadResult(
            status=status,
            observation=ConfirmedPdfCurrentAuthorityObservation(
                exists=True, lineage_kind=lineage, current_artifact_fingerprint=artifact_fp, slot_version=1
            ),
        )
    return FinalConfirmedPdfAuthorityReadResult(status=status)


class TestDownloadCvFinalPdf:
    async def test_exact_bytes(self) -> None:
        _found_owner_override()
        pdf_bytes = b"%PDF-1.4 exact"
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
            retrieval_result=types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=pdf_bytes),
        )
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 200
        assert resp.content == pdf_bytes

    async def test_pdf_media_type(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=b"%PDF-x")
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.headers["content-type"] == "application/pdf"

    async def test_fixed_filename(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=b"%PDF-x")
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert 'filename="cv-final.pdf"' in resp.headers["content-disposition"]

    async def test_content_length(self) -> None:
        _found_owner_override()
        pdf_bytes = b"%PDF-1.4 content length test"
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=pdf_bytes)
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.headers["content-length"] == str(len(pdf_bytes))

    async def test_nosniff(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=b"%PDF-x")
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.headers["x-content-type-options"] == "nosniff"

    async def test_no_store(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=b"%PDF-x")
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.headers["cache-control"] == "no-store"

    async def test_etag_and_304(self) -> None:
        _found_owner_override()
        pdf_bytes = b"%PDF-1.4 etag test"
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=pdf_bytes)
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/final-pdf", headers={"if-none-match": etag}
            )
        assert second.status_code == 304

    async def test_304_empty_body(self) -> None:
        _found_owner_override()
        pdf_bytes = b"%PDF-1.4 empty body test"
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=pdf_bytes)
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/final-pdf", headers={"if-none-match": etag}
            )
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert second.headers["cache-control"] == "no-store"
        assert "content-disposition" not in second.headers
        assert "content-type" not in second.headers
        assert "x-content-type-options" not in second.headers
        assert "content-length" not in second.headers

    async def test_current_final_lineage_served(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
                artifact_fp="a" * 64,
            ),
        )
        repo.retrieval_result = types.SimpleNamespace(status=FinalConfirmedPdfRetrievalStatus.OK, pdf_bytes=b"%PDF-x")
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 200

    async def test_legacy_current_returns_404(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(
                FinalConfirmedPdfAuthorityReadStatus.FOUND,
                lineage=ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX,
                artifact_fp="a" * 64,
            ),
        )
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 404

    async def test_404_no_current(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(FinalConfirmedPdfAuthorityReadStatus.ABSENT)
        )
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 404

    async def test_503_storage(self) -> None:
        _found_owner_override()
        repo = _FakeFinalConfirmedPdfRepository(
            authority_result=_authority_result(FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE)
        )
        _override(get_final_confirmed_pdf_repository, repo)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# FINAL DOCX GET
# ---------------------------------------------------------------------------


class _FakeFinalSnapshotRepository:
    def __init__(self, content: bytes | None):
        self.content = content

    def retrieve_final_docx_bytes(self, final_docx_sha256):
        return self.content


class TestDownloadCvFinalDocx:
    def _override_common(self, monkeypatch, snapshot, content):
        _override(get_sync_sessionmaker, object())
        _override(get_final_docx_snapshot_repository, _FakeFinalSnapshotRepository(content))
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(
                status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot
            )
            if snapshot is not None
            else LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.NOT_FOUND),
        )

    async def test_exact_bytes(self, monkeypatch) -> None:
        _found_owner_override()
        content = b"final docx exact bytes"
        snapshot = _make_final_docx_snapshot()
        self._override_common(monkeypatch, snapshot, content)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.status_code == 200
        assert resp.content == content

    async def test_docx_media_type(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        self._override_common(monkeypatch, snapshot, b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    async def test_fixed_filename(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        self._override_common(monkeypatch, snapshot, b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert 'filename="cv-final.docx"' in resp.headers["content-disposition"]

    async def test_content_length(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        content = b"final docx content length test"
        self._override_common(monkeypatch, snapshot, content)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.headers["content-length"] == str(len(content))

    async def test_nosniff(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        self._override_common(monkeypatch, snapshot, b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.headers["x-content-type-options"] == "nosniff"

    async def test_no_store(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        self._override_common(monkeypatch, snapshot, b"x")
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.headers["cache-control"] == "no-store"

    async def test_etag_and_304(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        content = b"same final docx bytes"
        self._override_common(monkeypatch, snapshot, content)
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/final-docx", headers={"if-none-match": etag}
            )
        assert second.status_code == 304

    async def test_304_empty_body(self, monkeypatch) -> None:
        _found_owner_override()
        snapshot = _make_final_docx_snapshot()
        content = b"final docx 304 empty body test"
        self._override_common(monkeypatch, snapshot, content)
        async with _client() as client:
            first = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
            etag = first.headers["etag"]
            second = await client.get(
                "/api/v1/jobs/job-1/cv-documents/final-docx", headers={"if-none-match": etag}
            )
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert second.headers["cache-control"] == "no-store"
        assert "content-disposition" not in second.headers
        assert "content-type" not in second.headers
        assert "x-content-type-options" not in second.headers
        assert "content-length" not in second.headers

    async def test_404_missing(self, monkeypatch) -> None:
        _found_owner_override()
        self._override_common(monkeypatch, None, None)
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.status_code == 404

    async def test_503_storage(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_sync_sessionmaker, object())
        _override(get_final_docx_snapshot_repository, _FakeFinalSnapshotRepository(None))
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(status=LatestFinalSnapshotQueryStatus.STORAGE_UNAVAILABLE),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.status_code == 503

    async def test_503_storage_metadata_conflict(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_sync_sessionmaker, object())
        _override(get_final_docx_snapshot_repository, _FakeFinalSnapshotRepository(None))
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(
                status=LatestFinalSnapshotQueryStatus.STORAGE_METADATA_CONFLICT
            ),
        )
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents/final-docx")
        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == "DOCUMENT_WORKFLOW_UNAVAILABLE"
        assert set(body.keys()) == {"code", "message", "retryable", "request_id", "details"}
        text = resp.text
        for forbidden in ("fingerprint", "sha256", "SELECT", "/Users/", ".resume_matcher"):
            assert forbidden not in text


# ---------------------------------------------------------------------------
# ERROR SAFETY
# ---------------------------------------------------------------------------


class TestErrorSafety:
    async def test_unexpected_exception_yields_static_500(self) -> None:
        class _BoomQueryService:
            def get_workflow_status(self, owner_key):
                raise RuntimeError("kaboom: sensitive internal detail")

        _found_owner_override()
        _override(get_workflow_query_service, _BoomQueryService())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "INTERNAL_ERROR"
        assert body["message"] == "An unexpected error occurred."

    async def test_raw_exception_text_absent(self) -> None:
        class _BoomQueryService:
            def get_workflow_status(self, owner_key):
                raise RuntimeError("kaboom: sensitive internal detail")

        _found_owner_override()
        _override(get_workflow_query_service, _BoomQueryService())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert "kaboom" not in resp.text
        assert "sensitive internal detail" not in resp.text

    async def test_sql_text_absent(self) -> None:
        class _BoomQueryService:
            def get_workflow_status(self, owner_key):
                raise RuntimeError("SELECT * FROM cv_docx_final_snapshots WHERE 1=1")

        _found_owner_override()
        _override(get_workflow_query_service, _BoomQueryService())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert "SELECT" not in resp.text

    async def test_path_absent(self) -> None:
        class _BoomQueryService:
            def get_workflow_status(self, owner_key):
                raise RuntimeError("/Users/someone/.resume_matcher/cv_documents/blobs/x")

        _found_owner_override()
        _override(get_workflow_query_service, _BoomQueryService())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert "/Users/" not in resp.text
        assert ".resume_matcher" not in resp.text

    async def test_fingerprint_absent(self) -> None:
        class _BoomQueryService:
            def get_workflow_status(self, owner_key):
                raise RuntimeError(f"fingerprint mismatch: {'a' * 64}")

        _found_owner_override()
        _override(get_workflow_query_service, _BoomQueryService())
        async with _client() as client:
            resp = await client.get("/api/v1/jobs/job-1/cv-documents")
        assert "a" * 64 not in resp.text

    async def test_diagnostics_absent(self, monkeypatch) -> None:
        _found_owner_override()
        _override(get_final_docx_snapshot_pdf_source_reader, object())
        _override(get_docx_to_pdf_converter, object())
        _override(get_final_confirmed_pdf_repository, object())
        snapshot = _make_final_docx_snapshot()
        monkeypatch.setattr(
            cvdocs,
            "query_latest_final_docx_snapshot",
            lambda *a, **k: LatestFinalSnapshotQueryResult(
                status=LatestFinalSnapshotQueryStatus.FOUND, snapshot=snapshot
            ),
        )
        result = FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONVERSION_FAILED,
            diagnostics=("internal diagnostic that must never leak",),
        )
        monkeypatch.setattr(cvdocs, "generate_final_confirmed_pdf", lambda **k: result)
        async with _client() as client:
            resp = await client.post("/api/v1/jobs/job-1/cv-documents/final-pdf")
        assert "internal diagnostic" not in resp.text

    async def test_crlf_injection_impossible_via_fixed_filenames(self) -> None:
        _found_owner_override()
        _override(
            get_cv_document_artifact_repository,
            _FakeArtifactRepository(artifact=_FakeProposalArtifact(), content=b"x"),
        )
        async with _client() as client:
            # job_id itself can never influence the filename -- it is a
            # fixed server-side constant, never templated with caller input.
            resp = await client.get(
                "/api/v1/jobs/evil%0d%0aSet-Cookie%3A%20a%3Db/cv-documents/proposal"
            )
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert "\r" not in resp.headers["content-disposition"]
            assert "\n" not in resp.headers["content-disposition"]
