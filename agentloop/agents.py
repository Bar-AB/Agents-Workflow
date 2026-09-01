"""Worker and validator wrappers: build prompts, invoke the ModelRunner,
record attempts/metrics in the store, parse validator verdicts."""

from __future__ import annotations

import json
import re

from .config import LoopConfig, estimate_cost_usd
from .memory import MemoryService
from .models import (
    PlannedTask,
    RunResult,
    Task,
    TaskStatus,
    TestResult,
    ToolRequestSource,
    ToolRequestStatus,
    Verdict,
    VerdictKind,
)
from .registry import Registry
from .runner import ModelRunner
from .store import Store

# Bound as names, not as a module: `agents.py` binds every import this way, and
# the byte-for-byte differential neuters this slice by patching
# `agentloop.agents.tools_for` / `.parse_tool_requests`. Patching the
# `toolpolicy` module object would be inert here, so the "neutered" run would
# silently be the live run and the differential would compare a run to itself.
#
# `MAX_TOOL_REASON_CHARS` is imported rather than restated: it is one bound on
# one field, and a second copy beside the parser that already truncates to it
# would be two numbers that must agree with nothing making them. Public in
# `toolpolicy` rather than imported under its underscore: a name another module
# depends on is part of that module's interface whatever it is spelled, and
# `from .x import _y` only hides the coupling from the reader of `x`.
from .toolpolicy import (
    MAX_TOOL_REASON_CHARS,
    ToolClass,
    classify,
    parse_tool_requests,
    tools_for,
)

# Decoration and/or whitespace: markdown emphasis, code ticks, spaces, newlines.
# Interleaved rather than "decoration then whitespace", because `**VERDICT:**
# approve` puts them in that order and `**VERDICT: approve**` in the other.
_VERDICT_GAP = r"[\s*_`~]*"
# The same, plus the separators a model puts *between* the three fields.
_VERDICT_SEP = r"[\s*_`~,;|·–—-]*"

# The decision-critical parser, deliberately tolerant of **decoration and
# separators** and deliberately strict about **meaning**.
#
# The strict original accepted exactly one rendering, and an unparseable verdict
# escalates at confidence 0 — below `severe_threshold`, so straight to
# NEEDS_HUMAN with no revision round and a `reasoning` reading "Unparseable
# validator output", which hides that the validator actually approved. Measured
# against real formatting, five of six ordinary shapes did that: per-field
# emphasis, comma and pipe separators, a bare `.95`, `n/a` for the value the
# prompt spells `na`, and a percentage.
#
# `toolpolicy._MARKER_RE` already made this argument and won it — it tolerates a
# markdown prefix, four reason separators, CRLF and every Unicode line
# terminator, because LLM output is markdown. This parser drives an automatic
# state transition and had none of that.
#
# What is NOT widened: the three verdict *kinds*, the requirement that all
# three labelled fields be present, and — most importantly — the **range** of a
# confidence. Prose that merely *sounds* like an approval still escalates at 0,
# and nothing here guesses a verdict. The two error directions are not
# symmetric — reading past a bold marker costs nothing, while failing to read a
# real decision spends a human's attention and is invisible in the record.
#
# The tests values *are* widened, to the `passed`/`failed` word forms, and that
# is a widening of spelling rather than of meaning: `passed` and `pass` are the
# same answer. The three answers themselves (true / false / no result) are the
# same three.
_VERDICT_RE = re.compile(
    rf"{_VERDICT_GAP}VERDICT{_VERDICT_GAP}:{_VERDICT_GAP}"
    rf"(approve|revise|escalate){_VERDICT_SEP}"
    rf"CONFIDENCE{_VERDICT_GAP}:{_VERDICT_GAP}"
    # A percentage is captured separately rather than folded into the number,
    # because `0.95` and `95%` are the same confidence written two ways and only
    # the `%` says which one was meant. A **bare** `95` is not disambiguated by
    # anything, so `parse_verdict` refuses it rather than guessing — see the
    # range check there, and note that an earlier version *clamped* instead,
    # which mapped it to 1.0 and auto-approved at maximum confidence.
    rf"(\d*\.?\d+){_VERDICT_GAP}(%?){_VERDICT_SEP}"
    rf"TESTS{_VERDICT_GAP}:{_VERDICT_GAP}"
    # `\b` so `TESTS: nap` is not read as `na`.
    #
    # `passed`/`failed` as well as `pass`/`fail`: both are at least as ordinary
    # an LLM rendering as the shapes this pattern was widened for, and the `\b`
    # above had silently narrowed them *out* — measured, `TESTS: passed` went
    # from parsing (under the old pattern, which had no `\b`) to escalating.
    # A slice whose stated purpose is surviving ordinary formatting must not
    # lose a form on the way. The optional suffixes restore them and keep the
    # `nap` protection, since `\b` still applies after the whole alternation.
    rf"(pass(?:ed)?|fail(?:ed|ing)?|n/a|na)\b",
    re.IGNORECASE,
)

# `n/a` is the same answer as `na`; the prompt asks for one and models write
# both. `None` means "no executed result to speak of", which the loop then
# resolves against `TestResult` rather than against this claim.
_TESTS_VALUES = {
    "pass": True,
    "passed": True,
    "fail": False,
    "failed": False,
    "failing": False,
    "na": None,
    "n/a": None,
}

# How much of the free-text context (feedback, or the output under review) feeds
# the memory retrieval query alongside the task definition.
_MAX_QUERY_EXTRA_CHARS = 2000

# How much of each completed dependency's output is handed to a dependent
# worker. Bounded because a task can wait on several others and their combined
# transcripts would otherwise be the largest thing in the prompt.
_MAX_UPSTREAM_CHARS = 2000

# How much of a tool's arguments the audit log keeps. The record answers "what
# was this tool called with", not "what did it write".
_MAX_TOOL_INPUT_CHARS = 2000
_MAX_TOOL_NAME_CHARS = 200
# A runner's own degradation note ("usage was estimated, here is why"). Wider
# than a tool name: it is a sentence with numbers in it, and truncating it to a
# name's width would cut off the part worth logging.
_MAX_RUNNER_NOTE_CHARS = 1000

# Largest reported token count treated as a number rather than as garbage. A
# bound on the *magnitude*, where the others here bound a length: see
# `_token_count` for why an unbounded `int` was a money bug rather than an
# untidy number.
_MAX_TOKEN_COUNT = 2**53

