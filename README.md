# agentloop

A general-purpose agentic development loop: worker agents execute tasks, an
independent validator reviews them against acceptance criteria, humans stay in
the loop at definition and review, and everything — state, metrics, audit
trail, memory — lives in one SQLite source of truth.

**Phase 1** (the vertical slice) and **Phase 2** (the live dashboard) are both
in. The full task lifecycle runs end-to-end with real, executed tests, and a
React dashboard streams the loop's state live from the same SQLite store the
loop writes to.

## Layout

```
agentloop/
  config.py    thresholds, budget caps, model pricing, sandbox + server knobs
  models.py    Task, Verdict, TestResult, AgentSpec, statuses
  store.py     SQLite source of truth: tasks, attempts (metrics), verdicts,
               test_runs, events (immutable audit log), two-tier memory
  registry.py  agent registry: role, model, prompt, tools, budget, version
  runner.py    ModelRunner seam: ClaudeSDKRunner | MockRunner
  agents.py    worker/validator prompt building + verdict parsing
  executor.py  sandboxed test execution in a per-task workspace
  memory.py    two-tier memory policy: gating + auto-promotion
  loop.py      orchestration state machine + human decisions + mid-run control
  eval.py      validator calibration harness (fixtures + agreement/confusion/
               calibration report)
  server.py    REST + SSE dashboard backend (stdlib only)
  cli.py       add / run / status / approve / reject / redo / pause / resume /
               abort / events / serve / memory / eval
web/           Vite + React + TypeScript dashboard
tests/         115 tests on MockRunner + real subprocesses (no API keys needed)
```

## Quick start

```bash
pip install -e ".[dev]"          # + ".[claude]" for the real runner
pytest -q                        # verify the loop

agentloop add "Add slugify util" \
  --goal "Write slugify(text) in utils.py" \
  --criteria "Lowercase, hyphen-separated, handles unicode, has tests" \
  --risk 1

agentloop run --runner claude    # or --runner mock for a dry run
agentloop status 1               # metrics: tokens, cost, wall time, verdicts
agentloop events 1               # immutable audit trail
agentloop approve 1              # human sign-off for escalated/high-risk tasks
agentloop redo 1                 # full redo: fresh start, no carried context
agentloop pause 1                # steer a running loop: pause / resume / abort
agentloop memory add k v --pinned --approved   # a fact that always injects
agentloop eval --runner mock     # validator calibration report (mock or claude)
```

### Dashboard (Phase 2)

```bash
cd web && npm install && npm run build   # once
agentloop serve                          # http://127.0.0.1:8765
```

Task board, agent state, cost/token tiles, verdict history, executed test
runs, a live audit feed, memory gating, and approve/reject/redo — all reading
the same store the loop writes to. `npm run dev` proxies the API for hot
reload.

## Decision rules (spec §4–§5)

Validator returns `VERDICT: <kind> CONFIDENCE: <0-1> TESTS: <pass|fail|na>`:

| Condition | Outcome |
|---|---|
| approve, confidence ≥ 0.70, tests not failing | done (or human sign-off if risk ≥ 2) |
| revise, or approve below threshold | revision with feedback, max 3 |
| escalate, or confidence < 0.40 | needs_human (severe disagreement) |
| worker replies `ESCALATE:` | needs_human (genuine ambiguity) |
| budget cap exceeded | needs_human (never burn unbounded) |
| unparseable verdict | needs_human (never guess-approve) |
| transient infra failure (runner/executor raises) | retried with backoff, then needs_human (`infra_error`) — not a revise |

**"Tests not failing" means the executed result.** Tests really run in the
task's workspace between worker and validator; the validator sees the real
output, and the gate consults the real status rather than the validator's
`TESTS:` claim. A validator claiming `pass` over an executed `fail` is logged
as a `test_disagreement` event — so a validator cannot approve past failing
tests, and its reliability is measured rather than assumed.

All thresholds live in `LoopConfig` (`loopconfig.json`), agents in
`agents.json` (`agentloop init-registry`).

## Memory (spec §7)

Two tiers, `project` and `loop`. Reads are gated on `approved`: a fact nobody
vetted never reaches a prompt, because a bad fact entering memory quietly
poisons every later task. Agent writes land unapproved and surface in the
dashboard (or `agentloop memory`) for a human to accept or discard. A project
fact read `memory_promote_threshold` times is promoted to the loop tier —
re-answering the same question is exactly the wasted spend tiering removes.

**Pinned facts.** Prompt injection is capped (20 facts) so memory can't crowd
out the task; past the cap, ordinary facts drop by alphabetical accident. Mark
a must-have fact `--pinned` (or pin it in the dashboard) and it sorts first and
bypasses the cap under its own smaller ceiling (10). Pinning does not bypass
approval — a pinned but unvetted fact still never reaches a prompt.

