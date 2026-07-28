"""Explicit Provenance Stage P6-B3b-B: unit tests for
``evaluate_final_confirmed_pdf_freshness`` -- informational-only Final
Confirmed PDF freshness evaluation.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    build_final_confirmed_pdf_artifact,
)
from app.schemas.cv_document_final_confirmed_pdf_freshness import (
    FinalConfirmedPdfFreshnessResult,
    FinalConfirmedPdfFreshnessStatus,
)
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_final_confirmed_pdf_freshness import evaluate_final_confirmed_pdf_freshness
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadResult,
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityReadResult,
    FinalConfirmedPdfAuthorityReadStatus,
    InMemoryFinalConfirmedPdfRepository,
)
from app.services.cv_document_owner_identity import build_owner_key


_POLICY = "freshness-policy-v1"


def _owner(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _identity(*, executable_sha256: str = "a" * 64):
    return build_converter_runtime_identity(
        implementation_id="libreoffice",
        implementation_version="7.6",
        executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256=executable_sha256,
        platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )


class _SpyRepository:
    """Wraps a real ``InMemoryFinalConfirmedPdfRepository`` and records
    every call -- used to assert freshness never calls ``save_artifact``/
    ``promote_current_authority`` (no converter call, no mutation)."""

    def __init__(self, inner: InMemoryFinalConfirmedPdfRepository | None = None) -> None:
        self.inner = inner or InMemoryFinalConfirmedPdfRepository()
        self.mutating_calls: list[str] = []
        self.override_read_current_authority: FinalConfirmedPdfAuthorityReadResult | None = None
        self.override_read_artifact: FinalConfirmedPdfArtifactReadResult | None = None

    def register_owner(self, owner_key) -> None:
        self.inner.register_owner(owner_key)

    def register_final_snapshot(self, snapshot) -> None:
        self.inner.register_final_snapshot(snapshot)

    def read_current_authority(self, owner_key):
        if self.override_read_current_authority is not None:
            return self.override_read_current_authority
        return self.inner.read_current_authority(owner_key)

    def read_artifact(self, artifact_fingerprint: str):
        if self.override_read_artifact is not None:
            return self.override_read_artifact
        return self.inner.read_artifact(artifact_fingerprint)

    def read_canonical_artifact_by_conversion_request_fingerprint(self, conversion_request_fingerprint: str):
        raise AssertionError("freshness must never call read_canonical_artifact_by_conversion_request_fingerprint")

    def retrieve_pdf_bytes_by_artifact(self, artifact_fingerprint: str):
        raise AssertionError("freshness must never call retrieve_pdf_bytes_by_artifact")

    def save_artifact(self, artifact, pdf_bytes: bytes):
        self.mutating_calls.append("save_artifact")
        raise AssertionError("freshness must never call save_artifact")

    def promote_current_authority(self, owner_key, target_artifact_fingerprint: str, expected_observation):
        self.mutating_calls.append("promote_current_authority")
        raise AssertionError("freshness must never call promote_current_authority")


def _promoted_env(*, snapshot_fp: str = "a" * 64, docx_sha: str = "b" * 64, policy: str = _POLICY):
    from app.schemas.cv_document_final_confirmed_pdf_source import FinalDocxSnapshotPdfSourceReadStatus  # noqa: F401

    owner_key = _owner()
    identity = _identity()
    repo = _SpyRepository()
    repo.register_owner(owner_key)

    from app.schemas.cv_document_final_snapshot import FINAL_SNAPSHOT_SCHEMA_VERSION, FinalDocxSnapshot

    final_docx_sha = hashlib.sha256(b"freshness docx bytes").hexdigest()
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="c" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha,
        final_docx_sha256=final_docx_sha,
        edited_by_user=False,
        finalization_policy_version="fin-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint="d" * 64,
    )
    repo.register_final_snapshot(snapshot)

    pdf_bytes = b"%PDF-1.4 freshness baseline"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=policy,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    save_result = repo.inner.save_artifact(artifact, pdf_bytes)
    assert save_result.status.value == "SAVED"
    cas_result = repo.inner.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert cas_result.status.value == "PROMOTED"

    return owner_key, snapshot, identity, artifact, repo


# -- FRESH -----------------------------------------------------------------------------


def test_fresh_when_all_four_dimensions_match() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        current_final_docx_sha256=snapshot.final_docx_sha256,
        current_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.FRESH
    assert result.artifact == artifact
    assert result.current_authority_observation.current_artifact_fingerprint == artifact.artifact_fingerprint
    assert repo.mutating_calls == []


# -- STALE_SOURCE_SNAPSHOT --------------------------------------------------------------


def test_stale_source_snapshot_on_snapshot_fingerprint_mismatch() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint="f" * 64,
        current_final_docx_sha256=snapshot.final_docx_sha256,
        current_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.STALE_SOURCE_SNAPSHOT
    assert result.artifact == artifact


def test_stale_source_snapshot_on_docx_sha_mismatch() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        current_final_docx_sha256="e" * 64,
        current_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.STALE_SOURCE_SNAPSHOT


# -- STALE_RUNTIME_IDENTITY --------------------------------------------------------------


def test_stale_runtime_identity_on_fingerprint_mismatch() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        current_final_docx_sha256=snapshot.final_docx_sha256,
        current_runtime_identity_fingerprint="1" * 64,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.STALE_RUNTIME_IDENTITY


# -- STALE_CONVERSION_POLICY --------------------------------------------------------------


def test_stale_conversion_policy_on_policy_mismatch() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        current_final_docx_sha256=snapshot.final_docx_sha256,
        current_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        current_conversion_policy_version="a-newer-policy-version",
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.STALE_CONVERSION_POLICY


# -- staleness precedence: source snapshot checked before identity/policy -------------------


def test_stale_precedence_source_snapshot_wins_over_other_dimensions() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint="f" * 64,
        current_final_docx_sha256="e" * 64,
        current_runtime_identity_fingerprint="1" * 64,
        current_conversion_policy_version="a-newer-policy-version",
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.STALE_SOURCE_SNAPSHOT


# -- no authority -> NOT_FOUND ---------------------------------------------------------------


def test_no_authority_is_not_found() -> None:
    owner_key = _owner()
    repo = _SpyRepository()
    repo.register_owner(owner_key)
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint="a" * 64,
        current_final_docx_sha256="b" * 64,
        current_runtime_identity_fingerprint="c" * 64,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.NOT_FOUND
    assert result.artifact is None
    assert result.current_authority_observation is None
    assert repo.mutating_calls == []


# -- LEGACY authority -> NOT_FOUND for this line ----------------------------------------------


def test_legacy_authority_is_not_found() -> None:
    owner_key = _owner()
    repo = _SpyRepository()
    repo.register_owner(owner_key)
    repo.override_read_current_authority = FinalConfirmedPdfAuthorityReadResult(
        status=FinalConfirmedPdfAuthorityReadStatus.FOUND,
        observation=ConfirmedPdfCurrentAuthorityObservation(
            exists=True,
            lineage_kind=ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX,
            current_artifact_fingerprint="a" * 64,
            slot_version=1,
        ),
    )
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint="a" * 64,
        current_final_docx_sha256="b" * 64,
        current_runtime_identity_fingerprint="c" * 64,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == FinalConfirmedPdfFreshnessStatus.NOT_FOUND


# -- metadata conflict / storage unavailable (authority read) -----------------------------------


@pytest.mark.parametrize(
    "authority_status,expected",
    [
        (FinalConfirmedPdfAuthorityReadStatus.STORAGE_METADATA_CONFLICT, FinalConfirmedPdfFreshnessStatus.STORAGE_METADATA_CONFLICT),
        (FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE, FinalConfirmedPdfFreshnessStatus.STORAGE_UNAVAILABLE),
    ],
)
def test_authority_read_failure_total_mapping(authority_status, expected) -> None:
    owner_key = _owner()
    repo = _SpyRepository()
    repo.register_owner(owner_key)
    repo.override_read_current_authority = FinalConfirmedPdfAuthorityReadResult(status=authority_status)
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint="a" * 64,
        current_final_docx_sha256="b" * 64,
        current_runtime_identity_fingerprint="c" * 64,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == expected


# -- metadata conflict / storage unavailable (artifact read after FINAL authority) ----------------


@pytest.mark.parametrize(
    "artifact_status,expected",
    [
        (FinalConfirmedPdfArtifactReadStatus.STORAGE_METADATA_CONFLICT, FinalConfirmedPdfFreshnessStatus.STORAGE_METADATA_CONFLICT),
        (FinalConfirmedPdfArtifactReadStatus.STORAGE_UNAVAILABLE, FinalConfirmedPdfFreshnessStatus.STORAGE_UNAVAILABLE),
        (FinalConfirmedPdfArtifactReadStatus.NOT_FOUND, FinalConfirmedPdfFreshnessStatus.STORAGE_METADATA_CONFLICT),
    ],
)
def test_artifact_read_failure_total_mapping(artifact_status, expected) -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    repo.override_read_artifact = FinalConfirmedPdfArtifactReadResult(status=artifact_status)
    result = evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        current_final_docx_sha256=snapshot.final_docx_sha256,
        current_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert result.status == expected


# -- no mutation / no converter call --------------------------------------------------------------


def test_freshness_never_mutates_and_never_calls_converter_or_replay_paths() -> None:
    owner_key, snapshot, identity, artifact, repo = _promoted_env()
    evaluate_final_confirmed_pdf_freshness(
        owner_key=owner_key,
        current_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        current_final_docx_sha256=snapshot.final_docx_sha256,
        current_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        current_conversion_policy_version=_POLICY,
        repository=repo,
    )
    assert repo.mutating_calls == []
    # `evaluate_final_confirmed_pdf_freshness` takes no `converter` parameter
    # at all -- it is structurally impossible for it to invoke one.
    import inspect

    sig = inspect.signature(evaluate_final_confirmed_pdf_freshness)
    assert "converter" not in sig.parameters


# -- result validator ------------------------------------------------------------------------------


def test_result_validator_forbids_payload_on_not_found() -> None:
    with pytest.raises(Exception):
        FinalConfirmedPdfFreshnessResult(
            status=FinalConfirmedPdfFreshnessStatus.NOT_FOUND,
            current_authority_observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
        )
