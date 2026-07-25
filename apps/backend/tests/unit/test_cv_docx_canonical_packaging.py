"""Unit tests for Explicit Provenance Stage P6-B2a: ``canonicalize_docx_package``.

Pure ZIP-level determinism tests, independent of renderer content --
exercises the canonicalization function directly against synthetic ZIP
bytes so timestamp/order/compression fixing can be verified without ever
invoking python-docx.
"""

from __future__ import annotations

import io
import zipfile

from app.services.cv_docx_canonical_packaging import canonicalize_docx_package


def _build_raw_zip(entries: dict[str, bytes], *, date_time=(2024, 6, 15, 10, 30, 0)) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(filename=name, date_time=date_time)
            zf.writestr(info, data)
    return buffer.getvalue()


_ENTRIES = {
    "word/document.xml": b"<document/>",
    "[Content_Types].xml": b"<Types/>",
    "_rels/.rels": b"<Relationships/>",
    "docProps/core.xml": b"<coreProperties/>",
}


def test_same_input_gives_identical_bytes() -> None:
    raw = _build_raw_zip(_ENTRIES)
    out1 = canonicalize_docx_package(raw)
    out2 = canonicalize_docx_package(raw)
    assert out1 == out2


def test_input_timestamp_never_leaks_into_output() -> None:
    raw_early = _build_raw_zip(_ENTRIES, date_time=(1990, 1, 1, 0, 0, 0))
    raw_late = _build_raw_zip(_ENTRIES, date_time=(2030, 12, 31, 23, 59, 58))
    assert canonicalize_docx_package(raw_early) == canonicalize_docx_package(raw_late)


def test_zip_entry_timestamps_are_fixed() -> None:
    raw = _build_raw_zip(_ENTRIES)
    out = canonicalize_docx_package(raw)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_zip_entry_order_is_fixed_regardless_of_input_order() -> None:
    forward = _build_raw_zip(_ENTRIES)
    reversed_entries = dict(reversed(list(_ENTRIES.items())))
    backward = _build_raw_zip(reversed_entries)

    out_forward = canonicalize_docx_package(forward)
    out_backward = canonicalize_docx_package(backward)
    assert out_forward == out_backward

    with zipfile.ZipFile(io.BytesIO(out_forward)) as zf:
        names = zf.namelist()
    assert names[0] == "[Content_Types].xml"
    assert names[1:] == sorted(names[1:])


def test_content_bytes_are_preserved_exactly() -> None:
    raw = _build_raw_zip(_ENTRIES)
    out = canonicalize_docx_package(raw)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        for name, expected in _ENTRIES.items():
            assert zf.read(name) == expected


def test_different_content_gives_different_bytes() -> None:
    raw_a = _build_raw_zip(_ENTRIES)
    mutated = dict(_ENTRIES)
    mutated["word/document.xml"] = b"<document>different</document>"
    raw_b = _build_raw_zip(mutated)
    assert canonicalize_docx_package(raw_a) != canonicalize_docx_package(raw_b)


def test_compression_policy_is_fixed() -> None:
    raw = _build_raw_zip(_ENTRIES)
    out = canonicalize_docx_package(raw)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED
