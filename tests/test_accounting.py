"""Token/cost accounting: cache tokens are captured, priced, and counted
toward the budget caps (the live defect this slice fixes)."""

from pathlib import Path

import pytest

from agentloop.config import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MODEL_PRICING,
    estimate_cost_usd,
)
from agentloop.loop import Loop
from agentloop.models import RunResult, Task, TaskStatus
from agentloop.registry import Registry
from agentloop import runner
from agentloop.runner import MockRunner, extract_usage
from agentloop.store import Store
from tests.test_loop import APPROVE, REVISE  # reuse scripted verdicts


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


# -- extract_usage: the field the old code dropped -------------------------


def test_extract_usage_reads_all_four_fields():
    usage = {
        "input_tokens": 2,
        "output_tokens": 50,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 20000,
    }
    assert extract_usage(usage) == (2, 50, 1000, 20000)


def test_extract_usage_tolerates_missing_and_none():
    assert extract_usage({}) == (0, 0, 0, 0)
    assert extract_usage({"input_tokens": None}) == (0, 0, 0, 0)


# -- pricing ----------------------------------------------------------------


def test_cache_is_priced_off_the_input_rate():
    pin, _ = MODEL_PRICING["claude-sonnet-5"]
    write = estimate_cost_usd("claude-sonnet-5", 0, 0, cache_creation_tokens=1_000_000)
    read = estimate_cost_usd("claude-sonnet-5", 0, 0, cache_read_tokens=1_000_000)
    assert write == pytest.approx(pin * CACHE_WRITE_MULTIPLIER)
    assert read == pytest.approx(pin * CACHE_READ_MULTIPLIER)


def test_cost_signature_backcompat_keeps_mock_free():
    # Old three-arg call site still works, and the zero-priced mock model is $0.
    assert estimate_cost_usd("mock", 5000, 5000) == 0.0
    assert estimate_cost_usd("mock", 0, 0, 9999, 9999) == 0.0


def test_mock_runner_reports_no_cache():
    r = MockRunner(["hi"]).run("sys", "prompt", "mock")
    assert r.cache_creation_tokens == 0 and r.cache_read_tokens == 0


# -- end to end: a cache-heavy run trips the cap ----------------------------


class CacheHeavyRunner:
    """Returns a real-priced run dominated by cache-read tokens — the shape of
    the run that the old accounting under-counted ~5,000x."""

    def __init__(self, outputs, cache_read=0, cache_creation=0):
        self.outputs = list(outputs)
        self.cache_read = cache_read
        self.cache_creation = cache_creation
        self.calls = []

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        self.calls.append({"prompt": prompt})
        out = self.outputs.pop(0) if self.outputs else "(out)"
        return RunResult(
            output=out,
            tokens_in=2,
            tokens_out=5,
            cache_creation_tokens=self.cache_creation,
            cache_read_tokens=self.cache_read,
            model="claude-sonnet-5",
        )


def test_cache_heavy_run_trips_cost_cap(store):
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    store.add_task(task)
    from agentloop.config import LoopConfig

    cfg = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(Path(store.db_path).parent / "ws"),
        allow_test_exec=False,
        vcs_enabled=False,  # slice 6: no git subprocess in a test about money
        max_cost_usd_per_task=0.01,
    )  # tiny cost cap
    # ~21k cache-read tokens at sonnet's 0.10x input rate; two attempts clear
    # $0.01. The cap is re-checked at the next iteration boundary after a
    # revision, which is where a cache-heavy run now (correctly) trips.
    runner = CacheHeavyRunner(["out", REVISE, "out2", APPROVE], cache_read=21_000)
    loop = Loop(store, runner, Registry.load(), cfg)
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "Budget cap" in task.escalation_reason
    _, cost = store.task_spend(task.id)
    assert cost > 0.01


def test_cache_tokens_count_toward_token_cap(store):
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    store.add_task(task)
    from agentloop.config import LoopConfig

    cfg = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(Path(store.db_path).parent / "ws"),
        allow_test_exec=False,
        vcs_enabled=False,  # slice 6: no git subprocess in a test about money
        max_cost_usd_per_task=1e9,  # cost cap out of the way
        max_tokens_per_task=1000,
    )  # token cap is the gate
    runner = CacheHeavyRunner(["out", REVISE, "out2", APPROVE], cache_read=5000)
    loop = Loop(store, runner, Registry.load(), cfg)
    loop.run_task(task)

    tokens, _ = store.task_spend(task.id)
    assert tokens >= 5000  # cache tokens are in the total
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "Budget cap" in task.escalation_reason


def test_cache_breakdown_persisted_and_rolled_up(store):
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    store.add_task(task)
    from agentloop.config import LoopConfig

    cfg = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(Path(store.db_path).parent / "ws"),
        allow_test_exec=False,
        vcs_enabled=False,  # slice 6: no git subprocess in a test about money
        max_tokens_per_task=10**9,
        max_cost_usd_per_task=10**9,
    )
    runner = CacheHeavyRunner(["out", APPROVE], cache_read=7000, cache_creation=300)
    Loop(store, runner, Registry.load(), cfg).run_task(task)

    m = store.run_metrics()
    assert m["cache_read_tokens"] == 14000  # worker + validator, 7000 each
    assert m["cache_creation_tokens"] == 600
    assert m["cost_usd"] > 0


# ---------------------------------------------------------------------------
# Slice 8: the seam's two halves must be equally total.
#
# Every totality guard in this module was written for slice 4's
# `OpenAICompatRunner` and never back-fitted to the *default* `ClaudeSDKRunner`.
# That asymmetry is a money defect, not a tidiness one: `agents._invoke` calls
# `runner.run()` outside any transaction and reaches `finish_attempt` only on a
# clean return, so a raise here discards a completion the provider already
# billed — and `loop._with_retry`'s bare `except Exception` then buys it again,
# `infra_max_retries + 1` times, reported as an `infra_error` pointing the
# operator at their network.
#
# Parameterised over BOTH extractors on purpose: a third backend inherits the
# test, and neither half can drift without the other noticing.
# ---------------------------------------------------------------------------

BAD_USAGE_VALUES = [
    "n/a",
    "12345",
    {"total": 5},
    [1, 2],
    float("nan"),
    float("inf"),
    True,
    None,
    object(),
]


@pytest.mark.parametrize(
    "extractor, field",
    [
        (runner.extract_usage, "input_tokens"),
        (runner.extract_usage, "output_tokens"),
        (runner.extract_usage, "cache_creation_input_tokens"),
        (runner.extract_usage, "cache_read_input_tokens"),
        (runner.extract_openai_usage, "prompt_tokens"),
        (runner.extract_openai_usage, "completion_tokens"),
    ],
)
@pytest.mark.parametrize("bad", BAD_USAGE_VALUES)
def test_both_usage_extractors_are_total_over_the_same_bad_input_matrix(
    extractor, field, bad
):
    got = extractor({field: bad})
    assert isinstance(got, tuple) and len(got) == 4
    assert all(isinstance(v, int) and v >= 0 for v in got)


@pytest.mark.parametrize("bad", ["nope", 5, {"a": 1}, object(), None])
def test_both_tool_call_extractors_are_total_over_malformed_content(bad):
    """`extract_tool_calls` iterates `message.content` directly; a non-iterable
    raised `TypeError` where its OpenAI twin returned `[]`."""

    class Msg:
        content = bad

    assert runner.extract_tool_calls(Msg()) == []
    assert runner.extract_openai_tool_calls({"tool_calls": bad}) == []


def test_extract_usage_still_reads_a_well_formed_dict():
    """The falsified control. A function that returns zeros for everything is
    also 'total', and would be useless — so assert the real path still works."""
    assert runner.extract_usage(
        {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_creation_input_tokens": 33,
            "cache_read_input_tokens": 44,
        }
    ) == (11, 22, 33, 44)


