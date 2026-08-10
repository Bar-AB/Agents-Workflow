"""ModelRunner — the provider seam.

The loop never talks to a model vendor directly; it talks to a ModelRunner.
Default backend is the Claude Agent SDK. This seam is what keeps the project
open-sourceable and multi-provider, and slice 4 is it being used for what it was
built for: `OpenAICompatRunner` is a second provider in the same shape, so a
validator can review a worker's output on a different model family than produced
it. Which backend serves which role is a registry decision
(`AgentSpec.runner`), resolved in `loop.py` — this module knows how to call a
provider, never which one a role should use.

Every vendor import and every outbound HTTP call in the package lives in this
file; a test walks the package and fails if that stops being true.

MockRunner powers tests and dry runs — no API keys, zero cost.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Protocol

from .models import RunResult


class RunnerConfigError(RuntimeError):
    """A permanent, operator-fixable problem with how a backend is configured:
    a missing API key, a base_url that cannot carry a bearer token safely, a
    model the endpoint refuses (401/403/404), a malformed request (400).

    Distinct from every other exception a runner raises, because `loop.py` runs
    `run()` *inside* `_with_retry`: without this type a revoked key is retried
    with backoff, burns the clock reaching the same conclusion, and escalates as
    `infra_error` — pointing the human at the network instead of at their key.
    `Loop._runner_for`/`_with_retry` map it to `_ConfigError`, which escalates to
    NEEDS_HUMAN with no retry and no `infra_error` event.

    A `RuntimeError` subclass so the pre-existing "missing key raises
    RuntimeError" contract (and its test) still holds.
    """


class ProviderResponseError(RuntimeError):
    """The provider answered, but not with a usable completion.

    Deliberately *not* a `RunnerConfigError`: an error envelope returned under
    HTTP 200 (what OpenRouter/vLLM/Azure gateways do for quota and policy
    failures), a null `content`, or a `finish_reason` of `length` are all things
    that can clear on a retry, so they stay on the transient path. What they
    must never do is become `output=""`: an empty worker output flows down the
    ordinary success path — `loop.py` only special-cases `ESCALATE:` — and a
    blank string can reach DONE looking like work.
    """


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


def extract_openai_usage(usage: dict) -> tuple[int, int, int, int]:
    """Pull (new input, output, cache write, cache read) from an OpenAI usage dict.

    Pure and separately testable for the same reason `extract_usage` is: this is
    the part that breaks when a provider changes its schema, and it is what the
    budget cap ultimately measures.

    The trap is that OpenAI's `prompt_tokens` is **inclusive** of the cached
    share, while Anthropic reports fresh input and cache reads as disjoint
    numbers — and `RunResult.tokens_in` is defined as *new* input only. Adding
    `cached_tokens` on top of `prompt_tokens` would therefore bill the cached
    portion twice, at the full input rate on top of the discounted one. The
    cached share is subtracted out here instead, clamped at zero so a provider
    reporting more cache than prompt can never produce a negative token count —
    that would *reduce* a task's measured spend and pull it back under a cap it
    had already passed.

    Cache *writes* are reported as 0: OpenAI's prompt caching is automatic and
    carries no write charge, so there is no such number to report. Reporting the
    prompt as a cache write instead would invent spend that never happened.

    Every field is read through `_int_or_zero`, so this function is **total**:
    no input dict makes it raise. That is not defensiveness for its own sake.
    `agents._invoke` calls `runner.run()` outside any transaction and only
    reaches `finish_attempt` on a clean return, so a raise here discards the
    tokens and cost of a completion the provider has already billed — and
    `loop.py`'s bare `except Exception` then classifies the deterministic parse
    failure as transient and pays for the same completion again. Exactly the
    defect `_tool_name_repr` documents, one layer earlier.
    """
    prompt = _int_or_zero(usage.get("prompt_tokens"))
    completion = _int_or_zero(usage.get("completion_tokens"))
    if prompt == 0:
        # Some providers report only the total. Ignoring it recorded 0 input
        # against a multi-KB prompt, and input is the dominant cost on a
        # validator call, so the budget cap measured almost nothing.
        total = _int_or_zero(usage.get("total_tokens"))
        prompt = max(0, total - completion)
    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = _int_or_zero(details.get("cached_tokens"))
    cached = max(0, min(cached, prompt))
    return (prompt - cached, completion, 0, cached)


def _int_or_zero(value) -> int:
    """A usage field as a non-negative int, or 0 for anything that is not one.

    `None` and a missing key were already handled; a *type* was not — `'n/a'`
    raised ValueError, a nested dict raised TypeError, and both landed after the
    completion was paid for (see `extract_openai_usage`). A wrong number here is
    caught by the never-zero guard in `run`, which estimates instead; a raise
    was not caught by anything that could keep the reply.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):  # inf / nan
        return 0


