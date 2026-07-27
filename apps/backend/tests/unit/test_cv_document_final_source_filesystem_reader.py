"""Unit tests for ``FilesystemFinalDocxSourceReader``: owner-identity
revalidation, the owner-lock boundary (exactly one acquire/release, no
nested acquisition, ``LOCK_PATH_IDENTITY_MISMATCH``/``LOCK_CAPABILITY_INVALID``
kept distinct), secure ``current.docx``/``binding.json`` observation
(including the exact binding.json I/O status mapping -- MISSING/LOCKED/
UNREADABLE/CHANGED_DURING_READ/TOO_LARGE never collapsed into
``WORKING_COPY_BINDING_INVALID``, which is reserved for a successful read
that fails to parse), defensive re-validation of a ``VERIFIED`` lineage
result against the exact values it was queried with (fail-closed on any
divergence -- a non-conforming ``ProposalArtifactLineageReader`` can never
smuggle a mismatched identity into a capture), the complete lineage-status
mapping, and "zero filesystem mutation".
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

import app.services.cv_document_final_source_filesystem_reader as reader_module
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_working_copy import (
    OwnerLockAcquireStatus,
    WorkingCopyBinding,
    WorkingCopyStableReadResult,
    WorkingCopyStableReadStatus,
    serialize_working_copy_binding,
)
from app.services.cv_document_final_source_filesystem_reader import (
    FilesystemFinalDocxSourceReader,
    _LINEAGE_STATUS_TO_READ_STATUS,
)
from app.services.cv_document_final_source_reader import WorkingCopyReadStatus
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_owner_lock import OwnerLockToken, PlatformOwnerLock
from app.services.cv_document_proposal_artifact_lineage_reader import (
    ProposalArtifactLineage,
    ProposalArtifactLineageReadResult,
    ProposalArtifactLineageStatus,
)
from app.services.cv_document_working_copy_paths import WorkingCopyPaths


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _owner_key(reference_id: str = "job-fs-reader-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


@pytest.fixture
def paths(tmp_path: Path) -> WorkingCopyPaths:
    return WorkingCopyPaths(tmp_path / "working_copy_root", tmp_path / "blob_store_root")


class _FakeLineageReader:
    def __init__(self, result: ProposalArtifactLineageReadResult) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def read_verified_proposal_artifact_lineage(
        self,
        *,
        artifact_fingerprint: str,
        expected_owner_key_fingerprint: str,
        expected_proposal_revision: int,
        expected_generated_docx_sha256: str,
    ) -> ProposalArtifactLineageReadResult:
        self.calls.append(
            (artifact_fingerprint, expected_owner_key_fingerprint, expected_proposal_revision, expected_generated_docx_sha256)
        )
        return self._result


def _verified_lineage(**overrides) -> ProposalArtifactLineageReadResult:
    defaults = dict(
        artifact_fingerprint=_sha("lineage-artifact"),
        owner_key_fingerprint=_sha("owner"),
        proposal_revision=1,
        generated_docx_content_hash=_sha("lineage-generated"),
        docx_bytes=b"the proposal's own bytes, never the capture's docx_bytes",
    )
    defaults.update(overrides)
    return ProposalArtifactLineageReadResult(
        status=ProposalArtifactLineageStatus.VERIFIED, lineage=ProposalArtifactLineage(**defaults)
    )


def _failed_lineage(status: ProposalArtifactLineageStatus) -> ProposalArtifactLineageReadResult:
    return ProposalArtifactLineageReadResult(status=status, error_message=f"{status.value} for test")


def _write_current_docx(paths: WorkingCopyPaths, owner_fp: str, data: bytes) -> None:
    paths.ensure_owner_dirs(owner_fp)
    paths.current_docx_path(owner_fp).write_bytes(data)


def _write_binding(
    paths: WorkingCopyPaths,
    owner_fp: str,
    *,
    artifact_fingerprint: str,
    revision: int,
    generated_hash: str,
    last_written_hash: str | None = None,
) -> None:
    paths.ensure_owner_dirs(owner_fp)
    binding = WorkingCopyBinding(
        owner_key_fingerprint=owner_fp,
        bound_proposal_artifact_fingerprint=artifact_fingerprint,
        bound_proposal_revision=revision,
        bound_generated_proposal_sha256=generated_hash,
        last_known_tool_written_hash=last_written_hash or generated_hash,
    )
    paths.binding_path(owner_fp).write_bytes(serialize_working_copy_binding(binding))


def _seed_full_working_copy(
    paths: WorkingCopyPaths,
    owner_key,
    *,
    docx_bytes: bytes = b"the current.docx bytes to finalize",
    artifact_fingerprint: str | None = None,
    revision: int = 1,
    generated_hash: str | None = None,
) -> tuple[str, int, str]:
    owner_fp = owner_key.owner_key_fingerprint
    artifact_fingerprint = artifact_fingerprint or _sha("sidecar-artifact-hint")
    generated_hash = generated_hash or _sha("sidecar-generated-hint")
    _write_current_docx(paths, owner_fp, docx_bytes)
    _write_binding(
        paths, owner_fp, artifact_fingerprint=artifact_fingerprint, revision=revision, generated_hash=generated_hash
    )
    return artifact_fingerprint, revision, generated_hash


def _reader(paths: WorkingCopyPaths, lineage_reader, **kwargs) -> FilesystemFinalDocxSourceReader:
    kwargs.setdefault("max_docx_size_bytes", 10_000_000)
    kwargs.setdefault("max_binding_size_bytes", 1_000_000)
    return FilesystemFinalDocxSourceReader(paths, lineage_reader, **kwargs)


# -- owner-identity revalidation --------------------------------------------------------


def test_owner_fingerprint_mismatch_is_rejected_before_touching_the_lock(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    tampered = owner_key.model_copy(update={"owner_key_fingerprint": _sha("a-different-owner-entirely")})

    def boom(*args, **kwargs):
        raise AssertionError("PlatformOwnerLock must never be constructed on an owner fingerprint mismatch")

    monkeypatch.setattr(reader_module, "PlatformOwnerLock", boom)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(tampered, _sha("expected-fp"), 1)
    assert result.status == WorkingCopyReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH
    assert result.capture is None


# -- happy path: exact bytes, capture from verified lineage --------------------------


def test_success_returns_exact_current_docx_bytes(paths) -> None:
    owner_key = _owner_key()
    docx_bytes = b"the exact bytes the user may have edited in Word"
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key, docx_bytes=docx_bytes)

    lineage_result = _verified_lineage(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_revision=revision,
        generated_docx_content_hash=generated_hash,
        docx_bytes=b"the proposal's own historical bytes -- must never appear in the capture",
    )
    reader = _reader(paths, _FakeLineageReader(lineage_result))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.SUCCESS
    assert result.capture is not None
    assert result.capture.docx_bytes == docx_bytes


# -- VERIFIED lineage coherence: a VERIFIED result is never trusted at face
# -- value -- it must cohere exactly with the four values this reader itself
# -- queried the lineage reader with (fail-closed on any divergence,
# -- exercised through the real production read_finalization_source path).


def test_verified_lineage_coherent_with_query_succeeds(paths) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key)

    lineage_result = _verified_lineage(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_revision=revision,
        generated_docx_content_hash=generated_hash,
    )
    fake_lineage_reader = _FakeLineageReader(lineage_result)
    reader = _reader(paths, fake_lineage_reader)

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.SUCCESS
    assert result.capture is not None
    assert result.capture.source_proposal_artifact_fingerprint == artifact_fp
    assert result.capture.source_proposal_revision == revision
    assert result.capture.generated_proposal_sha256 == generated_hash
    assert fake_lineage_reader.calls == [
        (artifact_fp, owner_key.owner_key_fingerprint, revision, generated_hash)
    ]


def test_verified_lineage_with_different_artifact_fingerprint_is_rejected(paths) -> None:
    """A VERIFIED result whose ``artifact_fingerprint`` diverges from the
    one this reader queried the lineage reader with is a
    ``ProposalArtifactLineageReader`` contract violation (a real
    implementation can never return this), not a legitimate Proposal
    outcome -- it must never produce a capture."""

    owner_key = _owner_key()
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key)

    lineage_result = _verified_lineage(
        artifact_fingerprint=_sha("a-completely-different-artifact-fingerprint"),
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_revision=revision,
        generated_docx_content_hash=generated_hash,
    )
    reader = _reader(paths, _FakeLineageReader(lineage_result))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.STORAGE_UNAVAILABLE
    assert result.capture is None


def test_verified_lineage_with_different_owner_key_fingerprint_is_rejected(paths) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key)

    lineage_result = _verified_lineage(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=_sha("a-completely-different-owner-entirely"),
        proposal_revision=revision,
        generated_docx_content_hash=generated_hash,
    )
    reader = _reader(paths, _FakeLineageReader(lineage_result))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.SOURCE_PROPOSAL_OWNER_MISMATCH
    assert result.capture is None


def test_verified_lineage_with_different_proposal_revision_is_rejected(paths) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key)

    lineage_result = _verified_lineage(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_revision=revision + 1,
        generated_docx_content_hash=generated_hash,
    )
    reader = _reader(paths, _FakeLineageReader(lineage_result))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.SOURCE_PROPOSAL_REVISION_MISMATCH
    assert result.capture is None


def test_verified_lineage_with_different_generated_docx_content_hash_is_rejected(paths) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key)

    lineage_result = _verified_lineage(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_revision=revision,
        generated_docx_content_hash=_sha("a-completely-different-generated-hash"),
    )
    reader = _reader(paths, _FakeLineageReader(lineage_result))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.SOURCE_PROPOSAL_HASH_MISMATCH
    assert result.capture is None


def test_sidecar_hints_drive_the_lineage_lookup_not_caller_supplied_expected_values(paths) -> None:
    owner_key = _owner_key()
    sidecar_artifact_fp, sidecar_revision, sidecar_generated_hash = _seed_full_working_copy(paths, owner_key)
    fake_lineage_reader = _FakeLineageReader(_verified_lineage(owner_key_fingerprint=owner_key.owner_key_fingerprint))
    reader = _reader(paths, fake_lineage_reader)

    caller_expected_fp = _sha("a-completely-different-caller-expected-fingerprint")
    reader.read_finalization_source(owner_key, caller_expected_fp, 12345)

    (called_fp, called_owner_fp, called_revision, called_hash) = fake_lineage_reader.calls[0]
    assert called_fp == sidecar_artifact_fp
    assert called_revision == sidecar_revision
    assert called_hash == sidecar_generated_hash
    assert called_fp != caller_expected_fp


# -- capture only ever follows VERIFIED --------------------------------------------------


@pytest.mark.parametrize(
    "lineage_status",
    [s for s in ProposalArtifactLineageStatus if s != ProposalArtifactLineageStatus.VERIFIED],
)
def test_lineage_failure_mappings(paths, lineage_status: ProposalArtifactLineageStatus) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    reader = _reader(paths, _FakeLineageReader(_failed_lineage(lineage_status)))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.capture is None
    assert result.status == _LINEAGE_STATUS_TO_READ_STATUS[lineage_status]


def test_lineage_status_mapping_is_complete() -> None:
    non_verified = {s for s in ProposalArtifactLineageStatus if s != ProposalArtifactLineageStatus.VERIFIED}
    assert non_verified == set(_LINEAGE_STATUS_TO_READ_STATUS)


# -- current.docx observation failures ----------------------------------------------------


def test_current_docx_missing(paths) -> None:
    owner_key = _owner_key()
    paths.ensure_owner_dirs(owner_key.owner_key_fingerprint)
    _write_binding(
        paths, owner_key.owner_key_fingerprint, artifact_fingerprint=_sha("a"), revision=1, generated_hash=_sha("b")
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_MISSING
    assert result.capture is None


def test_current_docx_too_large(paths) -> None:
    owner_key = _owner_key()
    _seed_full_working_copy(paths, owner_key, docx_bytes=b"x" * 1000)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()), max_docx_size_bytes=100)

    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_TOO_LARGE
    assert result.capture is None


def _patch_stable_read(monkeypatch: pytest.MonkeyPatch, *, docx=None, binding=None):
    real = reader_module._stable_read_with_size_classification

    def fake(path: Path, *, max_size_bytes: int):
        if docx is not None and path.name == "current.docx":
            return docx
        if binding is not None and path.name == "binding.json":
            return binding
        return real(path, max_size_bytes=max_size_bytes)

    monkeypatch.setattr(reader_module, "_stable_read_with_size_classification", fake)


def test_current_docx_locked(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        docx=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_LOCKED), False),
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_LOCKED


def test_current_docx_unreadable(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        docx=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE), False),
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_UNREADABLE


def test_current_docx_changed_during_read(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        docx=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ), False),
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ


# -- binding.json observation failures ----------------------------------------------------


def test_binding_missing(paths) -> None:
    owner_key = _owner_key()
    _write_current_docx(paths, owner_key.owner_key_fingerprint, b"docx bytes with no sidecar at all")
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_MISSING
    assert result.capture is None


def test_binding_invalid_json(paths) -> None:
    owner_key = _owner_key()
    owner_fp = owner_key.owner_key_fingerprint
    _write_current_docx(paths, owner_fp, b"docx bytes with an invalid sidecar")
    paths.binding_path(owner_fp).write_bytes(b"{ not valid json at all")
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_INVALID
    assert result.capture is None


def test_binding_owner_mismatch(paths) -> None:
    owner_key = _owner_key()
    owner_fp = owner_key.owner_key_fingerprint
    _write_current_docx(paths, owner_fp, b"docx bytes bound to a different owner's sidecar")
    # The sidecar file lives at *this* owner's path, but its own internal
    # owner_key_fingerprint field names a different owner entirely.
    binding = WorkingCopyBinding(
        owner_key_fingerprint=_sha("a-completely-different-owner"),
        bound_proposal_artifact_fingerprint=_sha("a"),
        bound_proposal_revision=1,
        bound_generated_proposal_sha256=_sha("b"),
        last_known_tool_written_hash=_sha("b"),
    )
    paths.binding_path(owner_fp).write_bytes(serialize_working_copy_binding(binding))

    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))
    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_OWNER_MISMATCH
    assert result.capture is None


def test_binding_too_large(paths) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()), max_binding_size_bytes=10)

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_TOO_LARGE
    assert result.capture is None


def test_binding_changed_during_read(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        binding=(
            WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ),
            False,
        ),
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ


def test_binding_unreadable_maps_to_unreadable(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """``WORKING_COPY_BINDING_INVALID`` means only "successfully read, then
    failed to parse" -- a stable-read I/O failure on ``binding.json`` is
    never collapsed into it. An ``UNREADABLE`` stable read maps to the same
    generic ``WORKING_COPY_UNREADABLE`` a locked/unreadable ``current.docx``
    would report."""

    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        binding=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE), False),
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_UNREADABLE
    assert result.status != WorkingCopyReadStatus.WORKING_COPY_BINDING_INVALID


def test_binding_locked_maps_to_working_copy_locked(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sharing-lock stable-read outcome on ``binding.json`` maps to the
    same generic ``WORKING_COPY_LOCKED`` a locked ``current.docx`` would
    report -- never ``WORKING_COPY_BINDING_INVALID``, which is reserved for
    a successful read that fails to parse."""

    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        binding=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_LOCKED), False),
    )
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_LOCKED
    assert result.status != WorkingCopyReadStatus.WORKING_COPY_BINDING_INVALID


