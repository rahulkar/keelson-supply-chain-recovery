"""Knowledge-base text handling (pure, no I/O): upload type detection, text extraction
(Markdown / plain text / PDF via pypdf) and heading-aware chunking.

Chunking (``docs/contracts/90-m4-investigation.md`` §7: "chunk by headings (~600 tokens, 80
overlap)"):

* Markdown is split into **sections** at ATX headings (``#`` … ``######``; headings inside fenced
  code blocks are ignored). A chunk never spans two sections; its ``heading`` is the heading path
  (``"12. Force majeure > 12.2 Notice"``; the document's own H1 is left out when it is the title).
* Plain text is one section without a heading; a PDF is one section per page (``"Page 3"``).
* Inside a section, paragraphs are packed greedily up to ``max_tokens``; a paragraph that is too
  long is split into sentences, a sentence into word windows, a word into character windows. Each
  new chunk of the same section starts with the last ``overlap_tokens`` worth of words of the
  previous chunk.
* Tokens are **estimated** as ``ceil(chars / 4)`` (deterministic and model-independent; the
  embedding models' own tokenizers are not available offline). Real token counts of English text
  are usually a little lower, so chunks stay safely under the embedding input limits.
"""

from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath

__all__ = [
    "MIME_BY_EXTENSION",
    "Chunk",
    "ExtractedText",
    "KbTextError",
    "chunk_sections",
    "detect_mime",
    "embedding_input",
    "estimate_tokens",
    "extract",
    "markdown_sections",
    "parse_heading",
    "safe_filename",
]

MIME_BY_EXTENSION = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}

