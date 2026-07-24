"""Explicit Provenance Stage P6-B1 SQL storage addendum: shared owner and
blob invariants (H1).

``CvDocumentArtifactSqlRepository`` (P6-B1 base) and
``FinalDocxSnapshotSqlRepository`` (this addendum) enforce the exact same
owner-upsert and blob-metadata-reuse rules. This module is the single place
either implements them -- both call these same functions, never two
independently maintained copies -- so a future change to one invariant can
never accidentally apply to only one repository. Extracted verbatim from
``cv_document_sql_repository.py``; the P6-B1 repository's public methods
and semantics are unchanged, and its own private ``_ensure_owner``/
``_row_to_owner_key``/``_require_owner_key``/``_ensure_blob_row`` methods
remain in place as thin delegating wrappers so existing monkeypatch-based
tests keep working unmodified.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas.cv_document_artifact import ArtifactOwnerKind, JobArtifactOwnerKey
from app.schemas.cv_document_storage import CvDocumentStorageErrorCode, DocumentBlobStorageStatus
from app.services.cv_document_blob_store import CvDocumentBlobStore, CvDocumentStorageError
from app.services.cv_document_models import CvDocumentArtifactOwner, CvDocumentBlob
from app.services.cv_document_owner_identity import compute_owner_key_fingerprint


def ensure_owner(session: Session, owner_key: JobArtifactOwnerKey) -> None:
    """Idempotent owner upsert.

    An absent ``owner_key_fingerprint`` is inserted fresh. An existing row
    under that fingerprint must match every constituent field exactly --
    any mismatch fails closed with ``OWNER_IDENTITY_MISMATCH`` rather than
    silently rebinding the existing row to different identity fields.
    """

    recomputed = compute_owner_key_fingerprint(
        person_entity_id=owner_key.person_entity_id,
        owner_kind=owner_key.owner_kind,
        owner_reference_id=owner_key.owner_reference_id,
        owner_key_schema_version=owner_key.owner_key_schema_version,
    )
    if recomputed != owner_key.owner_key_fingerprint:
        raise CvDocumentStorageError.of(
            CvDocumentStorageErrorCode.OWNER_IDENTITY_MISMATCH,
            "owner_key.owner_key_fingerprint does not match a recomputed fingerprint",
        )

    existing = session.get(CvDocumentArtifactOwner, recomputed)
    if existing is None:
        session.add(
            CvDocumentArtifactOwner(
                owner_key_fingerprint=recomputed,
                person_entity_id=str(owner_key.person_entity_id),
                owner_kind=owner_key.owner_kind.value,
                owner_reference_id=owner_key.owner_reference_id,
                owner_key_schema_version=owner_key.owner_key_schema_version,
            )
        )
        session.flush()
        return

    if (
        existing.person_entity_id != str(owner_key.person_entity_id)
        or existing.owner_kind != owner_key.owner_kind.value
        or existing.owner_reference_id != owner_key.owner_reference_id
        or existing.owner_key_schema_version != owner_key.owner_key_schema_version
    ):
        raise CvDocumentStorageError.of(
            CvDocumentStorageErrorCode.OWNER_IDENTITY_MISMATCH,
            "an existing owner row with this fingerprint has different constituent fields",
        )


def row_to_owner_key(owner_row: CvDocumentArtifactOwner) -> JobArtifactOwnerKey:
    return JobArtifactOwnerKey(
        person_entity_id=UUID(owner_row.person_entity_id),
        owner_kind=ArtifactOwnerKind(owner_row.owner_kind),
        owner_reference_id=owner_row.owner_reference_id,
        owner_key_schema_version=owner_row.owner_key_schema_version,
        owner_key_fingerprint=owner_row.owner_key_fingerprint,
    )


def require_owner_key(session: Session, owner_key_fingerprint: str) -> JobArtifactOwnerKey:
    owner_row = session.get(CvDocumentArtifactOwner, owner_key_fingerprint)
    if owner_row is None:
        raise CvDocumentStorageError.of(
            CvDocumentStorageErrorCode.OWNER_NOT_FOUND,
            f"no owner row for owner_key_fingerprint={owner_key_fingerprint}",
        )
    return row_to_owner_key(owner_row)


def ensure_blob_row(
    session: Session,
    blob_store: CvDocumentBlobStore,
    *,
    blob_sha256: str,
    byte_size: int,
    media_type: str,
) -> None:
    """Ensure a ``cv_document_blobs`` metadata row exists for
    ``blob_sha256`` -- never overwrite an existing row, whatever it
    currently claims. An existing row that disagrees on
    ``byte_size``/``storage_locator``/``media_type`` fails closed with
    ``STORAGE_METADATA_CONFLICT``.
    """

    locator = blob_store.locator_for(blob_sha256)
    existing = session.get(CvDocumentBlob, blob_sha256)
    if existing is None:
        session.add(
            CvDocumentBlob(
                blob_sha256=blob_sha256,
                byte_size=byte_size,
                media_type=media_type,
                storage_locator=locator,
                storage_status=DocumentBlobStorageStatus.OK.value,
            )
        )
        session.flush()
        return
    if (
        existing.byte_size != byte_size
        or existing.storage_locator != locator
        or existing.media_type != media_type
    ):
        raise CvDocumentStorageError.of(
            CvDocumentStorageErrorCode.STORAGE_METADATA_CONFLICT,
            "existing cv_document_blobs row does not match the freshly written blob's size/locator/media_type",
        )
