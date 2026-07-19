"""ModelRunner — the provider seam.

The loop never talks to a model vendor directly; it talks to a ModelRunner.
Default backend is the Claude Agent SDK. This seam is what keeps the project
open-sourceable and multi-provider: a litellm/OpenAI backend (e.g. the bonus
Codex cross-validator, spec §5) is just another implementation of `run()`.

MockRunner powers tests and dry runs — no API keys, zero cost.
"""

from __future__ import annotations

from typing import Protocol

from .models import RunResult


class ModelRunner(Protocol):
    def run(self, system_prompt: str, prompt: str, model: str) -> RunResult:
        """Execute one agent invocation and return output + token usage."""
        ...


class MockRunner:
    """Scripted runner for tests/dry runs. Feed it a list of outputs; each
    call pops the next one. Records prompts for assertions."""

    def __init__(self, outputs: list[str] | None = None):
        self.outputs = list(outputs or [])
        self.calls: list[dict] = []

    def run(self, system_prompt: str, prompt: str, model: str) -> RunResult:
        self.calls.append(
            {"system": system_prompt, "prompt": prompt, "model": model})
        output = self.outputs.pop(0) if self.outputs else "(mock output)"
        return RunResult(
            output=output,
            tokens_in=len(system_prompt.split()) + len(prompt.split()),
            tokens_out=len(output.split()),
            model="mock",
        )


class ClaudeSDKRunner:
    """Claude Agent SDK backend. Requires `pip install agentloop[claude]` and
    Anthropic credentials (ANTHROPIC_API_KEY or Claude Code auth)."""

    def run(self, system_prompt: str, prompt: str, model: str) -> RunResult:
        import anyio

        return anyio.run(self._run_async, system_prompt, prompt, model)

    async def _run_async(self, system_prompt: str, prompt: str,
                         model: str) -> RunResult:
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            max_turns=25,
        )
        chunks: list[str] = []
        tokens_in = tokens_out = 0
        async for message in query(prompt=prompt, options=options):
            # Collect assistant text; pull usage off the result message.
            text = getattr(message, "result", None)
            if isinstance(text, str):
                chunks.append(text)
            usage = getattr(message, "usage", None)
            if isinstance(usage, dict):
                tokens_in += usage.get("input_tokens", 0)
                tokens_out += usage.get("output_tokens", 0)
        return RunResult(output="\n".join(chunks), tokens_in=tokens_in,
                         tokens_out=tokens_out, model=model)


def get_runner(name: str) -> ModelRunner:
    if name == "mock":
        return MockRunner()
    if name == "claude":
        return ClaudeSDKRunner()
    raise ValueError(f"Unknown runner: {name!r} (expected 'claude' or 'mock')")
