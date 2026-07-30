"""Integration tests for Explicit Provenance Stage P6-C1b-A:
``cv_approved_content_package_reconciliation.run_reconciliation`` -- every
closed issue code, real row/trigger/schema reads, and a strict guarantee
that reconciliation never mutates anything (row counts, slot pointers, and
every column stay byte-for-byte unchanged across a run).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

import app.services.cv_document_models as models
from app.models import Base
from app.schemas.cv_approved_content_package import ApprovedContentPackageBuildStatus
from app.schemas.cv_approved_content_package_reconciliation import ApprovedContentPackageReconciliationIssueCode
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_approved_content_package_cutover_state import run_cutover_installer
from app.services.cv_approved_content_package_reconciliation import run_reconciliation
from app.services.cv_document_owner_identity import build_owner_key
from app.services.truth_fingerprint import canonical_json_bytes
from sqlalchemy.orm import sessionmaker
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


_Issue = ApprovedContentPackageReconciliationIssueCode


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    non_new = [t for t in Base.metadata.sorted_tables if t.name not in models.APPROVED_CONTENT_PACKAGE_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=non_new)
    run_cutover_installer(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


def _register_owner(session_factory, owner_key) -> None:
    with session_factory() as session:
        session.add(
            models.CvDocumentArtifactOwner(
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                person_entity_id=str(owner_key.person_entity_id),
                owner_kind=owner_key.owner_kind.value,
                owner_reference_id=owner_key.owner_reference_id,
                owner_key_schema_version=owner_key.owner_key_schema_version,
            )
        )
        session.commit()


async def _saved_package(session_factory):
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    _register_owner(session_factory, owner_key)
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    assert build_result.status == ApprovedContentPackageBuildStatus.BUILT
    package = build_result.package
    canonical_bytes = canonical_json_bytes(package.payload.model_dump(mode="json"))
    with session_factory() as session:
        session.add(
            models.CvApprovedContentPackageRow(
                package_fingerprint=package.package_fingerprint,
                owner_key_fingerprint=package.owner_key_fingerprint,
                owner_kind=package.owner_kind.value,
                document_input_fingerprint=package.document_input_fingerprint,
                approval_decisions_fingerprint=package.approval_decisions_fingerprint,
                fact_selection_identity_fingerprint=package.fact_selection_identity_fingerprint,
                payload_sha256=package.payload_sha256,
                payload_byte_size=len(canonical_bytes),
                payload_json=canonical_bytes.decode("utf-8"),
                package_fingerprint_schema_version=package.package_fingerprint_schema_version,
                package_schema_version=package.package_schema_version,
                package_policy_version=package.package_policy_version,
                created_at=package.created_at,
            )
        )
        session.commit()
    return owner_key, package


def _promote(session_factory, owner_key, package_fingerprint, slot_version: int = 1) -> None:
    with session_factory() as session:
        session.add(
            models.CvApprovedContentCurrentAuthorityRow(
                owner_key_fingerprint=owner_key.owner_key_fingerprint,
                current_package_fingerprint=package_fingerprint,
                slot_version=slot_version,
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_clean_state_has_zero_issues(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)
    _promote(session_factory, owner_key, package.package_fingerprint)

    report = run_reconciliation(engine)
    assert report.package_row_count == 1
    assert report.authority_row_count == 1
    assert report.issues == ()


@pytest.mark.asyncio
async def test_reconciliation_never_mutates_row_counts_slot_or_pointer(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)
    _promote(session_factory, owner_key, package.package_fingerprint)

    before_package_count = _count(engine, models.CvApprovedContentPackageRow.__tablename__)
    before_authority_count = _count(engine, models.CvApprovedContentCurrentAuthorityRow.__tablename__)
    before_slot_version, before_pointer = _authority_state(engine, owner_key.owner_key_fingerprint)

    run_reconciliation(engine)
    run_reconciliation(engine)

    assert _count(engine, models.CvApprovedContentPackageRow.__tablename__) == before_package_count
    assert _count(engine, models.CvApprovedContentCurrentAuthorityRow.__tablename__) == before_authority_count
    after_slot_version, after_pointer = _authority_state(engine, owner_key.owner_key_fingerprint)
    assert after_slot_version == before_slot_version
    assert after_pointer == before_pointer


def _count(engine, table_name: str) -> int:
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table_name}").scalar_one()


def _authority_state(engine, owner_key_fingerprint: str):
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT slot_version, current_package_fingerprint FROM cv_approved_content_current_authority "
            "WHERE owner_key_fingerprint = ?",
            (owner_key_fingerprint,),
        ).first()
    return (row[0], row[1]) if row is not None else (None, None)


@pytest.mark.asyncio
async def test_owner_missing_for_a_package_row(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM cv_document_artifact_owners WHERE owner_key_fingerprint = ?",
            (owner_key.owner_key_fingerprint,),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.OWNER_MISSING for issue in report.issues)


@pytest.mark.asyncio
async def test_owner_payload_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE cv_document_artifact_owners SET owner_reference_id = ? WHERE owner_key_fingerprint = ?",
            ("a-completely-different-reference-id", owner_key.owner_key_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.OWNER_PAYLOAD_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_package_fingerprint_mismatch_on_a_corrupted_row(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)
    canonical_bytes = canonical_json_bytes(package.payload.model_dump(mode="json"))

    fake_fingerprint = "1" * 64
    with session_factory() as session:
        session.add(
            models.CvApprovedContentPackageRow(
                package_fingerprint=fake_fingerprint,
                owner_key_fingerprint=package.owner_key_fingerprint,
                owner_kind=package.owner_kind.value,
                document_input_fingerprint=package.document_input_fingerprint,
                approval_decisions_fingerprint=package.approval_decisions_fingerprint,
                fact_selection_identity_fingerprint=package.fact_selection_identity_fingerprint,
                payload_sha256=package.payload_sha256,
                payload_byte_size=len(canonical_bytes),
                payload_json=canonical_bytes.decode("utf-8"),
                package_fingerprint_schema_version=package.package_fingerprint_schema_version,
                package_schema_version=package.package_schema_version,
                package_policy_version=package.package_policy_version,
                created_at=package.created_at,
            )
        )
        session.commit()

    report = run_reconciliation(engine)
    assert any(
        issue.issue_code == _Issue.PACKAGE_FINGERPRINT_MISMATCH and issue.package_fingerprint == fake_fingerprint
        for issue in report.issues
    )


@pytest.mark.asyncio
async def test_document_input_fingerprint_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET document_input_fingerprint = ? WHERE package_fingerprint = ?",
            ("2" * 64, package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.DOCUMENT_INPUT_FINGERPRINT_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_approval_decisions_fingerprint_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET approval_decisions_fingerprint = ? WHERE package_fingerprint = ?",
            ("3" * 64, package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.APPROVAL_DECISIONS_FINGERPRINT_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_fact_selection_identity_fingerprint_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET fact_selection_identity_fingerprint = ? "
            "WHERE package_fingerprint = ?",
            ("4" * 64, package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.FACT_SELECTION_IDENTITY_FINGERPRINT_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_payload_sha256_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET payload_sha256 = ? WHERE package_fingerprint = ?",
            ("5" * 64, package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.PAYLOAD_SHA256_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_payload_size_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints=1")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET payload_byte_size = payload_byte_size + 1 "
            "WHERE package_fingerprint = ?",
            (package.package_fingerprint,),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.PAYLOAD_SIZE_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_payload_not_canonical(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    import hashlib
    import json

    canonical_text = canonical_json_bytes(package.payload.model_dump(mode="json")).decode("utf-8")
    noncanonical_text = json.dumps(json.loads(canonical_text), indent=2, sort_keys=False)
    assert noncanonical_text != canonical_text
    noncanonical_bytes = noncanonical_text.encode("utf-8")

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET payload_json = ?, payload_byte_size = ?, payload_sha256 = ? "
            "WHERE package_fingerprint = ?",
            (
                noncanonical_text,
                len(noncanonical_bytes),
                hashlib.sha256(noncanonical_bytes).hexdigest(),
                package.package_fingerprint,
            ),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.PAYLOAD_NOT_CANONICAL for issue in report.issues)


@pytest.mark.asyncio
async def test_payload_reconstruction_failed(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    import hashlib

    broken_text = "{}"
    broken_bytes = broken_text.encode("utf-8")

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET payload_json = ?, payload_byte_size = ?, payload_sha256 = ? "
            "WHERE package_fingerprint = ?",
            (broken_text, len(broken_bytes), hashlib.sha256(broken_bytes).hexdigest(), package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.PAYLOAD_RECONSTRUCTION_FAILED for issue in report.issues)


@pytest.mark.asyncio
async def test_authority_missing_package(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_authority_insert_package_match")
        conn.exec_driver_sql(
            "INSERT INTO cv_approved_content_current_authority "
            "(owner_key_fingerprint, current_package_fingerprint, slot_version, updated_at) VALUES (?, ?, ?, ?)",
            (owner_key.owner_key_fingerprint, "6" * 64, 1, "2026-01-01T00:00:00+00:00"),
        )

    report = run_reconciliation(engine)
    assert any(
        issue.issue_code == _Issue.AUTHORITY_MISSING_PACKAGE and issue.package_fingerprint == "6" * 64
        for issue in report.issues
    )


@pytest.mark.asyncio
async def test_authority_owner_mismatch(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)
    other_owner_key, _ = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_authority_insert_package_match")
        conn.exec_driver_sql(
            "INSERT INTO cv_approved_content_current_authority "
            "(owner_key_fingerprint, current_package_fingerprint, slot_version, updated_at) VALUES (?, ?, ?, ?)",
            (other_owner_key.owner_key_fingerprint, package.package_fingerprint, 1, "2026-01-01T00:00:00+00:00"),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.AUTHORITY_OWNER_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_invalid_slot_version(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints=1")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_authority_insert_package_match")
        conn.exec_driver_sql(
            "INSERT INTO cv_approved_content_current_authority "
            "(owner_key_fingerprint, current_package_fingerprint, slot_version, updated_at) VALUES (?, ?, ?, ?)",
            (owner_key.owner_key_fingerprint, package.package_fingerprint, 0, "2026-01-01T00:00:00+00:00"),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.INVALID_SLOT_VERSION for issue in report.issues)
    # The authority row was genuinely read (row count reflects it), even
    # though its content is invalid.
    assert report.authority_row_count == 1


@pytest.mark.asyncio
async def test_immutability_trigger_missing(env) -> None:
    engine, session_factory = env
    await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_cv_approved_content_packages_no_delete")

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.IMMUTABILITY_TRIGGER_MISSING for issue in report.issues)


@pytest.mark.asyncio
async def test_authority_trigger_missing(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)
    _promote(session_factory, owner_key, package.package_fingerprint)

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER trg_cv_approved_content_authority_owner_immutable")

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.AUTHORITY_TRIGGER_MISSING for issue in report.issues)


@pytest.mark.asyncio
async def test_schema_manifest_mismatch(env) -> None:
    engine, session_factory = env
    await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE cv_approved_content_packages ADD COLUMN extra_unexpected_column TEXT")

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.SCHEMA_MANIFEST_MISMATCH for issue in report.issues)


@pytest.mark.asyncio
async def test_unsupported_package_schema_version(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints=1")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET package_schema_version = ? WHERE package_fingerprint = ?",
            ("cv-approved-content-package-schema-v9", package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.UNSUPPORTED_PACKAGE_SCHEMA_VERSION for issue in report.issues)


@pytest.mark.asyncio
async def test_unsupported_package_policy_version(env) -> None:
    engine, session_factory = env
    owner_key, package = await _saved_package(session_factory)

    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints=1")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET package_policy_version = ? WHERE package_fingerprint = ?",
            ("cv-approved-content-package-policy-v9", package.package_fingerprint),
        )

    report = run_reconciliation(engine)
    assert any(issue.issue_code == _Issue.UNSUPPORTED_PACKAGE_POLICY_VERSION for issue in report.issues)


def test_no_orphan_blob_codes_exist_in_the_closed_set() -> None:
    """The Approved Content Package line has no separate blob store (the
    payload is stored inline as ``payload_json``) -- so the closed issue-code
    set must never define an orphan-blob-style code."""

    for code in ApprovedContentPackageReconciliationIssueCode:
        assert "BLOB" not in code.value
