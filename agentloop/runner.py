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
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
except ImportError:  # core stays stdlib-only; MockRunner works without it
    anyio = None
    ResultMessage = None


def _is_result_message(message) -> bool:
    """True for the SDK's terminal ResultMessage, which carries run totals.

    Uses isinstance when the class is importable, else falls back to the class
    name so a minor SDK version change doesn't silently break usage capture.
    """
    if ResultMessage is not None:
        return isinstance(message, ResultMessage)
    return type(message).__name__ == "ResultMessage"


def extract_usage(usage: dict) -> tuple[int, int, int, int]:
    """Pull (new input, output, cache write, cache read) from an SDK usage dict.

    Split out and kept pure so the token accounting can be tested without the
    SDK or a live call. The four fields matter independently: the old code read
    only `input_tokens`, which on a cached run reported ~2 while cache holds
    tens of thousands — a multi-thousand-fold undercount that made the budget
    cap measure almost nothing.
    """
    return (
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
        int(usage.get("cache_creation_input_tokens", 0) or 0),
        int(usage.get("cache_read_input_tokens", 0) or 0),
    )


def extract_tool_calls(message) -> list[dict]:
    """Tool uses reported by one SDK message, as `{"tool", "input"}` dicts.

    Duck-typed on the block's class name for the same reason
    `_is_result_message` is: provenance is best-effort telemetry, so an SDK
    version that renames or restructures a block should cost us a record, not a
    run. Inputs are truncated — this is an index of what happened, not a copy
    of every file the agent wrote.
    """
    calls: list[dict] = []
    for block in getattr(message, "content", None) or []:
        if type(block).__name__ != "ToolUseBlock":
            continue
        raw = getattr(block, "input", None)
        calls.append(
            {
                "tool": getattr(block, "name", "unknown"),
                "input": {k: str(v)[:200] for k, v in raw.items()}
                if isinstance(raw, dict)
                else str(raw)[:200],
            }
        )
    return calls


class ModelRunner(Protocol):
    def run(
        self,
        system_prompt: str,
        prompt: str,
        model: str,
        tools: list[str] | None = None,
    ) -> RunResult:
        """Execute one agent invocation and return output + token usage.

        `tools` is the agent's allowlist from the registry (spec §3): the
        shared baseline plus its role-specific tools.
        """
        ...


class MockRunner:
    """Scripted runner for tests/dry runs. Feed it a list of outputs; each
    call pops the next one. Records prompts for assertions.

    A scripted item that is an exception instance is *raised* instead of
    returned — this simulates a transient infra failure (API 5xx, network blip)
    so the loop's retry/escalation path can be tested without a live provider.
    A scripted item that is already a `RunResult` is returned as-is, so a test
    can script tool calls or specific token counts without the SDK.

    **Single-threaded only.** The script is positional, so with
    `max_parallel_workers > 1` whichever thread calls first gets whatever output
    is next, and a test would end up asserting an ordering it created itself.
    For concurrent tests route on prompt *content* instead (see `GraphRunner` in
    tests/test_planner.py); `agentloop run --runner mock` with parallelism on is
    likewise nondeterministic by construction.
    """

    def __init__(self, outputs: list | None = None):
        self.outputs = list(outputs or [])
        self.calls: list[dict] = []

    def run(
        self,
        system_prompt: str,
        prompt: str,
        model: str,
        tools: list[str] | None = None,
    ) -> RunResult:
        self.calls.append(
            {
                "system": system_prompt,
                "prompt": prompt,
                "model": model,
                "tools": list(tools or []),
            }
        )
        output = self.outputs.pop(0) if self.outputs else "(mock output)"
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, RunResult):
            return output
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
    # Read without write, for roles that survey a codebase but must not change
    # it (the planner proposes work; only workers produce output a validator
    # reviews). `file_io` would hand those roles Write and Edit as well.
    "file_read": ["Read"],
    "search": ["Glob", "Grep"],
    "git": ["Bash"],
    "shell": ["Bash"],
    "task_state": [],  # served in-process via the store, not an SDK tool
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

    def run(
        self,
        system_prompt: str,
        prompt: str,
        model: str,
        tools: list[str] | None = None,
    ) -> RunResult:
        if anyio is None:
            raise RuntimeError(
                "ClaudeSDKRunner requires `pip install agentloop[claude]`"
            )
        return anyio.run(self._run_async, system_prompt, prompt, model, tools)

    def build_options(self, system_prompt: str, model: str, tools: list[str] | None):
        """Construct SDK options. Split out so the tool allowlist is testable
        without credentials or a live call."""
        allowed = resolve_tools(tools)
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            max_turns=25,
            allowed_tools=allowed,
        )

    async def _run_async(
        self,
        system_prompt: str,
        prompt: str,
        model: str,
        tools: list[str] | None = None,
    ) -> RunResult:
        options = self.build_options(system_prompt, model, tools)
        chunks: list[str] = []
        tool_calls: list[dict] = []
        tokens_in = tokens_out = cache_creation = cache_read = 0
        async for message in query(prompt=prompt, options=options):
            text = getattr(message, "result", None)
            if isinstance(text, str):
                chunks.append(text)
            # Tool uses accumulate across the stream (unlike usage, which is a
            # running total on the terminal message): each block is one call.
            tool_calls.extend(extract_tool_calls(message))
            # Usage comes from the terminal ResultMessage ONLY: its `usage` is
            # already the whole-run total, so reading it from every message and
            # summing (the old bug) double-counts. Assign, never accumulate.
            if _is_result_message(message):
                usage = getattr(message, "usage", None)
                if isinstance(usage, dict):
                    (tokens_in, tokens_out, cache_creation, cache_read) = extract_usage(
                        usage
                    )
        return RunResult(
            output="\n".join(chunks),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            model=model,
            tool_calls=tool_calls,
        )


def get_runner(name: str) -> ModelRunner:
    if name == "mock":
        return MockRunner()
    if name == "claude":
        return ClaudeSDKRunner()
    raise ValueError(f"Unknown runner: {name!r} (expected 'claude' or 'mock')")
