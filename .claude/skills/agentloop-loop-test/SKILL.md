---
name: agentloop-loop-test
description: Use when writing or updating an end-to-end test for agentloop's loop behavior — any test in tests/test_loop.py that drives a task through the Loop with a scripted MockRunner and asserts on status, metrics, or the prompts agents saw. Encodes the repo's test idiom so new decision rules get correctly-shaped coverage.
---

# Write an end-to-end loop test

Tests in this repo are **end-to-end through the `Loop` with `MockRunner`** — no
API keys, no network, zero cost. You script the exact strings the worker and
validator "return" and assert on the resulting task state. Never mock internal
methods; drive the real state machine.

## The idiom (from `tests/test_loop.py`)

```python
loop, runner = make_loop(store, [worker_out, VERDICT, worker_out2, VERDICT2])
loop.run_task(task)
assert task.status == TaskStatus.DONE
```

- **`MockRunner(outputs)`** pops one output per `run()` call, in order.
- Outputs **alternate worker, validator, worker, validator, …** because each
  revision round is one worker call then one validator call. Count carefully:
  N revision rounds = 2N scripted outputs.
- Reuse the module-level verdict constants — don't hand-roll verdict strings:
  - `APPROVE` — `VERDICT: approve CONFIDENCE: 0.92 TESTS: pass`
  - `REVISE` — `VERDICT: revise CONFIDENCE: 0.55 TESTS: fail` + feedback
  - `SEVERE` — `VERDICT: escalate CONFIDENCE: 0.10 TESTS: fail`
  For a bespoke case (e.g. below-threshold approve), inline a verdict string
  matching the `VERDICT: … CONFIDENCE: … TESTS: …` grammar in `agents.py`.

## Helpers already in the file

- `store` fixture — a fresh `Store` on a `tmp_path` db.
- `make_loop(store, outputs, **cfg_overrides)` — builds `Loop` + `MockRunner`;
  pass config overrides like `max_revisions=2`, `max_tokens_per_task=10`.
- `add_task(store, risk=1)` — inserts a standard task, returns it.

## What to assert (pick what the rule is about)

1. **Terminal status** — `task.status == TaskStatus.DONE | NEEDS_HUMAN | FAILED`.
2. **Escalation reason** — `"Exhausted 2 revisions" in task.escalation_reason`,
   `"Budget cap" in ...`, `"Severe disagreement" in ...`. This proves *which*
   rule fired, not just that the task stopped.
3. **Revision count** — `task.revision_count == N` (e.g. severe → 0, no loop).
4. **Metrics** — `store.task_metrics(task.id)`: `attempts`, `tokens`, and the
   ordered `verdicts` list (`kinds == ["revise", "approve"]`).
5. **What an agent saw** — `runner.calls[n]["prompt"]` / `["system"]`. Use this
   to prove the validator got the worker's output, or that a revision prompt
   carried the validator's feedback (`"empty input" in runner.calls[2]["prompt"]`).
6. **Audit trail** — `store.events(task.id)` kinds, for human-decision and
   memory-write tests.

## Rules

- **One test per decision rule.** If you touched `loop.py`, this test is part
  of the change, not optional (see `agentloop-decision-rule`).
- **Assert the reason, not just the status.** Two different rules can both end
  in `NEEDS_HUMAN`; the `escalation_reason` substring is what distinguishes them.
- **Don't hit the network or add real-runner tests here.** Provider backends are
  verified for protocol shape only (see `agentloop-add-runner`).
- Run `pytest -q` and confirm green before claiming done.
