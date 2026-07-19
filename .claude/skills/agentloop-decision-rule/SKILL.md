---
name: agentloop-decision-rule
description: Use when adding or changing any decision rule in agentloop's orchestration state machine (loop.py) — approve/revise/escalate logic, confidence thresholds, budget caps, risk gating, bounded revisions, or how a validator verdict maps to a task status.
---

# Change a loop decision rule

The decision rules are the heart of agentloop and its most drift-prone code.
CLAUDE.md states the rule explicitly: **do not change the decision rules
without updating tests + README.** This skill makes that non-negotiable.

## Where the rules live

The state machine is `Loop.run_task` in `agentloop/loop.py`. Each round:

1. Budget check → `_budget_tripped` → `NEEDS_HUMAN`.
2. Worker output starting `ESCALATE:` → `NEEDS_HUMAN` (genuine ambiguity).
3. `severe` = verdict `escalate` OR `confidence < severe_threshold` →
   `NEEDS_HUMAN` immediately (no revision loop on severe disagreement).
4. `approved` = verdict `approve` AND `confidence >= approve_threshold` AND
   `tests_passed is not False` → `DONE`, unless `risk_level >=
   human_review_risk_level` → `NEEDS_HUMAN` for sign-off.
5. Otherwise → revise, bounded by `max_revisions`; exhausted → `NEEDS_HUMAN`.

Thresholds are fields on `LoopConfig` in `agentloop/config.py`
(`approve_threshold` 0.70, `severe_threshold` 0.40, `max_revisions` 3,
`human_review_risk_level` 2, budget caps). Change values there, not inline.

## Checklist — all four must move together

Create a todo per item. A change that lands in code but not the docs/tests is
considered incomplete.

1. **Edit `Loop.run_task` (and/or `_budget_tripped`) in `loop.py`.** Preserve
   the ordering: severe-disagreement is checked *before* approval, and
   budget *before* everything, on purpose. State the `reason=` string clearly —
   it is what the human sees in `status`/`events`.
2. **Update `LoopConfig` in `config.py`** if you add or rename a threshold.
   Keep the docstring at the top of `config.py` accurate.
3. **Update the module docstring at the top of `loop.py`** — it's a plain-English
   copy of the rules and must stay in sync.
4. **Update `README.md`** — the "Decision rules" table (§4–§5) and **`CLAUDE.md`**
   — the "Decision rules" section. These are the human contract; a code/doc
   mismatch here erodes trust in the whole loop.
5. **Add an e2e test** in `tests/test_loop.py` that exercises the new/changed
   rule. Use the `agentloop-loop-test` skill — every decision rule has a
   corresponding test asserting the resulting `task.status` and
   `escalation_reason`. Existing examples map 1:1 to rules
   (`test_severe_disagreement_escalates_immediately`,
   `test_bounded_retries_then_escalate`, `test_budget_cap_trips_to_human`, …).
6. **Run `pytest -q`** and confirm green before claiming done.

## Guardrails baked into the design (don't regress these)

- **Never guess-approve.** An unparseable verdict escalates at confidence 0
  (`parse_verdict` in `agents.py`). Any new path must fail safe toward
  `NEEDS_HUMAN`, never toward `DONE`.
- **No revision loop on severe disagreement** — severe short-circuits straight
  to human. Don't route it back through revise.
- **Budget is checked first, every round** — a new rule must not let the loop
  run an invocation before the budget gate.
- **Low-confidence approve is not done** — an `approve` below
  `approve_threshold` forces a revision (`test_low_confidence_approve_is_not_done`).
