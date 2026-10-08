"""Trade-press RSS adapter (``100`` §3): RSS 2.0 / Atom → untrusted news items.

Only the headline, a <= 300 character plain-text snippet (from ``description``/``summary``, HTML
stripped; ``content:encoded`` is never read) and the link are kept (copyright: headline + snippet +
link only). Items go to the quarantine (guard + extractor) before they can become ``inference``
signals. XML is parsed with ``defusedxml`` (no entity expansion / external entities).
"""

from __future__ import annotations

import html
import re
from typing import Final
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from signal_hub import scoring
from signal_hub.models import NewsItemDraft, SourceContext, canonical_url, clip, safe_source_url
from signal_hub.timeutil import not_after, parse_ts

__all__ = ["MAX_SNIPPET_CHARS", "RssError", "parse_feed", "strip_html"]

MAX_SNIPPET_CHARS: Final = 300
LICENSE: Final = "Headline, snippet and link only (publisher copyright)"
_TAG_RE: Final = re.compile(r"<[^>]*>")
_ATOM: Final = "{http://www.w3.org/2005/Atom}"


class RssError(ValueError):
    pass


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _text(el: Element | None) -> str | None:
    if el is None:
        return None
    return "".join(el.itertext()).strip() or None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(
    body: bytes,
    ctx: SourceContext,
    *,
    connector_id: str,
    source_name: str,
    max_items: int = 40,
) -> list[NewsItemDraft]:
    try:
        root = SafeET.fromstring(body)
    except Exception as exc:
        raise RssError(f"{connector_id}: not well-formed XML ({type(exc).__name__})") from exc
    items: list[tuple[str | None, str | None, str | None, str | None, str | None]] = []
    if _local(root.tag) == "rss" or root.find("channel") is not None:
        channel = root.find("channel")
        for item in (channel.findall("item") if channel is not None else []):
            items.append((
                _text(item.find("title")),
                _text(item.find("link")),
                _text(item.find("guid")),
                _text(item.find("pubDate")) or _text(item.find("{http://purl.org/dc/elements/1.1/}date")),
                _text(item.find("description")),
            ))
    elif _local(root.tag) == "feed":
        for entry in root.findall(f"{_ATOM}entry"):
            link_el = next((ln for ln in entry.findall(f"{_ATOM}link") if ln.get("rel") in (None, "alternate")), None)
            items.append((
                _text(entry.find(f"{_ATOM}title")),
                link_el.get("href") if link_el is not None else None,
                _text(entry.find(f"{_ATOM}id")),
                _text(entry.find(f"{_ATOM}published")) or _text(entry.find(f"{_ATOM}updated")),
                _text(entry.find(f"{_ATOM}summary")),
            ))
    else:
        raise RssError(f"{connector_id}: neither RSS 2.0 nor Atom")

    drafts: list[NewsItemDraft] = []
    seen: set[str] = set()
    for title_raw, link, guid, date_text, description in items[: max(0, max_items)]:
        title = clip(strip_html(title_raw), 300)
        if not title:
            continue
        url = safe_source_url(link)
        native = (guid or "").strip()[:400] or canonical_url(link) or ""
        if not native or native in seen:
            continue
        seen.add(native)
        drafts.append(NewsItemDraft(
            tenant=ctx.tenant,
            connector_id=ctx.connector(connector_id),
            native_key=ctx.key(native),
            origin=ctx.origin("news"),
            source_name=ctx.source_name(source_name),
            license=LICENSE,
            authority_bp=scoring.AUTHORITY["rss"],
            url=url,
            title=title,
            snippet=clip(strip_html(description), MAX_SNIPPET_CHARS),
            published_at=not_after(parse_ts(date_text), ctx.retrieved_at),
            retrieved_at=ctx.retrieved_at,
            context={"native_id": native},
            pack_id=ctx.pack_id,
        ))
    return drafts
