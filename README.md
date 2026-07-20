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
  loop.py      orchestration state machine + human decisions
  server.py    REST + SSE dashboard backend (stdlib only)
  cli.py       add / run / status / approve / reject / redo / events /
               serve / memory
web/           Vite + React + TypeScript dashboard
tests/         61 tests on MockRunner + real subprocesses (no API keys needed)
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
agentloop memory list            # inspect + gate what agents may remember
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

## Sandboxing

The test command is allowlisted in config, never taken from model output. It
is split into argv and run without a shell, with cwd pinned to the task's own
workspace under `.agentloop/ws/task-{id}/`, plus a timeout and an output cap.
A redo wipes the workspace, so "fresh start" means it.

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
- [ ] Context-budget handoff (summarize + fresh agent at <70% confidence)
- [ ] RAG store (Chroma/LanceDB) behind the memory tables
- [ ] Planner agent + task graph; parallel workers
- [ ] Second-provider cross-validator
- [ ] Agent-requested tools with an auto-approval policy
- [ ] git-commit-per-task rollback; infra retry/backoff; batch evaluation
