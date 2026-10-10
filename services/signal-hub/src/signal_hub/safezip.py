"""Bounded reading of one member of a feed archive (NHC KMZ, GDELT export zip; ``124`` §1).

Feed archives are untrusted input. :func:`read_member` uses the stdlib :mod:`zipfile` and refuses:

* anything that is not a zip, or has more than ``max_members`` entries;
* an encrypted member (flag bit 0), an absolute name or one with a ``..`` / backslash component;
* a member whose *declared* size is over ``max_uncompressed`` or whose declared compression ratio is
  over ``max_ratio`` (a zip bomb), and — counted while streaming — an *actual* size over the cap,
  so a lying header never gets past it (zipfile also checks the CRC at the end);
* zero or more than one member matching ``suffixes`` (ambiguous archives are not guessed at).

Every refusal is a :class:`UnsafeArchive` (a ``ValueError``: the poll fails with backoff).
"""

from __future__ import annotations

import io
import zipfile
import zlib
from collections.abc import Sequence
from typing import Final

__all__ = ["DEFAULT_MAX_RATIO", "DEFAULT_MAX_UNCOMPRESSED", "UnsafeArchive", "is_zip", "read_member"]

DEFAULT_MAX_UNCOMPRESSED: Final = 8 * 1024 * 1024
DEFAULT_MAX_RATIO: Final = 200
MAX_MEMBERS: Final = 64
_CHUNK: Final = 64 * 1024


class UnsafeArchive(ValueError):
    """A feed archive the hub refuses to unpack."""


def is_zip(body: bytes) -> bool:
    """A local-file-header (or empty-archive) zip signature."""
    return body[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def _safe_name(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name.split("/", 1)[0]:
        return False
    return ".." not in name.split("/")


def read_member(
    body: bytes,
    *,
    suffixes: Sequence[str],
    max_uncompressed: int = DEFAULT_MAX_UNCOMPRESSED,
    max_ratio: int = DEFAULT_MAX_RATIO,
    max_members: int = MAX_MEMBERS,
) -> bytes:
    """The bytes of the single member ending in one of ``suffixes`` (case-insensitive)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise UnsafeArchive(f"not a zip archive ({type(exc).__name__})") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > max_members:
            raise UnsafeArchive(f"archive has {len(infos)} members (at most {max_members})")
        for info in infos:
            if not _safe_name(info.filename):
                raise UnsafeArchive(f"unsafe member name {info.filename[:80]!r}")
            if info.flag_bits & 0x1:
                raise UnsafeArchive(f"encrypted member {info.filename[:80]!r}")
        wanted = tuple(s.lower() for s in suffixes)
        matches = sorted((i for i in infos if not i.is_dir() and i.filename.lower().endswith(wanted)),
                         key=lambda i: i.filename)
        if not matches:
            raise UnsafeArchive(f"no member ending in {', '.join(suffixes)}")
        if len(matches) > 1:
            raise UnsafeArchive(f"{len(matches)} members end in {', '.join(suffixes)} (ambiguous archive)")
        info = matches[0]
        if info.file_size > max_uncompressed:
            raise UnsafeArchive(f"member {info.filename[:80]!r} declares {info.file_size} bytes (cap {max_uncompressed})")
        if info.file_size and info.file_size > max(1, info.compress_size) * max_ratio:
            raise UnsafeArchive(f"member {info.filename[:80]!r} compression ratio over {max_ratio}")
        out = bytearray()
        try:
            with archive.open(info) as fh:
                while chunk := fh.read(_CHUNK):
                    out += chunk
                    if len(out) > max_uncompressed:
                        raise UnsafeArchive(f"member {info.filename[:80]!r} inflates beyond {max_uncompressed} bytes")
        except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, OSError, RuntimeError) as exc:
            raise UnsafeArchive(f"member {info.filename[:80]!r} is corrupt ({type(exc).__name__})") from None
        return bytes(out)
