"""CLI — plain structured output, plus the Phase-2 dashboard server.

agentloop add "Title" --goal "..." --criteria "..." [--risk 0|1|2]
agentloop plan "Goal" --criteria "..." [--runner ...]   # decompose into a graph
agentloop approve-plan PLAN_ID [--note ...]             # release a plan's tasks
agentloop run [--runner claude|openai|mock] [--max-tasks N]
agentloop status [TASK_ID]
agentloop approve TASK_ID [--note ...]
agentloop reject TASK_ID [--note ...]
agentloop redo TASK_ID [--note ...]
agentloop events TASK_ID
agentloop serve [--host H] [--port P]   # live dashboard (spec §8)
agentloop memory list|approve|reject|add
agentloop tools list|approve|reject   # the agent-requested tool queue
agentloop charter show|set|clear|history   # project-wide rules for every agent
agentloop workspace prune [--repo-root PATH]     # remove terminal tasks' worktrees
agentloop workspace rebless [--repo-root PATH]   # human sign-off on a changed
                                                  # .git/config baseline (worktree mode)
agentloop init-registry          # write default agents.json for editing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import LoopConfig
from .executor import workspace_for
from .loop import Loop
from .models import Task, TaskStatus, ToolRequestStatus
from .registry import DEFAULT_AGENTS, Registry
from .runner import (
    RunnerConfigError,
    get_runner,
    resolve_tools,
    tools_sharing_capability,
)
from .server import serve_forever
from .store import Store
from .toolpolicy import declared_tools, decision_effect


def _memory_cmd(store: Store, args) -> int:
    if args.mem_cmd == "list":
        rows = store.memory_list()
        if not rows:
            print("(no memory yet)")
        for r in rows:
            flag = "approved" if r["approved"] else "PENDING "
            print(
                f"[{r['id']:3d}] {flag} {r['tier']:8s} tasks={r['hit_count']:<3d}"
                f" {r['key']}: {r['value'][:60]}"
            )
    elif args.mem_cmd == "approve":
        store.memory_set_approved(args.memory_id, True)
        print(f"Memory {args.memory_id} approved — agents may now read it.")
    elif args.mem_cmd == "reject":
        store.memory_delete(args.memory_id)
        print(f"Memory {args.memory_id} deleted.")
    elif args.mem_cmd == "add":
        store.memory_write(
            args.tier, args.key, args.value, approved=args.approved, pinned=args.pinned
        )
        state = "approved" if args.approved else "pending approval"
        pin = ", pinned" if args.pinned else ""
        print(f"Wrote {args.tier}/{args.key} ({state}{pin}).")
    elif args.mem_cmd in ("pin", "unpin"):
        store.memory_set_pinned(args.memory_id, args.mem_cmd == "pin")
        print(f"Memory {args.memory_id} {args.mem_cmd}ned.")
    return 0


def _tool_consequence(tool: str) -> str:
    """What one logical tool *is*, in one line — the map's half of the story.

    Two facts a bare logical name hides, both of them the reason this renders at
    all rather than printing `req.tool`:

    - `resolve_tools` expands the label, so `git` grants `Bash` — the label
      understates the capability.
    - `LOGICAL_TOOL_MAP` is not injective, so the *concrete* list is what the
      gate enforces and a decision on `shell` lands on `git` too.

    That second line is a statement about the **map**, and it is all this function
    is allowed to say. Whether the sharing actually costs or gains anything here
    depends on the row's role and on its sibling rows' statuses, neither of which
    the map knows — so "approving grants it too; rejecting stops it working" was
    false in three reachable states. The outcome is `_tool_outcome`'s job, and it
    comes from `toolpolicy.decision_effect`.

    Derived from `runner`'s pure map on every read rather than stored: the map is
    the single source of truth for it, and a copy in a column would be a second
    one that drifts the first time a logical name gains a concrete tool.
    """
    resolved = resolve_tools([tool])
    text = f"{tool} -> {', '.join(resolved) if resolved else '(nothing)'}"
    shared = tools_sharing_capability(tool)
    if shared:
        also = "; ".join(f"{n} -> {', '.join(c)}" for n, c in sorted(shared.items()))
        text += f"  [also decides: {also}]"
    return text


def _and_list(names: list[str]) -> str:
    if len(names) > 1:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return "".join(names)


def _agrees(names: list[str], singular: str, plural: str) -> str:
    """The verb that agrees with an `_and_list` of `names`.

    `web` resolves to two concrete names and `file_io` to three, so every one of
    these sentences is reachable in the plural — `WebFetch and WebSearch is not
    available to this role` was the read. Cheap, and it is the line a human reads
    immediately before granting a capability.
    """
    return plural if len(names) > 1 else singular


def _tool_outcome(request, effect) -> str:
    """What deciding *this row* would really do — the half the map cannot supply.

    Every branch is a statement about a field of `DecisionEffect`, and each of
    those is a difference between two evaluations of the list the gate enforces.
    So this cannot promise a grant that will not happen, or warn of a loss that
    cannot occur — the two failure directions of the sentence it replaces, the
    second of which pushed a human toward granting.
    """
    resolved = resolve_tools([request.tool])
    if request.status is ToolRequestStatus.PENDING:
        if effect.approve_grants:
            approving = f"approving grants {_and_list(effect.approve_grants)}"
            if effect.approve_enables:
                works = _agrees(effect.approve_enables, "works", "work")
                approving += f", and {_and_list(effect.approve_enables)} {works} again"
        elif effect.capability_live:
            approving = (
                f"approving changes nothing — this role already has "
                f"{_and_list(effect.capability_live)}"
            )
        elif not resolved:
            approving = "approving grants no capability — this name confers none"
        else:
            # Not granted, not already held, and the name does confer something:
            # the only remaining cause is another withheld row on this task
            # conferring the same concrete tool and still subtracting it.
            approving = (
                f"approving does not deliver {_and_list(resolved)} — another "
                f"withheld request on this task confers it too"
            )
        if effect.reject_removes:
            # No agreement to fix here or on `approving grants …`: the subject of
            # both is the gerund, not the list.
            rejecting = (
                f"rejecting also stops {_and_list(effect.reject_removes)} working"
            )
        elif effect.reject_loses:
            rejecting = f"rejecting withdraws {_and_list(effect.reject_loses)}"
        else:
            rejecting = "rejecting takes nothing further away"
        already = (
            f" ({_and_list(effect.costs_now)} "
            f"{_agrees(effect.costs_now, 'is', 'are')} already withheld "
            f"while this stands)"
            if effect.costs_now
            else ""
        )
        return f"{approving}; {rejecting}{already}"
    if request.status in (ToolRequestStatus.APPROVED, ToolRequestStatus.AUTO):
        if effect.in_effect:
            return (
                f"in force: the role has {_and_list(resolved)}"
                if resolved
                else "in force"
            )
        # An empty `resolved` is not a withheld capability, it is no capability:
        # a name in `tool_readonly_allowlist` that `LOGICAL_TOOL_MAP` lacks is an
        # `auto` row conferring nothing, and this branch rendered it as
        # `NOT in force:  is withheld on account of another request on this task`
        # — an empty list and a withholding that never happened, in one sentence.
        if not resolved:
            return "confers no capability — nothing to be in force"
        # "another *request*", not "another decision": the sibling that subtracts
        # the capability may be `pending`, which is nobody's decision.
        return (
            f"NOT in force: {_and_list(resolved)} is withheld on account of "
            f"another request on this task"
        )
    if not resolved:
        return "nothing to withhold — this name confers no capability"
    # `capability_live`, never `in_effect`: the sentence below is about the
    # *concrete* capability, and for a `refused` row the two disagree —
    # `withheld_tools` never sees `refused`, so the row subtracts nothing while its
    # logical name is still absent from `allowed`. Branching on the logical test
    # told a human `Bash is not available to this role` about a `Bash` the gate was
    # handing to the runner through the worker's declared `git`.
    missing = effect.capability_missing
    if not missing:
        return f"no effect: the role has {_and_list(effect.capability_live)} regardless"
    still = (
        f"; it still has {_and_list(effect.capability_live)}"
        if effect.capability_live
        else (
            f"; {_and_list(effect.costs_now)} "
            f"{_agrees(effect.costs_now, 'is', 'are')} withheld with it"
            if effect.costs_now
            else ""
        )
    )
    # "not available", not "withheld by this": a `refused` row (an unknown name, or
    # the per-task cap) subtracts nothing at all — `withheld_tools` never sees it —
    # so this states the outcome without claiming this row caused it.
    return (
        f"{_and_list(missing)} {_agrees(missing, 'is', 'are')} "
        f"not available to this role{still}"
    )


def _tool_effect(store: Store, loop: Loop, request) -> str:
    """`_tool_outcome` for one row, against the role's own declared list."""
    return _tool_outcome(
        request,
        decision_effect(
            store,
            loop.config,
            request.role,
            declared_tools(loop.registry, request.role),
            request,
        ),
    )


