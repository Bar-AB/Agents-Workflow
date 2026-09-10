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
from .toolpolicy import decision_effect, declared_tools


def _project_ref(raw: str | None) -> int | str | None:
    """Every `--project`/positional project argument on this CLI is typed
    `str` by argparse (none of them declare `type=int`, because the same
    flag also has to accept a name) -- so `Store.resolve_project`'s `int`
    branch, which is what makes a numeric id resolve, was unreachable from
    the CLI: `resolve_project("3")` took the name-lookup branch and raised
    `unknown project '3'` even when project 3 exists. Every CLI call site
    funnels its raw project text through this first. A project name that is
    itself all digits becomes untypeable through this CLI after this fix --
    the same trade-off `resolve_project` already made by using
    `isinstance(project, int)` as its own dispatch."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def _memory_cmd(store: Store, args) -> int:
    """`--project` is resolved here, once, only for the two sub-commands
    that need it to scope a QUERY or a WRITE (`list`/`add`) -- a bad
    name/id raises `KeyError`, caught by `main`'s existing handler.
    `approve`/`reject`/`pin`/`unpin` operate on a `memory_id` that already
    identifies its own project row; `--project` is accepted on those four
    for symmetry/discoverability but never consulted.

    `list` and `add` resolve an omitted `--project` DIFFERENTLY, on
    purpose: `Store.memory_list`'s own docstring names this the one place
    `project_id=None` ("every project", matching `list_tasks`'s unfiltered
    convention) and `resolve_project(None)` ("the default project") would
    otherwise collide -- `list` must pass the raw, unresolved `None`
    through so an unflagged `agentloop memory list` still shows every
    registered project's facts, exactly as it did before this slice. `add`
    is the opposite: a write must land in exactly one concrete project, so
    resolving an omitted `--project` to the default there is correct."""
    if args.mem_cmd == "list":
        project_id = (
            None
            if args.project is None
            else store.resolve_project(_project_ref(args.project))
        )
        rows = store.memory_list(project_id=project_id)
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
        project_id = store.resolve_project(_project_ref(args.project))
        store.memory_write(
            args.tier,
            args.key,
            args.value,
            approved=args.approved,
            pinned=args.pinned,
            project_id=project_id,
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
            approving = (
                f"approving does not deliver {_and_list(resolved)} — another "
                f"withheld request on this task confers it too"
            )
        if effect.reject_removes:
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
        if not resolved:
            return "confers no capability — nothing to be in force"
        return (
            f"NOT in force: {_and_list(resolved)} is withheld on account of "
            f"another request on this task"
        )
    if not resolved:
        return "nothing to withhold — this name confers no capability"
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
            print(f"        effect: {_tool_effect(store, loop, r)}")
            if r.reason:
                print(f"        reason: {r.reason}")
            if r.decided_by:
                note = f" — {r.decided_note}" if r.decided_note else ""
                print(f"        decided by {r.decided_by}{note}")
        return 0

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
    decided = store.tool_request_get(args.request_id)
    if decided is not None:
        print(f"        effect: {_tool_effect(store, loop, decided)}")
    parked = " (still parked on this request)" if decided and decided.parked else ""
    print(f"Task {task.id} -> {task.status.value}{parked}")
    return 0


def _charter_cmd(store: Store, args) -> int:
    """The human write surface for the project charter. Agents have none.

    `--project` is resolved once, here, through the same `_project_ref` +
    `resolve_project` path every other project-aware command uses — an
    omitted flag resolves to the default project, since a charter read or
    write always targets exactly one project, never "every project"."""
    project_id = _project_ref(args.project)
    if args.charter_cmd == "show":
        if args.version:
            row = store.charter_version(args.version, project_id)
            if row is None:
                raise KeyError(f"No charter version {args.version} on this project")
            print(f"--- charter v{row['id']} ---")
            if row["note"]:
                print(f"note: {row['note']}")
            print(row["body"] or "(cleared)")
            return 0
        active = store.charter_active(project_id)
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
            body = (
                Path(args.file).read_text(encoding="utf-8-sig")
                if args.file
                else args.text
            )
        except OSError as exc:
            raise ValueError(f"cannot read {args.file}: {exc}") from exc
        version = store.charter_set(body, args.note, project_id)
        print(f"Charter v{version} set ({len(body)} chars) — every agent prompt now.")
    elif args.charter_cmd == "clear":
        version = store.charter_clear(args.note, project_id)
        print(f"Charter cleared (v{version}); prompts return to having no charter.")
    elif args.charter_cmd == "history":
        rows = store.charter_history(project_id)
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
            raise ValueError(
                f"eval --mode batch requires --runner mock (got {args.runner!r}): "
                "batch fixtures are scripted whole-task transcripts, and a real "
                "provider cannot be handed a script."
            )
        result = evalmod.run_batch_eval(store, registry)
        print(evalmod.format_batch_report(result))
        return 0

    if args.runner == "claude":
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
    a.add_argument("--project", default=None, help="Project name or id")

    pl = sub.add_parser("plan", help="Decompose a goal into a task graph")
    pl.add_argument("goal")
    pl.add_argument("--criteria", required=True)
    pl.add_argument("--title", default="")
    pl.add_argument("--risk", type=int, default=1, choices=[0, 1, 2])
    pl.add_argument("--runner", default="claude", choices=["claude", "openai", "mock"])
    pl.add_argument("--project", default=None, help="Project name or id")

    ap = sub.add_parser("approve-plan", help="Sign a plan off; its tasks may run")
    ap.add_argument("task_id", type=int)
    ap.add_argument("--note", default="")

    r = sub.add_parser("run", help="Run the loop over pending tasks")
    r.add_argument("--runner", default="claude", choices=["claude", "openai", "mock"])
    r.add_argument("--max-tasks", type=int, default=None)
    r.add_argument(
        "--project",
        default=None,
        help="Project name or id (omit to span every registered project)",
    )

    s = sub.add_parser("status", help="Show tasks (or one task's metrics)")
    s.add_argument("task_id", nargs="?", type=int)
    s.add_argument("--project", default=None, help="Project name or id")

    for name in ("approve", "reject", "redo"):
        c = sub.add_parser(name, help=f"Human decision: {name} a task")
        c.add_argument("task_id", type=int)
        c.add_argument("--note", default="")

    for name in ("pause", "resume", "abort"):
        c = sub.add_parser(name, help=f"Mid-run control: {name} a task")
        c.add_argument("task_id", type=int)
        c.add_argument("--note", default="")

    e = sub.add_parser("events", help="Audit trail for a task (or a whole project)")
    e.add_argument("task_id", nargs="?", type=int)
    e.add_argument("--project", default=None, help="Project name or id")

    sv = sub.add_parser("serve", help="Run the live dashboard (Phase 2)")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
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
    mlist = msub.add_parser("list", help="Show all facts, both tiers")
    mlist.add_argument("--project", default=None, help="Project name or id")
    for name in ("approve", "reject", "pin", "unpin"):
        mc = msub.add_parser(name, help=f"{name} a memory fact")
        mc.add_argument("memory_id", type=int)
        mc.add_argument("--project", default=None, help="Project name or id")
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
    ma.add_argument("--project", default=None, help="Project name or id")

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
    cshow.add_argument("--project", default=None, help="Project name or id")
    cset = chsub.add_parser("set", help="Publish a new charter version")
    cset.add_argument("--file", default=None, help="Read the charter from a file")
    cset.add_argument("--text", default=None, help="Charter body inline")
    cset.add_argument("--note", default="", help="Why this edit was made")
    cset.add_argument("--project", default=None, help="Project name or id")
    cclear = chsub.add_parser("clear", help="Turn the charter off (audited)")
    cclear.add_argument("--note", default="")
    cclear.add_argument("--project", default=None, help="Project name or id")
    chist = chsub.add_parser("history", help="Every version, oldest first")
    chist.add_argument("--project", default=None, help="Project name or id")

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

    prj = sub.add_parser("project", help="Manage registered projects")
    prjsub = prj.add_subparsers(dest="project_cmd", required=True)
    prjadd = prjsub.add_parser("add", help="Register a new project")
    prjadd.add_argument("name")
    prjadd.add_argument("--repo-root", required=True)
    prjadd.add_argument(
        "--workspace-mode", default="scratch", choices=["scratch", "worktree"]
    )
    prjadd.add_argument(
        "--default", action="store_true", help="Make this the active default"
    )
    prjrename = prjsub.add_parser("rename", help="Rename a project")
    prjrename.add_argument("old_name")
    prjrename.add_argument("new_name")
    prjrepoint = prjsub.add_parser(
        "repoint", help="Change a project's repo_root/workspace_mode"
    )
    prjrepoint.add_argument("name")
    prjrepoint.add_argument("--repo-root", required=True)
    prjrepoint.add_argument(
        "--workspace-mode",
        default=None,
        choices=["scratch", "worktree"],
        help="Omit to keep the project's current workspace_mode unchanged",
    )
    prjlist = prjsub.add_parser("list", help="Every registered project")
    prjlist.add_argument(
        "--archived", action="store_true", help="Include archived projects too"
    )
    prjarchive = prjsub.add_parser(
        "archive", help="Archive a project (no active tasks)"
    )
    prjarchive.add_argument("name")
    prjuse = prjsub.add_parser("use", help="Set the active default project")
    prjuse.add_argument("name")

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
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1

    try:
        if args.cmd == "run":
            raw = getattr(args, "project", None)
            args.project_id = (
                store.resolve_project(_project_ref(raw)) if raw is not None else None
            )
        elif args.cmd in ("add", "plan", "status", "events"):
            args.project_id = store.resolve_project(
                _project_ref(getattr(args, "project", None))
            )
        return _dispatch(args, store, loop)
    except (KeyError, ValueError) as exc:
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
            project_id=args.project_id,
        )
        tid = store.add_task(task)
        print(f"Task {tid} defined: {args.title}")

    elif args.cmd == "plan":
        plan = loop.plan(
            args.goal, args.criteria, args.title, args.risk, project_id=args.project_id
        )
        children = store.plan_tasks(plan.id)
        if not children:
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
        n = loop.run(max_tasks=args.max_tasks, project_id=args.project_id)
        print(f"Processed {n} task(s).")
        for t in store.list_tasks(project_id=args.project_id):
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
                repo_root = loop._worktree_repo_root(t)
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
            for t in store.list_tasks(project_id=args.project_id):
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
        events = store.events(args.task_id, project_id=args.project_id)
        for ev in events:
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

    elif args.cmd == "project":
        return _project_cmd(store, args)
    return 0


def _project_cmd(store: Store, args) -> int:
    """`agentloop project add|rename|repoint|list|archive|use`. Raises
    KeyError/ValueError for a bad name/id, an unknown project, a duplicate
    name, or an invalid repo_root/workspace_mode — `main`'s existing
    `except (KeyError, ValueError)` handler renders these as a clean
    `error: ...` line, no new exception-handling code needed here."""
    if args.project_cmd == "add":
        pid = store.create_project(
            args.name, args.repo_root, workspace_mode=args.workspace_mode
        )
        if args.default:
            store.set_default_project(pid)
        print(f"Project {pid} registered: {args.name}")

    elif args.project_cmd == "rename":
        pid = store.resolve_project(_project_ref(args.old_name))
        store.rename_project(pid, args.new_name)
        print(f"Project {pid} renamed: {args.old_name} -> {args.new_name}")

    elif args.project_cmd == "repoint":
        pid = store.resolve_project(_project_ref(args.name))
        workspace_mode = args.workspace_mode
        if workspace_mode is None:
            workspace_mode = store.get_project(pid)["workspace_mode"]
        store.repoint_project(pid, args.repo_root, workspace_mode)
        print(f"Project {pid} repointed: {args.repo_root} ({workspace_mode})")

    elif args.project_cmd == "list":
        for p in store.list_projects(include_archived=args.archived):
            default_marker = "*" if p["is_default"] else " "
            archived_marker = " [archived]" if p["archived"] else ""
            print(
                f"{default_marker}[{p['id']}] {p['name']:20s} {p['repo_root']}"
                f"  ({p['workspace_mode']}){archived_marker}"
            )

    elif args.project_cmd == "archive":
        pid = store.resolve_project(_project_ref(args.name))
        store.archive_project(pid)
        print(f"Project {pid} archived: {args.name}")

    elif args.project_cmd == "use":
        pid = store.resolve_project(_project_ref(args.name))
        store.set_default_project(pid)
        print(f"Project {pid} is now the active default: {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
