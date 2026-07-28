"""Explicit Provenance Stage P6-B3b-B: unit tests for
``generate_final_confirmed_pdf`` -- the Final Confirmed PDF orchestration
composition root.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.schemas.cv_document_final_confirmed_pdf import (
    ConfirmedPdfCurrentAuthorityObservation,
    ConfirmedPdfLineageKind,
    compute_final_confirmed_pdf_request_fingerprint,
)
from app.schemas.cv_document_final_confirmed_pdf_orchestration import (
    FinalConfirmedPdfGenerationResult,
    FinalConfirmedPdfGenerationStatus,
)
from app.schemas.cv_document_final_confirmed_pdf_source import (
    FinalDocxSnapshotPdfSourceReadResult,
    FinalDocxSnapshotPdfSourceReadStatus,
)
from app.schemas.cv_document_final_snapshot import FINAL_SNAPSHOT_SCHEMA_VERSION, FinalDocxSnapshot
from app.schemas.cv_document_pdf_conversion import DocxToPdfConversionResult, DocxToPdfConversionStatus
from app.schemas.cv_document_pdf_conversion_runtime import ConverterRuntimeIdentity, build_converter_runtime_identity
import app.services.cv_document_final_confirmed_pdf_orchestrator as orchestrator_module
from app.services.cv_document_final_confirmed_pdf_orchestrator import generate_final_confirmed_pdf
from app.services.cv_document_final_confirmed_pdf_repository_protocol import (
    FinalConfirmedPdfAuthorityCasResult,
    FinalConfirmedPdfAuthorityCasStatus,
    FinalConfirmedPdfAuthorityReadResult,
    FinalConfirmedPdfAuthorityReadStatus,
    FinalConfirmedPdfCanonicalReadResult,
    FinalConfirmedPdfCanonicalReadStatus,
    FinalConfirmedPdfRetrievalResult,
    FinalConfirmedPdfRetrievalStatus,
    FinalConfirmedPdfSaveResult,
    FinalConfirmedPdfSaveStatus,
    InMemoryFinalConfirmedPdfRepository,
)
from app.services.cv_document_final_docx_snapshot_pdf_source_reader import FinalDocxSnapshotPdfSourceReader
from app.services.cv_document_final_snapshot_repository import (
    InMemoryFinalDocxSnapshotRepository,
    compute_final_snapshot_fingerprint,
)
from app.services.cv_document_owner_identity import build_owner_key


_POLICY = "orchestrator-policy-v1"


# -- helpers ------------------------------------------------------------------------


def _owner(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _snapshot(owner_key, docx_bytes: bytes, *, policy: str = "fin-v1") -> FinalDocxSnapshot:
    final_hash = hashlib.sha256(docx_bytes).hexdigest()
    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint="a" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_hash,
        final_docx_sha256=final_hash,
        finalization_policy_version=policy,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    return FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint="a" * 64,
        source_proposal_revision=1,
        generated_proposal_sha256=final_hash,
        final_docx_sha256=final_hash,
        edited_by_user=False,
        finalization_policy_version=policy,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )


def _runtime_identity(*, executable_sha256: str = "a" * 64) -> ConverterRuntimeIdentity:
    return build_converter_runtime_identity(
        implementation_id="libreoffice",
        implementation_version="7.6",
        executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256=executable_sha256,
        platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )


def _source_reader(owner_key, snapshot: FinalDocxSnapshot, docx_bytes: bytes) -> FinalDocxSnapshotPdfSourceReader:
    repo = InMemoryFinalDocxSnapshotRepository()
    repo.save_final_docx_snapshot(snapshot, docx_bytes)
    return FinalDocxSnapshotPdfSourceReader(repo)


class _FixedSourceReader:
    def __init__(self, result: FinalDocxSnapshotPdfSourceReadResult) -> None:
        self._result = result

    def read_source(self, *, owner_key, final_snapshot_fingerprint) -> FinalDocxSnapshotPdfSourceReadResult:
        return self._result


class _FakeConverter:
    """A test double for ``DocxToPdfConverter``. Every call is recorded in
    ``self.calls`` so tests can assert the orchestrator passed the correct
    verified arguments through, never a caller-supplied claim."""

    def __init__(self, *, identity=None, identity_raises: bool = False, result_factory=None) -> None:
        self._identity = identity
        self._identity_raises = identity_raises
        self._result_factory = result_factory
        self.calls: list[dict] = []

    @property
    def runtime_identity(self):
        if self._identity_raises:
            raise RuntimeError("simulated runtime identity probe failure")
        return self._identity

    def convert_docx_to_pdf(
        self, *, docx_bytes: bytes, source_snapshot_fingerprint: str, source_docx_sha256: str, conversion_policy_version: str
    ):
        self.calls.append(
            dict(
                docx_bytes=docx_bytes,
                source_snapshot_fingerprint=source_snapshot_fingerprint,
                source_docx_sha256=source_docx_sha256,
                conversion_policy_version=conversion_policy_version,
            )
        )
        if self._result_factory is not None:
            return self._result_factory(
                docx_bytes=docx_bytes,
                source_snapshot_fingerprint=source_snapshot_fingerprint,
                source_docx_sha256=source_docx_sha256,
                conversion_policy_version=conversion_policy_version,
            )
        pdf_bytes = b"%PDF-1.4 fake pdf " + source_docx_sha256.encode()
        return DocxToPdfConversionResult(
            status=DocxToPdfConversionStatus.SUCCEEDED,
            pdf_bytes=pdf_bytes,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            runtime_identity=self._identity,
            conversion_policy_version=conversion_policy_version,
        )


def _success_converter(identity: ConverterRuntimeIdentity) -> _FakeConverter:
    return _FakeConverter(identity=identity)


class _ControllableRepository:
    """Wraps a real ``InMemoryFinalConfirmedPdfRepository`` so tests can
    force a specific closed status from any single method for exactly the
    next call, while every other call delegates to real, correct behavior.
    Also counts calls to ``read_current_authority``/
    ``promote_current_authority`` so tests can assert the orchestrator never
    re-reads authority and never retries a CAS write."""

    def __init__(self) -> None:
        self.inner = InMemoryFinalConfirmedPdfRepository()
        self.override_read_current_authority: FinalConfirmedPdfAuthorityReadResult | None = None
        self.override_read_canonical: FinalConfirmedPdfCanonicalReadResult | None = None
        self.override_retrieve: FinalConfirmedPdfRetrievalResult | None = None
        self.override_save: FinalConfirmedPdfSaveResult | None = None
        self.override_cas: FinalConfirmedPdfAuthorityCasResult | None = None
        self.read_current_authority_calls = 0
        self.promote_current_authority_calls = 0
        self.save_calls: list[tuple] = []

    def register_owner(self, owner_key) -> None:
        self.inner.register_owner(owner_key)

    def register_final_snapshot(self, snapshot: FinalDocxSnapshot) -> None:
        self.inner.register_final_snapshot(snapshot)

    def read_current_authority(self, owner_key):
        self.read_current_authority_calls += 1
        if self.override_read_current_authority is not None:
            return self.override_read_current_authority
        return self.inner.read_current_authority(owner_key)

    def read_artifact(self, artifact_fingerprint: str):
        return self.inner.read_artifact(artifact_fingerprint)

    def read_canonical_artifact_by_conversion_request_fingerprint(self, conversion_request_fingerprint: str):
        if self.override_read_canonical is not None:
            return self.override_read_canonical
        return self.inner.read_canonical_artifact_by_conversion_request_fingerprint(conversion_request_fingerprint)

    def retrieve_pdf_bytes_by_artifact(self, artifact_fingerprint: str):
        if self.override_retrieve is not None:
            return self.override_retrieve
        return self.inner.retrieve_pdf_bytes_by_artifact(artifact_fingerprint)

    def save_artifact(self, artifact, pdf_bytes: bytes):
        self.save_calls.append((artifact, pdf_bytes))
        if self.override_save is not None:
            return self.override_save
        return self.inner.save_artifact(artifact, pdf_bytes)

    def promote_current_authority(self, owner_key, target_artifact_fingerprint: str, expected_observation):
        self.promote_current_authority_calls += 1
        if self.override_cas is not None:
            return self.override_cas
        return self.inner.promote_current_authority(owner_key, target_artifact_fingerprint, expected_observation)


def _seeded_env(*, docx_bytes: bytes = b"orchestrator docx bytes", policy: str = _POLICY):
    owner_key = _owner()
    snapshot = _snapshot(owner_key, docx_bytes, policy="fin-v1")
    reader = _source_reader(owner_key, snapshot, docx_bytes)
    identity = _runtime_identity()
    converter = _success_converter(identity)
    repo = _ControllableRepository()
    repo.register_final_snapshot(snapshot)
    return owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy


def _call(owner_key, snapshot, reader, converter, repo, policy) -> FinalConfirmedPdfGenerationResult:
    return generate_final_confirmed_pdf(
        owner_key=owner_key,
        final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        conversion_policy_version=policy,
        source_reader=reader,
        converter=converter,
        repository=repo,
    )


# -- 1-5: exact public signature / no forbidden caller-supplied fields -------------


def test_public_signature_is_keyword_only_and_forbids_internal_fields() -> None:
    sig = inspect.signature(generate_final_confirmed_pdf)
    params = sig.parameters
    assert set(params) == {
        "owner_key",
        "final_snapshot_fingerprint",
        "conversion_policy_version",
        "source_reader",
        "converter",
        "repository",
    }
    for name, param in params.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, name

    forbidden = {
        "docx_bytes",
        "pdf_bytes",
        "source_docx_sha256",
        "pdf_sha256",
        "runtime_identity",
        "conversion_request_fingerprint",
        "artifact_fingerprint",
        "current_authority_observation",
        "expected_slot_version",
        "executable_path",
        "input_path",
        "output_path",
    }
    assert forbidden.isdisjoint(params)


# -- 6-7: source-reader total mapping ------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [s for s in FinalDocxSnapshotPdfSourceReadStatus if s != FinalDocxSnapshotPdfSourceReadStatus.VERIFIED],
)
def test_source_reader_failure_total_mapping(status: FinalDocxSnapshotPdfSourceReadStatus) -> None:
    owner_key = _owner()
    reader = _FixedSourceReader(FinalDocxSnapshotPdfSourceReadResult(status=status))
    repo = _ControllableRepository()
    converter = _success_converter(_runtime_identity())

    result = generate_final_confirmed_pdf(
        owner_key=owner_key,
        final_snapshot_fingerprint="a" * 64,
        conversion_policy_version=_POLICY,
        source_reader=reader,
        converter=converter,
        repository=repo,
    )

    expected = {
        FinalDocxSnapshotPdfSourceReadStatus.OWNER_KEY_FINGERPRINT_MISMATCH: FinalConfirmedPdfGenerationStatus.OWNER_KEY_FINGERPRINT_MISMATCH,
        FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_NOT_FOUND: FinalConfirmedPdfGenerationStatus.FINAL_SNAPSHOT_NOT_FOUND,
        FinalDocxSnapshotPdfSourceReadStatus.FINAL_SNAPSHOT_OWNER_MISMATCH: FinalConfirmedPdfGenerationStatus.FINAL_SNAPSHOT_OWNER_MISMATCH,
        FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_BYTES_NOT_FOUND: FinalConfirmedPdfGenerationStatus.FINAL_DOCX_BYTES_NOT_FOUND,
        FinalDocxSnapshotPdfSourceReadStatus.FINAL_DOCX_HASH_MISMATCH: FinalConfirmedPdfGenerationStatus.FINAL_DOCX_HASH_MISMATCH,
        FinalDocxSnapshotPdfSourceReadStatus.STORAGE_UNAVAILABLE: FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE,
    }
    assert result.status == expected[status]
    assert converter.calls == []
    assert repo.read_current_authority_calls == 0


# -- 8-10: runtime identity ----------------------------------------------------------


def test_runtime_identity_none_is_converter_unavailable() -> None:
    owner_key, snapshot, docx_bytes, reader, _identity, _converter, repo, policy = _seeded_env()
    converter = _FakeConverter(identity=None)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONVERTER_UNAVAILABLE
    assert converter.calls == []


def test_runtime_identity_property_raising_is_contract_violation() -> None:
    owner_key, snapshot, docx_bytes, reader, _identity, _converter, repo, policy = _seeded_env()
    converter = _FakeConverter(identity_raises=True)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION
    assert converter.calls == []


def test_runtime_identity_wrong_type_is_contract_violation() -> None:
    owner_key, snapshot, docx_bytes, reader, _identity, _converter, repo, policy = _seeded_env()
    converter = _FakeConverter(identity="not-a-runtime-identity")
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION
    assert converter.calls == []


# -- 11-19: replay lookup + replay retrieval total mapping ---------------------------


def test_replay_found_skips_converter_and_returns_replayed_and_promoted() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()

    # Seed a canonical artifact directly (bypassing the orchestrator) so its
    # conversion_request_fingerprint is already indexed, but current
    # authority is never promoted -- this is exactly what a prior partial
    # run (or a concurrent winner) leaves behind.
    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    pdf_bytes = b"%PDF-1.4 preexisting pdf"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=policy,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.register_owner(owner_key)
    save_result = repo.save_artifact(artifact, pdf_bytes)
    assert save_result.status == FinalConfirmedPdfSaveStatus.SAVED

    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    assert result.status == FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED
    assert converter.calls == []
    assert result.replay_used is True
    assert result.canonical_artifact_reused is True
    assert result.conversion_performed is False
    assert result.current_authority_promoted is True
    assert result.pdf_bytes == pdf_bytes
    assert result.artifact == artifact


@pytest.mark.parametrize("status", list(FinalConfirmedPdfRetrievalStatus))
def test_replay_found_retrieval_total_mapping(status: FinalConfirmedPdfRetrievalStatus) -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()

    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    pdf_bytes = b"%PDF-1.4 replay retrieval pdf"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=policy,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    repo.register_owner(owner_key)
    repo.save_artifact(artifact, pdf_bytes)

    if status == FinalConfirmedPdfRetrievalStatus.OK:
        result = _call(owner_key, snapshot, reader, converter, repo, policy)
        assert result.status == FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED
        return

    repo.override_retrieve = FinalConfirmedPdfRetrievalResult(status=status)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    expected = {
        FinalConfirmedPdfRetrievalStatus.ARTIFACT_NOT_FOUND: FinalConfirmedPdfGenerationStatus.PDF_ARTIFACT_NOT_FOUND,
        FinalConfirmedPdfRetrievalStatus.BLOB_MISSING: FinalConfirmedPdfGenerationStatus.PDF_BLOB_MISSING,
        FinalConfirmedPdfRetrievalStatus.BLOB_HASH_MISMATCH: FinalConfirmedPdfGenerationStatus.PDF_BLOB_HASH_MISMATCH,
        FinalConfirmedPdfRetrievalStatus.BLOB_SIZE_MISMATCH: FinalConfirmedPdfGenerationStatus.PDF_BLOB_SIZE_MISMATCH,
        FinalConfirmedPdfRetrievalStatus.BLOB_MEDIA_TYPE_MISMATCH: FinalConfirmedPdfGenerationStatus.PDF_BLOB_MEDIA_TYPE_MISMATCH,
        FinalConfirmedPdfRetrievalStatus.STORAGE_METADATA_CONFLICT: FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT,
        FinalConfirmedPdfRetrievalStatus.STORAGE_UNAVAILABLE: FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE,
    }
    assert result.status == expected[status]
    assert converter.calls == []
    assert result.replay_used is True
    assert result.canonical_artifact_reused is True


@pytest.mark.parametrize(
    "canonical_status",
    [FinalConfirmedPdfCanonicalReadStatus.STORAGE_METADATA_CONFLICT, FinalConfirmedPdfCanonicalReadStatus.STORAGE_UNAVAILABLE],
)
def test_replay_lookup_failure_total_mapping(canonical_status: FinalConfirmedPdfCanonicalReadStatus) -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.override_read_canonical = FinalConfirmedPdfCanonicalReadResult(status=canonical_status)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    expected = {
        FinalConfirmedPdfCanonicalReadStatus.STORAGE_METADATA_CONFLICT: FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT,
        FinalConfirmedPdfCanonicalReadStatus.STORAGE_UNAVAILABLE: FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE,
    }
    assert result.status == expected[canonical_status]
    assert converter.calls == []


# -- 20-22, 25-26: replay NOT_FOUND runs converter + converter total mapping --------


def test_replay_not_found_runs_converter_with_verified_arguments() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    assert result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    assert len(converter.calls) == 1
    call = converter.calls[0]
    # 25/26: the orchestrator always passes the *verified* snapshot fingerprint
    # and DOCX sha -- never a caller-supplied claim -- to the converter.
    assert call["source_snapshot_fingerprint"] == snapshot.final_snapshot_fingerprint
    assert call["source_docx_sha256"] == snapshot.final_docx_sha256
    assert call["docx_bytes"] == docx_bytes
    assert call["conversion_policy_version"] == policy


@pytest.mark.parametrize(
    "status", [s for s in DocxToPdfConversionStatus if s != DocxToPdfConversionStatus.SUCCEEDED]
)
def test_converter_failure_total_mapping(status: DocxToPdfConversionStatus) -> None:
    owner_key, snapshot, docx_bytes, reader, identity, _converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    def _factory(**kwargs):
        return DocxToPdfConversionResult(
            status=status, conversion_policy_version=kwargs["conversion_policy_version"], error_code="simulated"
        )

    converter = _FakeConverter(identity=identity, result_factory=_factory)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    expected = {
        DocxToPdfConversionStatus.INVALID_REQUEST: FinalConfirmedPdfGenerationStatus.INVALID_REQUEST,
        DocxToPdfConversionStatus.CONVERTER_UNAVAILABLE: FinalConfirmedPdfGenerationStatus.CONVERTER_UNAVAILABLE,
        DocxToPdfConversionStatus.PROCESS_START_FAILED: FinalConfirmedPdfGenerationStatus.PROCESS_START_FAILED,
        DocxToPdfConversionStatus.PROCESS_OUTPUT_LIMIT_EXCEEDED: FinalConfirmedPdfGenerationStatus.PROCESS_OUTPUT_LIMIT_EXCEEDED,
        DocxToPdfConversionStatus.CONVERSION_TIMEOUT: FinalConfirmedPdfGenerationStatus.CONVERSION_TIMEOUT,
        DocxToPdfConversionStatus.PROCESS_TERMINATION_FAILED: FinalConfirmedPdfGenerationStatus.PROCESS_TERMINATION_FAILED,
        DocxToPdfConversionStatus.CONVERSION_FAILED: FinalConfirmedPdfGenerationStatus.CONVERSION_FAILED,
        DocxToPdfConversionStatus.PDF_OUTPUT_MISSING: FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_MISSING,
        DocxToPdfConversionStatus.PDF_OUTPUT_LOCKED: FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_LOCKED,
        DocxToPdfConversionStatus.PDF_OUTPUT_UNREADABLE: FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_UNREADABLE,
        DocxToPdfConversionStatus.PDF_OUTPUT_CHANGED_DURING_READ: FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_CHANGED_DURING_READ,
        DocxToPdfConversionStatus.PDF_OUTPUT_TOO_LARGE: FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_TOO_LARGE,
        DocxToPdfConversionStatus.PDF_OUTPUT_INVALID: FinalConfirmedPdfGenerationStatus.PDF_OUTPUT_INVALID,
        DocxToPdfConversionStatus.WORKSPACE_SETUP_FAILED: FinalConfirmedPdfGenerationStatus.WORKSPACE_SETUP_FAILED,
        DocxToPdfConversionStatus.WORKSPACE_CLEANUP_FAILED: FinalConfirmedPdfGenerationStatus.WORKSPACE_CLEANUP_FAILED,
        DocxToPdfConversionStatus.CONVERTER_CONTRACT_VIOLATION: FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION,
    }
    assert result.status == expected[status]
    assert result.conversion_performed is True
    assert repo.save_calls == []
    assert repo.promote_current_authority_calls == 0


# -- 23-24, 27: converter success validation ------------------------------------------


def test_converter_success_identity_mismatch_is_contract_violation() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, _converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)
    other_identity = _runtime_identity(executable_sha256="b" * 64)

    def _factory(**kwargs):
        pdf_bytes = b"%PDF-1.4 mismatched identity"
        return DocxToPdfConversionResult(
            status=DocxToPdfConversionStatus.SUCCEEDED,
            pdf_bytes=pdf_bytes,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            runtime_identity=other_identity,
            conversion_policy_version=kwargs["conversion_policy_version"],
        )

    converter = _FakeConverter(identity=identity, result_factory=_factory)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION
    assert repo.save_calls == []


def test_converter_success_pdf_hash_mismatch_is_contract_violation() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, _converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    class _TamperedResult:
        status = DocxToPdfConversionStatus.SUCCEEDED
        pdf_bytes = b"%PDF-1.4 tampered"
        pdf_sha256 = "f" * 64  # deliberately not sha256(pdf_bytes)
        conversion_policy_version = _POLICY

        def __init__(self, runtime_identity):
            self.runtime_identity = runtime_identity

    def _factory(**kwargs):
        return _TamperedResult(identity)

    converter = _FakeConverter(identity=identity, result_factory=_factory)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION
    assert repo.save_calls == []


def test_converter_success_policy_mismatch_is_contract_violation() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, _converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    def _factory(**kwargs):
        pdf_bytes = b"%PDF-1.4 wrong policy"
        return DocxToPdfConversionResult(
            status=DocxToPdfConversionStatus.SUCCEEDED,
            pdf_bytes=pdf_bytes,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            runtime_identity=identity,
            conversion_policy_version="a-completely-different-policy",
        )

    converter = _FakeConverter(identity=identity, result_factory=_factory)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONVERTER_CONTRACT_VIOLATION
    assert repo.save_calls == []


# -- 28: build request fingerprint binding --------------------------------------------


def test_artifact_conversion_request_fingerprint_is_bound_to_pre_replay_request_fingerprint() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    expected_request_fp = compute_final_confirmed_pdf_request_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        conversion_policy_version=policy,
    )

    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    assert result.artifact.conversion_request_fingerprint == expected_request_fp


# -- 29-33: save mapping ----------------------------------------------------------------


def test_save_saved_is_confirmed_pdf_created() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    assert result.canonical_artifact_reused is False
    assert result.replay_used is False
    assert result.conversion_performed is True


def test_save_already_exists_identical_is_replayed_and_promoted() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    pdf_bytes = b"%PDF-1.4 already exists identical"
    existing = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=policy,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    # Actually store `existing` so the subsequent real CAS call (against the
    # real inner repository) can find it as a valid target artifact. Force
    # the replay lookup itself to miss so the orchestrator genuinely takes
    # the real-conversion -> save-race path (steps 7-10), never the replay
    # path (steps 5-6) -- `existing` shares the same conversion_request_
    # fingerprint, so an un-forced replay lookup would find it too.
    real_save = repo.inner.save_artifact(existing, pdf_bytes)
    assert real_save.status == FinalConfirmedPdfSaveStatus.SAVED
    repo.override_read_canonical = FinalConfirmedPdfCanonicalReadResult(
        status=FinalConfirmedPdfCanonicalReadStatus.NOT_FOUND
    )
    repo.override_save = FinalConfirmedPdfSaveResult(
        status=FinalConfirmedPdfSaveStatus.ALREADY_EXISTS_IDENTICAL, artifact=existing
    )
    repo.override_retrieve = FinalConfirmedPdfRetrievalResult(
        status=FinalConfirmedPdfRetrievalStatus.OK, artifact=existing, pdf_bytes=pdf_bytes
    )

    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED
    assert result.canonical_artifact_reused is True
    assert result.replay_used is False
    assert result.conversion_performed is True
    assert result.pdf_bytes == pdf_bytes
    assert result.artifact == existing


def test_save_request_already_canonical_never_returns_losing_bytes() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    winner_pdf_bytes = b"%PDF-1.4 winner bytes"
    winner = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=policy,
        pdf_sha256=hashlib.sha256(winner_pdf_bytes).hexdigest(),
    )
    # Actually store `winner` so the subsequent real CAS call (against the
    # real inner repository) can find it as a valid target artifact. Force
    # the replay lookup itself to miss for the same reason as the
    # ALREADY_EXISTS_IDENTICAL test above.
    real_save = repo.inner.save_artifact(winner, winner_pdf_bytes)
    assert real_save.status == FinalConfirmedPdfSaveStatus.SAVED
    repo.override_read_canonical = FinalConfirmedPdfCanonicalReadResult(
        status=FinalConfirmedPdfCanonicalReadStatus.NOT_FOUND
    )
    repo.override_save = FinalConfirmedPdfSaveResult(
        status=FinalConfirmedPdfSaveStatus.REQUEST_ALREADY_CANONICAL, artifact=winner
    )
    repo.override_retrieve = FinalConfirmedPdfRetrievalResult(
        status=FinalConfirmedPdfRetrievalStatus.OK, artifact=winner, pdf_bytes=winner_pdf_bytes
    )

    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED
    assert result.canonical_artifact_reused is True
    assert result.replay_used is False
    # 32: the (different, losing) converter-produced bytes are never returned.
    assert result.pdf_bytes == winner_pdf_bytes
    assert result.pdf_bytes != converter.calls  # sanity: not comparing to the wrong thing
    assert result.artifact == winner


@pytest.mark.parametrize(
    "status",
    [
        FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND,
        FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND,
        FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH,
        FinalConfirmedPdfSaveStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH,
        FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT,
        FinalConfirmedPdfSaveStatus.STORAGE_UNAVAILABLE,
    ],
)
def test_save_failures_total_mapping_and_never_perform_cas(status: FinalConfirmedPdfSaveStatus) -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)
    repo.override_save = FinalConfirmedPdfSaveResult(status=status)

    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    expected = {
        FinalConfirmedPdfSaveStatus.OWNER_NOT_FOUND: FinalConfirmedPdfGenerationStatus.OWNER_NOT_FOUND,
        FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND: FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_SNAPSHOT_NOT_FOUND,
        FinalConfirmedPdfSaveStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH: FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_SNAPSHOT_OWNER_MISMATCH,
        FinalConfirmedPdfSaveStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH: FinalConfirmedPdfGenerationStatus.SOURCE_FINAL_DOCX_HASH_MISMATCH,
        FinalConfirmedPdfSaveStatus.STORAGE_METADATA_CONFLICT: FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT,
        FinalConfirmedPdfSaveStatus.STORAGE_UNAVAILABLE: FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE,
    }
    assert result.status == expected[status]
    assert repo.promote_current_authority_calls == 0


# -- 34: captured already current ------------------------------------------------------


def test_captured_authority_already_current_skips_cas() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    first = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert first.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    cas_calls_after_first = repo.promote_current_authority_calls

    second = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert second.status == FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT
    assert second.current_authority_promoted is False
    assert second.replay_used is True
    assert second.canonical_artifact_reused is True
    assert second.artifact == first.artifact
    assert repo.promote_current_authority_calls == cas_calls_after_first


# -- 35-36: CAS PROMOTED (fresh conversion / replay) ------------------------------------


def test_cas_promoted_after_fresh_conversion_is_confirmed_pdf_created() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    assert result.current_authority_promoted is True
    assert result.final_authority_observation.current_artifact_fingerprint == result.artifact.artifact_fingerprint


def test_cas_promoted_after_replay_is_replayed_and_promoted() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    pdf_bytes = b"%PDF-1.4 seeded without promotion"
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=policy,
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
    # Seed the artifact directly (as a concurrent winner would leave it)
    # without ever promoting authority.
    repo.save_artifact(artifact, pdf_bytes)

    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED
    assert result.current_authority_promoted is True
    assert result.replay_used is True


# -- 37-38: CAS STALE_REVISION reclassification -----------------------------------------


def test_cas_stale_revision_but_fresh_state_already_targets_winner_is_already_current() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    first = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert first.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    repo.promote_current_authority_calls = 0

    # Force a stale captured observation (pretend nothing was authoritative
    # yet) while the real repository state already targets our own winner.
    repo.override_read_current_authority = FinalConfirmedPdfAuthorityReadResult(
        status=FinalConfirmedPdfAuthorityReadStatus.ABSENT,
        observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
    )
    repo.override_read_canonical = FinalConfirmedPdfCanonicalReadResult(status=FinalConfirmedPdfCanonicalReadStatus.NOT_FOUND)
    converter2 = _FakeConverter(
        identity=identity,
        result_factory=lambda **kwargs: DocxToPdfConversionResult(
            status=DocxToPdfConversionStatus.SUCCEEDED,
            pdf_bytes=(pdf := b"%PDF-1.4 fake pdf " + kwargs["source_docx_sha256"].encode()),
            pdf_sha256=hashlib.sha256(pdf).hexdigest(),
            runtime_identity=identity,
            conversion_policy_version=kwargs["conversion_policy_version"],
        ),
    )
    result = _call(owner_key, snapshot, reader, converter2, repo, policy)

    assert result.status == FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT
    assert result.current_authority_promoted is False
    assert result.final_authority_observation.current_artifact_fingerprint == first.artifact.artifact_fingerprint
    assert repo.promote_current_authority_calls == 1


def test_cas_stale_revision_with_different_current_is_slot_conflict() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    other_snapshot = _snapshot(owner_key, b"a wholly different docx", policy="fin-v1")
    other_reader = _source_reader(owner_key, other_snapshot, b"a wholly different docx")
    repo.register_final_snapshot(other_snapshot)
    other_converter = _success_converter(identity)
    other_result = _call(owner_key, other_snapshot, other_reader, other_converter, repo, policy)
    assert other_result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    repo.promote_current_authority_calls = 0

    # Force our own captured observation to look stale/absent while the real
    # authority is already at `other_result.artifact` -- a different target.
    repo.override_read_current_authority = FinalConfirmedPdfAuthorityReadResult(
        status=FinalConfirmedPdfAuthorityReadStatus.ABSENT,
        observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
    )
    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    assert result.status == FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT
    assert result.current_authority_promoted is False
    assert result.final_authority_observation.current_artifact_fingerprint == other_result.artifact.artifact_fingerprint
    assert result.final_authority_observation.current_artifact_fingerprint != result.artifact.artifact_fingerprint
    assert repo.promote_current_authority_calls == 1


# -- 39-43: CAS failure total mapping -----------------------------------------------------


@pytest.mark.parametrize(
    "cas_status,expected",
    [
        (FinalConfirmedPdfAuthorityCasStatus.OWNER_NOT_FOUND, FinalConfirmedPdfGenerationStatus.OWNER_NOT_FOUND),
        (
            FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_NOT_FOUND,
            FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT,
        ),
        (
            FinalConfirmedPdfAuthorityCasStatus.TARGET_ARTIFACT_OWNER_MISMATCH,
            FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT,
        ),
        (
            FinalConfirmedPdfAuthorityCasStatus.STORAGE_METADATA_CONFLICT,
            FinalConfirmedPdfGenerationStatus.STORAGE_METADATA_CONFLICT,
        ),
        (FinalConfirmedPdfAuthorityCasStatus.STORAGE_UNAVAILABLE, FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE),
    ],
)
def test_cas_failure_total_mapping(
    cas_status: FinalConfirmedPdfAuthorityCasStatus, expected: FinalConfirmedPdfGenerationStatus
) -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)
    repo.override_cas = FinalConfirmedPdfAuthorityCasResult(status=cas_status)

    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == expected
    assert result.current_authority_promoted is False


# -- 44-45: zero CAS retry / authority never recaptured ------------------------------------


def test_zero_cas_retry_and_authority_read_exactly_once() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)
    repo.override_cas = FinalConfirmedPdfAuthorityCasResult(
        status=FinalConfirmedPdfAuthorityCasStatus.STALE_REVISION,
        current_observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
    )
    result = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert result.status == FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT
    assert repo.promote_current_authority_calls == 1
    assert repo.read_current_authority_calls == 1


# -- 46-47: result validators / CURRENT_PDF_SLOT_CONFLICT invariants ------------------------


def test_result_validator_rejects_payload_on_non_payload_status() -> None:
    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    owner_key = _owner()
    snapshot = _snapshot(owner_key, b"validator docx")
    identity = _runtime_identity()
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=hashlib.sha256(b"validator pdf").hexdigest(),
    )
    with pytest.raises(Exception):
        FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.STORAGE_UNAVAILABLE,
            artifact=artifact,
        )


def test_result_validator_requires_promoted_true_for_confirmed_pdf_created() -> None:
    from app.schemas.cv_document_final_confirmed_pdf import build_final_confirmed_pdf_artifact

    owner_key = _owner()
    snapshot = _snapshot(owner_key, b"validator docx 2")
    identity = _runtime_identity()
    artifact = build_final_confirmed_pdf_artifact(
        owner_key=owner_key,
        source_final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
        source_final_docx_sha256=snapshot.final_docx_sha256,
        runtime_identity=identity,
        conversion_policy_version=_POLICY,
        pdf_sha256=hashlib.sha256(b"validator pdf 2").hexdigest(),
    )
    observation = ConfirmedPdfCurrentAuthorityObservation(
        exists=True,
        lineage_kind=ConfirmedPdfLineageKind.FINAL_DOCX_SNAPSHOT,
        current_artifact_fingerprint=artifact.artifact_fingerprint,
        slot_version=1,
    )
    with pytest.raises(Exception):
        FinalConfirmedPdfGenerationResult(
            status=FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED,
            artifact=artifact,
            pdf_bytes=b"validator pdf 2",
            source_final_snapshot=snapshot,
            final_authority_observation=observation,
            current_authority_promoted=False,
        )


def test_current_pdf_slot_conflict_partial_success_invariants() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    other_snapshot = _snapshot(owner_key, b"conflict docx b", policy="fin-v1")
    other_reader = _source_reader(owner_key, other_snapshot, b"conflict docx b")
    repo.register_final_snapshot(other_snapshot)
    other_result = _call(owner_key, other_snapshot, other_reader, _success_converter(identity), repo, policy)
    assert other_result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED

    repo.override_read_current_authority = FinalConfirmedPdfAuthorityReadResult(
        status=FinalConfirmedPdfAuthorityReadStatus.ABSENT,
        observation=ConfirmedPdfCurrentAuthorityObservation(exists=False),
    )
    result = _call(owner_key, snapshot, reader, converter, repo, policy)

    assert result.status == FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT
    # Partial-success invariants: artifact/pdf_bytes/source snapshot are all
    # still present (the conversion+save genuinely succeeded), only the CAS
    # promotion did not happen.
    assert result.artifact is not None
    assert result.pdf_bytes is not None
    assert result.source_final_snapshot is not None
    assert result.current_authority_promoted is False


# -- 48-50: replay_used / canonical_artifact_reused / conversion_performed semantics --------


def test_flag_semantics_across_paths() -> None:
    owner_key, snapshot, docx_bytes, reader, identity, converter, repo, policy = _seeded_env()
    repo.register_owner(owner_key)

    fresh = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert fresh.replay_used is False
    assert fresh.canonical_artifact_reused is False
    assert fresh.conversion_performed is True

    already_current = _call(owner_key, snapshot, reader, converter, repo, policy)
    assert already_current.replay_used is True
    assert already_current.canonical_artifact_reused is True
    assert already_current.conversion_performed is False


# -- 51-53: exception/import boundaries -----------------------------------------------------


def test_orchestrator_never_imports_sqlalchemy() -> None:
    tree = ast.parse(inspect.getsource(orchestrator_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sqlalchemy")
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("sqlalchemy")


def test_orchestrator_never_imports_working_copy() -> None:
    tree = ast.parse(inspect.getsource(orchestrator_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "working_copy" not in node.module


def test_orchestrator_never_imports_semantic_validation() -> None:
    tree = ast.parse(inspect.getsource(orchestrator_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "semantic" not in node.module
            assert "truth_library" not in node.module


def test_orchestrator_never_imports_blob_store_or_storage_error() -> None:
    tree = ast.parse(inspect.getsource(orchestrator_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "blob_store" not in node.module
