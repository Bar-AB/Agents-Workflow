"""Two-tier memory wiring (spec §7).

The store owns the tables; this owns the policy:

- Reads are gated on `approved`. A fact nobody vetted never reaches a prompt,
  because a bad fact entering memory quietly poisons every later task. The gate
  is on the *value*: changing the content of an approved key revokes it (see
  `Store.memory_write`), so a rewrite cannot inherit someone else's approval.
- Writes from agents land unapproved, and surface in the dashboard for a human
  to accept or drop — memory writes are auditable by construction, since every
  one lands in the append-only events log.
- A `project` fact that is relevant to `promote_threshold` tasks is promoted to
  the `loop` tier: repeatedly re-answering the same question is exactly the
  wasted token spend the tiering exists to remove. Relevance, not injection —
  see `_record_reads`.

Selection is ranked by relevance to the calling task when a query is supplied,
through the `retrieval.RetrievalBackend` seam — a vector store slots in behind
`facts_for_prompt` without the loop noticing. With no query (or no backend) the
pre-ranking alphabetical selection is used unchanged: there is nothing to rank
against, and silently inventing an ordering would be worse than the old one.
"""

from __future__ import annotations

from .retrieval import _MAX_QUERY_CHARS, RetrievalBackend
from .store import Store

# Cap injected context so memory can't crowd out the actual task. Pinned facts
# get a separate, smaller ceiling *above* the main cap: they are the facts a
# human declared must always be present, so they bypass the alphabetical
# tail-off that drops ordinary facts past the cap — but are still bounded, so
# pinning everything can't reintroduce the crowding the cap exists to prevent.
_MAX_FACTS_IN_PROMPT = 20
_MAX_PINNED_FACTS = 10
_MAX_VALUE_CHARS = 400


