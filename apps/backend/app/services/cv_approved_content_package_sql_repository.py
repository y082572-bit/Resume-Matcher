"""Explicit Provenance Stage P6-C1b-A: the synchronous SQL
``ApprovedContentPackageRepository`` implementation.

Never trusts a domain ``ApprovedContentPackage`` at face value just because
it was produced by ``cv_approved_content_package_builder`` -- every save and
every read independently recomputes the canonical payload bytes, the
payload SHA-256/byte size, every intermediate fingerprint (document input,
approval decisions, fact selection identity), and the final
``package_fingerprint`` before ever trusting a stored or incoming row.

Ordering discipline, mirroring ``cv_document_final_confirmed_pdf_sql_repository.py``:

1. Independently recompute every fingerprint before ever trusting a
   caller-supplied or stored field.
2. Open one ``Session``/transaction per mutating call. A race between two
   concurrent first-writers of the same content-addressed
   ``package_fingerprint`` (or two concurrent authority promotions for the
   same owner) is resolved by closing the losing transaction's ``Session``
   and opening a brand-new one to re-read and report the exact winning
   row/current state -- never by reusing the raised-on ``Session`` and never
   by retrying the write.
3. This repository never raises a raw SQLAlchemy exception or a Pydantic
   ``ValidationError`` to its own caller for a storage-integrity problem --
   every technical failure of that kind is mapped onto one of this module's
   closed result statuses instead. A genuine programmer error (a caller
   passing the wrong type, for instance) is never caught here and always
   propagates.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import DatabaseError, DataError, IntegrityError, OperationalError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from app.schemas.cv_approved_content_package import (
    PAYLOAD_LIMIT_BYTES,
    ApprovedContentAuthorityPromotionResult,
    ApprovedContentAuthorityPromotionStatus,
    ApprovedContentAuthorityReadResult,
    ApprovedContentAuthorityReadStatus,
    ApprovedContentCurrentAuthority,
    ApprovedContentPackage,
    ApprovedContentPackagePayload,
    ApprovedContentPackageReadResult,
    ApprovedContentPackageReadStatus,
    ApprovedContentPackageSaveResult,
    ApprovedContentPackageSaveStatus,
)
from app.schemas.cv_document_artifact import ArtifactOwnerKind, JobArtifactOwnerKey
from app.services.cv_approved_content_package_builder import (
    build_fingerprint_input,
    compute_approval_decisions_fingerprint,
    compute_fact_selection_identity_fingerprint,
    compute_package_fingerprint,
)
from app.services.cv_content_approval_replay import compute_approved_content_replay_input_fingerprint
from app.services.cv_document_input_validation import compute_document_input_fingerprint
from app.services.cv_document_models import (
    CvApprovedContentCurrentAuthorityRow,
    CvApprovedContentPackageRow,
    CvDocumentArtifactOwner,
)
from app.services.truth_fingerprint import canonical_json_bytes


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _MetadataConflict(Exception):
    """Internal-only: a stored row (or an incoming domain object, before it
    is ever persisted) failed independent re-verification. Always caught
    within the same method and mapped onto a ``*_METADATA_CONFLICT``/
    ``*_FINGERPRINT_MISMATCH`` status -- never surfaced to a caller."""


class _ConcurrentSlotMutation(Exception):
    """Internal-only: the authority row's optimistic ``slot_version`` no
    longer matched at UPDATE time. Always caught within the same method and
    converted into ``ApprovedContentAuthorityPromotionStatus.STALE_REVISION``."""


class ApprovedContentPackageSqlRepository:
    """Synchronous SQL implementation of the ``ApprovedContentPackageRepository``
    Protocol. Never installs its own tables or triggers -- see
    ``cv_approved_content_package_cutover_state.py`` for that; this class
    assumes the cutover is already ``READY``."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # -- independent re-derivation (fail-closed) --------------------------------

    def _reverify_payload(
        self, payload: ApprovedContentPackagePayload, *, expected_payload_sha256: str, expected_payload_byte_size: int
    ) -> bytes:
        payload_bytes = canonical_json_bytes(payload.model_dump(mode="json"))
        if len(payload_bytes) > PAYLOAD_LIMIT_BYTES:
            raise _MetadataConflict("payload exceeds PAYLOAD_LIMIT_BYTES on re-derivation")
        if len(payload_bytes) != expected_payload_byte_size:
            raise _MetadataConflict("payload byte size does not match its recomputed size")
        if hashlib.sha256(payload_bytes).hexdigest() != expected_payload_sha256:
            raise _MetadataConflict("payload_sha256 does not match a recomputed hash of the payload")
        return payload_bytes

    def _reverify_fingerprint_chain(
        self,
        payload: ApprovedContentPackagePayload,
        *,
        owner_key_fingerprint: str,
        owner_kind: str,
        payload_sha256: str,
        document_input_fingerprint: str,
        approval_decisions_fingerprint: str,
        fact_selection_identity_fingerprint: str,
        package_fingerprint_schema_version: str,
        package_schema_version: str,
        package_policy_version: str,
        expected_package_fingerprint: str,
    ) -> None:
        result = payload.approved_content_result
        plan = payload.source_content_plan

        current_p55_replay_input_fingerprint = compute_approved_content_replay_input_fingerprint(
            payload.current_p55_replay_input
        )
        recomputed_document_input_fingerprint = compute_document_input_fingerprint(
            approved_content_result_fingerprint=result.result_fingerprint,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            owner_kind=owner_kind,
            owner_key_fingerprint=owner_key_fingerprint,
            current_p55_replay_input_fingerprint=current_p55_replay_input_fingerprint,
        )
        if recomputed_document_input_fingerprint != document_input_fingerprint:
            raise _MetadataConflict("document_input_fingerprint does not match its recomputed content")

        recomputed_approval_decisions_fingerprint = compute_approval_decisions_fingerprint(
            payload.approval_decisions,
            expected_semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
        )
        if (
            recomputed_approval_decisions_fingerprint is None
            or recomputed_approval_decisions_fingerprint != approval_decisions_fingerprint
        ):
            raise _MetadataConflict("approval_decisions_fingerprint does not match its recomputed content")

        recomputed_fact_selection_identity_fingerprint = compute_fact_selection_identity_fingerprint(
            plan.source_decision_fingerprints
        )
        if (
            recomputed_fact_selection_identity_fingerprint is None
            or recomputed_fact_selection_identity_fingerprint != fact_selection_identity_fingerprint
        ):
            raise _MetadataConflict("fact_selection_identity_fingerprint does not match its recomputed content")

        fingerprint_input = build_fingerprint_input(
            owner_key_fingerprint=owner_key_fingerprint,
            owner_kind=ArtifactOwnerKind(owner_kind),
            document_input_fingerprint=document_input_fingerprint,
            approved_content_result_fingerprint=result.result_fingerprint,
            content_plan_fingerprint=plan.content_plan_fingerprint,
            semantic_validation_result_fingerprint=result.semantic_validation_result_fingerprint,
            approval_decisions_fingerprint=approval_decisions_fingerprint,
            fact_selection_identity_fingerprint=fact_selection_identity_fingerprint,
            role_strategy_context_fingerprint=plan.role_strategy_context_fingerprint,
            strategy_selection_result_fingerprint=plan.strategy_selection_result_fingerprint,
            strategy_ranking_input_fingerprint=plan.strategy_ranking_input_fingerprint,
            strategy_integration_policy_version=plan.strategy_integration_policy_version,
            p5a_result_fingerprint=result.p5a_result_fingerprint,
            p5b_result_fingerprint=result.p5b_result_fingerprint,
            payload_sha256=payload_sha256,
            document_input_schema_version=payload.document_input_schema_version,
            document_input_policy_version=payload.document_input_policy_version,
            package_schema_version=package_schema_version,
            package_policy_version=package_policy_version,
        )
        if fingerprint_input.package_fingerprint_schema_version != package_fingerprint_schema_version:
            raise _MetadataConflict("package_fingerprint_schema_version does not match the frozen constant")
        recomputed_package_fingerprint = compute_package_fingerprint(fingerprint_input)
        if recomputed_package_fingerprint != expected_package_fingerprint:
            raise _MetadataConflict("package_fingerprint does not match its recomputed content")

    def _row_to_domain_package(self, row: CvApprovedContentPackageRow) -> ApprovedContentPackage:
        try:
            parsed = json.loads(row.payload_json)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise _MetadataConflict("stored payload_json failed to parse as JSON") from exc

        try:
            payload = ApprovedContentPackagePayload.model_validate(parsed)
        except ValidationError as exc:
            raise _MetadataConflict("stored payload_json failed domain reconstruction") from exc

        recanonicalized = canonical_json_bytes(payload.model_dump(mode="json"))
        if recanonicalized != row.payload_json.encode("utf-8"):
            raise _MetadataConflict(
                "stored payload_json is not the canonical serialization of its own parsed value"
            )
        if len(recanonicalized) != row.payload_byte_size:
            raise _MetadataConflict("payload_byte_size does not match the stored payload's byte length")

        if hashlib.sha256(recanonicalized).hexdigest() != row.payload_sha256:
            raise _MetadataConflict("payload_sha256 does not match a recomputed hash of the stored payload")

        self._reverify_fingerprint_chain(
            payload,
            owner_key_fingerprint=row.owner_key_fingerprint,
            owner_kind=row.owner_kind,
            payload_sha256=row.payload_sha256,
            document_input_fingerprint=row.document_input_fingerprint,
            approval_decisions_fingerprint=row.approval_decisions_fingerprint,
            fact_selection_identity_fingerprint=row.fact_selection_identity_fingerprint,
            package_fingerprint_schema_version=row.package_fingerprint_schema_version,
            package_schema_version=row.package_schema_version,
            package_policy_version=row.package_policy_version,
            expected_package_fingerprint=row.package_fingerprint,
        )

        try:
            return ApprovedContentPackage(
                package_fingerprint=row.package_fingerprint,
                owner_key_fingerprint=row.owner_key_fingerprint,
                owner_kind=ArtifactOwnerKind(row.owner_kind),
                document_input_fingerprint=row.document_input_fingerprint,
                approval_decisions_fingerprint=row.approval_decisions_fingerprint,
                fact_selection_identity_fingerprint=row.fact_selection_identity_fingerprint,
                payload_sha256=row.payload_sha256,
                payload_byte_size=row.payload_byte_size,
                payload=payload,
                package_fingerprint_schema_version=row.package_fingerprint_schema_version,
                package_schema_version=row.package_schema_version,
                package_policy_version=row.package_policy_version,
                created_at=row.created_at,
            )
        except ValidationError as exc:
            raise _MetadataConflict("stored package row failed domain reconstruction") from exc

    # -- save ---------------------------------------------------------------------

    def _reread_after_race(self, package_fingerprint: str) -> ApprovedContentPackageSaveResult:
        """Always opens a brand-new ``Session`` -- the one that raised
        ``IntegrityError`` is already fully exited by the time this runs."""

        with self._session_factory() as fresh_session:
            row = fresh_session.get(CvApprovedContentPackageRow, package_fingerprint)
            if row is None:
                return ApprovedContentPackageSaveResult(
                    status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT
                )
            try:
                winner = self._row_to_domain_package(row)
            except _MetadataConflict:
                return ApprovedContentPackageSaveResult(
                    status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT
                )
            return ApprovedContentPackageSaveResult(
                status=ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL, package=winner
            )

    def save_package(
        self, *, owner_key: JobArtifactOwnerKey, package: ApprovedContentPackage
    ) -> ApprovedContentPackageSaveResult:
        if package.payload_byte_size > PAYLOAD_LIMIT_BYTES:
            return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.PAYLOAD_TOO_LARGE)

        try:
            payload_bytes = self._reverify_payload(
                package.payload,
                expected_payload_sha256=package.payload_sha256,
                expected_payload_byte_size=package.payload_byte_size,
            )
        except _MetadataConflict:
            return ApprovedContentPackageSaveResult(
                status=ApprovedContentPackageSaveStatus.PAYLOAD_FINGERPRINT_MISMATCH
            )

        try:
            self._reverify_fingerprint_chain(
                package.payload,
                owner_key_fingerprint=package.owner_key_fingerprint,
                owner_kind=package.owner_kind.value,
                payload_sha256=package.payload_sha256,
                document_input_fingerprint=package.document_input_fingerprint,
                approval_decisions_fingerprint=package.approval_decisions_fingerprint,
                fact_selection_identity_fingerprint=package.fact_selection_identity_fingerprint,
                package_fingerprint_schema_version=package.package_fingerprint_schema_version,
                package_schema_version=package.package_schema_version,
                package_policy_version=package.package_policy_version,
                expected_package_fingerprint=package.package_fingerprint,
            )
        except _MetadataConflict:
            return ApprovedContentPackageSaveResult(
                status=ApprovedContentPackageSaveStatus.PACKAGE_FINGERPRINT_MISMATCH
            )

        if package.owner_key_fingerprint != owner_key.owner_key_fingerprint:
            return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.OWNER_MISMATCH)

        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        owner_row = session.get(CvDocumentArtifactOwner, owner_key.owner_key_fingerprint)
                        if owner_row is None:
                            return ApprovedContentPackageSaveResult(
                                status=ApprovedContentPackageSaveStatus.OWNER_NOT_FOUND
                            )

                        existing = session.get(CvApprovedContentPackageRow, package.package_fingerprint)
                        if existing is not None:
                            try:
                                existing_domain = self._row_to_domain_package(existing)
                            except _MetadataConflict:
                                return ApprovedContentPackageSaveResult(
                                    status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT
                                )
                            # ``package.package_fingerprint`` was already
                            # independently re-verified against its own
                            # content above, and ``existing_domain`` was
                            # just independently re-verified against its own
                            # stored content too -- two values that both
                            # genuinely hash to the same package_fingerprint
                            # are content-identical by SHA-256 collision
                            # resistance in every field except ``created_at``
                            # (never a fingerprint input), so that field
                            # alone is excluded from the comparison.
                            if existing_domain.model_dump(exclude={"created_at"}) == package.model_dump(
                                exclude={"created_at"}
                            ):
                                return ApprovedContentPackageSaveResult(
                                    status=ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL,
                                    package=existing_domain,
                                )
                            return ApprovedContentPackageSaveResult(
                                status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT
                            )

                        session.add(
                            CvApprovedContentPackageRow(
                                package_fingerprint=package.package_fingerprint,
                                owner_key_fingerprint=package.owner_key_fingerprint,
                                owner_kind=package.owner_kind.value,
                                document_input_fingerprint=package.document_input_fingerprint,
                                approval_decisions_fingerprint=package.approval_decisions_fingerprint,
                                fact_selection_identity_fingerprint=package.fact_selection_identity_fingerprint,
                                payload_sha256=package.payload_sha256,
                                payload_byte_size=package.payload_byte_size,
                                payload_json=payload_bytes.decode("utf-8"),
                                package_fingerprint_schema_version=package.package_fingerprint_schema_version,
                                package_schema_version=package.package_schema_version,
                                package_policy_version=package.package_policy_version,
                                created_at=package.created_at,
                            )
                        )
                        session.flush()
                except IntegrityError:
                    return self._reread_after_race(package.package_fingerprint)

                return ApprovedContentPackageSaveResult(
                    status=ApprovedContentPackageSaveStatus.SAVED, package=package.model_copy(deep=True)
                )
        except OperationalError:
            return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.STORAGE_UNAVAILABLE)
        except DataError:
            return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT)
        except DatabaseError:
            # ``DatabaseError`` is a broad catch-all for every remaining
            # DBAPI-level failure not already matched above (it is never
            # proven to be a metadata conflict here).
            return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.STORAGE_UNAVAILABLE)
        except StatementError:
            # ``StatementError`` is ``DatabaseError``'s own superclass, so
            # this branch is only ever reached for a non-``DatabaseError``
            # statement-construction failure (invalid persisted metadata
            # that never even reached the DBAPI driver).
            return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT)

    # -- read ---------------------------------------------------------------------

    def get_package_by_fingerprint(self, package_fingerprint: str) -> ApprovedContentPackageReadResult:
        try:
            with self._session_factory() as session:
                row = session.get(CvApprovedContentPackageRow, package_fingerprint)
                if row is None:
                    return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.NOT_FOUND)
                package = self._row_to_domain_package(row)
                return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.FOUND, package=package)
        except _MetadataConflict:
            return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT)
        except OperationalError:
            return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.STORAGE_UNAVAILABLE)
        except DataError:
            return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT)
        except DatabaseError:
            return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.STORAGE_UNAVAILABLE)
        except StatementError:
            return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.STORAGE_METADATA_CONFLICT)

    def read_current_authority(self, owner_key: JobArtifactOwnerKey) -> ApprovedContentAuthorityReadResult:
        try:
            with self._session_factory() as session:
                row = session.get(CvApprovedContentCurrentAuthorityRow, owner_key.owner_key_fingerprint)
                if row is None:
                    return ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.NOT_FOUND)
                authority = ApprovedContentCurrentAuthority(
                    package_fingerprint=row.current_package_fingerprint, slot_version=row.slot_version
                )
                return ApprovedContentAuthorityReadResult(
                    status=ApprovedContentAuthorityReadStatus.FOUND, authority=authority
                )
        except OperationalError:
            return ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.STORAGE_UNAVAILABLE)
        except DataError:
            return ApprovedContentAuthorityReadResult(
                status=ApprovedContentAuthorityReadStatus.STORAGE_METADATA_CONFLICT
            )
        except DatabaseError:
            return ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.STORAGE_UNAVAILABLE)
        except StatementError:
            return ApprovedContentAuthorityReadResult(
                status=ApprovedContentAuthorityReadStatus.STORAGE_METADATA_CONFLICT
            )

    # -- authority CAS --------------------------------------------------------------

    def _reread_authority_after_race(self, owner_key: JobArtifactOwnerKey) -> ApprovedContentCurrentAuthority | None:
        with self._session_factory() as fresh_session:
            row = fresh_session.get(CvApprovedContentCurrentAuthorityRow, owner_key.owner_key_fingerprint)
            if row is None:
                return None
            return ApprovedContentCurrentAuthority(
                package_fingerprint=row.current_package_fingerprint, slot_version=row.slot_version
            )

    def promote_current_authority(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        package_fingerprint: str,
        expected_previous_slot_version: int | None,
    ) -> ApprovedContentAuthorityPromotionResult:
        try:
            with self._session_factory() as session:
                try:
                    with session.begin():
                        package_row = session.get(CvApprovedContentPackageRow, package_fingerprint)
                        if package_row is None:
                            return ApprovedContentAuthorityPromotionResult(
                                status=ApprovedContentAuthorityPromotionStatus.PACKAGE_NOT_FOUND
                            )
                        if package_row.owner_key_fingerprint != owner_key.owner_key_fingerprint:
                            return ApprovedContentAuthorityPromotionResult(
                                status=ApprovedContentAuthorityPromotionStatus.OWNER_PACKAGE_MISMATCH
                            )

                        current_row = session.get(
                            CvApprovedContentCurrentAuthorityRow, owner_key.owner_key_fingerprint
                        )
                        current_slot_version = current_row.slot_version if current_row is not None else None
                        if current_slot_version != expected_previous_slot_version:
                            fresh_authority = (
                                ApprovedContentCurrentAuthority(
                                    package_fingerprint=current_row.current_package_fingerprint,
                                    slot_version=current_row.slot_version,
                                )
                                if current_row is not None
                                else None
                            )
                            return ApprovedContentAuthorityPromotionResult(
                                status=ApprovedContentAuthorityPromotionStatus.STALE_REVISION,
                                authority=fresh_authority,
                            )

                        if current_row is not None and current_row.current_package_fingerprint == package_fingerprint:
                            return ApprovedContentAuthorityPromotionResult(
                                status=ApprovedContentAuthorityPromotionStatus.ALREADY_CURRENT,
                                authority=ApprovedContentCurrentAuthority(
                                    package_fingerprint=package_fingerprint, slot_version=current_row.slot_version
                                ),
                            )

                        now = _utcnow_iso()
                        if current_row is None:
                            session.add(
                                CvApprovedContentCurrentAuthorityRow(
                                    owner_key_fingerprint=owner_key.owner_key_fingerprint,
                                    current_package_fingerprint=package_fingerprint,
                                    slot_version=1,
                                    updated_at=now,
                                )
                            )
                            session.flush()
                            next_slot_version = 1
                        else:
                            # Captured *before* the UPDATE executes: SQLAlchemy's
                            # ORM-enabled bulk UPDATE synchronizes already-loaded
                            # matching objects' attributes in-place immediately, so
                            # re-reading current_row.slot_version *after*
                            # session.execute() below would silently observe the
                            # row's *new* value, not the expected-previous one.
                            expected_slot_version = current_row.slot_version
                            result = session.execute(
                                update(CvApprovedContentCurrentAuthorityRow)
                                .where(
                                    CvApprovedContentCurrentAuthorityRow.owner_key_fingerprint
                                    == owner_key.owner_key_fingerprint,
                                    CvApprovedContentCurrentAuthorityRow.slot_version == expected_slot_version,
                                )
                                .values(
                                    current_package_fingerprint=package_fingerprint,
                                    slot_version=expected_slot_version + 1,
                                    updated_at=now,
                                )
                            )
                            if result.rowcount != 1:
                                raise _ConcurrentSlotMutation()
                            next_slot_version = expected_slot_version + 1
                except (IntegrityError, _ConcurrentSlotMutation):
                    fresh_authority = self._reread_authority_after_race(owner_key)
                    return ApprovedContentAuthorityPromotionResult(
                        status=ApprovedContentAuthorityPromotionStatus.STALE_REVISION, authority=fresh_authority
                    )

                return ApprovedContentAuthorityPromotionResult(
                    status=ApprovedContentAuthorityPromotionStatus.PROMOTED,
                    authority=ApprovedContentCurrentAuthority(
                        package_fingerprint=package_fingerprint, slot_version=next_slot_version
                    ),
                )
        except OperationalError:
            return ApprovedContentAuthorityPromotionResult(
                status=ApprovedContentAuthorityPromotionStatus.STORAGE_UNAVAILABLE
            )
        except DataError:
            return ApprovedContentAuthorityPromotionResult(
                status=ApprovedContentAuthorityPromotionStatus.STORAGE_METADATA_CONFLICT
            )
        except DatabaseError:
            return ApprovedContentAuthorityPromotionResult(
                status=ApprovedContentAuthorityPromotionStatus.STORAGE_UNAVAILABLE
            )
        except StatementError:
            return ApprovedContentAuthorityPromotionResult(
                status=ApprovedContentAuthorityPromotionStatus.STORAGE_METADATA_CONFLICT
            )