def _tools_cmd(store: Store, loop: Loop, args) -> int:
    """The human decision surface for the tool queue.

    `list` is the decision *point*: the consequence has to be readable here,
    before an `approve`/`reject` is typed, so each row carries the resolved
    concrete tools and the logical names that share them. The two decisions echo
    the same line, because "what did I just grant/deny" is the same question one
    moment later.
    """
    if args.tools_cmd == "list":
        rows = store.tool_requests(
            task_id=args.task, status="pending" if args.pending else None
        )
        if not rows:
            print("(no tool requests)")
        for r in rows:
            flags = "blocking" if r.blocking else "optional"
            if r.parked:
                flags += " PARKED"
            print(
                f"[{r.id:3d}] {r.status.value:8s} {flags:16s} task={r.task_id}"
                f" {r.agent_kind}/{r.role}  {_tool_consequence(r.tool)}"
            )
            # The map's sharing statement above says what a decision *touches*;
            # this says what it would actually do to this role on this task.
            print(f"        effect: {_tool_effect(store, loop, r)}")
            if r.reason:
                print(f"        reason: {r.reason}")
            if r.decided_by:
                note = f" — {r.decided_note}" if r.decided_note else ""
                print(f"        decided by {r.decided_by}{note}")
        return 0

    # A bad id raises KeyError, and an already-decided row raises ValueError;
    # both reach main()'s handler as `error: ...` with exit 1. Two humans (or one
    # double-click) racing is exactly that ValueError — the store's
    # compare-and-swap picks the winner and the loser is told, rather than being
    # guarded against here and silently reported as a success.
    request = store.tool_request_get(args.request_id)
    if request is None:
        raise KeyError(f"No tool request {args.request_id}")
    if args.tools_cmd == "approve":
        task = loop.approve_tool_request(args.request_id, args.note)
        verb = "approved"
    else:
        task = loop.reject_tool_request(args.request_id, args.note)
        verb = "rejected"
    print(f"Tool request {args.request_id} {verb}: {_tool_consequence(request.tool)}")
    # Read `parked` back rather than reusing the pre-decision row: a release
    # clears it, so the pre-decision value would report a lifted park as still
    # holding the task.
    decided = store.tool_request_get(args.request_id)
    if decided is not None:
        # The outcome of the decision just made, computed against the rows as they
        # now stand — so an approve that granted nothing (a rejected sibling still
        # withholds the capability) says so instead of echoing the request.
        print(f"        effect: {_tool_effect(store, loop, decided)}")
    parked = " (still parked on this request)" if decided and decided.parked else ""
    print(f"Task {task.id} -> {task.status.value}{parked}")
    return 0


