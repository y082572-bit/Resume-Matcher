"""Unit tests for Explicit Provenance Stage P6-C1b-A: canonical JSON
serialization of ``ApprovedContentPackagePayload``, and the storage layer's
enforcement of exact byte size / SHA-256 / canonical-form invariants.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
import app.services.cv_document_models as models
from app.schemas.cv_approved_content_package import (
    ApprovedContentPackageReadStatus,
    ApprovedContentPackageSaveStatus,
)
from app.services.cv_approved_content_package_builder import build_approved_content_package
from app.services.cv_approved_content_package_cutover_state import run_cutover_installer
from app.services.cv_approved_content_package_sql_repository import ApprovedContentPackageSqlRepository
from app.services.cv_document_owner_identity import build_owner_key
from app.services.truth_fingerprint import canonical_json_bytes
from tests.unit.test_cv_document_input_validation import build_document_input, build_ready_integrated_context


def _make_engine_and_repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    tables = [t for t in Base.metadata.sorted_tables if t.name not in models.APPROVED_CONTENT_PACKAGE_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=tables)
    run_cutover_installer(engine)
    session_factory = sessionmaker(bind=engine)
    return engine, session_factory, ApprovedContentPackageSqlRepository(session_factory)


async def _build_real_package():
    ctx = await build_ready_integrated_context()
    document_input = build_document_input(ctx)
    plan = ctx["plan"]
    owner_key = build_owner_key(
        person_entity_id=plan.person_entity_id,
        owner_kind=document_input.owner_kind,
        owner_reference_id=plan.job_or_application_id,
    )
    build_result = build_approved_content_package(owner_key=owner_key, document_input=document_input)
    return owner_key, build_result.package


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


def test_canonical_bytes_are_deterministic() -> None:
    value = {"b": 1, "a": [3, 2, 1], "c": {"z": True, "y": None}}
    first = canonical_json_bytes(value)
    second = canonical_json_bytes(value)
    assert first == second


def test_reordered_dict_produces_identical_canonical_bytes() -> None:
    value_a = {"alpha": 1, "beta": {"x": 1, "y": 2}, "gamma": [1, 2, 3]}
    value_b = {"gamma": [1, 2, 3], "alpha": 1, "beta": {"y": 2, "x": 1}}
    assert canonical_json_bytes(value_a) == canonical_json_bytes(value_b)


@pytest.mark.asyncio
async def test_payload_byte_size_is_exact() -> None:
    _, package = await _build_real_package()
    exact_bytes = canonical_json_bytes(package.payload.model_dump(mode="json"))
    assert package.payload_byte_size == len(exact_bytes)
    assert package.payload_sha256 == hashlib.sha256(exact_bytes).hexdigest()


@pytest.mark.asyncio
async def test_payload_too_large_is_rejected_at_the_model_boundary() -> None:
    from pydantic import ValidationError

    from app.schemas.cv_approved_content_package import ApprovedContentPackage

    _, package = await _build_real_package()
    # ``model_copy(update=...)`` never re-runs field validators -- only a
    # real constructor call proves the ``le=PAYLOAD_LIMIT_BYTES`` bound is
    # actually enforced.
    with pytest.raises(ValidationError):
        ApprovedContentPackage(**{**package.model_dump(), "payload_byte_size": 1_048_576 + 1})


@pytest.mark.asyncio
async def test_payload_sha256_mismatch_is_rejected_on_save() -> None:
    _, session_factory, repo = _make_engine_and_repo()
    owner_key, package = await _build_real_package()
    _register_owner(session_factory, owner_key)

    tampered = package.model_copy(update={"payload_sha256": "b" * 64})
    result = repo.save_package(owner_key=owner_key, package=tampered)
    assert result.status == ApprovedContentPackageSaveStatus.PAYLOAD_FINGERPRINT_MISMATCH


@pytest.mark.asyncio
async def test_semantically_valid_noncanonical_stored_json_is_rejected_on_read() -> None:
    """A stored ``payload_json`` that ``json.loads`` parses into the exact
    same semantic value, but whose raw bytes are not the canonical
    serialization (extra whitespace here), must never be silently accepted
    -- it is always ``STORAGE_METADATA_CONFLICT``."""

    engine, session_factory, repo = _make_engine_and_repo()
    owner_key, package = await _build_real_package()
    _register_owner(session_factory, owner_key)

    saved = repo.save_package(owner_key=owner_key, package=package)
    assert saved.status == ApprovedContentPackageSaveStatus.SAVED

    canonical_text = canonical_json_bytes(package.payload.model_dump(mode="json")).decode("utf-8")
    reparsed = json.loads(canonical_text)
    noncanonical_text = json.dumps(reparsed, indent=2, sort_keys=False)
    assert noncanonical_text != canonical_text
    assert json.loads(noncanonical_text) == reparsed

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS trg_cv_approved_content_packages_no_update")
        conn.exec_driver_sql(
            "UPDATE cv_approved_content_packages SET payload_json = ?, payload_byte_size = ? WHERE package_fingerprint = ?",
            (noncanonical_text, len(noncanonical_text.encode("utf-8")), package.package_fingerprint),
        )

    result = repo.get_package_by_fingerprint(package.package_fingerprint)
    assert result.status == ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT
