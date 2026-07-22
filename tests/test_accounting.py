"""Token/cost accounting: cache tokens are captured, priced, and counted
toward the budget caps (the live defect this slice fixes)."""

from pathlib import Path

import pytest

from agentloop.config import (CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER,
                              MODEL_PRICING, estimate_cost_usd)
from agentloop.loop import Loop
from agentloop.models import RunResult, Task, TaskStatus
from agentloop.registry import Registry
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
    usage = {"input_tokens": 2, "output_tokens": 50,
             "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 20000}
    assert extract_usage(usage) == (2, 50, 1000, 20000)


def test_extract_usage_tolerates_missing_and_none():
    assert extract_usage({}) == (0, 0, 0, 0)
    assert extract_usage({"input_tokens": None}) == (0, 0, 0, 0)


# -- pricing ----------------------------------------------------------------

def test_cache_is_priced_off_the_input_rate():
    pin, _ = MODEL_PRICING["claude-sonnet-5"]
    write = estimate_cost_usd("claude-sonnet-5", 0, 0,
                              cache_creation_tokens=1_000_000)
    read = estimate_cost_usd("claude-sonnet-5", 0, 0,
                             cache_read_tokens=1_000_000)
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

    def run(self, system_prompt, prompt, model, tools=None):
        self.calls.append({"prompt": prompt})
        out = self.outputs.pop(0) if self.outputs else "(out)"
        return RunResult(output=out, tokens_in=2, tokens_out=5,
                         cache_creation_tokens=self.cache_creation,
                         cache_read_tokens=self.cache_read,
                         model="claude-sonnet-5")


def test_cache_heavy_run_trips_cost_cap(store):
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    store.add_task(task)
    from agentloop.config import LoopConfig
    cfg = LoopConfig(db_path=store.db_path,
                     workspace_root=str(Path(store.db_path).parent / "ws"),
                     allow_test_exec=False,
                     max_cost_usd_per_task=0.01)  # tiny cost cap
    # ~21k cache-read tokens at sonnet's 0.10x input rate; two attempts clear
    # $0.01. The cap is re-checked at the next iteration boundary after a
    # revision, which is where a cache-heavy run now (correctly) trips.
    runner = CacheHeavyRunner(["out", REVISE, "out2", APPROVE],
                              cache_read=21_000)
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
    cfg = LoopConfig(db_path=store.db_path,
                     workspace_root=str(Path(store.db_path).parent / "ws"),
                     allow_test_exec=False,
                     max_cost_usd_per_task=1e9,      # cost cap out of the way
                     max_tokens_per_task=1000)       # token cap is the gate
    runner = CacheHeavyRunner(["out", REVISE, "out2", APPROVE], cache_read=5000)
    loop = Loop(store, runner, Registry.load(), cfg)
    loop.run_task(task)

    tokens, _ = store.task_spend(task.id)
    assert tokens >= 5000            # cache tokens are in the total
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "Budget cap" in task.escalation_reason


def test_cache_breakdown_persisted_and_rolled_up(store):
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    store.add_task(task)
    from agentloop.config import LoopConfig
    cfg = LoopConfig(db_path=store.db_path,
                     workspace_root=str(Path(store.db_path).parent / "ws"),
                     allow_test_exec=False, max_tokens_per_task=10**9,
                     max_cost_usd_per_task=10**9)
    runner = CacheHeavyRunner(["out", APPROVE], cache_read=7000,
                              cache_creation=300)
    Loop(store, runner, Registry.load(), cfg).run_task(task)

    m = store.run_metrics()
    assert m["cache_read_tokens"] == 14000   # worker + validator, 7000 each
    assert m["cache_creation_tokens"] == 600
    assert m["cost_usd"] > 0
