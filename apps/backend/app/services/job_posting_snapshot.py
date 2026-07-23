"""Deterministic ``JobPostingSnapshot`` construction and evidence
segmentation for Explicit Provenance Stage PRE-P4, Step 1.

Segmentation never depends on an LLM, a clock, or Python's randomized
built-in hash function. It is a pure function of a snapshot's ``canonical_text``: the
same text always yields the same segments, in the same order, with the
same ``segment_id`` values.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence

from app.schemas.job_posting_analysis import (
    JOB_EVIDENCE_SEGMENT_SCHEMA_VERSION,
    JOB_POSTING_SNAPSHOT_SCHEMA_VERSION,
    JobEvidenceSegment,
    JobEvidenceSegmentType,
    JobPostingSnapshot,
    JobPostingSourceType,
)
from app.services.truth_fingerprint import canonical_json_bytes


SNAPSHOT_FINGERPRINT_VERSION = "job-posting-snapshot-fingerprint-v1"
SEGMENT_ID_VERSION = "job-evidence-segment-id-v1"
SEGMENT_FINGERPRINT_VERSION = "job-evidence-segment-fingerprint-v1"

_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*•‣◦]|\d{1,3}[.)])\s+")
_HEADING_MAX_WORDS = 8
_HEADING_MAX_CHARS = 80


class InvalidJobPostingSnapshotError(ValueError):
    """Raised when a job posting cannot be normalized into a valid snapshot."""


def _fingerprint(version: str, content: dict) -> str:
    projection = {"fingerprint_version": version, "content": content}
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def normalize_canonical_text(raw_text: str) -> str:
    """NFKC-normalize, unify line endings, and collapse horizontal
    whitespace deterministically. Never trims semantically meaningful blank
    lines used to separate paragraphs beyond leading/trailing whitespace."""

    normalized = unicodedata.normalize("NFKC", raw_text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        _HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip() for line in normalized.split("\n")
    ]
    return "\n".join(lines).strip()


def compute_job_posting_snapshot_fingerprint(
    *,
    source_type: JobPostingSourceType,
    company_name: str | None,
    role_title: str | None,
    location: str | None,
    employment_type: str | None,
    canonical_text: str,
    source_language: str,
    snapshot_schema_version: str = JOB_POSTING_SNAPSHOT_SCHEMA_VERSION,
) -> str:
    """Content-only identity. ``posting_id`` and ``source_url`` never
    participate -- neither is a substitute for content-derived identity."""

    content = {
        "source_type": source_type.value,
        "company_name": company_name,
        "role_title": role_title,
        "location": location,
        "employment_type": employment_type,
        "canonical_text": canonical_text,
        "source_language": source_language,
        "snapshot_schema_version": snapshot_schema_version,
    }
    return _fingerprint(SNAPSHOT_FINGERPRINT_VERSION, content)


def build_job_posting_snapshot(
    *,
    posting_id: str | None,
    source_type: JobPostingSourceType,
    raw_text: str,
    source_language: str,
    source_url: str | None = None,
    company_name: str | None = None,
    role_title: str | None = None,
    location: str | None = None,
    employment_type: str | None = None,
) -> JobPostingSnapshot:
    canonical_text = normalize_canonical_text(raw_text)
    if not canonical_text:
        raise InvalidJobPostingSnapshotError("posting text is empty after normalization")

    fingerprint = compute_job_posting_snapshot_fingerprint(
        source_type=source_type,
        company_name=company_name,
        role_title=role_title,
        location=location,
        employment_type=employment_type,
        canonical_text=canonical_text,
        source_language=source_language,
    )
    return JobPostingSnapshot(
        posting_id=posting_id,
        source_type=source_type,
        source_url=source_url,
        company_name=company_name,
        role_title=role_title,
        location=location,
        employment_type=employment_type,
        canonical_text=canonical_text,
        source_language=source_language,
        snapshot_fingerprint=fingerprint,
    )


def recompute_job_posting_snapshot_fingerprint(snapshot: JobPostingSnapshot) -> str:
    """Independently re-derive a snapshot's fingerprint from its own
    content fields -- used to reject a tampered or stale self-reported
    ``snapshot_fingerprint`` (never trusted as-is)."""

    return compute_job_posting_snapshot_fingerprint(
        source_type=snapshot.source_type,
        company_name=snapshot.company_name,
        role_title=snapshot.role_title,
        location=snapshot.location,
        employment_type=snapshot.employment_type,
        canonical_text=snapshot.canonical_text,
        source_language=snapshot.source_language,
        snapshot_schema_version=snapshot.snapshot_schema_version,
    )


def compute_canonical_text_fingerprint(canonical_text: str) -> str:
    return _fingerprint(SNAPSHOT_FINGERPRINT_VERSION + "-text-only", {"canonical_text": canonical_text})


def _is_heading(line: str) -> bool:
    if not line or len(line) > _HEADING_MAX_CHARS:
        return False
    if line.endswith(":"):
        return len(line.split()) <= _HEADING_MAX_WORDS
    letters = [ch for ch in line if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters) and len(line.split()) <= _HEADING_MAX_WORDS:
        return True
    return False


def _classify_line(line: str) -> JobEvidenceSegmentType:
    if _is_heading(line):
        return JobEvidenceSegmentType.HEADING
    if _BULLET_PREFIX_RE.match(line):
        return JobEvidenceSegmentType.BULLET_ITEM
    return JobEvidenceSegmentType.PARAGRAPH


def compute_job_evidence_segment_id(
    *,
    posting_fingerprint: str,
    normalized_text: str,
    segment_type: JobEvidenceSegmentType,
    duplicate_discriminator: int,
) -> str:
    content = {
        "posting_fingerprint": posting_fingerprint,
        "normalized_text": normalized_text,
        "segment_type": segment_type.value,
        "duplicate_discriminator": duplicate_discriminator,
    }
    return _fingerprint(SEGMENT_ID_VERSION, content)


def compute_job_evidence_segment_fingerprint(
    *,
    segment_id: str,
    segment_type: JobEvidenceSegmentType,
    source_text: str,
    normalized_text: str,
    posting_fingerprint: str,
    section_hint: str | None,
    segment_order: int,
) -> str:
    content = {
        "segment_id": segment_id,
        "segment_type": segment_type.value,
        "source_text": source_text,
        "normalized_text": normalized_text,
        "posting_fingerprint": posting_fingerprint,
        "section_hint": section_hint,
        "segment_order": segment_order,
    }
    return _fingerprint(SEGMENT_FINGERPRINT_VERSION, content)


def segment_job_posting(snapshot: JobPostingSnapshot) -> tuple[JobEvidenceSegment, ...]:
    """Deterministically segment ``snapshot.canonical_text`` into evidence
    segments. Pure function of the snapshot's content -- never touches a
    clock, a database, or an LLM."""

    posting_fingerprint = snapshot.snapshot_fingerprint
    lines = [line for line in snapshot.canonical_text.split("\n") if line.strip()]

    duplicate_counts: dict[tuple[str, str], int] = {}
    segments: list[JobEvidenceSegment] = []
    current_heading: str | None = None
    order = 0

    for raw_line in lines:
        line = raw_line.strip()
        segment_type = _classify_line(line)
        normalized_text = line.lower()
        section_hint = current_heading if segment_type != JobEvidenceSegmentType.HEADING else current_heading

        key = (segment_type.value, normalized_text)
        discriminator = duplicate_counts.get(key, 0)
        duplicate_counts[key] = discriminator + 1

        segment_id = compute_job_evidence_segment_id(
            posting_fingerprint=posting_fingerprint,
            normalized_text=normalized_text,
            segment_type=segment_type,
            duplicate_discriminator=discriminator,
        )
        segment_fingerprint = compute_job_evidence_segment_fingerprint(
            segment_id=segment_id,
            segment_type=segment_type,
            source_text=line,
            normalized_text=normalized_text,
            posting_fingerprint=posting_fingerprint,
            section_hint=section_hint,
            segment_order=order,
        )
        segments.append(
            JobEvidenceSegment(
                segment_id=segment_id,
                segment_type=segment_type,
                source_text=line,
                normalized_text=normalized_text,
                posting_fingerprint=posting_fingerprint,
                section_hint=section_hint,
                segment_order=order,
                segment_fingerprint=segment_fingerprint,
            )
        )
        order += 1
        if segment_type == JobEvidenceSegmentType.HEADING:
            current_heading = line.lower()

    return tuple(segments)


def recompute_job_evidence_segment_fingerprint(segment: JobEvidenceSegment) -> str:
    return compute_job_evidence_segment_fingerprint(
        segment_id=segment.segment_id,
        segment_type=segment.segment_type,
        source_text=segment.source_text,
        normalized_text=segment.normalized_text,
        posting_fingerprint=segment.posting_fingerprint,
        section_hint=segment.section_hint,
        segment_order=segment.segment_order,
    )


def find_duplicate_segment_ids(segments: Sequence[JobEvidenceSegment]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for segment in segments:
        if segment.segment_id in seen:
            duplicates.add(segment.segment_id)
        seen.add(segment.segment_id)
    return tuple(sorted(duplicates))
