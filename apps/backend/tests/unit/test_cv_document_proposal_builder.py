"""Unit tests for Explicit Provenance Stage P6-A:
``build_current_docx_proposal``.

Uses a real PRE-P4 -> P3(strategy) -> P4(``ROLE_STRATEGY_INTEGRATED``) ->
P5a -> P5.5 pipeline (``EXACT_COPY``, no LLM call) to produce a genuine
``ApprovedContentDocumentInput`` (via the shared
``build_ready_integrated_context``/``build_document_input`` factory in
``test_cv_document_input_validation.py`` -- P6-A accepts no other plan
mode), plus deterministic fake ``DocxTemplateProvider``/
``CvDocxRenderingAdapter`` implementations -- P6-A ships no real renderer
or template provider.

Also covers the repository immutability/defensive-copy invariants
(``REPOSITORY_CAS_OR_ATOMICITY_FALSE`` remediation): every object the
repository returns is a fresh copy, never its own internally stored
reference, so a caller mutating what it was handed back (or the object it
originally passed to ``replace_current_proposal``) can never change
repository state.
"""

from __future__ import annotations

import hashlib
import inspect
from uuid import uuid4

import pytest

from app.schemas.cv_document_artifact import (
    ArtifactOwnerKind,
    CvDocxRenderingResult,
    DocxRenderingFailureCode,
    DocxRenderingStatus,
    ProposalBuildStatus,
)
from app.services.cv_document_adapters import CvDocxRenderingAdapter, DocxTemplateHandle, DocxTemplateProvider
from app.services.cv_document_owner_identity import build_owner_key
from app.services.cv_document_proposal_builder import build_current_docx_proposal, compute_generation_input_fingerprint
from app.services.cv_document_repository_protocol import CasReplaceStatus, InMemoryCvDocumentArtifactRepository
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


build_ready_context = build_ready_integrated_context


class _FakeTemplateProvider:
    def __init__(self, template_bytes: bytes = b"FAKE-DOCX-TEMPLATE-BYTES-V1", adapter_id: str = "fake-template-provider-v1") -> None:
        self._template_bytes = template_bytes
        self._adapter_id = adapter_id

    def get_template(self) -> DocxTemplateHandle:
        return DocxTemplateHandle(
            template_bytes=self._template_bytes,
            template_fingerprint=hashlib.sha256(self._template_bytes).hexdigest(),
            template_adapter_id=self._adapter_id,
        )


class _FakeRenderer:
    """A deterministic fake renderer: places only ``approved_content_result``
    content (its fingerprint) alongside the template bytes. Never accepts a
    path and never generates new content."""

    def __init__(self, adapter_id: str = "fake-renderer-v1", fail: bool = False, tamper_output_hash: bool = False) -> None:
        self._adapter_id = adapter_id
        self._fail = fail
        self._tamper_output_hash = tamper_output_hash
        self.last_call_kwargs: dict | None = None

    @property
    def renderer_adapter_id(self) -> str:
        return self._adapter_id

    def render(
        self,
        *,
        approved_content_result,
        content_plan,
        template_bytes: bytes,
        template_fingerprint: str,
        rendering_policy_version: str,
    ) -> CvDocxRenderingResult:
        self.last_call_kwargs = {
            "approved_content_result": approved_content_result,
            "content_plan": content_plan,
            "template_bytes": template_bytes,
            "template_fingerprint": template_fingerprint,
            "rendering_policy_version": rendering_policy_version,
        }
        if self._fail:
            return CvDocxRenderingResult(
                status=DocxRenderingStatus.FAILED,
                renderer_adapter_id=self._adapter_id,
                rendering_policy_version=rendering_policy_version,
                failure_code=DocxRenderingFailureCode.RENDERER_ERROR,
                diagnostics=("forced failure for test",),
            )
        output = template_bytes + b"::" + approved_content_result.result_fingerprint.encode()
        real_hash = hashlib.sha256(output).hexdigest()
        claimed_hash = ("0" * 64) if self._tamper_output_hash else real_hash
        return CvDocxRenderingResult(
            status=DocxRenderingStatus.SUCCEEDED,
            renderer_adapter_id=self._adapter_id,
            rendering_policy_version=rendering_policy_version,
            docx_bytes=output,
            output_sha256=claimed_hash,
        )


