"""Unit tests for the P6-B2b-B composition root
``finalize_working_copy_to_final_docx_snapshot``: the complete
inspection-status mapping, the ``OBSERVED``+binding-state branch, a
successful flow, build-failure passthrough, the three
``source_proposal_is_current`` outcomes, the ``FinalizeWorkingCopyResult``
validator, and the caller-facing signature contract (no bytes/hash/
revision/fingerprint/edited_by_user/path parameter).
"""

from __future__ import annotations

import hashlib
import inspect
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshotBuildStatus
from app.schemas.cv_document_finalize_working_copy import FinalizeWorkingCopyResult
from app.schemas.cv_document_working_copy import WorkingCopyInspectionStatus
from app.services.cv_document_final_source_reader import (
    FinalDocxSourceCapture,
    WorkingCopyReadResult,
    WorkingCopyReadStatus,
)
from app.services.cv_document_finalize_working_copy import (
    _INSPECTION_FAILURE_TO_BUILD_STATUS,
    finalize_working_copy_to_final_docx_snapshot,
)
from app.services.cv_document_final_snapshot_repository import InMemoryFinalDocxSnapshotRepository
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import InMemoryCvDocumentArtifactRepository
from app.services.cv_document_working_copy_paths import WorkingCopyPaths
from app.services.cv_document_working_copy_store import WorkingCopyStore


FINALIZATION_POLICY_VERSION = "cv-document-finalize-working-copy-test-policy-v1"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_key(reference_id: str = "job-finalize-working-copy-test"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _proposal_artifact(owner_key, *, revision: int, artifact_fingerprint: str, docx_hash: str) -> CvDocxProposalArtifact:
    return CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint=_sha("approved-content"),
        content_plan_fingerprint=_sha("content-plan"),
        document_schema_version="v1",
        rendering_policy_version="v1",
        renderer_adapter_id="adapter",
        template_fingerprint=_sha("template"),
        generation_input_fingerprint=_sha("generation-input"),
        generated_docx_content_hash=docx_hash,
        current_file_hash=docx_hash,
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=artifact_fingerprint,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )


@pytest.fixture
def working_copy_store(tmp_path) -> WorkingCopyStore:
    paths = WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")
    return WorkingCopyStore(paths, max_docx_size_bytes=10_000_000)


@pytest.fixture
def artifact_repository() -> InMemoryCvDocumentArtifactRepository:
    return InMemoryCvDocumentArtifactRepository()


@pytest.fixture
def snapshot_repository() -> InMemoryFinalDocxSnapshotRepository:
    return InMemoryFinalDocxSnapshotRepository()


class _FakeInspectingStore:
    """Stands in for a real ``WorkingCopyStore`` to drive
    ``inspect_working_copy``'s return value directly, without needing real
    filesystem state for every inspection-status branch."""

    def __init__(self, result) -> None:
        self._result = result

    def inspect_working_copy(self, *, owner_key):
        return self._result


class _FakeSourceReader:
    def __init__(self, result: WorkingCopyReadResult) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def read_finalization_source(self, owner_key, expected_proposal_artifact_fingerprint, expected_proposal_revision):
        self.calls.append((owner_key, expected_proposal_artifact_fingerprint, expected_proposal_revision))
        return self._result


def _capture(owner_key, *, proposal_fingerprint: str, proposal_revision: int, docx_bytes: bytes) -> FinalDocxSourceCapture:
    return FinalDocxSourceCapture(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_fingerprint,
        source_proposal_revision=proposal_revision,
        generated_proposal_sha256=hashlib.sha256(docx_bytes).hexdigest(),
        docx_bytes=docx_bytes,
    )


def _success_read(capture: FinalDocxSourceCapture) -> WorkingCopyReadResult:
    return WorkingCopyReadResult(status=WorkingCopyReadStatus.SUCCESS, capture=capture)


# -- complete inspection-status mapping --------------------------------------------------


def test_inspection_status_mapping_is_complete() -> None:
    non_observed = {s for s in WorkingCopyInspectionStatus if s != WorkingCopyInspectionStatus.OBSERVED}
    assert non_observed == set(_INSPECTION_FAILURE_TO_BUILD_STATUS)


