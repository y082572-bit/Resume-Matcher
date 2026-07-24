"""Unit tests for the Explicit Provenance Stage P6-A Final DOCX Snapshot
Domain Addendum: ``FinalDocxSnapshot`` schema invariants, the
``FinalDocxSourceReader`` contract (``FinalDocxSourceCapture`` /
``WorkingCopyReadResult``), and ``InMemoryFinalDocxSnapshotRepository``.
"""

from __future__ import annotations

import hashlib
import inspect
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_snapshot import (
    FINAL_SNAPSHOT_SCHEMA_VERSION,
    FinalDocxSnapshot,
)
from app.services.cv_document_final_snapshot_repository import (
    FinalDocxSnapshotRepository,
    FinalSnapshotSaveStatus,
    InMemoryFinalDocxSnapshotRepository,
    compute_final_snapshot_fingerprint,
)
from app.services.cv_document_final_source_reader import (
    FinalDocxSourceCapture,
    FinalDocxSourceReader,
    WorkingCopyReadResult,
    WorkingCopyReadStatus,
)
from app.services.cv_document_owner_identity import build_owner_key


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_key(reference_id: str = "job-final-snapshot-repo-test"):
    return build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )


def _final_snapshot(
    *,
    owner_key=None,
    source_proposal_artifact_fingerprint: str | None = None,
    source_proposal_revision: int = 1,
    generated_proposal_sha256: str | None = None,
    final_docx_sha256: str | None = None,
    finalization_policy_version: str = "final-docx-finalization-policy-v1",
    snapshot_schema_version: str = FINAL_SNAPSHOT_SCHEMA_VERSION,
) -> FinalDocxSnapshot:
    owner_key = owner_key or _owner_key()
    source_proposal_artifact_fingerprint = source_proposal_artifact_fingerprint or _sha("proposal-artifact-v1")
    generated_proposal_sha256 = generated_proposal_sha256 or _sha("generated-docx-v1")
    final_docx_sha256 = final_docx_sha256 or generated_proposal_sha256
    fingerprint = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=source_proposal_artifact_fingerprint,
        source_proposal_revision=source_proposal_revision,
        generated_proposal_sha256=generated_proposal_sha256,
        final_docx_sha256=final_docx_sha256,
        finalization_policy_version=finalization_policy_version,
        snapshot_schema_version=snapshot_schema_version,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=source_proposal_artifact_fingerprint,
        source_proposal_revision=source_proposal_revision,
        generated_proposal_sha256=generated_proposal_sha256,
        final_docx_sha256=final_docx_sha256,
        edited_by_user=generated_proposal_sha256 != final_docx_sha256,
        finalization_policy_version=finalization_policy_version,
        snapshot_schema_version=snapshot_schema_version,
        final_snapshot_fingerprint=fingerprint,
    )


# -- SCHEMA -------------------------------------------------------------------------


def test_final_docx_snapshot_is_frozen() -> None:
    snapshot = _final_snapshot()
    with pytest.raises(ValidationError):
        snapshot.edited_by_user = True  # type: ignore[misc]


def test_final_docx_snapshot_extra_fields_rejected() -> None:
    snapshot = _final_snapshot()
    data = snapshot.model_dump()
    data["structural_validation_result"] = {"status": "VALID"}
    with pytest.raises(ValidationError):
        FinalDocxSnapshot(**data)


def test_edited_by_user_false_for_identical_hashes() -> None:
    same_hash = _sha("identical-docx-bytes")
    snapshot = _final_snapshot(generated_proposal_sha256=same_hash, final_docx_sha256=same_hash)
    assert snapshot.edited_by_user is False


def test_edited_by_user_true_for_different_hashes() -> None:
    snapshot = _final_snapshot(
        generated_proposal_sha256=_sha("generated-v1"), final_docx_sha256=_sha("edited-in-word-v1")
    )
    assert snapshot.edited_by_user is True


def test_inconsistent_edited_by_user_is_rejected() -> None:
    same_hash = _sha("identical-docx-bytes-2")
    owner_key = _owner_key()
    fingerprint = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=_sha("proposal-artifact-v1"),
        source_proposal_revision=1,
        generated_proposal_sha256=same_hash,
        final_docx_sha256=same_hash,
        finalization_policy_version="final-docx-finalization-policy-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    with pytest.raises(ValidationError):
        FinalDocxSnapshot(
            owner_key=owner_key,
            source_proposal_artifact_fingerprint=_sha("proposal-artifact-v1"),
            source_proposal_revision=1,
            generated_proposal_sha256=same_hash,
            final_docx_sha256=same_hash,
            edited_by_user=True,  # inconsistent: hashes are identical
            finalization_policy_version="final-docx-finalization-policy-v1",
            snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
            final_snapshot_fingerprint=fingerprint,
        )