# How much of a validator's findings section the verdict row keeps. Truncation
# is acceptable here and deliberately *not* for the charter: the charter is an
# input the agent must obey in full, so cutting it removes a rule, while
# findings are a record of something that already happened, so cutting them
# loses detail from an account — the same trade `_tool_input_repr` makes.
_MAX_FINDINGS_CHARS = 4000

# The validator's findings marker. Soft by design: line-anchored and
# case-insensitive, matched only *after* the verdict line, and a miss simply
# yields no findings. A grammar would need validation, and validation is an
# exception path in something that must never fail an attempt.
_FINDINGS_RE = re.compile(r"^[ \t]*FINDINGS:[ \t]*", re.IGNORECASE | re.MULTILINE)
# A markdown *section heading* after the findings ends them, so a validator that
# writes `## Reasoning` below its list does not fold the reasoning into the
# evidence. Any heading level, matching what the docs promise.
#
# The blank line is load-bearing, not decoration. A bare `^#{1,6}\s` also matches
# a `# TODO: ...` line quoted *inside* a finding — a code reviewer quoting a
# comment is the common case here, not an exotic one — and every finding after it
# would be dropped with no signal. Requiring the blank line that precedes a real
# heading biases the remaining ambiguity toward keeping too much rather than too
# little, which is the right direction: these findings are evidence, so
# over-inclusion costs tidiness while under-inclusion destroys the record.
_FINDINGS_END_RE = re.compile(r"\n[ \t]*\n[ \t]*#{1,6}\s")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

# Which agents may author a tool request. An explicit allowlist, not
# `kind != "summarizer"`: the summarizer must never be parsed, because its output
# compresses a transcript that may quote a marker verbatim and parsing it would
# manufacture a request out of a *quotation*. A negative test would hand marker
# parsing to any role added later, silently, reintroducing exactly that bug. With
# an allowlist the failure of omission is "a new role's genuine ask is ignored"
# -> the tool is withheld -> the safe direction.
_MARKER_AGENT_KINDS = ("worker", "validator", "planner")

# Which ledger status each policy verdict lands on. A mapping rather than
# branches, so a `ToolClass` added later fails at the lookup instead of falling
# through to whichever branch happened to be last.
#
# "Fails at the lookup" is only an improvement because the lookup is now done
# *outside* `_invoke`'s closing transaction. Inside it — where it originally sat —
# "fails" meant rolling back an already-paid `finish_attempt` and having
# `_with_retry` buy the completion again, so the missing-branch `KeyError` this
# mapping exists to raise was a triple charge for a typo in an enum. The
# discipline is one thing, not two: choose the failing shape *and* put it where
# failing is cheap.
_TOOL_CLASS_STATUS = {
    ToolClass.AUTO: ToolRequestStatus.AUTO.value,
    ToolClass.GATED: ToolRequestStatus.PENDING.value,
    ToolClass.UNKNOWN: ToolRequestStatus.REFUSED.value,
}

# Largest planner reply worth attempting to parse. A real plan is a few KB; far
# past that the reply is runaway or hostile, and parsing it is the expensive
# part, so the check belongs before `json.loads`, not after.
_MAX_PLAN_REPLY_CHARS = 200_000


