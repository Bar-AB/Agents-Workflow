"""Slice 4: a second-provider cross-validator.

Two independent things are tested here, because the slice is two things:

- `OpenAICompatRunner` — a stdlib OpenAI-compatible backend behind the same
  `ModelRunner` seam. Tested against a canned HTTP response; nothing here
  touches the network.
- Per-role runner pinning (`AgentSpec.runner`) — the validator can run on a
  different provider than the worker, and the verdict path must not change
  because of it.
"""

import io
import json
import threading
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import pytest

import agentloop.runner as runner_mod

# -- usage parsing (the part that breaks when a provider changes its schema) --


def test_openai_usage_excludes_cached_tokens_from_new_input():
    # OpenAI's prompt_tokens is INCLUSIVE of cached_tokens. RunResult.tokens_in
    # is new input only, so the cached share must move, not be counted twice.
    usage = {
        "prompt_tokens": 10_000,
        "completion_tokens": 250,
        "prompt_tokens_details": {"cached_tokens": 8_000},
    }
    assert runner_mod.extract_openai_usage(usage) == (2_000, 250, 0, 8_000)


def test_openai_usage_without_cache_details():
    usage = {"prompt_tokens": 700, "completion_tokens": 30}
    assert runner_mod.extract_openai_usage(usage) == (700, 30, 0, 0)


def test_openai_usage_tolerates_missing_null_and_overlarge_cache():
    assert runner_mod.extract_openai_usage({}) == (0, 0, 0, 0)
    assert runner_mod.extract_openai_usage({"prompt_tokens": None}) == (0, 0, 0, 0)
    assert runner_mod.extract_openai_usage(
        {"prompt_tokens": 5, "prompt_tokens_details": None}
    ) == (5, 0, 0, 0)
    # A provider reporting more cached than prompt tokens must not produce a
    # negative new-input count, which would *reduce* the measured budget spend.
    assert runner_mod.extract_openai_usage(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 400}}
    ) == (0, 0, 0, 100)


# -- the runner itself, against a canned response (never the network) ---------


def _response(
    content="VERDICT: approve CONFIDENCE: 0.90 TESTS: pass\nfine",
    usage=None,
    tool_calls=None,
    model="gpt-5-mini",
):
    """One /v1/chat/completions body, in the shape the provider returns it."""
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"model": model, "choices": [{"message": message}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _stubbed(monkeypatch, response, **kwargs):
    """An OpenAICompatRunner whose one HTTP call is replaced by `response`."""
    # A key has to be present for the call to be attempted at all; that gate is
    # tested on its own below.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    r = runner_mod.OpenAICompatRunner(**kwargs)
    sent = {}

    def fake_post(payload):
        sent["payload"] = payload
        return response

    monkeypatch.setattr(r, "_post", fake_post)
    return r, sent


def test_openai_runner_parses_a_canned_response(monkeypatch):
    usage = {
        "prompt_tokens": 1_200,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 1_000},
    }
    r, sent = _stubbed(monkeypatch, _response(usage=usage))
    result = r.run("system rules", "review this", "gpt-5-mini", ["file_read"])

    assert result.output.startswith("VERDICT: approve")
    assert (result.tokens_in, result.tokens_out) == (200, 40)
    assert (result.cache_creation_tokens, result.cache_read_tokens) == (0, 1_000)
    # The model string is what pricing is looked up by, so it must be a key.
    from agentloop.config import MODEL_PRICING

    assert result.model in MODEL_PRICING
    # The system prompt is a message, not a prefix glued onto the user prompt.
    roles = [m["role"] for m in sent["payload"]["messages"]]
    assert roles == ["system", "user"]


def test_openai_runner_records_the_serving_model_not_the_requested_one(monkeypatch):
    # Providers alias and substitute; cost is attributed to what actually ran.
    r, _ = _stubbed(
        monkeypatch, _response(model="gpt-5-mini", usage={"prompt_tokens": 5})
    )
    assert r.run("s", "p", "gpt-5").model == "gpt-5-mini"


def test_openai_runner_reports_tool_calls_only_when_present(monkeypatch):
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "Read", "arguments": '{"path": "a.py"}'},
        }
    ]
    r, _ = _stubbed(
        monkeypatch, _response(usage={"prompt_tokens": 9}, tool_calls=calls)
    )
    # `executed: False` is load-bearing: this backend sends no `tools` and has
    # no execution loop, so a reported call is one the model asked for and
    # nobody ran. CLAUDE.md's rule is that a `tool_call` event records a tool an
    # agent *actually invoked*.
    assert r.run("s", "p", "gpt-5-mini").tool_calls == [
        {"tool": "Read", "input": '{"path": "a.py"}', "executed": False}
    ]

    r2, _ = _stubbed(monkeypatch, _response(usage={"prompt_tokens": 9}))
    assert r2.run("s", "p", "gpt-5-mini").tool_calls == []


def test_openai_runner_never_reports_zero_tokens(monkeypatch):
    # A response with no usage block must not price as a free run: a $0 attempt
    # defeats the budget cap silently, which is the failure the cap exists for.
    # (`pytest.warns`, not capsys: these are `warnings.warn` calls now — see
    # test_both_runner_warnings_are_real_warnings_not_stdout for why.)
    r, _ = _stubbed(monkeypatch, _response(usage=None))
    with pytest.warns(RuntimeWarning):
        result = r.run("system", "a fairly long prompt " * 20, "gpt-5-mini")
    assert result.tokens_in > 0
    assert result.tokens_out > 0


def test_openai_runner_warns_when_it_drops_a_tool_allowlist(monkeypatch):
    # A chat-completions call has no tool-execution loop, so a pinned role's
    # registry tools cannot be honored. Degrading (rather than refusing) follows
    # run_summarizer's precedent; warning follows sandbox_isolation='strict',
    # which also degrades loudly — and, since the remediation, does so through
    # the same `warnings.warn` that precedent uses. Silence would leave the
    # capability gap to be discovered from a confused verdict.
    r, sent = _stubbed(
        monkeypatch, _response(usage={"prompt_tokens": 7, "completion_tokens": 2})
    )
    with pytest.warns(RuntimeWarning) as caught:
        r.run("s", "p", "gpt-5-mini", ["file_io", "search"])
    warning = "\n".join(str(w.message) for w in caught)
    assert "file_io" in warning and "search" in warning
    # Dropped means dropped: the names must not be offered to the model either.
    assert "file_io" not in json.dumps(sent["payload"])

    r2, _ = _stubbed(
        monkeypatch, _response(usage={"prompt_tokens": 7, "completion_tokens": 2})
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an empty allowlist drops nothing
        r2.run("s", "p", "gpt-5-mini", [])


def test_openai_runner_takes_a_cwd_positionally_and_drops_it_silently(
    monkeypatch, tmp_path
):
    """The seam is one protocol, so this backend must *accept* `cwd` even
    though it has no filesystem to resolve a path against. Ten lines of comment
    in `runner.py` justify the silent drop and nothing pinned it: removing the
    parameter left the whole suite green and surfaced only as a `TypeError`
    inside `_with_retry` on a live pinned validator — three paid retries and an
    `infra_error`, the exact shape already measured on the test doubles.

    Passed **positionally**, because that is how `agents._invoke` calls the
    seam: a keyword-only assertion would still pass for a runner that renamed
    the parameter.
    """
    r, sent = _stubbed(
        monkeypatch, _response(usage={"prompt_tokens": 7, "completion_tokens": 2})
    )
    with warnings.catch_warnings():
        # Silent, unlike the `tools` drop one test above: a working directory is
        # *meaningless* to a chat-completions call rather than dangerous, so a
        # warning would fire on every attempt of every pinned role and report
        # nothing an operator can act on.
        warnings.simplefilter("error")
        result = r.run("s", "p", "gpt-5-mini", None, str(tmp_path))
    assert result.output.startswith("VERDICT: approve")
    # Dropped means dropped: it must not reach the wire either.
    assert str(tmp_path) not in json.dumps(sent["payload"])


def test_openai_runner_needs_its_key_and_never_leaks_it(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        runner_mod.OpenAICompatRunner().run("s", "p", "gpt-5-mini")
    assert "OPENAI_API_KEY" in str(exc.value)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    r, sent = _stubbed(monkeypatch, _response(usage={"prompt_tokens": 3}))
    result = r.run("s", "p", "gpt-5-mini")
    # The key authenticates the call and must never reach an attempt row, an
    # event payload or a prompt echo.
    assert "sk-secret-value" not in json.dumps(sent["payload"])
    assert "sk-secret-value" not in result.output


# -- pricing: no silent fallback, and the right cache discount ---------------


def test_every_openai_model_is_priced_without_falling_back():
    from agentloop.config import (
        CACHE_MULTIPLIERS,
        DEFAULT_PRICING,
        MODEL_PRICING,
        OPENAI_MODELS,
    )

    assert OPENAI_MODELS, "the runner's model catalogue must not be empty"
    for model in OPENAI_MODELS:
        assert model in MODEL_PRICING, f"{model} would price at DEFAULT_PRICING"
        # ...and the same guard on the cache table beside it: a model priced
        # here but missing there silently takes *Anthropic's* cache rates, which
        # is the five-fold under-bill CACHE_MULTIPLIERS exists to prevent.
        assert model in CACHE_MULTIPLIERS, (
            f"{model} would use the Anthropic cache multipliers"
        )
    # And the fallback really is a different number, so "priced" above is not
    # accidentally satisfied by a row that matches the fallback anyway.
    assert MODEL_PRICING["gpt-5-mini"] != DEFAULT_PRICING


def test_openai_cached_input_uses_the_providers_own_discount():
    # The 1.25x/0.10x globals are the *Anthropic* pricing model. OpenAI charges
    # nothing to write a cache entry and discounts a cached read by a factor
    # that differs per family; pricing a gpt-4o read at Anthropic's 0.10x would
    # under-bill it 5x.
    from agentloop.config import cache_multipliers, estimate_cost_usd

    assert cache_multipliers("claude-sonnet-5") == (1.25, 0.10)
    assert cache_multipliers("gpt-5-mini") == (0.0, 0.10)
    assert cache_multipliers("gpt-4o") == (0.0, 0.50)

    pin, _ = __import__("agentloop.config", fromlist=["MODEL_PRICING"]).MODEL_PRICING[
        "gpt-4o"
    ]
    read = estimate_cost_usd("gpt-4o", 0, 0, cache_read_tokens=1_000_000)
    assert read == pytest.approx(pin * 0.50)
    # An OpenAI cache write is free, so a run reporting one costs nothing extra.
    assert estimate_cost_usd("gpt-4o", 0, 0, cache_creation_tokens=1_000_000) == 0.0


def test_get_runner_knows_the_openai_backend():
    assert isinstance(runner_mod.get_runner("openai"), runner_mod.OpenAICompatRunner)
    with pytest.raises(ValueError):
        runner_mod.get_runner("nope")


# -- pinning a role to a provider (AgentSpec.runner) --------------------------


APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."
REVISE = (
    "VERDICT: revise CONFIDENCE: 0.55 TESTS: fail\n"
    "Edge case for empty input is not handled; add a guard and a test."
)


@pytest.fixture()
def store(tmp_path):
    from agentloop.store import Store

    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _registry(**pins):
    """The default registry with some roles pinned to a named runner."""
    from dataclasses import replace

    from agentloop.registry import Registry

    reg = Registry.load()
    for role, name in pins.items():
        reg.agents[role] = replace(reg.agents[role], runner=name)
    return reg


def _config(store, **overrides):
    from agentloop.config import LoopConfig

    overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    overrides.setdefault("allow_test_exec", False)
    # Slice 6: git is off in the loop tests. A repo per workspace would
    # spawn real subprocesses in ~300 tests that are not about durability.
    overrides.setdefault("vcs_enabled", False)
    return LoopConfig(db_path=store.db_path, **overrides)


def _task(store, title="Add slugify util"):
    from agentloop.models import Task

    task = Task(
        id=None,
        title=title,
        goal="Write a slugify(text) function.",
        acceptance_criteria="Lowercase, hyphen-separated, tested.",
    )
    store.add_task(task)
    return task


def test_agentspec_runner_defaults_to_none():
    from agentloop.models import AgentSpec

    spec = AgentSpec(role="validator", model="gpt-5-mini", system_prompt="s")
    assert spec.runner is None


def test_agents_json_predating_the_runner_field_still_loads(tmp_path):
    # Backward compatibility is load-bearing: agents.json is hand-edited and
    # gitignored, so every existing install has one without this key.
    from agentloop.registry import Registry

    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "worker": {
                    "role": "worker",
                    "model": "claude-sonnet-5",
                    "system_prompt": "w",
                    "tools": ["file_io"],
                    "context_budget_tokens": 1000,
                    "version": "2",
                }
            }
        ),
        encoding="utf-8",
    )
    assert Registry.load(path).get("worker").runner is None


def test_validator_runs_on_a_different_runner_than_the_worker(store):
    """The headline: worker and validator on two different backends, and the
    verdict path is the single-runner path unchanged."""
    from agentloop.loop import Loop
    from agentloop.models import TaskStatus
    from agentloop.runner import MockRunner

    worker_runner = MockRunner(["v1 output", "v2 output fixed"])
    validator_runner = MockRunner([REVISE, APPROVE])
    task = _task(store)
    loop = Loop(
        store,
        worker_runner,
        _registry(validator="openai"),
        _config(store),
        runners={"openai": validator_runner},
    )
    loop.run_task(task)

    # Same status transition as the single-runner revise-then-approve case.
    assert task.status == TaskStatus.DONE
    assert task.revision_count == 1
    kinds = [v["kind"] for v in store.task_metrics(task.id)["verdicts"]]
    assert kinds == ["revise", "approve"]
    # Each provider saw only its own role's prompts — no cross-talk.
    assert len(worker_runner.calls) == 2 and len(validator_runner.calls) == 2
    assert "Task under review" in validator_runner.calls[0]["prompt"]
    assert "Worker output" in validator_runner.calls[0]["prompt"]
    assert all("Task under review" not in c["prompt"] for c in worker_runner.calls)
    # And the validator's feedback still reached the worker's revision prompt.
    assert "empty input" in worker_runner.calls[1]["prompt"]


def test_an_unpinned_run_uses_the_loops_default_runner_only(store):
    # The absent-feature guarantee: with nothing pinned, every role goes to the
    # one runner the loop was constructed with, exactly as before slice 4.
    from agentloop.loop import Loop
    from agentloop.models import TaskStatus
    from agentloop.runner import MockRunner

    default = MockRunner(["out", APPROVE])
    other = MockRunner([APPROVE])
    task = _task(store)
    Loop(
        store,
        default,
        _registry(),
        _config(store),
        runners={"openai": other},
    ).run_task(task)

    assert task.status == TaskStatus.DONE
    assert len(default.calls) == 2
    assert other.calls == []


def test_cost_accounting_attributes_each_provider(store):
    """Per-attempt rows carry the model that served them, and both providers'
    costs land in the one task spend the budget cap reads."""
    from agentloop.config import estimate_cost_usd
    from agentloop.loop import Loop
    from agentloop.models import RunResult
    from agentloop.runner import MockRunner

    worker_result = RunResult(
        output="worker output",
        tokens_in=1_000,
        tokens_out=2_000,
        model="claude-sonnet-5",
    )
    validator_result = RunResult(
        output=APPROVE,
        tokens_in=3_000,
        tokens_out=100,
        cache_read_tokens=10_000,
        model="gpt-5-mini",
    )
    task = _task(store)
    Loop(
        store,
        MockRunner([worker_result]),
        _registry(validator="openai"),
        _config(store),
        runners={"openai": MockRunner([validator_result])},
    ).run_task(task)

    by_model = {r["model"]: r for r in store.run_metrics()["by_model"]}
    assert set(by_model) == {"claude-sonnet-5", "gpt-5-mini"}
    assert by_model["claude-sonnet-5"]["attempts"] == 1
    assert by_model["gpt-5-mini"]["attempts"] == 1

    worker_cost = estimate_cost_usd("claude-sonnet-5", 1_000, 2_000)
    validator_cost = estimate_cost_usd(
        "gpt-5-mini", 3_000, 100, cache_read_tokens=10_000
    )
    assert by_model["gpt-5-mini"]["cost_usd"] == pytest.approx(validator_cost)
    tokens, cost = store.task_spend(task.id)
    assert cost == pytest.approx(worker_cost + validator_cost)
    assert tokens == 1_000 + 2_000 + 3_000 + 100 + 10_000


