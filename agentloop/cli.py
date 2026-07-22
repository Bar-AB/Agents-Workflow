"""CLI — plain structured output, plus the Phase-2 dashboard server.

  agentloop add "Title" --goal "..." --criteria "..." [--risk 0|1|2]
  agentloop run [--runner claude|mock] [--max-tasks N]
  agentloop status [TASK_ID]
  agentloop approve TASK_ID [--note ...]
  agentloop reject TASK_ID [--note ...]
  agentloop redo TASK_ID [--note ...]
  agentloop events TASK_ID
  agentloop serve [--host H] [--port P]   # live dashboard (spec §8)
  agentloop memory list|approve|reject|add
  agentloop init-registry          # write default agents.json for editing
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import LoopConfig
from .loop import Loop
from .models import Task
from .registry import DEFAULT_AGENTS, Registry
from .runner import get_runner
from .server import serve_forever
from .store import Store


def _memory_cmd(store: Store, args) -> int:
    if args.mem_cmd == "list":
        rows = store.memory_list()
        if not rows:
            print("(no memory yet)")
        for r in rows:
            flag = "approved" if r["approved"] else "PENDING "
            print(f"[{r['id']:3d}] {flag} {r['tier']:8s} hits={r['hit_count']:<3d}"
                  f" {r['key']}: {r['value'][:60]}")
    elif args.mem_cmd == "approve":
        store.memory_set_approved(args.memory_id, True)
        print(f"Memory {args.memory_id} approved — agents may now read it.")
    elif args.mem_cmd == "reject":
        store.memory_delete(args.memory_id)
        print(f"Memory {args.memory_id} deleted.")
    elif args.mem_cmd == "add":
        store.memory_write(args.tier, args.key, args.value,
                           approved=args.approved, pinned=args.pinned)
        state = "approved" if args.approved else "pending approval"
        pin = ", pinned" if args.pinned else ""
        print(f"Wrote {args.tier}/{args.key} ({state}{pin}).")
    elif args.mem_cmd in ("pin", "unpin"):
        store.memory_set_pinned(args.memory_id, args.mem_cmd == "pin")
        print(f"Memory {args.memory_id} {args.mem_cmd}ned.")
    return 0


def _eval_cmd(store: Store, args) -> int:
    """Run the validator calibration harness (spec: eval)."""
    from . import eval as evalmod
    registry = Registry.load(
        LoopConfig.load(getattr(args, "config", None) or "loopconfig.json")
        .registry_path)
    if args.runner == "claude":
        # Opt-in and skipped without credentials — never a hard failure in CI.
        from .runner import anyio as _sdk
        if _sdk is None or not os.environ.get("ANTHROPIC_API_KEY"):
            print("eval --runner claude skipped: set ANTHROPIC_API_KEY and "
                  "install agentloop[claude] to run a real calibration.")
            return 0
        from .runner import ClaudeSDKRunner
        runner = ClaudeSDKRunner()
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentloop")
    p.add_argument("--config", default=None, help="Path to loopconfig.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Define a task")
    a.add_argument("title")
    a.add_argument("--goal", required=True)
    a.add_argument("--criteria", required=True)
    a.add_argument("--risk", type=int, default=1, choices=[0, 1, 2])

    r = sub.add_parser("run", help="Run the loop over pending tasks")
    r.add_argument("--runner", default="claude", choices=["claude", "mock"])
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
    sv.add_argument("--runner", default="mock", choices=["claude", "mock"])

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
    ma.add_argument("--pinned", action="store_true",
                    help="Pin: always injected, ahead of the cap (still needs "
                         "approval to be read)")

    ev = sub.add_parser("eval", help="Validator calibration harness")
    ev.add_argument("--runner", default="mock", choices=["claude", "mock"])

    sub.add_parser("init-registry", help="Write default agents.json")

    args = p.parse_args(argv)

    if args.cmd == "init-registry":
        Registry(dict(DEFAULT_AGENTS)).save("agents.json")
        print("Wrote agents.json")
        return 0

    store, loop = _build(args)
    try:
        if args.cmd == "add":
            task = Task(id=None, title=args.title, goal=args.goal,
                        acceptance_criteria=args.criteria,
                        risk_level=args.risk)
            tid = store.add_task(task)
            print(f"Task {tid} defined: {args.title}")

        elif args.cmd == "run":
            n = loop.run(max_tasks=args.max_tasks)
            print(f"Processed {n} task(s).")
            for t in store.list_tasks():
                print(f"  [{t.id}] {t.status.value:12s} {t.title}"
                      + (f"  <- {t.escalation_reason}"
                         if t.escalation_reason else ""))

        elif args.cmd == "status":
            if args.task_id:
                t = store.get_task(args.task_id)
                if not t:
                    print(f"No task {args.task_id}", file=sys.stderr)
                    return 1
                print(f"[{t.id}] {t.title}\n  status: {t.status.value}"
                      f"\n  revisions: {t.revision_count}"
                      f"\n  risk: {t.risk_level}")
                if t.escalation_reason:
                    print(f"  escalation: {t.escalation_reason}")
                print("  metrics:",
                      json.dumps(store.task_metrics(t.id), indent=4))
                if t.output:
                    print(f"\n--- output ---\n{t.output}")
            else:
                for t in store.list_tasks():
                    print(f"[{t.id}] {t.status.value:12s} rev={t.revision_count}"
                          f" risk={t.risk_level}  {t.title}")

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
                print(f"{ev['ts']:.0f} {ev['kind']:20s} "
                      f"{json.dumps(ev['payload'])[:120]}")

        elif args.cmd == "serve":
            config = LoopConfig.load(args.config or "loopconfig.json")
            serve_forever(store, loop, Registry.load(config.registry_path),
                          config, args.host, args.port)

        elif args.cmd == "eval":
            return _eval_cmd(store, args)

        elif args.cmd == "memory":
            return _memory_cmd(store, args)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