def test_the_sdk_runner_never_records_a_silent_zero(monkeypatch):
    """Drive `ClaudeSDKRunner._run_async` over a fake stream whose terminal
    message carries a NON-dict `usage` — the shape an SDK ships the day it moves
    to a dataclass. Before this, that returned four zeros with
    `usage_estimated=False` and `notes=""`, so `agents._invoke` logged no
    `runner_warning` and `$0.00` reached `attempts` as a measurement."""
    import asyncio

    from agentloop.runner import ClaudeSDKRunner

    class Usage:  # a dataclass-shaped usage, not a dict
        input_tokens = 1234
        output_tokens = 56

    class Result:
        result = "the worker output"
        usage = Usage()
        content = []

    async def fake_query(prompt, options):
        yield Result()

    monkeypatch.setattr(runner, "query", fake_query, raising=False)
    monkeypatch.setattr(runner, "ResultMessage", Result, raising=False)

    r = ClaudeSDKRunner()
    with pytest.warns(RuntimeWarning):
        out = asyncio.run(
            r._run_async("sys prompt here", "user prompt here", "claude-opus-5", None)
        )

    assert out.tokens_in > 0 and out.tokens_out > 0
    assert out.usage_estimated is True
    assert "estimated" in out.notes
    assert "claude-agent-sdk" in out.notes


def test_the_sdk_runner_reports_a_real_usage_dict_as_measured(monkeypatch):
    """The falsified control: when usage IS readable, nothing is estimated and
    no warning fires. Without this, a guard that always estimates would pass."""
    import asyncio

    from agentloop.runner import ClaudeSDKRunner

    class Result:
        result = "the worker output"
        usage = {
            "input_tokens": 900,
            "output_tokens": 120,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 40,
        }
        content = []

    async def fake_query(prompt, options):
        yield Result()

    monkeypatch.setattr(runner, "query", fake_query, raising=False)
    monkeypatch.setattr(runner, "ResultMessage", Result, raising=False)

    out = asyncio.run(
        ClaudeSDKRunner()._run_async("sys", "prompt", "claude-opus-5", None)
    )
    assert (out.tokens_in, out.tokens_out, out.cache_read_tokens) == (900, 120, 40)
    assert out.usage_estimated is False
    assert out.notes == ""


def test_an_unreadable_cache_field_is_reported_not_recorded_as_zero(monkeypatch):
    """A usage field that is *present and unreadable* is not the same as an
    absent one, and only the cache fields could not tell the difference.

    Nothing estimates `cache_creation`/`cache_read`, so `_int_or_zero` coerced a
    garbage `cache_read_input_tokens` to 0 and the result was recorded as a
    measurement — `estimated` empty, `note` empty, `usage_estimated=False`, no
    `runner_warning`. Per the decision rules the token total includes cache
    reads and cost prices them at 0.10x, so on a cache-heavy run that is the
    dominant term of the budget cap: the cap silently under-measures and the
    dashboard renders the fabricated zero as spend."""
    import asyncio

    from agentloop.runner import ClaudeSDKRunner

    class Result:
        result = "the worker output"
        usage = {
            "input_tokens": 5000,
            "output_tokens": 300,
            "cache_read_input_tokens": "n/a",  # present, unreadable
        }
        content = []

    async def fake_query(prompt, options):
        yield Result()

    monkeypatch.setattr(runner, "query", fake_query, raising=False)
    monkeypatch.setattr(runner, "ResultMessage", Result, raising=False)

    with pytest.warns(RuntimeWarning):
        out = asyncio.run(ClaudeSDKRunner()._run_async("s", "p", "claude-opus-5", None))

    # The readable fields are still measured, not estimated over.
    assert (out.tokens_in, out.tokens_out) == (5000, 300)
    assert out.cache_read_tokens == 0
    # ...and the zero is declared rather than passed off as a measurement.
    assert out.usage_estimated is True
    assert "cache_read_input_tokens" in out.notes
    assert "unreadable" in out.notes


def test_a_wholly_readable_usage_dict_reports_nothing(monkeypatch):
    """The control: the reporter must fire on garbage, not on every call."""
    import asyncio

    from agentloop.runner import ClaudeSDKRunner

    class Result:
        result = "out"
        usage = {
            "input_tokens": 5000,
            "output_tokens": 300,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 120,
        }
        content = []

    async def fake_query(prompt, options):
        yield Result()

    monkeypatch.setattr(runner, "query", fake_query, raising=False)
    monkeypatch.setattr(runner, "ResultMessage", Result, raising=False)

    out = asyncio.run(ClaudeSDKRunner()._run_async("s", "p", "claude-opus-5", None))
    assert out.usage_estimated is False
    assert out.notes == ""
    assert out.cache_read_tokens == 120