@pytest.mark.parametrize(
    "inspection_status,expected_build_status",
    list(_INSPECTION_FAILURE_TO_BUILD_STATUS.items()),
)
def test_each_inspection_failure_maps_to_its_build_status(
    inspection_status, expected_build_status, artifact_repository, snapshot_repository
) -> None:
    from app.schemas.cv_document_working_copy import WorkingCopyInspectionResult

    owner_key = _owner_key()
    fake_store = _FakeInspectingStore(WorkingCopyInspectionResult(status=inspection_status))
    source_reader = _FakeSourceReader(_success_read(_capture(owner_key, proposal_fingerprint=_sha("p"), proposal_revision=1, docx_bytes=b"x")))

    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=fake_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == expected_build_status
    assert result.snapshot is None
    assert source_reader.calls == []  # inspection failure never reaches the builder


# -- OBSERVED + binding MISSING / INVALID -------------------------------------------------


def test_observed_with_binding_missing(working_copy_store, artifact_repository, snapshot_repository) -> None:
    owner_key = _owner_key()
    docx_bytes = b"current.docx exists but binding.json never was written"
    paths = working_copy_store._paths
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    paths.current_docx_path(owner_key.owner_key_fingerprint).write_bytes(docx_bytes)

    source_reader = _FakeSourceReader(_success_read(_capture(owner_key, proposal_fingerprint=_sha("p"), proposal_revision=1, docx_bytes=docx_bytes)))
    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISSING
    assert result.snapshot is None
    assert source_reader.calls == []


def test_observed_with_binding_invalid(working_copy_store, artifact_repository, snapshot_repository) -> None:
    owner_key = _owner_key()
    owner_fp = owner_key.owner_key_fingerprint
    paths = working_copy_store._paths
    paths.ensure_owner_dirs(owner_fp)
    paths.current_docx_path(owner_fp).write_bytes(b"current.docx exists with a corrupt sidecar")
    paths.binding_path(owner_fp).write_bytes(b"{ not valid json")

    source_reader = _FakeSourceReader(_success_read(_capture(owner_key, proposal_fingerprint=_sha("p"), proposal_revision=1, docx_bytes=b"x")))
    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_INVALID
    assert result.snapshot is None
    assert source_reader.calls == []


# -- owner-key fingerprint mismatch --------------------------------------------------------


