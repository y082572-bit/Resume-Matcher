"""Unit tests for Explicit Provenance Stage P6-A: ``JobArtifactOwnerKey``
identity. Owner identity must depend only on
(person_entity_id, owner_kind, owner_reference_id) -- never on a
content-plan fingerprint, a URL, an employer name, a job title, a
filename, or a filesystem path.
"""

from __future__ import annotations

from uuid import uuid4

from app.schemas.cv_document_artifact import ArtifactOwnerKind
from app.services.cv_document_owner_identity import build_owner_key, compute_owner_key_fingerprint


def test_job_and_application_same_string_id_differ() -> None:
    person_id = uuid4()
    reference_id = "shared-string-id-123"

    job_key = build_owner_key(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )
    application_key = build_owner_key(
        person_entity_id=person_id,
        owner_kind=ArtifactOwnerKind.APPLICATION,
        owner_reference_id=reference_id,
    )

    assert job_key.owner_key_fingerprint != application_key.owner_key_fingerprint


def test_owner_reference_id_participates_in_fingerprint() -> None:
    person_id = uuid4()

    key_a = build_owner_key(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-a"
    )
    key_b = build_owner_key(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-b"
    )

    assert key_a.owner_reference_id == "job-a"
    assert key_a.owner_key_fingerprint != key_b.owner_key_fingerprint


def test_owner_identity_unaffected_by_content_plan_changes() -> None:
    """Two content-plan versions of the very same owner (different
    ``content_plan_fingerprint``) must resolve to the exact same owner
    key -- the owner key never takes a content-plan fingerprint as input."""

    person_id = uuid4()
    reference_id = "job-stable-owner"

    key_v1 = build_owner_key(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )
    key_v2 = build_owner_key(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )

    assert key_v1.owner_key_fingerprint == key_v2.owner_key_fingerprint
    assert key_v1 == key_v2


def test_person_entity_id_change_changes_owner_identity() -> None:
    reference_id = "job-x"
    key_a = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )
    key_b = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id=reference_id
    )

    assert key_a.owner_key_fingerprint != key_b.owner_key_fingerprint


def test_owner_key_fingerprint_is_deterministic() -> None:
    person_id = uuid4()
    fp1 = compute_owner_key_fingerprint(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.APPLICATION, owner_reference_id="app-1"
    )
    fp2 = compute_owner_key_fingerprint(
        person_entity_id=person_id, owner_kind=ArtifactOwnerKind.APPLICATION, owner_reference_id="app-1"
    )
    assert fp1 == fp2
    assert len(fp1) == 64


def test_owner_key_model_is_closed() -> None:
    key = build_owner_key(
        person_entity_id=uuid4(), owner_kind=ArtifactOwnerKind.JOB, owner_reference_id="job-closed"
    )
    assert key.model_dump(mode="json").keys() == {
        "person_entity_id",
        "owner_kind",
        "owner_reference_id",
        "owner_key_schema_version",
        "owner_key_fingerprint",
    }
