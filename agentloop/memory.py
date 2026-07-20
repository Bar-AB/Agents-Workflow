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

# Cap injected context so memory can't crowd out the actual task.
_MAX_FACTS_IN_PROMPT = 20
_MAX_VALUE_CHARS = 400


class MemoryService:
    def __init__(self, store: Store, promote_threshold: int = 3):
        self.store = store
        self.promote_threshold = promote_threshold

    # -- reads ---------------------------------------------------------------

    def facts_for_prompt(self, limit: int = _MAX_FACTS_IN_PROMPT) -> str:
        """Approved facts as a prompt block. Loop-tier facts come first: they
        are the cross-project ones that earned their place."""
        rows = [r for r in self.store.memory_list(approved_only=True)]
        rows.sort(key=lambda r: (0 if r["tier"] == "loop" else 1, r["key"]))
        rows = rows[:limit]
        if not rows:
            return ""
        lines = [f"- ({r['tier']}) {r['key']}: {r['value'][:_MAX_VALUE_CHARS]}"
                 for r in rows]
        self._record_reads(rows)
        return "\n".join(lines)

    def read(self, tier: str, key: str) -> str | None:
        value = self.store.memory_read(tier, key, approved_only=True)
        if value is not None:
            self.maybe_promote(tier, key)
        return value

    # -- writes --------------------------------------------------------------

    def remember(self, tier: str, key: str, value: str,
                 approved: bool = False) -> None:
        """Record a candidate fact. Unapproved by default: a human gates it
        before it can ever influence a prompt."""
        self.store.memory_write(tier, key, value, approved=approved)

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
        self.store.memory_write("loop", key, row["value"],
                                approved=bool(row["approved"]))
        self.store.log_event(None, "memory_promoted", {
            "key": key, "from": "project", "to": "loop",
            "hit_count": row["hit_count"]})
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
