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

from .toolpolicy import (
    MAX_TOOL_REASON_CHARS,
    ToolClass,
    classify,
    parse_tool_requests,
    tools_for,
)

_VERDICT_GAP = r"[\s*_`~]*"
_VERDICT_SEP = r"[\s*_`~,;|·–—-]*"

_VERDICT_RE = re.compile(
    rf"{_VERDICT_GAP}VERDICT{_VERDICT_GAP}:{_VERDICT_GAP}"
    rf"(approve|revise|escalate){_VERDICT_SEP}"
    rf"CONFIDENCE{_VERDICT_GAP}:{_VERDICT_GAP}"
    rf"(\d*\.?\d+){_VERDICT_GAP}(%?){_VERDICT_SEP}"
    rf"TESTS{_VERDICT_GAP}:{_VERDICT_GAP}"
    rf"(pass(?:ed)?|fail(?:ed|ing)?|n/a|na)\b",
    re.IGNORECASE,
)

_TESTS_VALUES = {
    "pass": True,
    "passed": True,
    "fail": False,
    "failed": False,
    "failing": False,
    "na": None,
    "n/a": None,
}

_MAX_QUERY_EXTRA_CHARS = 2000

_MAX_UPSTREAM_CHARS = 2000

_MAX_TOOL_INPUT_CHARS = 2000
_MAX_TOOL_NAME_CHARS = 200
_MAX_RUNNER_NOTE_CHARS = 1000

_MAX_TOKEN_COUNT = 2**53

_MAX_FINDINGS_CHARS = 4000

_FINDINGS_RE = re.compile(r"^[ \t]*FINDINGS:[ \t]*", re.IGNORECASE | re.MULTILINE)
_FINDINGS_END_RE = re.compile(r"\n[ \t]*\n[ \t]*#{1,6}\s")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

_MARKER_AGENT_KINDS = ("worker", "validator", "planner")

_TOOL_CLASS_STATUS = {
    ToolClass.AUTO: ToolRequestStatus.AUTO.value,
    ToolClass.GATED: ToolRequestStatus.PENDING.value,
    ToolClass.UNKNOWN: ToolRequestStatus.REFUSED.value,
}

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
    cwd: str | None = None,
) -> tuple[RunResult, int]:
    """Run one agent invocation with full attempt/metrics bookkeeping.

    `cwd` is the directory this invocation's tools resolve relative paths
    against, handed straight to the seam (`ModelRunner.run`) and recorded in the
    `{kind}_prompt` event beside `tools`. It goes there for the same reason
    `tools` does: it is an **enforcement surface**, the one that decides where a
    `Write` lands, and a second one is not derivable from the first. An earlier
    version of this docstring argued it was derivable from the task id and the
    workspace root and left it unlogged — true only while every role's cwd is
    `workspace_for(root, id)`, which slice 9's P3 ends by handing the planner
    `repo_root`. Which role got which directory is then a fact no other row
    carries, and it would be invisible in `agentloop events`, the REST API and
    the SSE feed. It is a bounded string, so it adds no telemetry risk to the
    one `json.dumps` at `log_event` time. Every caller passes it by keyword and
    it defaults to `None`, so a role with no workspace to name (the summarizer,
    and the planner until P3) records `None` and is otherwise unchanged.

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
    with store.transaction():
        attempt_id = store.start_attempt(task.id, kind, role, model, charter_version)
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
            {
                "role": role,
                "prompt": prompt,
                "tools": list(tools or []),
                "cwd": cwd,
            },
        )
    result = runner.run(system, prompt, model, tools, cwd)
    # The completion is billed the instant `run()` returns: nothing from here
    # to the closing transaction below may raise, or a retry pays for it
    # again. Every `result.*` field is coerced to a safe value before use.
    if not isinstance(result.output, str):
        result.notes = (
            f"{_runner_note_repr(result.notes)} / " if result.notes else ""
        ) + (
            f"Runner returned a non-str output ({type(result.output).__name__}); "
            f"recorded as empty, which escalates the task rather than sending a "
            f"non-text reply to the validator."
        )
        result.output = ""
    else:
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
    served_model = _tool_name_repr(result.model)
    tokens_in = _token_count(result.tokens_in)
    tokens_out = _token_count(result.tokens_out)
    cache_creation = _token_count(result.cache_creation_tokens)
    cache_read = _token_count(result.cache_read_tokens)
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
                    "executed": bool(call.get("executed", True)),
                },
            )
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
                    "note": _runner_note_repr(result.notes),
                },
            )
        for p, cls, status in classified:
            store.tool_request_add(
                task.id,
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
                max_per_task=None if p.blocking else config.max_tool_requests_per_task,
            )
        store.log_event(
            task.id,
            f"{kind}_output",
            {
                "role": role,
                "output": result.output,
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
        try:
            text = repr(value)
        except Exception:
            text = f"<unrepresentable {type(value).__name__}>"
    return text[:_MAX_TOOL_INPUT_CHARS]


def _charter_block(
    store: Store, project_id: int | str | None = None
) -> tuple[str, int | None]:
    """Project-wide rules every agent works under, and the version they are.

    `project_id` is the calling task's own project (never left to
    default-resolve inside a multi-project database) — the same reason
    `_memory_block` threads it through, so a worker on one project never
    sees another registered project's charter injected into its prompt.

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
    active = store.charter_active(project_id)
    if active is None:
        return "", None
    version, body = active
    return f"\n## Project charter (v{version})\n{body}\n", version


