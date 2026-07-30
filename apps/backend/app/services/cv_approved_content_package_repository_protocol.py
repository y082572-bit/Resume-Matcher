"""Explicit Provenance Stage P6-C1b-A: the ``ApprovedContentPackageRepository``
Protocol (immutable package storage + single-owner current-package
authority) and its in-memory (test-only) implementation.

Every method returns a closed, typed result -- never an ORM row, a raw
SQLAlchemy exception, or a filesystem path. A package row is immutable and
content-addressed by ``package_fingerprint``; the current-authority CAS is
independent per owner and keyed by an explicit ``slot_version`` integer --
never a full-object compare-and-swap.
"""

from __future__ import annotations

import threading

from typing import Protocol, runtime_checkable

from app.schemas.cv_approved_content_package import (
    ApprovedContentAuthorityPromotionResult,
    ApprovedContentAuthorityPromotionStatus,
    ApprovedContentAuthorityReadResult,
    ApprovedContentAuthorityReadStatus,
    ApprovedContentCurrentAuthority,
    ApprovedContentPackage,
    ApprovedContentPackageReadResult,
    ApprovedContentPackageReadStatus,
    ApprovedContentPackageSaveResult,
    ApprovedContentPackageSaveStatus,
)
from app.schemas.cv_document_artifact import JobArtifactOwnerKey


@runtime_checkable
class ApprovedContentPackageRepository(Protocol):
    def save_package(
        self, *, owner_key: JobArtifactOwnerKey, package: ApprovedContentPackage
    ) -> ApprovedContentPackageSaveResult: ...

    def get_package_by_fingerprint(self, package_fingerprint: str) -> ApprovedContentPackageReadResult: ...

    def read_current_authority(self, owner_key: JobArtifactOwnerKey) -> ApprovedContentAuthorityReadResult: ...

    def promote_current_authority(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        package_fingerprint: str,
        expected_previous_slot_version: int | None,
    ) -> ApprovedContentAuthorityPromotionResult: ...


class InMemoryApprovedContentPackageRepository:
    """Test-only in-memory repository. Owners must be registered explicitly
    (:meth:`register_owner`) before ``save_package``/``promote_current_authority``
    can succeed -- this mirrors the SQL repository's own FK-backed owner
    check without requiring a real database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: set[str] = set()
        self._packages: dict[str, ApprovedContentPackage] = {}
        #: owner_key_fingerprint -> (package_fingerprint, slot_version)
        self._authority: dict[str, tuple[str, int]] = {}

    # -- test setup helper (not part of the Protocol) -------------------------

    def register_owner(self, owner_key: JobArtifactOwnerKey) -> None:
        self._owners.add(owner_key.owner_key_fingerprint)

    # -- save -------------------------------------------------------------------

    def save_package(
        self, *, owner_key: JobArtifactOwnerKey, package: ApprovedContentPackage
    ) -> ApprovedContentPackageSaveResult:
        with self._lock:
            if owner_key.owner_key_fingerprint not in self._owners:
                return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.OWNER_NOT_FOUND)
            if package.owner_key_fingerprint != owner_key.owner_key_fingerprint:
                return ApprovedContentPackageSaveResult(status=ApprovedContentPackageSaveStatus.OWNER_MISMATCH)

            existing = self._packages.get(package.package_fingerprint)
            if existing is not None:
                if existing == package:
                    return ApprovedContentPackageSaveResult(
                        status=ApprovedContentPackageSaveStatus.ALREADY_EXISTS_IDENTICAL,
                        package=existing.model_copy(deep=True),
                    )
                return ApprovedContentPackageSaveResult(
                    status=ApprovedContentPackageSaveStatus.STORAGE_METADATA_CONFLICT
                )

            stored = package.model_copy(deep=True)
            self._packages[package.package_fingerprint] = stored
            return ApprovedContentPackageSaveResult(
                status=ApprovedContentPackageSaveStatus.SAVED, package=stored.model_copy(deep=True)
            )

    # -- read ---------------------------------------------------------------------

    def get_package_by_fingerprint(self, package_fingerprint: str) -> ApprovedContentPackageReadResult:
        with self._lock:
            package = self._packages.get(package_fingerprint)
            if package is None:
                return ApprovedContentPackageReadResult(status=ApprovedContentPackageReadStatus.NOT_FOUND)
            return ApprovedContentPackageReadResult(
                status=ApprovedContentPackageReadStatus.FOUND, package=package.model_copy(deep=True)
            )

    def read_current_authority(self, owner_key: JobArtifactOwnerKey) -> ApprovedContentAuthorityReadResult:
        with self._lock:
            current = self._authority.get(owner_key.owner_key_fingerprint)
            if current is None:
                return ApprovedContentAuthorityReadResult(status=ApprovedContentAuthorityReadStatus.NOT_FOUND)
            package_fingerprint, slot_version = current
            return ApprovedContentAuthorityReadResult(
                status=ApprovedContentAuthorityReadStatus.FOUND,
                authority=ApprovedContentCurrentAuthority(
                    package_fingerprint=package_fingerprint, slot_version=slot_version
                ),
            )

    # -- authority CAS --------------------------------------------------------------

    def promote_current_authority(
        self,
        *,
        owner_key: JobArtifactOwnerKey,
        package_fingerprint: str,
        expected_previous_slot_version: int | None,
    ) -> ApprovedContentAuthorityPromotionResult:
        with self._lock:
            target = self._packages.get(package_fingerprint)
            if target is None:
                return ApprovedContentAuthorityPromotionResult(
                    status=ApprovedContentAuthorityPromotionStatus.PACKAGE_NOT_FOUND
                )
            if target.owner_key_fingerprint != owner_key.owner_key_fingerprint:
                return ApprovedContentAuthorityPromotionResult(
                    status=ApprovedContentAuthorityPromotionStatus.OWNER_PACKAGE_MISMATCH
                )

            current = self._authority.get(owner_key.owner_key_fingerprint)
            current_slot_version = current[1] if current is not None else None
            if current_slot_version != expected_previous_slot_version:
                fresh_authority = (
                    ApprovedContentCurrentAuthority(package_fingerprint=current[0], slot_version=current[1])
                    if current is not None
                    else None
                )
                return ApprovedContentAuthorityPromotionResult(
                    status=ApprovedContentAuthorityPromotionStatus.STALE_REVISION, authority=fresh_authority
                )

            if current is not None and current[0] == package_fingerprint:
                return ApprovedContentAuthorityPromotionResult(
                    status=ApprovedContentAuthorityPromotionStatus.ALREADY_CURRENT,
                    authority=ApprovedContentCurrentAuthority(
                        package_fingerprint=package_fingerprint, slot_version=current[1]
                    ),
                )

            next_slot_version = 1 if current is None else current[1] + 1
            self._authority[owner_key.owner_key_fingerprint] = (package_fingerprint, next_slot_version)
            return ApprovedContentAuthorityPromotionResult(
                status=ApprovedContentAuthorityPromotionStatus.PROMOTED,
                authority=ApprovedContentCurrentAuthority(
                    package_fingerprint=package_fingerprint, slot_version=next_slot_version
                ),
            )
