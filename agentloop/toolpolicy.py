"""Tool policy — the capability seam.

Agents may *request* tools at run time: read-only ones are auto-approved,
side-effecting ones queue for a human. This module holds every judgment behind
that, in the register of `retrieval.py`: a small interface, all the
classification behind it, unit-testable with no runner and — for three of the
four functions — no store.

**The one place a gate can bite is the `tools` list handed to `runner.run()`.**
agentloop never executes an agent's tools: the Claude SDK runs them *inside*
`runner.run()` and `OpenAICompatRunner` runs none, so a gate that fires after a
`tool_call` reaches the audit log is already too late. `tools_for` is therefore
the single enforcement point, called from `run_worker`/`run_validator`/
`run_planner` before the model call, and there is deliberately no post-hoc check
on observed `tool_calls` and no use of the SDK's live `can_use_tool` callback
(which would block a paid call waiting on a human).

Two asymmetries worth stating once, because they are what makes the feature
work rather than merely look safe:

- **A grant adds; the gate removes.** A grant is not "unlock something the role
  already declared" — the slice exists so an agent that discovers mid-task it
  needs a capability its role was never given can ask for one. So an `auto` or
  `approved` row puts a tool into the list even when `spec.tools` does not
  mention it, and the gate only ever takes a declared tool out.
- **The gate is enforced over the *concrete* capability, and the ledger's currency
  stays logical.** `LOGICAL_TOOL_MAP` is not injective: `git` and `shell` both
  resolve to `Bash`, and the shipped worker declares `git`. So withholding the
  logical name `shell` while passing `git` through left `Bash` in the SDK's
  `allowed_tools` — the ledger and the `{kind}_prompt` event recorded a withheld
  capability that had in fact held, produced by a *compliant* backend. `tools_for`
  therefore subtracts a withheld tool's concrete footprint, dropping every logical
  name that overlaps it at all — so the removal is coarser than the footprint, and
  denying one concrete tool takes every sibling of the logical name with it. Fail
  closed, deliberately: this is a
  permission gate on a system whose threat model is arbitrary AI-generated code,
  and a gate a human believes is closed but is not is worse than an inconvenient
  one. Rejected: keeping the old behavior and warning loudly — honest, but the
  gate would stay unable to stop `Bash` for any role declaring `git`. The
  surprising half is audited (`tool_capability_withheld`) rather than left to be
  discovered. The concrete names appear in **event payloads only**, exactly where
  `tool_requested`'s pre-existing `resolved` field already puts them, because
  "which capability" is the one question a logical name cannot answer; no column,
  no prompt and no policy decision is keyed on a vendor name. **Withheld here means
  a human's denial, plus an undecided ask for something the role does not already
  hold.** An undecided ask for a capability the role holds unconditionally is a
  no-op: nobody decided anything, and the marker path queues a row for any gated
  name, so subtracting on it let an agent revoke its own baseline by asking for it.
  See `tools_for` for where that line is drawn and why it is drawn there.
- **`parse_tool_requests` is total.** It runs on the output of an attempt that
  has already been paid for, inside the transaction that records that payment, so
  a raise here would discard the tokens and cost of a completion the provider has
  billed — and the retry would buy it again. It degrades to `[]`, in the register
  of `agents._extract_findings`.

It lives in its own module rather than in `config.py` (which stays declarative),
`agents.py` (whose job is prompts) or `store.py` (whose job is rows), and it
imports neither of the latter two — the dependency runs one way, so the seam
stays unit-testable and cannot cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .models import ToolRequestSource, ToolRequestStatus
from .registry import DEFAULT_AGENTS
from .runner import LOGICAL_TOOL_MAP, resolve_tools

if TYPE_CHECKING:  # imports for annotations only; no cycle at run time
    from .config import LoopConfig
    from .models import AgentSpec
    from .store import Store


MAX_TOOL_REASON_CHARS = 200

# `\r?$`, not bare `$`: under re.MULTILINE, `$` doesn't match before `\r`, so
# on CRLF (this project is developed on Windows) the shortest, flagless
# marker form silently matched nothing — no row, no event, capability
# withheld with zero trace. Don't simplify this back to `|$)`.
_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+>][ \t]*|\d+[.)][ \t]*|\*\*)?TOOL_REQUEST[ \t]*:[ \t]*"
    r"(?P<tool>[A-Za-z0-9_.\-]{1,40})(?=[ \t(]|\r?$)"
    r"(?:[ \t]*\((?P<flag>blocking|optional)\))?"
    r"[ \t]*(?:[-—:][ \t]*)?"
    r"(?P<reason>[^\r\n]*)",
    re.MULTILINE | re.IGNORECASE,
)


class ToolClass(str, Enum):
    """What policy says about one requested tool.

    `UNKNOWN` is not "unrecognised, so be careful" — it is terminal. A logical
    name outside `LOGICAL_TOOL_MAP` resolves to no SDK tool at all, so granting
    it would record a permission that means nothing and mislead the next reader
    of the ledger.
    """

    AUTO = "auto"
    GATED = "gated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedToolRequest:
    """One marker, parsed. A policy result, never persisted as such: the store's
    `ToolRequest` is the row, and this is what the parser hands the caller."""

    tool: str
    blocking: bool
    reason: str


def classify(tool: str, config: LoopConfig) -> ToolClass:
    """Which tier a *marker-named* tool falls in.

    `config` is required, never `None` — a caller with no config
    (`run_summarizer`, the eval harness) doesn't classify at all rather than
    guessing a read-only judgment. The allowlist wins over `LOGICAL_TOOL_MAP`
    when both apply. Doesn't consult `baseline_tools`: this answers "may a
    marker be auto-granted", not "does the role already have it" — a marker
    naming a baseline tool still queues an audited row, not a silent grant.
    That distinction (a `pending` row must not cost an already-held
    capability; a `rejected` row must) is applied in `tools_for`.
    """
    if tool in config.tool_readonly_allowlist:
        return ToolClass.AUTO
    if tool not in LOGICAL_TOOL_MAP:
        return ToolClass.UNKNOWN
    return ToolClass.GATED


def parse_tool_requests(text: str) -> list[ParsedToolRequest]:
    """Every tool request an agent wrote in its own reply.

    **Total: it returns a list for any input and never raises.** See the module
    docstring — it is called on an output whose attempt is already paid for.

    Duplicates within one reply collapse to one request and **the strongest
    significance wins**: asked as `optional` and again as `blocking`, the answer
    is blocking, because the second statement is the agent saying it cannot in
    fact proceed. The first non-empty reason is kept — that is the agent's own
    lead statement of why, and a later line does not overwrite it.

    The marker is never removed from the text here or anywhere: `task.output` and
    `Verdict.reasoning` keep it verbatim (the `FINDINGS:` precedent — added to,
    never subtracted from), because the output is what the validator reviews and
    what dependent tasks consume, and a stripped copy would be a second, lossy
    version of what the agent said.
    """
    try:
        text = "\n".join(str(text).splitlines())
        found: dict[str, ParsedToolRequest] = {}
        for match in _MARKER_RE.finditer(text):
            tool = match.group("tool").lower()
            flag = (match.group("flag") or "").lower()
            blocking = flag == "blocking"
            reason = (match.group("reason") or "").strip()[:MAX_TOOL_REASON_CHARS]
            previous = found.get(tool)
            if previous is None:
                found[tool] = ParsedToolRequest(tool, blocking, reason)
                continue
            found[tool] = ParsedToolRequest(
                tool,
                previous.blocking or blocking,
                previous.reason or reason,
            )
        return list(found.values())
    except Exception:
        return []


def baseline_tools(role: str) -> list[str]:
    """The tools this role ships with, read from the registry at call time —
    derived rather than duplicated into config, so it can't drift from
    `DEFAULT_AGENTS`. A role with no shipped entry gets `[]` (fail-safe: a
    bespoke role's capabilities are approved once, not assumed).
    """
    spec = DEFAULT_AGENTS.get(role)
    return list(spec.tools) if spec else []


def subtract_withheld(
    allowed: list[str], withheld: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """`allowed` minus every concrete capability `withheld` names.

    Returns `(kept, lost, capability)`: the surviving logical names, the ones
    subtracted that were **not themselves** withheld (collateral loss a human
    must be told about), and the concrete tools that did the subtracting.

    Pure, and applied **once over the whole resolution**, never per request —
    per request the order requests are processed in would change the answer
    (a later grant could re-introduce a capability an earlier denial removed).
    A name resolving to nothing (`task_state`, or outside the map) confers no
    capability, so it can neither withhold nor be withheld here.
    """
    denied = set(resolve_tools(withheld))
    if not denied:
        return list(allowed), [], []
    kept: list[str] = []
    lost: list[str] = []
    capability: list[str] = []
    for tool in allowed:
        overlap = [c for c in LOGICAL_TOOL_MAP.get(tool, []) if c in denied]
        if not overlap:
            kept.append(tool)
            continue
        if tool not in withheld:
            lost.append(tool)
        for concrete in overlap:
            if concrete not in capability:
                capability.append(concrete)
    return kept, lost, capability


@dataclass(frozen=True)
class ToolEffect:
    """What one role would actually get, given one set of rows.

    The **single** implementation of that question: `tools_for` computes the
    enforced list from it and then writes; the CLI and dashboard compute the
    *consequence of a decision* from it and write nothing, so the two can't
    drift the way a screen re-deriving the answer from `LOGICAL_TOOL_MAP`
    alone (blind to declared lists and sibling rows) used to.

    `queued` is the declared names that need a row — advice to a caller that
    may write, never a write of its own.
    """

    allowed: list[str]
    queued: list[str]
    withheld: list[str]
    removed: list[str]
    lost: list[str]
    capability: list[str]


@dataclass(frozen=True)
class DecisionEffect:
    """What approving or rejecting one request would actually do.

    Every list field is a *difference between two `effective_tools` results*,
    so none can claim a consequence the gate wouldn't produce; `verb` is a
    branch over those differences, computed here rather than on a screen so
    a permission label is never an assertion no gate backs. Fields can come
    out empty in ways worth knowing: `approve_grants` is empty when another
    withheld row already confers the same capability; `reject_removes` is
    empty with `costs_now` non-empty when the sibling is already withheld
    (approving restores it, rejecting takes nothing further); both empty
    means this role never held the sibling, so denial costs it nothing.
    """

    in_effect: bool
    capability_live: list[str]
    capability_missing: list[str]
    verb: str
    costs_now: list[str]  # other logical names not working because of this row
    approve_grants: list[str]  # concrete capability gained by approving...
    approve_enables: list[str]  # ...and the logical names that come with it
    reject_removes: list[str]  # logical names that would stop working on reject...
    reject_loses: list[str]  # ...and the concrete capability that goes with them


def declared_tools(registry, role: str) -> list[str]:
    """The tools `role` declares, or its shipped baseline when the registry has
    no entry for it. Shared by both read-only surfaces so "which list is the
    role's" is one answer rather than two."""
    spec = registry.agents.get(role)
    return list(spec.tools) if spec else baseline_tools(role)


def effective_tools(
    config: LoopConfig,
    role: str,
    declared: list[str],
    granted: list[str],
    withheld: list[str],
    undecided: list[str],
) -> ToolEffect:
    """The tools `role` may actually use, given these rows. **Pure: writes
    nothing, reads no store** — so a read-only surface can call it to preview
    a decision without turning a GET into a mutation. `tools_for` wraps this
    with the writes and nothing else.
    """
    allowed: list[str] = []
    held: set[str] = set()
    queued: list[str] = []
    for tool in declared:
        name = tool if isinstance(tool, str) else str(tool)
        if not config.gate_declared_tools:
            allowed.append(name)
            held.add(name)
            continue
        if name in config.tool_readonly_allowlist or name in baseline_tools(role):
            allowed.append(name)
            held.add(name)
            continue
        if name in granted:
            allowed.append(name)
            held.add(name)
            continue
        queued.append(name)

    for tool in granted:
        if tool not in allowed and tool in LOGICAL_TOOL_MAP:
            allowed.append(tool)
        if tool in LOGICAL_TOOL_MAP:
            held.add(tool)

    pending = set(undecided)
    held_capabilities = set(resolve_tools(sorted(held)))

    def _pending_costs_nothing(tool: str) -> bool:
        """Whether subtracting this undecided row would take a capability the
        role already holds. **Intersection, not subset** — `LOGICAL_TOOL_MAP`
        has partial overlaps as well as exact collisions (`file_io` resolves
        to `[Read, Write, Edit]`, `file_read` to `[Read]`), so a subset test
        would miss a partially-overlapping pending ask and let it strip a
        capability the role does hold. Exempting can't fail open: a pending
        tool is never in `allowed` regardless, so skipping the subtraction
        only ever *keeps* something the role independently declared.
        """
        if tool in held:
            return True
        return bool(set(resolve_tools([tool])) & held_capabilities)

    effective_withheld = [
        t for t in withheld if not (t in pending and _pending_costs_nothing(t))
    ]
    before = list(allowed)
    allowed, lost, capability = subtract_withheld(allowed, effective_withheld)
    return ToolEffect(
        allowed=allowed,
        queued=queued,
        withheld=effective_withheld,
        removed=[t for t in before if t not in allowed],
        lost=lost,
        capability=capability,
    )


def _row_facts(store: Store, task_id: int, role: str) -> tuple[list, list, list]:
    """`(granted, withheld, undecided)` for one role on one task — the store
    reads `effective_tools` is evaluated against. A read, never a write."""
    return (
        store.granted_tools(task_id, role),
        store.withheld_tools(task_id, role),
        store.withheld_tools(
            task_id, role, statuses=(ToolRequestStatus.PENDING.value,)
        ),
    )


def _verb(
    status: ToolRequestStatus,
    mine: list[str],
    capability_live: list[str],
    approve_grants: list[str],
) -> str:
    """The headline claim for one row — `DecisionEffect.verb`.

    Every branch is keyed on a *computed* field, never on `status` alone, which is
    what the panel's `verbFor(status)` did: it rendered "would grant [Bash]" over a
    body reading "Approving does not deliver Bash", "grants [Bash]" over "NOT in
    force", and "denied [Bash]" over a live capability. A status says what a human
    did; it cannot say what the gate will do.

    The result reads into the row's resolved chip — "would grant" + `Bash` — so a
    row conferring nothing concrete answers "confers" and lets the chip say what
    kind of nothing it is (`task_state` is served in-process; a name outside the
    map is not a tool).
    """
    if not mine:
        return "confers"
    if status is ToolRequestStatus.PENDING:
        if approve_grants:
            return "would grant"
        return "already has" if capability_live else "would not deliver"
    if status in (ToolRequestStatus.APPROVED, ToolRequestStatus.AUTO):
        return "grants" if capability_live else "does not deliver"
    return "does not withhold" if capability_live else "denied"


def decision_effect(
    store: Store,
    config: LoopConfig,
    role: str,
    declared: list[str],
    request,
) -> DecisionEffect:
    """What approving or rejecting `request` would really do. **Read-only.**

    It evaluates `effective_tools` four times over the same declared list: as the
    rows stand, with this row approved, with it rejected, and with it absent
    altogether. Every returned **list** is a difference between two of those, so
    the surfaces render a computed consequence rather than a restatement of
    `LOGICAL_TOOL_MAP` — which knows neither the role's declared list nor the
    sibling rows, and so could only assert what it could not know. `verb` is a
    branch over those lists rather than a difference itself, and it is here for the
    same reason they are: it is a claim, `web/` has no test runner, and a claim
    decided on the screen is the one this slice keeps getting wrong.

    It deliberately does **not** call `tools_for`: that writes rows and logs
    events, so a GET built on it would be a mutation.
    """
    tool = request.tool
    granted, withheld, undecided = _row_facts(store, request.task_id, role)
    without = [u for u in undecided if u != tool]

    def resolve_for(effect: ToolEffect) -> list[str]:
        return resolve_tools(effect.allowed)

    now = effective_tools(config, role, declared, granted, withheld, undecided)
    approved = effective_tools(
        config,
        role,
        declared,
        granted if tool in granted else [*granted, tool],
        [w for w in withheld if w != tool],
        without,
    )
    rejected = effective_tools(
        config,
        role,
        declared,
        [g for g in granted if g != tool],
        withheld if tool in withheld else [*withheld, tool],
        without,
    )
    absent = effective_tools(
        config,
        role,
        declared,
        [g for g in granted if g != tool],
        [w for w in withheld if w != tool],
        without,
    )

    now_concrete = set(resolve_for(now))
    rejected_concrete = set(resolve_for(rejected))
    mine = resolve_tools([tool])
    capability_live = [c for c in mine if c in now_concrete]
    approve_grants = [c for c in resolve_for(approved) if c not in now_concrete]
    decidable = request.status is ToolRequestStatus.PENDING
    return DecisionEffect(
        in_effect=tool in now.allowed,
        capability_live=capability_live,
        capability_missing=[c for c in mine if c not in now_concrete],
        verb=_verb(request.status, mine, capability_live, approve_grants),
        costs_now=[t for t in absent.allowed if t not in now.allowed and t != tool],
        approve_grants=approve_grants,
        approve_enables=[
            t for t in approved.allowed if t not in now.allowed and t != tool
        ],
        reject_removes=[
            t for t in now.allowed if t not in rejected.allowed and t != tool
        ]
        if decidable
        else [],
        reject_loses=[c for c in resolve_for(now) if c not in rejected_concrete]
        if decidable
        else [],
    )


def tools_for(
    store: Store,
    config: LoopConfig,
    spec: AgentSpec,
    task_id: int | None,
    agent_kind: str,
) -> list[str]:
    """The tools one invocation may actually use — the single enforcement point.

    **The computation itself lives in `effective_tools`, and this function is that
    computation plus its writes.** They are not two implementations of the same
    rule and must never become two: the CLI and the dashboard render the
    *consequence* of a decision from the same function, and a screen that
    disagrees with the gate is the defect this slice keeps producing. `tools_for`
    cannot be called from a read path — it writes rows and logs events, so a GET
    built on it would be a mutation — which is exactly why the computation was
    extracted rather than duplicated.

    Resolution order per declared tool, first match wins:

    1. in `config.tool_readonly_allowlist` -> allowed, no row, no event;
    2. in `baseline_tools(spec.role)`      -> allowed, no row, no event;
    3. an `auto`/`approved` row exists     -> allowed;
    4. not in `LOGICAL_TOOL_MAP`           -> refused (it names no real tool);
    5. otherwise                           -> withheld, queued `pending`.

    Steps 2-5 are reached only when `gate_declared_tools` is on. With it off — the
    default — every declared tool is allowed, so with **no `tool_requests` row for
    `(task_id, spec.role)`** this returns `spec.tools`' content *and order* and
    writes nothing at all. That is the whole of the inertness guarantee, and the
    no-row condition is load-bearing rather than incidental: the subtraction below
    is not gated
    on the knob, so with a row present the gate-off path can return **less** than
    `spec.tools`, and it can write — a `tool_capability_withheld` event. Order
    matters because the `{kind}_prompt` event records `tools`.

    Granted tools the role did not declare are then **appended**, in ascending
    request id (creation) order, which is how an auto-approved read-only request
    reaches the *next* invocation. Never sorted: request order is stable under a
    rename, and it is what the audit trail shows.

    Then, last and **once over the whole list**, every withheld tool's concrete
    capability is subtracted (`subtract_withheld`). That step is not gated on
    `gate_declared_tools` — the knob decides whether a declared tool needs a grant,
    not whether a decision already made is honored — and it is the only step that
    can remove a tool the four steps above allowed. It is what makes a denial real
    rather than cosmetic, at the cost of also disabling any other logical name
    sharing the capability; see the module docstring for why that trade is the one
    this project takes.

    **Withheld means `rejected`, plus `pending` on a tool the role does not already
    hold.** A `rejected` row is a human's denial and subtracts unconditionally,
    baseline or not — exempting the baseline there would make rejecting `file_io`
    achieve nothing, which is the cosmetic denial the subtraction exists to end. A
    `pending` row is nobody's decision: the marker path queues one for *any* gated
    logical name, a non-blocking one never reaches a human at all
    (`pending_blocking_tool_requests` cannot see it), and subtracting on it meant an
    agent revoked its own baseline `Read`/`Write`/`Edit` simply by writing
    `TOOL_REQUEST: file_io`. So on a tool held unconditionally — steps 1 and 2, and
    every declared tool while the gate is off — a `pending` row is a no-op: audited,
    and genuinely harmless. It is decided here, and not in `withheld_tools` or
    `subtract_withheld`, because "already holds" is a policy fact assembled from
    the allowlist, the registry baseline and the knob, and this is the one place
    that holds all three; the store stays a parameterised read and the subtraction
    stays pure over its arguments.

    `agent_kind` is the loop's own literal for which agent is running; the `role`
    is derived from `spec.role` and is deliberately **not** a parameter. Both are
    stored on every row, and they are different strings — a custom
    `task.worker_role` makes them differ — so a parameter that could only ever
    hold `spec.role` would be a parameter that could hold the wrong one, sitting
    next to a near-synonym waiting to be transposed.

    Every row this writes is created with `blocking=False`, unconditionally: the
    agent never asked for a declared tool, so it cannot have called it
    load-bearing. `gate_declared_tools=True` therefore withholds and continues —
    this function has no code path that can park a task, which is what keeps a
    knob from being an escalation engine.

    A `task_id` of `None` (a task not yet persisted) withholds gated tools
    without recording anything: there is no row to attach a request to, and
    failing open would hand over the capability the gate exists to hold back.
    """
    role = spec.role
    if task_id is None:
        return effective_tools(config, role, list(spec.tools), [], [], []).allowed

    effect = effective_tools(
        config, role, list(spec.tools), *_row_facts(store, task_id, role)
    )
    for name in effect.queued:
        unknown = name not in LOGICAL_TOOL_MAP
        store.tool_request_add(
            task_id,
            role=role,
            agent_kind=agent_kind,
            tool=name,
            status=(
                ToolRequestStatus.REFUSED.value
                if unknown
                else ToolRequestStatus.PENDING.value
            ),
            source=ToolRequestSource.DECLARED.value,
            reason="declared in the agent registry; needs a grant while "
            "gate_declared_tools is on",
            blocking=False,
            why="not a known logical tool name" if unknown else "",
            max_per_task=config.max_tool_requests_per_task,
        )

    if effect.queued:
        effect = effective_tools(
            config, role, list(spec.tools), *_row_facts(store, task_id, role)
        )

    allowed, withheld = effect.allowed, effect.withheld
    lost, capability, removed = effect.lost, effect.capability, effect.removed
    if removed:
        cause = [
            w
            for w in withheld
            if any(c in capability for c in LOGICAL_TOOL_MAP.get(w, []))
        ]
        store.log_event(
            task_id,
            "tool_capability_withheld",
            {
                "role": role,
                "agent_kind": agent_kind,
                "withheld": list(withheld),
                "removed": removed,
                "also_lost": lost,
                "capability": capability,
                "message": (
                    f"{_and_list(removed)} stopped working for role {role!r} on "
                    f"this task: it needs {_and_list(capability)}, which is the "
                    f"capability {_and_list(cause)} confers, and that is withheld "
                    f"(rejected, or awaiting approval)."
                    + (
                        f" {_and_list(lost)} shares that capability, so approving "
                        f"the withheld request restores both; there is no way to "
                        f"deny one and keep the other."
                        if lost
                        else " Approving the withheld request restores it."
                    )
                ),
            },
        )
    return allowed


def _and_list(names: list[str]) -> str:
    """`['a', 'b']` -> `a and b`. A sentence a human reads is the deliverable
    here, so the joining lives with the sentence rather than in the caller."""
    if not names:
        return "nothing"
    if len(names) == 1:
        return str(names[0])
    return f"{', '.join(str(n) for n in names[:-1])} and {names[-1]}"
