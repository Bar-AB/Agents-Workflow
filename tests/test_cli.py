"""0d: the CLI turns expected user-input errors (bad ids) into a clean stderr
message + exit 1, instead of leaking a raw Python traceback."""

import pytest

from agentloop.cli import main


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    # Keep the throwaway agentloop.db out of the repo.
    monkeypatch.chdir(tmp_path)


def test_bad_task_id_prints_clean_error_and_exits_1(capsys):
    rc = main(["approve", "9999"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "9999" in err
    assert "Traceback" not in err  # no leaked stack trace


@pytest.mark.parametrize("cmd", ["reject", "redo", "pause", "resume", "abort"])
def test_all_id_commands_handle_a_bad_id(cmd, capsys):
    rc = main([cmd, "9999"])
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err


def test_bad_memory_id_prints_clean_error(capsys):
    rc = main(["memory", "approve", "9999"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "9999" in err
    assert "Traceback" not in err


def test_valid_command_still_returns_0(capsys):
    rc = main(["add", "A task", "--goal", "do it", "--criteria", "works"])
    assert rc == 0
    assert "defined" in capsys.readouterr().out


# -- planner CLI (slice 3) ----------------------------------------------------


def test_approve_plan_on_a_bad_id_is_a_clean_error(capsys):
    rc = main(["approve-plan", "9999"])
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err


def test_approve_plan_on_an_ordinary_task_is_a_clean_error(capsys):
    """A wrong-command-for-this-id mistake is user input, not a crash: the
    ValueError must render like a bad id does."""
    main(["add", "A task", "--goal", "do it", "--criteria", "works"])
    capsys.readouterr()
    rc = main(["approve-plan", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a plan" in err
    assert "Traceback" not in err


def test_plan_command_prints_the_graph_and_gates_it(capsys, monkeypatch):
    """The mock runner is scripted through the CLI's own runner factory, so this
    exercises the real `agentloop plan` path with no API key."""
    import agentloop.cli as cli
    from agentloop.runner import MockRunner
    from tests.test_planner import PLAN_JSON

    monkeypatch.setattr(cli, "get_runner", lambda name: MockRunner([PLAN_JSON]))
    rc = main(
        ["plan", "Build a slugify library", "--criteria", "tested", "--runner", "mock"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Plan 1 created: 3 task(s)" in out
    assert "Write slugify()" in out
    assert "depends on 2" in out  # the graph's one dependency, by task id
    assert "approve-plan 1" in out  # gated by default

    rc = main(["approve-plan", "1"])
    assert rc == 0
    assert "3 task(s) released" in capsys.readouterr().out


# -- charter CLI (slice 3c) ---------------------------------------------------


def test_charter_cli_show_set_clear_history(capsys, tmp_path):
    """The human write surface, end to end. Agents have no write path at all,
    so this and the dashboard are the only two ways the charter can change."""
    assert main(["charter", "show"]) == 0
    assert "no charter set" in capsys.readouterr().out

    assert main(["charter", "set", "--text", "1. Raise, never return None."]) == 0
    assert "Charter v1 set" in capsys.readouterr().out
    assert main(["charter", "show"]) == 0
    out = capsys.readouterr().out
    assert "v1 (in effect)" in out and "Raise, never return None" in out

    rules = tmp_path / "RULES.md"
    rules.write_text("1. Raise, never return None.\n2. Docstrings required.\n")
    assert main(["charter", "set", "--file", str(rules), "--note", "added rule 2"]) == 0
    assert "Charter v2 set" in capsys.readouterr().out

    # A past version stays readable after a newer one exists — that is what
    # makes "approved under the old rules" answerable.
    assert main(["charter", "show", "--version", "1"]) == 0
    assert "Docstrings required" not in capsys.readouterr().out

    assert main(["charter", "clear", "--note", "paused"]) == 0
    assert "Charter cleared" in capsys.readouterr().out
    assert main(["charter", "show"]) == 0
    assert "no charter set" in capsys.readouterr().out

    assert main(["charter", "history"]) == 0
    hist = capsys.readouterr().out
    assert "[v  1]" in hist and "[v  2]" in hist and "CLEARED" in hist
    assert "added rule 2" in hist


def test_oversize_charter_set_is_a_clean_error_not_a_traceback(capsys):
    """The charter is refused loudly at write time precisely because it is
    never trimmed at inject time. That refusal must read as a user error."""
    from agentloop.store import _MAX_CHARTER_CHARS

    rc = main(["charter", "set", "--text", "x" * (_MAX_CHARTER_CHARS + 1)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "over the" in err
    assert "Traceback" not in err


def test_charter_set_needs_exactly_one_source(capsys):
    rc = main(["charter", "set"])
    assert rc == 1
    assert "exactly one of --file or --text" in capsys.readouterr().err

    rc = main(["charter", "set", "--text", "a", "--file", "b.md"])
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err


def test_charter_show_of_a_missing_version_is_a_clean_error(capsys):
    rc = main(["charter", "show", "--version", "42"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "42" in err and "Traceback" not in err


def test_plan_that_cannot_be_parsed_exits_1_with_no_tasks(capsys, monkeypatch):
    import agentloop.cli as cli
    from agentloop.runner import MockRunner

    monkeypatch.setattr(
        cli, "get_runner", lambda name: MockRunner(["I'd start with the parser."])
    )
    rc = main(["plan", "Build a thing", "--criteria", "works", "--runner", "mock"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "produced no tasks" in err
    assert "Traceback" not in err


def test_a_runner_that_refuses_to_construct_is_a_clean_error(capsys, monkeypatch):
    """Slice 4: construction failures need their own handler.

    `_build` runs *before* the try/except that renders `error: ...`, because
    that block's `finally` closes a store which does not exist yet. Slice 4 gave
    the runner real constructor-time refusals (an unknown `--runner` name, a
    plaintext `OPENAI_BASE_URL`), and the whole value of refusing loudly at
    construction is lost if the refusal reaches the operator as a traceback.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "http://not-https.example.com/v1")
    rc = main(["run", "--runner", "openai", "--max-tasks", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "https" in err  # the refusal explains itself
    assert "Traceback" not in err


def test_an_unknown_runner_name_is_a_clean_error(capsys):
    rc = main(["run", "--runner", "mock", "--max-tasks", "0"])
    assert rc == 0  # sanity: a known runner still builds

    from agentloop import cli

    def _boom(name):
        raise ValueError(f"Unknown runner: {name!r}")

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "get_runner", _boom)
        rc = main(["run", "--runner", "mock", "--max-tasks", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown runner" in err
    assert "Traceback" not in err


# -- tool requests CLI (slice 5, Phase 7) -------------------------------------


def _seed_tool_request(
    tool: str = "shell", *, blocking: bool = True, parked: bool = True
) -> int:
    """A task with one pending request, written through the real store so the
    CLI reads exactly what the loop would have written."""
    from agentloop.models import Task, TaskStatus
    from agentloop.store import Store

    store = Store("agentloop.db")
    try:
        task = Task(
            id=None,
            title="Ship it",
            goal="do it",
            acceptance_criteria="works",
        )
        task_id = store.add_task(task)
        request_id = store.tool_request_add(
            task_id,
            role="worker",
            agent_kind="worker",
            tool=tool,
            status="pending",
            source="marker",
            reason="need to run the build",
            blocking=blocking,
        )
        if parked:
            task.status = TaskStatus.NEEDS_HUMAN
            task.escalation_reason = (
                f"Awaiting tool approval: {tool} (requested by worker)."
            )
            store.update_task(task)
            store.tool_requests_mark_parked(task_id, [request_id])
        return request_id
    finally:
        store.close()


def test_cli_tools_list(capsys):
    _seed_tool_request()
    rc = main(["tools", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shell" in out
    assert "pending" in out
    assert "need to run the build" in out


def test_cli_tools_list_renders_the_collateral_consequence(capsys):
    """The carried E3 item: a decision on `shell` also decides `git`, because
    `LOGICAL_TOOL_MAP` is not injective. A human must see that before choosing,
    and must also see what the label actually grants (`shell -> Bash`)."""
    _seed_tool_request("shell")
    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "Bash" in out  # what the grant actually confers
    assert "git" in out  # what it also decides


def test_cli_tools_list_states_the_outcome_and_not_only_the_sharing(capsys):
    """E4/F1 at the CLI. `[also decides: git -> Bash]` is a true statement about
    the map and stays. What may not be inferred from it is the consequence: while
    this ask is *pending* its `Bash` is already withheld, so `git` does not work
    today — rejecting takes nothing away and approving is what restores it."""
    _seed_tool_request("shell")
    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "[also decides: git -> Bash]" in out  # the sharing line is untouched
    assert "effect: approving grants Bash" in out
    assert "git works again" in out
    assert "rejecting takes nothing further away" in out
    assert "git is already withheld" in out


def test_cli_tools_does_not_promise_a_grant_that_will_not_happen(capsys):
    """E4/F1 state 1, at the CLI, before *and* after the click: `git` rejected
    means approving `shell` yields no `Bash` at all, because `subtract_withheld`
    removes it on account of the denial. The list must not promise the capability
    and the approve echo must not claim it landed."""
    from agentloop.store import Store

    _seed_tool_request("shell")
    store = Store("agentloop.db")
    try:
        git = store.tool_request_add(
            1,
            role="worker",
            agent_kind="worker",
            tool="git",
            status="pending",
            source="marker",
        )
        store.tool_request_decide(git, approved=False, by="human")
    finally:
        store.close()

    main(["tools", "list", "--pending"])
    out = capsys.readouterr().out
    assert "approving does not deliver Bash" in out
    assert "approving grants Bash" not in out

    main(["tools", "approve", "1"])
    after = capsys.readouterr().out
    assert "NOT in force" in after


def test_cli_tools_a_refused_row_does_not_claim_to_withhold_anything(capsys):
    """A `refused` row is the machine declining to *queue* an ask, and
    `withheld_tools` deliberately never sees it, so it subtracts nothing.

    The population has **two halves** and the fix has to cover both. An unknown
    name confers nothing, so there is nothing to withhold either way; a row
    refused **over the per-task cap** names a real capability, and if the role
    holds that capability through another declared name the gate hands it over —
    so "Bash is not available to this role" would tell a human a gate is closed
    while it is open. A genuine `rejected` row must still report the sibling it
    takes down with it.
    """
    from agentloop.store import Store

    _seed_tool_request("shell")  # task 1, pending
    store = Store("agentloop.db")
    try:
        store.tool_request_add(
            1,
            role="worker",
            agent_kind="worker",
            tool="docker",
            status="refused",
            source="marker",
            why="not a known logical tool name",
        )
    finally:
        store.close()

    main(["tools", "reject", "1", "--note", "no shells"])
    capsys.readouterr()
    main(["tools", "list"])
    out = capsys.readouterr().out
    # The denial: `git` shares `Bash`, so it goes down with `shell`.
    assert "Bash is not available to this role; git is withheld with it" in out
    # The refusal that confers nothing: there is nothing to withhold.
    assert "nothing to withhold — this name confers no capability" in out


def test_cli_tools_a_cap_refused_row_does_not_report_a_live_capability_as_gone(
    capsys,
):
    """The half of the `refused` population that carries a real capability, and
    where the HIGH lived. One `pending` row fills a cap of one, so the next ask is
    refused by the machine; the shipped worker declares `git`, so `Bash` is fully
    live and the gate hands it to the runner. The screen may not call it
    unavailable."""
    import json

    from agentloop.runner import resolve_tools
    from agentloop.store import Store

    json.dump({"max_tool_requests_per_task": 1}, open("loopconfig.json", "w"))
    _seed_tool_request("web", blocking=False, parked=False)  # task 1, pending
    store = Store("agentloop.db")
    try:
        assert (
            store.tool_request_add(
                1,
                role="worker",
                agent_kind="worker",
                tool="shell",
                status="pending",
                source="marker",
                max_per_task=1,
            )
            is None
        )  # over the cap -> a `refused` row for a tool conferring `Bash`
        row = next(r for r in store.tool_requests() if r.tool == "shell")
        assert row.status.value == "refused"
        from agentloop.config import LoopConfig
        from agentloop.registry import DEFAULT_AGENTS
        from agentloop.toolpolicy import tools_for

        # What the gate really hands the runner for this role on this task.
        assert "Bash" in resolve_tools(
            tools_for(store, LoopConfig(), DEFAULT_AGENTS["worker"], 1, "worker")
        )
    finally:
        store.close()

    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "no effect: the role has Bash regardless" in out
    assert "Bash is not available to this role" not in out


def test_cli_tools_a_grant_of_nothing_is_not_rendered_as_a_withheld_capability(
    capsys,
):
    """M2. `classify` consults the read-only allowlist *before* the map, so a name
    in the allowlist that `LOGICAL_TOOL_MAP` lacks is an `auto` row conferring
    nothing. The list collapsed to the empty string and the sentence claimed a
    withholding that never happened: `NOT in force:  is withheld on account of
    another request on this task`."""
    import json

    from agentloop.store import Store

    json.dump(
        {"tool_readonly_allowlist": ["file_read", "search", "task_state", "kubectl"]},
        open("loopconfig.json", "w"),
    )
    _seed_tool_request("shell")
    store = Store("agentloop.db")
    try:
        store.tool_request_add(
            1,
            role="worker",
            agent_kind="worker",
            tool="kubectl",
            status="auto",
            source="marker",
        )
    finally:
        store.close()

    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "confers no capability" in out
    assert "NOT in force" not in out
    assert "is withheld on account of" not in out


def test_cli_tools_plural_agreement_on_a_multi_name_capability(capsys):
    """LOW: `git and web starts working again` — a plural list under a singular
    verb, on the one line a human reads before granting a capability. `web`
    resolves to two concrete names, so a denial of it is where the agreement is
    reachable; `shell` resolves to one and must stay singular."""
    from agentloop.store import Store

    _seed_tool_request("shell")  # task 1, pending, resolves to one name
    store = Store("agentloop.db")
    try:
        web = store.tool_request_add(
            1,
            role="worker",
            agent_kind="worker",
            tool="web",
            status="pending",
            source="marker",
        )
        store.tool_request_decide(web, approved=False, by="human")
    finally:
        store.close()

    main(["tools", "reject", "1", "--note", "no shells"])
    capsys.readouterr()
    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "WebFetch and WebSearch are not available to this role" in out
    assert "Bash is not available to this role" in out  # one name stays singular


def test_cli_tools_list_on_an_empty_queue_says_so(capsys):
    rc = main(["tools", "list"])
    assert rc == 0
    assert "(no tool requests)" in capsys.readouterr().out


def test_cli_tools_list_shows_who_decided(capsys):
    _seed_tool_request()
    main(["tools", "approve", "1", "--note", "fine for this task"])
    capsys.readouterr()
    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "approved" in out
    assert "decided by human" in out
    assert "fine for this task" in out


def test_cli_tools_list_filters(capsys):
    first = _seed_tool_request("shell")
    second = _seed_tool_request("web", blocking=False, parked=False)
    assert first and second

    main(["tools", "list", "--task", "2"])
    only_second = capsys.readouterr().out
    assert "web" in only_second
    assert "shell" not in only_second

    # --pending hides a decided row: the queue is what still needs a human.
    main(["tools", "approve", "1"])
    capsys.readouterr()
    main(["tools", "list", "--pending"])
    pending_only = capsys.readouterr().out
    assert "web" in pending_only
    assert "shell" not in pending_only


def test_cli_tools_approve(capsys):
    _seed_tool_request()
    rc = main(["tools", "approve", "1", "--note", "fine"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "approved" in out
    assert "git" in out  # also granted
    assert "pending" in out  # the park was lifted
    # `parked` is read back after the decision, not reused from the row that was
    # decided: a release clears it, and the pre-decision value would report a
    # lifted park as still holding the task.
    assert "still parked" not in out


def test_cli_tools_reject(capsys):
    _seed_tool_request()
    rc = main(["tools", "reject", "1", "--note", "no shells"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rejected" in out
    assert "git" in out  # what stops working too
    assert "needs_human" in out  # rejection has no release path


def test_cli_tools_bad_id(capsys):
    for action in ("approve", "reject"):
        rc = main(["tools", action, "9999"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "9999" in err
        assert "Traceback" not in err


def test_cli_tools_deciding_twice_is_a_clean_error(capsys):
    """`tool_request_decide` accepts only a `pending` row. That ValueError is
    user input (two humans, or one double-click), not a crash."""
    _seed_tool_request()
    assert main(["tools", "approve", "1"]) == 0
    capsys.readouterr()
    rc = main(["tools", "reject", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.startswith("error: ")


def test_cli_tools_a_partly_live_capability_reports_both_halves(capsys):
    """The gate's subtraction is coarse — any overlap drops the whole logical
    name — so a row's own footprint can be split: a `file_io` ask (Read, Write,
    Edit) on a role that declares only `file_read` keeps `Read` and loses the
    other two. Reporting either half alone is a false sentence in one of the two
    directions this cycle is about."""
    import json

    from agentloop.store import Store

    json.dump({"max_tool_requests_per_task": 1}, open("loopconfig.json", "w"))
    json.dump(
        {
            "worker": {
                "role": "worker",
                "model": "mock",
                "system_prompt": "be brief",
                "tools": ["file_read", "search"],
            }
        },
        open("agents.json", "w"),
    )
    _seed_tool_request("web", blocking=False, parked=False)  # fills the cap of 1
    store = Store("agentloop.db")
    try:
        assert (
            store.tool_request_add(
                1,
                role="worker",
                agent_kind="worker",
                tool="file_io",
                status="pending",
                source="marker",
                max_per_task=1,
            )
            is None
        )
    finally:
        store.close()

    main(["tools", "list"])
    out = capsys.readouterr().out
    assert "Write and Edit are not available to this role" in out
    assert "it still has Read" in out


# -- batch eval mode (slice 6, Part 3) ----------------------------------------


@pytest.mark.parametrize("runner", ["claude", "openai"])
def test_batch_mode_refuses_a_non_mock_runner(runner, capsys):
    """Batch fixtures are scripted, so a real provider would be handed a script
    it cannot consume. Following the documented `--runner openai` precedent: a
    number that measured nothing is worse than none."""
    rc = main(["eval", "--mode", "batch", "--runner", runner])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "batch" in err and runner in err
    assert "Traceback" not in err


def test_batch_mode_with_the_mock_runner_writes_one_row(capsys, tmp_path):
    from agentloop.store import Store

    rc = main(["eval", "--mode", "batch"])
    assert rc == 0
    assert "Agreement with gold" in capsys.readouterr().out
    s = Store(tmp_path / "agentloop.db")
    try:
        rows = s.eval_runs()
        assert [r["kind"] for r in rows] == ["batch"]
    finally:
        s.close()


def test_eval_with_no_flags_still_runs_the_verdict_harness(capsys, tmp_path):
    from agentloop.store import Store

    rc = main(["eval"])
    assert rc == 0
    assert "Validator calibration" in capsys.readouterr().out
    s = Store(tmp_path / "agentloop.db")
    try:
        assert [r["kind"] for r in s.eval_runs()] == ["verdict"]
    finally:
        s.close()