RENDERING_POLICY_VERSION = "cv-document-rendering-policy-test-v1"


@pytest.mark.asyncio
async def test_first_generation_creates_current_proposal() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )

    assert result.status == ProposalBuildStatus.GENERATED
    assert result.artifact is not None
    assert result.artifact.proposal_revision == 1
    assert repository.get_current_proposal(result.artifact.owner_key) == result.artifact


@pytest.mark.asyncio
async def test_regeneration_replaces_current_and_supersedes_previous() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    first = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(adapter_id="renderer-v1"),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == ProposalBuildStatus.GENERATED

    second = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(adapter_id="renderer-v2"),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert second.status == ProposalBuildStatus.GENERATED
    assert second.artifact.proposal_revision == 2

    owner_key = first.artifact.owner_key
    current = repository.get_current_proposal(owner_key)
    assert current.artifact_fingerprint == second.artifact.artifact_fingerprint
    assert current.superseded is False

    history = repository._proposal_history[owner_key.owner_key_fingerprint]
    assert len(history) == 1
    assert history[0].artifact_fingerprint == first.artifact.artifact_fingerprint
    assert history[0].superseded is True


@pytest.mark.asyncio
async def test_stale_cas_revision_blocks() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    first = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == ProposalBuildStatus.GENERATED

    stale_attempt = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,  # stale: current revision is already 1
    )
    assert stale_attempt.status == ProposalBuildStatus.STALE_REVISION
    assert stale_attempt.artifact is None
    # current proposal must remain the first one, untouched
    assert repository.get_current_proposal(first.artifact.owner_key).artifact_fingerprint == first.artifact.artifact_fingerprint


@pytest.mark.asyncio
async def test_renderer_failure_creates_no_proposal() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(fail=True),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )

    assert result.status == ProposalBuildStatus.RENDERING_FAILED
    assert result.artifact is None
    owner_key = build_owner_key(
        person_entity_id=ctx["plan"].person_entity_id,
        owner_kind=ArtifactOwnerKind.JOB,
        owner_reference_id=ctx["plan"].job_or_application_id,
    )
    assert repository.get_current_proposal(owner_key) is None


@pytest.mark.asyncio
async def test_renderer_failure_leaves_previous_proposal_untouched() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    first = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == ProposalBuildStatus.GENERATED

    failed_regeneration = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(fail=True),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert failed_regeneration.status == ProposalBuildStatus.RENDERING_FAILED
    current = repository.get_current_proposal(first.artifact.owner_key)
    assert current.artifact_fingerprint == first.artifact.artifact_fingerprint
    assert current.proposal_revision == 1


@pytest.mark.asyncio
async def test_regeneration_does_not_remove_existing_pdf() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    first = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    owner_key = first.artifact.owner_key

    from app.schemas.cv_document_artifact import ConfirmedCvPdfArtifact, DocumentProvenanceMode

    fake_pdf = ConfirmedCvPdfArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        source_validated_docx_snapshot_fingerprint="a" * 64,
        source_docx_sha256="b" * 64,
        approved_cv_content_fingerprint=ctx["result"].result_fingerprint,
        content_plan_fingerprint=ctx["plan"].content_plan_fingerprint,
        pdf_sha256="c" * 64,
        conversion_adapter_id="fake-pdf-adapter",
        conversion_policy_version="pdf-policy-v1",
        provenance_mode=DocumentProvenanceMode.PIPELINE_UNMODIFIED_DOCUMENT,
        artifact_fingerprint="d" * 64,
        pdf_revision=1,
        superseded=False,
    )
    replace_result = repository.replace_current_pdf(owner_key, fake_pdf, b"%PDF-fake-bytes", None)
    assert replace_result.status == CasReplaceStatus.REPLACED

    second = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert second.status == ProposalBuildStatus.GENERATED
    assert repository.get_current_pdf(owner_key).artifact_fingerprint == "d" * 64


