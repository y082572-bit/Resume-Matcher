"""Explicit Provenance Stage P6-B3b-B: concurrency tests for
``generate_final_confirmed_pdf`` against a real
``FinalConfirmedPdfSqlRepository`` -- exactly one canonical artifact per
request fingerprint, exactly one CAS write ever wins per race, a losing
concurrent writer always reports the real winning artifact/bytes, and a
stale request whose captured authority was overtaken by an unrelated
promotion during conversion always reports ``CURRENT_PDF_SLOT_CONFLICT``
without ever clobbering the newer authority.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
import app.services.cv_document_models as cdm
from app.schemas.cv_document_artifact import ArtifactOwnerKind, CvDocxProposalArtifact, DocxProposalProvenanceMode
from app.schemas.cv_document_final_confirmed_pdf_orchestration import FinalConfirmedPdfGenerationStatus
from app.schemas.cv_document_final_snapshot import FINAL_SNAPSHOT_SCHEMA_VERSION, FinalDocxSnapshot
from app.schemas.cv_document_pdf_conversion import DocxToPdfConversionResult, DocxToPdfConversionStatus
from app.schemas.cv_document_pdf_conversion_runtime import build_converter_runtime_identity
from app.services.cv_document_blob_store import CvDocumentBlobStore
from app.services.cv_document_final_confirmed_pdf_cutover_state import run_cutover_installer
from app.services.cv_document_final_confirmed_pdf_orchestrator import generate_final_confirmed_pdf
from app.services.cv_document_final_confirmed_pdf_sql_repository import FinalConfirmedPdfSqlRepository
from app.services.cv_document_final_docx_snapshot_pdf_source_reader import FinalDocxSnapshotPdfSourceReader
from app.services.cv_document_final_snapshot_repository import compute_final_snapshot_fingerprint
from app.services.cv_document_final_snapshot_sql_repository import FinalDocxSnapshotSqlRepository
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_builder import (
    compute_generation_input_fingerprint,
    compute_proposal_artifact_fingerprint,
)
from app.services.cv_document_sql_repository import CvDocumentArtifactSqlRepository


_POLICY_VERSION = "orchestrator-concurrency-policy-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _CountingRepository:
    """Delegates every call to a real ``FinalConfirmedPdfSqlRepository`` --
    only adds a call counter on ``promote_current_authority`` so tests can
    assert there is never a hidden second CAS write."""

    def __init__(self, inner: FinalConfirmedPdfSqlRepository) -> None:
        self.inner = inner
        self.promote_current_authority_calls = 0
        self._lock = threading.Lock()

    def read_current_authority(self, owner_key):
        return self.inner.read_current_authority(owner_key)

    def read_artifact(self, artifact_fingerprint: str):
        return self.inner.read_artifact(artifact_fingerprint)

    def read_canonical_artifact_by_conversion_request_fingerprint(self, conversion_request_fingerprint: str):
        return self.inner.read_canonical_artifact_by_conversion_request_fingerprint(conversion_request_fingerprint)

    def retrieve_pdf_bytes_by_artifact(self, artifact_fingerprint: str):
        return self.inner.retrieve_pdf_bytes_by_artifact(artifact_fingerprint)

    def save_artifact(self, artifact, pdf_bytes: bytes):
        return self.inner.save_artifact(artifact, pdf_bytes)

    def promote_current_authority(self, owner_key, target_artifact_fingerprint: str, expected_observation):
        with self._lock:
            self.promote_current_authority_calls += 1
        return self.inner.promote_current_authority(owner_key, target_artifact_fingerprint, expected_observation)


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orchestrator_concurrency.db'}", future=True)
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in cdm.FINAL_CONFIRMED_PDF_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = CvDocumentBlobStore(tmp_path / "blob_store", max_blob_size_bytes=10_000_000)
    base_repo = CvDocumentArtifactSqlRepository(session_factory, store)
    final_snapshot_repo = FinalDocxSnapshotSqlRepository(session_factory, store)
    repo = _CountingRepository(FinalConfirmedPdfSqlRepository(session_factory, store))
    source_reader = FinalDocxSnapshotPdfSourceReader(final_snapshot_repo)
    return base_repo, final_snapshot_repo, repo, source_reader


def _owner_key(reference_id: str = "job-1"):
    return build_owner_key(person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id)


def _runtime_identity():
    return build_converter_runtime_identity(
        implementation_id="libreoffice",
        implementation_version="7.6",
        executable_canonical_path="/usr/bin/soffice",
        executable_file_identity="dev:1,inode:2",
        executable_sha256="a" * 64,
        platform_identity="darwin-arm64",
        font_environment_id="fonts-v1",
    )


def _setup_final_snapshot(
    base_repo,
    final_snapshot_repo,
    owner_key,
    content: bytes,
    *,
    revision: int = 1,
    expected_previous_revision: int | None = None,
) -> FinalDocxSnapshot:
    gen_hash = _sha256(content)
    gen_input_fp = compute_generation_input_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        template_fingerprint="c" * 64,
        renderer_adapter_id="fake-renderer",
        rendering_policy_version="v1",
        document_schema_version="cv-document-proposal-schema-v1",
    )
    artifact_fp = compute_proposal_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash,
        proposal_revision=revision,
    )
    proposal = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint="a" * 64,
        content_plan_fingerprint="b" * 64,
        role_strategy_context_fingerprint=None,
        document_schema_version="cv-document-proposal-schema-v1",
        rendering_policy_version="v1",
        renderer_adapter_id="fake-renderer",
        template_fingerprint="c" * 64,
        generation_input_fingerprint=gen_input_fp,
        generated_docx_content_hash=gen_hash,
        current_file_hash=gen_hash,
        validated_file_hash=None,
        proposal_revision=revision,
        superseded=False,
        artifact_fingerprint=artifact_fp,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )
    replace_result = base_repo.replace_current_proposal(owner_key, proposal, content, expected_previous_revision)
    assert replace_result.status.value == "REPLACED"

    fp = compute_final_snapshot_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=gen_hash,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
    )
    snapshot = FinalDocxSnapshot(
        owner_key=owner_key,
        source_proposal_artifact_fingerprint=proposal.artifact_fingerprint,
        source_proposal_revision=revision,
        generated_proposal_sha256=proposal.generated_docx_content_hash,
        final_docx_sha256=gen_hash,
        edited_by_user=False,
        finalization_policy_version=_POLICY_VERSION,
        snapshot_schema_version=FINAL_SNAPSHOT_SCHEMA_VERSION,
        final_snapshot_fingerprint=fp,
    )
    save_result = final_snapshot_repo.save_final_docx_snapshot(snapshot, content)
    assert save_result.status.value in ("SAVED", "ALREADY_EXISTS_IDENTICAL")
    return snapshot


class _DeterministicConverter:
    """A pure, deterministic fake ``DocxToPdfConverter``: identical inputs
    always produce byte-identical output, distinct ``variant`` values
    always produce distinct output. Never touches LibreOffice/a subprocess.
    """

    def __init__(self, identity, *, variant: bytes = b"", before_convert=None) -> None:
        self._identity = identity
        self._variant = variant
        self._before_convert = before_convert

    @property
    def runtime_identity(self):
        return self._identity

    def convert_docx_to_pdf(
        self, *, docx_bytes: bytes, source_snapshot_fingerprint: str, source_docx_sha256: str, conversion_policy_version: str
    ) -> DocxToPdfConversionResult:
        if self._before_convert is not None:
            self._before_convert()
        pdf_bytes = b"%PDF-1.4 " + hashlib.sha256(docx_bytes + conversion_policy_version.encode() + self._variant).hexdigest().encode()
        return DocxToPdfConversionResult(
            status=DocxToPdfConversionStatus.SUCCEEDED,
            pdf_bytes=pdf_bytes,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            runtime_identity=self._identity,
            conversion_policy_version=conversion_policy_version,
        )


# -- 1-6: two identical requests, both miss replay, one PROMOTED, one ALREADY_CURRENT --


def test_two_identical_requests_one_promoted_one_already_current(env) -> None:
    base_repo, final_snapshot_repo, repo, source_reader = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"identical request content")
    identity = _runtime_identity()

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        converter = _DeterministicConverter(identity, before_convert=barrier.wait)
        results[name] = generate_final_confirmed_pdf(
            owner_key=owner_key,
            final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
            conversion_policy_version=_POLICY_VERSION,
            source_reader=source_reader,
            converter=converter,
            repository=repo,
        )

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [r.status for r in results.values()]
    # Both convert to byte-identical PDFs (same docx_bytes/policy/identity),
    # so both build the exact same candidate artifact -- one SAVED, the
    # other ALREADY_EXISTS_IDENTICAL, and exactly one CAS write ever wins.
    assert statuses.count(FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED) == 1
    assert statuses.count(FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT) == 1

    winner_fingerprints = {r.artifact.artifact_fingerprint for r in results.values()}
    assert len(winner_fingerprints) == 1
    # Both threads capture authority as ABSENT before either promotes, so
    # both attempt exactly one CAS write each -- the winner PROMOTED, the
    # loser STALE_REVISION-but-already-pointing-at-the-same-winner (byte-
    # identical builds share the same artifact_fingerprint) -- never a
    # second write from either thread.
    assert repo.promote_current_authority_calls == 2

    already_current = next(
        r for r in results.values() if r.status == FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT
    )
    assert already_current.current_authority_promoted is False
    assert already_current.pdf_bytes == next(
        r for r in results.values() if r.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    ).pdf_bytes


# -- 7-11: two requests converting to different bytes -----------------------------------


def test_two_requests_different_bytes_one_saved_one_request_already_canonical(env) -> None:
    base_repo, final_snapshot_repo, repo, source_reader = env
    owner_key = _owner_key()
    snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"different bytes race content")
    identity = _runtime_identity()

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, variant: bytes) -> None:
        converter = _DeterministicConverter(identity, variant=variant, before_convert=barrier.wait)
        results[name] = generate_final_confirmed_pdf(
            owner_key=owner_key,
            final_snapshot_fingerprint=snapshot.final_snapshot_fingerprint,
            conversion_policy_version=_POLICY_VERSION,
            source_reader=source_reader,
            converter=converter,
            repository=repo,
        )

    t1 = threading.Thread(target=worker, args=("A", b"variant-a"))
    t2 = threading.Thread(target=worker, args=("B", b"variant-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 8-9: the two builds produce distinct artifacts (distinct pdf_sha256),
    # so the save race resolves to exactly one canonical_artifact_reused=False
    # (the real SAVED writer) and one canonical_artifact_reused=True (the
    # REQUEST_ALREADY_CANONICAL loser, which converges onto the same target
    # artifact as the winner).
    reused_flags = [r.canonical_artifact_reused for r in results.values()]
    assert reused_flags.count(False) == 1
    assert reused_flags.count(True) == 1

    # 10: both target the same winning artifact -- because the save race
    # already converged both threads onto that one artifact before either
    # attempts its own (single) CAS write.
    winner_fingerprints = {r.artifact.artifact_fingerprint for r in results.values()}
    assert len(winner_fingerprints) == 1

    # 11: both report the same, real canonical winning bytes -- neither the
    # A-variant nor the B-variant bytes on their own, but whichever one won.
    winning_bytes = {r.pdf_bytes for r in results.values()}
    assert len(winning_bytes) == 1

    # Since both threads converge on the same target artifact before CAS,
    # whichever attempts the CAS write first wins PROMOTED (status
    # CONFIRMED_PDF_CREATED if it was the SAVED writer, REPLAYED_AND_PROMOTED
    # if it was the REQUEST_ALREADY_CANONICAL writer); the other is
    # reclassified ALREADY_CURRENT after its own STALE_REVISION, never
    # CURRENT_PDF_SLOT_CONFLICT, since the fresh state already matches its
    # own target.
    statuses = [r.status for r in results.values()]
    assert statuses.count(FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT) == 1
    promoting_statuses = {
        FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED,
        FinalConfirmedPdfGenerationStatus.REPLAYED_AND_PROMOTED,
    }
    assert sum(1 for s in statuses if s in promoting_statuses) == 1
    promoted_result = next(r for r in results.values() if r.status in promoting_statuses)
    assert promoted_result.current_authority_promoted is True
    already_current_result = next(
        r for r in results.values() if r.status == FinalConfirmedPdfGenerationStatus.ALREADY_CURRENT
    )
    assert already_current_result.current_authority_promoted is False

    assert repo.promote_current_authority_calls == 2


# -- 12-15: unrelated request promotes authority during a stale request's conversion --


def test_unrelated_promotion_during_conversion_yields_slot_conflict_without_overwrite(env) -> None:
    base_repo, final_snapshot_repo, repo, source_reader = env
    owner_key = _owner_key()
    stale_snapshot = _setup_final_snapshot(base_repo, final_snapshot_repo, owner_key, b"stale request content")
    unrelated_snapshot = _setup_final_snapshot(
        base_repo, final_snapshot_repo, owner_key, b"unrelated newer content", revision=2, expected_previous_revision=1
    )
    identity = _runtime_identity()

    conversion_may_proceed = threading.Event()
    unrelated_promotion_done = threading.Event()

    def _stale_before_convert() -> None:
        # Authority was already captured (ABSENT) before this call -- block
        # here so the unrelated request can complete a full promotion first.
        unrelated_promotion_done.wait(timeout=5)

    stale_converter = _DeterministicConverter(identity, variant=b"stale", before_convert=_stale_before_convert)

    stale_result_holder: dict[str, object] = {}

    def stale_worker() -> None:
        stale_result_holder["result"] = generate_final_confirmed_pdf(
            owner_key=owner_key,
            final_snapshot_fingerprint=stale_snapshot.final_snapshot_fingerprint,
            conversion_policy_version=_POLICY_VERSION,
            source_reader=source_reader,
            converter=stale_converter,
            repository=repo,
        )

    t_stale = threading.Thread(target=stale_worker)
    t_stale.start()

    unrelated_converter = _DeterministicConverter(identity, variant=b"unrelated")
    unrelated_result = generate_final_confirmed_pdf(
        owner_key=owner_key,
        final_snapshot_fingerprint=unrelated_snapshot.final_snapshot_fingerprint,
        conversion_policy_version=_POLICY_VERSION,
        source_reader=source_reader,
        converter=unrelated_converter,
        repository=repo,
    )
    assert unrelated_result.status == FinalConfirmedPdfGenerationStatus.CONFIRMED_PDF_CREATED
    unrelated_promotion_done.set()

    t_stale.join(timeout=5)
    stale_result = stale_result_holder["result"]

    # 13: the stale request reports CURRENT_PDF_SLOT_CONFLICT.
    assert stale_result.status == FinalConfirmedPdfGenerationStatus.CURRENT_PDF_SLOT_CONFLICT
    assert stale_result.current_authority_promoted is False
    assert (
        stale_result.final_authority_observation.current_artifact_fingerprint
        == unrelated_result.artifact.artifact_fingerprint
    )

    # 14: the newer (unrelated) authority is never overwritten by the stale request.
    final_authority = repo.read_current_authority(owner_key)
    assert final_authority.observation.current_artifact_fingerprint == unrelated_result.artifact.artifact_fingerprint

    # 15: zero second CAS write from the stale request -- exactly the
    # unrelated request's PROMOTED write, plus the stale request's own
    # single (losing) attempt.
    assert repo.promote_current_authority_calls == 2