def _charter_cmd(store: Store, args) -> int:
    """The human write surface for the project charter. Agents have none."""
    if args.charter_cmd == "show":
        if args.version:
            row = store.charter_version(args.version)
            if row is None:
                raise KeyError(f"No charter version {args.version}")
            print(f"--- charter v{row['id']} ---")
            if row["note"]:
                print(f"note: {row['note']}")
            print(row["body"] or "(cleared)")
            return 0
        active = store.charter_active()
        if active is None:
            print("(no charter set — agent prompts are unchanged)")
            return 0
        version, body = active
        print(f"--- charter v{version} (in effect) ---")
        print(body)
    elif args.charter_cmd == "set":
        if bool(args.file) == bool(args.text):
            raise ValueError("give exactly one of --file or --text")
        try:
            # utf-8-sig: a rules file is hand-written, often on Windows.
            body = (
                Path(args.file).read_text(encoding="utf-8-sig")
                if args.file
                else args.text
            )
        except OSError as exc:
            # An unreadable path is user input, not a crash — render it like a
            # bad id rather than a traceback.
            raise ValueError(f"cannot read {args.file}: {exc}") from exc
        # A ValueError from the store (oversize, or whitespace-only) surfaces
        # through main()'s handler as `error: ...`, which is the loud write-time
        # refusal the charter trades for never being trimmed at inject time.
        version = store.charter_set(body, args.note)
        print(f"Charter v{version} set ({len(body)} chars) — every agent prompt now.")
    elif args.charter_cmd == "clear":
        version = store.charter_clear(args.note)
        print(f"Charter cleared (v{version}); prompts return to having no charter.")
    elif args.charter_cmd == "history":
        rows = store.charter_history()
        if not rows:
            print("(no charter history)")
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
            label = f"{len(r['body'])} chars" if r["body"].strip() else "CLEARED"
            note = f"  {r['note']}" if r["note"] else ""
            print(f"[v{r['id']:3d}] {when}  {label}{note}")
    return 0


