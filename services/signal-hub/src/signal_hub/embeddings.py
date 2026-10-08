"""Cluster terms and the local similarity profile ``local-terms-v1`` (``docs/contracts/123`` §3).

Pure and deterministic (integer arithmetic only): the same input gives the same terms, vectors and
similarities in the hub, the tests and ``data/scenarios/validate.py``. No model, no pgvector.

**Cluster terms** (:func:`cluster_terms`, stored in ``signal.cluster_terms``, sorted, unique,
at most :data:`MAX_TERMS`) describe *what* happened and *where*, from structured fields only:

* ``k:<kind>``, ``s:<subtype>``, ``t:<taxonomy key>`` (unless ``OTHER``);
* ``p:<word>`` — place words: the words of the extracted place / port / chokepoint mentions,
  **including unresolved ones** (an unknown terminal name still carries "hamburg"); a signal
  without mentions (feeds, connector events) uses the words of the gazetteer's curated names of
  its port and chokepoint refs instead. Mentions passed the quarantine (pattern screen, guard,
  strict schema) and are short lists; their words are only ever compared, never shown or sent
  anywhere except an opt-in remote embedding (§6). (Adding the curated names to mentions would
  dilute a resolved report against an unresolved one naming the same facility differently);
* ``o:<word>`` — organisation words of the extracted company mentions.

Country and region refs add no words (too coarse: every report from one country would look alike);
a mention that spells a country still does. Headlines and snippets are never terms.

**Local vectors** (:func:`local_vector`) weight the terms (``p:`` 2, every other term 1); an event
vector is the sum of its current members' vectors. :func:`similarity_bp` is the cosine similarity
in basis points (exact integer arithmetic, half-up), and **0 unless the two vectors share at least
one place or organisation word**: kind/subtype agreement alone is never evidence that two reports
describe the same disruption.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from keelson_contracts.signals import LOCAL_SIMILARITY_PROFILE, taxonomy_key_for
from signal_hub.gazetteer import Gazetteer, normalise

__all__ = [
    "LOCAL_PROFILE",
    "MAX_TERMS",
    "PLACE_STOPWORDS",
    "WEIGHTS",
    "add_vectors",
    "cluster_terms",
    "cluster_text",
    "local_vector",
    "similarity_bp",
    "term_words",
]

LOCAL_PROFILE: Final = LOCAL_SIMILARITY_PROFILE
MAX_TERMS: Final = 64
MAX_TERM_CHARS: Final = 80
MAX_MENTIONS: Final = 40
WEIGHTS: Final[dict[str, int]] = {"k": 1, "s": 1, "t": 1, "p": 2, "o": 1}
_SPECIFIC: Final = ("p:", "o:")
_NAMED_SCHEMES: Final = ("UNLOCODE", "CHOKEPOINT")
PLACE_STOPWORDS: Final = frozenset({
    "a", "al", "an", "and", "at", "by", "city", "container", "containers", "de", "del", "der", "di", "du", "el",
    "for", "from", "harbor", "harbour", "in", "la", "le", "near", "of", "off", "on", "port", "ports", "seaport",
    "the", "terminal", "terminals", "to", "von",
})
_ORG_STOPWORDS: Final = frozenset({
    "a", "and", "co", "company", "corp", "corporation", "group", "inc", "limited", "llc", "ltd", "of", "plc", "the",
})


def term_words(text: str, stopwords: frozenset[str] = PLACE_STOPWORDS) -> list[str]:
    """Lowercase ASCII words of ``text`` (gazetteer normalisation) without stopwords, 2..40 chars."""
    return [w for w in normalise(text).split() if 2 <= len(w) <= 40 and w not in stopwords]


def cluster_terms(
    *,
    kind: str,
    subtype: str,
    location_refs: Iterable[str],
    gaz: Gazetteer,
    mentions: Iterable[str] = (),
    companies: Iterable[str] = (),
) -> tuple[str, ...]:
    """The cluster terms of one signal (see the module docstring)."""
    core = [f"k:{kind}", f"s:{subtype}"]
    taxonomy = taxonomy_key_for(subtype)
    if taxonomy != "OTHER":
        core.append(f"t:{taxonomy}")
    places: set[str] = set()
    for mention in list(mentions)[:MAX_MENTIONS]:
        places.update(term_words(mention))
    if not places:
        for ref in location_refs:
            if ref.split(":", 1)[0] in _NAMED_SCHEMES and (name := gaz.display_name(ref)):
                places.update(term_words(name))
    orgs: set[str] = set()
    for company in list(companies)[:MAX_MENTIONS]:
        orgs.update(term_words(company, _ORG_STOPWORDS))
    terms = [t[:MAX_TERM_CHARS] for t in core]
    room = MAX_TERMS - len(terms)
    specific = [f"p:{w}" for w in sorted(places)] + [f"o:{w}" for w in sorted(orgs)]
    terms += specific[:room]
    return tuple(sorted(set(terms)))


def local_vector(terms: Iterable[str]) -> dict[str, int]:
    """Sparse weighted term vector of one signal (unknown prefixes are ignored)."""
    out: dict[str, int] = {}
    for term in terms:
        weight = WEIGHTS.get(term.split(":", 1)[0])
        if weight:
            out[term] = weight
    return out


def add_vectors(vectors: Iterable[Mapping[str, int]]) -> dict[str, int]:
    """Sum of vectors (an event's centroid direction: the sum of its members' vectors)."""
    out: dict[str, int] = {}
    for vec in vectors:
        for term, weight in vec.items():
            out[term] = out.get(term, 0) + weight
    return out


def similarity_bp(a: Mapping[str, int], b: Mapping[str, int]) -> int:
    """Cosine similarity of two non-negative integer vectors in basis points, rounded half-up with
    exact integer arithmetic; 0 when they share no place or organisation term."""
    shared = a.keys() & b.keys()
    if not any(t.startswith(_SPECIFIC) for t in shared):
        return 0
    dot = sum(a[t] * b[t] for t in shared)
    na = sum(w * w for w in a.values())
    nb = sum(w * w for w in b.values())
    if dot <= 0 or na <= 0 or nb <= 0:
        return 0
    # n = floor(10000·dot/sqrt(na·nb) + 1/2) = (k + 1) // 2 with k the largest integer such that
    # k² · na·nb <= (20000·dot)², i.e. k = isqrt((20000·dot)² // (na·nb)).
    k = math.isqrt((20_000 * dot) ** 2 // (na * nb))
    return max(0, min(10_000, (k + 1) // 2))


def cluster_text(terms: Sequence[str], location_refs: Sequence[str], *, headline: str | None = None) -> str:
    """The canonical text a **remote** embedding profile embeds: structured fields only
    (``kind=…; subtype=…; refs=…; places=…; organisations=…``); the untrusted headline is appended
    only when ``SIGNALS_EMBED_INCLUDE_HEADLINE=1`` (off by default)."""

    def values(prefix: str) -> list[str]:
        return [t.split(":", 1)[1] for t in terms if t.startswith(prefix)]

    parts = [
        f"kind={' '.join(values('k:'))}",
        f"subtype={' '.join(values('s:'))}",
        f"taxonomy={' '.join(values('t:'))}",
        f"refs={' '.join(list(location_refs)[:50])}",
        f"places={' '.join(values('p:'))}",
        f"organisations={' '.join(values('o:'))}",
    ]
    if headline:
        parts.append(f"headline={' '.join(headline.split())[:300]}")
    return "; ".join(parts)[:2000]