MAX_HEADING_SOURCE_CHARS = 1000
"""Only this much of a heading line is cleaned up (a heading is cut to 200 characters anyway)."""
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_PARA_SPLIT_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_RE = re.compile(r"(?<=[.!?;:])\s+(?=\S)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FILENAME_BAD_RE = re.compile(r"[\x00-\x1f\x7f]")


class KbTextError(ValueError):
    """Upload rejected: ``status`` (HTTP) + ``code`` (enum-like) + a safe message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    text: str
    tokens: int
    heading: str | None


@dataclass(frozen=True, slots=True)
class ExtractedText:
    mime: str
    title: str
    sections: list[tuple[str | None, str]]
    """``(heading, body)`` in document order."""

    @property
    def chars(self) -> int:
        return sum(len(b) for _, b in self.sections)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4) if text else 0


def safe_filename(name: str | None) -> str:
    """Base name only (no directories), control characters removed, ≤ 200 characters."""
    raw = (name or "").replace("\\", "/")
    base = PurePath(raw).name if raw else ""
    base = _FILENAME_BAD_RE.sub("", base).strip()
    return base[:200] or "document"


def detect_mime(filename: str, data: bytes) -> str:
    """MIME type from the file extension (the browser's Content-Type is not trusted), checked
    against the content: a PDF must start with ``%PDF-``; text must not contain NUL bytes."""
    ext = PurePath(filename.lower()).suffix
    mime = MIME_BY_EXTENSION.get(ext)
    if mime is None:
        raise KbTextError(415, "unsupported_media_type", "only .md, .txt and .pdf files can be uploaded")
    if mime == "application/pdf":
        if b"%PDF-" not in data[:1024]:
            raise KbTextError(422, "invalid_pdf", "the file is not a PDF document")
    elif b"\x00" in data:
        raise KbTextError(422, "invalid_text", "the file is not a text document")
    return mime


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    return _CONTROL_RE.sub("", text)


def _decode(data: bytes) -> str:
    try:
        return _clean(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise KbTextError(422, "invalid_text", "text files must be UTF-8 encoded") from None


def parse_heading(line: str) -> tuple[int, str] | None:
    """ATX heading ``(level, raw text)`` or None — a linear scan, no backtracking regex (an upload
    can hold multi-megabyte lines; ``^ {0,3}(#{1,6})[ \\t]+(.+?)(?:[ \\t]+#+)?[ \\t]*$`` is
    quadratic on a long run of blanks and held the GIL for minutes)."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith("#"):
        return None
    level = len(stripped) - len(stripped.lstrip("#"))
    rest = stripped[level:]
    if level > 6 or not rest or rest[0] not in " \t":
        return None
    content = rest.strip(" \t")
    unclosed = content.rstrip("#")
    if unclosed != content and (not unclosed or unclosed[-1] in " \t"):
        content = unclosed.rstrip(" \t")  # optional closing sequence "## Title ##"
    return level, content


def _strip_md_inline(text: str) -> str:
    text = text[:MAX_HEADING_SOURCE_CHARS]  # the link regex below is quadratic on "[[[[…"
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return " ".join(text.split())


def markdown_sections(text: str) -> tuple[str | None, list[tuple[list[str], str]]]:
    """``(first H1, [(heading path, body), …])``. Headings in fenced code blocks are text."""
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    first_h1: str | None = None
    in_fence: str | None = None

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            sections.append(([t for _, t in stack], content))
        body.clear()

    for line in text.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if in_fence is None:
                in_fence = marker
            elif marker == in_fence:
                in_fence = None
            body.append(line)
            continue
        heading = None if in_fence else parse_heading(line)
        if heading:
            flush()
            level = heading[0]
            title = _strip_md_inline(heading[1])[:200]
            if level == 1 and first_h1 is None and title:
                first_h1 = title
            while stack and stack[-1][0] >= level:
                stack.pop()
            if title:
                stack.append((level, title))
            continue
        body.append(line)
    flush()
    return first_h1, sections


def _heading_path(path: list[str], title: str) -> str | None:
    if len(path) > 1 and path[0] == title:
        path = path[1:]
    joined = " > ".join(path)
    return joined[:500] or None


PDF_MAX_STREAM_BYTES = 4_000_000
"""pypdf decode limits per stream (its defaults are 75 MB): normal page content streams are KBs."""
PDF_LIMITS = {
    "maximum_declared_stream_length": PDF_MAX_STREAM_BYTES,
    "array_based_stream_maximum_output_length": PDF_MAX_STREAM_BYTES,
    "lzw_maximum_output_length": PDF_MAX_STREAM_BYTES,
    "run_length_maximum_output_length": PDF_MAX_STREAM_BYTES,
    "zlib_maximum_output_length": PDF_MAX_STREAM_BYTES,
    "image_maximum_buffer_size": PDF_MAX_STREAM_BYTES,
    "xform_maximum_invocations_per_extraction": 500,
}


def extract(filename: str, data: bytes, *, title: str | None = None, max_pdf_pages: int = 500,
            max_pdf_chars: int | None = None) -> ExtractedText:
    """Detect the type and extract ``(heading, body)`` sections. Raises :class:`KbTextError`.

    PDFs are parsed with tightened pypdf limits and stop once the text exceeds ``max_pdf_chars``;
    engine-api never calls this for PDFs in-process (see ``core/kb_pdf.py``)."""
    mime = detect_mime(filename, data)
    stem = PurePath(filename).stem.replace("_", " ").replace("-", " ").strip() or "Document"
    wanted = " ".join((title or "").split())[:200] or None
    if mime == "text/markdown":
        h1, md_sections = markdown_sections(_decode(data))
        doc_title = wanted or h1 or stem
        sections = [(_heading_path(path, doc_title), body) for path, body in md_sections]
    elif mime == "text/plain":
        doc_title = wanted or stem
        content = _decode(data).strip()
        sections = [(None, content)] if content else []
    else:
        doc_title, sections = _pdf_sections(data, wanted or None, stem, max_pdf_pages, max_pdf_chars)
    if not any(body.strip() for _, body in sections):
        raise KbTextError(422, "no_text", "the document contains no extractable text")
    return ExtractedText(mime=mime, title=doc_title[:200], sections=sections)


def _pdf_sections(data: bytes, wanted: str | None, stem: str, max_pages: int,
                  max_chars: int | None = None) -> tuple[str, list[tuple[str | None, str]]]:
    from pypdf import apply_configuration

    with apply_configuration(**PDF_LIMITS):
        return _pdf_sections_limited(data, wanted, stem, max_pages, max_chars)


def _pdf_sections_limited(data: bytes, wanted: str | None, stem: str, max_pages: int,
                          max_chars: int | None) -> tuple[str, list[tuple[str | None, str]]]:
    from pypdf import PdfReader
    from pypdf.errors import LimitReachedError, PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:  # noqa: BLE001 - unsupported encryption → refused below
                ok = 0
            if not ok:
                raise KbTextError(422, "encrypted_pdf", "password-protected PDFs are not supported")
        pages = reader.pages
        if len(pages) > max_pages:
            raise KbTextError(422, "too_many_pages", f"the PDF has more than {max_pages} pages")
        sections: list[tuple[str | None, str]] = []
        chars = 0
        for i, page in enumerate(pages, start=1):
            content = _clean(page.extract_text() or "").strip()
            if content:
                sections.append((f"Page {i}", content))
                chars += len(content)
                if max_chars is not None and chars > max_chars:
                    raise KbTextError(422, "too_many_chunks", "the document is too long")
        meta_title = None
        try:
            meta = reader.metadata
            meta_title = " ".join(str(meta.title).split())[:200] if meta and meta.title else None
        except Exception:  # noqa: BLE001 - metadata is optional
            meta_title = None
    except KbTextError:
        raise
    except (LimitReachedError, MemoryError, RecursionError):
        raise KbTextError(422, "pdf_too_complex", "the PDF is too complex to extract text from") from None
    except (PyPdfError, ValueError, KeyError, TypeError, IndexError, AttributeError, AssertionError, OSError):
        raise KbTextError(422, "invalid_pdf", "the PDF could not be read") from None
    return wanted or meta_title or stem, sections


# ───────────────────────────── chunking ─────────────────────────────


def _hard_split(word: str, max_tokens: int) -> list[str]:
    size = max(1, max_tokens * 4)
    return [word[i:i + size] for i in range(0, len(word), size)]


# The packers below track running character lengths instead of re-joining the current chunk for
# every piece: ``estimate_tokens(s) > max_tokens`` ⇔ ``len(s) > 4 * max_tokens``. Re-joining was
# quadratic (a 5 MB upload of tiny sentences took minutes of CPU).


def _word_windows(text: str, max_tokens: int) -> list[str]:
    max_chars = 4 * max_tokens
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for word in text.split():
        parts = _hard_split(word, max_tokens) if len(word) > max_chars else [word]
        for part in parts:
            if cur and cur_len + 1 + len(part) > max_chars:
                out.append(" ".join(cur))
                cur, cur_len = [], 0
            cur_len += (1 if cur else 0) + len(part)
            cur.append(part)
    if cur:
        out.append(" ".join(cur))
    return out


def _pieces(body: str, max_tokens: int) -> list[tuple[str, str]]:
    """Atomic ``(text, separator-before)`` pieces of ≤ ``max_tokens`` each."""
    out: list[tuple[str, str]] = []
    for para in _PARA_SPLIT_RE.split(body):
        para = para.strip()
        if not para:
            continue
        sep = "\n\n"
        if estimate_tokens(para) <= max_tokens:
            out.append((para, sep))
            continue
        for sentence in _SENTENCE_RE.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            windows = [sentence] if estimate_tokens(sentence) <= max_tokens else _word_windows(sentence, max_tokens)
            for w in windows:
                out.append((w, sep))
                sep = " "
    return out


def _join(parts: list[tuple[str, str]]) -> str:
    if not parts:
        return ""
    return parts[0][0] + "".join(sep + text for text, sep in parts[1:])


def _tail(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0:
        return ""
    max_chars = 4 * overlap_tokens
    taken: list[str] = []
    taken_len = 0
    for word in reversed(text.split()):
        new_len = len(word) + (1 + taken_len if taken else 0)
        if new_len > max_chars:
            break
        taken.append(word)
        taken_len = new_len
    return " ".join(reversed(taken))


def _chunk_body(body: str, max_tokens: int, overlap_tokens: int, limit: int | None = None) -> list[str]:
    max_chars = 4 * max_tokens
    chunks: list[str] = []
    cur: list[tuple[str, str]] = []
    cur_len = 0

    def grown(piece: tuple[str, str]) -> int:  # len(_join([*cur, piece]))
        return cur_len + len(piece[1]) + len(piece[0]) if cur else len(piece[0])

    for piece in _pieces(body, max_tokens):
        if cur and grown(piece) > max_chars:
            emitted = _join(cur)
            chunks.append(emitted)
            if limit is not None and len(chunks) > limit:
                return chunks
            tail = _tail(emitted, overlap_tokens)
            cur = [(tail, "")] if tail else []  # the overlap: last words of the previous chunk
            cur_len = len(tail)
            if cur and grown(piece) > max_chars:
                cur, cur_len = [], 0
        cur_len = grown(piece)
        cur.append(piece)
    if cur:
        chunks.append(_join(cur))
    return chunks


def chunk_sections(sections: list[tuple[str | None, str]], *, max_tokens: int = 600,
                   overlap_tokens: int = 80, max_chunks: int | None = None) -> list[Chunk]:
    """Heading-aware chunks. With ``max_chunks``, stops as soon as more than that many exist (the
    caller refuses the document; there is no point chunking the rest of a huge upload)."""
    if max_tokens < 8:
        raise ValueError("max_tokens must be >= 8")
    overlap_tokens = max(0, min(overlap_tokens, max_tokens // 2))
    out: list[Chunk] = []
    for heading, body in sections:
        remaining = None if max_chunks is None else max_chunks - len(out)
        for text in _chunk_body(body, max_tokens, overlap_tokens, remaining):
            out.append(Chunk(ordinal=len(out), text=text, tokens=estimate_tokens(text), heading=heading))
        if max_chunks is not None and len(out) > max_chunks:
            break
    return out


def embedding_input(title: str, heading: str | None, text: str) -> str:
    """What is embedded for a chunk: document title + heading path give the vector its context."""
    head = f"{title}\n{heading}" if heading else title
    return f"{head}\n\n{text}"
