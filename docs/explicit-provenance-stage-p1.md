# Explicit Provenance — Stage 10D-A-P1

## Purpose and scope

P1 establishes inactive, durable identities for Truth entities, facts, variants,
evidence and transformation permissions. It adds Pydantic contracts, an additive
SQLite schema, deterministic content and snapshot fingerprints, fail-closed policy
primitives, optimistic locking, and an internal transaction boundary.

P1 does not migrate legacy Truth Library content and does not implement CV
planning, approval, generation, provenance manifests, validation, DOCX/PDF
metadata, public endpoints, frontend behavior, LLM calls, dual-write, or UUID
backfill. Nothing in the active Stage 10A, 10B, 10C or 10D flow reads or writes
the new tables.

## Durable models and DDL

The five new tables are `truth_entities`, `truth_facts`,
`truth_fact_variants`, `truth_evidence`, and `truth_permissions`. Each record has
an immutable UUIDv4 identity, schema version `1.0`, revision, SHA-256 content
fingerprint, audit timestamps and archive consistency constraints. Foreign keys
use `ON DELETE RESTRICT`; repository APIs expose archive/revoke operations and no
physical delete.

The schema enforces closed status/type/operation sets, JSON validity and shape,
hash format, revision lower bounds, parent safety, variant semantic uniqueness,
evidence expression uniqueness and one active permission per fact and target.
SQLite foreign keys are enabled on sync and async connections. Triggers prevent
entity-type changes and reject a non-`EMPLOYMENT` employment scope on fact insert
or update.

Every database identity constraint requires canonical lowercase UUIDv4 text,
including the version-4 nibble and RFC 4122 variant nibble. UUIDv1/v3/v5,
uppercase text, misplaced or additional hyphens, invalid hexadecimal characters
and non-standard variants are rejected by SQLite even when input bypasses
Pydantic.

## UUID identity decision

Identity is generated with `uuid.uuid4()`. It never derives from text, display
name, source reference, order, list index, target path, company or role. A split
or merge creates new fact identities; an update retains the existing identity.
An exact same-ID, same-fingerprint, same-status create retry is idempotent. Other
same-ID creates conflict, including attempts to reuse an archived identity.

## Canonical JSON and fingerprints

`truth-canonical-json-v1` serializes UTF-8 JSON with sorted object keys and compact
separators. It applies Unicode NFKC, newline and controlled whitespace
normalization, rejects non-finite numbers, preserves ordered arrays, and sorts
only arrays explicitly declared set-like by path. Datetimes must be timezone
aware and serialize as ISO-8601 values.

Versioned content fingerprints exclude identity, creation/update/archive
timestamps, source reference and actor audit fields. They include semantic
content, status and eligibility/transfer/policy fields. The `truth-snapshot-v1`
projection includes IDs, revisions, content fingerprints, statuses, source
references, evidence content hashes and the policy registry version. Therefore a
source-reference-only update preserves the content fingerprint, increments the
revision and changes the snapshot fingerprint.

## Policy hierarchy

The effective decision is the intersection of `HardSafetyPolicy`, the registered
fact-type transformation policy, and an active explicit permission. Every layer
is fail-closed. Missing policy or permission denies. Permission can narrow but
cannot expand a policy. Rejected/archived facts, forbidden exact-only rephrases,
scope transfer for non-transferable/employment-scoped facts, and protected-number
changes are denied before permission evaluation. P1 does not invoke this policy
from generation or validation.

## Repository, transactions and optimistic locking

`TruthRepository` owns one caller-provided `AsyncSession`, flushes changes and
never commits or rolls back. `TruthService.transaction()` supplies the explicit
`session.begin()` boundary. Updates use identity plus `expected_revision`; a
zero-row update is classified as not found, archived or revision conflict.
Concurrent updates from the same revision produce one winner. Integrity errors
leave the transaction before they are mapped to a stable domain error, so partial
writes are rolled back.

## Feature flag and activation state

`EXPLICIT_PROVENANCE_ENABLED` maps to the strict boolean setting
`explicit_provenance_enabled` and defaults to `false`. Invalid input is a
configuration error. In P1, neither value changes legacy flow: the flag is not
read by production routers, planning, generation, validation or Truth Library
loading. It does not enable dual-write or migration. There is no public P1 API.

