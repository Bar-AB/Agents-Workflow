"""Loop configuration.

Defaults reflect the seed spec:
- approve_threshold: validator confidence >= this -> task done (spec §5, ~0.7).
- severe_threshold: confidence < this -> severe disagreement -> escalate to
  human / full redo instead of another revision cycle.
- max_revisions: bounded retries before escalation (spec §4.5).
- max_cost_usd_per_task / max_tokens_per_task: hard budget caps (spec §11) —
  a stuck revision loop trips to human review instead of burning spend.

All values are tunable globally here or per task via Task.overrides.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class LoopConfig:
    approve_threshold: float = 0.70
    severe_threshold: float = 0.40
    max_revisions: int = 3
    max_tokens_per_task: int = 500_000
    max_cost_usd_per_task: float = 5.00
    # Tasks at or above this risk level require explicit human sign-off even
    # after validator approval (spec §4.7). Levels: 0=low, 1=normal, 2=high.
    human_review_risk_level: int = 2
    db_path: str = "agentloop.db"
    registry_path: str = "agents.json"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "LoopConfig":
        """Load from a JSON file if it exists, else defaults."""
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})
        return cls()


# Rough $/1M tokens for cost estimates (spec §6). Update as pricing changes;
# unknown models fall back to DEFAULT_PRICING.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model: (input $/1M, output $/1M)
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (15.00, 75.00),
    "mock": (0.0, 0.0),
}
DEFAULT_PRICING: tuple[float, float] = (3.00, 15.00)


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000