def test_fingerprint_does_not_depend_on_edited_by_user() -> None:
    """Two snapshots differing only in whether the user edited the file
    (unmodified vs. edited) but sharing the same generated/final hash pair
    -- which cannot both be true simultaneously for the *same* snapshot --
    prove the point structurally instead: the fingerprint function itself
    takes no ``edited_by_user`` parameter at all."""

    signature = inspect.signature(compute_final_snapshot_fingerprint)
    assert "edited_by_user" not in signature.parameters


def test_fingerprint_excludes_path_and_timestamp_fields_not_on_model() -> None:
    forbidden = {"path", "filename", "locator", "timestamp", "mtime", "filesize"}
    assert forbidden.isdisjoint(set(FinalDocxSnapshot.model_fields))
    assert forbidden.isdisjoint(set(inspect.signature(compute_final_snapshot_fingerprint).parameters))


# -- SOURCE READER CONTRACT ----------------------------------------------------------


def test_success_requires_capture() -> None:
    with pytest.raises(ValidationError):
        WorkingCopyReadResult(status=WorkingCopyReadStatus.SUCCESS, capture=None)


def test_failure_forbids_capture() -> None:
    capture = FinalDocxSourceCapture(
        owner_key_fingerprint=_sha("owner"),
        source_proposal_artifact_fingerprint=_sha("proposal"),
        source_proposal_revision=1,
        generated_proposal_sha256=_sha("generated"),
        docx_bytes=b"some docx bytes",
    )
    with pytest.raises(ValidationError):
        WorkingCopyReadResult(
            status=WorkingCopyReadStatus.WORKING_COPY_MISSING,
            capture=capture,
            error_code=WorkingCopyReadStatus.WORKING_COPY_MISSING,
            error_message="working copy missing",
        )


def test_capture_preserves_owner_proposal_baseline_binding() -> None:
    owner_key_fingerprint = _sha("owner-binding")
    proposal_fingerprint = _sha("proposal-binding")
    generated_sha = _sha("generated-baseline")
    capture = FinalDocxSourceCapture(
        owner_key_fingerprint=owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal_fingerprint,
        source_proposal_revision=3,
        generated_proposal_sha256=generated_sha,
        docx_bytes=b"exact working copy bytes",
    )
    assert capture.owner_key_fingerprint == owner_key_fingerprint
    assert capture.source_proposal_artifact_fingerprint == proposal_fingerprint
    assert capture.source_proposal_revision == 3
    assert capture.generated_proposal_sha256 == generated_sha


def test_no_caller_controlled_path_field() -> None:
    forbidden = {"path", "filesystem_path", "file_path", "filename", "locator"}
    assert forbidden.isdisjoint(set(FinalDocxSourceCapture.model_fields))
    signature = inspect.signature(FinalDocxSourceReader.read_finalization_source)
    assert forbidden.isdisjoint(set(signature.parameters))


# -- REPOSITORY -----------------------------------------------------------------------


def test_repository_satisfies_protocol_and_saves() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    assert isinstance(repo, FinalDocxSnapshotRepository)

    docx_bytes = b"final docx payload for protocol conformance"
    snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(docx_bytes).hexdigest())
    result = repo.save_final_docx_snapshot(snapshot, docx_bytes)
    assert result.status == FinalSnapshotSaveStatus.SAVED


def test_save_recomputes_final_docx_sha256() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    real_bytes = b"the actual final docx bytes"
    snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(real_bytes).hexdigest())

    result = repo.save_final_docx_snapshot(snapshot, b"a completely different payload")
    assert result.status == FinalSnapshotSaveStatus.DOCX_HASH_MISMATCH


def test_save_recomputes_final_snapshot_fingerprint() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    docx_bytes = b"final docx payload for fingerprint recompute test"
    valid_snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(docx_bytes).hexdigest())
    tampered = valid_snapshot.model_copy(update={"final_snapshot_fingerprint": _sha("tampered-fingerprint")})

    result = repo.save_final_docx_snapshot(tampered, docx_bytes)
    assert result.status == FinalSnapshotSaveStatus.SNAPSHOT_FINGERPRINT_MISMATCH


def test_metadata_retrieval_only_by_final_snapshot_fingerprint() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    docx_bytes = b"final docx payload for metadata retrieval test"
    snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(docx_bytes).hexdigest())
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    assert repo.get_final_docx_snapshot(snapshot.final_snapshot_fingerprint) is not None
    assert repo.get_final_docx_snapshot(snapshot.final_docx_sha256) is None


def test_bytes_retrieval_only_by_bytes_hash() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    docx_bytes = b"final docx payload for bytes retrieval test"
    snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(docx_bytes).hexdigest())
    repo.save_final_docx_snapshot(snapshot, docx_bytes)

    assert repo.retrieve_final_docx_bytes(snapshot.final_docx_sha256) == docx_bytes
    assert repo.retrieve_final_docx_bytes(snapshot.final_snapshot_fingerprint) is None


