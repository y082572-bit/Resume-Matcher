"""Explicit Provenance Stage P6-B2A-R1: the production composition root for
the deterministic Proposal DOCX.

``generate_current_deterministic_docx_proposal`` is the single supported
production path from an ``ApprovedContentDocumentInput`` to a current
Proposal DOCX. It never accepts header strings, a ``CvDocxHeaderLines``, an
arbitrary render plan, a template provider, a renderer adapter, or template
bytes from its caller -- every one of those is built internally, in a fixed
order:

    ApprovedContentDocumentInput
    -> JobArtifactOwnerKey revalidation
    -> CandidateIdentityHeaderSqlService.resolve_header_binding
    -> owner-bound CvDocxHeaderBinding
    -> complete CvDocxRenderPlan
    -> DeterministicCvDocxRenderer
    -> build_current_docx_proposal (existing P6-A orchestration)
    -> existing CvDocumentArtifactRepository
    -> current Proposal DOCX slot.

There is no manual full_name/email/phone/location/LinkedIn passthrough
anywhere on this path: every header field originates only from
``JobArtifactOwnerKey`` -> ``candidate_identity_bindings`` -> the Master
Resume's ``PersonalInfo``, resolved fresh on every call by
``CandidateIdentityHeaderSqlService``. This module introduces no second
proposal table, no second current-proposal slot, and reuses the existing
``build_current_docx_proposal``/``CvDocumentArtifactRepository`` current
proposal CAS slot unchanged; it never touches the confirmed PDF slot.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.candidate_identity_binding import CvDocxHeaderBinding, CvDocxHeaderBindingStatus
from app.schemas.cv_document_artifact import (
    ApprovedContentDocumentInput,
    CvDocxProposalArtifact,
    DocumentInputValidationStatus,
    ProposalBuildStatus,
)
from app.schemas.cv_docx_visual_contract import CvDocxVisualContract
from app.services.cv_docx_header_binding_builder import CandidateIdentityHeaderSqlService
from app.services.cv_document_input_validation import validate_approved_content_document_input
from app.services.cv_document_proposal_builder import build_current_docx_proposal
from app.services.cv_document_repository_protocol import CvDocumentArtifactRepository
from app.services.cv_docx_render_plan_builder import CvDocxHeaderLines, build_cv_docx_render_plan
from app.services.cv_docx_renderer import DeterministicCvDocxRenderer
from app.services.cv_docx_template import DeterministicDocxTemplateProvider


class DeterministicDocxProposalGenerationStatus(StrEnum):
    """Closed set of outcomes
    ``generate_current_deterministic_docx_proposal`` can return. Never a
    raw SQLAlchemy/Pydantic/ZIP/python-docx exception, and never a raw
    ``ProposalBuildStatus``/``CvDocxHeaderBindingStatus`` value."""

    GENERATED = "GENERATED"
    INPUT_INVALID = "INPUT_INVALID"
    HEADER_RESOLUTION_FAILED = "HEADER_RESOLUTION_FAILED"
    HEADER_OWNER_MISMATCH = "HEADER_OWNER_MISMATCH"
    RENDER_PLAN_FAILED = "RENDER_PLAN_FAILED"
    PROPOSAL_BUILD_FAILED = "PROPOSAL_BUILD_FAILED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


class DeterministicDocxProposalGenerationResult(BaseModel):
    """The single return value of
    ``generate_current_deterministic_docx_proposal``. Only ``GENERATED``
    ever carries an artifact/bytes."""

    model_config = ConfigDict(extra="forbid")

    status: DeterministicDocxProposalGenerationStatus
    artifact: CvDocxProposalArtifact | None = None
    docx_bytes: bytes | None = None

    @model_validator(mode="after")
    def _status_consistency(self) -> "DeterministicDocxProposalGenerationResult":
        if self.status == DeterministicDocxProposalGenerationStatus.GENERATED:
            if self.artifact is None or self.docx_bytes is None:
                raise ValueError("GENERATED requires artifact and docx_bytes")
        else:
            if self.artifact is not None or self.docx_bytes is not None:
                raise ValueError(f"{self.status} must carry no artifact/docx_bytes")
        return self


def _result(status: DeterministicDocxProposalGenerationStatus) -> DeterministicDocxProposalGenerationResult:
    return DeterministicDocxProposalGenerationResult(status=status)


def _header_contact_tokens(header: CvDocxHeaderBinding) -> tuple[str, ...]:
    return tuple(token for token in (header.email, header.phone, header.location, header.linkedin) if token)


def generate_current_deterministic_docx_proposal(
    *,
    document_input: ApprovedContentDocumentInput,
    visual_contract: CvDocxVisualContract,
    header_service: CandidateIdentityHeaderSqlService,
    repository: CvDocumentArtifactRepository,
    rendering_policy_version: str,
    expected_previous_revision: int | None,
) -> DeterministicDocxProposalGenerationResult:
    """The single supported production composition path for the
    deterministic Proposal DOCX. See module docstring for the full flow."""

    validation_result = validate_approved_content_document_input(document_input)
    if validation_result.status != DocumentInputValidationStatus.VALID:
        return _result(DeterministicDocxProposalGenerationStatus.INPUT_INVALID)

    owner_key = validation_result.owner_key
    assert owner_key is not None  # VALID always carries an owner_key

    header_result = header_service.resolve_header_binding(owner_key=owner_key)
    if header_result.status == CvDocxHeaderBindingStatus.BINDING_OWNER_MISMATCH:
        return _result(DeterministicDocxProposalGenerationStatus.HEADER_OWNER_MISMATCH)
    if header_result.status == CvDocxHeaderBindingStatus.STORAGE_UNAVAILABLE:
        return _result(DeterministicDocxProposalGenerationStatus.STORAGE_UNAVAILABLE)
    if header_result.status != CvDocxHeaderBindingStatus.RESOLVED:
        return _result(DeterministicDocxProposalGenerationStatus.HEADER_RESOLUTION_FAILED)

    header = header_result.header
    assert header is not None  # RESOLVED always carries a header

    # Independent re-check: the resolved header must be bound to the exact
    # same owner this call is generating a proposal for -- never trusted
    # merely because resolve_header_binding returned RESOLVED.
    if header.owner_key_fingerprint != owner_key.owner_key_fingerprint or header.person_entity_id != owner_key.person_entity_id:
        return _result(DeterministicDocxProposalGenerationStatus.HEADER_OWNER_MISMATCH)

    header_lines = CvDocxHeaderLines(full_name=header.full_name, contact_tokens=_header_contact_tokens(header))

    try:
        render_plan = build_cv_docx_render_plan(
            approved_content_result=document_input.approved_content_result,
            visual_contract=visual_contract,
            header_lines=header_lines,
        )
    except ValueError:
        return _result(DeterministicDocxProposalGenerationStatus.RENDER_PLAN_FAILED)

    template_provider = DeterministicDocxTemplateProvider(visual_contract)
    renderer = DeterministicCvDocxRenderer(visual_contract, render_plan=render_plan)

    build_result = build_current_docx_proposal(
        document_input=document_input,
        template_provider=template_provider,
        renderer=renderer,
        repository=repository,
        rendering_policy_version=rendering_policy_version,
        expected_previous_revision=expected_previous_revision,
        source_personal_info_fingerprint=header.source_personal_info_fingerprint,
        render_plan_fingerprint=render_plan.render_plan_fingerprint,
    )

    if build_result.status != ProposalBuildStatus.GENERATED:
        return _result(DeterministicDocxProposalGenerationStatus.PROPOSAL_BUILD_FAILED)

    assert build_result.artifact is not None
    assert build_result.docx_bytes is not None
    return DeterministicDocxProposalGenerationResult(
        status=DeterministicDocxProposalGenerationStatus.GENERATED,
        artifact=build_result.artifact,
        docx_bytes=build_result.docx_bytes,
    )