# -- lineage reader is never invoked on any binding.json I/O failure ------------------


class _BoomLineageReader:
    """A lineage reader that raises if ever called -- proves the lineage
    lookup never happens when binding.json itself could not be read, as
    opposed to being read successfully and failing to parse/bind."""

    def read_verified_proposal_artifact_lineage(self, **kwargs: object) -> ProposalArtifactLineageReadResult:
        raise AssertionError("lineage reader must never be called on a binding.json I/O failure")


def test_lineage_reader_never_called_when_binding_is_missing(paths) -> None:
    owner_key = _owner_key()
    _write_current_docx(paths, owner_key.owner_key_fingerprint, b"docx bytes with no sidecar at all")
    reader = _reader(paths, _BoomLineageReader())

    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_MISSING
    assert result.capture is None


def test_lineage_reader_never_called_when_binding_is_locked(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        binding=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_LOCKED), False),
    )
    reader = _reader(paths, _BoomLineageReader())

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_LOCKED
    assert result.capture is None


def test_lineage_reader_never_called_when_binding_is_unreadable(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        binding=(WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_UNREADABLE), False),
    )
    reader = _reader(paths, _BoomLineageReader())

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_UNREADABLE
    assert result.capture is None


def test_lineage_reader_never_called_when_binding_changed_during_read(
    paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    _patch_stable_read(
        monkeypatch,
        binding=(
            WorkingCopyStableReadResult(status=WorkingCopyStableReadStatus.WORKING_COPY_CHANGED_DURING_READ),
            False,
        ),
    )
    reader = _reader(paths, _BoomLineageReader())

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_CHANGED_DURING_READ
    assert result.capture is None


def test_lineage_reader_never_called_when_binding_is_too_large(paths) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    reader = _reader(paths, _BoomLineageReader(), max_binding_size_bytes=10)

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_TOO_LARGE
    assert result.capture is None


# -- owner-lock boundary -----------------------------------------------------------------


def _make_spy_lock_class():
    calls = {"acquire": 0, "release": 0, "instances": 0}

    class _SpyLock:
        def __init__(self, path, *, owner_key_fingerprint):
            calls["instances"] += 1
            self._real = PlatformOwnerLock(path, owner_key_fingerprint=owner_key_fingerprint)

        def acquire(self, *, timeout_seconds):
            calls["acquire"] += 1
            return self._real.acquire(timeout_seconds=timeout_seconds)

        def validate_active_token(self, token, *, owner_key_fingerprint):
            return self._real.validate_active_token(token, owner_key_fingerprint=owner_key_fingerprint)

        def release(self, token):
            calls["release"] += 1
            return self._real.release(token)

    return _SpyLock, calls


def test_exactly_one_acquire_and_release_on_success(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, generated_hash = _seed_full_working_copy(paths, owner_key)
    spy_lock_class, calls = _make_spy_lock_class()
    monkeypatch.setattr(reader_module, "PlatformOwnerLock", spy_lock_class)
    lineage_result = _verified_lineage(
        artifact_fingerprint=artifact_fp,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        proposal_revision=revision,
        generated_docx_content_hash=generated_hash,
    )
    reader = _reader(paths, _FakeLineageReader(lineage_result))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.SUCCESS
    assert calls == {"acquire": 1, "release": 1, "instances": 1}


def test_release_happens_in_finally_even_on_an_early_observation_failure(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    _write_current_docx(paths, owner_key.owner_key_fingerprint, b"docx with no sidecar -- early failure path")
    spy_lock_class, calls = _make_spy_lock_class()
    monkeypatch.setattr(reader_module, "PlatformOwnerLock", spy_lock_class)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, _sha("expected"), 1)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_BINDING_MISSING
    assert calls["acquire"] == 1
    assert calls["release"] == 1


def test_no_nested_lock_acquisition(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly one ``PlatformOwnerLock`` instance is ever constructed per
    ``read_finalization_source`` call -- there is no code path in this
    reader that acquires a second lock while the first is still held."""

    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)
    spy_lock_class, calls = _make_spy_lock_class()
    monkeypatch.setattr(reader_module, "PlatformOwnerLock", spy_lock_class)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert calls["instances"] == 1


def test_lock_path_identity_mismatch_is_reported_distinctly(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)

    class _MismatchLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def acquire(self, *, timeout_seconds: float):
            from app.services.cv_document_owner_lock import OwnerLockAcquireResult

            return OwnerLockAcquireResult(status=OwnerLockAcquireStatus.LOCK_PATH_IDENTITY_MISMATCH)

        def validate_active_token(self, *args, **kwargs) -> bool:
            return False

        def release(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(reader_module, "PlatformOwnerLock", _MismatchLock)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH


def test_lock_capability_invalid_is_reported_distinctly_from_path_identity_mismatch(
    paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)

    class _NeverValidatesLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def acquire(self, *, timeout_seconds: float):
            from app.services.cv_document_owner_lock import OwnerLockAcquireResult

            token = OwnerLockToken(lock_instance_id=1, acquisition_id=1, owner_key_fingerprint="x")
            return OwnerLockAcquireResult(status=OwnerLockAcquireStatus.ACQUIRED, token=token)

        def validate_active_token(self, *args, **kwargs) -> bool:
            return False

        def release(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(reader_module, "PlatformOwnerLock", _NeverValidatesLock)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.LOCK_CAPABILITY_INVALID
    assert result.status != WorkingCopyReadStatus.WORKING_COPY_LOCK_PATH_IDENTITY_MISMATCH


def test_owner_lock_timeout_maps_to_working_copy_locked(paths, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_key = _owner_key()
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key)

    class _TimeoutLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def acquire(self, *, timeout_seconds: float):
            from app.services.cv_document_owner_lock import OwnerLockAcquireResult

            return OwnerLockAcquireResult(status=OwnerLockAcquireStatus.OWNER_LOCK_TIMEOUT)

        def validate_active_token(self, *args, **kwargs) -> bool:
            return False

        def release(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr(reader_module, "PlatformOwnerLock", _TimeoutLock)
    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))

    result = reader.read_finalization_source(owner_key, artifact_fp, revision)
    assert result.status == WorkingCopyReadStatus.WORKING_COPY_LOCKED


# -- zero filesystem mutation ------------------------------------------------------------


def test_reader_never_mutates_current_docx_or_binding(paths) -> None:
    owner_key = _owner_key()
    docx_bytes = b"bytes that must remain byte-for-byte unchanged"
    artifact_fp, revision, _ = _seed_full_working_copy(paths, owner_key, docx_bytes=docx_bytes)
    owner_fp = owner_key.owner_key_fingerprint

    docx_before = paths.current_docx_path(owner_fp).read_bytes()
    binding_before = paths.binding_path(owner_fp).read_bytes()

    reader = _reader(paths, _FakeLineageReader(_verified_lineage()))
    reader.read_finalization_source(owner_key, artifact_fp, revision)
    # A second call, this time a failing one, must also mutate nothing.
    reader_failing = _reader(paths, _FakeLineageReader(_failed_lineage(ProposalArtifactLineageStatus.NOT_FOUND)))
    reader_failing.read_finalization_source(owner_key, artifact_fp, revision)

    assert paths.current_docx_path(owner_fp).read_bytes() == docx_before
    assert paths.binding_path(owner_fp).read_bytes() == binding_before
