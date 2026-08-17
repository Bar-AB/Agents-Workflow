"""Tool policy — the capability seam (roadmap slice 5).

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
from .runner import LOGICAL_TOOL_MAP

if TYPE_CHECKING:  # imports for annotations only; no cycle at run time
    from .config import LoopConfig
    from .models import AgentSpec
    from .store import Store


# How much of an agent's stated reason is kept. A reason is a sentence about why
# a capability is needed, read by the human clearing the queue; it is not a place
# for the agent to store prose, and the field reaches `log_event`'s one
# `json.dumps` inside a paid transaction.
MAX_TOOL_REASON_CHARS = 200

# `TOOL_REQUEST: <tool> (blocking|optional) - <reason>`
#
# Line-anchored, but the anchor admits one leading markdown list, quote or
# emphasis marker, because **LLM output is markdown**. A bare `^[ \t]*` dropped
# `- TOOL_REQUEST: shell (blocking) - …` entirely: an agent enumerating two
# capability needs writes them as a bullet list at least as readily as it writes
# the four reason separators this grammar already tolerates for that same reason.
# The failure was silent rather than safe — no row, no event, a capability
# withheld with nothing in the ledger — and under the park rule it is worse than
# that: a dropped `(blocking)` ask means the park never fires, so "I cannot
# finish without this" becomes "continue without it and tell no human".
#
# The prefix is a closed set of punctuation, never `.*`: `please TOOL_REQUEST:
# shell` and `the tool: TOOL_REQUEST: shell` must stay non-matches, or a marker
# quoted mid-sentence becomes a live request and the grammar stops being
# something an agent can quote at all.
#
# Case-insensitive on both the label and the flag. The tool is
# a bare logical name, and the lookahead after it is what makes a malformed name
# *no match at all* rather than a silently truncated one: without it
# `TOOL_REQUEST: sh!ell` would record a request for `sh`, and a 41-character name
# would record its first 40. (It does *not* save `TOOL_REQUEST: sh ell!!`, which
# is a legal short name followed by prose and is deliberately a request for `sh`
# — the ledger refuses and audits that rather than the parser guessing.)
#
# `\r?$` rather than `$`: under `re.MULTILINE` `$` matches before `\n` but not
# before `\r`, so on CRLF input — mainstream, this project is developed on
# Windows — the flagless, reasonless form (the shortest the grammar permits, and
# the one an agent asking for a read-only tool actually types) matched nothing at
# all. That failure is silent rather than safe: no row, no event, a capability
# withheld with nothing in the ledger, which is precisely what the module
# docstring says must not happen.
#
# The flag is optional in the grammar and **absent means optional**: a malformed
# marker must never gain the power to stall a task, the same direction
# `_extract_findings` degrades in. The separator before the reason may be `-`, an
# em-dash, `:` or nothing at all, because an agent writing prose will use all
# four and a request lost to punctuation is a capability silently withheld.
#
# Known limitation, deliberately not fixed: a marker inside a fenced code block
# is a live request. Understanding markdown fences is not this parser's job, and
# a fence-aware grammar would be a second, lossy model of the reply. It matters
# to slice 5's park rule (a quoted `(blocking)` example would stall a task), so
# it is stated here rather than discovered there.
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

    `config` is required and is never `None`. A policy function whose config may
    be absent would have to invent a risk judgment — what this project considers
    read-only — out of a missing argument, which is exactly the silent degrade to
    a working default that `retrieval.get_backend` refuses by raising. Callers
    with no config (`run_summarizer`, the eval harness) do not classify at all.

    It does not defend against a config of the wrong *type* either, and that is
    also deliberate: `tool in None` used to raise `TypeError` here — inside
    `_invoke`'s closing transaction, over a paid `finish_attempt`, three times.
    Swallowing it would mean inventing the same missing risk judgment; the fix is
    upstream, where `LoopConfig` normalizes and refuses field types at
    construction, so `tool_readonly_allowlist` is a `list[str]` by the time any
    policy reads it.

    The allowlist is consulted first, so widening it is how a project moves the
    line, and it wins over the `LOGICAL_TOOL_MAP` check rather than the reverse.

    Note this does **not** consult `baseline_tools`: the baseline is about a
    role's *declared* list, and a marker naming a baseline tool queues a row
    while the tool reaches the runner anyway — audited, harmless, not a grant.
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
        # Line boundaries are normalized before matching, and that closes a class
        # rather than a case. `\r?$` in the pattern fixed CRLF, but a lookahead
        # enumerating terminators leaks once per terminator nobody thought of:
        # a lone `\r` (classic Mac) and ` `/`` are also line breaks to
        # `str.splitlines` and *not* to `$`, so each silently dropped a
        # well-formed marker — the same no-row, no-event, capability-withheld
        # failure CRLF had, discovered one terminator at a time. `splitlines`
        # already knows every Unicode line boundary, so joining its result on
        # `\n` makes the question "which terminators did we remember?" stop
        # existing. The pattern keeps `\r?$` regardless: it must stay correct for
        # a caller who hands it raw text directly.
        text = "\n".join(str(text).splitlines())
        found: dict[str, ParsedToolRequest] = {}
        for match in _MARKER_RE.finditer(text):
            # Lower-cased because the pattern is `IGNORECASE` over its whole
            # length: `File_Read` therefore *matches*, while `classify` and
            # `LOGICAL_TOOL_MAP` are case-sensitive and would call it UNKNOWN. A
            # grammar that advertises a tolerance the classifier lacks turns a
            # well-formed ask into an audited refusal, so the capture is
            # normalized to the case the tables are written in. Case-folding also
            # makes `SHELL` and `shell` in one reply the one request they are.
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
        # Safe precisely because this function touches no store and no
        # transaction: there is nothing half-written to leave behind, and an
        # unparseable reply means "no request", which withholds the tool.
        return []


def baseline_tools(role: str) -> list[str]:
    """The tools this role ships with, read from the registry at call time.

    Derived rather than transcribed into config: a hand-written baseline would be
    a second source of truth against `DEFAULT_AGENTS`, and drift there fails
    silently *in the withholding direction* — a shipped agent quietly losing a
    tool it was built around.

    A role with no shipped entry gets `[]`, so everything it declares is gated.
    That is the fail-safe direction: a bespoke role's capabilities are approved
    once rather than assumed.
    """
    spec = DEFAULT_AGENTS.get(role)
    # A copy: the registry's own list must not be mutable through a policy read.
    return list(spec.tools) if spec else []


def tools_for(
    store: Store,
    config: LoopConfig,
    spec: AgentSpec,
    task_id: int | None,
    agent_kind: str,
) -> list[str]:
    """The tools one invocation may actually use — the single enforcement point.

    Resolution order per declared tool, first match wins:

    1. in `config.tool_readonly_allowlist` -> allowed, no row, no event;
    2. in `baseline_tools(spec.role)`      -> allowed, no row, no event;
    3. an `auto`/`approved` row exists     -> allowed;
    4. not in `LOGICAL_TOOL_MAP`           -> refused (it names no real tool);
    5. otherwise                           -> withheld, queued `pending`.

    Steps 2-5 are reached only when `gate_declared_tools` is on. With it off — the
    default — every declared tool passes through untouched and this returns
    `spec.tools`' content *and order*, writing nothing: the inertness guarantee is
    structural rather than a promise about how the branches happen to fall. Order
    matters because the `{kind}_prompt` event records `tools`.

    Granted tools the role did not declare are then **appended**, in ascending
    request id (creation) order, which is how an auto-approved read-only request
    reaches the *next* invocation. Never sorted: request order is stable under a
    rename, and it is what the audit trail shows.

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
    granted = store.granted_tools(task_id, role) if task_id is not None else []

    allowed: list[str] = []
    for tool in spec.tools:
        if not config.gate_declared_tools:
            allowed.append(tool)
            continue
        if tool in config.tool_readonly_allowlist or tool in baseline_tools(role):
            allowed.append(tool)
            continue
        if tool in granted:
            allowed.append(tool)
            continue
        if task_id is not None:
            # A declared name is only as trustworthy as `agents.json`, which loads
            # unvalidated through `AgentSpec(**spec)` — `tools: [null]` handed a
            # `None` to a `NOT NULL` column. The ledger takes the name as a
            # bounded string so a human reads back what was actually declared
            # instead of a NULL, and an unusable name is refused rather than
            # dropped: withholding it silently is the one outcome this module's
            # docstring rules out. (The store coerces too — that is where the
            # never-raises promise is made — but a caller that hands a paid
            # transaction a value only the callee saves is a caller relying on
            # somebody else's guarantee.)
            name = tool if isinstance(tool, str) else str(tool)
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

    for tool in granted:
        # A grant for a name that maps to no SDK tool is not a capability, so it
        # is never returned however the row was decided.
        if tool not in allowed and tool in LOGICAL_TOOL_MAP:
            allowed.append(tool)
    return allowed