def _invoke(
    store: Store,
    runner: ModelRunner,
    task: Task,
    kind: str,
    role: str,
    model: str,
    system: str,
    prompt: str,
    tools: list[str] | None = None,
    retrieval: dict | None = None,
    charter_version: int | None = None,
    config: LoopConfig | None = None,
) -> tuple[RunResult, int]:
    """Run one agent invocation with full attempt/metrics bookkeeping.

    `charter_version` is which version of the project charter the caller put in
    `prompt`. It is a column on the attempt rather than an event: one scalar per
    invocation, unlike the variable-length lists `retrieval` and `tool_call`
    record. The caller builds the block and passes the version it built from, so
    the recorded version is by construction the one in the prompt.

    `config` is what makes slice 5 present. **`None` means the slice is absent
    from this invocation** — no marker parsing, no rows, no events — which is why
    `run_summarizer` and the eval harness, neither of which has a config to give,
    behave exactly as they did before it existed. `classify` therefore never sees
    `None` and keeps a required `config`: a policy function whose config may be
    missing would have to invent a risk judgment, which is the silent degrade to
    a working default `retrieval.get_backend` refuses by raising.
    """
    # The model call sits deliberately *between* two transactions, never inside
    # one: the store lock must not be held across a network call. Each paired
    # write (attempt row + its audit event) is atomic on its own.
    with store.transaction():
        attempt_id = store.start_attempt(task.id, kind, role, model, charter_version)
        # Which facts memory put in front of *this* agent on *this* round. It
        # goes in the opening transaction rather than the closing one because
        # the retrieval already happened — it built the prompt below — so a
        # failed model call must not lose the record of what was injected.
        # `tool_call` is its mirror image: those only exist once the run is over.
        if retrieval is not None:
            store.log_event(
                task.id,
                "retrieval",
                {
                    "attempt_id": attempt_id,
                    "agent_kind": kind,
                    "role": role,
                    **retrieval,
                },
            )
        store.log_event(
            task.id,
            f"{kind}_prompt",
            {"role": role, "prompt": prompt, "tools": list(tools or [])},
        )
    result = runner.run(system, prompt, model, tools)
    # The completion is now paid for, and every statement between here and the end
    # of the closing transaction runs over that payment: a raise discards the
    # tokens and cost the provider billed and `_with_retry` buys the same
    # completion again. `notes` and a tool's *name* are coerced at this seam
    # already, on the stated grounds that the `ModelRunner` protocol is a promise
    # and not a guarantee — and `output`, the *shape* of `tool_calls` and the cost
    # call's own arguments were trusted at the same seam, in the same transaction,
    # for the same money. Each was three paid calls, zero attempt rows, `$0`
    # measured spend and an `infra_error` pointing the human at the network. So
    # every value the rest of this function reads off `result` is normalized here,
    # once, above the transaction.
    if not isinstance(result.output, str):
        # A reply that is not text is not work. Coercing it with `str()` would
        # hand a validator an object's repr to review as a work product; blanking
        # it takes the existing empty-output rule to NEEDS_HUMAN, which is the
        # fail-safe direction. **Assigned back onto `result`, not kept as a
        # local**: `loop.run_task` reads `result.output.strip()` right after this
        # returns, and that read is *outside* `_with_retry` — `run_task` has no
        # `except Exception` of its own and neither does `_run_serial` — so a local
        # would leave a deterministic `AttributeError` one frame up that aborts the
        # whole batch instead of being retried. A different failure from the
        # re-charge, and a worse one.
        result.notes = (
            f"{_runner_note_repr(result.notes)} / " if result.notes else ""
        ) + (
            f"Runner returned a non-str output ({type(result.output).__name__}); "
            f"recorded as empty, which escalates the task rather than sending a "
            f"non-text reply to the validator."
        )
        result.output = ""
    else:
        # `isinstance(str)` bounds the *type* and says nothing about whether the
        # text can be written down. A lone surrogate (`'\ud83d'`, the high half
        # of a truncated emoji) is a perfectly ordinary `str` that sqlite cannot
        # store, so it killed `finish_attempt` itself — the first statement of
        # the closing transaction, over the paid completion — and `_with_retry`
        # bought the reply again. `json.loads` on a provider body containing one
        # produces exactly this, which puts it inside slice 4's shipped
        # `OpenAICompatRunner` rather than in theory.
        #
        # Repaired rather than blanked, unlike the non-`str` case above: that
        # reply was not text at all, while this one is a real work product with
        # one unwritable character in it, and blanking it would escalate a task
        # whose worker did the work. The substitution is visible (the escape
        # text, not a silent `?`) and audited through the same `runner_warning`
        # event, so nothing is quietly altered.
        safe_output = _utf8_safe(result.output)
        if safe_output != result.output:
            result.notes = (
                f"{_runner_note_repr(result.notes)} / " if result.notes else ""
            ) + (
                "Runner returned output that is not utf-8-encodable (a lone "
                "surrogate); the offending characters were escaped so the "
                "attempt could be recorded."
            )
            result.output = safe_output
    # Audited, not swallowed: `result.notes` is what the `runner_warning` event
    # below carries, so the degrade lands in `agentloop events`, the REST API and
    # the SSE feed instead of only in the attempt row's blank output.
    # The *serving* model, kept distinct from the requested `model` parameter: it
    # is what the pricing table and the attempt row must both read (slice 4), and
    # it crosses the seam, so it is coerced like any other reported field.
    served_model = _tool_name_repr(result.model)
    tokens_in = _token_count(result.tokens_in)
    tokens_out = _token_count(result.tokens_out)
    cache_creation = _token_count(result.cache_creation_tokens)
    cache_read = _token_count(result.cache_read_tokens)
    # A clamped count is a **substitution**, and it says so in the audit log. The
    # surrogate branch above already appends to `notes` for exactly this reason and
    # this branch did not, which is this project's recurring "coercion at some
    # sites and not others" shape: the ceiling landed in `attempts` and in
    # `task_metrics` indistinguishable from a measurement, and the dashboard
    # renders it as one. Appended to `notes` rather than given an event of its own,
    # so it rides the `runner_warning` slice 4 added for precisely this class.
    clamped = [
        name
        for name, reported in (
            ("tokens_in", result.tokens_in),
            ("tokens_out", result.tokens_out),
            ("cache_creation_tokens", result.cache_creation_tokens),
            ("cache_read_tokens", result.cache_read_tokens),
        )
        if _clamped_count(reported)
    ]
    if clamped:
        result.notes = (
            f"{_runner_note_repr(result.notes)} / " if result.notes else ""
        ) + (
            f"Runner reported {', '.join(clamped)} above the recordable ceiling "
            f"({_MAX_TOKEN_COUNT}); the recorded count is that ceiling, which is a "
            f"substitution and not a measurement."
        )
    cost = estimate_cost_usd(
        served_model, tokens_in, tokens_out, cache_creation, cache_read
    )
    tool_calls = _tool_call_records(result.tool_calls)
    # Parsed *before* the closing transaction opens, and deliberately: everything
    # from here to the end of that block runs over an already-paid
    # `finish_attempt`, so a raise inside it discards tokens and cost the provider
    # has billed and `_with_retry` buys the completion again. `parse_tool_requests`
    # is total on a `str` — which the coercion above is what guarantees, since a
    # regex over a non-`str` raises — and touches no store, but running it out here
    # means the regex, the bounds and the dedupe are not even in the transaction's
    # blast radius.
    #
    # The classification and the status lookup are hoisted out here for the same
    # reason, and it took a review to notice they were not: `classify` reads
    # `config.tool_readonly_allowlist`, whose type nothing validated, and
    # `_TOOL_CLASS_STATUS[cls]` is a deliberate `KeyError`. Both were raise-sources
    # sitting *inside* the paid transaction, one statement below a comment
    # explaining why raise-sources must be removed from it.
    #
    # **What hoisting buys is the rollback, not the re-charge** — an earlier
    # version of this comment claimed "once instead of three times" and that is
    # false, measured: `_invoke` runs inside `fn` under `_with_retry`, whose
    # `except Exception` catches a raise from *anywhere* in this function, so both
    # positions are re-paid `infra_max_retries + 1` times. Hoisted, the raise
    # merely happens before any closing write, so there is nothing committed to
    # roll back. (Not "and no `TransactionAborted` at an outer boundary" — that was
    # the same overclaim one size smaller: the closing block *is* the outermost
    # transaction here and nothing encloses it, so that exception was unreachable
    # from either position and naming it made hoisting sound like it bought a
    # protection it does not.) The consequence
    # matters more than the correction: hoisting is *not* an alternative to
    # removing a raise-source, which is why every value below is coerced and the
    # config is validated at construction (`config._coerced`) as well.
    parsed = (
        parse_tool_requests(result.output)
        if config is not None and kind in _MARKER_AGENT_KINDS
        else []
    )
    classified = [
        (p, cls, _TOOL_CLASS_STATUS[cls])
        for p, cls in ((p, classify(p.tool, config)) for p in parsed)
    ]
    with store.transaction():
        store.finish_attempt(
            attempt_id,
            result.output,
            tokens_in,
            tokens_out,
            cost,
            model=served_model,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
        )
        # Provenance for what the agent actually did, not just what it said:
        # one event per tool use, in the same transaction as the attempt it
        # belongs to. Slice 5's approval policy layers on top of this record.
        for call in tool_calls:
            store.log_event(
                task.id,
                "tool_call",
                {
                    "attempt_id": attempt_id,
                    "agent_kind": kind,
                    "role": role,
                    "tool": _tool_name_repr(call.get("tool")),
                    "input": _tool_input_repr(call.get("input")),
                    # Whether the tool *ran*, or was merely requested. The SDK
                    # path executes what it reports, so it defaults to True; a
                    # chat-completions backend has no execution loop and reports
                    # False. Recording both as the same fact would let slice 5's
                    # auto-approval policy read a request as an execution.
                    "executed": bool(call.get("executed", True)),
                },
            )
        # A runner that had to estimate its own usage, or degrade in any other
        # way, says so *in the audit log* — the one place `agentloop events`,
        # the REST API and the SSE feed all read. A `warnings.warn` alone is
        # invisible there, and the estimated tokens land in `attempts` and in
        # the `{kind}_output` event indistinguishable from measured ones, so the
        # dashboard renders a fabricated cost as a real measurement.
        if result.usage_estimated or result.notes:
            store.log_event(
                task.id,
                "runner_warning",
                {
                    "attempt_id": attempt_id,
                    "agent_kind": kind,
                    "role": role,
                    "model": served_model,
                    "usage_estimated": bool(result.usage_estimated),
                    # Coerced for the same reason `tool` is, and it is not
                    # optional: this event shares `log_event`'s one
                    # `json.dumps`, so an unencodable note here would raise
                    # inside the closing transaction and roll back the
                    # already-paid `finish_attempt` above.
                    "note": _runner_note_repr(result.notes),
                },
            )
        # The capability asks this agent wrote in its own reply. No `try`/`except`
        # anywhere in this loop, by design and not by omission: a swallowed
        # failure of a *nested* transaction leaves `_txn_aborted` set, so the
        # outer block rolls back the paid `finish_attempt` above and then raises
        # `TransactionAborted` at the outermost boundary — turning a one-in-a-
        # million telemetry hiccup into a guaranteed double charge. The
        # raise-sources are removed instead: the parse is already done, every
        # value below is a bounded plain `str`/`bool` before the call,
        # `tool_request_add` resolves the UNIQUE collision by reading rather than
        # by letting `INSERT` raise, and `tool_requests` declares no foreign keys.
        for p, cls, status in classified:
            store.tool_request_add(
                task.id,
                # Two distinct facts, never interchangeable: `role` is the
                # registry role (`spec.role`), `kind` is the loop's own literal
                # for which agent ran. A custom `task.worker_role` makes them
                # differ, and `granted_tools` keys on the role.
                role=role,
                agent_kind=kind,
                tool=_tool_name_repr(p.tool),
                status=status,
                source=ToolRequestSource.MARKER.value,
                reason=_plain_str(p.reason, MAX_TOOL_REASON_CHARS),
                blocking=bool(p.blocking),
                attempt_id=attempt_id,
                why=(
                    "not a known logical tool name" if cls is ToolClass.UNKNOWN else ""
                ),
                # **A blocking ask is exempt from the per-task queue cap.** Over
                # the cap a request is stored `refused`, and
                # `pending_blocking_tool_requests` reads `pending` only — so a
                # capped blocking ask could never park, turning "I cannot finish
                # without this" into "continue without it and tell no human",
                # which is the fail-safe inversion this project does not trade.
                # What the exemption costs is bounded by construction rather than
                # by trust: only a name in `LOGICAL_TOOL_MAP` and outside the
                # read-only allowlist can become a *pending blocking* row (an
                # unknown name is `refused`, a read-only one `auto`), so the
                # ceiling is those few names times the roles on the task —
                # single digits, whatever the agent emits.
                #
                # Rejected: escalating the refusal instead. `tool_request_decide`
                # accepts only a `pending` row, so a `refused` row can never be
                # decided; a task escalating on one would re-escalate every round
                # with no human action able to clear it. A park nobody can lift is
                # worse than a queue one row longer.
                #
                # `parked` is not passed at all: only the loop's park writes it.
                max_per_task=None if p.blocking else config.max_tool_requests_per_task,
            )
        store.log_event(
            task.id,
            f"{kind}_output",
            {
                "role": role,
                "output": result.output,
                # The coerced numbers, not the reported ones: this event shares
                # `log_event`'s one `json.dumps`, so a token field of a type it
                # cannot encode would raise here — inside the closing transaction,
                # over the paid `finish_attempt` above.
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_creation_tokens": cache_creation,
                "cache_read_tokens": cache_read,
                "cost_usd": cost,
            },
        )
    return result, attempt_id


