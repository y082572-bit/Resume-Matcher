"""Explicit Provenance Stage P6-C1b-A: read-only Approved Content Package
reconciliation.

``run_reconciliation`` reads every package row, every current-authority row,
every owner row, and the live SQLite schema/indexes/constraints/triggers of
both tables -- and reports what it found. It never issues an ``INSERT``,
``UPDATE``, or ``DELETE``, never promotes an authority row, and never
repairs a row it finds inconsistent.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.schemas.cv_approved_content_package import (
    APPROVED_CONTENT_PACKAGE_POLICY_VERSION,
    APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION,
    ApprovedContentPackagePayload,
)
from app.schemas.cv_approved_content_package_reconciliation import (
    ApprovedContentPackageReconciliationIssue,
    ApprovedContentPackageReconciliationIssueCode,
    ApprovedContentPackageReconciliationReport,
)
from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.services.cv_approved_content_package_builder import (
    build_fingerprint_input,
    compute_approval_decisions_fingerprint,
    compute_fact_selection_identity_fingerprint,
    compute_package_fingerprint,
)
from app.services.cv_approved_content_package_schema_manifest import (
    AUTHORITY_TABLE_NAME,
    AUTHORITY_TRIGGER_NAMES,
    EXPECTED_TRIGGER_BODIES,
    PACKAGE_TABLE_NAME,
    PACKAGE_TRIGGER_NAMES,
    preflight_approved_content_package_schema,
    table_matches_manifest,
)
from app.services.cv_content_approval_replay import compute_approved_content_replay_input_fingerprint
from app.services.cv_document_input_validation import compute_document_input_fingerprint
from app.services.cv_document_models import (
    CvApprovedContentCurrentAuthorityRow,
    CvApprovedContentPackageRow,
    CvDocumentArtifactOwner,
)
from app.services.truth_fingerprint import canonical_json_bytes


_IssueCode = ApprovedContentPackageReconciliationIssueCode


def _issue(
    code: _IssueCode,
    *,
    package_fingerprint: str | None = None,
    owner_key_fingerprint: str | None = None,
    detail: str,
) -> ApprovedContentPackageReconciliationIssue:
    return ApprovedContentPackageReconciliationIssue(
        issue_code=code,
        package_fingerprint=package_fingerprint,
        owner_key_fingerprint=owner_key_fingerprint,
        detail=detail,
    )


def _check_package_row(
    row: CvApprovedContentPackageRow, owner_ids: set[str], owners_by_id: dict[str, CvDocumentArtifactOwner]
) -> list[ApprovedContentPackageReconciliationIssue]:
    issues: list[ApprovedContentPackageReconciliationIssue] = []
    fp = row.package_fingerprint

    if row.owner_key_fingerprint not in owner_ids:
        issues.append(
            _issue(
                _IssueCode.OWNER_MISSING,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="package references an owner_key_fingerprint with no owner row",
            )
        )
        return issues

    if row.package_schema_version != APPROVED_CONTENT_PACKAGE_SCHEMA_VERSION:
        issues.append(
            _issue(
                _IssueCode.UNSUPPORTED_PACKAGE_SCHEMA_VERSION,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail=f"package_schema_version={row.package_schema_version!r} is not recognized",
            )
        )
    if row.package_policy_version != APPROVED_CONTENT_PACKAGE_POLICY_VERSION:
        issues.append(
            _issue(
                _IssueCode.UNSUPPORTED_PACKAGE_POLICY_VERSION,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail=f"package_policy_version={row.package_policy_version!r} is not recognized",
            )
        )

    try:
        parsed = json.loads(row.payload_json)
        payload = ApprovedContentPackagePayload.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError, TypeError, UnicodeDecodeError) as exc:
        issues.append(
            _issue(
                _IssueCode.PAYLOAD_RECONSTRUCTION_FAILED,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail=f"payload_json failed to reconstruct: {exc}",
            )
        )
        return issues

    recanonicalized = canonical_json_bytes(payload.model_dump(mode="json"))
    if recanonicalized != row.payload_json.encode("utf-8"):
        issues.append(
            _issue(
                _IssueCode.PAYLOAD_NOT_CANONICAL,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="stored payload_json is not the canonical serialization of its own parsed value",
            )
        )
    if len(recanonicalized) != row.payload_byte_size:
        issues.append(
            _issue(
                _IssueCode.PAYLOAD_SIZE_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="payload_byte_size does not match the recomputed canonical byte length",
            )
        )
    if hashlib.sha256(recanonicalized).hexdigest() != row.payload_sha256:
        issues.append(
            _issue(
                _IssueCode.PAYLOAD_SHA256_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="payload_sha256 does not match a recomputed hash of the stored payload",
            )
        )

    owner_row = owners_by_id.get(row.owner_key_fingerprint)
    plan = payload.source_content_plan
    if owner_row is not None and (
        owner_row.owner_kind != row.owner_kind
        or owner_row.person_entity_id != str(plan.person_entity_id)
        or owner_row.owner_reference_id != plan.job_or_application_id
    ):
        issues.append(
            _issue(
                _IssueCode.OWNER_PAYLOAD_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="owner row identity does not match the payload's source_content_plan",
            )
        )

    result = payload.approved_content_result
    current_p55_replay_input_fingerprint = compute_approved_content_replay_input_fingerprint(
        payload.current_p55_replay_input
    )
    recomputed_document_input_fingerprint = compute_document_input_fingerprint(
        approved_content_result_fingerprint=result.result_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        owner_kind=row.owner_kind,
        owner_key_fingerprint=row.owner_key_fingerprint,
        current_p55_replay_input_fingerprint=current_p55_replay_input_fingerprint,
    )
    if recomputed_document_input_fingerprint != row.document_input_fingerprint:
        issues.append(
            _issue(
                _IssueCode.DOCUMENT_INPUT_FINGERPRINT_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="document_input_fingerprint does not match its recomputed content",
            )
        )

    recomputed_approval_decisions_fingerprint = compute_approval_decisions_fingerprint(
        payload.approval_decisions,
        expected_semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
    )
    if (
        recomputed_approval_decisions_fingerprint is None
        or recomputed_approval_decisions_fingerprint != row.approval_decisions_fingerprint
    ):
        issues.append(
            _issue(
                _IssueCode.APPROVAL_DECISIONS_FINGERPRINT_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="approval_decisions_fingerprint does not match its recomputed content",
            )
        )

    recomputed_fact_selection_identity_fingerprint = compute_fact_selection_identity_fingerprint(
        plan.source_decision_fingerprints
    )
    if (
        recomputed_fact_selection_identity_fingerprint is None
        or recomputed_fact_selection_identity_fingerprint != row.fact_selection_identity_fingerprint
    ):
        issues.append(
            _issue(
                _IssueCode.FACT_SELECTION_IDENTITY_FINGERPRINT_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="fact_selection_identity_fingerprint does not match its recomputed content",
            )
        )

    fingerprint_input = build_fingerprint_input(
        owner_key_fingerprint=row.owner_key_fingerprint,
        owner_kind=ArtifactOwnerKind(row.owner_kind),
        document_input_fingerprint=row.document_input_fingerprint,
        approved_content_result_fingerprint=result.result_fingerprint,
        content_plan_fingerprint=plan.content_plan_fingerprint,
        semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
        approval_decisions_fingerprint=row.approval_decisions_fingerprint,
        fact_selection_identity_fingerprint=row.fact_selection_identity_fingerprint,
        role_strategy_context_fingerprint=plan.role_strategy_context_fingerprint,
        strategy_selection_result_fingerprint=plan.strategy_selection_result_fingerprint,
        strategy_ranking_input_fingerprint=plan.strategy_ranking_input_fingerprint,
        strategy_integration_policy_version=plan.strategy_integration_policy_version,
        p5a_result_fingerprint=result.p5a_result_fingerprint,
        p5b_result_fingerprint=result.p5b_result_fingerprint,
        payload_sha256=row.payload_sha256,
        document_input_schema_version=payload.document_input_schema_version,
        document_input_policy_version=payload.document_input_policy_version,
        package_schema_version=row.package_schema_version,
        package_policy_version=row.package_policy_version,
    )
    if compute_package_fingerprint(fingerprint_input) != row.package_fingerprint:
        issues.append(
            _issue(
                _IssueCode.PACKAGE_FINGERPRINT_MISMATCH,
                package_fingerprint=fp,
                owner_key_fingerprint=row.owner_key_fingerprint,
                detail="package_fingerprint does not match its recomputed content",
            )
        )

    return issues


def run_reconciliation(engine: Engine) -> ApprovedContentPackageReconciliationReport:
    issues: list[ApprovedContentPackageReconciliationIssue] = []

    preflight = preflight_approved_content_package_schema(engine)

    if not preflight["package_table_exists"]:
        return ApprovedContentPackageReconciliationReport(package_row_count=0, authority_row_count=0, issues=())

    if not table_matches_manifest(preflight["package_schema"], table_name=PACKAGE_TABLE_NAME):
        issues.append(
            _issue(
                _IssueCode.SCHEMA_MANIFEST_MISMATCH,
                detail=f"{PACKAGE_TABLE_NAME} does not match the hand-written schema manifest",
            )
        )
    if preflight["authority_table_exists"] and not table_matches_manifest(
        preflight["authority_schema"], table_name=AUTHORITY_TABLE_NAME
    ):
        issues.append(
            _issue(
                _IssueCode.SCHEMA_MANIFEST_MISMATCH,
                detail=f"{AUTHORITY_TABLE_NAME} does not match the hand-written schema manifest",
            )
        )

    for name in PACKAGE_TRIGGER_NAMES:
        body = preflight["package_triggers"].get(name)
        if body is None or body != EXPECTED_TRIGGER_BODIES[name]:
            issues.append(
                _issue(_IssueCode.IMMUTABILITY_TRIGGER_MISSING, detail=f"trigger {name} missing or tampered")
            )

    if preflight["authority_table_exists"]:
        for name in AUTHORITY_TRIGGER_NAMES:
            body = preflight["authority_triggers"].get(name)
            if body is None or body != EXPECTED_TRIGGER_BODIES[name]:
                issues.append(
                    _issue(_IssueCode.AUTHORITY_TRIGGER_MISSING, detail=f"trigger {name} missing or tampered")
                )

    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        owners = list(session.execute(select(CvDocumentArtifactOwner)).scalars())
        owner_ids = {owner.owner_key_fingerprint for owner in owners}
        owners_by_id = {owner.owner_key_fingerprint: owner for owner in owners}

        package_rows = list(session.execute(select(CvApprovedContentPackageRow)).scalars())
        package_row_count = len(package_rows)
        packages_by_fingerprint = {row.package_fingerprint: row for row in package_rows}
        for row in package_rows:
            issues.extend(_check_package_row(row, owner_ids, owners_by_id))

        authority_row_count = 0
        if preflight["authority_table_exists"]:
            authority_rows = list(session.execute(select(CvApprovedContentCurrentAuthorityRow)).scalars())
            authority_row_count = len(authority_rows)
            for row in authority_rows:
                if row.owner_key_fingerprint not in owner_ids:
                    issues.append(
                        _issue(
                            _IssueCode.OWNER_MISSING,
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            detail="authority row references an owner_key_fingerprint with no owner row",
                        )
                    )

                target = packages_by_fingerprint.get(row.current_package_fingerprint)
                if target is None:
                    issues.append(
                        _issue(
                            _IssueCode.AUTHORITY_MISSING_PACKAGE,
                            package_fingerprint=row.current_package_fingerprint,
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            detail="authority points at a package_fingerprint with no package row",
                        )
                    )
                elif target.owner_key_fingerprint != row.owner_key_fingerprint:
                    issues.append(
                        _issue(
                            _IssueCode.AUTHORITY_OWNER_MISMATCH,
                            package_fingerprint=row.current_package_fingerprint,
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            detail="authority owner does not match the target package's owner",
                        )
                    )

                if row.slot_version < 1:
                    issues.append(
                        _issue(
                            _IssueCode.INVALID_SLOT_VERSION,
                            package_fingerprint=row.current_package_fingerprint,
                            owner_key_fingerprint=row.owner_key_fingerprint,
                            detail=f"slot_version={row.slot_version} is not >= 1",
                        )
                    )

    return ApprovedContentPackageReconciliationReport(
        package_row_count=package_row_count, authority_row_count=authority_row_count, issues=tuple(issues)
    )