@pytest.mark.asyncio
async def test_tampered_renderer_output_hash_blocks() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(tamper_output_hash=True),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == ProposalBuildStatus.RENDERER_OUTPUT_HASH_MISMATCH
    assert result.artifact is None


@pytest.mark.asyncio
async def test_renderer_receives_only_approved_content_and_no_path() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    renderer = _FakeRenderer()

    build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=renderer,
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )

    assert renderer.last_call_kwargs["approved_content_result"] is document_input.approved_content_result
    assert renderer.last_call_kwargs["content_plan"] is document_input.source_content_plan

    signature = inspect.signature(CvDocxRenderingAdapter.render)
    assert "path" not in signature.parameters
    assert "target_path" not in signature.parameters
    assert "master_resume_path" not in signature.parameters


def test_template_bytes_hash_stable_across_provider_calls() -> None:
    provider = _FakeTemplateProvider()
    handle_one = provider.get_template()
    handle_two = provider.get_template()
    assert handle_one.template_fingerprint == handle_two.template_fingerprint
    assert hashlib.sha256(handle_one.template_bytes).hexdigest() == handle_one.template_fingerprint


def test_generation_input_fingerprint_changes_with_template() -> None:
    base_kwargs = dict(
        owner_key_fingerprint="a" * 64,
        approved_cv_content_fingerprint="b" * 64,
        content_plan_fingerprint="c" * 64,
        role_strategy_context_fingerprint=None,
        renderer_adapter_id="renderer-1",
        rendering_policy_version="policy-1",
        document_schema_version="schema-1",
    )
    fp1 = compute_generation_input_fingerprint(template_fingerprint="d" * 64, **base_kwargs)
    fp2 = compute_generation_input_fingerprint(template_fingerprint="e" * 64, **base_kwargs)
    assert fp1 != fp2


def test_generation_input_fingerprint_changes_with_rendering_policy() -> None:
    base_kwargs = dict(
        owner_key_fingerprint="a" * 64,
        approved_cv_content_fingerprint="b" * 64,
        content_plan_fingerprint="c" * 64,
        role_strategy_context_fingerprint=None,
        template_fingerprint="d" * 64,
        renderer_adapter_id="renderer-1",
        document_schema_version="schema-1",
    )
    fp1 = compute_generation_input_fingerprint(rendering_policy_version="policy-1", **base_kwargs)
    fp2 = compute_generation_input_fingerprint(rendering_policy_version="policy-2", **base_kwargs)
    assert fp1 != fp2


def test_generation_input_fingerprint_changes_with_approved_content() -> None:
    base_kwargs = dict(
        owner_key_fingerprint="a" * 64,
        content_plan_fingerprint="c" * 64,
        role_strategy_context_fingerprint=None,
        template_fingerprint="d" * 64,
        renderer_adapter_id="renderer-1",
        rendering_policy_version="policy-1",
        document_schema_version="schema-1",
    )
    fp1 = compute_generation_input_fingerprint(approved_cv_content_fingerprint="b" * 64, **base_kwargs)
    fp2 = compute_generation_input_fingerprint(approved_cv_content_fingerprint="f" * 64, **base_kwargs)
    assert fp1 != fp2


@pytest.mark.asyncio
async def test_proposal_identity_carries_no_path_field() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()

    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    dumped_keys = result.artifact.model_dump(mode="json").keys()
    assert not any("path" in key.lower() for key in dumped_keys)
    assert not any("filename" in key.lower() for key in dumped_keys)