def _tool_name_repr(value) -> str:
    """A tool's name as a bounded string, for the audit log only.

    Same hazard as `_tool_input_repr`, same transaction, and it was missed the
    first time: `tool` and `input` are encoded by the *one* `json.dumps` in
    `log_event`, so coercing only the input still let a non-string name roll
    back an already-paid `finish_attempt` and hand the call back to
    `_with_retry`. Duck-typed `ToolUseBlock`s make the name exactly as
    untrustworthy as the arguments.

    Not routed through `_tool_input_repr`: this field is rendered as a name by
    the dashboard and read as one by slice 5's policy, and `json.dumps` would
    turn `Read` into `"Read"`.
    """
    return _plain_str(value, _MAX_TOOL_NAME_CHARS)


def _runner_note_repr(value) -> str:
    """A runner's degradation note as a bounded string, for the audit log only.

    Same coercion as `_tool_name_repr` and for the same reason — it rides the
    same `json.dumps` inside the same closing transaction, and the `notes` field
    crosses the `ModelRunner` seam, where the type is a protocol's promise
    rather than a guarantee. Given a wider bound than a tool name because a note
    is a sentence about why a number is an estimate, and a truncated one loses
    exactly the numbers.
    """
    return _plain_str(value, _MAX_RUNNER_NOTE_CHARS)


