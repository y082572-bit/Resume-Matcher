"""Internal transactional repository for the inactive Explicit Provenance P1 store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    TruthEntity,
    TruthEvidence,
    TruthFact,
    TruthFactVariant,
    TruthPermission,
)
from app.schemas.truth_entity import (
    EntityStatus,
    EntityType,
    TruthEntityCreate,
    TruthEntityRead,
    TruthEntityUpdate,
)
from app.schemas.truth_fact import (
    EvidenceStatus,
    FactStatus,
    PermissionStatus,
    TruthEvidenceCreate,
    TruthEvidenceRead,
    TruthEvidenceUpdate,
    TruthFactCreate,
    TruthFactRead,
    TruthFactUpdate,
    TruthFactVariantCreate,
    TruthFactVariantRead,
    TruthFactVariantUpdate,
    TruthPermissionCreate,
    TruthPermissionRead,
    TruthPermissionUpdate,
    TruthSnapshot,
    parse_permission_datetime,
    serialize_permission_datetime,
)
from app.services.truth_fingerprint import (
    POLICY_REGISTRY_VERSION,
    build_truth_snapshot_projection,
    compute_entity_fingerprint,
    compute_evidence_fingerprint,
    compute_fact_fingerprint,
    compute_permission_fingerprint,
    compute_truth_snapshot_fingerprint,
    compute_variant_fingerprint,
)
from app.services.truth_identity import (
    TruthArchivedError,
    TruthIdentityConflictError,
    TruthIntegrityError,
    TruthNotFoundError,
    TruthPolicyViolationError,
    TruthRevisionConflictError,
    compare_identity_replay,
)
from app.services.truth_policy import PolicyContext, TruthTransformationPolicyRegistry


ReadModel = TypeVar("ReadModel")
FingerprintFunction = Callable[[Mapping[str, Any]], str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        column.key: getattr(row, column.key)
        for column in row.__table__.columns
    }


def _read(row: Any, schema: type[ReadModel]) -> ReadModel:
    return schema.model_validate(row)


class TruthRepository:
    """One-session repository. It flushes but never commits or rolls back."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        policy_registry: TruthTransformationPolicyRegistry | None = None,
    ) -> None:
        self.session = session
        self.policy_registry = policy_registry or TruthTransformationPolicyRegistry()

    async def _create(
        self,
        *,
        orm: type[Any],
        read_schema: type[ReadModel],
        id_field: str,
        values: dict[str, Any],
        fingerprint_fn: FingerprintFunction,
    ) -> ReadModel:
        identity = str(values[id_field])
        values[id_field] = identity
        if values.get("status") == "ARCHIVED":
            raise TruthArchivedError("TRUTH_CANNOT_CREATE_ARCHIVED_IDENTITY")
        fingerprint = fingerprint_fn(values)
        existing = await self.session.get(orm, identity)
        if existing is not None:
            decision = compare_identity_replay(
                existing_fingerprint=existing.content_fingerprint,
                incoming_fingerprint=fingerprint,
                existing_status=existing.status,
                incoming_status=str(values.get("status")),
            )
            if decision.is_replay:
                return _read(existing, read_schema)
            raise TruthIdentityConflictError("TRUTH_IDENTITY_CONFLICT")
        now = _now()
        row = orm(
            **values,
            revision=1,
            content_fingerprint=fingerprint,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        self.session.add(row)
        await self.session.flush()
        return _read(row, read_schema)

    async def _get(self, orm: type[Any], identity: str, error_code: str) -> Any:
        row = await self.session.get(orm, str(identity))
        if row is None:
            raise TruthNotFoundError(error_code)
        return row

    async def _update(
        self,
        *,
        orm: type[Any],
        read_schema: type[ReadModel],
        id_field: str,
        identity: str,
        expected_revision: int,
        changes: dict[str, Any],
        fingerprint_fn: FingerprintFunction,
    ) -> ReadModel:
        row = await self._get(orm, identity, "TRUTH_RECORD_NOT_FOUND")
        if row.status == "ARCHIVED":
            raise TruthArchivedError("TRUTH_ARCHIVED_IDENTITY_IMMUTABLE")
        current = _row_dict(row)
        archived_at = current.get("archived_at")
        if changes.get("status") == "ARCHIVED":
            archived_at = _now()
        elif "status" in changes and changes["status"] != "ARCHIVED":
            archived_at = None
        semantic = {**current, **changes, "archived_at": archived_at}
        fingerprint = fingerprint_fn(semantic)
        new_revision = expected_revision + 1
        statement = (
            update(orm)
            .where(
                getattr(orm, id_field) == identity,
                orm.revision == expected_revision,
                orm.status != "ARCHIVED",
            )
            .values(
                **changes,
                revision=new_revision,
                content_fingerprint=fingerprint,
                updated_at=_now(),
                archived_at=archived_at,
            )
        )
        result = await self.session.execute(statement)
        if result.rowcount != 1:
            current_row = await self.session.scalar(
                select(orm).where(getattr(orm, id_field) == identity)
            )
            if current_row is None:
                raise TruthNotFoundError("TRUTH_RECORD_NOT_FOUND")
            if current_row.status == "ARCHIVED":
                raise TruthArchivedError("TRUTH_ARCHIVED_IDENTITY_IMMUTABLE")
            raise TruthRevisionConflictError("TRUTH_REVISION_CONFLICT")
        await self.session.flush()
        refreshed = await self.session.scalar(
            select(orm)
            .where(getattr(orm, id_field) == identity)
            .execution_options(populate_existing=True)
        )
        assert refreshed is not None
        return _read(refreshed, read_schema)

    async def _assert_entity_exists(self, entity_id: str) -> TruthEntity:
        return await self._get(TruthEntity, entity_id, "TRUTH_ENTITY_NOT_FOUND")

    async def _assert_fact_exists(self, fact_id: str) -> TruthFact:
        return await self._get(TruthFact, fact_id, "TRUTH_FACT_NOT_FOUND")

    async def _assert_parent_safe(
        self, *, entity_id: str, parent_entity_id: str | None
    ) -> None:
        if parent_entity_id is None:
            return
        if entity_id == parent_entity_id:
            raise TruthIntegrityError("TRUTH_ENTITY_SELF_PARENT")
        current_id: str | None = parent_entity_id
        visited = {entity_id}
        while current_id is not None:
            if current_id in visited:
                raise TruthIntegrityError("TRUTH_ENTITY_PARENT_CYCLE")
            visited.add(current_id)
            parent = await self._assert_entity_exists(current_id)
            current_id = parent.parent_entity_id

    async def create_entity(self, value: TruthEntityCreate) -> TruthEntityRead:
        data = value.model_dump(mode="json")
        await self._assert_parent_safe(
            entity_id=data["entity_id"], parent_entity_id=data["parent_entity_id"]
        )
        return await self._create(
            orm=TruthEntity, read_schema=TruthEntityRead, id_field="entity_id",
            values=data, fingerprint_fn=compute_entity_fingerprint,
        )

    async def get_entity(self, entity_id: str) -> TruthEntityRead:
        return _read(
            await self._get(TruthEntity, entity_id, "TRUTH_ENTITY_NOT_FOUND"),
            TruthEntityRead,
        )

    async def list_entities(self) -> list[TruthEntityRead]:
        rows = (
            await self.session.scalars(select(TruthEntity).order_by(TruthEntity.entity_id))
        ).all()
        return [_read(row, TruthEntityRead) for row in rows]

    async def update_entity(
        self, entity_id: str, value: TruthEntityUpdate
    ) -> TruthEntityRead:
        changes = value.model_dump(mode="json", exclude_unset=True)
        changes.pop("expected_revision")
        current = await self._get(TruthEntity, entity_id, "TRUTH_ENTITY_NOT_FOUND")
        parent_id = changes.get("parent_entity_id", current.parent_entity_id)
        await self._assert_parent_safe(entity_id=entity_id, parent_entity_id=parent_id)
        return await self._update(
            orm=TruthEntity, read_schema=TruthEntityRead, id_field="entity_id",
            identity=entity_id, expected_revision=value.expected_revision,
            changes=changes, fingerprint_fn=compute_entity_fingerprint,
        )

    async def archive_entity(
        self, entity_id: str, *, expected_revision: int
    ) -> TruthEntityRead:
        return await self.update_entity(
            entity_id,
            TruthEntityUpdate(
                expected_revision=expected_revision, status=EntityStatus.ARCHIVED
            ),
        )

    async def _assert_employment_scope(self, entity_id: str | None) -> None:
        if entity_id is None:
            return
        entity = await self._assert_entity_exists(entity_id)
        if entity.entity_type != EntityType.EMPLOYMENT.value:
            raise TruthIntegrityError("TRUTH_EMPLOYMENT_SCOPE_NOT_EMPLOYMENT")

    async def create_fact(self, value: TruthFactCreate) -> TruthFactRead:
        data = value.model_dump(mode="json")
        await self._assert_entity_exists(data["entity_id"])
        await self._assert_employment_scope(data["employment_scope_entity_id"])
        return await self._create(
            orm=TruthFact, read_schema=TruthFactRead, id_field="fact_id",
            values=data, fingerprint_fn=compute_fact_fingerprint,
        )

    async def get_fact(self, fact_id: str) -> TruthFactRead:
        return _read(
            await self._get(TruthFact, fact_id, "TRUTH_FACT_NOT_FOUND"), TruthFactRead
        )

    async def list_entity_facts(self, entity_id: str) -> list[TruthFactRead]:
        await self._assert_entity_exists(entity_id)
        rows = (
            await self.session.scalars(
                select(TruthFact)
                .where(TruthFact.entity_id == entity_id)
                .order_by(TruthFact.fact_id)
            )
        ).all()
        return [_read(row, TruthFactRead) for row in rows]

    async def update_fact(self, fact_id: str, value: TruthFactUpdate) -> TruthFactRead:
        changes = value.model_dump(mode="json", exclude_unset=True)
        changes.pop("expected_revision")
        if "employment_scope_entity_id" in changes:
            await self._assert_employment_scope(changes["employment_scope_entity_id"])
        return await self._update(
            orm=TruthFact, read_schema=TruthFactRead, id_field="fact_id",
            identity=fact_id, expected_revision=value.expected_revision,
            changes=changes, fingerprint_fn=compute_fact_fingerprint,
        )

    async def archive_fact(
        self, fact_id: str, *, expected_revision: int
    ) -> TruthFactRead:
        return await self.update_fact(
            fact_id,
            TruthFactUpdate(
                expected_revision=expected_revision, status=FactStatus.ARCHIVED
            ),
        )

    async def create_variant(
        self, value: TruthFactVariantCreate
    ) -> TruthFactVariantRead:
        data = value.model_dump(mode="json")
        await self._assert_fact_exists(data["fact_id"])
        return await self._create(
            orm=TruthFactVariant, read_schema=TruthFactVariantRead,
            id_field="variant_id", values=data,
            fingerprint_fn=compute_variant_fingerprint,
        )

    async def get_variant(self, variant_id: str) -> TruthFactVariantRead:
        return _read(
            await self._get(TruthFactVariant, variant_id, "TRUTH_VARIANT_NOT_FOUND"),
            TruthFactVariantRead,
        )

    async def update_variant(
        self, variant_id: str, value: TruthFactVariantUpdate
    ) -> TruthFactVariantRead:
        changes = value.model_dump(mode="json", exclude_unset=True)
        changes.pop("expected_revision")
        return await self._update(
            orm=TruthFactVariant, read_schema=TruthFactVariantRead,
            id_field="variant_id", identity=variant_id,
            expected_revision=value.expected_revision, changes=changes,
            fingerprint_fn=compute_variant_fingerprint,
        )

    async def archive_variant(
        self, variant_id: str, *, expected_revision: int
    ) -> TruthFactVariantRead:
        return await self.update_variant(
            variant_id,
            TruthFactVariantUpdate(expected_revision=expected_revision, status="ARCHIVED"),
        )

    async def create_evidence(self, value: TruthEvidenceCreate) -> TruthEvidenceRead:
        data = value.model_dump(mode="json")
        await self._assert_fact_exists(data["fact_id"])
        if data["source_entity_id"] is not None:
            await self._assert_entity_exists(data["source_entity_id"])
        return await self._create(
            orm=TruthEvidence, read_schema=TruthEvidenceRead, id_field="evidence_id",
            values=data, fingerprint_fn=compute_evidence_fingerprint,
        )

    async def get_evidence(self, evidence_id: str) -> TruthEvidenceRead:
        return _read(
            await self._get(TruthEvidence, evidence_id, "TRUTH_EVIDENCE_NOT_FOUND"),
            TruthEvidenceRead,
        )

    async def update_evidence(
        self, evidence_id: str, value: TruthEvidenceUpdate
    ) -> TruthEvidenceRead:
        changes = value.model_dump(mode="json", exclude_unset=True)
        changes.pop("expected_revision")
        if "source_entity_id" in changes and changes["source_entity_id"] is not None:
            await self._assert_entity_exists(changes["source_entity_id"])
        return await self._update(
            orm=TruthEvidence, read_schema=TruthEvidenceRead, id_field="evidence_id",
            identity=evidence_id, expected_revision=value.expected_revision,
            changes=changes, fingerprint_fn=compute_evidence_fingerprint,
        )

    async def archive_evidence(
        self, evidence_id: str, *, expected_revision: int
    ) -> TruthEvidenceRead:
        return await self.update_evidence(
            evidence_id,
            TruthEvidenceUpdate(expected_revision=expected_revision, status="ARCHIVED"),
        )

    def _assert_permission_policy(
        self, fact: TruthFact, value: TruthPermissionCreate | TruthPermissionUpdate,
        *, current: TruthPermission | None = None,
    ) -> None:
        status = value.status or (current.status if current is not None else None)
        if status != PermissionStatus.ACTIVE:
            return
        operations = value.allowed_operations or (
            current.allowed_operations if current is not None else []
        )
        target_scope = value.target_scope or (
            current.target_scope if current is not None else ""
        )
        for operation in operations:
            decision = self.policy_registry.evaluate(
                PolicyContext(
                    fact_type=fact.fact_type,
                    fact_status=FactStatus(fact.status),
                    transferability=fact.transferability,
                    operation=operation,
                    target_scope=target_scope,
                ),
                permission_operations=frozenset(operations),
                permission_target_scope=target_scope,
                permission_active=True,
            )
            if not decision.allowed:
                raise TruthPolicyViolationError(decision.reason_code)

    @staticmethod
    def _validate_permission_window(
        valid_from: str | None, valid_until: str | None
    ) -> None:
        try:
            start = parse_permission_datetime(valid_from)
            end = parse_permission_datetime(valid_until)
        except ValueError as error:
            raise TruthIntegrityError(
                "TRUTH_PERMISSION_TIMESTAMP_NOT_CANONICAL"
            ) from error
        if start is not None and end is not None and end <= start:
            raise TruthIntegrityError("TRUTH_PERMISSION_INVALID_TIME_WINDOW")

    @staticmethod
    def _canonical_permission_changes(
        value: TruthPermissionCreate | TruthPermissionUpdate,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        for field in ("valid_from", "valid_until"):
            if field in value.model_fields_set or isinstance(value, TruthPermissionCreate):
                data[field] = serialize_permission_datetime(getattr(value, field))
        return data

    async def create_permission(
        self, value: TruthPermissionCreate
    ) -> TruthPermissionRead:
        data = self._canonical_permission_changes(
            value, value.model_dump(mode="json")
        )
        self._validate_permission_window(data["valid_from"], data["valid_until"])
        fact = await self._assert_fact_exists(data["fact_id"])
        self._assert_permission_policy(fact, value)
        return await self._create(
            orm=TruthPermission, read_schema=TruthPermissionRead,
            id_field="permission_id", values=data,
            fingerprint_fn=compute_permission_fingerprint,
        )

    async def get_permission(self, permission_id: str) -> TruthPermissionRead:
        return _read(
            await self._get(TruthPermission, permission_id, "TRUTH_PERMISSION_NOT_FOUND"),
            TruthPermissionRead,
        )

    async def update_permission(
        self, permission_id: str, value: TruthPermissionUpdate
    ) -> TruthPermissionRead:
        current = await self._get(
            TruthPermission, permission_id, "TRUTH_PERMISSION_NOT_FOUND"
        )
        fact = await self._assert_fact_exists(current.fact_id)
        self._assert_permission_policy(fact, value, current=current)
        changes = self._canonical_permission_changes(
            value, value.model_dump(mode="json", exclude_unset=True)
        )
        changes.pop("expected_revision")
        merged_valid_from = changes.get("valid_from", current.valid_from)
        merged_valid_until = changes.get("valid_until", current.valid_until)
        self._validate_permission_window(merged_valid_from, merged_valid_until)
        return await self._update(
            orm=TruthPermission, read_schema=TruthPermissionRead,
            id_field="permission_id", identity=permission_id,
            expected_revision=value.expected_revision, changes=changes,
            fingerprint_fn=compute_permission_fingerprint,
        )

    async def revoke_permission(
        self, permission_id: str, *, expected_revision: int
    ) -> TruthPermissionRead:
        return await self.update_permission(
            permission_id,
            TruthPermissionUpdate(expected_revision=expected_revision, status="REVOKED"),
        )

    async def archive_permission(
        self, permission_id: str, *, expected_revision: int
    ) -> TruthPermissionRead:
        return await self.update_permission(
            permission_id,
            TruthPermissionUpdate(expected_revision=expected_revision, status="ARCHIVED"),
        )

    async def compute_truth_snapshot(self) -> TruthSnapshot:
        tables: Sequence[tuple[str, type[Any]]] = (
            ("entities", TruthEntity),
            ("facts", TruthFact),
            ("variants", TruthFactVariant),
            ("evidence", TruthEvidence),
            ("permissions", TruthPermission),
        )
        values: dict[str, list[dict[str, Any]]] = {}
        for name, orm in tables:
            rows = (await self.session.scalars(select(orm))).all()
            values[name] = [_row_dict(row) for row in rows]
        projection = build_truth_snapshot_projection(
            entities=values["entities"], facts=values["facts"],
            variants=values["variants"], evidence=values["evidence"],
            permissions=values["permissions"],
            policy_registry_version=POLICY_REGISTRY_VERSION,
        )
        fingerprint = compute_truth_snapshot_fingerprint(
            entities=values["entities"], facts=values["facts"],
            variants=values["variants"], evidence=values["evidence"],
            permissions=values["permissions"],
            policy_registry_version=POLICY_REGISTRY_VERSION,
        )
        return TruthSnapshot(fingerprint=fingerprint, **projection)


class TruthService:
    """Transaction boundary for future internal callers; no legacy wiring."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TruthRepository]:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    yield TruthRepository(session)
        except IntegrityError as error:
            raise TruthIntegrityError("TRUTH_DATABASE_INTEGRITY_ERROR") from error