def test_both_providers_costs_count_toward_one_budget_cap(store):
    # Neither provider's spend alone clears the cap; together they do. A cap
    # that only saw the loop's default runner would never trip here.
    from agentloop.loop import Loop
    from agentloop.models import RunResult, TaskStatus
    from agentloop.runner import MockRunner

    expensive_worker = RunResult(
        output="out", tokens_in=1_000_000, tokens_out=0, model="claude-sonnet-5"
    )  # $3.00
    expensive_validator = RunResult(
        output=REVISE, tokens_in=1_000_000, tokens_out=0, model="gpt-5"
    )  # $1.25
    task = _task(store)
    Loop(
        store,
        MockRunner([expensive_worker]),
        _registry(validator="openai"),
        _config(store, max_cost_usd_per_task=4.00),
        runners={"openai": MockRunner([expensive_validator])},
    ).run_task(task)

    _, cost = store.task_spend(task.id)
    assert cost == pytest.approx(4.25)
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "Budget cap" in task.escalation_reason


def test_a_pin_naming_an_unknown_runner_escalates_as_a_config_error(store):
    # Same shape as the missing-planner-role rule: a configuration error is not
    # transient, so it must not be retried with backoff and reported as an
    # infra failure pointing the human at the network.
    from agentloop.loop import Loop
    from agentloop.models import TaskStatus
    from agentloop.runner import MockRunner

    task = _task(store)
    Loop(
        store,
        MockRunner(["out", APPROVE]),
        _registry(validator="gemeni"),  # typo'd name in agents.json
        _config(store),
    ).run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "gemeni" in task.escalation_reason
    assert "infra_error" not in task.escalation_reason
    kinds = [e["kind"] for e in store.events(task.id)]
    assert "infra_error" not in kinds


# -- the seam holds: no vendor SDK outside runner.py --------------------------


def _imported_module_names(node) -> list[str]:
    """Every module an import statement reaches, as dotted names.

    `from urllib import request` names `urllib` as the module and `request` as
    an alias, so checking `node.module` alone misses the single import form most
    likely to sneak a vendor dependency past this check. The aliases are folded
    in — `from x import y` is treated as reaching `x` *and* `x.y`.
    """
    import ast

    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return [module] + [
            f"{module}.{a.name}" if module else a.name for a in node.names
        ]
    return []


def test_the_seam_check_sees_a_from_urllib_import_request():
    """The checker's own regression test: the form it used to miss."""
    import ast

    tree = ast.parse("from urllib import request\nimport requests\n")
    reached = set()
    for node in ast.walk(tree):
        reached.update(_imported_module_names(node))
    assert "urllib.request" in reached
    assert "requests" in reached


def test_no_provider_sdk_or_http_call_outside_the_runner_seam():
    """Mechanical, not by inspection: the ModelRunner seam is only a seam while
    nothing else in the package knows a vendor."""
    import ast

    vendor = {
        "claude_agent_sdk",
        "anyio",
        "openai",
        "anthropic",
        "litellm",
        "requests",
        "httpx",
        "urllib.request",
        "urllib.error",
        "http.client",
    }
    package = Path(runner_mod.__file__).parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name == "runner.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in _imported_module_names(node):
                # A dotted import matches on any prefix: `urllib.request.urlopen`
                # is as much a provider call as `import requests`.
                parts = name.split(".")
                prefixes = {".".join(parts[: i + 1]) for i in range(len(parts))}
                if prefixes & vendor:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == [], f"provider access outside runner.py: {offenders}"


# ===========================================================================
# Slice 4 remediation: findings from an independent review + a failure hunt.
# Each test below names the defect it pins; every one fails against the code as
# originally shipped.
# ===========================================================================


# -- R1: usage parsing is total, and never discards a paid completion ---------


def test_openai_usage_coerces_non_numeric_fields_to_zero():
    """A wrong *type* used to raise, and the raise landed after the provider had
    already billed the completion: `agents._invoke` calls the runner outside any
    transaction, so nothing was recorded and `_with_retry` paid for the same
    completion again. Missing and None were handled; `'n/a'` and a nested dict
    were not."""
    assert runner_mod.extract_openai_usage(
        {"prompt_tokens": "n/a", "completion_tokens": 12}
    ) == (0, 12, 0, 0)
    assert runner_mod.extract_openai_usage({"prompt_tokens": {"text": 5}}) == (
        0,
        0,
        0,
        0,
    )
    assert runner_mod.extract_openai_usage(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": "many"}}
    ) == (100, 0, 0, 0)
    # A float is a number, not a surprise: truncate rather than zero it.
    assert runner_mod.extract_openai_usage(
        {"prompt_tokens": 10.7, "completion_tokens": 3.2}
    ) == (10, 3, 0, 0)


