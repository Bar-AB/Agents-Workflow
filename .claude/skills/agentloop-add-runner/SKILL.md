---
name: agentloop-add-runner
description: Use when adding a new model or provider backend to agentloop — litellm, OpenAI, a second-provider or Codex cross-validator, or any non-Claude runner that plugs into the ModelRunner seam in runner.py. Triggers include a new --runner choice, provider token/cost wiring, or MODEL_PRICING and budget-cap changes for a new backend.
---

# Add a ModelRunner backend

The loop never imports a vendor SDK directly — it only knows the `ModelRunner`
protocol (`agentloop/runner.py`). A new provider is a new implementation of
`run()`, nothing more. This is the seam that keeps agentloop open-sourceable
and model-agnostic (roadmap #5: second-provider cross-validator).

## The contract

```python
class ModelRunner(Protocol):
    def run(self, system_prompt: str, prompt: str, model: str) -> RunResult: ...
```

`run()` executes exactly one agent invocation and returns a `RunResult`
(`agentloop/models.py`): `output`, `tokens_in`, `tokens_out`, `model`. That is
the entire surface. The loop handles prompts, verdicts, retries, and budgets;
your runner only turns (system, prompt, model) into text + token usage.

## Checklist

Create a todo per item.

1. **Implement the class in `agentloop/runner.py`.** Follow `ClaudeSDKRunner`
   as the template. Keep the vendor SDK import *inside* `run()` (a local
   import), so the core stays stdlib-only and the dependency is optional.
2. **Return real token counts.** `tokens_in`/`tokens_out` feed `attempts`
   metrics and the budget caps in `loop._budget_tripped`. If the provider
   doesn't report usage, estimate conservatively — never return 0, or budget
   caps silently stop protecting you. Set `RunResult.model` to the string that
   matches a `MODEL_PRICING` key (next step).
3. **Register in `get_runner(name)`** at the bottom of `runner.py`, and add the
   name to the `choices=[...]` list for `--runner` in `agentloop/cli.py`.
4. **Add pricing to `MODEL_PRICING` in `agentloop/config.py`** for every model
   id your runner emits, as `(input $/1M, output $/1M)`. `estimate_cost_usd`
   falls back to `DEFAULT_PRICING` for unknown models — a silent wrong-cost
   trap. Match the exact model string your runner puts in `RunResult.model`.
5. **Declare the optional dependency** as an extra in `pyproject.toml` (mirror
   the `claude` extra), so `pip install -e ".[dev]"` stays dependency-free.
6. **Test through MockRunner parity, not the live API.** Do NOT add a test that
   hits the network. Instead verify your runner satisfies the protocol shape
   (returns a `RunResult` with non-zero tokens and the right `model`). The
   loop's behavior is already covered by the MockRunner e2e suite; see
   `agentloop-loop-test`.

## Cross-validator note (roadmap #5)

A second-provider validator is just a runner whose model is wired to
`Task.validator_role`'s `AgentSpec.model` (see `agentloop-agent-role`). The
validator must still return the parseable first line
(`VERDICT: … CONFIDENCE: … TESTS: …`) — that contract lives in the validator
system prompt, not the runner. The runner is provider-agnostic; the format is
enforced by `parse_verdict` in `agentloop/agents.py`.

## Common mistakes

- Importing the SDK at module top level → breaks the stdlib-only core.
- Returning `tokens_*` as 0 → budget caps never trip; cost metrics read $0.00.
- `RunResult.model` not matching a `MODEL_PRICING` key → cost silently wrong.
- Forgetting the `cli.py` `choices` list → `--runner yours` errors out.