class MemoryService:
    def __init__(
        self,
        store: Store,
        promote_threshold: int = 3,
        backend: RetrievalBackend | None = None,
    ):
        self.store = store
        self.promote_threshold = promote_threshold
        # None = no relevance ranking; selection stays alphabetical.
        self.backend = backend

    # -- reads ---------------------------------------------------------------

    def facts_for_prompt(
        self,
        limit: int = _MAX_FACTS_IN_PROMPT,
        pinned_limit: int = _MAX_PINNED_FACTS,
        query: str = "",
        task_id: int | None = None,
    ) -> tuple[str, dict | None]:
        """Approved facts as a prompt block, plus the provenance of that block.

        Ordering: pinned facts first (they bypass the main cap under their own
        ceiling), then the rest. Within each group, `query` decides — the facts
        most relevant to the task at hand keep the slots, and loop-tier before
        project then alphabetical breaks ties the query cannot. Without a query
        or a backend, that tie-break *is* the whole ordering, i.e. exactly the
        pre-ranking behaviour.

        The caps and the approval gate are unchanged by ranking: relevance only
        decides order, so a fact that fits under the cap is never dropped for
        scoring low, and an unapproved fact is never a candidate however well it
        matches.

        The provenance dict (None when nothing was ranked) is *returned* rather
        than logged here: a retrieval is only meaningful as part of the agent
        invocation it fed, and this service does not know which attempt that is.
        The caller logs it against that attempt — see `agents._invoke`.

        `task_id` is what a hit is counted against. Without it a fact's
        `hit_count` counted prompts, and worker + validator + one revision is
        three prompts inside a single task — so one ordinary task promoted a
        fact on its own at the default threshold of three."""
        approved = list(self.store.memory_list(approved_only=True))

        def order(r):
            return (0 if r["tier"] == "loop" else 1, r["key"])

        pinned = sorted((r for r in approved if r["pinned"]), key=order)
        unpinned = sorted((r for r in approved if not r["pinned"]), key=order)

        scores: dict[int, float] = {}
        ranked = bool(query.strip()) and self.backend is not None
        if ranked:
            pinned = self._rank(query, pinned, pinned_limit, scores)
            unpinned = self._rank(query, unpinned, limit, scores)
            rows = pinned + unpinned
        else:
            rows = pinned[:pinned_limit] + unpinned[:limit]
        if not rows:
            return "", None
        lines = [
            f"- ({r['tier']}){' *' if r['pinned'] else ''} {r['key']}: "
            f"{r['value'][:_MAX_VALUE_CHARS]}"
            for r in rows
        ]
        provenance = (
            self._provenance(query, rows, scores, len(approved)) if ranked else None
        )
        self._record_reads(rows, scores, ranked, task_id)
        return "\n".join(lines), provenance

    def _rank(
        self, query: str, rows: list[dict], top_k: int, scores: dict[int, float]
    ) -> list[dict]:
        """Reorder one group by backend relevance, recording the scores.

        Ranking decides order, never membership. A backend may return fewer than
        `top_k` — a real index returns its own hits, not the caller's set — so
        candidates it ignored are appended behind the ranked ones in the order
        the store supplied (`ORDER BY tier, key`), and the group is truncated to
        `top_k` here rather than wherever the backend felt like stopping. A
        group of N therefore always yields min(N, top_k), and an ignored fact
        scores 0.0: it loses a promotion credit, not its slot, which is the rule
        a low-scoring fact already lived under."""
        if not rows:
            return []
        by_id = {int(r["id"]): r for r in rows}
        ordered = []
        for mem_id, score in self.backend.search(query, rows, top_k):
            row = by_id.pop(int(mem_id), None)
            if row is None:
                continue  # a backend can only ever return fewer, never other
            scores[int(mem_id)] = float(score)
            ordered.append(row)
        # Whatever the backend did not rank, in candidate order, behind it.
        ordered.extend(r for r in rows if int(r["id"]) in by_id)
        return ordered[:top_k]

    def _provenance(
        self,
        query: str,
        rows: list[dict],
        scores: dict[int, float],
        n_candidates: int,
    ) -> dict:
        """What memory put in front of an agent, and why.

        The audit log already records what entered memory; without this it never
        recorded what was *read out of* it, so a bad answer could not be traced
        back to the fact that caused it."""
        return {
            "query": query[:_MAX_QUERY_CHARS],
            "backend": getattr(self.backend, "name", type(self.backend).__name__),
            "n_candidates": n_candidates,
            "n_selected": len(rows),
            "facts": [
                {
                    "id": int(r["id"]),
                    "tier": r["tier"],
                    "key": r["key"],
                    "pinned": bool(r["pinned"]),
                    "score": round(scores.get(int(r["id"]), 0.0), 4),
                }
                for r in rows
            ],
        }

    def read(self, tier: str, key: str, task_id: int | None = None) -> str | None:
        value = self.store.memory_read(tier, key, approved_only=True, task_id=task_id)
        if value is not None:
            self.maybe_promote(tier, key)
        return value

    # -- writes --------------------------------------------------------------

    def remember(
        self,
        tier: str,
        key: str,
        value: str,
        approved: bool = False,
        pinned: bool = False,
    ) -> None:
        """Record a candidate fact. Unapproved by default: a human gates it
        before it can ever influence a prompt. Pinning still requires approval
        to be injected — a pinned but unapproved fact is not read."""
        self.store.memory_write(tier, key, value, approved=approved, pinned=pinned)

    # -- promotion -----------------------------------------------------------

    def maybe_promote(self, tier: str, key: str) -> bool:
        """Promote a hot project fact to loop memory. Returns True if promoted.

        The read of `hit_count` and the move it justifies happen in one
        transaction: with parallel workers two threads can otherwise both read
        the same threshold-crossing count and both promote, writing two
        `memory_promoted` events for one promotion and making the audit log
        disagree with what actually happened.

        Promotion is a *transition* — `Store.memory_promote` moves the row
        rather than copying it, so this cannot fire twice for one fact: the row
        is no longer `project`, and nothing else is."""
        if tier != "project":
            return False
        with self.store.transaction():
            row = self._find(tier, key)
            if row is None or row["hit_count"] < self.promote_threshold:
                return False
            self.store.memory_promote(int(row["id"]))
        return True

    def _record_reads(
        self,
        rows: list[dict],
        scores: dict[int, float],
        ranked: bool,
        task_id: int | None = None,
    ) -> None:
        """Count a hit only where there is evidence the fact was relevant.

        Injection is not evidence: while the store holds fewer facts than the
        cap, *every* approved fact is injected into *every* prompt, so counting
        injections made `hit_count` mean "existed while N tasks ran" and promoted
        the whole project tier to `loop` on schedule. A positive relevance score
        is the weakest signal that actually distinguishes facts from each other,
        so it is what counts — an unranked selection counts nothing at all.

        A hit is counted per *task*, not per prompt: worker, validator and each
        revision retrieve separately, so counting prompts made one ordinary task
        promote a fact on its own at the default threshold of three. The store
        keys the hit on `task_id` and only bumps `hit_count` the first time a
        task turns up, which is what makes "relevant to three tasks" true.

        It is still a proxy. What promotion really wants to know is whether the
        fact changed the agent's output, which needs the `retrieval` events to be
        joined against attempt outcomes; the provenance now attached to each
        attempt is what makes that measurable later.
        """
        if not ranked:
            return
        for r in rows:
            if scores.get(int(r["id"]), 0.0) <= 0.0:
                continue
            self.store.memory_read(
                r["tier"], r["key"], approved_only=True, task_id=task_id
            )
            self.maybe_promote(r["tier"], r["key"])

    def _find(self, tier: str, key: str) -> dict | None:
        for r in self.store.memory_list(tier=tier):
            if r["key"] == key:
                return r
        return None