def test_usage_extraction_blowing_up_never_discards_a_paid_completion(monkeypatch):
    """The whole point: whatever the schema does, `run` returns the reply. The
    estimate keeps the budget cap measuring; raising would throw the completion
    away and then re-pay for it."""
    r, _ = _stubbed(monkeypatch, _response(usage={"prompt_tokens": 5}))

    def boom(usage):
        raise TypeError("the provider changed its usage schema")

    monkeypatch.setattr(runner_mod, "extract_openai_usage", boom)
    with pytest.warns(RuntimeWarning):
        result = r.run("s", "a prompt " * 50, "gpt-5-mini")
    assert result.output.startswith("VERDICT")
    assert result.tokens_in > 0 and result.tokens_out > 0
    assert result.usage_estimated is True


# -- R2: the never-zero guard checks each field independently ----------------


def test_openai_usage_falls_back_to_total_minus_completion():
    """`total_tokens` was ignored entirely, so a provider omitting
    `prompt_tokens` recorded 0 input against a multi-KB prompt."""
    assert runner_mod.extract_openai_usage(
        {"total_tokens": 1500, "completion_tokens": 100}
    ) == (1400, 100, 0, 0)
    # A total below the completion count cannot produce a negative input.
    assert runner_mod.extract_openai_usage(
        {"total_tokens": 50, "completion_tokens": 100}
    ) == (0, 100, 0, 0)
    # An explicit prompt_tokens still wins; the total is only a fallback.
    assert runner_mod.extract_openai_usage(
        {"total_tokens": 999, "prompt_tokens": 40, "completion_tokens": 10}
    ) == (40, 10, 0, 0)


def test_partial_usage_estimates_only_the_field_that_is_missing(monkeypatch):
    """The old guard required all three fields to be zero, so partial usage
    recorded tokens_in=0 with no warning — and input is the dominant cost on a
    validator prompt carrying the whole worker output."""
    r, _ = _stubbed(monkeypatch, _response(usage={"completion_tokens": 100}))
    with pytest.warns(RuntimeWarning):
        result = r.run("system", "a fairly long prompt " * 40, "gpt-5-mini")
    assert result.tokens_in > 0
    assert result.tokens_out == 100  # measured, not overwritten by an estimate
    assert result.usage_estimated is True


def test_a_fully_cached_prompt_is_not_mistaken_for_missing_usage(monkeypatch):
    """tokens_in == 0 with cache_read > 0 is a legitimately fully-cached prompt,
    not an absent usage block. Estimating over it would invent fresh input at
    the full rate on top of the discounted cached read."""
    usage = {
        "prompt_tokens": 9_000,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 9_000},
    }
    r, _ = _stubbed(monkeypatch, _response(usage=usage))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here fails the test
        result = r.run("s", "p", "gpt-5-mini")
    assert (result.tokens_in, result.cache_read_tokens) == (0, 9_000)
    assert result.usage_estimated is False


# -- R3: warnings are warnings, and an estimate is audited -------------------


def test_both_runner_warnings_are_real_warnings_not_stdout(monkeypatch, capsys):
    """`print` writes to the stream the CLI's structured output owns, is
    invisible to `-W` and `pytest.warns`, does not dedup, and so repeats on
    every attempt of every task. The precedent these two warnings cite
    (`sandbox_isolation='strict'`) is `warnings.warn`."""
    r, _ = _stubbed(monkeypatch, _response(usage=None))
    with pytest.warns(RuntimeWarning, match="usage"):
        r.run("system", "prompt", "gpt-5-mini")
    assert capsys.readouterr().out == ""

    r2, _ = _stubbed(monkeypatch, _response(usage={"prompt_tokens": 7}))
    with pytest.warns(RuntimeWarning, match="file_io"):
        r2.run("s", "p", "gpt-5-mini", ["file_io", "search"])
    assert capsys.readouterr().out == ""


def test_an_estimated_usage_reaches_the_audit_log(store):
    """A stdout warning is unrecorded: it never reaches `agentloop events`, the
    REST API or the SSE feed, and the estimated tokens then land in `attempts`
    indistinguishably from provider-measured ones."""
    from agentloop.loop import Loop
    from agentloop.models import RunResult
    from agentloop.runner import MockRunner

    worker = RunResult(
        output="out",
        tokens_in=10,
        tokens_out=5,
        model="gpt-5-mini",
        usage_estimated=True,
        notes="api.openai.com reported no usage; estimated.",
    )
    task = _task(store)
    Loop(store, MockRunner([worker, APPROVE]), _registry(), _config(store)).run_task(
        task
    )

    warns = [e for e in store.events(task.id) if e["kind"] == "runner_warning"]
    assert len(warns) == 1, "the estimate must be recorded, not just printed"
    payload = warns[0]["payload"]
    assert payload["usage_estimated"] is True
    assert "no usage" in payload["note"]
    assert payload["agent_kind"] == "worker"
    # And it did not cost the attempt: the paid completion is still recorded.
    tokens, _ = store.task_spend(task.id)
    assert tokens >= 15


def test_a_runner_note_that_cannot_be_stringified_never_fails_the_attempt(store):
    """Telemetry must never fail an attempt — the `_tool_name_repr` rule, one
    field further out. A note whose `__str__` raises would otherwise blow up
    inside `_invoke`'s closing transaction and roll back an already-paid
    `finish_attempt`."""
    from agentloop.loop import Loop
    from agentloop.models import RunResult, TaskStatus
    from agentloop.runner import MockRunner

    class Unrepresentable:
        def __str__(self):
            raise RuntimeError("nope")

        __repr__ = __str__

    worker = RunResult(
        output="out",
        tokens_in=10,
        tokens_out=5,
        model="gpt-5-mini",
        usage_estimated=True,
        notes=Unrepresentable(),
    )
    task = _task(store)
    Loop(store, MockRunner([worker, APPROVE]), _registry(), _config(store)).run_task(
        task
    )

    assert task.status == TaskStatus.DONE
    assert "infra_error" not in [e["kind"] for e in store.events(task.id)]
    warns = [e for e in store.events(task.id) if e["kind"] == "runner_warning"]
    assert len(warns) == 1 and "Unrepresentable" in warns[0]["payload"]["note"]


