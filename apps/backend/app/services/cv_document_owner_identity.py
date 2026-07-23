"""Explicit Provenance Stage P6-A: document artifact owner identity.

Builds the stable ``JobArtifactOwnerKey`` that names one document artifact
owner (one job or one application). The key is derived only from
``person_entity_id``, an explicit ``ArtifactOwnerKind``, and
``owner_reference_id`` (always ``CvContentPlan.job_or_application_id``,
never an independently supplied string) -- never a URL, employer name, job
title, filename, filesystem path, or content-plan fingerprint.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from app.schemas.cv_document_artifact import ArtifactOwnerKind, JobArtifactOwnerKey, OWNER_KEY_SCHEMA_VERSION
from app.services.truth_fingerprint import canonical_json_bytes


OWNER_KEY_FINGERPRINT_VERSION = "cv-document-owner-key-v1"


def compute_owner_key_fingerprint(
    *,
    person_entity_id: UUID,
    owner_kind: ArtifactOwnerKind,
    owner_reference_id: str,
    owner_key_schema_version: str = OWNER_KEY_SCHEMA_VERSION,
) -> str:
    content = {
        "fingerprint_version": OWNER_KEY_FINGERPRINT_VERSION,
        "content": {
            "person_entity_id": str(person_entity_id),
            "owner_kind": owner_kind.value,
            "owner_reference_id": owner_reference_id,
            "owner_key_schema_version": owner_key_schema_version,
        },
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def build_owner_key(
    *,
    person_entity_id: UUID,
    owner_kind: ArtifactOwnerKind,
    owner_reference_id: str,
) -> JobArtifactOwnerKey:
    """Build the owner key for one document artifact owner.

    ``owner_reference_id`` must always be the caller's live
    ``CvContentPlan.job_or_application_id`` -- passing anything else (a
    URL, a filename, a path) breaks the owner-identity guarantee this key
    exists to provide.
    """

    fingerprint = compute_owner_key_fingerprint(
        person_entity_id=person_entity_id,
        owner_kind=owner_kind,
        owner_reference_id=owner_reference_id,
    )
    return JobArtifactOwnerKey(
        person_entity_id=person_entity_id,
        owner_kind=owner_kind,
        owner_reference_id=owner_reference_id,
        owner_key_schema_version=OWNER_KEY_SCHEMA_VERSION,
        owner_key_fingerprint=fingerprint,
    )
