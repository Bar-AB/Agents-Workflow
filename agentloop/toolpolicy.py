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
#
# The other half of the same closed-set trade, stated because it is the failure
# that is silent rather than loud: the prefix group admits **exactly one** token,
# so `- > TOOL_REQUEST:`, `> - TOOL_REQUEST:`, `| TOOL_REQUEST:` (a table cell),
# `<!-- TOOL_REQUEST:`, `### TOOL_REQUEST:`, `- [ ] TOOL_REQUEST:` and a
# backticked `` `TOOL_REQUEST:` `` all drop. Accepted rather than widened:
# repeating the group makes `please TOOL_REQUEST:` reachable through no more than
# a couple of tokens, and the direction of *that* failure is a quotation becoming
# a live park. Each of these is one combination further out than the plain bullet
# the group exists for, and the withheld capability is recoverable by a human;
# a manufactured park on quoted prose is not.
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
    role's *declared* list, while this answers "may a marker's ask be granted
    without a human". A marker naming a baseline tool therefore still queues a
    row, and that row is an audited ask, not a grant.

    What it is emphatically **not** is a revocation. A `pending` row is nobody's
    decision — no human is ever prompted for a non-blocking one — so it must not
    cost the role a capability it already holds unconditionally, while a
    `rejected` row must, or a human's denial would be cosmetic. That distinction
    is applied in `tools_for`, which knows both halves of "already holds" (the
    read-only allowlist and `baseline_tools`); it is stated here because this
    function's silence about the baseline is only safe while that holds.
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