def test_a_requested_tool_call_is_not_logged_as_an_executed_one(store):
    """`tool_call` is defined as "a tool the agent actually invoked". A
    chat-completions backend executes nothing, so its records carry
    `executed: False` — slice 5's auto-approval policy reads this event, and a
    policy that cannot tell a request from an execution approves on neither."""
    from agentloop.loop import Loop
    from agentloop.models import RunResult
    from agentloop.runner import MockRunner

    worker = RunResult(
        output="out",
        tokens_in=1,
        tokens_out=1,
        model="gpt-5-mini",
        tool_calls=[{"tool": "Read", "input": "{}", "executed": False}],
    )
    task = _task(store)
    Loop(store, MockRunner([worker, APPROVE]), _registry(), _config(store)).run_task(
        task
    )
    calls = [e for e in store.events(task.id) if e["kind"] == "tool_call"]
    assert len(calls) == 1 and calls[0]["payload"]["executed"] is False


# -- R4: the key travels in the Authorization header and nowhere else --------


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    """Stands in for the runner's own opener — one layer below `_post`, so the
    real request object is built and can be inspected."""

    def __init__(self, body: dict):
        self.body = body
        self.request = None

    def open(self, request, timeout=None):
        self.request = request
        return _FakeResponse(self.body)


class _RaisingOpener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, request, timeout=None):
        raise self.exc


def test_the_api_key_travels_only_in_the_authorization_header(monkeypatch):
    """The old assertion stubbed `_post` — the only place the key is ever used —
    and then asserted the key was absent from a payload that never had a path to
    it. It could not fail. This one drives the real `_post`."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    r = runner_mod.OpenAICompatRunner()
    opener = _RecordingOpener(_response(usage={"prompt_tokens": 3}))
    monkeypatch.setattr(r, "_opener", opener)

    result = r.run("s", "p", "gpt-5-mini")

    request = opener.request
    assert request.get_header("Authorization") == "Bearer sk-secret-value"
    others = {k: v for k, v in request.header_items() if k.lower() != "authorization"}
    assert "sk-secret-value" not in json.dumps(others)
    assert "sk-secret-value" not in request.data.decode("utf-8")
    assert "sk-secret-value" not in request.full_url
    assert "sk-secret-value" not in result.output


# -- R5: a dated snapshot id must price as its family ------------------------


def test_a_dated_snapshot_id_prices_as_its_family():
    """OpenAI echoes the resolved snapshot (`gpt-4o-mini-2024-07-18`), and the
    pricing lookup was exact-match, so on real traffic every new pricing row and
    the whole per-provider cache table were unreachable: gpt-4o-mini priced at
    DEFAULT_PRICING's 20x input rate *and* took Anthropic's cache multipliers."""
    from agentloop.config import (
        DEFAULT_PRICING,
        MODEL_PRICING,
        cache_multipliers,
        estimate_cost_usd,
    )

    assert "gpt-4o-mini-2024-07-18" not in MODEL_PRICING, "the fixture must be a miss"
    assert estimate_cost_usd("gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(
        0.15
    )
    assert cache_multipliers("gpt-4o-mini-2024-07-18") == (0.0, 0.50)
    assert cache_multipliers("gpt-5-2025-08-07") == (0.0, 0.10)
    # A longer family name wins over its own prefix.
    assert estimate_cost_usd("gpt-5-mini-2025-08-07", 1_000_000, 0) == pytest.approx(
        0.25
    )
    # An unrelated model still falls back rather than matching something near it.
    assert estimate_cost_usd("llama-3-70b", 1_000_000, 0) == pytest.approx(
        DEFAULT_PRICING[0]
    )


def test_the_serving_snapshot_id_is_kept_for_the_audit_trail(monkeypatch):
    """Normalize at the *pricing* boundary, not by discarding provenance: the
    attempt row must still say which snapshot actually served the call."""
    r, _ = _stubbed(
        monkeypatch,
        _response(model="gpt-4o-mini-2024-07-18", usage={"prompt_tokens": 5}),
    )
    assert r.run("s", "p", "gpt-4o-mini").model == "gpt-4o-mini-2024-07-18"


# -- R6: `eval --runner openai` really runs OpenAI, or says it cannot --------


def _eval_config(tmp_path) -> str:
    cfg = tmp_path / "loopconfig.json"
    cfg.write_text(json.dumps({"db_path": str(tmp_path / "eval.db")}), encoding="utf-8")
    return str(cfg)


def test_eval_openai_skips_without_credentials_instead_of_running_the_fixtures(
    monkeypatch, tmp_path, capsys
):
    """`--runner openai` fell through to `else:` and printed scripted-fixture
    agreement numbers as a calibration report. A calibration number that
    measured nothing is worse than none."""
    from agentloop.cli import main

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["--config", _eval_config(tmp_path), "eval", "--runner", "openai"]) == 0
    out = capsys.readouterr().out.lower()
    assert "openai" in out and "skipped" in out
    assert "agreement" not in out, "the mock fixtures must not have run"


def test_eval_openai_uses_the_openai_backend_when_a_key_is_present(
    monkeypatch, tmp_path
):
    from agentloop import eval as evalmod
    from agentloop.cli import main

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    captured = {}
    monkeypatch.setattr(
        evalmod, "run_eval", lambda s, runner, reg: captured.setdefault("r", runner)
    )
    monkeypatch.setattr(evalmod, "format_report", lambda result: "(report)")
    assert main(["--config", _eval_config(tmp_path), "eval", "--runner", "openai"]) == 0
    assert isinstance(captured["r"], runner_mod.OpenAICompatRunner)


# -- R7: permanent provider errors are config errors, with the body kept -----


def _pinned(**pins):
    """The default registry with roles pinned to (runner, model) pairs."""
    from dataclasses import replace

    from agentloop.registry import Registry

    reg = Registry.load()
    for role, (name, model) in pins.items():
        reg.agents[role] = replace(reg.agents[role], runner=name, model=model)
    return reg


def test_a_missing_key_escalates_as_a_config_error_not_an_infra_failure(
    store, monkeypatch
):
    """`run()` is called *inside* `_with_retry`, so the RuntimeError for a
    missing key was caught like a network blip: retried with backoff and
    reported as `infra_error`, pointing the human at the network instead of at
    their environment."""
    from agentloop.loop import Loop
    from agentloop.models import TaskStatus
    from agentloop.runner import MockRunner

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task = _task(store)
    Loop(
        store,
        MockRunner(["out", APPROVE]),
        _pinned(validator=("openai", "gpt-5-mini")),
        _config(store),
    ).run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "OPENAI_API_KEY" in task.escalation_reason
    assert "infra_error" not in [e["kind"] for e in store.events(task.id)]


def test_a_permanent_http_error_is_a_config_error_carrying_the_provider_body(
    monkeypatch,
):
    """A 401, a 404 `model_not_found` and a transient 503 all reached the audit
    log as `HTTPError: HTTP Error 4xx: <reason>` after three paid round trips,
    indistinguishable from each other."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    for code in (400, 401, 403, 404):
        r = runner_mod.OpenAICompatRunner()
        body = io.BytesIO(
            b'{"error": {"message": "The model `claude-sonnet-5` does not exist"}}'
        )
        monkeypatch.setattr(
            r,
            "_opener",
            _RaisingOpener(
                urllib.error.HTTPError(
                    "https://api.openai.com/v1/chat/completions",
                    code,
                    "Not Found",
                    {},
                    body,
                )
            ),
        )
        with pytest.raises(runner_mod.RunnerConfigError) as exc:
            r.run("s", "p", "gpt-5-mini")
        assert "does not exist" in str(exc.value)
        assert str(code) in str(exc.value)