def test_two_lineage_records_can_share_same_bytes() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    owner_key = _owner_key()
    docx_bytes = b"identical final docx bytes across two revisions"
    docx_sha256 = hashlib.sha256(docx_bytes).hexdigest()

    revision_one = _final_snapshot(owner_key=owner_key, source_proposal_revision=1, final_docx_sha256=docx_sha256)
    revision_two = _final_snapshot(owner_key=owner_key, source_proposal_revision=2, final_docx_sha256=docx_sha256)
    assert revision_one.final_snapshot_fingerprint != revision_two.final_snapshot_fingerprint

    result_one = repo.save_final_docx_snapshot(revision_one, docx_bytes)
    result_two = repo.save_final_docx_snapshot(revision_two, docx_bytes)
    assert result_one.status == FinalSnapshotSaveStatus.SAVED
    assert result_two.status == FinalSnapshotSaveStatus.SAVED

    assert repo.get_final_docx_snapshot(revision_one.final_snapshot_fingerprint) == revision_one
    assert repo.get_final_docx_snapshot(revision_two.final_snapshot_fingerprint) == revision_two
    assert repo.retrieve_final_docx_bytes(docx_sha256) == docx_bytes


def test_repository_never_returns_arbitrary_snapshot_by_bytes_hash() -> None:
    assert not hasattr(InMemoryFinalDocxSnapshotRepository, "get_final_docx_snapshot_by_bytes_hash")
    signature = inspect.signature(InMemoryFinalDocxSnapshotRepository.retrieve_final_docx_bytes)
    assert signature.return_annotation in ("bytes | None", bytes | None)


def test_idempotent_identical_save() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    docx_bytes = b"final docx payload for idempotent save test"
    snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(docx_bytes).hexdigest())

    first = repo.save_final_docx_snapshot(snapshot, docx_bytes)
    second = repo.save_final_docx_snapshot(snapshot, docx_bytes)
    assert first.status == FinalSnapshotSaveStatus.SAVED
    assert second.status == FinalSnapshotSaveStatus.ALREADY_EXISTS_IDENTICAL


def test_same_fingerprint_different_metadata_blocks() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    docx_bytes = b"final docx payload for metadata conflict test"
    docx_sha256 = hashlib.sha256(docx_bytes).hexdigest()

    shared_owner_key_fingerprint = _sha("shared-owner-key-fingerprint")
    owner_a = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-a"
    ).model_copy(update={"owner_key_fingerprint": shared_owner_key_fingerprint})
    owner_b = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-b"
    ).model_copy(update={"owner_key_fingerprint": shared_owner_key_fingerprint})
    assert owner_a.person_entity_id != owner_b.person_entity_id

    snapshot_a = _final_snapshot(owner_key=owner_a, final_docx_sha256=docx_sha256)
    snapshot_b = _final_snapshot(owner_key=owner_b, final_docx_sha256=docx_sha256)
    assert snapshot_a.final_snapshot_fingerprint == snapshot_b.final_snapshot_fingerprint
    assert snapshot_a != snapshot_b

    first = repo.save_final_docx_snapshot(snapshot_a, docx_bytes)
    second = repo.save_final_docx_snapshot(snapshot_b, docx_bytes)
    assert first.status == FinalSnapshotSaveStatus.SAVED
    assert second.status == FinalSnapshotSaveStatus.SNAPSHOT_METADATA_CONFLICT


def test_same_bytes_hash_different_bytes_blocks() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    real_bytes = b"final docx payload for bytes conflict test"
    real_sha256 = hashlib.sha256(real_bytes).hexdigest()
    repo._bytes[real_sha256] = b"corrupted internal bytes already stored under this hash"

    snapshot = _final_snapshot(final_docx_sha256=real_sha256)
    result = repo.save_final_docx_snapshot(snapshot, real_bytes)
    assert result.status == FinalSnapshotSaveStatus.DOCX_BYTES_CONFLICT


def test_external_bytearray_mutation_after_save_does_not_affect_repository() -> None:
    repo = InMemoryFinalDocxSnapshotRepository()
    payload = bytearray(b"final docx payload for mutation-after-save test")
    original_bytes = bytes(payload)
    snapshot = _final_snapshot(final_docx_sha256=hashlib.sha256(original_bytes).hexdigest())

    result = repo.save_final_docx_snapshot(snapshot, payload)
    assert result.status == FinalSnapshotSaveStatus.SAVED

    payload[0:5] = b"XXXXX"  # caller mutates its own buffer after the call returns

    stored_bytes = repo.retrieve_final_docx_bytes(snapshot.final_docx_sha256)
    assert stored_bytes == original_bytes

    stored_metadata = repo.get_final_docx_snapshot(snapshot.final_snapshot_fingerprint)
    assert stored_metadata is not None
    assert stored_metadata is not snapshot
