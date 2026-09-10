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
import os
import re
import warnings
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _is_within(child: str, parent: str) -> bool:
    """Whether `child` resolves inside `parent`, following symlinks/junctions.

    Realpath, not `os.path.abspath` — this is a pure containment *comparison*
    with no subprocess to spawn, so it is unlike `vcs._git`'s `-C` value (which
    stays lexical precisely so a junction cannot substitute the directory git
    is asked to run in). Here there is nothing to substitute; a junction is
    exactly the case a lexical compare would miss, and it is the shape of
    escape `vcs._same_path`/`vcs._is_within` were built to close (see their
    docstrings) — `worktree_root` outside `repo_root` is the same class of
    security boundary residual 2 depends on, so it gets the same comparison
    method. `normcase` after `realpath` matches Windows' case-insensitive,
    separator-normalised filesystem; it is identity on POSIX. Any error (a
    mixed-drive path, an unresolvable UNC share) refuses by returning True —
    fail closed, since this guards a config refusal, not a git spawn."""
    try:
        c = Path(os.path.normcase(os.path.realpath(os.path.expanduser(str(child)))))
        p = Path(os.path.normcase(os.path.realpath(os.path.expanduser(str(parent)))))
        return c.is_relative_to(p)
    except Exception:
        return True


def _coerced(name: str, declared: str, value: object) -> object:
    """One config field, normalized to its declared type or refused loudly.

    `LoopConfig.load` does `cls(**data)` on hand-edited JSON, and nothing used to
    check what came back. That is not a tidiness problem: `toolpolicy.classify`
    reads `tool in config.tool_readonly_allowlist` **inside `agents._invoke`'s
    closing transaction**, over an already-paid `finish_attempt`. A `null` there
    raised `TypeError: argument of type 'NoneType' is not a container`, the
    transaction rolled the paid attempt back, and `_with_retry` bought the same
    completion twice more — three billed completions, zero attempt rows, and a
    budget cap that cannot see spend that happened. So the refusal is here, at
    write time, in the register of `Store.charter_set`: loud where a human is
    editing, which is what pays for a total read path everywhere downstream.
    `cli.main` already renders `ValueError` as `error: …`.

    `None` on a list field is the one wrong type with an unambiguous reading —
    "off" — so it normalizes to `[]` instead of raising. A bare **string** is the
    case that must not: `tool in "file_read"` is a *substring* test, so the gate
    would silently auto-approve every tool whose name is a substring of it, which
    is failing open on a plausible typo.

    Fields whose annotation is none of these fall through unchecked rather than
    being rejected by a validator that has not been taught about them yet: this
    normalizes what it understands, and refusing an unknown shape would make
    adding a knob a two-place edit with a crash as the reminder.
    """
    if declared == "list[str]":
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            bad = [v for v in value if not isinstance(v, str)]
            if bad:
                raise ValueError(
                    f"{name} must contain only strings; got {bad[0]!r} "
                    f"({type(bad[0]).__name__})."
                )
            return list(value)
        raise ValueError(
            f"{name} must be a list of strings, not "
            f"{type(value).__name__} ({value!r}). A bare string here is a "
            f"substring test, not a list of names."
        )
    # bool checked first: bool is a subclass of int, so an int/float branch
    # without `not isinstance(value, bool)` would silently accept True as 1.
    if declared == "bool":
        if isinstance(value, bool):
            return value
    elif declared == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif declared == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif declared == "str":
        if isinstance(value, str):
            return value
    else:
        return value
    raise ValueError(
        f"{name} must be a {declared}, not {type(value).__name__} ({value!r})."
    )


@dataclass
class LoopConfig:
    approve_threshold: float = 0.70
    severe_threshold: float = 0.40
    max_revisions: int = 3
    max_tokens_per_task: int = 3_000_000
    max_cost_usd_per_task: float = 5.00
    human_review_risk_level: int = 2
    db_path: str = "agentloop.db"
    registry_path: str = "agents.json"

    allow_test_exec: bool = True
    test_command: str = "pytest -q"
    test_timeout_s: int = 120
    workspace_root: str = ".agentloop/ws"
    sandbox_env_allowlist: list[str] = field(default_factory=list)
    sandbox_isolation: str = "env"

    vcs_enabled: bool = True
    vcs_command: str = "git"
    vcs_timeout_s: int = 30

    workspace_mode: str = "scratch"
    repo_root: str = "."
    worktree_root: str = "~/.agentloop/ws"
    vcs_base_ref: str = "HEAD"
    vcs_branch_prefix: str = "agentloop/task-"

    memory_promote_threshold: int = 3

    memory_retrieval_backend: str = "hash"

    context_handoff_ratio: float = 0.70

    plan_requires_approval: bool = True
    max_plan_tasks: int = 20

    max_parallel_workers: int = 1

    infra_max_retries: int = 2
    infra_retry_backoff_s: float = 0.0

    tool_readonly_allowlist: list[str] = field(
        default_factory=lambda: ["file_read", "search", "task_state"]
    )
    gate_declared_tools: bool = False
    max_tool_requests_per_task: int = 10

    server_host: str = "127.0.0.1"
    server_port: int = 8765
    stream_poll_seconds: float = 0.5

    def __post_init__(self) -> None:
        """Normalize every field to its declared type, or refuse (see `_coerced`).

        On the dataclass rather than only in `load` because `load`'s own
        `cls(**data)` runs through here, so one implementation covers both the
        JSON path and a programmatic `LoopConfig(...)` — and the downstream
        promise ("`classify` cannot raise on config") is then a property of the
        type, not of one constructor. Driven by the declared annotation rather
        than a hand-listed set of fields, which would be a second source of truth
        that drifts silently the first time a knob is added.
        """
        for f in fields(self):
            setattr(self, f.name, _coerced(f.name, f.type, getattr(self, f.name)))
        self.repo_root = os.path.expanduser(str(self.repo_root))
        if self.workspace_mode not in ("scratch", "worktree"):
            raise ValueError(
                f"workspace_mode must be 'scratch' or 'worktree', not "
                f"{self.workspace_mode!r}."
            )
        if self.workspace_mode == "worktree" and _is_within(
            self.worktree_root, self.repo_root
        ):
            raise ValueError(
                f"worktree_root ({self.worktree_root!r}) resolves inside "
                f"repo_root ({self.repo_root!r}). Worktree-mode workspaces "
                f"must live outside the repository they check out — inside, "
                f"the executor's documented `..`-escape (see executor.py) "
                f"lands writes in the operator's real working tree instead "
                f"of an agentloop-owned directory, bypassing review, "
                f"vcs.rollback and the audit log. Point worktree_root "
                f"somewhere else, e.g. '~/.agentloop/ws'."
            )

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
                warnings.warn(
                    f"ignoring unknown config keys in {path}: "
                    f"{', '.join(unknown)} — check for a typo; the shipped "
                    f"default is in force for anything you meant to set.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            config = cls(**{k: v for k, v in data.items() if k in known})
            config.unknown_keys = list(unknown)
            config.unknown_keys_path = str(path)
            return config
        return cls()


MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (15.00, 75.00),
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


CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

CACHE_MULTIPLIERS: dict[str, tuple[float, float]] = {
    **{m: (0.0, 0.10) for m in ("gpt-5", "gpt-5-mini", "gpt-5-nano")},
    **{m: (0.0, 0.25) for m in ("gpt-4.1", "gpt-4.1-mini", "o3")},
    **{m: (0.0, 0.50) for m in ("gpt-4o", "gpt-4o-mini")},
}


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
