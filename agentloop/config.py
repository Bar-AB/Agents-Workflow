"""Loop configuration.

Defaults reflect the seed spec:
- approve_threshold: validator confidence >= this -> task done (spec §5, ~0.7).
- severe_threshold: confidence < this -> severe disagreement -> escalate to
  human / full redo instead of another revision cycle.
- max_revisions: bounded retries before escalation (spec §4.5).
- max_cost_usd_per_task / max_tokens_per_task: hard budget caps (spec §11) —
  a stuck revision loop trips to human review instead of burning spend.
- test_command / workspace_root / test_timeout_s: the sandboxed execution seam
  (spec §5). Tests really run; their result is authoritative over the
  validator's self-report.
- memory_promote_threshold: project facts read this many times are promoted to
  loop memory (spec §7).

All values are tunable globally here or via loopconfig.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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

    # Sandboxed test execution (spec §5). The command is allowlisted here, not
    # taken from model output, and never runs through a shell.
    allow_test_exec: bool = True
    test_command: str = "pytest -q"
    test_timeout_s: int = 120
    workspace_root: str = ".agentloop/ws"
    # The subprocess env is scrubbed to an allowlist so generated code can't
    # read ANTHROPIC_API_KEY or other secrets. Extra vars a project genuinely
    # needs go here (base allowlist lives in executor._BASE_ENV_ALLOWLIST).
    sandbox_env_allowlist: list[str] = field(default_factory=list)
    # 'env' = env-scrub only (default). 'strict' asks for a container /
    # no-network / read-only-fs tier when a backend is available and degrades
    # to env-scrub with a warning when it is not (documented residual risk).
    sandbox_isolation: str = "env"

    # Memory (spec §7): a project fact read this often is promoted to the
    # cross-project loop tier.
    memory_promote_threshold: int = 3

    # Infra resilience: a transient runner/executor failure (API 5xx, network
    # blip) is retried up to this many times with exponential backoff before
    # the task escalates to NEEDS_HUMAN with an infra_error reason. This is
    # distinct from a "revise": infra failure is not a task-quality failure and
    # is not counted against max_revisions. Default backoff is 0.0 so tests run
    # instantly; operators raise it in production. Delay = backoff * 2**(n-1).
    infra_max_retries: int = 2
    infra_retry_backoff_s: float = 0.0

    # Phase 2 dashboard server.
    server_host: str = "127.0.0.1"
    server_port: int = 8765
    stream_poll_seconds: float = 0.5

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "LoopConfig":
        """Load from a JSON file if it exists, else defaults.

        Read as utf-8-sig: Windows editors (Notepad, PowerShell's Out-File)
        write a BOM, and plain utf-8 would reject the file with a stack trace
        that says nothing about the real problem.
        """
        if path and Path(path).exists():
            raw = Path(path).read_text(encoding="utf-8-sig")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} is not valid JSON: {exc}") from exc
            known = {f for f in cls.__dataclass_fields__}
            unknown = sorted(set(data) - known)
            if unknown:
                # Surface typos rather than silently ignoring a setting the
                # user believes is in effect.
                print(
                    f"warning: ignoring unknown config keys in {path}: "
                    f"{', '.join(unknown)}"
                )
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


# Prompt-cache multipliers on the input rate (Anthropic pricing model):
# writing a cache entry costs more than fresh input, reading one costs far less.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def estimate_cost_usd(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Cost of one invocation in USD.

    Cache arguments default to 0 so existing callers and the zero-priced
    MockRunner stay at $0. Cache tokens are priced off the input rate: writes
    at 1.25x, reads at 0.10x; output is unaffected.
    """
    pin, pout = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (
        tokens_in * pin
        + tokens_out * pout
        + cache_creation_tokens * pin * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * pin * CACHE_READ_MULTIPLIER
    ) / 1_000_000