def test_owner_key_fingerprint_mismatch_is_rejected_before_inspection(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    tampered = owner_key.model_copy(update={"owner_key_fingerprint": _sha("tampered")})
    source_reader = _FakeSourceReader(_success_read(_capture(owner_key, proposal_fingerprint=_sha("p"), proposal_revision=1, docx_bytes=b"x")))

    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=tampered,
        working_copy_store=working_copy_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.OWNER_KEY_FINGERPRINT_MISMATCH
    assert source_reader.calls == []


# -- successful end-to-end flow (real WorkingCopyStore + fake source reader) -----------


def _seed_current_proposal(artifact_repository, owner_key, *, revision: int, docx_bytes: bytes, artifact_fingerprint: str):
    docx_hash = hashlib.sha256(docx_bytes).hexdigest()
    artifact = _proposal_artifact(owner_key, revision=revision, artifact_fingerprint=artifact_fingerprint, docx_hash=docx_hash)
    expected_previous = revision - 1 or None
    result = artifact_repository.replace_current_proposal(owner_key, artifact, docx_bytes, expected_previous)
    assert result.status.value == "REPLACED"
    return artifact


def test_success_finalizes_and_returns_snapshot(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"the proposal's original bytes"
    proposal = _seed_current_proposal(
        artifact_repository, owner_key, revision=1, docx_bytes=docx_bytes, artifact_fingerprint=_sha("proposal-1")
    )
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=artifact_repository)

    capture = _capture(
        owner_key, proposal_fingerprint=proposal.artifact_fingerprint, proposal_revision=1, docx_bytes=docx_bytes
    )
    source_reader = _FakeSourceReader(_success_read(capture))

    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot is not None
    assert result.snapshot.final_docx_sha256 == hashlib.sha256(docx_bytes).hexdigest()
    # the expected_* args passed through to the source reader came from the
    # observed binding, not any caller-supplied value.
    assert source_reader.calls == [(owner_key, proposal.artifact_fingerprint, 1)]


# -- build failure passthrough -------------------------------------------------------------


def test_build_failure_passes_through_status_and_diagnostics(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal bytes for a build-failure passthrough test"
    _seed_current_proposal(artifact_repository, owner_key, revision=1, docx_bytes=docx_bytes, artifact_fingerprint=_sha("proposal-1"))
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=artifact_repository)

    failing_reader = _FakeSourceReader(
        WorkingCopyReadResult(
            status=WorkingCopyReadStatus.SOURCE_PROPOSAL_NOT_FOUND,
            error_code=WorkingCopyReadStatus.SOURCE_PROPOSAL_NOT_FOUND,
            error_message="proposal artifact not found for test",
        )
    )
    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=failing_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.SOURCE_PROPOSAL_NOT_FOUND
    assert result.snapshot is None
    assert result.diagnostics


# -- source_proposal_is_current: True / False / current missing / lookup unavailable ----


def test_source_proposal_is_current_true_when_still_current(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"unedited proposal bytes, still current"
    proposal = _seed_current_proposal(
        artifact_repository, owner_key, revision=1, docx_bytes=docx_bytes, artifact_fingerprint=_sha("proposal-current")
    )
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=artifact_repository)

    capture = _capture(owner_key, proposal_fingerprint=proposal.artifact_fingerprint, proposal_revision=1, docx_bytes=docx_bytes)
    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=_FakeSourceReader(_success_read(capture)),
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.source_proposal_is_current is True


def test_source_proposal_is_current_false_when_superseded(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    docx_bytes_n = b"historical proposal N bytes"
    proposal_n = _seed_current_proposal(
        artifact_repository, owner_key, revision=1, docx_bytes=docx_bytes_n, artifact_fingerprint=_sha("proposal-n")
    )
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=artifact_repository)

    # Move the current slot forward to N+1 -- the working copy is still
    # bound to (and the fake source reader still reports) N.
    _seed_current_proposal(
        artifact_repository, owner_key, revision=2, docx_bytes=b"proposal N+1 bytes", artifact_fingerprint=_sha("proposal-n-plus-1")
    )

    capture = _capture(owner_key, proposal_fingerprint=proposal_n.artifact_fingerprint, proposal_revision=1, docx_bytes=docx_bytes_n)
    source_reader = _FakeSourceReader(_success_read(capture))

    # inspect_working_copy would ordinarily now observe a stale binding
    # against the new current proposal, but the finalization flow here is
    # driven entirely by the fake source reader's own capture -- so we
    # bypass the store's own OBSERVED/VALID gate via a fake inspecting store
    # reporting the same (now-stale) observed binding.
    from app.schemas.cv_document_working_copy import (
        WorkingCopyBindingState,
        WorkingCopyInspectionResult,
        WorkingCopyInspectionStatus,
        WorkingCopyObservedState,
        compute_binding_state_fingerprint,
    )

    fingerprint = compute_binding_state_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        working_copy_sha256=hashlib.sha256(docx_bytes_n).hexdigest(),
        binding_state=WorkingCopyBindingState.VALID,
        sidecar_raw_sha256=_sha("sidecar"),
        binding_schema_version="cv-document-working-copy-binding-schema-v1",
        bound_proposal_artifact_fingerprint=proposal_n.artifact_fingerprint,
        bound_proposal_revision=1,
    )
    observed_state = WorkingCopyObservedState(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        working_copy_sha256=hashlib.sha256(docx_bytes_n).hexdigest(),
        binding_state=WorkingCopyBindingState.VALID,
        binding_state_fingerprint=fingerprint,
        sidecar_raw_sha256=_sha("sidecar"),
        binding_schema_version="cv-document-working-copy-binding-schema-v1",
        bound_proposal_artifact_fingerprint=proposal_n.artifact_fingerprint,
        bound_proposal_revision=1,
    )
    fake_store = _FakeInspectingStore(
        WorkingCopyInspectionResult(status=WorkingCopyInspectionStatus.OBSERVED, observed_state=observed_state)
    )

    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=fake_store,
        source_reader=source_reader,
        snapshot_repository=snapshot_repository,
        artifact_repository=artifact_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.source_proposal_is_current is False