@pytest.mark.asyncio
async def test_input_invalid_document_never_reaches_renderer() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    tampered = document_input.model_copy(update={"document_input_fingerprint": "0" * 64})
    repository = InMemoryCvDocumentArtifactRepository()
    renderer = _FakeRenderer()

    result = build_current_docx_proposal(
        document_input=tampered,
        template_provider=_FakeTemplateProvider(),
        renderer=renderer,
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert result.status == ProposalBuildStatus.INPUT_INVALID
    assert renderer.last_call_kwargs is None


# -- Repository immutability / defensive copying (REPOSITORY_CAS_OR_ATOMICITY_FALSE) --


@pytest.mark.asyncio
async def test_get_current_proposal_never_returns_same_object_identity() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    owner_key = build_owner_key(
        person_entity_id=ctx["plan"].person_entity_id,
        owner_kind=ArtifactOwnerKind.JOB,
        owner_reference_id=ctx["plan"].job_or_application_id,
    )
    first = repository.get_current_proposal(owner_key)
    second = repository.get_current_proposal(owner_key)
    assert first == second
    assert first is not second
    assert first is not repository._current_proposal[owner_key.owner_key_fingerprint]
    assert second is not repository._current_proposal[owner_key.owner_key_fingerprint]


@pytest.mark.asyncio
async def test_returned_proposal_is_frozen_against_field_assignment() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    owner_key = result.artifact.owner_key
    returned = repository.get_current_proposal(owner_key)

    with pytest.raises(Exception):
        returned.proposal_revision = 999
    with pytest.raises(Exception):
        returned.superseded = True

    # neither attempted mutation changed repository state
    still_current = repository.get_current_proposal(owner_key)
    assert still_current.proposal_revision == 1
    assert still_current.superseded is False


@pytest.mark.asyncio
async def test_model_copy_on_returned_proposal_never_changes_repository() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    owner_key = result.artifact.owner_key
    returned = repository.get_current_proposal(owner_key)

    locally_modified = returned.model_copy(update={"proposal_revision": 999, "superseded": True})
    assert locally_modified.proposal_revision == 999  # a genuine new local object

    still_current = repository.get_current_proposal(owner_key)
    assert still_current.proposal_revision == 1
    assert still_current.superseded is False


@pytest.mark.asyncio
async def test_mutating_original_artifact_after_replace_does_not_change_repository() -> None:
    """Even the very object handed to ``replace_current_proposal`` cannot
    later change repository state -- the repository stores its own
    defensive copy at write time, and the artifact model is itself frozen."""

    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    original_artifact = result.artifact

    with pytest.raises(Exception):
        original_artifact.proposal_revision = 42  # frozen: cannot mutate in place

    stored = repository.get_current_proposal(original_artifact.owner_key)
    assert stored.proposal_revision == 1
    assert stored is not original_artifact


@pytest.mark.asyncio
async def test_stale_cas_still_blocks_after_defensive_copying() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    first = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    assert first.status == ProposalBuildStatus.GENERATED

    stale = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,  # stale
    )
    assert stale.status == ProposalBuildStatus.STALE_REVISION
    # failed replace changed neither current nor history
    current = repository.get_current_proposal(first.artifact.owner_key)
    assert current.artifact_fingerprint == first.artifact.artifact_fingerprint
    history = repository._proposal_history.get(first.artifact.owner_key.owner_key_fingerprint, [])
    assert history == []


@pytest.mark.asyncio
async def test_repository_never_holds_two_current_proposals_for_one_owner() -> None:
    ctx = await build_ready_context()
    document_input = build_document_input(ctx)
    repository = InMemoryCvDocumentArtifactRepository()
    owner_key_fingerprint_seen = set()

    first = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=None,
    )
    second = build_current_docx_proposal(
        document_input=document_input,
        template_provider=_FakeTemplateProvider(),
        renderer=_FakeRenderer(),
        repository=repository,
        rendering_policy_version=RENDERING_POLICY_VERSION,
        expected_previous_revision=1,
    )
    assert second.status == ProposalBuildStatus.GENERATED
    owner_key_fingerprint_seen.add(first.artifact.owner_key.owner_key_fingerprint)
    # exactly one entry in the current-proposal dict for this owner
    assert len([k for k in repository._current_proposal if k in owner_key_fingerprint_seen]) == 1


def test_history_getter_exposes_no_mutable_internal_state() -> None:
    """``_proposal_history`` entries are themselves frozen models, so even
    direct access to the private list (as other P6-A tests already do)
    cannot mutate a stored superseded record in place."""

    from app.schemas.cv_document_artifact import CvDocxProposalArtifact

    assert CvDocxProposalArtifact.model_config.get("frozen") is True
