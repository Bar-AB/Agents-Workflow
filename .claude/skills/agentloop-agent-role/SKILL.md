---
name: agentloop-agent-role
description: Use when adding a new agent role to agentloop's registry (e.g. a planner, a second-provider cross-validator, a specialized worker) — anything that needs an AgentSpec with its own model, system prompt, tools, and context budget, and wiring into a task's worker/validator role.
---

# Register a new agent role

Agents are defined in `agentloop/registry.py` as `AgentSpec`s (role, model,
system prompt, tools, context budget, version). Keeping them in a versioned
registry — code defaults, overridable via `agents.json` — makes agent behavior
reproducible and auditable (spec §3). This is the seam for roadmap #4 (planner
agent) and the second-provider cross-validator.

## The shape

```python
AgentSpec(
    role="planner",
    model="claude-sonnet-5",          # must exist in MODEL_PRICING (config.py)
    system_prompt=PLANNER_SYSTEM,
    tools=["file_io", "search", "task_state"],
    context_budget_tokens=120_000,
    version="1",
)
```

## Checklist

1. **Write the system prompt** as a module constant in `registry.py` (mirror
   `WORKER_SYSTEM` / `VALIDATOR_SYSTEM`). If the role emits machine-parsed
   output, specify the exact format in the prompt — the parser trusts it. For a
   validator-type role, the first line **must** be
   `VERDICT: <approve|revise|escalate> CONFIDENCE: <0-1> TESTS: <pass|fail|na>`
   or `parse_verdict` in `agents.py` escalates at confidence 0.
2. **Add the `AgentSpec` to `DEFAULT_AGENTS`** keyed by role name.
3. **Pick a model that exists in `MODEL_PRICING`** (`config.py`) so cost
   estimates are right — a validator can use a cheaper tier than the worker.
4. **Wire the role to tasks.** Roles are selected via `Task.worker_role` /
   `Task.validator_role` (default `"worker"`/`"validator"`, stored per task in
   the `tasks` table). A brand-new *kind* of agent (e.g. planner) also needs an
   invocation path — add a `run_<role>` wrapper in `agentloop/agents.py`
   following `run_worker`/`run_validator`, which handles attempt/metrics
   bookkeeping via `_invoke` and logs prompt+output events. Then call it from
   `loop.py`.
5. **Prompt building goes in `agents.py`, not the registry.** The registry
   holds the static system prompt; `run_worker`/`run_validator` assemble the
   per-task user prompt. Keep that split.
6. **Regenerate `agents.json` awareness.** `agentloop init-registry` writes
   `DEFAULT_AGENTS` to `agents.json` for editing; it's gitignored, so the code
   defaults are the source of truth. Bump `version` when you change a prompt so
   runs stay traceable.
7. **Add an e2e test** if the role changes loop behavior (see
   `agentloop-loop-test`).

## Notes

- The `tools` list is a declared baseline; real tool execution in the worker is
  roadmap #1 (sandboxed file I/O, git, test running). Until then it's metadata.
- `context_budget_tokens` is where the roadmap #2 context-handoff logic will
  read from — set it deliberately, don't copy blindly.