def test_a_transient_http_error_stays_retryable_and_keeps_its_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    for code in (408, 429, 500, 503):
        r = runner_mod.OpenAICompatRunner()
        body = io.BytesIO(b'{"error": {"message": "the engine is overloaded"}}')
        monkeypatch.setattr(
            r,
            "_opener",
            _RaisingOpener(
                urllib.error.HTTPError(
                    "https://api.openai.com/v1/chat/completions",
                    code,
                    "Service Unavailable",
                    {},
                    body,
                )
            ),
        )
        with pytest.raises(Exception) as exc:
            r.run("s", "p", "gpt-5-mini")
        assert not isinstance(exc.value, runner_mod.RunnerConfigError)
        assert "overloaded" in str(exc.value)


# -- R8: a pinned role's model must belong to its pinned provider ------------


def test_a_claude_model_pinned_to_the_openai_backend_is_a_config_error(store):
    """The single most likely first-use edit: set `runner: openai` on the
    validator and forget `model`. It used to send `claude-sonnet-5` to OpenAI,
    take a 404, retry it three times and escalate reading `infra_error` — the
    exact failure the `AgentSpec.runner` comment claims to have designed away."""
    from agentloop.loop import Loop
    from agentloop.models import TaskStatus
    from agentloop.runner import MockRunner

    task = _task(store)
    Loop(
        store,
        MockRunner(["out", APPROVE]),
        _pinned(validator=("openai", "claude-sonnet-5")),
        _config(store),
    ).run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "validator" in task.escalation_reason
    assert "claude-sonnet-5" in task.escalation_reason
    assert "infra_error" not in [e["kind"] for e in store.events(task.id)]


def test_a_custom_model_id_on_an_openai_endpoint_is_allowed_with_a_warning(store):
    """Permissive by design: a self-hosted or gateway model id is a legitimate
    thing to point this backend at. Only a `claude-*` model is a hard error."""
    from agentloop.loop import Loop

    loop = Loop(
        store,
        runner_mod.MockRunner(),
        _pinned(validator=("openai", "mixtral-8x7b-instruct")),
        _config(store),
    )
    with pytest.warns(RuntimeWarning, match="mixtral"):
        backend = loop._runner_for("validator")
    assert isinstance(backend, runner_mod.OpenAICompatRunner)


# -- R9: the bearer token never follows a redirect, nor rides plaintext ------


def test_a_plaintext_base_url_is_refused_unless_it_is_local():
    with pytest.raises(runner_mod.RunnerConfigError, match="https"):
        runner_mod.OpenAICompatRunner(base_url="http://api.example.com/v1")
    # A local endpoint (ollama, vLLM, a test double) never leaves the machine.
    for local in ("http://localhost:8000/v1", "http://127.0.0.1:11434/v1"):
        assert runner_mod.OpenAICompatRunner(base_url=local).base_url == local


