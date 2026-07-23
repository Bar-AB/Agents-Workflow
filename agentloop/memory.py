"""Two-tier memory wiring (spec §7).

The store owns the tables; this owns the policy:

- Reads are gated on `approved`. A fact nobody vetted never reaches a prompt,
  because a bad fact entering memory quietly poisons every later task.
- Writes from agents land unapproved, and surface in the dashboard for a human
  to accept or drop — memory writes are auditable by construction, since every
  one lands in the append-only events log.
- A `project` fact read `promote_threshold` times is promoted to the `loop`
  tier: repeatedly re-answering the same question is exactly the wasted token
  spend the tiering exists to remove.

Retrieval is keyword/key-based for now. A vector store slots in behind
`facts_for_prompt` without the loop noticing.
"""

from __future__ import annotations

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
    def __init__(self, store: Store, promote_threshold: int = 3):
        self.store = store
        self.promote_threshold = promote_threshold

    # -- reads ---------------------------------------------------------------

    def facts_for_prompt(
        self, limit: int = _MAX_FACTS_IN_PROMPT, pinned_limit: int = _MAX_PINNED_FACTS
    ) -> str:
        """Approved facts as a prompt block.

        Ordering: pinned facts first (they bypass the main cap under their own
        ceiling), then the rest. Within each group, loop-tier before project,
        then alphabetical — loop facts are the cross-project ones that earned
        their place. Without pinning, the alphabetical tail past `limit` drops
        by accident; pinning is how a must-have fact keeps its slot."""
        approved = list(self.store.memory_list(approved_only=True))
        order = lambda r: (0 if r["tier"] == "loop" else 1, r["key"])
        pinned = sorted((r for r in approved if r["pinned"]), key=order)
        unpinned = sorted((r for r in approved if not r["pinned"]), key=order)
        rows = pinned[:pinned_limit] + unpinned[:limit]
        if not rows:
            return ""
        lines = [
            f"- ({r['tier']}){' *' if r['pinned'] else ''} {r['key']}: "
            f"{r['value'][:_MAX_VALUE_CHARS]}"
            for r in rows
        ]
        self._record_reads(rows)
        return "\n".join(lines)

    def read(self, tier: str, key: str) -> str | None:
        value = self.store.memory_read(tier, key, approved_only=True)
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
        """Promote a hot project fact to loop memory. Returns True if promoted."""
        if tier != "project":
            return False
        row = self._find(tier, key)
        if row is None or row["hit_count"] < self.promote_threshold:
            return False
        # Carry approval across: a fact already vetted for this project does
        # not need re-vetting to be reused, and it stays visible in the log.
        self.store.memory_write(
            "loop", key, row["value"], approved=bool(row["approved"])
        )
        self.store.log_event(
            None,
            "memory_promoted",
            {
                "key": key,
                "from": "project",
                "to": "loop",
                "hit_count": row["hit_count"],
            },
        )
        return True

    def _record_reads(self, rows: list[dict]) -> None:
        for r in rows:
            self.store.memory_read(r["tier"], r["key"], approved_only=True)
            self.maybe_promote(r["tier"], r["key"])

    def _find(self, tier: str, key: str) -> dict | None:
        for r in self.store.memory_list(tier=tier):
            if r["key"] == key:
                return r
        return None