def subtract_withheld(
    allowed: list[str], withheld: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """`allowed` minus every concrete capability `withheld` names.

    Returns `(kept, lost, capability)`: the surviving logical names, the ones
    subtracted that were **not themselves** withheld (the collateral loss a human
    has to be told about), and the concrete tools that did the subtracting.

    Pure — it reads `LOGICAL_TOOL_MAP` and nothing else — and applied **once, over
    the whole resolution**, never per request. Per request it could not be
    correct: `tools_for` appends grants *after* the declared loop, so a granted
    `git` would re-introduce the `Bash` a rejected `shell` had just removed, and
    which of the two won would depend on which loop ran last. Applied to the final
    list the answer is the same in either order, and it is the closed one.

    A name resolving to nothing (`task_state`, or a name outside the map) can
    neither withhold nor be withheld here: it confers no capability, so there is
    nothing to subtract and nothing to lose.
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
        # A tool that is *itself* withheld is an ordinary denial, not collateral:
        # the ledger already says why it is gone. Only a name the role legitimately
        # held and lost anyway is news.
        if tool not in withheld:
            lost.append(tool)
        for concrete in overlap:
            if concrete not in capability:
                capability.append(concrete)
    return kept, lost, capability


@dataclass(frozen=True)
class ToolEffect:
    """What one role would actually get, given one set of rows.

    The **single** implementation of that question. `tools_for` computes the
    enforced list from it and then does its writes; the CLI and the dashboard
    compute the *consequence of a decision* from it and write nothing. Two
    implementations would drift, and drift between what the screen promises and
    what the gate enforces is this slice's recurring defect — the panel used to
    render "approving grants it too" from `LOGICAL_TOOL_MAP` alone, which knows
    neither the role's declared list nor the sibling rows' statuses, so it
    asserted a consequence it could not know.

    `queued` is the declared names that need a row; it is advice to a caller that
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

    Every list field is a *difference between two `effective_tools` results*, so
    none of them can claim a consequence the gate would not produce. `verb` is the
    one field that is not — it is a branch over those differences, which is exactly
    why it lives here and not on a screen. The three states
    that falsified the old sharing-map text are each one of these fields being
    empty where the map alone said otherwise:

    - `approve_grants == []` — approving grants nothing, because another withheld
      row on the task confers the same concrete capability.
    - `reject_removes == []` with `costs_now != []` — the sibling is *already*
      withheld while this row is pending, so approving restores it and rejecting
      takes nothing further away.
    - `reject_removes == []` with `costs_now == []` — this role never held the
      sibling, so denial costs it nothing. An invented cost attached to *denial*
      pushes a human toward granting, which is the direction that matters.
    """

    # Is this request's own tool available to the role right now? An `approved`
    # row whose capability another denial still subtracts is not in force, and a
    # green chip beside "grants Bash" would say it is.
    #
    # **This is a logical-name membership test, and the surfaces must not state
    # anything about the concrete capability from it.** The two diverge on the
    # whole `refused` population: `withheld_tools` deliberately never sees
    # `refused` (`store.withheld_tools`), so such a row subtracts nothing, while
    # its logical name is still absent from `allowed`. Both surfaces read
    # "{resolved} is not available to this role" off this flag and so told a human
    # a capability was withheld while the gate handed it over through another
    # declared name — a gate a human believes is closed but is not, which the
    # module docstring names as the worse direction. `capability_live` is the
    # concrete answer; this one stays for the questions that really are about the
    # logical name.
    in_effect: bool
    # This row's own concrete capability that the role actually has right now: its
    # `resolve_tools` footprint intersected with the footprint of the enforced
    # list. Computed like every other field here — out of an `effective_tools`
    # evaluation rather than out of `LOGICAL_TOOL_MAP`, which knows neither the
    # role's declared list nor the sibling rows. Empty is the only honest basis
    # for saying a capability is not available.
    capability_live: list[str]
    # The complement of `capability_live` over the same footprint: this row's own
    # concrete capability the role does **not** have. It is served rather than
    # derived on each surface because it is the *subject* of the sentence a human
    # reads as a closed gate ("Write and Edit are not available to this role"), and
    # the coarse subtraction makes a partial split reachable — a `refused`
    # `file_io` on a role holding `file_read` keeps `Read` and loses the other two.
    capability_missing: list[str]
    # The headline claim the surfaces put above the consequence, as one word or
    # two, reading directly into the row's resolved chip ("would grant" + Bash).
    #
    # It lives here rather than in each surface because it is a *claim*, and this
    # slice's recurring defect is a claim the code does not back. The panel's own
    # `verbFor(status)` keyed on the status alone and contradicted the body two
    # lines under it in three reachable states, and `web/` has no test runner — so
    # a verb computed in TSX is a permission-screen assertion no gate covers.
    # Computed here, `tests/test_tool_policy.py` pins every state.
    verb: str
    # Other logical names not working *because* this row stands as it does.
    costs_now: list[str]
    # Concrete capability the role would gain by approving...
    approve_grants: list[str]
    # ...and the other logical names that would start working again with it.
    approve_enables: list[str]
    # Other logical names that would stop working if this were rejected...
    reject_removes: list[str]
    # ...and the concrete capability that would go with them.
    reject_loses: list[str]


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
    """The tools `role` may actually use, given these rows. **Pure: it writes
    nothing — no rows, no events — and reads no store.**

    That purity is the point of the extraction, not a bonus: `tools_for` writes
    rows and logs events, so a read-only surface calling *it* to preview a
    decision would turn a GET into a mutation. The resolution order, the
    pending-vs-rejected distinction and the capability subtraction all live here;
    `tools_for` adds the writes around it and nothing else.

    See `tools_for` for what each step means and why — this is that docstring's
    body, moved.
    """
    allowed: list[str] = []
    # The names this role holds without anyone deciding anything — what a
    # `pending` row must not be allowed to revoke.
    held: set[str] = set()
    queued: list[str] = []
    for tool in declared:
        # A declared name is only as trustworthy as `agents.json`, which loads
        # unvalidated through `AgentSpec(**spec)` — `tools: [null]` handed a `None`
        # to a `NOT NULL` column, and `tools: [["file_io"]]` an unhashable value to
        # a `set` membership test. Coerced here, unconditionally, rather than in
        # the gated branch alone: with the gate off the raw entry used to travel
        # into the subtraction below and raise `TypeError` during prompt
        # construction, inside `_with_retry` — three retries reported as
        # `infra_error`, pointing the human at the network instead of at the
        # registry, which is the classification the config-error rule exists to
        # prevent. A `str` passes through as itself, so the inertness guarantee is
        # untouched for every list that is one. `tools_for` writes `queued` to the
        # ledger, so a human reads back what was actually declared instead of a
        # NULL, and an unusable name is refused rather than dropped: withholding it
        # silently is the one outcome this module's docstring rules out.
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
        # A grant for a name that maps to no SDK tool is not a capability, so it
        # is never returned however the row was decided.
        if tool not in allowed and tool in LOGICAL_TOOL_MAP:
            allowed.append(tool)
        # ...and a granted name is **held**, which is what the pending exemption
        # below is asking about. `held` was built only from the declared paths,
        # so with `gate_declared_tools=True` a human could approve `git` and the
        # agent's next `TOOL_REQUEST: shell` — the registry's own worked example,
        # undecided, never shown to anyone — would subtract the `Bash` that human
        # had just granted. A grant is the strongest form of "already holds"
        # there is; leaving it out made the exemption weakest exactly where a
        # human had been most explicit.
        if tool in LOGICAL_TOOL_MAP:
            held.add(tool)

    # An undecided ask for something the role already holds is not a denial of
    # it; a decided one is. One filter rather than a branch inside the
    # subtraction, so `subtract_withheld` stays pure over its arguments.
    #
    # The exemption is decided in the **same currency as the subtraction** —
    # concrete capabilities, not logical names. `LOGICAL_TOOL_MAP` is not
    # injective, so a name test and a capability subtraction disagree on exactly
    # the collision pairs this module was written around. Measured with the
    # shipped `worker` spec and the default config (`gate_declared_tools=False`,
    # so every declared name is held):
    #
    #   declares:                    ['file_io', 'git', 'search', 'task_state']
    #   pending, optional 'shell' -> ['file_io', 'search', 'task_state']  # !!
    #   pending, optional 'git'   -> ['file_io', 'git', 'search', 'task_state']
    #
    # `shell` and `git` both resolve to `Bash`, so an *undecided, non-blocking*
    # ask — nobody's decision, never shown to a human — silently cost the role a
    # capability it already had, for the rest of the task. The trigger is the
    # shipped system prompt's own worked example: `registry.py` teaches
    # `TOOL_REQUEST: shell (blocking) - the criteria require running the build`,
    # and the worker declares `git`, not `shell`.
    #
    # Only the *pending* exemption widens. A **rejected** row still subtracts
    # unconditionally, baseline or not — that is the fail-closed half, and it is
    # what stops an agent revoking its own baseline by asking for it.
    pending = set(undecided)
    held_capabilities = set(resolve_tools(sorted(held)))

    def _pending_costs_nothing(tool: str) -> bool:
        """Whether subtracting this undecided row would take away a capability
        the role already holds.

        **Intersection, not subset**, and the difference is a live defect one
        collision pair over from the one this exemption was written for.
        `LOGICAL_TOOL_MAP` has *partial* overlaps as well as exact collisions:
        `file_io` resolves to `[Read, Write, Edit]` and `file_read` to `[Read]`,
        and the shipped **planner** declares `file_read`. Under a subset test
        `{Read, Write, Edit} <= {Read, ...}` is False, so the row was not exempt
        and `subtract_withheld` removed every name overlapping `Read` — measured:

            planner declares:          ['file_read', 'search', 'task_state']
            pending optional file_io -> ['search']   lost ['file_read']

        An **optional, undecided** ask — never shown to a human, nobody's
        decision — silently cost the planner the `Read` it already had. That is
        the same self-revocation CLAUDE.md's rule forbids, and the same one the
        `git`/`shell` fix closed; only the exact-collision case had been
        measured.

        Exempting cannot fail open. A pending row's tool is in `queued`, never
        in `allowed`, so skipping the subtraction can only *keep* capabilities
        the role independently declared — it can never hand over a new one. And
        only the **pending** side is widened: a rejected row is not in
        `pending`, so a human's denial still subtracts unconditionally."""
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
        # Nothing to gain and the capability is already live: the role holds it
        # through a name it declared, so approving changes nothing rather than
        # failing to deliver.
        return "already has" if capability_live else "would not deliver"
    if status in (ToolRequestStatus.APPROVED, ToolRequestStatus.AUTO):
        return "grants" if capability_live else "does not deliver"
    # `rejected` or `refused`. A `refused` row subtracts nothing at all
    # (`withheld_tools` never sees it), so whether anything is actually denied
    # here is a question about the concrete capability and not about the status.
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
    # Only a `pending` row can be rejected (`store.tool_request_decide` refuses
    # every other status), so on any other status these two describe a decision
    # nobody can take. Computed for the one status that can, rather than computed
    # always and rendered nowhere: a field that is dead but populated is what a
    # later surface renders by accident.
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
        # No row to attach a request to, so a gated tool is withheld and nothing
        # is recorded: failing open would hand over the capability the gate exists
        # to hold back.
        return effective_tools(config, role, list(spec.tools), [], [], []).allowed

    effect = effective_tools(
        config, role, list(spec.tools), *_row_facts(store, task_id, role)
    )
    # `effect.queued` is already coerced to `str` by `effective_tools` (see the
    # comment there), so every value reaching the ledger below is a plain bounded
    # string — which is what keeps this call, inside a paid transaction, unable to
    # raise.
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
        # Recomputed against the rows that now exist, rather than against a
        # prediction of them: a name this very call queued is withheld in the list
        # this very call returns — the alternative hands over a capability for one
        # invocation and records it as withheld in the same breath. Re-reading is
        # also the only way to be right about a row the per-task cap `refused`
        # instead of queueing, which `withheld_tools` deliberately does not see.
        effect = effective_tools(
            config, role, list(spec.tools), *_row_facts(store, task_id, role)
        )

    allowed, withheld = effect.allowed, effect.withheld
    lost, capability, removed = effect.lost, effect.capability, effect.removed
    if removed:
        # Audited where a human reads the trail, not merely warned. The event is
        # per invocation, beside the `{kind}_prompt` it explains, because that is
        # when the capability was actually withheld; the row-level fact ("deciding
        # this also decides that") rides `tool_requested` instead.
        #
        # The guard is `removed`, not `lost`. `lost` is the *collateral* half by
        # construction — `subtract_withheld` excludes a tool that is itself
        # withheld — so a denial that removes only the tool a human denied fired
        # nothing at all, and that is the one case with no other signal in the
        # ledger (the ask's own `tool_requested` event says a row was created, not
        # that a capability went away). A conditional whose predicate excludes the
        # requested tool cannot report the loss of the requested tool.
        cause = [
            w
            for w in withheld
            # Only the names that actually did the subtracting: a withheld
            # `task_state` resolves to nothing, so saying it "confers" the lost
            # capability names a tool that confers none.
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
                # The concrete names appear in the event payload only, exactly as
                # `tool_requested`'s `resolved` does: the ledger's columns, the
                # prompts and the dashboard stay provider-neutral, and this is the
                # one field that can answer "why did *that* stop working".
                "capability": capability,
                "message": (
                    f"{_and_list(removed)} stopped working for role {role!r} on "
                    f"this task: it needs {_and_list(capability)}, which is the "
                    f"capability {_and_list(cause)} confers, and that is withheld "
                    f"(rejected, or awaiting approval)."
                    + (
                        # NOT "was never requested": `lost` is "removed and not
                        # itself withheld", which is not the same claim. A
                        # collateral name can be an *approved* row — a human
                        # approving `shell` while another denied `git` puts
                        # `shell` here — and the event then asserted a human's own
                        # decision never happened. Say only what `lost` proves.
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