def test_the_opener_refuses_redirects_so_the_bearer_token_cannot_follow_one():
    """Python's HTTPRedirectHandler strips only content-length/content-type and
    forwards `Authorization`, cross-host included. This project scrubs
    ANTHROPIC_API_KEY out of the sandbox by construction; the same care belongs
    on the wire."""
    r = runner_mod.OpenAICompatRunner()
    handlers = [
        h
        for h in r._opener.handlers
        if isinstance(h, urllib.request.HTTPRedirectHandler)
    ]
    assert handlers, "the runner must own its redirect policy, not inherit it"
    request = urllib.request.Request("https://api.openai.com/v1/chat/completions")
    with pytest.raises(runner_mod.RunnerConfigError, match="redirect"):
        handlers[0].redirect_request(
            request, None, 302, "Found", {}, "https://elsewhere.example.com/v1"
        )


# -- R12: one pinned backend, one instance, even under parallel workers ------


def test_a_pinned_backend_is_constructed_once_under_concurrent_lookups(
    store, monkeypatch
):
    """`_runners` was check-then-act. Both shipped backends are stateless so
    duplicate construction is benign today, but the dict is sold as a cache
    *guaranteeing* one instance per name — and a MockRunner injected through
    `runners={...}` is shared across threads while being documented
    single-threaded-only."""
    import time as _time

    from agentloop.loop import Loop

    built = []

    def slow_get_runner(name):
        _time.sleep(0.05)  # wide enough for an unguarded check-then-act to race
        made = runner_mod.MockRunner()
        built.append(made)
        return made

    monkeypatch.setattr("agentloop.loop.get_runner", slow_get_runner)
    loop = Loop(
        store,
        runner_mod.MockRunner(),
        _pinned(validator=("slowbackend", "gpt-5-mini")),
        _config(store),
    )
    seen = []
    threads = [
        threading.Thread(target=lambda: seen.append(loop._runner_for("validator")))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(built) == 1, f"constructed {len(built)} backends for one name"
    assert len({id(x) for x in seen}) == 1


# -- the runner half of the empty-output hole (the loop half is out of scope) -


def test_an_http_200_error_envelope_raises_instead_of_an_empty_output(monkeypatch):
    """OpenRouter/vLLM/Azure gateways return `{"error": {...}}` with HTTP 200 for
    quota and policy failures. Returning a plausible-looking `output=""` sends a
    blank worker output down the ordinary success path."""
    r, _ = _stubbed(
        monkeypatch,
        {"error": {"message": "insufficient quota", "type": "insufficient_quota"}},
    )
    with pytest.raises(runner_mod.ProviderResponseError, match="insufficient quota"):
        r.run("s", "p", "gpt-5-mini")


def test_a_contentless_or_truncated_completion_raises(monkeypatch):
    r, _ = _stubbed(monkeypatch, _response(content=None, usage={"prompt_tokens": 5}))
    with pytest.raises(runner_mod.ProviderResponseError):
        r.run("s", "p", "gpt-5-mini")

    truncated = _response(usage={"prompt_tokens": 5})
    truncated["choices"][0]["finish_reason"] = "length"
    r2, _ = _stubbed(monkeypatch, truncated)
    with pytest.raises(runner_mod.ProviderResponseError, match="length"):
        r2.run("s", "p", "gpt-5-mini")

    # `stop` is normal and must not raise; nor must an absent finish_reason.
    ok = _response(usage={"prompt_tokens": 5})
    ok["choices"][0]["finish_reason"] = "stop"
    r3, _ = _stubbed(monkeypatch, ok)
    assert r3.run("s", "p", "gpt-5-mini").output.startswith("VERDICT")


def test_an_unreadable_nested_cache_field_is_reported_on_the_openai_shape(monkeypatch):
    """The reporter was wired into both backends and could only *see* one of
    them, which reads as coverage and is not.

    Anthropic reports its cache counts at the top level; OpenAI nests them under
    `prompt_tokens_details.cached_tokens`, and `prompt_tokens_details` does not
    end in `tokens` — so a garbage cached count was invisible while
    `extract_openai_usage` coerced it to 0 and recorded that as a measurement.
    Measured before the fix: `coerced_usage_fields(...) -> []`.
    """
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 300,
        "prompt_tokens_details": {"cached_tokens": "n/a"},
    }
    assert runner_mod.coerced_usage_fields(usage) == [
        "prompt_tokens_details.cached_tokens"
    ]

    r, _sent = _stubbed(monkeypatch, _response(usage=usage))
    with pytest.warns(RuntimeWarning):
        result = r.run("system", "prompt", "gpt-4o-mini")

    # The readable fields stay measured; only the unreadable one is declared.
    assert result.tokens_in == 5000 and result.tokens_out == 300
    assert result.cache_read_tokens == 0
    assert result.usage_estimated is True
    assert "cached_tokens" in result.notes
    assert "unreadable" in result.notes


def test_a_readable_nested_cache_field_reports_nothing(monkeypatch):
    """The control: the reporter must fire on garbage, not on the shape."""
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 300,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }
    assert runner_mod.coerced_usage_fields(usage) == []

    r, _sent = _stubbed(monkeypatch, _response(usage=usage))
    result = r.run("system", "prompt", "gpt-4o-mini")
    assert result.usage_estimated is False
    assert result.notes == ""
    # ...and the cached share is still subtracted out of the inclusive prompt.
    assert result.tokens_in == 4000 and result.cache_read_tokens == 1000