def _token_count(value) -> int:
    """A reported token count as a non-negative int, or 0 for anything else.

    The same rule `runner._int_or_zero` applies at the other end of the seam, and
    for the same reason: a *type* nothing validated (a `'n/a'`, a nested dict)
    raised after the completion was billed. Here it protects two more statements
    than it does there — `estimate_cost_usd`'s arithmetic, and the `json.dumps`
    inside the closing transaction that encodes these numbers into the output
    event. A wrong number is caught by the runner's own never-zero guard; a raise
    was caught by nothing that could keep the reply.

    Restated rather than imported: the runner's copy is private and CLAUDE.md
    documents `extract_openai_usage`'s totality under that name, so the
    alternative was renaming it public in a module this change does not touch.

    **The magnitude is bounded too, not only the type.** Python's `int` is
    unbounded, so a 400-digit count — which is what `json.loads` hands back for a
    provider body carrying one — passed the type guard and then made
    `estimate_cost_usd`'s `tokens_in * pin` an `OverflowError: int too large to
    convert to float`. That raise is above the closing transaction, so it cost no
    attempt row, but `_with_retry` still bought the same completion three times
    and reported it as `infra_error`. Clamped rather than zeroed because the two
    directions are not symmetric: a clamp overstates the spend where a 0 would
    report an unbounded number as free. `2**53` is where an `int` stops being
    exactly representable as a float, so past it the value is no longer a
    measurement of anything.

    **How far "overstates" carries is bounded, and the docstring used to overstate
    it in turn.** The clamp trips the budget cap only if a later
    **iteration boundary** is reached — that is where the cap is read — so on a
    single approving round the task reaches DONE carrying the ceiling and no status
    changes at all. The escalation is therefore a tendency, not a guarantee, and it
    is not what keeps the substituted number honest: the `runner_warning` event
    `_invoke` fires for a clamped count is.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, min(int(value), _MAX_TOKEN_COUNT))
    except (ValueError, OverflowError):  # inf / nan
        return 0


def _clamped_count(value) -> bool:
    """Whether `_token_count` had to substitute its ceiling for this value.

    A separate predicate rather than a second return value from `_token_count`:
    that function has four call sites and a tuple return would rewrite all of them
    to carry a flag three of them would then have to re-join. Total on the same
    terms — anything that is not a count at all, or is `inf`/`nan`, is zeroed by
    `_token_count` rather than clamped, so it is not a substitution of a ceiling
    and does not report as one.

    Named residual, since the next reader will ask: a *zeroed* count is a
    substitution too, in the opposite and more dangerous direction (0 understates
    the spend where the ceiling overstates it), and it fires no warning today. That
    is a wider gap than this predicate — it would have to distinguish "the provider
    reported nothing" from "the provider reported garbage", which is
    `extract_usage`'s never-zero guard one layer down rather than a coercion here —
    so it is stated rather than quietly implied to be covered.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return int(value) > _MAX_TOKEN_COUNT
    except (ValueError, OverflowError):  # inf / nan
        return False


