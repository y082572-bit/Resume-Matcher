"""Explicit Provenance Stage P6-A: current DOCX proposal generation.

``build_current_docx_proposal`` never trusts a caller's document input at
face value (it re-runs ``validate_approved_content_document_input`` first),
never trusts a template provider's or renderer's self-reported hash
(everything is independently recomputed), and only ever replaces the
repository's current-proposal slot through its CAS ``expected_previous_revision``
contract. A renderer failure, a tampered hash, or a stale CAS revision
never creates a proposal and never touches the existing current
proposal/PDF.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInput,
    CvDocxProposalArtifact,
    CvDocxProposalBuildResult,
    DocumentInputValidationStatus,
    DocxProposalProvenanceMode,
    DocxRenderingStatus,
    ProposalBuildStatus,
)
from app.services.cv_document_adapters import CvDocxRenderingAdapter, DocxTemplateProvider
from app.services.cv_document_input_validation import validate_approved_content_document_input
from app.services.cv_document_repository_protocol import (
    CasReplaceStatus,
    CvDocumentArtifactRepository,
)
from app.services.truth_fingerprint import canonical_json_bytes


DOCUMENT_SCHEMA_VERSION = "cv-document-proposal-schema-v1"
GENERATION_INPUT_FINGERPRINT_VERSION = "cv-document-proposal-generation-input-v1"
PROPOSAL_ARTIFACT_FINGERPRINT_VERSION = "cv-document-proposal-artifact-v1"


def compute_generation_input_fingerprint(
    *,
    owner_key_fingerprint: str,
    approved_cv_content_fingerprint: str,
    content_plan_fingerprint: str,
    role_strategy_context_fingerprint: str | None,
    template_fingerprint: str,
    renderer_adapter_id: str,
    rendering_policy_version: str,
    document_schema_version: str,
    source_personal_info_fingerprint: str | None = None,
    render_plan_fingerprint: str | None = None,
) -> str:
    content = {
        "fingerprint_version": GENERATION_INPUT_FINGERPRINT_VERSION,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "approved_cv_content_fingerprint": approved_cv_content_fingerprint,
            "content_plan_fingerprint": content_plan_fingerprint,
            "role_strategy_context_fingerprint": role_strategy_context_fingerprint,
            "template_fingerprint": template_fingerprint,
            "renderer_adapter_id": renderer_adapter_id,
            "rendering_policy_version": rendering_policy_version,
            "document_schema_version": document_schema_version,
            # Owner-bound header identity (P6-B2A-R1, additive): a change to
            # full_name/email/phone/location/LinkedIn changes
            # source_personal_info_fingerprint/render_plan_fingerprint and
            # therefore this fingerprint, so it always propagates into
            # artifact_fingerprint below. None for any caller that does not
            # generate a header (unchanged P6-A semantics).
            "source_personal_info_fingerprint": source_personal_info_fingerprint,
            "render_plan_fingerprint": render_plan_fingerprint,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def compute_proposal_artifact_fingerprint(
    *,
    owner_key_fingerprint: str,
    generation_input_fingerprint: str,
    generated_docx_content_hash: str,
    proposal_revision: int,
) -> str:
    content = {
        "fingerprint_version": PROPOSAL_ARTIFACT_FINGERPRINT_VERSION,
        "content": {
            "owner_key_fingerprint": owner_key_fingerprint,
            "generation_input_fingerprint": generation_input_fingerprint,
            "generated_docx_content_hash": generated_docx_content_hash,
            "proposal_revision": proposal_revision,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def build_current_docx_proposal(
    *,
    document_input: ApprovedContentDocumentInput,
    template_provider: DocxTemplateProvider,
    renderer: CvDocxRenderingAdapter,
    repository: CvDocumentArtifactRepository,
    rendering_policy_version: str,
    expected_previous_revision: int | None,
    source_personal_info_fingerprint: str | None = None,
    render_plan_fingerprint: str | None = None,
) -> CvDocxProposalBuildResult:
    validation_result = validate_approved_content_document_input(document_input)
    if validation_result.status != DocumentInputValidationStatus.VALID:
        return CvDocxProposalBuildResult(
            status=ProposalBuildStatus.INPUT_INVALID,
            input_violations=validation_result.violations,
        )

    owner_key = validation_result.owner_key
    assert owner_key is not None  # VALID always carries an owner_key

    template_handle = template_provider.get_template()
    pre_render_hash = hashlib.sha256(template_handle.template_bytes).hexdigest()
    if pre_render_hash != template_handle.template_fingerprint:
        return CvDocxProposalBuildResult(
            status=ProposalBuildStatus.TEMPLATE_HASH_CHANGED,
            diagnostics=("template_provider returned bytes not matching its own template_fingerprint",),
        )

    approved_content_result = document_input.approved_content_result
    content_plan = document_input.source_content_plan

    rendering_result = renderer.render(
        approved_content_result=approved_content_result,
        content_plan=content_plan,
        template_bytes=template_handle.template_bytes,
        template_fingerprint=template_handle.template_fingerprint,
        rendering_policy_version=rendering_policy_version,
    )

    if rendering_result.status != DocxRenderingStatus.SUCCEEDED:
        return CvDocxProposalBuildResult(
            status=ProposalBuildStatus.RENDERING_FAILED,
            diagnostics=rendering_result.diagnostics,
        )

    assert rendering_result.docx_bytes is not None
    assert rendering_result.output_sha256 is not None
    recomputed_output_hash = hashlib.sha256(rendering_result.docx_bytes).hexdigest()
    if recomputed_output_hash != rendering_result.output_sha256:
        return CvDocxProposalBuildResult(
            status=ProposalBuildStatus.RENDERER_OUTPUT_HASH_MISMATCH,
            diagnostics=("renderer output_sha256 does not match a recomputed hash of docx_bytes",),
        )

    post_render_hash = hashlib.sha256(template_handle.template_bytes).hexdigest()
    if post_render_hash != template_handle.template_fingerprint:
        return CvDocxProposalBuildResult(
            status=ProposalBuildStatus.TEMPLATE_HASH_CHANGED,
            diagnostics=("template bytes hash changed after the renderer call",),
        )

    generation_input_fingerprint = compute_generation_input_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        approved_cv_content_fingerprint=approved_content_result.result_fingerprint,
        content_plan_fingerprint=content_plan.content_plan_fingerprint,
        role_strategy_context_fingerprint=content_plan.role_strategy_context_fingerprint,
        template_fingerprint=template_handle.template_fingerprint,
        renderer_adapter_id=rendering_result.renderer_adapter_id,
        rendering_policy_version=rendering_result.rendering_policy_version,
        document_schema_version=DOCUMENT_SCHEMA_VERSION,
        source_personal_info_fingerprint=source_personal_info_fingerprint,
        render_plan_fingerprint=render_plan_fingerprint,
    )

    proposal_revision = (expected_previous_revision or 0) + 1
    generated_docx_content_hash = recomputed_output_hash

    artifact_fingerprint = compute_proposal_artifact_fingerprint(
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        generation_input_fingerprint=generation_input_fingerprint,
        generated_docx_content_hash=generated_docx_content_hash,
        proposal_revision=proposal_revision,
    )

    artifact = CvDocxProposalArtifact(
        artifact_id=uuid4(),
        owner_key=owner_key,
        approved_cv_content_fingerprint=approved_content_result.result_fingerprint,
        content_plan_fingerprint=content_plan.content_plan_fingerprint,
        role_strategy_context_fingerprint=content_plan.role_strategy_context_fingerprint,
        document_schema_version=DOCUMENT_SCHEMA_VERSION,
        rendering_policy_version=rendering_result.rendering_policy_version,
        renderer_adapter_id=rendering_result.renderer_adapter_id,
        template_fingerprint=template_handle.template_fingerprint,
        generation_input_fingerprint=generation_input_fingerprint,
        generated_docx_content_hash=generated_docx_content_hash,
        current_file_hash=generated_docx_content_hash,
        validated_file_hash=None,
        proposal_revision=proposal_revision,
        superseded=False,
        artifact_fingerprint=artifact_fingerprint,
        provenance_mode=DocxProposalProvenanceMode.GENERATED_BY_RENDERER,
    )

    replace_result = repository.replace_current_proposal(
        owner_key, artifact, rendering_result.docx_bytes, expected_previous_revision
    )
    if replace_result.status != CasReplaceStatus.REPLACED:
        return CvDocxProposalBuildResult(
            status=ProposalBuildStatus.STALE_REVISION,
            diagnostics=("expected_previous_revision did not match the repository's current revision",),
        )

    return CvDocxProposalBuildResult(status=ProposalBuildStatus.GENERATED, artifact=artifact, docx_bytes=rendering_result.docx_bytes)
