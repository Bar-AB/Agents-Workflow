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

try:  # optional extra: `pip install agentloop[claude]`
    import anyio
    from claude_agent_sdk import ClaudeAgentOptions, query
except ImportError:  # core stays stdlib-only; MockRunner works without it
    anyio = None


class ModelRunner(Protocol):
    def run(self, system_prompt: str, prompt: str, model: str,
            tools: list[str] | None = None) -> RunResult:
        """Execute one agent invocation and return output + token usage.

        `tools` is the agent's allowlist from the registry (spec §3): the
        shared baseline plus its role-specific tools.
        """
        ...


class MockRunner:
    """Scripted runner for tests/dry runs. Feed it a list of outputs; each
    call pops the next one. Records prompts for assertions."""

    def __init__(self, outputs: list[str] | None = None):
        self.outputs = list(outputs or [])
        self.calls: list[dict] = []

    def run(self, system_prompt: str, prompt: str, model: str,
            tools: list[str] | None = None) -> RunResult:
        self.calls.append({"system": system_prompt, "prompt": prompt,
                           "model": model, "tools": list(tools or [])})
        output = self.outputs.pop(0) if self.outputs else "(mock output)"
        return RunResult(
            output=output,
            tokens_in=len(system_prompt.split()) + len(prompt.split()),
            tokens_out=len(output.split()),
            model="mock",
        )


# The registry names tools logically so it stays provider-neutral (spec §3);
# translating to concrete vendor tool names is the seam's job, not the
# registry's. An unknown logical name maps to nothing rather than being passed
# through blind — an agent silently gaining an unintended tool is worse than
# one missing a tool it asked for.
LOGICAL_TOOL_MAP: dict[str, list[str]] = {
    "file_io": ["Read", "Write", "Edit"],
    "search": ["Glob", "Grep"],
    "git": ["Bash"],
    "shell": ["Bash"],
    "task_state": [],      # served in-process via the store, not an SDK tool
    "web": ["WebFetch", "WebSearch"],
}


def resolve_tools(logical: list[str] | None) -> list[str]:
    """Map registry tool names to concrete SDK tool names, de-duplicated."""
    resolved: list[str] = []
    for name in logical or []:
        for concrete in LOGICAL_TOOL_MAP.get(name, []):
            if concrete not in resolved:
                resolved.append(concrete)
    return resolved


class ClaudeSDKRunner:
    """Claude Agent SDK backend. Requires `pip install agentloop[claude]` and
    Anthropic credentials (ANTHROPIC_API_KEY or Claude Code auth)."""

    def run(self, system_prompt: str, prompt: str, model: str,
            tools: list[str] | None = None) -> RunResult:
        if anyio is None:
            raise RuntimeError(
                "ClaudeSDKRunner requires `pip install agentloop[claude]`")
        return anyio.run(self._run_async, system_prompt, prompt, model, tools)

    def build_options(self, system_prompt: str, model: str,
                      tools: list[str] | None):
        """Construct SDK options. Split out so the tool allowlist is testable
        without credentials or a live call."""
        allowed = resolve_tools(tools)
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            max_turns=25,
            allowed_tools=allowed,
        )

    async def _run_async(self, system_prompt: str, prompt: str, model: str,
                         tools: list[str] | None = None) -> RunResult:
        options = self.build_options(system_prompt, model, tools)
        chunks: list[str] = []
        tokens_in = tokens_out = 0
        async for message in query(prompt=prompt, options=options):
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