def _utf8_safe(text: str) -> str:
    """A `str` that sqlite and `json.dumps` can both actually write down.

    A lone surrogate passes every `isinstance(str)` check in this module and then
    raises `UnicodeEncodeError` at the sqlite driver — for `output` and `model`
    that meant inside `_invoke`'s closing transaction, on the already-paid
    `finish_attempt`. `backslashreplace` rather than `replace`: the escape keeps
    the evidence of what the provider sent, where a `?` would erase the one
    detail a reader would need, and both are equally safe to store.

    Total by contract, like every other coercion here — the surrogate is the only
    way a `str` fails to encode, and it is handled rather than raised.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _tool_call_records(value) -> list[dict]:
    """The tool calls in a reported `tool_calls`, dropping anything that is not one.

    `RunResult.tool_calls` is typed `list[dict]`, which across the `ModelRunner`
    seam is a promise: `None` made `for call in ...` a `TypeError`, and a list of
    bare names made `call.get(...)` an `AttributeError` — both inside the closing
    transaction, over an already-paid `finish_attempt`. This is the seam's existing
    "nothing recorded rather than a wrong record" contract (`extract_tool_calls`
    states it) applied to the *shape* of the field and not only to the values in
    it, so a mis-shaped report costs the provenance of that call rather than the
    attempt it belongs to.

    The `try` is deliberate and is not the ADR-4 hazard: it is here, in a pure
    function above the transaction, precisely so that there is none inside it.
    """
    try:
        items = list(value)
    except Exception:
        return []
    return [call for call in items if isinstance(call, dict)]


def _plain_str(value, limit: int) -> str:
    """Any value as a bounded plain string, never raising, never JSON-quoted.

    Bounded *and* encodable: the strings this returns are the served model, a
    tool name and a request reason, and all three go into a column inside a
    transaction holding a paid `finish_attempt`, so a lone surrogate anywhere in
    them is the same money bug as one in `output`. Scrubbed here, once, rather
    than at each of the three call sites — the companion-write hole this project
    has opened before is a coercion applied at some sites and not others.
    """
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return _utf8_safe(value)[:limit]
    try:
        text = str(value)
    except Exception:
        # A `__str__` that raises would put us straight back in the transaction
        # this function exists to protect.
        text = f"<unrepresentable {type(value).__name__}>"
    return _utf8_safe(text)[:limit]


def _tool_input_repr(value) -> str:
    """A tool's arguments as a bounded string, for the audit log only.

    Telemetry must never fail an attempt. `json.dumps` on a value it cannot
    encode raised inside `_invoke`'s closing transaction, rolling back
    `finish_attempt` — the output, tokens and cost of a model call already paid
    for — after which `_with_retry` ran the same call again. `ClaudeSDKRunner`
    sanitises its blocks, but the `ModelRunner` seam does not require it and a
    second provider is a new chance to get it wrong, so the coercion lives here,
    at the seam, rather than in one backend.

    Truncated because the log records what a tool was called with, not a copy of
    its payload: a megabyte of file content in an event row buys nothing.
    """
    try:
        text = json.dumps(value, sort_keys=True)
    except Exception:
        # `repr` is a fallback, not a guarantee — a __repr__ that raises would
        # put us straight back in the transaction this function exists to
        # protect, so the last resort names the type and nothing else.
        try:
            text = repr(value)
        except Exception:
            text = f"<unrepresentable {type(value).__name__}>"
    return text[:_MAX_TOOL_INPUT_CHARS]


def _charter_block(store: Store) -> tuple[str, int | None]:
    """Project-wide rules every agent works under, and the version they are.

    Returned with the version so the caller can record what this invocation
    actually ran under (`attempts.charter_version`), the same way `_memory_block`
    returns its provenance.

    `("", None)` when there is no charter — the same "empty means absent"
    convention the memory block uses, which is what makes "an empty or absent
    charter produces byte-for-byte today's prompt" hold structurally rather than
    by inspection.

    The body goes in verbatim and whole. It is never truncated, reordered or
    dropped here: a rule that silently falls off the end of a cap is worse than
    no rule, so oversize is refused loudly at write time in `Store.charter_set`
    instead. Note the block is rebuilt on every call, so the charter also
    survives a context handoff, which a rule mentioned once in a transcript does
    not.
    """
    active = store.charter_active()
    if active is None:
        return "", None
    version, body = active
    return f"\n## Project charter (v{version})\n{body}\n", version


def _memory_block(
    memory: MemoryService | None, query: str = "", task_id: int | None = None
) -> tuple[str, dict | None]:
    """Approved facts only — unvetted memory never reaches a prompt.

    `query` is what the facts are ranked against: the task the agent is about to
    work on. Passing it is what turns memory injection from "the first 20 facts
    alphabetically" into "the 20 facts about this task".

    Returns the block and its provenance, which the caller hands to `_invoke` so
    the retrieval is recorded against the attempt it fed. An unranked selection
    (no query, or ranking disabled) has no provenance and logs nothing.

    `task_id` is what any hit this injection earns is counted against — see
    `MemoryService._record_reads`."""
    if memory is None:
        return "", None
    facts, provenance = memory.facts_for_prompt(query=query, task_id=task_id)
    block = f"\n## Known project facts\n{facts}\n" if facts else ""
    return block, provenance


def _retrieval_query(task: Task, extra: str = "") -> str:
    """The text a task's memory is retrieved against.

    Title/goal/criteria are the stable statement of what the task needs;
    `extra` (validator feedback, or the output under review) is what makes a
    revision retrieve differently from the first attempt. Bounded, so a huge
    worker output can't drown the task's own vocabulary.

    The project charter is deliberately **not** part of this. It is prompt
    content only: folding it in would rank every task's memory against the same
    house-rule vocabulary, converging the ordering across all tasks and quietly
    undoing the relevance work retrieval exists to do."""
    base = f"{task.title}\n{task.goal}\n{task.acceptance_criteria}"
    return f"{base}\n{extra[:_MAX_QUERY_EXTRA_CHARS]}" if extra else base


def _upstream_block(store: Store, task: Task) -> str:
    """What the tasks this one waited for actually produced.

    A dependency edge that only delays a task is a schedule, not a plan: the
    reason "write the tests" waits for "write slugify()" is that it needs to see
    slugify(). Bounded per upstream task so a fan-in node's prompt stays mostly
    about its own work.

    Only DONE dependencies contribute, and that is checked here rather than
    assumed from the claim gate. The gate holds at *claim* time, but this block
    is rebuilt on every revision round — so an upstream task rejected or redone
    mid-round would otherwise be fed downstream as "what this task waited for"
    when it is no longer a finished result at all.
    """
    if task.id is None:
        return ""
    outputs = []
    for dep_id in store.dependencies(task.id):
        dep = store.get_task(dep_id)
        if dep is None or dep.status != TaskStatus.DONE or not dep.output.strip():
            continue
        outputs.append(f"### {dep.title}\n{dep.output[:_MAX_UPSTREAM_CHARS]}")
    if not outputs:
        return ""
    return (
        "\n## Upstream results (output of the tasks this one depends on)\n"
        + "\n\n".join(outputs)
        + "\n"
    )


def _test_block(result: TestResult | None) -> str:
    """Real executed results, so the validator judges reality rather than the
    worker's account of it."""
    if result is None or result.status == "na":
        return ""
    return (
        f"\n## Executed test results (authoritative)\n"
        f"status: {result.status} (exit code {result.exit_code})\n"
        f"{result.summary}\n\n"
        f"```\n{result.stdout_tail[-1500:]}\n```\n"
    )


def _gated_tools(
    store: Store,
    config: LoopConfig | None,
    spec,
    task_id: int | None,
    agent_kind: str,
) -> list[str]:
    """The tools this invocation may use — the gate's only bite point.

    The SDK executes an agent's tools *inside* `runner.run()`, so nothing after
    that call can withhold a capability; the `tools` list handed to it is the
    whole enforcement surface.

    **`config is None` means the slice is absent from this invocation**, not the
    slice running on defaults: the caller gets `spec.tools` itself, the same list
    object today's code passes, so `eval` and any other direct caller are
    byte-identical to the pre-slice-5 loop — `tools` order included, since the
    `{kind}_prompt` event records it. Stated once here rather than three times at
    the call sites, because it is a rule the differential measures.
    """
    if config is None:
        return spec.tools
    return tools_for(store, config, spec, task_id, agent_kind)


def run_worker(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    task: Task,
    feedback: str = "",
    memory: MemoryService | None = None,
    workspace: str | None = None,
    test_result: TestResult | None = None,
    handoff_summary: str | None = None,
    config: LoopConfig | None = None,
) -> RunResult:
    spec = registry.get(task.worker_role)
    prompt = (
        f"# Task: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n"
    )
    charter, charter_version = _charter_block(store)
    prompt += charter
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(task, feedback), task.id
    )
    prompt += memory_block
    prompt += _upstream_block(store, task)
    if workspace:
        prompt += (
            f"\n## Workspace\nWrite your files and tests under `{workspace}`. "
            f"They are executed there automatically after you finish.\n"
        )
    if handoff_summary is not None:
        # Context-budget handoff (slice 1): the prior worker's context grew past
        # its budget, so a fresh instance continues from a compacted summary in
        # place of the raw transcript (previous output + feedback), which is
        # exactly what would have overflowed.
        prompt += (
            f"\n## Handoff summary of prior work (context compacted)\n"
            f"{handoff_summary}\n"
            f"\nContinue the task from this summary, addressing the feedback it "
            f"describes. Prior work is summarized above rather than repeated in "
            f"full."
        )
    elif feedback:
        prompt += (
            f"\n## Your previous output\n{task.output}\n"
            f"\n## Validator feedback (revision {task.revision_count})\n"
            f"{feedback}\n"
        )
        prompt += _test_block(test_result)
        prompt += "\nRevise your output to address the feedback."
    result, _ = _invoke(
        store,
        runner,
        task,
        "worker",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        _gated_tools(store, config, spec, task.id, "worker"),
        retrieval,
        charter_version,
        config,
    )
    return result


