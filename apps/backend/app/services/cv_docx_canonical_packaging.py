"""Explicit Provenance Stage P6-B2a: canonical DOCX (OPC/ZIP) packaging.

``canonicalize_docx_package`` is the single place P6-B2a neutralizes every
byte-level source of nondeterminism a raw ``python-docx``/``zipfile`` save
can introduce: per-entry timestamps, entry order, compression policy, and
external attributes. It is a pure function -- bytes in, bytes out, no
filesystem access, no wall-clock read, no random value -- so the exact same
raw ZIP bytes always canonicalize to the exact same final bytes, on any
host, in any process, regardless of ``PYTHONHASHSEED``.

It never rewrites the *content* of any part (``word/document.xml`` and
friends keep whatever bytes ``python-docx``/``lxml`` produced) -- only the
ZIP container-level metadata around those parts.
"""

from __future__ import annotations

import zipfile
from io import BytesIO


_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FIXED_EXTERNAL_ATTR = 0o600 << 16
_FIXED_CREATE_SYSTEM = 0  # fixed regardless of the host OS zipfile was built on
_PRIORITY_ENTRY = "[Content_Types].xml"
_COMPRESSION = zipfile.ZIP_DEFLATED
_COMPRESSLEVEL = 6


def canonicalize_docx_package(raw_zip_bytes: bytes) -> bytes:
    """Rewrite ``raw_zip_bytes`` (an OPC/ZIP package as produced by
    ``python-docx``'s own ``Document.save()``) with fixed per-entry
    timestamps, a fixed entry order, and a fixed compression policy."""

    with zipfile.ZipFile(BytesIO(raw_zip_bytes), "r") as source:
        names = source.namelist()
        parts = {name: source.read(name) for name in names}

    ordered_names = sorted(names)
    if _PRIORITY_ENTRY in ordered_names:
        ordered_names.remove(_PRIORITY_ENTRY)
        ordered_names.insert(0, _PRIORITY_ENTRY)

    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=_COMPRESSION, compresslevel=_COMPRESSLEVEL
    ) as target:
        for name in ordered_names:
            info = zipfile.ZipInfo(filename=name, date_time=_FIXED_DATE_TIME)
            info.compress_type = _COMPRESSION
            info.external_attr = _FIXED_EXTERNAL_ATTR
            info.create_system = _FIXED_CREATE_SYSTEM
            target.writestr(info, parts[name])

    return output.getvalue()
