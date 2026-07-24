"""Unit tests for the Explicit Provenance Stage P6-A Final DOCX Snapshot
Domain Addendum: ``finalize_current_docx_for_pdf``.

Every scenario is driven by a deterministic fake ``FinalDocxSourceReader``
and either the real ``InMemoryFinalDocxSnapshotRepository`` or a thin spy
wrapper around it -- P6-A ships no real filesystem reader.
"""

from __future__ import annotations

import hashlib
import inspect
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_snapshot import (
    FinalDocxSnapshot,
    FinalDocxSnapshotBuildStatus,
)
from app.services.cv_document_final_snapshot_builder import finalize_current_docx_for_pdf
from app.services.cv_document_final_snapshot_repository import (
    FinalSnapshotSaveStatus,
    InMemoryFinalDocxSnapshotRepository,
)
from app.services.cv_document_final_source_reader import (
    FinalDocxSourceCapture,
    WorkingCopyReadResult,
    WorkingCopyReadStatus,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_repository_protocol import InMemoryCvDocumentArtifactRepository

import app.services.cv_document_final_snapshot_builder as builder_module


FINALIZATION_POLICY_VERSION = "cv-document-final-snapshot-finalization-policy-test-v1"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_key(reference_id: str = "job-final-snapshot-builder-test"):
    return build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )


def _capture(
    owner_key,
    *,
    proposal_fingerprint: str,
    proposal_revision: int,
    generated_sha256: str,
    docx_bytes: bytes,
) -> FinalDocxSourceCapture:
    return FinalDocxSourceCapture(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_fingerprint,
        source_proposal_revision=proposal_revision,
        generated_proposal_sha256=generated_sha256,
        docx_bytes=docx_bytes,
    )


def _success(capture: FinalDocxSourceCapture) -> WorkingCopyReadResult:
    return WorkingCopyReadResult(status=WorkingCopyReadStatus.SUCCESS, capture=capture)


def _failure(status: WorkingCopyReadStatus) -> WorkingCopyReadResult:
    return WorkingCopyReadResult(status=status, error_code=status, error_message=f"{status.value} for test")


class _SpySourceReader:
    def __init__(self, result: WorkingCopyReadResult) -> None:
        self._result = result
        self.call_count = 0
        self.calls: list[tuple] = []

    def read_finalization_source(self, owner_key, expected_proposal_artifact_fingerprint, expected_proposal_revision):
        self.call_count += 1
        self.calls.append((owner_key, expected_proposal_artifact_fingerprint, expected_proposal_revision))
        return self._result


_UNSET = object()


class _SpySnapshotRepository:
    """Wraps a real in-memory repository but lets tests override what a
    post-save re-read returns, to prove the builder recomputes/re-checks
    rather than blindly trusting the repository."""

    def __init__(self) -> None:
        self._inner = InMemoryFinalDocxSnapshotRepository()
        self.get_calls: list[str] = []
        self.override_bytes: bytes | None | object = _UNSET
        self.override_metadata: FinalDocxSnapshot | None | object = _UNSET

    def save_final_docx_snapshot(self, snapshot, docx_bytes):
        return self._inner.save_final_docx_snapshot(snapshot, docx_bytes)

    def get_final_docx_snapshot(self, final_snapshot_fingerprint):
        self.get_calls.append(final_snapshot_fingerprint)
        if self.override_metadata is not _UNSET:
            return self.override_metadata
        return self._inner.get_final_docx_snapshot(final_snapshot_fingerprint)

    def retrieve_final_docx_bytes(self, final_docx_sha256):
        if self.override_bytes is not _UNSET:
            return self.override_bytes
        return self._inner.retrieve_final_docx_bytes(final_docx_sha256)


def _default_flow(
    *,
    owner_key=None,
    docx_bytes: bytes = b"the current word working copy bytes",
    generated_sha256: str | None = None,
):
    owner_key = owner_key or _owner_key()
    proposal_fingerprint = _sha("proposal-artifact-fingerprint-v1")
    proposal_revision = 1
    generated_sha256 = generated_sha256 or hashlib.sha256(docx_bytes).hexdigest()
    capture = _capture(
        owner_key,
        proposal_fingerprint=proposal_fingerprint,
        proposal_revision=proposal_revision,
        generated_sha256=generated_sha256,
        docx_bytes=docx_bytes,
    )
    reader = _SpySourceReader(_success(capture))
    repository = InMemoryFinalDocxSnapshotRepository()
    return owner_key, proposal_fingerprint, proposal_revision, reader, repository


# -- BUILDER --------------------------------------------------------------------------


def test_unmodified_bytes_give_edited_by_user_false() -> None:
    docx_bytes = b"pipeline-generated docx bytes, never touched"
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow(docx_bytes=docx_bytes)

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.edited_by_user is False


def test_changed_bytes_give_edited_by_user_true() -> None:
    docx_bytes = b"the user opened this in Word and typed something new"
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow(
        docx_bytes=docx_bytes, generated_sha256=_sha("original-pipeline-generated-hash")
    )

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.edited_by_user is True


def test_builder_does_not_accept_generated_proposal_sha256_argument() -> None:
    signature = inspect.signature(finalize_current_docx_for_pdf)
    assert "generated_proposal_sha256" not in signature.parameters
    assert set(signature.parameters) == {
        "owner_key",
        "expected_proposal_artifact_fingerprint",
        "expected_proposal_revision",
        "source_reader",
        "snapshot_repository",
        "finalization_policy_version",
    }


def test_builder_uses_baseline_from_source_capture() -> None:
    docx_bytes = b"exact bytes for baseline-from-capture test"
    baseline = _sha("the-one-and-only-trusted-baseline")
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow(
        docx_bytes=docx_bytes, generated_sha256=baseline
    )

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert result.snapshot.generated_proposal_sha256 == baseline


def test_fake_owner_binding_blocks() -> None:
    owner_key = _owner_key()
    proposal_fingerprint = _sha("proposal-artifact-fingerprint-v1")
    docx_bytes = b"bytes for fake owner binding test"
    capture = _capture(
        owner_key,
        proposal_fingerprint=proposal_fingerprint,
        proposal_revision=1,
        generated_sha256=hashlib.sha256(docx_bytes).hexdigest(),
        docx_bytes=docx_bytes,
    ).model_copy(update={"owner_key_fingerprint": _sha("a-completely-different-owner")})
    reader = _SpySourceReader(_success(capture))
    repository = InMemoryFinalDocxSnapshotRepository()

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=1,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH
    assert result.snapshot is None


def test_fake_proposal_fingerprint_blocks() -> None:
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow()
    reader._result = reader._result.model_copy(
        update={
            "capture": reader._result.capture.model_copy(
                update={"source_proposal_artifact_fingerprint": _sha("a-different-proposal-artifact")}
            )
        }
    )

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH


def test_fake_proposal_revision_blocks() -> None:
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow()
    reader._result = reader._result.model_copy(
        update={"capture": reader._result.capture.model_copy(update={"source_proposal_revision": proposal_revision + 1})}
    )

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_BINDING_MISMATCH


