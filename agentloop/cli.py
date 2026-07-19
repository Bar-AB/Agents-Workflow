"""CLI — plain structured output for Phase 1 (viz is Phase 2, spec §8).

  agentloop add "Title" --goal "..." --criteria "..." [--risk 0|1|2]
  agentloop run [--runner claude|mock] [--max-tasks N]
  agentloop status [TASK_ID]
  agentloop approve TASK_ID [--note ...]
  agentloop reject TASK_ID [--note ...]
  agentloop redo TASK_ID [--note ...]
  agentloop events TASK_ID
  agentloop init-registry          # write default agents.json for editing
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import LoopConfig
from .loop import Loop
from .models import Task
from .registry import DEFAULT_AGENTS, Registry
from .runner import get_runner
from .store import Store


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

    e = sub.add_parser("events", help="Audit trail for a task")
    e.add_argument("task_id", type=int)

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

        elif args.cmd == "events":
            for ev in store.events(args.task_id):
                print(f"{ev['ts']:.0f} {ev['kind']:20s} "
                      f"{json.dumps(ev['payload'])[:120]}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
