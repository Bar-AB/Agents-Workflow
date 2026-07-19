# agentloop

A general-purpose agentic development loop: worker agents execute tasks, an
independent validator reviews them against acceptance criteria, humans stay in
the loop at definition and review, and everything — state, metrics, audit
trail, memory — lives in one SQLite source of truth.

This is the **vertical slice** (Phase 1 seed): the full task lifecycle runs
end-to-end, sequentially, with plain CLI output. Visualization is Phase 2 and
the data model already supports it.

## Layout

```
agentloop/
  config.py    thresholds, budget caps, model pricing
  models.py    Task, Verdict, AgentSpec, statuses
  store.py     SQLite source of truth: tasks, attempts (metrics),
               verdicts, events (immutable audit log), two-tier memory
  registry.py  agent registry: role, model, prompt, tools, budget, version
  runner.py    ModelRunner seam: ClaudeSDKRunner | MockRunner
  agents.py    worker/validator prompt building + verdict parsing
  loop.py      orchestration state machine + human decisions
  cli.py       add / run / status / approve / reject / redo / events
tests/         12 end-to-end tests on MockRunner (no API keys needed)
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
```

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

All thresholds live in `LoopConfig` (`loopconfig.json`), agents in
`agents.json` (`agentloop init-registry`).

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
- [ ] Real tool use in worker (file I/O, git, test execution in sandbox)
- [ ] Context-budget handoff (summarize + fresh agent at <70% confidence)
- [ ] RAG store (Chroma/LanceDB) behind the memory tables
- [ ] Planner agent + task graph; parallel workers
- [ ] Second-provider cross-validator
- [ ] Phase 2: live visualization over the same store