## Token & cost accounting

Usage is read from the SDK's terminal `ResultMessage` only (it already carries
the whole-run total, so summing per-message double-counts) and captures all
four fields: new input, output, and **prompt-cache** writes and reads. Cache is
priced off the input rate — writes ×1.25, reads ×0.10 — and counts toward both
the cost and token budget caps. This matters: a cached run can report 2 new
input tokens against ~21,000 in cache, so the pre-fix cap, blind to cache,
measured almost nothing. `estimate_cost_usd`'s cache arguments default to 0, so
the zero-priced `MockRunner` stays free.

## Mid-run human control

A running task can be steered between iterations without killing the process:
`agentloop pause|resume|abort <id>`, or the buttons in the dashboard. The signal
is written through the store, so it works cross-process and is read fresh at
each loop boundary (where the budget cap is checked). A paused task survives a
restart and does not auto-resume; an aborted task is terminal but defensible —
its output and full audit trail are left intact. Every transition is an event.

## Validator calibration harness

The decision rules lean on the validator's `CONFIDENCE` number, so `agentloop
eval` measures whether it's calibrated. It runs ~20 fixtures with known-correct
verdicts through `run_validator` and reports agreement rate, an
approve/revise/escalate confusion matrix, and a confidence-vs-correctness
calibration table (buckets straddling the 0.40/0.70 thresholds). `--runner mock`
is deterministic and runs in CI to exercise the harness mechanics; `--runner
claude` (opt-in, skipped without `ANTHROPIC_API_KEY`) produces a genuine
calibration measurement. Results persist to the `eval_runs` table.

## Sandboxing

This runs **arbitrary, AI-generated code** — whatever the worker wrote, invoked
by the test command. So command hijack is the *lesser* worry; the code the
command runs is the real exposure. Defenses are layered:

- **Command**: allowlisted in config, never taken from model output; split into
  argv and run without a shell (`pytest -q; rm -rf /` is literal argv, not
  operators); cwd pinned to `.agentloop/ws/task-{id}/`; timeout + output cap.
  A redo wipes the workspace, so "fresh start" means it.
- **Environment**: the child gets a **scrubbed, allowlisted env** — the parent
  environment (which holds `ANTHROPIC_API_KEY` and every other secret) is never
  passed wholesale, so generated code can't read credentials from it. Extra
  vars a project genuinely needs go in `sandbox_env_allowlist`.
- **Isolation tier** (`sandbox_isolation`): `env` (default) is env-scrub only.
  `strict` asks for a container / no-network / read-only-fs tier when a backend
  is available and **degrades to env-scrub with a warning** when it is not.
  **Residual risk of the env-scrub tier:** generated code still has this
  process's filesystem write access (absolute / `..` paths escape the
  workspace) and network access — only the environment is contained. Run under
  `strict` with a real backend, or an external sandbox, for untrusted code.

## Provider seam

The loop only knows the `ModelRunner` protocol. `ClaudeSDKRunner` is the
default backend; a litellm/OpenAI runner (e.g. a Codex cross-validator) is
just another implementation — this keeps the project open-sourceable and
model-agnostic. Your code, your license; SDK users bring their own
Anthropic credentials.

## Roadmap (from the seed spec)

- [x] Task lifecycle: define → execute → validate → approve/revise/escalate
- [x] Bounded retries, severe-disagreement tier, human escalation
- [x] Metrics per attempt (tokens, cost, wall time), budget caps
- [x] Immutable audit log; resumable loop; two-tier gated memory
- [x] Real test execution in a sandboxed per-task workspace, feeding the
      validator; registry tools passed through to the SDK
- [x] Memory wired into prompts, with gating and auto-promotion
- [x] Phase 2: live dashboard (REST + SSE) over the same store
- [x] Accurate token/cost accounting incl. prompt cache, feeding the budget cap
- [x] Validator calibration harness (`agentloop eval`)
- [x] Mid-run human control: pause / resume / abort, cross-process
- [x] Pinned memory facts that bypass the injection cap
- [ ] Context-budget handoff (summarize + fresh agent at <70% confidence)
- [ ] RAG store (Chroma/LanceDB) behind the memory tables
- [ ] Retrieval / tool-call provenance events (with the RAG/MCP work that needs
      them)
- [ ] Planner agent + task graph; parallel workers
- [ ] Second-provider cross-validator
- [ ] Agent-requested tools with an auto-approval policy
- [ ] git-commit-per-task rollback; infra retry/backoff; batch evaluation