def test_caller_cannot_falsify_baseline() -> None:
    """There is no parameter through which a caller can inject a baseline
    hash -- whatever the source capture reports is what gets used, no
    matter what a caller might otherwise expect or intend."""

    docx_bytes = b"bytes for caller-cannot-falsify-baseline test"
    true_baseline = _sha("true-pipeline-generated-baseline")
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow(
        docx_bytes=docx_bytes, generated_sha256=true_baseline
    )

    call_kwargs = dict(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    with pytest.raises(TypeError):
        finalize_current_docx_for_pdf(generated_proposal_sha256=_sha("attacker-supplied-baseline"), **call_kwargs)

    result = finalize_current_docx_for_pdf(**call_kwargs)
    assert result.snapshot.generated_proposal_sha256 == true_baseline


def test_source_reader_called_exactly_once() -> None:
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow()

    finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert reader.call_count == 1


def test_persisted_bytes_are_exactly_capture_bytes() -> None:
    docx_bytes = b"the exact bytes that must be persisted byte-for-byte"
    owner_key, proposal_fingerprint, proposal_revision, reader, repository = _default_flow(docx_bytes=docx_bytes)

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert repository.retrieve_final_docx_bytes(result.snapshot.final_docx_sha256) == docx_bytes


def test_post_save_bytes_hash_is_recalculated() -> None:
    owner_key, proposal_fingerprint, proposal_revision, reader, _ = _default_flow()
    spy_repository = _SpySnapshotRepository()
    spy_repository.override_bytes = b"corrupted bytes that do not match final_docx_sha256"

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=spy_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINAL_SNAPSHOT_PERSISTENCE_FAILED
    assert result.snapshot is None


def test_post_save_metadata_is_read_by_fingerprint() -> None:
    owner_key, proposal_fingerprint, proposal_revision, reader, _ = _default_flow()
    spy_repository = _SpySnapshotRepository()

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=spy_repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.FINALIZED
    assert spy_repository.get_calls == [result.snapshot.final_snapshot_fingerprint]


def test_missing_working_copy_blocks() -> None:
    owner_key = _owner_key()
    reader = _SpySourceReader(_failure(WorkingCopyReadStatus.WORKING_COPY_MISSING))
    repository = InMemoryFinalDocxSnapshotRepository()

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=_sha("proposal"),
        expected_proposal_revision=1,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_MISSING
    assert result.snapshot is None


def test_locked_working_copy_blocks() -> None:
    owner_key = _owner_key()
    reader = _SpySourceReader(_failure(WorkingCopyReadStatus.WORKING_COPY_LOCKED))
    repository = InMemoryFinalDocxSnapshotRepository()

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=_sha("proposal"),
        expected_proposal_revision=1,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_LOCKED


def test_changed_during_read_blocks() -> None:
    owner_key = _owner_key()
    reader = _SpySourceReader(_failure(WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ))
    repository = InMemoryFinalDocxSnapshotRepository()

    result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=_sha("proposal"),
        expected_proposal_revision=1,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert result.status == FinalDocxSnapshotBuildStatus.WORKING_COPY_CHANGED_DURING_READ


def _import_lines(module) -> list[str]:
    return [
        line.strip()
        for line in inspect.getsource(module).splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]


def test_structural_validator_not_in_flow() -> None:
    imports = _import_lines(builder_module)
    assert not any("cv_document_adapters" in line for line in imports)
    assert not any("DocxStructuralValidator" in line for line in imports)


def test_manual_confirmation_not_in_flow() -> None:
    imports = _import_lines(builder_module)
    assert not any("ManualDocumentSnapshotConfirmation" in line for line in imports)
    assert not any("cv_document_snapshot_builder" in line for line in imports)


def test_truth_library_not_in_flow() -> None:
    imports = _import_lines(builder_module)
    assert not any("truth_repository" in line for line in imports)
    assert not any("truth_validator" in line for line in imports)


def test_finalization_does_not_use_cv_document_artifact_repository() -> None:
    imports = _import_lines(builder_module)
    assert not any("cv_document_repository_protocol" in line for line in imports)
    assert not any("CvDocumentArtifactRepository" in line for line in imports)


def test_finalization_does_not_touch_existing_proposal_repository() -> None:
    """Running the new addendum flow against a fresh
    ``InMemoryFinalDocxSnapshotRepository`` must leave a completely
    separate, pre-populated P6-A ``InMemoryCvDocumentArtifactRepository``
    untouched."""

    from app.schemas.cv_document_artifact import CvDocxProposalArtifact, DocxProposalProvenanceMode

    legacy_repository = InMemoryCvDocumentArtifactRepository()
    owner_key = _owner_key()
    legacy_proposal_bytes = b"legacy P6-A proposal bytes untouched by the addendum"
    legacy_docx_hash = hashlib.sha256(legacy_proposal_bytes).hexdigest()
    legacy_artifact = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint=_sha("approved-content"),
        content_plan_fingerprint=_sha("content-plan"),
        role_strategy_context_fingerprint=None,
        document_schema_version="document-schema-v1",
        rendering_policy_version="rendering-policy-v1",
        renderer_adapter_id="renderer-v1",
        template_fingerprint=_sha("template"),
        generation_input_fingerprint=_sha("generation-input"),
        generated_docx_content_hash=legacy_docx_hash,
        current_file_hash=legacy_docx_hash,
        validated_file_hash=None,
        proposal_revision=1,
        superseded=False,
        artifact_fingerprint=_sha("legacy-proposal-artifact"),
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    legacy_repository.replace_current_proposal(owner_key, legacy_artifact, legacy_proposal_bytes, None)

    _, proposal_fingerprint, proposal_revision, reader, repository = _default_flow(owner_key=owner_key)

    addendum_result = finalize_current_docx_for_pdf(
        owner_key=owner_key,
        expected_proposal_artifact_fingerprint=proposal_fingerprint,
        expected_proposal_revision=proposal_revision,
        source_reader=reader,
        snapshot_repository=repository,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
    )
    assert addendum_result.status == FinalDocxSnapshotBuildStatus.FINALIZED

    still_current = legacy_repository.get_current_proposal(owner_key)
    assert still_current is not None
    assert still_current.artifact_fingerprint == legacy_artifact.artifact_fingerprint
    assert legacy_repository.read_current_proposal_bytes(owner_key) == legacy_proposal_bytes