## Schema preflight and migration

`preflight_explicit_provenance_p1_schema()` runs before `create_all`:

- no P1 tables: `ABSENT_CREATE_REQUIRED`;
- all expected tables with exact columns: `P1_SCHEMA_READY`;
- a subset: `ERROR_PARTIAL_P1_SCHEMA`;
- all tables with incompatible columns: `ERROR_INCOMPATIBLE_P1_SCHEMA`;
- any existing table whose name matches a canonical P1 name case-insensitively
  but is not the exact lowercase canonical name (e.g. `TRUTH_ENTITIES`,
  `Truth_Entities`): `ERROR_INCOMPATIBLE_P1_SCHEMA_NAME`.

The case-insensitive check is applied before any mutation. SQLite treats ASCII
table identifiers case-insensitively, so a wrong-case P1 table would silently
collide with `CREATE TABLE IF NOT EXISTS`; the preflight detects this and raises
before `create_all`, index installation, trigger installation, or any other
sqlite_master write. Only an exact ASCII lowercase match is accepted as a
canonical P1 table. Tables with names that only share a prefix or substring with
canonical names (e.g. `truth_entities_backup`, `truth_entity`) are not treated
as P1 tables. Partial and incompatible states stop startup without drop, repair
or creation of missing tables. A clean upgrade is additive; post-create
installation uses idempotent index/trigger DDL and validates a deterministic
manifest. Repeated startup preserves the manifest and all existing tables and
contents. No legacy data receives a UUID or provenance row.

The approved `explicit-provenance-p1-schema-manifest-v1` compares complete,
normalized DDL: column order/type/nullability/default/PK, FK source and target
with `ON UPDATE`/`ON DELETE`, CHECK and UNIQUE constraints, index uniqueness,
partial predicates, expression definitions, and trigger bodies. Existing READY
schemas are validated read-only; `create_all` and runtime-object installers run
only when all five P1 tables were initially absent.

Permission validity boundaries are normalized before persistence to fixed-width
UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Update validation combines stored and supplied
values before checking chronological order. Matching SQLite constraints reject
noncanonical offsets and make the final lexical window check safe.

The serializer assembles every component numerically, including a four-digit
year for the complete Python `datetime` range `0001`–`9999`; parsing performs a
strict component-wise round-trip and preserves all six microsecond digits.
SQLite validates the same fixed width plus explicit year/month/day/leap-year,
hour, minute and second bounds. A calendar round-trip is retained as an
additional guard, but validity never relies on `julianday()` normalization.
Consequently dates such as `2026-02-29`, `2026-02-31` and `2026-04-31`, hour 24,
and leap seconds are rejected by direct SQL. The earlier R1 timestamp CHECK is
classified as incompatible and is never migrated or repaired automatically.

## Test matrix and anti-pattern gates

Unit tests cover UUID independence, Pydantic immutability, canonical bytes,
array policies, Unicode/non-finite numbers, every fingerprint type, snapshot
semantics and policy intersection. Database tests cover parent/FK integrity,
scope/type triggers, constraints, uniqueness, archive behavior, rollback,
idempotent/conflicting replay and stale/concurrent locking. Migration tests cover
clean-base upgrade, three restarts, manifest stability, preservation of legacy
counts/content and fail-closed partial/incompatible states. Flag tests cover
default/true/false/invalid values and absence of public API or runtime wiring.

Static anti-pattern gates reject imports or coupling to legacy `TruthIndex`, CV
generation/validation, LLM providers, target paths, bullet indexes, source-prefix
matching and production dual-write. Git scope review separately confirms no
router or frontend changes.

## Rollback and P2

Because P1 is inactive and additive, operational rollback is to deploy the base
application while leaving the unused tables intact. Automated destructive DDL is
intentionally absent; table removal, if ever approved, requires a separate,
backed-up migration. A future P2 may define explicit legacy-to-UUID mapping and
controlled activation, but must not infer identity from mutable text or metadata
and must receive separate review. P1 itself is not production activation.
