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
    # bool before int: `bool` is a subclass of `int`, so an unguarded int check
    # would accept `True` for a count and a float check would price at 1.0.
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
    # Raised from 500_000 in slice 8. Measured on a real `--runner claude` run
    # against the README's own quick-start task: the validator returned
    # `approve` at 0.85 and the task escalated anyway with "Budget cap exceeded
    # (tokens=556515, cost=$0.49)" — the *token* cap tripping at under a tenth
    # of the cost cap, on the first task a new user runs.
    #
    # The two caps measure different things and only one of them was calibrated.
    # Per the decision rules, the token total includes prompt-cache reads, so a
    # cached second round re-counts the whole prompt; 500k was roughly one and a
    # half rounds of a real conversation. The cache-read *rule* is deliberately
    # unchanged — the cap stays a true ceiling on everything the loop consumes,
    # priced at 0.10x on the cost side where it belongs — because changing what
    # the number counts would rewrite a rule documented in CLAUDE.md, the README
    # table and the tests, to fix what was only ever a badly chosen default.
    max_tokens_per_task: int = 3_000_000
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

    # Per-task workspace version control (roadmap slice 6). `git` is an external
    # executable in the same category as `test_command`: its absence degrades
    # the feature and never breaks a run. `vcs_command` is an executable *path*,
    # not a command line — deliberately unlike `test_command`, which is a
    # command line and goes through `executor.split_command`.
    vcs_enabled: bool = True
    vcs_command: str = "git"
    vcs_timeout_s: int = 30

    # Existing-repository workspaces (roadmap slice 9). 'scratch' (the default)
    # is a **proven** behavioral no-op, in the same register as slice 6's
    # `vcs_enabled=False`: `worktree_root`/`repo_root`/`vcs_base_ref`/
    # `vcs_branch_prefix` go unread and `workspace_root` behaves exactly as it
    # did before this slice. 'worktree' gives each task a real `git worktree`
    # checkout of `repo_root` on its own branch instead of a blank directory —
    # see `vcs.init_repo`'s worktree branch and `executor.workspace_for`.
    workspace_mode: str = "scratch"
    # The repository a worktree-mode task checks out. Unread in scratch mode.
    repo_root: str = "."
    # Where worktree-mode workspaces are created — deliberately **outside**
    # `repo_root` (enforced below, not merely defaulted). A workspace inside
    # the repository turns `executor.py`'s already-documented `..`-escape into
    # a write on the operator's real working tree, bypassing every recovery
    # path (review, `vcs.rollback`, the audit log) this slice exists to add —
    # see the plan's residual 2. Layout: `<worktree_root>/<repo-name>-<hash>/
    # task-<id>`, the hash over the *absolute* `repo_root` so two checkouts of
    # one repository, or two repositories sharing a basename, cannot collide
    # in one shared root.
    worktree_root: str = "~/.agentloop/ws"
    # What each task's worktree branches from. `HEAD`, not `main`: a ticket
    # usually branches from where the operator is standing.
    vcs_base_ref: str = "HEAD"
    # Branch naming is derived from this prefix plus the task id, never stored
    # — avoids a schema migration for a value `f"{prefix}{task_id}"` already
    # reconstructs.
    vcs_branch_prefix: str = "agentloop/task-"

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

    # Agent-requested tools (roadmap slice 5). Which *logical* tool names this
    # project judges safe to hand over without a human, so a request for one is
    # auto-approved (still audited — the trail must show the decision, not the
    # absence of a gate).
    #
    # Config rather than a constant in `runner.py`: what counts as read-only is
    # a project's own risk judgment, and a project whose sandbox is wired
    # differently will draw the line elsewhere.
    #
    # `web` is deliberately absent. WebFetch/WebSearch read remotely but egress
    # the prompt, and this project scrubs ANTHROPIC_API_KEY out of the sandbox
    # rather than trusting what runs there (`_check_base_url`'s reasoning) — the
    # wire gets the same standard. `task_state` maps to no SDK tool at all, and
    # is listed anyway so the allowlist stays a statement about logical names
    # rather than about what happens to resolve today.
    tool_readonly_allowlist: list[str] = field(
        default_factory=lambda: ["file_read", "search", "task_state"]
    )
    # Whether a role's *declared* `AgentSpec.tools` are gated too. Off by
    # default: the shipped worker declares `file_io` and `git`, neither of them
    # read-only, so gating declared tools on a fresh install would gut it. Turned
    # on, a declared tool outside the read-only allowlist and outside the role's
    # shipped baseline needs a grant.
    gate_declared_tools: bool = False
    # Bound on a task's *undecided* request queue: `pending` rows, and nothing
    # else. That is the queue a human has to clear, and it is the only thing here
    # worth bounding — `auto` and `refused` were answered by the machine and
    # `approved`/`rejected` by a human, and no count of answered questions makes
    # the next one more expensive. Over the cap is refused *and audited*, never
    # silently dropped — a request that vanished without a trace is worse than one
    # denied.
    #
    # Stated precisely because the obvious reading is wrong: this is **not** a
    # bound on rows. `UNIQUE(task_id, role, tool)` absorbs repeats of one name, so
    # an agent emitting 500 markers for `shell` creates one row and one event —
    # but 500 *distinct* names create 500 refused rows and 500 refusal events,
    # because each is a different ask that has to be answered somewhere. Decided
    # rows are excluded from the count on purpose: counting them made the bound
    # self-fulfilling, and a task holding this many answers could then record
    # nothing at all, not even an auto-approved read-only request.
    #
    # A true row bound would have to drop an ask silently or collapse distinct
    # names into one row, and both hide from the human the thing the ledger is
    # for. Bounding the *queue* is the promise this knob can actually keep.
    max_tool_requests_per_task: int = 10

    # Phase 2 dashboard server.
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
        # HIGH-4 (slice 9 remediation): expanded exactly once, here, so every
        # downstream consumer of `repo_root` sees the same value `_is_within`
        # validates below. Before this fix `_is_within` expanded `~` on its
        # own *copy* for the containment check only, while `loop.py`'s
        # `_worktree_repo_root` read the raw, unexpanded field with a bare
        # `os.path.abspath` -- so a config-valid `repo_root: "~/myproj"`
        # passed validation against the expanded path and then ran against
        # the literal, never-existing `~/myproj` relative to the process's
        # own cwd. `executor.worktree_root_for` already expands
        # `worktree_root` at its own use site; `repo_root` gets the same
        # treatment, but centralised rather than duplicated at each call
        # site, since a value expanded twice is idempotent but a value
        # expanded at only *some* of its call sites is exactly this bug.
        # Unconditional (not scoped to worktree mode) because it is a no-op
        # for the `"."` default and for any scratch-mode value that never
        # contains `~` -- scoping it would only reintroduce a second
        # normalisation path to keep in sync with the unconditional one
        # `_is_within` already applies.
        self.repo_root = os.path.expanduser(str(self.repo_root))
        # An unknown mode raises rather than degrading to 'scratch' (the
        # `memory_retrieval_backend` precedent): which mode ran is part of how
        # the run behaved, and silently substituting one is exactly the kind
        # of drift the audit log exists to prevent.
        if self.workspace_mode not in ("scratch", "worktree"):
            raise ValueError(
                f"workspace_mode must be 'scratch' or 'worktree', not "
                f"{self.workspace_mode!r}."
            )
        # Scoped to worktree mode: in scratch mode `worktree_root`/`repo_root`
        # are unread (the proven-no-op contract above), so refusing on their
        # values there would make an irrelevant knob able to break a scratch
        # config. Checked at load time, before it reaches a paid attempt — the
        # same reasoning as the bare-string allowlist check `_coerced`
        # documents.
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
                # Surface typos rather than silently ignoring a setting the
                # user believes is in effect.
                #
                # `warnings.warn`, not `print`. The stakes are a budget: an
                # operator who writes `"max_cost_per_task"` (dropping `usd`)
                # keeps the *default* cap and bills every run against a limit
                # they believe they lowered. A `print` is strictly weaker than
                # the `warnings.warn` this project already calls insufficient
                # for recorded degradation — it is invisible to `-W error`,
                # uncapturable by `pytest.warns`, and under `agentloop serve`
                # it scrolls past in a terminal nobody is watching. This is the
                # same argument `runner.py` makes for preferring `warn` over
                # `print`, applied to the one file that decides what a run is
                # allowed to spend.
                warnings.warn(
                    f"ignoring unknown config keys in {path}: "
                    f"{', '.join(unknown)} — check for a typo; the shipped "
                    f"default is in force for anything you meant to set.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            config = cls(**{k: v for k, v in data.items() if k in known})
            # Carried so `Loop.__init__` can put it in the audit log, where this
            # project's own convention says a degradation belongs — the warning
            # above is still invisible to `agentloop events`, the REST API and
            # the SSE feed, which is the exact gap that made the previous
            # `print` insufficient. Set as a plain attribute rather than a
            # dataclass field on purpose: it is a fact about *this load*, not a
            # setting, so it must not appear in `asdict`, in `/api/config`, or
            # in a round-tripped `loopconfig.json`.
            config.unknown_keys = list(unknown)
            config.unknown_keys_path = str(path)
            return config
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