_TERMINAL_STATUSES = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ABORTED)


def _workspace_cmd(store: Store, config: LoopConfig, args) -> int:
    """`agentloop workspace prune|rebless` — the two operator-facing surfaces
    slice 9's P2 review named as no longer optional.

    `prune` removes the worktrees of tasks that reached a terminal state
    (`done`/`failed`/`aborted`) and then runs a bare `git worktree prune` as a
    catch-all, because `vcs.remove_worktree` correctly *refuses* a workspace
    whose directory is already gone rather than repairing the stale admin
    entry it leaves (see `vcs.prune_worktrees`'s docstring) — this command is
    the only thing that clears that case.

    `rebless` is the human-only surface `vcs._init_worktree`'s docstring names
    as deliberately absent from P2: once a repository has a recorded config
    baseline, a legitimate operator edit to `.git/config` (a new hook, a
    filter) makes every later task on that repository refuse forever with
    `config-changed`, because nothing else re-blesses it. This re-mints the
    baseline from the config as it stands *now* and records it through
    `Store.rebless_vcs_repo_pin` — an explicit, audited, never-automatic
    action; no agent and no `vcs.py`/`loop.py` call path may reach it.

    **`repo_root` resolution (P3 remediation cycle 3, HIGH 1).** `Store.
    _repo_key`/`vcs.config_pin` resolve a relative `repo_root` (the config
    default, `"."`) against *this process's* cwd — correct for the loop,
    which runs as one long-lived process at a fixed cwd, but `agentloop
    workspace rebless` is a separate, short-lived, human-invoked process
    that may run from anywhere. Measured: with `repo_root: "."` and no
    `--repo-root`, invoking `rebless` from inside a *different*, unrelated
    git repository silently reblessed *that* repository — a real,
    successful write, `rc=0`, a "success" message — while the intended
    repository's actual `config-changed`-blocking pin was never touched. No
    lexical resolution can recover "the right repository" from an
    ambiguous relative default (both directories are equally valid
    repositories from git's point of view), so this refuses the ambiguity
    outright rather than guessing: a relative `repo_root` is only accepted
    when the operator names the repository explicitly with `--repo-root`.
    An absolute `repo_root` (via `--repo-root` or an absolute value in
    `loopconfig.json`) is `os.path.abspath`'d once here and is then
    cwd-independent for the rest of the call — the same lexical convention
    `vcs._git`'s `-C` argument uses, and deliberately not `Path.resolve()`
    (`config._is_within`'s realpath is for pure containment comparisons,
    not for deciding what a git subprocess sees)."""
    from . import vcs

    repo_root_arg = args.repo_root or config.repo_root
    if args.workspace_cmd == "prune":
        if config.workspace_mode != "worktree":
            # `repo_root` is unread in scratch mode (the proven-no-op
            # contract), so it must not be able to break this fast path —
            # checked *before* the relative-path refusal below.
            print("workspace_mode is 'scratch' — nothing to prune.")
            return 0

    if not os.path.isabs(repo_root_arg):
        print(
            f"error: repo_root ({repo_root_arg!r}) is a relative path. "
            f"'agentloop workspace {args.workspace_cmd}' is a human-only, "
            f"security-relevant action and must not guess which repository "
            f"a relative path means from an unknown cwd — pass an absolute "
            f"--repo-root, or set an absolute repo_root in loopconfig.json.",
            file=sys.stderr,
        )
        return 1
    repo_root = os.path.abspath(repo_root_arg)
    if args.workspace_cmd == "prune":
        pin = store.vcs_repo_pin(repo_root)
        removed, skipped = 0, 0
        for t in store.list_tasks():
            if t.kind != "task" or t.status not in _TERMINAL_STATUSES:
                continue
            ws = workspace_for(
                config.workspace_root, t.id, config=config, repo_root=repo_root
            )
            if not ws.exists():
                continue
            result = vcs.remove_worktree(
                ws,
                config,
                pin,
                repo_root=repo_root,
                task_id=t.id,
                branch_prefix=config.vcs_branch_prefix,
            )
            if result.ok:
                removed += 1
                print(f"  task {t.id}: removed ({ws})")
            else:
                skipped += 1
                print(f"  task {t.id}: not removed ({result.reason}) — {ws}")
        # The catch-all: clears any *agentloop* admin entry `git worktree
        # list` still reports for a directory that is already gone by some
        # other means (a manual rm, a redo's rollback fallback) — the case
        # `remove_worktree` refuses rather than repairs. Scoped to
        # `vcs_branch_prefix` (HIGH 2, P3 remediation cycle 3): a bare
        # `git worktree prune` is repo-wide and would clear the operator's
        # own unrelated worktree registrations too.
        pruned = vcs.prune_worktrees(
            repo_root, config, branch_prefix=config.vcs_branch_prefix
        )
        if pruned.ok:
            print(f"Pruned stale worktree registrations under {repo_root}.")
        elif pruned.reason not in ("disabled", ""):
            print(f"git worktree prune: {pruned.reason}")
        print(f"{removed} worktree(s) removed, {skipped} skipped.")
        return 0

    if args.workspace_cmd == "rebless":
        fingerprint = vcs.config_pin(repo_root, repo_root)
        if not fingerprint:
            print(
                f"error: cannot read {repo_root}/.git/config — nothing to bless.",
                file=sys.stderr,
            )
            return 1
        old = store.vcs_repo_pin(repo_root)
        store.rebless_vcs_repo_pin(repo_root, fingerprint, args.note)
        if old:
            print(
                f"Repository baseline reblessed for {repo_root}: "
                f"{old[:12]} -> {fingerprint[:12]}"
            )
        else:
            print(
                f"Repository baseline recorded for {repo_root}: "
                f"{fingerprint[:12]} (was unpinned)."
            )
        return 0
    return 0