def test_source_proposal_is_current_false_when_current_missing(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal bytes, then the current slot gets emptied"
    proposal = _seed_current_proposal(
        artifact_repository, owner_key, revision=1, docx_bytes=docx_bytes, artifact_fingerprint=_sha("proposal-1")
    )
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=artifact_repository)

    empty_repository = InMemoryCvDocumentArtifactRepository()
    capture = _capture(owner_key, proposal_fingerprint=proposal.artifact_fingerprint, proposal_revision=1, docx_bytes=docx_bytes)
    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=_FakeSourceReader(_success_read(capture)),
        snapshot_repository=snapshot_repository,
        artifact_repository=empty_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.source_proposal_is_current is False


def test_source_proposal_is_current_none_when_lookup_unavailable(
    working_copy_store, artifact_repository, snapshot_repository
) -> None:
    owner_key = _owner_key()
    docx_bytes = b"proposal bytes for a lookup-unavailable test"
    proposal = _seed_current_proposal(
        artifact_repository, owner_key, revision=1, docx_bytes=docx_bytes, artifact_fingerprint=_sha("proposal-1")
    )
    working_copy_store.create_or_refresh_working_copy(owner_key=owner_key, repository=artifact_repository)

    class _RaisingRepository:
        def get_current_proposal(self, owner_key):
            raise RuntimeError("simulated repository failure during best-effort lookup")

    capture = _capture(owner_key, proposal_fingerprint=proposal.artifact_fingerprint, proposal_revision=1, docx_bytes=docx_bytes)
    result = finalize_working_copy_to_final_docx_snapshot(
        owner_key=owner_key,
        working_copy_store=working_copy_store,
        source_reader=_FakeSourceReader(_success_read(capture)),
        snapshot_repository=snapshot_repository,
        artifact_repository=_RaisingRepository(),
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot is not None
    assert result.source_proposal_is_current is None
    assert result.diagnostics


# -- FinalizeWorkingCopyResult validator ----------------------------------------------------


def test_finalized_without_snapshot_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.FINALIZED, snapshot=None)


def test_failure_with_snapshot_is_rejected() -> None:
    from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot

    owner_key = _owner_key()
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=_sha("p"),
        source_proposal_revision=1,
        generated_proposal_sha256=_sha("g"),
        final_docx_sha256=_sha("g"),
        edited_by_user=False,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
        final_snapshot_fingerprint=_sha("fp"),
    )
    with pytest.raises(ValidationError):
        FinalizeWorkingCopyResult(status=FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING, snapshot=snapshot)


def test_failure_with_source_proposal_is_current_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FinalizeWorkingCopyResult(
            status=FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING, source_proposal_is_current=True
        )


# -- caller-facing signature contract -------------------------------------------------------


def test_signature_never_accepts_caller_supplied_bytes_hash_revision_fingerprint_or_path() -> None:
    signature = inspect.signature(finalize_working_copy_to_final_docx_snapshot)
    forbidden_substrings = ("bytes", "hash", "revision", "fingerprint", "edited_by_user", "path")
    for name in signature.parameters:
        if name == "owner_key" or name == "finalization_policy_version":
            continue
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"parameter {name!r} looks like a forbidden caller-supplied value"


def test_signature_is_keyword_only_and_exactly_these_names() -> None:
    signature = inspect.signature(finalize_working_copy_to_final_docx_snapshot)
    assert set(signature.parameters) == {
        "owner_key",
        "working_copy_store",
        "source_reader",
        "snapshot_repository",
        "artifact_repository",
        "finalization_policy_version",
    }
    for param in signature.parameters.values():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