def _memory_block(
    memory: MemoryService | None,
    query: str = "",
    task_id: int | None = None,
    project_id: int | None = None,
) -> tuple[str, dict | None]:
    """Approved facts only — unvetted memory never reaches a prompt.

    `query` is what the facts are ranked against: the task the agent is about to
    work on. Passing it is what turns memory injection from "the first 20 facts
    alphabetically" into "the 20 facts about this task".

    Returns the block and its provenance, which the caller hands to `_invoke` so
    the retrieval is recorded against the attempt it fed. An unranked selection
    (no query, or ranking disabled) has no provenance and logs nothing.

    `task_id` is what any hit this injection earns is counted against — see
    `MemoryService._record_reads`. `project_id` is the task's own project
    (slice 10) — never left to default-resolve, or a task belonging to a
    non-default project would be shown a different project's facts."""
    if memory is None:
        return "", None
    facts, provenance = memory.facts_for_prompt(
        query=query, task_id=task_id, project_id=project_id
    )
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


def _workspace_block(workspace: str, config: LoopConfig | None) -> str:
    """The `## Workspace` block. Scratch mode's wording (the `config is None`
    or `config.workspace_mode != 'worktree'` case) is kept **byte-for-byte**
    what it was before slice 9's P3 — the same "cleared vs never-set must be
    indistinguishable" discipline `_charter_block`/`_memory_block` already use
    in this file, applied to a new axis: an absent or scratch-mode config must
    leave this prompt exactly what it was.

    Worktree mode tells the worker something scratch mode's wording would be
    actively wrong about: the directory is a real checkout of the operator's
    repository, not blank space to create files in. "Write your files ... "
    reads as an invitation to scaffold a project from nothing; the worktree
    wording says to read first, match what is already there, and not
    restructure it wholesale."""
    if config is not None and config.workspace_mode == "worktree":
        return (
            f"\n## Workspace\n`{workspace}` is a checkout of the project's "
            f"existing repository, on its own branch. Read the code that is "
            f"already there before writing: follow its existing conventions "
            f"(style, structure, test layout) and make the smallest change "
            f"that satisfies the task — do not restructure or rewrite what "
            f"already works. Tests run there automatically after you finish.\n"
        )
    return (
        f"\n## Workspace\nWrite your files and tests under `{workspace}`. "
        f"They are executed there automatically after you finish.\n"
    )


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
    charter, charter_version = _charter_block(store, task.project_id)
    prompt += charter
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(task, feedback), task.id, project_id=task.project_id
    )
    prompt += memory_block
    prompt += _upstream_block(store, task)
    if workspace:
        prompt += _workspace_block(workspace, config)
    if handoff_summary is not None:
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
        cwd=workspace,
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
    cwd: str | None = None,
) -> RunResult:
    """Decompose a goal into a task graph (roadmap slice 3).

    `cwd` is `None` from a scratch-mode caller and the operator's `repo_root`
    from a worktree-mode one — slice 9 P4: `Loop.plan` passes
    `str(self._worktree_repo_root())` when it is not `None`. A plan row has no
    task workspace — there is no `task-<id>` directory for a goal that has not
    been decomposed yet - so in scratch mode there is nothing honest to point
    the planner at, and pointing it at the orchestrator's directory was the
    bug slice 9 P1 removed. In worktree mode this is the *operator's* repo,
    read-only: the planner declares `file_read`, not `file_io`, so surveying a
    codebase it may not modify is exactly what the role is for.

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
    charter, charter_version = _charter_block(store, plan_task.project_id)
    prompt += charter
    memory_block, retrieval = _memory_block(
        memory,
        _retrieval_query(plan_task),
        plan_task.id,
        project_id=plan_task.project_id,
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
        cwd=cwd,
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
    cwd: str | None = None,
) -> tuple[Verdict, int]:
    """Review the worker's output and return a parsed verdict.

    `cwd` is used for **one** thing: it becomes the validator's working
    directory at the seam. It is named for what it does, and named *differently*
    from `run_worker`'s `workspace`, which does two things — the same value also
    goes into that prompt's `## Workspace` block. Here an absent value must
    leave this prompt byte-for-byte what it was, and the validator is told what
    to review by the `## Worker output` section, not by a path, so an added
    `if cwd:` prompt line below must read as obviously wrong.

    The validator declares `file_io`, so before this it read and wrote in the
    orchestrator's own directory while nominally reviewing work that lives in
    the workspace. The same bug as the worker's and quieter, since a validator
    that finds nothing where it looked still returns a verdict.
    """
    spec = registry.get(task.validator_role)
    prompt = (
        f"# Task under review: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Worker output\n{worker_output}\n"
    )
    charter, charter_version = _charter_block(store, task.project_id)
    prompt += charter
    memory_block, retrieval = _memory_block(
        memory,
        _retrieval_query(task, worker_output),
        task.id,
        project_id=task.project_id,
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
        cwd=cwd,
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
    # Rejected, not clamped: clamping an out-of-range value (e.g. a stray
    # "95" meant as a percentage) to 1.0 previously auto-approved at maximum
    # confidence. Out of range is an unparseable verdict, same as before.
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