def _eval_cmd(store: Store, args) -> int:
    """Run an evaluation harness: per-verdict calibration, or whole-loop batch.

    `--mode verdict` (the default) is the pre-existing validator calibration.
    `--mode batch` drives whole tasks through a real `Loop` and measures the
    *decision rules* against a gold final status.
    """
    from . import eval as evalmod

    registry = Registry.load(
        LoopConfig.load(
            getattr(args, "config", None) or "loopconfig.json"
        ).registry_path
    )
    if getattr(args, "mode", "verdict") == "batch":
        if args.runner != "mock":
            # Same refusal, and the same reason, as `--runner openai` falling
            # through to the mock branch used to earn: batch fixtures are
            # scripted round-by-round, so a real provider cannot consume one and
            # anything printed afterwards would be a number that measured
            # nothing -- which is worse than none.
            raise ValueError(
                f"eval --mode batch requires --runner mock (got {args.runner!r}): "
                "batch fixtures are scripted whole-task transcripts, and a real "
                "provider cannot be handed a script."
            )
        result = evalmod.run_batch_eval(store, registry)
        print(evalmod.format_batch_report(result))
        return 0

    if args.runner == "claude":
        # Opt-in and skipped without credentials — never a hard failure in CI.
        from .runner import anyio as _sdk

        if _sdk is None or not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "eval --runner claude skipped: set ANTHROPIC_API_KEY and "
                "install agentloop[claude] to run a real calibration."
            )
            return 0
        from .runner import ClaudeSDKRunner

        runner = ClaudeSDKRunner()
    elif args.runner == "openai":
        # The choice was accepted and then fell through to the mock branch, so
        # an operator asking for an OpenAI calibration got scripted-fixture
        # agreement numbers printed as a calibration report, with no warning and
        # exit 0. A calibration number that measured nothing is worse than none.
        if not os.environ.get("OPENAI_API_KEY"):
            print(
                "eval --runner openai skipped: set OPENAI_API_KEY to run a real "
                "calibration against an OpenAI-compatible endpoint."
            )
            return 0
        from .runner import OpenAICompatRunner

        runner = OpenAICompatRunner()
    else:
        runner = evalmod.mock_runner_for(evalmod.FIXTURES)

    result = evalmod.run_eval(store, runner, registry)
    print(evalmod.format_report(result))
    return 0


def _build(args) -> tuple[Store, Loop]:
    config = LoopConfig.load(getattr(args, "config", None) or "loopconfig.json")
    store = Store(config.db_path)
    registry = Registry.load(config.registry_path)
    runner = get_runner(getattr(args, "runner", "claude"))
    return store, Loop(store, runner, registry, config)


