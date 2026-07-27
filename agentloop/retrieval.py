"""RetrievalBackend — the memory-relevance seam (roadmap slice 2).

`MemoryService.facts_for_prompt` used to select approved facts by tier and then
alphabetical key. That ordering has nothing to do with the task at hand: past
the injection cap, facts drop by alphabetical accident, so the one fact that
answers the current task can be crowded out by irrelevant ones that happen to
sort earlier. This module ranks them by relevance to a query instead.

It mirrors `runner.ModelRunner`: the loop and `MemoryService` talk to a
protocol, never to a retrieval engine. One backend ships — `HashingBackend`,
stdlib only: hashed bag-of-words vectors and cosine similarity, brute-forced
over the candidate set. It matches on shared vocabulary, not paraphrase; that is
the price of zero dependencies, and it is still far better than alphabetical.

The seam is the point, not the backend count. A real embedding model (or a
vector index in front of one) is a drop-in `search()`, and the day the ranking
needs to understand paraphrase, that is the change to make. A Chroma backend
shipped here briefly and was removed: it embedded with the same `embed()` as the
stdlib backend, so it returned the same order at any fact count this store
holds — an optional, CI-untested dependency buying nothing but a tie-break bug.

**The approval gate does not live here.** Callers pass in candidates already
filtered by `approved`, so no backend — present or future, local or remote — can
surface an unvetted fact. A future index must keep that property: it may only
re-rank rows the store just handed over, never resurrect a revoked one, which is
what makes any such index a derived cache rather than a second source of truth.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

# Vector width. Small enough that brute-force cosine over a few thousand facts
# is free, wide enough that unrelated short facts rarely collide.
_DIMS = 256

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Only the words that carry no signal at all. Deliberately tiny: an aggressive
# stopword list mostly discards vocabulary that would have matched.
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or that the this to with".split()
)

_MAX_QUERY_CHARS = 1000


def _dim_for(token: str) -> int:
    """Stable dimension for a token.

    blake2b, *not* builtin `hash()`: the latter is salted per process, so an
    index written by one process would silently fail to match a query embedded
    by another — the kind of bug that shows up as quietly worse retrieval
    rather than as an error.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % _DIMS


def embed(text: str) -> list[float]:
    """Hashed bag-of-words vector, L2-normalized (so cosine is a dot product).

    Empty or all-stopword text yields the zero vector, which scores 0.0 against
    everything — an empty query ranks nothing rather than ranking arbitrarily.
    """
    vec = [0.0] * _DIMS
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS:
            continue
        vec[_dim_for(token)] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two `embed` outputs (already normalized)."""
    return sum(x * y for x, y in zip(a, b))


def _fact_text(candidate: dict) -> str:
    """What a memory row is matched on. The key carries as much signal as the
    value ('test_command' is the whole question a worker is asking)."""
    return f"{candidate.get('key', '')} {candidate.get('value', '')}"


def rank_exact(
    query: str, candidates: list[dict], top_k: int
) -> list[tuple[int, float]]:
    """Exact cosine ranking, best first, as `(memory id, score)` pairs.

    The sort is stable and keyed only on the negated score, so equally relevant
    facts keep the order the caller supplied — which is the store's
    `ORDER BY tier, key`. That is what makes tier the tie-break between facts
    the query cannot distinguish, deterministically and without this module
    needing to know the tier policy.
    """
    q = embed(query)
    scored = [(int(c["id"]), cosine(q, embed(_fact_text(c)))) for c in candidates]
    scored.sort(key=lambda pair: -pair[1])
    return scored[:top_k]


class RetrievalBackend(Protocol):
    def search(
        self, query: str, candidates: list[dict], top_k: int
    ) -> list[tuple[int, float]]:
        """Rank `candidates` (approved memory rows) against `query`.

        Returns at most `top_k` `(memory id, score)` pairs, best first. Ids not
        returned are simply not injected.
        """
        ...


class HashingBackend:
    """Stdlib backend: brute-force cosine over the candidate set."""

    name = "hash"

    def search(
        self, query: str, candidates: list[dict], top_k: int
    ) -> list[tuple[int, float]]:
        return rank_exact(query, candidates, top_k)


def get_backend(name: str = "hash", config=None) -> RetrievalBackend | None:
    """Resolve a `memory_retrieval_backend` setting to a backend.

    `hash` is the stdlib ranking; `none` disables ranking entirely and restores
    the pre-slice-2 alphabetical selection. Anything else raises: a name this
    build does not implement is a misconfiguration, and silently substituting a
    different ranking is how a run's behaviour changes without anyone noticing.

    `config` is unused today and kept deliberately — it is where a backend that
    needs settings (an endpoint, a model name, an index path) reads them, so
    adding one does not change every call site.
    """
    if name == "none":
        return None
    if name == "hash":
        return HashingBackend()
    raise ValueError(
        f"Unknown memory_retrieval_backend: {name!r} (expected 'hash' or 'none')"
    )
