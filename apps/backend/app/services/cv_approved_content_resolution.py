"""Explicit Provenance Stage P6-C1b-B: the Approved Content Resolution
composition root.

``resolve_current_approved_content_document_input`` is the only entry point
this module exposes. Given an already self-consistent ``JobArtifactOwnerKey``
and an ``ApprovedContentPackageRepository``, it performs one stable
observation of that owner's current-authority pointer, reads the immutable
package it names, re-validates every binding between that package and the
caller's own owner key, re-confirms the authority pointer has not moved
underneath the read, flat-reconstructs an ``ApprovedContentDocumentInput``
from the package's own payload, recomputes its ``document_input_fingerprint``
via the existing helper (never a hand-rolled copy of that formula), and
finally re-runs the existing, independent
``validate_approved_content_document_input`` before ever returning ``FOUND``.

Nothing here is retried: ``read_current_authority`` is called at most twice
(the ``A1``/``A2`` observation pair) and ``get_package_by_fingerprint`` at
most once. The package is immutable and content-addressed by its own
fingerprint, so a second read of the very same package would only ever
re-confirm what the first read already proved -- it is never re-read.

This module never imports FastAPI, Starlette, SQLAlchemy, a router, or an
HTTP schema -- it is a plain, synchronous composition root over the existing
``ApprovedContentPackageRepository`` Protocol. It never creates, saves, or
promotes an Approved Content Package, never generates a Proposal, and
implements no HTTP surface -- see
``docs/explicit-provenance-stage-p6c1bb-approved-content-resolution.md`` for
the full scope boundary.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.schemas.cv_approved_content_package import (
    ApprovedContentAuthorityReadStatus,
    ApprovedContentPackageReadStatus,
)
from app.schemas.cv_approved_content_resolution import (
    ApprovedContentResolutionResult,
    ApprovedContentResolutionStatus,
)
from app.schemas.cv_document_artifact import ApprovedContentDocumentInput, DocumentInputValidationStatus, JobArtifactOwnerKey
from app.services.cv_approved_content_package_repository_protocol import ApprovedContentPackageRepository
from app.services.cv_content_approval_replay import compute_approved_content_replay_input_fingerprint
from app.services.cv_document_input_validation import compute_document_input_fingerprint, validate_approved_content_document_input
from app.services.cv_document_owner_identity import build_owner_key


def _not_found_result(status: ApprovedContentResolutionStatus) -> ApprovedContentResolutionResult:
    return ApprovedContentResolutionResult(status=status)


def resolve_current_approved_content_document_input(
    *,
    owner_key: JobArtifactOwnerKey,
    repository: ApprovedContentPackageRepository,
) -> ApprovedContentResolutionResult:
    # Owner-key self-consistency: an owner_key handed to this composition
    # root is never trusted at face value just because it came from a
    # server-side caller -- it is always independently rebuilt from its own
    # declared identity fields and compared field-by-field, including the
    # fingerprint, before the very first repository read.
    recomputed_owner_key = build_owner_key(
        person_entity_id=owner_key.person_entity_id,
        owner_kind=owner_key.owner_kind,
        owner_reference_id=owner_key.owner_reference_id,
    )
    if (
        recomputed_owner_key != owner_key
        or recomputed_owner_key.owner_key_fingerprint != owner_key.owner_key_fingerprint
    ):
        return _not_found_result(ApprovedContentResolutionStatus.OWNER_MISMATCH)

    # A1: first authority observation.
    authority_a1 = repository.read_current_authority(owner_key)
    if authority_a1.status == ApprovedContentAuthorityReadStatus.NOT_FOUND:
        return _not_found_result(ApprovedContentResolutionStatus.NOT_FOUND)
    if authority_a1.status == ApprovedContentAuthorityReadStatus.STORAGE_METADATA_CONFLICT:
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)
    if authority_a1.status == ApprovedContentAuthorityReadStatus.STORAGE_UNAVAILABLE:
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_UNAVAILABLE)
    assert authority_a1.authority is not None

    # Package read named by A1. The package is immutable and
    # content-addressed, so it is read exactly once regardless of how many
    # times the authority pointer is observed.
    package_read = repository.get_package_by_fingerprint(authority_a1.authority.package_fingerprint)
    if package_read.status == ApprovedContentPackageReadStatus.NOT_FOUND:
        # A dangling authority pointer (a promoted fingerprint with no
        # backing package row) is a storage-integrity problem, never an
        # ordinary "not found" -- it can only mean the package store and the
        # authority store have gone out of sync with each other.
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)
    if package_read.status == ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT:
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)
    if package_read.status == ApprovedContentPackageReadStatus.STORAGE_UNAVAILABLE:
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_UNAVAILABLE)
    assert package_read.package is not None
    package = package_read.package
    payload = package.payload
    plan = payload.source_content_plan

    # Owner binding between the package just read and the caller's own
    # owner key -- every one of these must hold before A2 is ever read, and
    # none of them is skipped just because an earlier one already passed.
    owner_binding_holds = (
        package.owner_key_fingerprint == owner_key.owner_key_fingerprint
        and package.owner_kind == owner_key.owner_kind
        and payload.owner_kind == owner_key.owner_kind
        and plan.person_entity_id == owner_key.person_entity_id
        and plan.job_or_application_id == owner_key.owner_reference_id
    )
    if not owner_binding_holds:
        return _not_found_result(ApprovedContentResolutionStatus.OWNER_MISMATCH)

    # A2: second authority observation -- the linearization point. FOUND is
    # only ever reachable when A1 and A2 agree exactly; a package that
    # legitimately matched the owner at A1 is discarded fail-closed the
    # moment authority has moved by the time A2 is read.
    authority_a2 = repository.read_current_authority(owner_key)
    if authority_a2.status == ApprovedContentAuthorityReadStatus.NOT_FOUND:
        return _not_found_result(ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)
    if authority_a2.status == ApprovedContentAuthorityReadStatus.STORAGE_METADATA_CONFLICT:
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_METADATA_CONFLICT)
    if authority_a2.status == ApprovedContentAuthorityReadStatus.STORAGE_UNAVAILABLE:
        return _not_found_result(ApprovedContentResolutionStatus.STORAGE_UNAVAILABLE)
    assert authority_a2.authority is not None
    if (
        authority_a2.authority.package_fingerprint != authority_a1.authority.package_fingerprint
        or authority_a2.authority.slot_version != authority_a1.authority.slot_version
    ):
        return _not_found_result(ApprovedContentResolutionStatus.STALE_AUTHORITY_OBSERVATION)

    # Flat reconstruction from the package's own payload only -- never from
    # ``package.document_input_fingerprint`` itself, which is instead the
    # comparison target for the freshly recomputed value below.
    current_p55_replay_input_fingerprint = compute_approved_content_replay_input_fingerprint(
        payload.current_p55_replay_input
    )
    recomputed_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=payload.approved_content_result.result_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        owner_kind=owner_key.owner_kind.value,
        owner_key_fingerprint=owner_key.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=current_p55_replay_input_fingerprint,
    )
    if recomputed_document_input_fingerprint != package.document_input_fingerprint:
        return _not_found_result(ApprovedContentResolutionStatus.DOCUMENT_INPUT_FINGERPRINT_MISMATCH)

    try:
        document_input = ApprovedContentDocumentInput(
            approved_content_result=payload.approved_content_result.model_copy(deep=True),
            source_content_plan=payload.source_content_plan.model_copy(deep=True),
            current_p55_replay_input=payload.current_p55_replay_input.model_copy(deep=True),
            semantic_validation_replay_input=payload.semantic_validation_replay_input.model_copy(deep=True),
            approval_decisions=tuple(decision.model_copy(deep=True) for decision in payload.approval_decisions),
            owner_kind=payload.owner_kind,
            document_input_schema_version=payload.document_input_schema_version,
            document_input_policy_version=payload.document_input_policy_version,
            document_input_fingerprint=recomputed_document_input_fingerprint,
        )
    except ValidationError:
        # A normal (non-``model_copy``) constructor call re-runs every
        # nested model's own validators -- a payload whose embedded
        # ``CvContentPlan`` fails its own ``ROLE_STRATEGY_INTEGRATED``
        # strategy-field check can never be reconstructed at all. That is
        # still exactly a document-input validity problem, never a raw
        # exception escaping to the caller.
        return _not_found_result(ApprovedContentResolutionStatus.DOCUMENT_INPUT_INVALID)

    validation = validate_approved_content_document_input(document_input)
    if validation.status != DocumentInputValidationStatus.VALID:
        return _not_found_result(ApprovedContentResolutionStatus.DOCUMENT_INPUT_INVALID)
    if validation.owner_key is not None and validation.owner_key != owner_key:
        return _not_found_result(ApprovedContentResolutionStatus.OWNER_MISMATCH)

    return ApprovedContentResolutionResult(
        status=ApprovedContentResolutionStatus.FOUND,
        document_input=document_input.model_copy(deep=True),
        observed_package_fingerprint=authority_a2.authority.package_fingerprint,
        observed_authority_slot_version=authority_a2.authority.slot_version,
    )