def run_summarizer(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    task: Task,
    feedback: str = "",
    test_result: TestResult | None = None,
) -> RunResult:
    """Compact a task's working state for a context-budget handoff (slice 1).

    A ModelRunner call (so it works under MockRunner in tests). Recorded as its
    own attempt (kind='summarizer'), so its cost feeds the task budget cap but
    is kept separate from the worker's accumulated-context measure.

    Deliberately the one role with **no** project charter block: its output is
    consumed by a worker that rebuilds the charter fresh on every call,
    post-handoff included, so charging every handoff for a second copy of rules
    that arrive by another route buys nothing."""
    try:
        spec = registry.get("summarizer")
    except KeyError:
        # A hand-edited agents.json may predate the summarizer role; fall back
        # to the worker's spec so a handoff degrades rather than crashing.
        spec = registry.get(task.worker_role)
    prompt = (
        f"# Task being handed off: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Work so far (latest worker output)\n{task.output}\n"
    )
    if feedback:
        prompt += f"\n## Latest validator feedback\n{feedback}\n"
    prompt += _test_block(test_result)
    prompt += (
        "\nSummarize this working state so a fresh worker instance can continue "
        "with no loss of what matters."
    )
    result, _ = _invoke(
        store,
        runner,
        task,
        "summarizer",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        spec.tools,
    )
    return result


class PlanError(ValueError):
    """A planner reply that cannot become a task graph.

    Raised for anything unusable — unparseable JSON, a missing field, a
    dangling `depends_on` ref, a cycle, an oversized plan. The caller discards
    the *whole* plan and escalates: a half-applied decomposition is worse than
    none, because the missing half is invisible while the present half looks
    like a complete plan someone approved.
    """


def run_planner(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    plan_task: Task,
    memory: MemoryService | None = None,
    config: LoopConfig | None = None,
) -> RunResult:
    """Decompose a goal into a task graph (roadmap slice 3).

    Recorded as its own attempt (kind='planner') against the plan row, so the
    decomposition is auditable and its cost is attributed like any other agent
    invocation. Returns the raw reply; `parse_plan` turns it into nodes.
    """
    spec = registry.get("planner")
    prompt = (
        f"# Goal to decompose\n{plan_task.goal}\n\n"
        f"## Acceptance criteria for the goal as a whole\n"
        f"{plan_task.acceptance_criteria}\n"
    )
    # The planner is chartered because it authors the acceptance criteria the
    # validator later judges against: criteria that contradict a house rule
    # reproduce the conflict one level up, before any worker runs.
    charter, charter_version = _charter_block(store)
    prompt += charter
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(plan_task), plan_task.id
    )
    prompt += memory_block
    prompt += (
        "\nDecompose this into independently executable tasks and their "
        "dependencies, in the JSON format your instructions specify."
    )
    result, _ = _invoke(
        store,
        runner,
        plan_task,
        "planner",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        _gated_tools(store, config, spec, plan_task.id, "planner"),
        retrieval,
        charter_version,
        config,
    )
    return result


def parse_plan(text: str, max_tasks: int) -> list[PlannedTask]:
    """Parse a planner reply into graph nodes, or raise `PlanError`.

    Validation is deliberately all-or-nothing and happens *before* anything is
    written: refs must be unique, every `depends_on` must name a task in the
    same plan, and the edges must form a DAG. A plan that fails any of these is
    not repaired or partially applied — it escalates to a human, the same way an
    unparseable verdict escalates rather than being guessed at.
    """
    # Bound the input before parsing it. `max_tasks` is a cap on the *parsed*
    # plan, which is too late: a runaway or hostile reply costs the CPU and
    # memory of parsing it first, and deep nesting raises RecursionError, which
    # is not a JSONDecodeError and would escape as a traceback rather than an
    # escalation. Model replies that are legitimately plans are far under this.
    if len(text) > _MAX_PLAN_REPLY_CHARS:
        raise PlanError(
            f"planner reply is {len(text)} chars, over the "
            f"{_MAX_PLAN_REPLY_CHARS} limit; refusing to parse it"
        )
    fenced = _JSON_FENCE_RE.search(text)
    raw = fenced.group(1) if fenced else text
    try:
        data = json.loads(raw.strip())
    except RecursionError as exc:
        raise PlanError("planner reply is nested too deeply to parse") from exc
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise PlanError(f"planner reply is not valid JSON: {exc}") from exc

    # Accept the documented {"tasks": [...]} shape, and a bare array, which is
    # the one deviation a model reliably makes and which is unambiguous anyway.
    if isinstance(data, dict):
        items = data.get("tasks")
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if not isinstance(items, list):
        raise PlanError("planner reply has no 'tasks' array")
    if not items:
        raise PlanError("planner returned an empty plan (no tasks)")
    if len(items) > max_tasks:
        raise PlanError(
            f"plan has {len(items)} tasks, more than the {max_tasks} allowed "
            f"(max_plan_tasks)"
        )

    planned: list[PlannedTask] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PlanError(f"task #{i + 1} is not an object")
        ref = str(item.get("ref") or "").strip() or f"task-{i + 1}"
        missing = [f for f in ("title", "goal") if not str(item.get(f) or "").strip()]
        if missing:
            raise PlanError(f"task {ref!r} is missing: {', '.join(missing)}")
        criteria = str(item.get("acceptance_criteria") or "").strip()
        if not criteria:
            # The validator judges strictly against acceptance criteria, so a
            # task without any is a task no validator can ever approve.
            raise PlanError(f"task {ref!r} is missing: acceptance_criteria")
        try:
            risk = int(item.get("risk_level", 1))
        except (TypeError, ValueError):
            raise PlanError(f"task {ref!r} has a non-numeric risk_level") from None
        if risk not in (0, 1, 2):
            raise PlanError(f"task {ref!r} has risk_level {risk} (expected 0, 1 or 2)")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanError(f"task {ref!r} has a non-list depends_on")
        # De-duplicate: the edge table is keyed on (task, depends_on), so a
        # repeated ref inserts one row. Counting it twice would make the
        # `plan_created` event and the `task_dependency` events disagree with
        # the graph they claim to describe.
        deps = list(dict.fromkeys(str(d).strip() for d in deps))
        planned.append(
            PlannedTask(
                ref=ref,
                title=str(item["title"]).strip(),
                goal=str(item["goal"]).strip(),
                acceptance_criteria=criteria,
                risk_level=risk,
                depends_on=deps,
            )
        )

    refs = [p.ref for p in planned]
    if len(set(refs)) != len(refs):
        raise PlanError("plan reuses a task ref; refs must be unique")
    known = set(refs)
    for p in planned:
        for dep in p.depends_on:
            if dep not in known:
                raise PlanError(
                    f"task {p.ref!r} depends on {dep!r}, which is not in this plan"
                )
            if dep == p.ref:
                raise PlanError(f"task {p.ref!r} depends on itself (cycle)")
    _reject_cycles(planned)
    return planned


