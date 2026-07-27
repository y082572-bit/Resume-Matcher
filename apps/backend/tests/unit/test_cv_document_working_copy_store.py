"""Unit tests for the P6-B2b-A Working Copy store
(``cv_document_working_copy_store.py``): every lifecycle-matrix branch of
``create_or_refresh_working_copy`` (edited working copies are never
silently overwritten), the ``reset_working_copy_to_current_proposal`` CAS,
and proposal-freshness/repository-bytes verification.

The final section is the *closed error channel* suite: it proves the three
public operations never let ``WorkingCopyPublishError`` (an internal
post-write verification failure), a ``RuntimeError`` from the owner-lock
internals, or a raw filesystem ``OSError`` escape -- each is mapped to a
closed status instead.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, JobArtifactOwnerKey
from app.schemas.cv_document_working_copy import (
    OwnerLockAcquireStatus,
    WorkingCopyBinding,
    WorkingCopyCreateRefreshStatus,
    WorkingCopyInspectionStatus,
    WorkingCopyResetConfirmation,
    WorkingCopyResetStatus,
    serialize_working_copy_binding,
)
from app.services import cv_document_working_copy_store
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import CasReplaceStatus, InMemoryCvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_store import WorkingCopyPublishError, WorkingCopyStore


def _owner_key() -> JobArtifactOwnerKey:
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-1")


def _artifact(
    owner_key: JobArtifactOwnerKey,
    *,
    docx_hash: str,
    revision: int,
    artifact_fingerprint: str,
) -> CvDocxProposalArtifact:
    return CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        document_schema_version="v1",
        rendering_policy_version="v1",
        renderer_adapter_id="adapter",
        template_fingerprint="c" * 64,
        generation_input_fingerprint="d" * 64,
        generated_docx_content_hash=docx_hash,
        current_file_hash=docx_hash,
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=artifact_fingerprint,
    )


def _seed_proposal(
    repo: InMemoryCvDocumentArtifactRepository,
    owner_key: JobArtifactOwnerKey,
    docx_bytes: bytes,
    *,
    revision: int = 1,
    artifact_fingerprint: str = "e" * 64,
    expected_previous_revision: int | None = None,
) -> CvDocxProposalArtifact:
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    artifact = _artifact(owner_key, docx_hash=docx_hash, revision=revision, artifact_fingerprint=artifact_fingerprint)
    result = repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision)
    assert result.status == CasReplaceStatus.REPLACED
    return artifact


@pytest.fixture
def repo() -> InMemoryCvDocumentArtifactRepository:
    return InMemoryCvDocumentArtifactRepository()


@pytest.fixture
def store(tmp_path: Path) -> WorkingCopyStore:
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    return WorkingCopyStore(paths, max_docx_size_bytes=10_000_000)


def _paths_of(store: WorkingCopyStore) -> WorkingCopyPaths:
    return store._paths  # test-only introspection of the fixture's own paths


# -- branch A: nothing exists -------------------------------------------------------


def test_branch_a_creates_pair_when_nothing_exists(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    assert result.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED
    assert result.published_working_copy_sha256 == artifact.generated_docx_content_hash
    assert result.published_binding is not None
    assert result.published_binding.bound_proposal_revision == 1

    docx_path = _paths_of(store).current_docx_path(owner_key.owner_key_fingerprint)
    assert docx_path.read_bytes() == docx_bytes


def test_no_current_proposal_is_reported(store: WorkingCopyStore, repo) -> None:
    owner_key = _owner_key()
    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.NO_CURRENT_PROPOSAL
    assert result.published_working_copy_sha256 is None


# -- branch B: docx exists, sidecar missing ------------------------------------------


def test_branch_b_recovers_sidecar_when_docx_matches_current_proposal(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    paths = _paths_of(store)
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(docx_bytes)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.RECOVERED_UNEDITED_HALF_STATE
    assert paths.binding_path(owner_key.owner_key_fingerprint).exists()
    # The docx bytes are untouched -- only the sidecar was (re)written.
    assert paths.current_docx_path(owner_key.owner_key_fingerprint).read_bytes() == docx_bytes


def test_branch_b_preserves_edited_docx_when_no_sidecar(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    paths = _paths_of(store)
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    edited_bytes = b"a user has manually edited this docx"
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(edited_bytes)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT
    assert result.published_working_copy_sha256 is None
    assert not paths.binding_path(owner_key.owner_key_fingerprint).exists()
    assert paths.current_docx_path(owner_key.owner_key_fingerprint).read_bytes() == edited_bytes


# -- branch C: docx missing, sidecar exists ------------------------------------------


def test_branch_c_recreates_pair_when_docx_missing_but_sidecar_exists(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)

    paths = _paths_of(store)
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    binding = WorkingCopyBinding(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        bound_proposal_artifact_fingerprint=artifact.artifact_fingerprint,
        bound_proposal_revision=1,
        bound_generated_proposal_sha256=artifact.generated_docx_content_hash,
        last_known_tool_written_hash=artifact.generated_docx_content_hash,
    )
    paths.binding_path(owner_key.owner_key_fingerprint).write_bytes(serialize_working_copy_binding(binding))

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_RECREATED_FROM_CURRENT_PROPOSAL
    assert paths.current_docx_path(owner_key.owner_key_fingerprint).read_bytes() == docx_bytes


# -- branch D: docx exists, sidecar valid --------------------------------------------


def test_branch_d_unchanged_when_binding_is_current_and_docx_unedited(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    first = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert first.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED

    second = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert second.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED
    assert second.published_working_copy_sha256 is None


def test_branch_d_refreshes_when_binding_is_stale_and_docx_unedited(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes_v1 = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)

    first = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert first.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED

    docx_bytes_v2 = b"proposal v2 bytes, superseding v1"
    artifact_v2 = _seed_proposal(
        repo, owner_key, docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64, expected_previous_revision=1
    )

    second = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert second.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_REFRESHED
    assert second.published_working_copy_sha256 == artifact_v2.generated_docx_content_hash
    assert second.published_binding.bound_proposal_revision == 2

    docx_path = _paths_of(store).current_docx_path(owner_key.owner_key_fingerprint)
    assert docx_path.read_bytes() == docx_bytes_v2


def test_branch_d_preserves_edited_docx_even_when_binding_is_stale(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes_v1 = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)

    first = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert first.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED

    paths = _paths_of(store)
    edited_bytes = b"the user edited this after it was created"
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(edited_bytes)

    docx_bytes_v2 = b"proposal v2 bytes, superseding v1"
    _seed_proposal(
        repo, owner_key, docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64, expected_previous_revision=1
    )

    second = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert second.status == WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT
    assert paths.current_docx_path(owner_key.owner_key_fingerprint).read_bytes() == edited_bytes


# -- branch E: docx exists, sidecar invalid ------------------------------------------


def test_branch_e_recovers_sidecar_when_invalid_but_docx_matches_current_proposal(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    paths = _paths_of(store)
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(docx_bytes)
    paths.binding_path(owner_key.owner_key_fingerprint).write_bytes(b"{not valid json at all")

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.RECOVERED_UNEDITED_HALF_STATE
    parsed = paths.binding_path(owner_key.owner_key_fingerprint).read_bytes()
    assert b"not valid json" not in parsed


def test_branch_e_preserves_edited_docx_with_invalid_sidecar(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    paths = _paths_of(store)
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    edited_bytes = b"edited docx bytes, does not match proposal"
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(edited_bytes)
    paths.binding_path(owner_key.owner_key_fingerprint).write_bytes(b"{not valid json at all")

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT
    assert paths.current_docx_path(owner_key.owner_key_fingerprint).read_bytes() == edited_bytes


# -- repository bytes hash mismatch ---------------------------------------------------


def test_repository_bytes_hash_mismatch_is_reported(store: WorkingCopyStore, tmp_path: Path) -> None:
    owner_key = _owner_key()

    class _TamperedRepository:
        def get_current_proposal(self, owner_key: JobArtifactOwnerKey) -> CvDocxProposalArtifact | None:
            return _artifact(owner_key, docx_hash="0" * 64, revision=1, artifact_fingerprint="e" * 64)

        def read_current_proposal_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None:
            return b"these bytes do not hash to the declared generated_docx_content_hash"

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=_TamperedRepository())
    assert result.status == WorkingCopyCreateRefreshStatus.REPOSITORY_BYTES_HASH_MISMATCH


# -- reset CAS --------------------------------------------------------------------------


def test_reset_to_current_proposal_publishes_exact_repository_bytes(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes_v1 = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    paths = _paths_of(store)
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(b"the user edited this docx")

    docx_bytes_v2 = b"proposal v2 bytes"
    artifact_v2 = _seed_proposal(
        repo, owner_key, docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64, expected_previous_revision=1
    )

    inspection = store.inspect_working_copy(owner_key=owner_key)
    assert inspection.observed_state is not None

    confirmation = WorkingCopyResetConfirmation(
        expected_working_copy_sha256=inspection.observed_state.working_copy_sha256,
        expected_binding_state_fingerprint=inspection.observed_state.binding_state_fingerprint,
        expected_target_proposal_artifact_fingerprint=artifact_v2.artifact_fingerprint,
        expected_target_proposal_revision=2,
    )
    result = store.reset_working_copy_to_current_proposal(owner_key=owner_key, repository=repo, confirmation=confirmation)

    assert result.status == WorkingCopyResetStatus.RESET_TO_CURRENT_PROPOSAL
    assert result.published_working_copy_sha256 == artifact_v2.generated_docx_content_hash
    assert paths.current_docx_path(owner_key.owner_key_fingerprint).read_bytes() == docx_bytes_v2


def test_reset_has_no_docx_bytes_argument() -> None:
    import inspect

    params = inspect.signature(WorkingCopyStore.reset_working_copy_to_current_proposal).parameters
    for name in params:
        assert "docx" not in name.lower() and "bytes" not in name.lower()


def test_reset_rejects_stale_working_copy_hash(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    inspection = store.inspect_working_copy(owner_key=owner_key)
    stale_confirmation = WorkingCopyResetConfirmation(
        expected_working_copy_sha256="0" * 64,  # deliberately wrong
        expected_binding_state_fingerprint=inspection.observed_state.binding_state_fingerprint,
        expected_target_proposal_artifact_fingerprint=artifact.artifact_fingerprint,
        expected_target_proposal_revision=1,
    )
    result = store.reset_working_copy_to_current_proposal(
        owner_key=owner_key, repository=repo, confirmation=stale_confirmation
    )
    assert result.status == WorkingCopyResetStatus.WORKING_COPY_CHANGED_SINCE_CONFIRMATION
    assert result.published_working_copy_sha256 is None


def test_reset_rejects_stale_binding_state_fingerprint(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    inspection = store.inspect_working_copy(owner_key=owner_key)
    stale_confirmation = WorkingCopyResetConfirmation(
        expected_working_copy_sha256=inspection.observed_state.working_copy_sha256,
        expected_binding_state_fingerprint="0" * 64,  # deliberately wrong
        expected_target_proposal_artifact_fingerprint=artifact.artifact_fingerprint,
        expected_target_proposal_revision=1,
    )
    result = store.reset_working_copy_to_current_proposal(
        owner_key=owner_key, repository=repo, confirmation=stale_confirmation
    )
    assert result.status == WorkingCopyResetStatus.WORKING_COPY_BINDING_CHANGED_SINCE_CONFIRMATION


def test_reset_rejects_when_current_proposal_moved_since_confirmation(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    owner_key = _owner_key()
    docx_bytes_v1 = b"proposal v1 bytes"
    artifact_v1 = _seed_proposal(repo, owner_key, docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    inspection = store.inspect_working_copy(owner_key=owner_key)

    # The proposal moves forward after the confirmation snapshot was taken.
    _seed_proposal(
        repo, owner_key, b"proposal v2 bytes", revision=2, artifact_fingerprint="f" * 64, expected_previous_revision=1
    )

    confirmation = WorkingCopyResetConfirmation(
        expected_working_copy_sha256=inspection.observed_state.working_copy_sha256,
        expected_binding_state_fingerprint=inspection.observed_state.binding_state_fingerprint,
        expected_target_proposal_artifact_fingerprint=artifact_v1.artifact_fingerprint,
        expected_target_proposal_revision=1,
    )
    result = store.reset_working_copy_to_current_proposal(owner_key=owner_key, repository=repo, confirmation=confirmation)
    assert result.status == WorkingCopyResetStatus.CURRENT_PROPOSAL_CHANGED_SINCE_CONFIRMATION


def test_reset_with_no_current_proposal_is_reported(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    inspection = store.inspect_working_copy(owner_key=owner_key)

    empty_repo = InMemoryCvDocumentArtifactRepository()
    confirmation = WorkingCopyResetConfirmation(
        expected_working_copy_sha256=inspection.observed_state.working_copy_sha256,
        expected_binding_state_fingerprint=inspection.observed_state.binding_state_fingerprint,
        expected_target_proposal_artifact_fingerprint=artifact.artifact_fingerprint,
        expected_target_proposal_revision=1,
    )
    result = store.reset_working_copy_to_current_proposal(owner_key=owner_key, repository=empty_repo, confirmation=confirmation)
    assert result.status == WorkingCopyResetStatus.NO_CURRENT_PROPOSAL


# -- inspect_working_copy -------------------------------------------------------------


def test_inspect_reports_missing_when_no_working_copy_exists(store: WorkingCopyStore) -> None:
    owner_key = _owner_key()
    from app.schemas.cv_document_working_copy import WorkingCopyInspectionStatus

    result = store.inspect_working_copy(owner_key=owner_key)
    assert result.status == WorkingCopyInspectionStatus.WORKING_COPY_MISSING
    assert result.observed_state is None


def test_inspect_is_read_only(store: WorkingCopyStore, repo, tmp_path: Path) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    paths = _paths_of(store)
    docx_path = paths.current_docx_path(owner_key.owner_key_fingerprint)
    binding_path = paths.binding_path(owner_key.owner_key_fingerprint)
    before_docx = docx_path.read_bytes()
    before_binding = binding_path.read_bytes()

    store.inspect_working_copy(owner_key=owner_key)
    store.inspect_working_copy(owner_key=owner_key)

    assert docx_path.read_bytes() == before_docx
    assert binding_path.read_bytes() == before_binding


# -- two writers never produce a mixed docx/sidecar pair -------------------------------


def test_current_proposal_changing_during_operation_leaves_no_mixed_pair(
    store: WorkingCopyStore, repo, tmp_path: Path
) -> None:
    """``get_current_proposal`` is called twice by the store around the
    publish step; a repository whose second call returns a different
    proposal than its first must cause the store to report
    ``CURRENT_PROPOSAL_CHANGED_DURING_OPERATION`` and publish nothing."""

    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact_v1 = _artifact(owner_key, docx_hash=hashlib.sha256(docx_bytes).hexdigest(), revision=1, artifact_fingerprint="e" * 64)
    docx_bytes_v2 = b"proposal v2 bytes"
    artifact_v2 = _artifact(
        owner_key, docx_hash=hashlib.sha256(docx_bytes_v2).hexdigest(), revision=2, artifact_fingerprint="f" * 64
    )

    class _FlippingRepository:
        def __init__(self) -> None:
            self.calls = 0

        def get_current_proposal(self, owner_key: JobArtifactOwnerKey) -> CvDocxProposalArtifact | None:
            self.calls += 1
            return artifact_v1 if self.calls == 1 else artifact_v2

        def read_current_proposal_bytes(self, owner_key: JobArtifactOwnerKey) -> bytes | None:
            return docx_bytes

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=_FlippingRepository())
    assert result.status == WorkingCopyCreateRefreshStatus.CURRENT_PROPOSAL_CHANGED_DURING_OPERATION
    assert result.published_working_copy_sha256 is None

    paths = _paths_of(store)
    assert not paths.current_docx_path(owner_key.owner_key_fingerprint).exists()
    assert not paths.binding_path(owner_key.owner_key_fingerprint).exists()


# ==================================================================================
# Closed error channel: no raw exception ever escapes a public operation
# ==================================================================================


def test_post_write_verification_failure_in_create_refresh_is_closed(
    store: WorkingCopyStore, repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    def _raise(*args, **kwargs):
        raise WorkingCopyPublishError("post-write current.docx verification failed")

    monkeypatch.setattr(store, "_atomic_publish", _raise)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.POST_WRITE_VERIFICATION_FAILED
    assert result.published_working_copy_sha256 is None
    assert result.published_binding is None


def test_post_write_verification_failure_in_reset_is_closed(
    store: WorkingCopyStore, repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    inspection = store.inspect_working_copy(owner_key=owner_key)

    confirmation = WorkingCopyResetConfirmation(
        expected_working_copy_sha256=inspection.observed_state.working_copy_sha256,
        expected_binding_state_fingerprint=inspection.observed_state.binding_state_fingerprint,
        expected_target_proposal_artifact_fingerprint=artifact.artifact_fingerprint,
        expected_target_proposal_revision=1,
    )

    def _raise(*args, **kwargs):
        raise WorkingCopyPublishError("post-write binding.json verification failed")

    monkeypatch.setattr(store, "_atomic_publish", _raise)

    result = store.reset_working_copy_to_current_proposal(owner_key=owner_key, repository=repo, confirmation=confirmation)
    assert result.status == WorkingCopyResetStatus.POST_WRITE_VERIFICATION_FAILED
    assert result.published_working_copy_sha256 is None
    assert result.published_binding is None


def test_invalid_owner_lock_capability_does_not_raise_runtime_error(
    store: WorkingCopyStore, repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates an internal owner-lock contract violation (e.g. the
    nested-acquisition guard) by making ``PlatformOwnerLock.acquire`` raise
    ``RuntimeError`` -- the public operation must map this to
    ``LOCK_CAPABILITY_INVALID``, never let the ``RuntimeError`` escape."""

    owner_key = _owner_key()
    _seed_proposal(repo, owner_key, b"proposal v1 bytes")

    class _RaisingLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def acquire(self, *, timeout_seconds: float):
            raise RuntimeError("PlatformOwnerLock does not support nested acquisition")

        def validate_active_token(self, *args, **kwargs) -> bool:
            return False

        def release(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(cv_document_working_copy_store, "PlatformOwnerLock", _RaisingLock)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.LOCK_CAPABILITY_INVALID
    assert result.published_working_copy_sha256 is None

    inspect_result = store.inspect_working_copy(owner_key=owner_key)
    assert inspect_result.status == WorkingCopyInspectionStatus.LOCK_CAPABILITY_INVALID


def test_lock_capability_invalid_status_still_reported_without_raising(
    store: WorkingCopyStore, repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary (non-exception) path to ``LOCK_CAPABILITY_INVALID`` --
    ``validate_active_token`` returning ``False`` -- must keep working
    exactly as before the closed-error-channel wrapping was added."""

    owner_key = _owner_key()
    _seed_proposal(repo, owner_key, b"proposal v1 bytes")

    class _NeverValidatesLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def acquire(self, *, timeout_seconds: float):
            from app.services.cv_document_owner_lock import OwnerLockAcquireResult, OwnerLockToken

            token = OwnerLockToken(lock_instance_id=1, acquisition_id=1, owner_key_fingerprint="x")
            return OwnerLockAcquireResult(status=OwnerLockAcquireStatus.ACQUIRED, token=token)

        def validate_active_token(self, *args, **kwargs) -> bool:
            return False

        def release(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(cv_document_working_copy_store, "PlatformOwnerLock", _NeverValidatesLock)
    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.LOCK_CAPABILITY_INVALID


def test_filesystem_oserror_is_mapped_to_storage_unavailable(
    store: WorkingCopyStore, repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_key = _owner_key()
    _seed_proposal(repo, owner_key, b"proposal v1 bytes")

    paths = _paths_of(store)

    def _raise(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(paths, "ensure_owner_dirs", _raise)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.STORAGE_UNAVAILABLE
    assert result.published_working_copy_sha256 is None


def test_filesystem_oserror_in_inspect_is_mapped_to_storage_unavailable(
    store: WorkingCopyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_key = _owner_key()
    paths = _paths_of(store)

    def _raise(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(paths, "ensure_owner_dirs", _raise)

    result = store.inspect_working_copy(owner_key=owner_key)
    assert result.status == WorkingCopyInspectionStatus.STORAGE_UNAVAILABLE
    assert result.observed_state is None


def test_preserve_branch_status_is_not_masked_by_closed_error_channel(store: WorkingCopyStore, repo) -> None:
    """A legitimate, exception-free preserve-branch outcome
    (``HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT``) must still be reported
    verbatim -- the new exception handling must never convert a status
    that was never raised as an exception in the first place."""

    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    _seed_proposal(repo, owner_key, docx_bytes)

    paths = _paths_of(store)
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    edited_bytes = b"a user has manually edited this docx"
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(edited_bytes)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.HALF_STATE_PRESERVED_POSSIBLE_USER_EDIT


def test_success_status_is_still_reported_after_closed_error_channel_wrapping(
    store: WorkingCopyStore, repo
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 bytes"
    artifact = _seed_proposal(repo, owner_key, docx_bytes)

    result = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
    assert result.status == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED
    assert result.published_working_copy_sha256 == artifact.generated_docx_content_hash
    assert result.published_binding is not None