def extract_openai_tool_calls(message: dict) -> list[dict]:
    """Tool uses on one OpenAI assistant message, as `{"tool", "input"}` dicts.

    Same contract as `extract_tool_calls`: report a call only when the response
    actually carries one, and degrade to "nothing recorded" rather than to a
    wrong record. Arguments arrive as a JSON *string* and are kept as one —
    `agents._tool_input_repr` bounds it before it reaches an event.

    **`executed=False`, always, and that is not a formality.** CLAUDE.md's rule
    is "every tool an agent *actually invokes* logs a `tool_call` event", and
    this backend invokes none: it sends no `tools` parameter and has no
    execution loop, so anything here is a call the model *asked for* and nobody
    ran. Recording it identically to an executed SDK call would put two
    different facts under one name, and slice 5's auto-approval policy reads
    exactly this record — a policy that cannot tell a request from an execution
    would approve on evidence of neither.
    """
    calls: list[dict] = []
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return calls
    for call in raw:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        fn = fn if isinstance(fn, dict) else {}
        calls.append(
            {
                "tool": fn.get("name", "unknown"),
                "input": str(fn.get("arguments", ""))[:200],
                "executed": False,
            }
        )
    return calls


def _estimate_tokens(system_prompt: str, prompt: str, output: str) -> tuple[int, int]:
    """A deliberately rough token estimate, used only when usage is missing.

    ~4 chars per token is the standard back-of-envelope for English text, and
    the point here is not accuracy but that the number is *non-zero*: the budget
    cap and the context-handoff measure both read these fields, and both stop
    working entirely at 0. Rounded up so an estimate never under-reports a real
    attempt into fitting under a cap it actually exceeded.
    """
    chars_in = len(system_prompt) + len(prompt)
    return (max(1, -(-chars_in // 4)), max(1, -(-len(output) // 4)))


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

        **An implementation must hold no per-call state.** `Loop._runner_for`
        resolves one backend instance per pinned name and hands that same object
        to every role and every thread, so with `max_parallel_workers > 1` a
        backend is called concurrently. Anything a call needs belongs in a
        local; anything on `self` must be immutable after construction (a
        base_url, a timeout, an opener) or synchronised by the backend itself.
        `MockRunner` deliberately does not meet this bar — its script is
        positional — which is why it documents itself single-threaded-only.
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


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
# How much of a provider's error body is worth keeping. Enough for a JSON
# `{"error": {"message": ...}}`, bounded because it lands in an event payload.
_MAX_ERROR_BODY_CHARS = 600
# Codes worth another round trip: a timeout, a rate limit, and anything the
# server blames on itself. Everything else in 4xx is the request or the
# credentials, which the next identical request will not fix.
_RETRYABLE_STATUS = {408, 409, 425, 429}


def _check_base_url(base_url: str) -> None:
    """Refuse a base_url that cannot carry a bearer token safely.

    `base_url` is operator config rather than model output, so this is not a
    live exploit — but this project scrubs `ANTHROPIC_API_KEY` out of the
    sandbox by construction rather than by trusting the code that runs there,
    and the same care belongs on the wire. Plaintext to a loopback address never
    leaves the machine (ollama, vLLM and a test double all live there), so that
    one case is allowed and nothing else is.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "") in _LOCAL_HOSTS:
        return
    raise RunnerConfigError(
        f"base_url {base_url!r} must be https (the API key travels in an "
        f"Authorization header on every request); plain http is accepted only "
        f"for a loopback host such as http://localhost:8000/v1."
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the Authorization header cannot follow one.

    Python's stock `HTTPRedirectHandler` strips only `Content-Length` and
    `Content-Type` from a redirected request and forwards everything else,
    `Authorization` included, across hosts. A redirecting completions endpoint
    is a misconfiguration either way, so failing is both safer and more
    informative than silently re-POSTing the key somewhere else.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RunnerConfigError(
            f"refusing to follow an HTTP {code} redirect from {req.full_url} to "
            f"{newurl}: the request carries a bearer token, and urllib would "
            f"forward it to the new host. Point base_url at the final endpoint."
        )


def _error_body(exc: urllib.error.HTTPError) -> str:
    """The provider's own explanation, bounded, never raising.

    The body is where `model_not_found`, `insufficient_quota` and
    `invalid_api_key` actually say which of them happened; `str(HTTPError)` is
    only the status line. Reading it can fail (the stream may be closed), and a
    failure here must not replace a useful diagnosis with a traceback.
    """
    try:
        raw = exc.read()
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw).strip()[:_MAX_ERROR_BODY_CHARS]


def _classify_http_error(exc: urllib.error.HTTPError, base_url: str) -> Exception:
    """Turn an HTTPError into a retryable error or a permanent config error.

    A 400/401/403/404 is the request, the key or the model — none of which the
    next identical request changes — so it becomes a `RunnerConfigError` that
    escalates straight to NEEDS_HUMAN. 408/429/5xx are the provider's own
    transient states and stay on the retry path. Either way the body travels
    with the message, because that is the only place the provider says why.
    """
    code = int(getattr(exc, "code", 0) or 0)
    body = _error_body(exc)
    detail = f" — {body}" if body else ""
    message = f"{base_url} returned HTTP {code} ({exc.reason}){detail}"
    if code in _RETRYABLE_STATUS or code >= 500:
        return ProviderResponseError(message)
    if 400 <= code < 500:
        return RunnerConfigError(
            f"{message}. This is a configuration error (the request, the API key "
            f"or the model), not a transient failure — check the pinned model "
            f"and credentials in agents.json rather than retrying."
        )
    return ProviderResponseError(message)


# `stop` is a normal completion and `tool_calls` is one that ended to call a
# tool. Everything else — notably `length` (truncated) and `content_filter` —
# means the text in hand is not the answer the agent meant to give.
_OK_FINISH_REASONS = {"stop", "tool_calls", "function_call"}


def _extract_message(data, base_url: str, model: str) -> tuple[dict, str]:
    """The assistant message and its text, or a raise.

    Total in the sense that it never propagates a surprise as an
    `AttributeError` from somewhere deep — but it *does* raise deliberately, for
    the three shapes that look like a completion and are not:

    - an `error` envelope under HTTP 200 (what OpenRouter/vLLM/Azure gateways
      return for quota and policy failures),
    - a null / non-string `content`,
    - a `finish_reason` outside the normal set, `length` above all: a truncated
      reply is a partial answer presented as a whole one.

    All three used to become `output=""`, and an empty worker output is not
    caught anywhere downstream: `loop.py` special-cases `ESCALATE:` and nothing
    else, so a blank string becomes `task.output` and can reach DONE.
    """
    if not isinstance(data, dict):
        raise ProviderResponseError(
            f"{base_url} returned {type(data).__name__}, not a JSON object, for "
            f"model {model!r}."
        )
    error = data.get("error")
    if error:
        raise ProviderResponseError(
            f"{base_url} returned an error envelope under HTTP 200 for model "
            f"{model!r}: {str(error)[:_MAX_ERROR_BODY_CHARS]}"
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderResponseError(
            f"{base_url} returned no choices for model {model!r}."
        )
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish = choice.get("finish_reason")
    if finish is not None and finish not in _OK_FINISH_REASONS:
        raise ProviderResponseError(
            f"{base_url} ended the completion for model {model!r} with "
            f"finish_reason={finish!r}; the reply is not a whole answer."
        )
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderResponseError(
            f"{base_url} returned a completion with no content for model "
            f"{model!r}; an empty output would flow down the ordinary success "
            f"path as though the agent had answered."
        )
    return message, content


class OpenAICompatRunner:
    """OpenAI-compatible /v1/chat/completions backend, over stdlib urllib.

    The second provider (roadmap slice 4), and the point of the seam: a
    validator running here reviews a worker's output on a different model family
    than produced it, so a model cannot rubber-stamp its own work.

    **Deliberately not an SDK.** The core is stdlib-only, and `retrieval.py`
    already established what an optional, CI-untested provider path is worth
    (its `ChromaBackend` was removed for being exactly that). A stdlib runner is
    fully exercisable against a canned response, which matters most for the
    usage parsing — that is what the budget cap ultimately measures, and it is
    the first thing a provider schema change breaks. It also makes "second
    provider" mean "second base_url": OpenAI, an Azure/OpenRouter/vLLM
    deployment or a local endpoint are all this one class.

    The API key is read from the environment at call time and used for exactly
    one thing — the Authorization header. It is never returned, logged, or put
    in a payload the audit log can see.

    `tools` is accepted and **ignored**: a chat-completions call has no
    tool-execution loop to hand an allowlist to, so a role pinned here reviews
    what is in its prompt rather than reading the workspace itself. Passing the
    names through as a `tools` parameter would be worse than ignoring them — the
    model would emit calls nobody executes and then reason as if they had run.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        timeout_s: float = 300.0,
    ):
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "").rstrip(
            "/"
        ) or "https://api.openai.com/v1"
        _check_base_url(self.base_url)
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        # Its own opener, not the module-level `urlopen`, so this backend owns
        # its redirect policy (see `_NoRedirect`). One per instance, built once,
        # and `_runner_for` guarantees one instance per pinned name.
        self._opener = urllib.request.build_opener(_NoRedirect())

    def run(
        self,
        system_prompt: str,
        prompt: str,
        model: str,
        tools: list[str] | None = None,
    ) -> RunResult:
        # Checked before the request, so a missing key is a configuration error
        # with a name in it rather than a provider 401. A `RunnerConfigError`
        # and not a bare RuntimeError because `run()` is called *inside*
        # `_with_retry`: as an ordinary exception this was retried with backoff
        # and escalated as `infra_error`, which is the very outcome the check
        # was written to avoid.
        if not os.environ.get(self.api_key_env):
            raise RunnerConfigError(
                f"OpenAICompatRunner requires the {self.api_key_env} environment "
                f"variable (the API key for {self.base_url})."
            )
        if tools:
            # Degrade loudly, the way `sandbox_isolation='strict'` degrades to
            # env-scrub when no container tier is wired: the run continues, but
            # the capability gap is stated at the moment it opens rather than
            # left to be inferred from a verdict that reviewed less than the
            # operator thinks it did. `warnings.warn` and not `print`, because
            # that precedent (executor.py) is a warning: stdout belongs to the
            # CLI's structured output, and a printed line is invisible to `-W`,
            # uncapturable by `pytest.warns`, and repeats on every attempt of
            # every task instead of deduping.
            warnings.warn(
                f"OpenAICompatRunner cannot execute tools; dropping "
                f"{', '.join(tools)} for model {model!r}. A role pinned to this "
                f"backend reviews what is in its prompt and cannot read the "
                f"workspace itself.",
                RuntimeWarning,
                stacklevel=2,
            )
        payload = {
            "model": model,
            # The system prompt stays a system message rather than being glued
            # to the front of the user prompt: the registry's role prompts are
            # written as instructions, and folding them into user text makes
            # them look like content the agent may negotiate with.
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        data = self._post(payload)
        # Content first, and it *may* raise: a reply with no usable completion
        # is worth nothing, and returning `output=""` for it is worse than
        # raising, because an empty worker output flows down the ordinary
        # success path (`loop.py` special-cases only `ESCALATE:`).
        message, output = _extract_message(data, self.base_url, model)

        # Usage second, and it may *not* raise: by this point the completion is
        # in hand and already billed. Any schema surprise degrades to the
        # estimate path instead of discarding it and re-paying via `_with_retry`.
        try:
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict) and usage:
                tokens_in, tokens_out, cache_creation, cache_read = (
                    extract_openai_usage(usage)
                )
            else:
                tokens_in, tokens_out, cache_creation, cache_read = 0, 0, 0, 0
        except Exception as exc:  # a provider schema change, not a bad run
            tokens_in, tokens_out, cache_creation, cache_read = 0, 0, 0, 0
            reason = f"usage could not be parsed ({type(exc).__name__}: {exc})"
        else:
            reason = "no usage was reported"

        # Never a silent zero, and checked **per field**: an attempt costing
        # $0.00 does not trip a budget cap and does not move the context-handoff
        # measure, so a provider omitting one field would quietly disable both.
        # `tokens_in == 0` alone is not evidence of that, though — a fully
        # cached prompt legitimately reports it with `cache_read > 0` — so the
        # input is only estimated when *both* are zero.
        est_in, est_out = _estimate_tokens(system_prompt, prompt, output)
        estimated: list[str] = []
        if tokens_in == 0 and cache_read == 0:
            tokens_in = est_in
            estimated.append("input")
        if tokens_out == 0:
            tokens_out = est_out
            estimated.append("output")
        note = ""
        if estimated:
            note = (
                f"{self.base_url}: {reason} for model {model!r}; estimated "
                f"{' and '.join(estimated)} tokens (~{tokens_in} in / "
                f"{tokens_out} out) so the budget cap still measures this attempt."
            )
            warnings.warn(note, RuntimeWarning, stacklevel=2)

        return RunResult(
            output=output,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            # The *serving* model, not the requested one: providers alias and
            # substitute, and cost must be attributed to what actually ran. It
            # is the dated snapshot id, which `config.pricing_key` normalizes at
            # the pricing boundary — provenance is not discarded to make a
            # lookup work.
            model=str(data.get("model") or model) if isinstance(data, dict) else model,
            tool_calls=extract_openai_tool_calls(message),
            # Carried so `agents._invoke` can log a `runner_warning` event: a
            # warning nobody sees in `agentloop events` is unrecorded, and the
            # estimate then reaches `attempts` indistinguishable from a
            # provider-measured number.
            usage_estimated=bool(estimated),
            notes=note,
        )

    def _post(self, payload: dict) -> dict:
        """One HTTP round trip. Split out so `run` is testable without a socket.

        The request goes through `self._opener`, not `urlopen`, so the bearer
        token cannot follow a redirect (see `_NoRedirect`). An `HTTPError` is
        classified rather than propagated raw: unclassified, a revoked key, a
        `model_not_found` and a transient 503 all reached the audit log as
        `HTTPError: HTTP Error 4xx: <reason>` after three paid round trips,
        indistinguishable from one another and with the provider's own
        explanation — which is in the response *body* — thrown away.
        """
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ[self.api_key_env]}",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc, self.base_url) from exc


def get_runner(name: str) -> ModelRunner:
    if name == "mock":
        return MockRunner()
    if name == "claude":
        return ClaudeSDKRunner()
    if name == "openai":
        return OpenAICompatRunner()
    raise ValueError(
        f"Unknown runner: {name!r} (expected 'claude', 'openai' or 'mock')"
    )