def _reject_cycles(planned: list[PlannedTask]) -> None:
    """Raise if the plan's edges are not a DAG.

    Kahn's algorithm: repeatedly remove nodes with no unmet dependency. Whatever
    remains is exactly the set of nodes trapped in (or downstream of) a cycle.
    """
    remaining = {p.ref: set(p.depends_on) for p in planned}
    while True:
        ready = [ref for ref, deps in remaining.items() if not deps]
        if not ready:
            break
        for ref in ready:
            del remaining[ref]
        for deps in remaining.values():
            deps.difference_update(ready)
    if remaining:
        raise PlanError(
            "plan dependencies contain a cycle involving: "
            + ", ".join(sorted(remaining))
        )


def run_validator(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    task: Task,
    worker_output: str,
    memory: MemoryService | None = None,
    test_result: TestResult | None = None,
    config: LoopConfig | None = None,
) -> tuple[Verdict, int]:
    spec = registry.get(task.validator_role)
    prompt = (
        f"# Task under review: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Worker output\n{worker_output}\n"
    )
    # The validator is chartered too: one that does not know the house rules
    # cannot catch a violation of them, and its verdict is the only route by
    # which a charter violation reaches the loop at all.
    charter, charter_version = _charter_block(store)
    prompt += charter
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(task, worker_output), task.id
    )
    prompt += memory_block
    prompt += _test_block(test_result)
    result, attempt_id = _invoke(
        store,
        runner,
        task,
        "validator",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        _gated_tools(store, config, spec, task.id, "validator"),
        retrieval,
        charter_version,
        config,
    )
    return parse_verdict(result.output), attempt_id


def _extract_findings(text: str) -> str:
    """The validator's `FINDINGS:` section, or "" — never an exception.

    A missing or malformed section must never fail an attempt, so every failure
    mode here degrades to empty. Findings run from the marker to the end of the
    text, or to the next markdown *section heading* — a heading on its own line
    after a blank one, so a `#`-prefixed line quoted inside a finding does not
    silently truncate the list.

    Deliberately lenient about the marker itself: it is matched anywhere at the
    start of a line rather than as a whole line, so a validator that writes
    `FINDINGS: nothing of note` inline is still recorded. Over-reading here
    stores a little extra prose; under-reading loses evidence, and only one of
    those is recoverable.

    Bounded, because this is a record of what already happened rather than an
    instruction that has to arrive intact.
    """
    try:
        m = _FINDINGS_RE.search(text)
        if not m:
            return ""
        tail = text[m.end() :]
        end = _FINDINGS_END_RE.search(tail)
        if end:
            tail = tail[: end.start()]
        return tail.strip()[:_MAX_FINDINGS_CHARS]
    except Exception:
        # Telemetry must never fail an attempt, and "no findings recorded" is a
        # legitimate state the loop already handles.
        return ""


def parse_verdict(text: str) -> Verdict:
    """Parse the validator's structured first line. An unparseable verdict is
    itself a failure signal -> escalate at confidence 0 (never guess-approve).

    `findings` is *added* to the verdict, never subtracted from `reasoning`:
    `reasoning` stays the whole post-verdict tail byte for byte, because the
    loop feeds it back to the worker as revision feedback and the findings are
    usually its most actionable part. Findings never rescue an unparseable
    verdict either — no `VERDICT:` line still escalates at confidence 0.
    """
    m = _VERDICT_RE.search(text)
    if not m:
        return Verdict(
            kind=VerdictKind.ESCALATE,
            confidence=0.0,
            reasoning=f"Unparseable validator output:\n{text}",
        )
    kind = VerdictKind(m.group(1).lower())
    raw_confidence = float(m.group(2))
    if m.group(3):  # written as a percentage
        raw_confidence /= 100.0
    # **Rejected, not clamped**, and the difference is the whole gate.
    #
    # The widened pattern accepts any magnitude, and an earlier version clamped
    # instead — which mapped every out-of-range number to `1.0`, the *top* of the
    # scale. Measured: `VERDICT: approve CONFIDENCE: 95 TESTS: pass` parsed as
    # APPROVE at 1.0, unconditionally clearing both `approve_threshold` (0.70)
    # and `severe_threshold` (0.40), so a task went DONE with no human — and
    # under the slice-3 graph that DONE releases every dependent. `CONFIDENCE:
    # 55` rewrote a validator's revise-band judgement into certainty the same
    # way. Before the widening, the strict pattern simply did not match those
    # replies and they escalated at 0.
    #
    # So widening the *pattern* turned a fail-safe non-match into a fail-open
    # maximum, on the one gate CLAUDE.md rules "never guess-approve". A bare
    # `95` is genuinely ambiguous — it could be a percentage missing its sign,
    # or a typo — and this parser does not guess: an out-of-range confidence is
    # an unparseable verdict, which is exactly what it was before.
    #
    # A percentage that clears 100 is refused on the same rule, for the same
    # reason. Nothing here clamps, because a clamp *substitutes the most
    # permissive legal value* for a value the model did not write.
    if not 0.0 <= raw_confidence <= 1.0:
        return Verdict(
            kind=VerdictKind.ESCALATE,
            confidence=0.0,
            reasoning=(
                f"Unparseable validator output (confidence "
                f"{m.group(2)}{m.group(3)} is outside 0-1):\n{text}"
            ),
        )
    confidence = raw_confidence
    tests = _TESTS_VALUES[m.group(4).lower()]
    reasoning = text[m.end() :].strip()
    return Verdict(
        kind=kind,
        confidence=confidence,
        reasoning=reasoning,
        tests_passed=tests,
        findings=_extract_findings(reasoning),
    )
