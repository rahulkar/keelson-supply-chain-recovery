"""PDF text extraction in a child process (knowledge-base uploads, contract 90 §7).

An uploaded PDF is untrusted: a 200 KB file of flate-compressed page content can make pypdf spend
minutes of CPU and ~1 GB of memory (measured: 20 pages x 10 MB of ``q Q`` operators → 425 s,
937 MB RSS), and pypdf cannot be pre-empted inside a page. engine-api therefore never parses a
PDF in-process: :func:`extract_pdf_isolated` runs ``python -I -m keelson_engine.core.kb_pdf`` with

* ``RLIMIT_AS`` = ``KB_PDF_MEMORY_MB`` and ``RLIMIT_CPU`` (set by the child before parsing),
* a wall-clock timeout ``KB_PDF_TIMEOUT_S`` (the child is killed),
* a minimal environment (no secrets are inherited), one extraction at a time per process,

and the tightened pypdf limits + text cap of :func:`keelson_engine.core.kb_text.extract`. Any
failure of the child is ``422 pdf_too_complex``; ordinary refusals (``invalid_pdf``,
``encrypted_pdf``, ``no_text``, ``too_many_*``) come back unchanged.

Protocol: stdin = one JSON line ``{filename, title, max_pages, max_chars, memory_mb, cpu_s}`` +
the raw PDF bytes; stdout = one JSON object ``{ok: true, mime, title, sections: [[heading, body]]}``
or ``{ok: false, status, code, message}``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import weakref
from typing import Any

from keelson_engine.core.kb_text import ExtractedText, KbTextError

__all__ = ["extract_pdf_isolated", "main"]

_TOO_COMPLEX = (422, "pdf_too_complex", "the PDF is too complex to extract text from")
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _too_complex() -> KbTextError:
    return KbTextError(*_TOO_COMPLEX)


def _lock() -> asyncio.Lock:
    """One PDF child at a time per event loop (bounds the extra memory to one ``KB_PDF_MEMORY_MB``)."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


async def extract_pdf_isolated(filename: str, data: bytes, *, title: str | None, max_pages: int,
                               max_chars: int | None, timeout_s: float, memory_mb: int) -> ExtractedText:
    """Extract a PDF's sections in a resource-limited child process. Raises :class:`KbTextError`."""
    header = json.dumps({"filename": filename, "title": title, "max_pages": int(max_pages),
                         "max_chars": max_chars, "memory_mb": int(memory_mb),
                         "cpu_s": max(1, math.ceil(timeout_s))}).encode("utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    async with _lock():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "keelson_engine.core.kb_pdf",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(header + b"\n" + data), timeout=timeout_s)
        except TimeoutError:
            raise _too_complex() from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    try:
        result = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        result = None
    if not isinstance(result, dict):
        from keelson_engine.core.logs import get_logger

        get_logger(__name__).warning("pdf extraction child failed", returncode=proc.returncode,
                                     stderr=err.decode("utf-8", "replace")[-500:])
        raise _too_complex()
    if not result.get("ok"):
        raise KbTextError(int(result["status"]), str(result["code"]), str(result["message"]))
    sections = [(h if isinstance(h, str) else None, str(b)) for h, b in result["sections"]]
    return ExtractedText(mime=str(result["mime"]), title=str(result["title"]), sections=sections)


def _set_limits(memory_mb: int, cpu_s: int) -> None:
    try:
        import resource
    except ImportError:  # not POSIX (a Windows dev box): the parent's timeout still applies
        return
    mem = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))


def _run(raw: bytes) -> dict[str, Any]:
    head, _, data = raw.partition(b"\n")
    params = json.loads(head)
    _set_limits(params["memory_mb"], params["cpu_s"])
    from keelson_engine.core.kb_text import extract

    try:
        ex = extract(params["filename"], data, title=params["title"], max_pdf_pages=params["max_pages"],
                     max_pdf_chars=params["max_chars"])
    except KbTextError as exc:
        return {"ok": False, "status": exc.status, "code": exc.code, "message": exc.message}
    except MemoryError:
        return {"ok": False, "status": _TOO_COMPLEX[0], "code": _TOO_COMPLEX[1], "message": _TOO_COMPLEX[2]}
    if ex.mime != "application/pdf":
        return {"ok": False, "status": 415, "code": "unsupported_media_type", "message": "not a PDF upload"}
    return {"ok": True, "mime": ex.mime, "title": ex.title, "sections": [[h, b] for h, b in ex.sections]}


def main() -> None:
    raw = sys.stdin.buffer.read()
    result = _run(raw)
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
