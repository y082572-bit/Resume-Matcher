"""Explicit Provenance Stage P6-B3b-A: domain-model and in-memory-repository
unit tests for ``FinalConfirmedPdfArtifact``/``FinalConfirmedPdfRepository``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    FinalConfirmedPdfArtifact,
    RUNTIME_IDENTITY_JSON_MAX_BYTES,
    build_final_confirmed_pdf_artifact,
    compute_canonical_runtime_identity_json,
    compute_final_confirmed_pdf_artifact_fingerprint,
    compute_final_confirmed_pdf_request_fingerprint,
)
from app.schemas.cv_document_final_snapshot import FinalDocxSnapshot, FINAL_SNAPSHOT_SCHEMA_VERSION
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfArtifactReadResult,
    FinalConfirmedPdfArtifactReadStatus,
    FinalConfirmedPdfAuthorityCasResult,
    FinalConfirmedPdfAuthorityCasStatus,
    FinalConfirmedPdfAuthorityReadResult,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfCanonicalReadResult,
    FinalConfirmedPdfCanonicalReadStatus,
    FinalConfirmedPdfRepository,
    FinalConfirmedPdfRetrievalResult,
    FinalConfirmedPdfRetrievalStatus,
    FinalConfirmedPdfSaveResult,
    FinalConfirmedPdfSaveStatus,
    InMemoryFinalConfirmedPdfRepository,
)
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint


def _owner(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _runtime_identity(**overrides):
    defaults = dict(
        implementation_id="libreoffice",
        implementation_version="7.6",
        executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256="a" * 64,
        platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )
    defaults.update(overrides)
    return build_converter_runtime_identity(**defaults)


def _snapshot(owner_key, *, final_docx_sha256: str = "c" * 64):
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint="a" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha256,
        final_docx_sha256=final_docx_sha256,
        finalization_policy_version="fin-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="a" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_docx_sha256,
        final_docx_sha256=final_docx_sha256,
        edited_by_user=False,
        finalization_policy_version="fin-v1",
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )


def _artifact(owner_key, *, pdf_sha256: str = "d" * 64, snapshot_fingerprint: str = "b" * 64, runtime_identity=None):
    return build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot_fingerprint,
        source_final_docx_sha256="c" * 64,
        runtime_identity=runtime_identity or _runtime_identity(),
        conversion_policy_version="policy-v1",
        pdf_sha256=pdf_sha256,
    )


# -- fingerprint determinism / sensitivity ---------------------------------------


def test_request_fingerprint_deterministic() -> None:
    kwargs = dict(
        owner_key_fingerprint="a" * 64,
        source_final_snapshot_fingerprint="b" * 64,
        source_final_docx_sha256="c" * 64,
        runtime_identity_fingerprint="d" * 64,
        conversion_policy_version="policy-v1",
    )
    assert compute_final_confirmed_pdf_request_fingerprint(**kwargs) == compute_final_confirmed_pdf_request_fingerprint(
        **kwargs
    )


@pytest.mark.parametrize(
    "field",
    ["owner_key_fingerprint", "source_final_snapshot_fingerprint", "source_final_docx_sha256", "runtime_identity_fingerprint"],
)
def test_request_fingerprint_changes_for_each_input(field: str) -> None:
    base = dict(
        owner_key_fingerprint="a" * 64,
        source_final_snapshot_fingerprint="b" * 64,
        source_final_docx_sha256="c" * 64,
        runtime_identity_fingerprint="d" * 64,
        conversion_policy_version="policy-v1",
    )
    changed = dict(base)
    changed[field] = "f" * 64
    assert compute_final_confirmed_pdf_request_fingerprint(**base) != compute_final_confirmed_pdf_request_fingerprint(
        **changed
    )


def test_request_fingerprint_changes_for_policy_version() -> None:
    base = dict(
        owner_key_fingerprint="a" * 64,
        source_final_snapshot_fingerprint="b" * 64,
        source_final_docx_sha256="c" * 64,
        runtime_identity_fingerprint="d" * 64,
        conversion_policy_version="policy-v1",
    )
    changed = dict(base, conversion_policy_version="policy-v2")
    assert compute_final_confirmed_pdf_request_fingerprint(**base) != compute_final_confirmed_pdf_request_fingerprint(
        **changed
    )


def test_artifact_fingerprint_deterministic() -> None:
    kwargs = dict(conversion_request_fingerprint="a" * 64, pdf_sha256="b" * 64)
    assert compute_final_confirmed_pdf_artifact_fingerprint(**kwargs) == compute_final_confirmed_pdf_artifact_fingerprint(
        **kwargs
    )


def test_artifact_fingerprint_changes_for_pdf_sha256() -> None:
    a = compute_final_confirmed_pdf_artifact_fingerprint(conversion_request_fingerprint="a" * 64, pdf_sha256="b" * 64)
    b = compute_final_confirmed_pdf_artifact_fingerprint(conversion_request_fingerprint="a" * 64, pdf_sha256="c" * 64)
    assert a != b


# -- runtime identity binding / canonical JSON -------------------------------------


def test_runtime_identity_fingerprint_binding_enforced() -> None:
    owner_key = _owner()
    artifact = _artifact(owner_key)
    tampered = artifact.model_dump()
    tampered["runtime_identity_fingerprint"] = "f" * 64
    with pytest.raises(ValidationError):
        FinalConfirmedPdfArtifact.model_validate(tampered)


def test_canonical_runtime_json_round_trip() -> None:
    rt = _runtime_identity()
    json_text = compute_canonical_runtime_identity_json(rt)
    from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity

    reparsed = ConverterRuntimeIdentity.model_validate_json(json_text)
    assert reparsed == rt
    assert compute_canonical_runtime_identity_json(reparsed) == json_text


def test_canonical_json_noncanonical_string_rejected() -> None:
    rt = _runtime_identity()
    canonical = compute_canonical_runtime_identity_json(rt)
    noncanonical = canonical.replace('"implementation_id"', '  "implementation_id"')
    assert noncanonical != canonical


def test_canonical_json_over_8192_bytes_rejected() -> None:
    # A 3-byte-UTF-8 CJK codepoint (unaffected by canonical_json_bytes' own
    # NFKC normalization, unlike a compatibility-decomposed codepoint)
    # repeated near the field's own character-count cap (4096) pushes the
    # canonical JSON well past 8192 *bytes* even though it stays within
    # every individual field's character-length cap.
    rt = _runtime_identity(executable_canonical_path="字" * 4000)
    assert len(compute_canonical_runtime_identity_json(rt).encode("utf-8")) > RUNTIME_IDENTITY_JSON_MAX_BYTES
    with pytest.raises(ValidationError):
        _artifact(_owner(), runtime_identity=rt)


# -- immutability / result invariants ------------------------------------------------


def test_domain_models_are_frozen() -> None:
    owner_key = _owner()
    artifact = _artifact(owner_key)
    with pytest.raises(ValidationError):
        artifact.pdf_sha256 = "e" * 64  # type: ignore[misc]


def test_save_result_invariants() -> None:
    with pytest.raises(ValidationError):
        FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.SAVED)  # missing artifact
    with pytest.raises(ValidationError):
        FinalConfirmedPdfSaveResult(status=FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND, artifact=_artifact(_owner()))


def test_authority_observation_invariants() -> None:
    with pytest.raises(ValidationError):
        ConfirmedPdfCurrentAuthorityObservation(exists=True)
    with pytest.raises(ValidationError):
        ConfirmedPdfCurrentAuthorityObservation(
            exists=False, lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT
        )
    with pytest.raises(ValidationError):
        ConfirmedPdfCurrentAuthorityObservation(
            exists=True,
            lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
            current_artifact_fingerprint="a" * 64,
            slot_version=0,
        )


def test_authority_read_result_invariants() -> None:
    with pytest.raises(ValidationError):
        FinalConfirmedPdfAuthorityReadResult(status=FinalConfirmedPdfAuthorityReadStatus.FOUND)
    with pytest.raises(ValidationError):
        FinalConfirmedPdfAuthorityReadResult(
            status=FinalConfirmedPdfAuthorityReadStatus.STORAGE_UNAVAILABLE,
            observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
        )


def test_artifact_read_result_invariants() -> None:
    with pytest.raises(ValidationError):
        FinalConfirmedPdfArtifactReadResult(status=FinalConfirmedPdfArtifactReadStatus.FOUND)
    with pytest.raises(ValidationError):
        FinalConfirmedPdfArtifactReadResult(
            status=FinalConfirmedPdfArtifactReadStatus.NOT_FOUND, artifact=_artifact(_owner())
        )


def test_canonical_read_result_invariants() -> None:
    with pytest.raises(ValidationError):
        FinalConfirmedPdfCanonicalReadResult(status=FinalConfirmedPdfCanonicalReadStatus.FOUND)
    with pytest.raises(ValidationError):
        FinalConfirmedPdfCanonicalReadResult(
            status=FinalConfirmedPdfCanonicalReadStatus.STORAGE_METADATA_CONFLICT, artifact=_artifact(_owner())
        )


def test_retrieval_result_invariants() -> None:
    with pytest.raises(ValidationError):
        FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.OK)
    with pytest.raises(ValidationError):
        FinalConfirmedPdfRetrievalResult(status=FinalConfirmedPdfRetrievalStatus.ARTIFACT_NOT_FOUND, pdf_bytes=b"x")


def test_cas_result_invariants() -> None:
    with pytest.raises(ValidationError):
        FinalConfirmedPdfAuthorityCasResult(status=FinalConfirmedPdfAuthorityCasStatus.PROMOTED)
    with pytest.raises(ValidationError):
        FinalConfirmedPdfAuthorityCasResult(
            status=FinalConfirmedPdfAuthorityCasStatus.PROMOTED,
            current_observation=ConfirmedPdfCurrentAuthorityObservation(
                exists=True,
                lineage_kind=ConfirmedPdfLineageKind.LEGACY_VALIDATED_DOCX,
                current_artifact_fingerprint="a" * 64,
                slot_version=1,
            ),
        )
    with pytest.raises(ValidationError):
        FinalConfirmedPdfAuthorityCasResult(status=FinalConfirmedPdfAuthorityCasStatus.OWNER_NOT_FOUND, current_observation=ConfirmedPdfCurrentAuthorityObservation(exists=False))


# -- in-memory repository ------------------------------------------------------------


def _seeded_repo():
    repo = InMemoryFinalConfirmedPdfRepository()
    owner_key = _owner()
    snapshot = _snapshot(owner_key)
    repo.register_final_snapshot(snapshot)
    return repo, owner_key, snapshot


def test_repository_is_a_runtime_checkable_protocol_instance() -> None:
    assert isinstance(InMemoryFinalConfirmedPdfRepository(), FinalConfirmedPdfRepository)


def test_in_memory_first_commit_wins() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    rt = _runtime_identity()
    artifact_a = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=rt,
        conversion_policy_version="policy-v1",
        pdf_sha256="1" * 64,
    )
    artifact_b = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=rt,
        conversion_policy_version="policy-v1",
        pdf_sha256="2" * 64,
    )
    assert artifact_a.conversion_request_fingerprint == artifact_b.conversion_request_fingerprint

    first = repo.save_artifact(artifact_a, b"pdf bytes a")
    second = repo.save_artifact(artifact_b, b"pdf bytes b")
    assert first.status == FinalConfirmedPdfSaveStatus.SAVED
    assert second.status == FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL
    assert second.artifact == first.artifact


def test_in_memory_identical_replay_is_idempotent() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=_runtime_identity(),
        conversion_policy_version="policy-v1",
        pdf_sha256="3" * 64,
    )
    first = repo.save_artifact(artifact, b"identical bytes")
    second = repo.save_artifact(artifact, b"identical bytes")
    assert first.status == FinalConfirmedPdfSaveStatus.SAVED
    assert second.status == FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL


def test_in_memory_nondeterministic_output_race() -> None:
    """Two concurrent conversions of the *same* request producing different
    (nondeterministic) PDF bytes must never both be canonical."""

    repo, owner_key, snapshot = _seeded_repo()
    rt = _runtime_identity()
    losing = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=rt,
        conversion_policy_version="policy-v1",
        pdf_sha256="4" * 64,
    )
    winning = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=rt,
        conversion_policy_version="policy-v1",
        pdf_sha256="5" * 64,
    )
    repo.save_artifact(winning, b"winning bytes")
    loser_result = repo.save_artifact(losing, b"losing bytes")
    assert loser_result.status == FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL
    assert loser_result.artifact.pdf_sha256 == winning.pdf_sha256


def test_in_memory_absent_row_cas() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    artifact = _artifact(owner_key, snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    repo.save_artifact(artifact, b"cas absent row bytes")
    result = repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert result.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED
    assert result.current_observation.slot_version == 1


def test_in_memory_existing_row_cas() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    artifact = _artifact(owner_key, snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    repo.save_artifact(artifact, b"cas existing row bytes")
    first = repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    second = repo.promote_current_authority(owner_key, artifact.artifact_fingerprint, first.current_observation)
    assert second.status == FinalConfirmedPdfAuthorityCasStatus.PROMOTED
    assert second.current_observation.slot_version == 2


def test_in_memory_stale_cas_returns_fresh_observation() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    artifact = _artifact(owner_key, snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    repo.save_artifact(artifact, b"cas stale bytes")
    repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    stale = repo.promote_current_authority(
        owner_key, artifact.artifact_fingerprint, ConfirmedPdfCurrentAuthorityObservation(exists=False)
    )
    assert stale.status == FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION
    assert stale.current_observation.exists is True
    assert stale.current_observation.slot_version == 1


def test_defensive_copy_ingress_bytes() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    artifact = _artifact(owner_key, snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    mutable = bytearray(b"defensive ingress bytes")
    repo.save_artifact(artifact, bytes(mutable))
    mutable[0:1] = b"X"
    retrieved = repo.retrieve_pdf_bytes_by_artifact(artifact.artifact_fingerprint)
    assert retrieved.pdf_bytes == b"defensive ingress bytes"


def test_defensive_copy_egress() -> None:
    repo, owner_key, snapshot = _seeded_repo()
    artifact = _artifact(owner_key, snapshot_fingerprint=snapshot.final_snapshot_fingerprint)
    repo.save_artifact(artifact, b"defensive egress bytes")
    first = repo.read_artifact(artifact.artifact_fingerprint)
    second = repo.read_artifact(artifact.artifact_fingerprint)
    assert first.artifact == second.artifact
    assert first.artifact is not second.artifact
