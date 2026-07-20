"""Stable UUIDv4 identity primitives and domain errors for Truth P1."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4


class TruthIdentityError(ValueError):
    """Base error for an invalid or conflicting durable Truth identity."""


class TruthNotFoundError(TruthIdentityError):
    pass


class TruthIdentityConflictError(TruthIdentityError):
    pass


class TruthRevisionConflictError(TruthIdentityError):
    pass


class TruthArchivedError(TruthIdentityError):
    pass


class TruthIntegrityError(TruthIdentityError):
    pass


class TruthPolicyViolationError(TruthIdentityError):
    pass


class UnsupportedTruthSchemaVersionError(TruthIdentityError):
    pass


def new_truth_id() -> str:
    """Create identity independently of content, metadata, order, and location."""

    return str(uuid4())


def require_uuid4(value: str | UUID) -> str:
    """Return canonical lowercase UUIDv4 text or raise a domain error."""

    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise TruthIntegrityError("TRUTH_INVALID_UUID") from error
    if parsed.version != 4:
        raise TruthIntegrityError("TRUTH_UUID_NOT_V4")
    return str(parsed)


@dataclass(frozen=True)
class IdentityReplayDecision:
    is_replay: bool
    is_conflict: bool


def compare_identity_replay(
    *, existing_fingerprint: str, incoming_fingerprint: str,
    existing_status: str, incoming_status: str,
) -> IdentityReplayDecision:
    """Converge only exact same-ID, same-content, same-status retries."""

    same = (
        existing_fingerprint == incoming_fingerprint
        and existing_status == incoming_status
    )
    return IdentityReplayDecision(is_replay=same, is_conflict=not same)