def _make_output_encoding_total() -> None:
    """Never let an em-dash end a command.

    On Windows, redirecting output (`agentloop status 1 > out.txt`, or any pipe)
    switches stdout from the console encoding to the ANSI code page, and this
    CLI prints prose containing en/em dashes. Measured: `agentloop status 1 |
    head` raised `UnicodeEncodeError` *before* reaching the output section, and
    `main`'s handler catches `(KeyError, ValueError)` — `UnicodeEncodeError`
    subclasses `ValueError` — so it surfaced as the single word `error: charmap`
    and exit 1. Redirecting `status` or `events` to a file is an ordinary thing
    to do, and losing the command to it is not an acceptable outcome for a
    character in a sentence.

    `errors="replace"` rather than forcing UTF-8: the goal is that output can
    always be written, not that a console silently changes encoding under the
    operator. Best-effort — `reconfigure` is absent if a caller has replaced the
    streams (pytest's `capsys` does), and that is not worth failing over."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _make_output_encoding_total()
    p = argparse.ArgumentParser(prog="agentloop")
    p.add_argument("--config", default=None, help="Path to loopconfig.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Define a task")
    a.add_argument("title")
    a.add_argument("--goal", required=True)
    a.add_argument("--criteria", required=True)
    a.add_argument("--risk", type=int, default=1, choices=[0, 1, 2])

    pl = sub.add_parser("plan", help="Decompose a goal into a task graph")
    pl.add_argument("goal")
    pl.add_argument("--criteria", required=True)
    pl.add_argument("--title", default="")
    pl.add_argument("--risk", type=int, default=1, choices=[0, 1, 2])
    pl.add_argument("--runner", default="claude", choices=["claude", "openai", "mock"])

    ap = sub.add_parser("approve-plan", help="Sign a plan off; its tasks may run")
    ap.add_argument("task_id", type=int)
    ap.add_argument("--note", default="")

    r = sub.add_parser("run", help="Run the loop over pending tasks")
    r.add_argument("--runner", default="claude", choices=["claude", "openai", "mock"])
    r.add_argument("--max-tasks", type=int, default=None)

    s = sub.add_parser("status", help="Show tasks (or one task's metrics)")
    s.add_argument("task_id", nargs="?", type=int)

    for name in ("approve", "reject", "redo"):
        c = sub.add_parser(name, help=f"Human decision: {name} a task")
        c.add_argument("task_id", type=int)
        c.add_argument("--note", default="")

    for name in ("pause", "resume", "abort"):
        c = sub.add_parser(name, help=f"Mid-run control: {name} a task")
        c.add_argument("task_id", type=int)
        c.add_argument("--note", default="")

    e = sub.add_parser("events", help="Audit trail for a task")
    e.add_argument("task_id", type=int)

    sv = sub.add_parser("serve", help="Run the live dashboard (Phase 2)")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    # Kept for argument-shape compatibility, but `serve` never invokes a model:
    # its routes call only the human decision methods (`approve`/`reject`/`redo`,
    # `pause`/`resume`/`abort`, the tool-request decisions), none of which run an
    # agent. An operator reasonably reads `serve --runner claude` as "the
    # dashboard will now drive real work", and it does nothing at all — so the
    # help text says so rather than leaving it to be discovered.
    sv.add_argument(
        "--runner",
        default="mock",
        choices=["claude", "openai", "mock"],
        help=(
            "Accepted but inert: `serve` runs no agents, it only serves the "
            "dashboard. Use `agentloop run --runner ...` to drive the loop."
        ),
    )

    m = sub.add_parser("memory", help="Inspect and gate the memory store")
    msub = m.add_subparsers(dest="mem_cmd", required=True)
    msub.add_parser("list", help="Show all facts, both tiers")
    for name in ("approve", "reject", "pin", "unpin"):
        mc = msub.add_parser(name, help=f"{name} a memory fact")
        mc.add_argument("memory_id", type=int)
    ma = msub.add_parser("add", help="Add a fact directly")
    ma.add_argument("key")
    ma.add_argument("value")
    ma.add_argument("--tier", default="project", choices=["project", "loop"])
    ma.add_argument("--approved", action="store_true")
    ma.add_argument(
        "--pinned",
        action="store_true",
        help="Pin: always injected, ahead of the cap (still needs approval to be read)",
    )

    tl = sub.add_parser("tools", help="The agent-requested tool queue")
    tlsub = tl.add_subparsers(dest="tools_cmd", required=True)
    tlist = tlsub.add_parser("list", help="Show requests, oldest first")
    tlist.add_argument("--task", type=int, default=None, help="One task only")
    tlist.add_argument(
        "--pending", action="store_true", help="Only requests awaiting a human"
    )
    for name in ("approve", "reject"):
        tc = tlsub.add_parser(name, help=f"{name} one tool request")
        tc.add_argument("request_id", type=int)
        tc.add_argument("--note", default="", help="Why, for the audit trail")

    ch = sub.add_parser("charter", help="Project-wide rules injected into agents")
    chsub = ch.add_subparsers(dest="charter_cmd", required=True)
    cshow = chsub.add_parser("show", help="The charter in effect (or one version)")
    cshow.add_argument(
        "--version", type=int, default=None, help="Show a past version instead"
    )
    cset = chsub.add_parser("set", help="Publish a new charter version")
    cset.add_argument("--file", default=None, help="Read the charter from a file")
    cset.add_argument("--text", default=None, help="Charter body inline")
    cset.add_argument("--note", default="", help="Why this edit was made")
    cclear = chsub.add_parser("clear", help="Turn the charter off (audited)")
    cclear.add_argument("--note", default="")
    chsub.add_parser("history", help="Every version, oldest first")

    ws = sub.add_parser("workspace", help="Worktree-mode workspace maintenance")
    wssub = ws.add_subparsers(dest="workspace_cmd", required=True)
    wsprune = wssub.add_parser(
        "prune", help="Remove terminal tasks' worktrees; clear stale registrations"
    )
    wsprune.add_argument(
        "--repo-root", default=None, help="Defaults to config.repo_root"
    )
    wsrebless = wssub.add_parser(
        "rebless",
        help="Human sign-off on a legitimately changed .git/config baseline",
    )
    wsrebless.add_argument(
        "--repo-root", default=None, help="Defaults to config.repo_root"
    )
    wsrebless.add_argument("--note", default="", help="Why, for the audit trail")

    ev = sub.add_parser("eval", help="Evaluation harness (calibration / batch)")
    ev.add_argument("--runner", default="mock", choices=["claude", "openai", "mock"])
    ev.add_argument(
        "--mode",
        default="verdict",
        choices=["verdict", "batch"],
        help="verdict: validator calibration; batch: whole-loop decision rules",
    )

    sub.add_parser("init-registry", help="Write default agents.json")

    args = p.parse_args(argv)

    if args.cmd == "init-registry":
        Registry(dict(DEFAULT_AGENTS)).save("agents.json")
        print("Wrote agents.json")
        return 0

    try:
        store, loop = _build(args)
    except (KeyError, ValueError, RunnerConfigError) as exc:
        # Construction happens before the store exists, so it needs its own
        # handler rather than the one below: there is nothing to `close()` yet.
        # A bad `--runner`, an unparseable loopconfig.json or an OPENAI_BASE_URL
        # the runner refuses are all user input, and the whole point of raising
        # them loudly at construction is defeated if they arrive as a traceback.
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1

    try:
        return _dispatch(args, store, loop)
    except (KeyError, ValueError) as exc:
        # Bad id (no such task/memory row), or an id used with the wrong command
        # (approve-plan on an ordinary task), and similar expected user-input
        # errors surface as a clean message, never a raw traceback. KeyError's
        # str() wraps the message in quotes; unwrap it.
        msg = exc.args[0] if exc.args else str(exc)
        print(f"error: {msg}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _dispatch(args, store: Store, loop: Loop) -> int:
    """Run one subcommand. Raises KeyError for a bad id; main() renders that as
    a clean error. store is closed by main()'s finally."""
    if args.cmd == "add":
        task = Task(
            id=None,
            title=args.title,
            goal=args.goal,
            acceptance_criteria=args.criteria,
            risk_level=args.risk,
        )
        tid = store.add_task(task)
        print(f"Task {tid} defined: {args.title}")

    elif args.cmd == "plan":
        plan = loop.plan(args.goal, args.criteria, args.title, args.risk)
        children = store.plan_tasks(plan.id)
        if not children:
            # Every plan failure ends here: no tasks exist, and the reason the
            # planner was rejected is the only useful thing to print.
            print(f"Plan {plan.id} produced no tasks.", file=sys.stderr)
            print(f"  {plan.escalation_reason}", file=sys.stderr)
            return 1
        print(f"Plan {plan.id} created: {len(children)} task(s)")
        for c in children:
            deps = store.dependencies(c.id)
            dep_note = (
                f"  (depends on {', '.join(str(d) for d in deps)})" if deps else ""
            )
            print(f"  [{c.id}] risk={c.risk_level} {c.title}{dep_note}")
        if plan.status == TaskStatus.NEEDS_HUMAN:
            print(f"\nAwaiting sign-off: agentloop approve-plan {plan.id}")
        else:
            print("\nPlan auto-approved (plan_requires_approval=false); run to start.")

    elif args.cmd == "approve-plan":
        plan = loop.approve_plan(args.task_id, args.note)
        n = len(store.plan_tasks(plan.id))
        print(f"Plan {plan.id} approved — {n} task(s) released to the loop.")

    elif args.cmd == "run":
        n = loop.run(max_tasks=args.max_tasks)
        print(f"Processed {n} task(s).")
        for t in store.list_tasks():
            tag = "PLAN " if t.kind == "plan" else "     "
            print(
                f"  [{t.id}] {tag}{t.status.value:12s} {t.title}"
                + (f"  <- {t.escalation_reason}" if t.escalation_reason else "")
            )

    elif args.cmd == "status":
        if args.task_id:
            t = store.get_task(args.task_id)
            if not t:
                print(f"No task {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"[{t.id}] {t.title}\n  status: {t.status.value}"
                f"\n  revisions: {t.revision_count}"
                f"\n  risk: {t.risk_level}"
            )
            if t.kind == "plan":
                approved = store.is_plan_approved(t.id)
                kids = store.plan_tasks(t.id)
                print(f"  plan: {len(kids)} task(s), approved={approved}")
            else:
                deps = store.dependencies(t.id)
                if deps:
                    # Say which of them is actually holding this task up — "has
                    # dependencies" doesn't explain why nothing is happening.
                    unmet = [
                        d
                        for d in deps
                        if (dt := store.get_task(d)) and dt.status != TaskStatus.DONE
                    ]
                    print(f"  depends on: {', '.join(str(d) for d in deps)}")
                    if unmet:
                        print(f"  blocked by: {', '.join(str(d) for d in unmet)}")
                if t.plan_id and not store.is_plan_approved(t.plan_id):
                    print(f"  blocked by: plan {t.plan_id} (awaiting sign-off)")
                # Slice 9 residual 4: a worktree survives DONE and lives
                # outside the repository it works on (`worktree_root`), so an
                # operator debugging a task can no longer just look beside
                # their project directory — this is the other half of that
                # trade, `agentloop workspace prune` being the first. Printed
                # rather than probed for existence: the path is deterministic
                # from config + task id whether or not anything has created it
                # yet, and a task that never ran still has a workspace it
                # *would* use.
                repo_root = (
                    os.path.abspath(loop.config.repo_root)
                    if loop.config.workspace_mode == "worktree"
                    else None
                )
                ws = workspace_for(
                    loop.config.workspace_root,
                    t.id,
                    config=loop.config,
                    repo_root=repo_root,
                )
                print(f"  workspace: {ws}")
            if t.escalation_reason:
                print(f"  escalation: {t.escalation_reason}")
            print("  metrics:", json.dumps(store.task_metrics(t.id), indent=4))
            if t.output:
                print(f"\n--- output ---\n{t.output}")
        else:
            for t in store.list_tasks():
                tag = "PLAN " if t.kind == "plan" else "     "
                print(
                    f"[{t.id}] {tag}{t.status.value:12s} rev={t.revision_count}"
                    f" risk={t.risk_level}  {t.title}"
                )

    elif args.cmd in ("approve", "reject", "redo"):
        t = getattr(loop, f"human_{args.cmd}")(args.task_id, args.note)
        print(f"Task {t.id} -> {t.status.value}")

    elif args.cmd in ("pause", "resume", "abort"):
        if args.cmd == "resume":
            t = loop.resume(args.task_id)
        elif args.cmd == "pause":
            t = loop.pause(args.task_id)
        else:
            t = loop.abort(args.task_id, args.note)
        print(f"Task {t.id} -> {t.status.value} (control={t.control})")

    elif args.cmd == "events":
        for ev in store.events(args.task_id):
            print(f"{ev['ts']:.0f} {ev['kind']:20s} {json.dumps(ev['payload'])[:120]}")

    elif args.cmd == "serve":
        config_path = args.config or "loopconfig.json"
        config = LoopConfig.load(config_path)
        serve_forever(
            store,
            loop,
            Registry.load(config.registry_path),
            config,
            args.host,
            args.port,
            config_path=config_path,
        )

    elif args.cmd == "eval":
        return _eval_cmd(store, args)

    elif args.cmd == "memory":
        return _memory_cmd(store, args)

    elif args.cmd == "tools":
        return _tools_cmd(store, loop, args)

    elif args.cmd == "charter":
        return _charter_cmd(store, args)

    elif args.cmd == "workspace":
        return _workspace_cmd(store, loop.config, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
