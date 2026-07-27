"""Integration tests for P6-B2b-A Working Copy concurrency: many
concurrent ``create_or_refresh_working_copy``/``inspect_working_copy``
callers on the same owner, serialized by the real OS-level
``PlatformOwnerLock``, always converge on exactly one coherent
``current.docx``/``binding.json`` pair -- never a mixed/torn pair, and
never more than one thread's publish actually landing per state
transition.
"""

from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, JobArtifactOwnerKey
from app.schemas.cv_document_working_copy import (
    WorkingCopyCreateRefreshStatus,
    parse_working_copy_binding,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import CasReplaceStatus, InMemoryCvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_store import WorkingCopyStore


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX owner-lock backend")


def _owner_key() -> JobArtifactOwnerKey:
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-1")


def _artifact(owner_key: JobArtifactOwnerKey, *, docx_bytes: bytes, revision: int, artifact_fingerprint: str) -> CvDocxProposalArtifact:
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
        generated_docx_content_hash=hashlib.sha256(docx_bytes).hexdigest(),
        current_file_hash=hashlib.sha256(docx_bytes).hexdigest(),
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=artifact_fingerprint,
    )


@pytest.fixture
def env(tmp_path: Path):
    repo = InMemoryCvDocumentArtifactRepository()
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    store = WorkingCopyStore(paths, max_docx_size_bytes=10_000_000, owner_lock_timeout_seconds=10.0)
    return repo, paths, store


def test_many_concurrent_create_or_refresh_calls_converge_on_one_pair(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key()
    docx_bytes = b"the one and only proposal v1 payload"
    artifact = _artifact(owner_key, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    result = repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision=None)
    assert result.status == CasReplaceStatus.REPLACED

    statuses: list[WorkingCopyCreateRefreshStatus] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait(timeout=5)
        outcome = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
        with lock:
            statuses.append(outcome.status)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(statuses) == 16
    creators = [s for s in statuses if s == WorkingCopyCreateRefreshStatus.WORKING_COPY_CREATED]
    no_ops = [s for s in statuses if s == WorkingCopyCreateRefreshStatus.WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED]
    # Exactly one caller ever performs the actual creation -- every other
    # concurrent caller observes the already-current pair and no-ops.
    assert len(creators) == 1
    assert len(no_ops) == 15

    docx_path = paths.current_docx_path(owner_key.owner_key_fingerprint)
    binding_path = paths.binding_path(owner_key.owner_key_fingerprint)
    final_docx_bytes = docx_path.read_bytes()
    final_binding = parse_working_copy_binding(binding_path.read_bytes())

    assert final_docx_bytes == docx_bytes
    assert final_binding is not None
    assert final_binding.last_known_tool_written_hash == hashlib.sha256(final_docx_bytes).hexdigest()
    assert final_binding.bound_proposal_artifact_fingerprint == artifact.artifact_fingerprint
    assert final_binding.bound_proposal_revision == 1


def test_concurrent_refreshers_across_a_proposal_revision_bump_never_mix_a_pair(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key()
    docx_bytes_v1 = b"proposal v1 payload"
    artifact_v1 = _artifact(owner_key, docx_bytes=docx_bytes_v1, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact_v1, docx_bytes_v1, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    docx_bytes_v2 = b"proposal v2 payload, superseding v1"
    artifact_v2 = _artifact(owner_key, docx_bytes=docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64)
    repo.replace_current_proposal(owner_key, artifact_v2, docx_bytes_v2, expected_previous_revision=1)

    statuses: list[WorkingCopyCreateRefreshStatus] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=5)
        outcome = store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)
        with lock:
            statuses.append(outcome.status)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    refreshed = [s for s in statuses if s == WorkingCopyCreateRefreshStatus.WORKING_COPY_REFRESHED]
    no_ops = [s for s in statuses if s == WorkingCopyCreateRefreshStatus.WORKING_COPY_UNCHANGED_NO_REFRESH_NEEDED]
    assert len(refreshed) == 1
    assert len(no_ops) == 7

    docx_path = paths.current_docx_path(owner_key.owner_key_fingerprint)
    binding_path = paths.binding_path(owner_key.owner_key_fingerprint)
    final_docx_bytes = docx_path.read_bytes()
    final_binding = parse_working_copy_binding(binding_path.read_bytes())

    # Never a torn/mixed pair: the docx bytes and the sidecar's declared
    # revision always agree on the exact same proposal version.
    assert final_docx_bytes == docx_bytes_v2
    assert final_binding.bound_proposal_revision == 2
    assert final_binding.last_known_tool_written_hash == hashlib.sha256(docx_bytes_v2).hexdigest()


def test_concurrent_inspect_calls_never_observe_a_torn_pair(env) -> None:
    repo, paths, store = env
    owner_key = _owner_key()
    docx_bytes = b"proposal v1 payload for inspect race"
    artifact = _artifact(owner_key, docx_bytes=docx_bytes, revision=1, artifact_fingerprint="e" * 64)
    repo.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous_revision=None)
    store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    docx_bytes_v2 = b"proposal v2 payload for inspect race"
    artifact_v2 = _artifact(owner_key, docx_bytes=docx_bytes_v2, revision=2, artifact_fingerprint="f" * 64)
    repo.replace_current_proposal(owner_key, artifact_v2, docx_bytes_v2, expected_previous_revision=1)

    observed_hashes: list[str] = []
    lock = threading.Lock()

    def refresher() -> None:
        store.create_or_refresh_working_copy(owner_key=owner_key, repository=repo)

    def inspector() -> None:
        result = store.inspect_working_copy(owner_key=owner_key)
        if result.observed_state is not None:
            with lock:
                observed_hashes.append(result.observed_state.working_copy_sha256)

    threads = [threading.Thread(target=refresher)]
    threads += [threading.Thread(target=inspector) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    valid_hashes = {hashlib.sha256(docx_bytes).hexdigest(), hashlib.sha256(docx_bytes_v2).hexdigest()}
    # Every concurrent inspector, whenever it does observe a working copy,
    # must see one of the two well-formed, self-consistent revisions --
    # never a hash that belongs to neither (i.e. never a torn read).
    assert set(observed_hashes).issubset(valid_hashes)
