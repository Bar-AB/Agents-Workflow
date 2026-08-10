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
import re
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

    # Memory retrieval (roadmap slice 2). Which RetrievalBackend ranks approved
    # facts against the task at hand before they are injected:
    #   'hash' - the stdlib backend, no dependency at all (the default)
    #   'none' - no ranking: the pre-slice-2 alphabetical selection
    # An unknown name raises rather than degrading to a working default: which
    # ranking ran is part of how a run behaved, so substituting one silently is
    # exactly the kind of drift the audit log exists to prevent. Ranking never
    # widens what may be injected — the approval gate and the injection caps are
    # applied around it, not by it.
    memory_retrieval_backend: str = "hash"

    # Context-budget handoff (roadmap slice 1). When a worker's accumulated
    # context on a task (summed across its attempts) reaches this fraction of
    # its AgentSpec.context_budget_tokens, the loop summarizes the working state
    # and restarts the worker with that summary in place of the raw transcript,
    # rather than letting context silently overflow. Fraction of the budget, not
    # a token count; ~0.70 leaves headroom for the next turn's own output.
    context_handoff_ratio: float = 0.70

    # Planner + task graph (roadmap slice 3).
    # A planner generating tasks *is* task definition, which humans stay in the
    # loop for, so a plan's children are not claimable until the plan is signed
    # off. Set False for autonomous batch runs, where the validator on each
    # child's output remains the gate.
    plan_requires_approval: bool = True
    # Hard cap on plan size. A planner that emits hundreds of tasks has
    # misunderstood the goal; better to escalate the plan than to queue a batch
    # nobody reviewed. The whole plan is rejected, never truncated — a silently
    # trimmed plan is missing exactly the parts nobody can see are missing.
    max_plan_tasks: int = 20

    # How many tasks may run at once. 1 (the default) is the sequential loop
    # unchanged: one claim id, one thread, identical ordering. Above 1, that
    # many threads each claim independently, so only tasks with no unfinished
    # dependency run together. The ceiling is deliberate — it bounds simultaneous
    # model spend and concurrent sandboxed test subprocesses, which "run
    # everything ready" would not.
    max_parallel_workers: int = 1

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
    def load(cls, path: str | Path | None = None) -> LoopConfig:
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
    # OpenAI-compatible models the second-provider runner emits (slice 4).
    # Point-in-time list rates; providers change them, and a stale row here is
    # a wrong cost, not a crash — check them when the numbers start to matter.
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (2.00, 8.00),
    "mock": (0.0, 0.0),
}
DEFAULT_PRICING: tuple[float, float] = (3.00, 15.00)

# The models `OpenAICompatRunner` is expected to be pointed at. Kept as an
# explicit list so a test can assert every one of them has a pricing row: a
# model missing from MODEL_PRICING does not fail, it silently prices at
# DEFAULT_PRICING, which is the quiet wrong-cost trap the skill warns about.
# Pointing the runner at some other OpenAI-compatible endpoint still works —
# it just prices at the fallback until its model is added here.
OPENAI_MODELS: tuple[str, ...] = (
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
)


# Prompt-cache multipliers on the input rate (Anthropic pricing model):
# writing a cache entry costs more than fresh input, reading one costs far less.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# ...which is why these two cannot stay global now that a second provider
# exists. They encode *Anthropic's* cache pricing, and OpenAI's differs on both
# axes: writing a cache entry is free (caching is automatic, not a paid
# operation), and a cached read is discounted by a factor that varies per model
# family — 0.10x on the gpt-5 line, 0.25x on gpt-4.1/o3, 0.50x on gpt-4o. Left
# global, a cross-provider run would under-bill a gpt-4o cached read five-fold,
# and the budget cap is only as honest as the worst-priced attempt under it.
#
# Overrides rather than a third element on the MODEL_PRICING tuple: that tuple
# is `(input, output)` in the docs, in every test that reads it and in the
# dashboard, and widening it would rewrite all of those to express something
# only two providers care about.
CACHE_MULTIPLIERS: dict[str, tuple[float, float]] = {
    # model: (cache write multiplier, cache read multiplier) on the input rate
    **{m: (0.0, 0.10) for m in ("gpt-5", "gpt-5-mini", "gpt-5-nano")},
    **{m: (0.0, 0.25) for m in ("gpt-4.1", "gpt-4.1-mini", "o3")},
    **{m: (0.0, 0.50) for m in ("gpt-4o", "gpt-4o-mini")},
}


# A dated snapshot suffix, e.g. `gpt-4o-mini-2024-07-18` or `gpt-5-2025-08-07`.
_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def pricing_key(model: str) -> str:
    """The pricing-table key for a *serving* model id.

    OpenAI echoes the resolved snapshot rather than the alias that was
    requested, so the exact-match lookup missed on every real call: a
    `gpt-4o-mini-2024-07-18` attempt priced at DEFAULT_PRICING — twenty times
    the true input rate — and picked up Anthropic's cache multipliers, the exact
    five-fold cached-read under-bill CACHE_MULTIPLIERS was written to prevent.
    Both tables were unreachable in production while the dashboard reported the
    fabricated numbers as measured ones.

    Normalization happens *here*, at the pricing boundary, and never by
    rewriting `RunResult.model`: which snapshot served a call is provenance the
    audit trail keeps.

    Exact match first, then the dated suffix stripped, then the longest table
    key the id extends at a `-` boundary — so `gpt-5-mini-2025-08-07` prices as
    `gpt-5-mini` and not as `gpt-5`. A family price for an unlisted variant is a
    close estimate; DEFAULT_PRICING for it is not an estimate at all.
    """
    if model in MODEL_PRICING:
        return model
    stripped = _SNAPSHOT_SUFFIX.sub("", model)
    if stripped in MODEL_PRICING:
        return stripped
    best = ""
    for key in MODEL_PRICING:
        if stripped.startswith(f"{key}-") and len(key) > len(best):
            best = key
    return best or model


def cache_multipliers(model: str) -> tuple[float, float]:
    """(cache write, cache read) multipliers on a model's input rate.

    Defaults to the Anthropic pair, so every pre-slice-4 model prices exactly as
    it did before and an unknown model is treated as the provider the loop was
    built against rather than as free.
    """
    return CACHE_MULTIPLIERS.get(
        pricing_key(model), (CACHE_WRITE_MULTIPLIER, CACHE_READ_MULTIPLIER)
    )


def estimate_cost_usd(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Cost of one invocation in USD.

    Cache arguments default to 0 so existing callers and the zero-priced
    MockRunner stay at $0. Cache tokens are priced off the input rate, at
    whatever multipliers that model's provider charges (`cache_multipliers`);
    output is unaffected.
    """
    pin, pout = MODEL_PRICING.get(pricing_key(model), DEFAULT_PRICING)
    write_mult, read_mult = cache_multipliers(model)
    return (
        tokens_in * pin
        + tokens_out * pout
        + cache_creation_tokens * pin * write_mult
        + cache_read_tokens * pin * read_mult
    ) / 1_000_000
