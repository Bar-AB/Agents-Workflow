"""Agent-requested tools with an auto-approval policy (slice 5).

Config knobs and domain types (phase 1), the `tool_requests` ledger and the
lease-release accessor (phase 2), the two pre-existing release paths the lease
repair fixes (phase 2b), and the `toolpolicy` seam itself (phase 3).
"""

import json
import threading
import time
from dataclasses import replace

import pytest

from agentloop.config import LoopConfig
from agentloop.models import (
    AgentSpec,
    Task,
    TaskStatus,
    ToolRequest,
    ToolRequestSource,
    ToolRequestStatus,
)
from agentloop.registry import DEFAULT_AGENTS, Registry
from agentloop.runner import MockRunner
from agentloop.store import Store
from agentloop.toolpolicy import (
    ParsedToolRequest,
    ToolClass,
    baseline_tools,
    classify,
    parse_tool_requests,
    tools_for,
)
from tests.test_loop import APPROVE, REVISE, SEVERE, add_task, make_loop
from tests.test_planner import PLAN_JSON


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _loop(store, outputs, registry=None, **cfg_overrides):
    """`tests.test_loop.make_loop`, plus the registry override several of these
    tests need (a gated declared list, a custom worker role, a small context
    budget). Kept local rather than widening the shared helper."""
    from pathlib import Path

    from agentloop.loop import Loop

    cfg_overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    config = LoopConfig(db_path=store.db_path, **cfg_overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, registry or Registry.load(), config), runner


def _registry_with_worker_tools(tools):
    """A registry whose worker declares `tools`, without touching the shared one.

    `Registry.load()` copies the *dict* but not the specs, so assigning to
    `registry.agents["worker"].tools` would mutate `DEFAULT_AGENTS` itself and
    leak into every later test in the session.
    """
    agents = dict(DEFAULT_AGENTS)
    agents["worker"] = replace(agents["worker"], tools=list(tools))
    return Registry(agents)


def _add(store, task_id, tool, **kw):
    """A marker-sourced request, with the fields a test rarely varies defaulted."""
    kw.setdefault("role", "worker")
    kw.setdefault("agent_kind", "worker")
    kw.setdefault("status", ToolRequestStatus.PENDING.value)
    kw.setdefault("source", ToolRequestSource.MARKER.value)
    return store.tool_request_add(task_id, tool=tool, **kw)


# -- phase 1: config knobs + domain types ----------------------------------


def test_tool_policy_config_defaults():
    cfg = LoopConfig()
    assert cfg.tool_readonly_allowlist == ["file_read", "search", "task_state"]
    assert cfg.max_tool_requests_per_task == 10


def test_gate_declared_tools_defaults_to_false():
    # Off by default: the shipped worker declares file_io + git, so gating
    # declared tools on a fresh install would gut it.
    assert LoopConfig().gate_declared_tools is False


def test_web_is_not_in_the_readonly_allowlist():
    # `web` reads remotely but egresses the prompt, and this project distrusts
    # egress by construction (runner._check_base_url's reasoning).
    assert "web" not in LoopConfig().tool_readonly_allowlist


def test_loopconfig_round_trips_the_new_knobs(tmp_path):
    path = tmp_path / "loopconfig.json"
    cfg = LoopConfig(
        tool_readonly_allowlist=["file_read"],
        gate_declared_tools=True,
        max_tool_requests_per_task=3,
    )
    path.write_text(cfg.to_json(), encoding="utf-8")
    # A typo'd field name would come back through `load`'s unknown-key warning
    # as a default rather than as the value written.
    loaded = LoopConfig.load(path)
    assert loaded.tool_readonly_allowlist == ["file_read"]
    assert loaded.gate_declared_tools is True
    assert loaded.max_tool_requests_per_task == 3
    assert "tool_readonly_allowlist" in json.loads(cfg.to_json())


def test_tool_request_defaults_are_the_safe_ones():
    req = ToolRequest(
        id=None, task_id=1, role="worker", agent_kind="worker", tool="shell"
    )
    # Every default is the one that grants nothing and moves no task.
    assert req.status is ToolRequestStatus.PENDING
    assert req.source is ToolRequestSource.MARKER
    assert req.blocking is False
    assert req.parked is False
    assert req.attempt_id is None
    assert req.decided_at is None
    assert isinstance(req.created_at, float)
    # str-Enums reach SQL and JSON as their values.
    assert ToolRequestStatus.AUTO.value == "auto"
    assert ToolRequestStatus.APPROVED.value == "approved"
    assert ToolRequestStatus.REJECTED.value == "rejected"
    assert ToolRequestStatus.REFUSED.value == "refused"
    assert ToolRequestSource.DECLARED.value == "declared"


# -- phase 2: the tool_requests ledger, its accessors, and release_claim ------


def test_a_grant_is_an_auto_or_approved_row(store):
    task = add_task(store)
    _add(store, task.id, "file_read", status=ToolRequestStatus.AUTO.value)
    _add(store, task.id, "shell")  # pending: an ask, not a grant
    rid = _add(store, task.id, "git")
    store.tool_request_decide(rid, approved=True, by="human")
    store.tool_request_decide(
        _add(store, task.id, "file_io"), approved=False, by="human"
    )

    # There is no separate grant object: auto/approved rows *are* the grants.
    assert store.granted_tools(task.id, "worker") == ["file_read", "git"]


def test_tool_request_add_pairs_the_row_with_its_event(store):
    task = add_task(store)
    pending_id = _add(store, task.id, "shell", reason="need to run a build")
    auto_id = _add(store, task.id, "file_read", status=ToolRequestStatus.AUTO.value)
    refused_id = _add(
        store,
        task.id,
        "teleport",
        status=ToolRequestStatus.REFUSED.value,
        why="unknown logical tool",
    )

    by_kind = {e["kind"]: e["payload"] for e in store.events(task.id)}
    assert by_kind["tool_requested"]["request_id"] == pending_id
    assert by_kind["tool_requested"]["tool"] == "shell"
    assert by_kind["tool_requested"]["reason"] == "need to run a build"
    assert by_kind["tool_requested"]["agent_kind"] == "worker"
    assert by_kind["tool_requested"]["role"] == "worker"
    assert by_kind["tool_requested"]["source"] == "marker"
    assert by_kind["tool_requested"]["upgraded"] is False
    assert by_kind["tool_auto_approved"]["request_id"] == auto_id
    assert by_kind["tool_auto_approved"]["status"] == "auto"
    assert by_kind["tool_request_refused"]["request_id"] == refused_id
    assert by_kind["tool_request_refused"]["why"] == "unknown logical tool"


def test_a_pending_row_upgrades_from_optional_to_blocking(store):
    task = add_task(store)
    first = _add(store, task.id, "shell", blocking=False)
    again = _add(store, task.id, "shell", blocking=True)

    # One row, upgraded in place — an INSERT OR IGNORE would silently drop the
    # upgrade and a task that should park would never park.
    assert again == first
    rows = store.tool_requests(task_id=task.id)
    assert len(rows) == 1
    assert rows[0].blocking is True
    upgrades = [
        e["payload"]
        for e in store.events(task.id)
        if e["kind"] == "tool_requested" and e["payload"]["upgraded"]
    ]
    assert len(upgrades) == 1
    assert upgrades[0]["request_id"] == first
    # And the reverse is not an upgrade: blocking is never lowered.
    assert _add(store, task.id, "shell", blocking=False) is None
    assert store.tool_request_get(first).blocking is True


@pytest.mark.parametrize("decided", ["approved", "rejected", "refused"])
def test_a_decided_row_is_never_upgraded(store, decided):
    task = add_task(store)
    if decided == "refused":
        rid = _add(store, task.id, "shell", status=ToolRequestStatus.REFUSED.value)
    else:
        rid = _add(store, task.id, "shell")
        store.tool_request_decide(rid, approved=(decided == "approved"), by="human")
    before = len(store.events(task.id))

    assert _add(store, task.id, "shell", blocking=True) is None
    assert store.tool_request_get(rid).blocking is False
    assert store.tool_request_get(rid).status.value == decided
    assert len(store.events(task.id)) == before  # no row change, no event


def test_an_unknown_logical_tool_is_refused(store):
    task = add_task(store)
    rid = _add(
        store,
        task.id,
        "teleport",
        status=ToolRequestStatus.REFUSED.value,
        why="not a known logical tool",
    )

    assert store.tool_request_get(rid).status is ToolRequestStatus.REFUSED
    # A refused row is never a grant, whatever the agent asks next.
    assert store.granted_tools(task.id, "worker") == []


def test_the_per_task_cap_refuses_once(store):
    task = add_task(store)
    for i in range(3):
        assert _add(store, task.id, f"tool{i}", max_per_task=3) is not None

    # Over the cap: refused and audited, never silently dropped.
    assert _add(store, task.id, "shell", max_per_task=3) is None
    refused = [r for r in store.tool_requests(task_id=task.id) if r.tool == "shell"]
    assert len(refused) == 1
    assert refused[0].status is ToolRequestStatus.REFUSED
    # Repeats are absorbed by the UNIQUE row: refused *once*, not once per marker.
    for _ in range(4):
        assert _add(store, task.id, "shell", max_per_task=3) is None
    events = [e for e in store.events(task.id) if e["kind"] == "tool_request_refused"]
    assert len(events) == 1


def test_the_cap_counts_only_rows_someone_can_still_act_on(store):
    """The cap bounds the *queue a human has to clear*, so its own refusals must
    not fill it. Counting them made a task that had refused `max_per_task` rows
    unable to record anything further — including a read-only request the policy
    says needs no human at all, which came back `refused` with no grant possible
    on that task for the rest of its life."""
    task = add_task(store)
    for i in range(10):
        _add(store, task.id, f"junk{i}", status=ToolRequestStatus.REFUSED.value)

    auto = _add(
        store,
        task.id,
        "file_read",
        status=ToolRequestStatus.AUTO.value,
        max_per_task=10,
    )
    assert auto is not None
    assert store.tool_request_get(auto).status is ToolRequestStatus.AUTO
    assert store.granted_tools(task.id, "worker") == ["file_read"]


def test_the_cap_counts_only_the_queue_a_human_still_has_to_clear(store):
    """`auto`, `approved` and `rejected` are *decided*: nobody can act on them, so
    counting them is the same dead end refusing rows was, one status along.

    It is reachable, not theoretical. `UNIQUE(task_id, role, tool)` is per role, so
    three roles over `LOGICAL_TOOL_MAP`'s seven names give 21 possible rows — and
    `gate_declared_tools=True` writes a declared row per role per tool on its own.
    Ten decided rows on one task therefore happens, and past that point every
    further ask came back `refused` for the life of the task, `file_read`
    included: a read-only tool the policy says needs no human at all, permanently
    ungrantable because ten already-answered questions were still counted as
    unanswered."""
    task = add_task(store)
    decided = (
        ToolRequestStatus.AUTO.value,
        ToolRequestStatus.APPROVED.value,
        ToolRequestStatus.REJECTED.value,
    )
    for i in range(10):
        assert (
            _add(store, task.id, f"answered{i}", status=decided[i % 3], max_per_task=10)
            is not None
        )

    # A read-only ask still auto-approves, and a blocking one is still visible to
    # the park check rather than buried as `refused` where nothing reads it.
    auto = _add(
        store,
        task.id,
        "file_read",
        status=ToolRequestStatus.AUTO.value,
        max_per_task=10,
    )
    assert auto is not None
    assert store.tool_request_get(auto).status is ToolRequestStatus.AUTO
    assert "file_read" in store.granted_tools(task.id, "worker")
    assert _add(store, task.id, "shell", blocking=True, max_per_task=10) is not None
    assert [r.tool for r in store.pending_blocking_tool_requests(task.id)] == ["shell"]

    # The bound is still a bound: it is the *pending* queue that fills up.
    for i in range(9):
        assert _add(store, task.id, f"queued{i}", max_per_task=10) is not None
    assert _add(store, task.id, "git", max_per_task=10) is None
    assert store.tool_request_get(auto).status is ToolRequestStatus.AUTO


def test_the_per_task_cap_bounds_distinct_tools_to_one_refusal_each(store):
    """The UNIQUE row absorbs *repeats* of one tool; distinct names each get
    their own row. So the cap's honest promise is "at most `max_per_task`
    actionable rows, plus one audited refusal per distinct name", not "at most
    `max_per_task` rows" — and `config.py` now says so."""
    task = add_task(store)
    for i in range(3):
        assert _add(store, task.id, f"tool{i}", max_per_task=3) is not None

    for over in ("shell", "git", "web"):
        assert _add(store, task.id, over, max_per_task=3) is None
        # And asking again is absorbed by the row, not re-audited.
        assert _add(store, task.id, over, max_per_task=3) is None

    rows = store.tool_requests(task_id=task.id)
    pending = [r for r in rows if r.status is ToolRequestStatus.PENDING]
    refused = [r for r in rows if r.status is ToolRequestStatus.REFUSED]
    assert len(pending) == 3, "the actionable queue stays at the cap"
    assert sorted(r.tool for r in refused) == ["git", "shell", "web"]
    events = [e for e in store.events(task.id) if e["kind"] == "tool_request_refused"]
    assert len(events) == 3, "one refusal event per distinct name, not per marker"


def test_an_over_cap_refusal_is_audited_as_the_cap(store):
    """`why` used to keep the caller's reason on the over-cap path, so a row
    refused *for the cap* could be audited as "not a known logical tool name" —
    a pair `tools_for` produces on its own."""
    task = add_task(store)
    _add(store, task.id, "shell", max_per_task=1)
    assert (
        _add(
            store,
            task.id,
            "teleport",
            max_per_task=1,
            why="not a known logical tool name",
        )
        is None
    )

    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_refused"]
    assert event["payload"]["why"].startswith("over the per-task cap of 1")
    # The caller's reason is kept alongside, never in place of, the cap's.
    assert "not a known logical tool name" in event["payload"]["why"]


def test_an_out_of_enum_status_cannot_poison_the_ledger(store):
    """A status outside `ToolRequestStatus` used to insert happily and then make
    `tool_requests()` and `tool_request_get()` raise *forever*, while
    `granted_tools` skipped the row and `task_metrics` still counted it.

    Coerced rather than rejected: this accessor runs inside `_invoke`'s closing
    transaction over an already-paid `finish_attempt`, so a raise here (or a DDL
    `CHECK`) would discard tokens the provider billed and the retry would buy
    them again. `refused` is the fail-safe landing place — it grants nothing and
    parks nothing — and the original value rides the event.
    """
    task = add_task(store)
    rid = _add(store, task.id, "shell", status="Pending")

    row = store.tool_request_get(rid)
    assert row.status is ToolRequestStatus.REFUSED
    assert [r.id for r in store.tool_requests(task_id=task.id)] == [rid]
    assert store.granted_tools(task.id, "worker") == []
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_refused"]
    assert "Pending" in event["payload"]["why"]


def test_granted_tools_is_scoped_to_task_and_role(store):
    task_a = add_task(store)
    task_b = add_task(store)
    _add(store, task_a.id, "shell", status=ToolRequestStatus.APPROVED.value)
    _add(
        store,
        task_a.id,
        "git",
        role="validator",
        agent_kind="validator",
        status=ToolRequestStatus.APPROVED.value,
    )

    assert store.granted_tools(task_a.id, "worker") == ["shell"]
    assert store.granted_tools(task_a.id, "validator") == ["git"]
    assert store.granted_tools(task_b.id, "worker") == []


def test_pending_blocking_tool_requests_ignores_optional_and_decided_rows(store):
    task = add_task(store)
    blocking = _add(store, task.id, "shell", blocking=True)
    _add(store, task.id, "web", blocking=False)
    decided = _add(store, task.id, "git", blocking=True)
    store.tool_request_decide(decided, approved=True, by="human")
    _add(
        store,
        task.id,
        "teleport",
        blocking=True,
        status=ToolRequestStatus.REFUSED.value,
    )

    assert [r.id for r in store.pending_blocking_tool_requests(task.id)] == [blocking]


def test_pending_blocking_tool_requests_parked_only_filters_unparked_rows(store):
    task = add_task(store)
    parked = _add(store, task.id, "shell", blocking=True)
    unparked = _add(store, task.id, "git", blocking=True)
    store.tool_requests_mark_parked(task.id, [parked])

    assert [r.id for r in store.pending_blocking_tool_requests(task.id)] == [
        parked,
        unparked,
    ]
    assert [
        r.id for r in store.pending_blocking_tool_requests(task.id, parked_only=True)
    ] == [parked]


def test_marking_and_clearing_parked_is_scoped_to_the_named_rows(store):
    task = add_task(store)
    a = _add(store, task.id, "shell", blocking=True)
    b = _add(store, task.id, "git", blocking=True)
    c = _add(store, task.id, "web", blocking=True)
    other = add_task(store)
    elsewhere = _add(store, other.id, "shell", blocking=True)

    store.tool_requests_mark_parked(task.id, [a, b])
    assert store.tool_request_get(a).parked is True
    assert store.tool_request_get(b).parked is True
    assert store.tool_request_get(c).parked is False  # not named, not stamped
    assert store.tool_request_get(elsewhere).parked is False  # another task

    store.tool_requests_clear_parked(task.id)
    assert [store.tool_request_get(r).parked for r in (a, b, c)] == [False] * 3
    # Clearing is per task; a peer task's park is its own live fact.
    store.tool_requests_mark_parked(other.id, [elsewhere])
    store.tool_requests_clear_parked(task.id)
    assert store.tool_request_get(elsewhere).parked is True


def test_marking_parked_refuses_an_id_set_it_cannot_stamp(store):
    """`release_claim` checks its `rowcount`; this must too. A mis-scoped id
    silently stamped nothing, the park proceeded anyway, and the release then
    found no parked row — so approving the very request the task was held on
    would leave it at NEEDS_HUMAN forever."""
    task = add_task(store)
    mine = _add(store, task.id, "shell", blocking=True)
    other = add_task(store)
    strangers = _add(store, other.id, "git", blocking=True)

    with pytest.raises(ValueError):
        store.tool_requests_mark_parked(task.id, [mine, strangers])
    # And nothing was stamped: the park is refused whole, not half-applied.
    assert store.tool_request_get(mine).parked is False
    assert store.tool_request_get(strangers).parked is False


def test_tool_request_add_never_writes_parked(store):
    task = add_task(store)
    ids = [
        _add(store, task.id, "shell", blocking=True),
        _add(store, task.id, "file_read", status=ToolRequestStatus.AUTO.value),
        _add(store, task.id, "teleport", status=ToolRequestStatus.REFUSED.value),
    ]
    # Only the park writes `parked`; a fresh row is never born parked, whatever
    # its status or blocking flag.
    assert [store.tool_request_get(i).parked for i in ids] == [False] * 3


def test_deciding_twice_is_refused(store):
    task = add_task(store)
    rid = _add(store, task.id, "shell")
    decided = store.tool_request_decide(rid, approved=True, by="human", note="ok")

    assert decided.status is ToolRequestStatus.APPROVED
    assert decided.decided_by == "human"
    assert decided.decided_note == "ok"
    assert isinstance(decided.decided_at, float)
    with pytest.raises(ValueError):
        store.tool_request_decide(rid, approved=False, by="human")
    with pytest.raises(KeyError):
        store.tool_request_decide(9999, approved=True, by="human")


def test_release_claim_clears_the_lease_and_the_task_is_claimable(store):
    task = add_task(store)
    claimed = store.claim_next_task("loop")
    assert claimed.id == task.id and claimed.claimed_by == "loop"

    # A release that only writes the status is the documented bug: the row is
    # PENDING with its lease still set, so the compare-and-swap can never match.
    fresh = store.get_task(task.id)
    store.set_status(fresh, TaskStatus.PENDING, reason="")
    assert store.claim_next_task("loop") is None  # control: the bug is real

    store.release_claim(task.id)
    assert store.get_task(task.id).claimed_by is None
    reclaimed = store.claim_next_task("loop")
    assert reclaimed is not None and reclaimed.id == task.id


def test_release_claim_logs_claim_released(store):
    task = add_task(store)
    store.claim_next_task("loop")
    store.release_claim(task.id)

    released = [e for e in store.events(task.id) if e["kind"] == "claim_released"]
    assert len(released) == 1
    assert released[0]["payload"]["worker"] == "loop"


def test_release_claim_on_an_unclaimed_task_is_a_silent_no_op(store):
    task = add_task(store)
    store.release_claim(task.id)  # never raises

    assert store.get_task(task.id).claimed_by is None
    # The log never claims a release that did not happen.
    assert [e for e in store.events(task.id) if e["kind"] == "claim_released"] == []


def test_tool_request_timestamps_are_real_not_text(store):
    types = {
        r["name"]: r["type"]
        for r in store._conn.execute("PRAGMA table_info(tool_requests)").fetchall()
    }
    assert types["created_at"] == "REAL"
    assert types["decided_at"] == "REAL"

    task = add_task(store)
    rid = _add(store, task.id, "shell")
    row = store.tool_request_get(rid)
    assert isinstance(row.created_at, float)
    assert row.decided_at is None
    decided = store.tool_request_decide(rid, approved=True, by="human")
    assert isinstance(decided.decided_at, float)


def test_task_metrics_exposes_tool_requests(store):
    task = add_task(store)
    _add(store, task.id, "shell", blocking=True, reason="build")
    other = add_task(store)
    _add(store, other.id, "git")

    requests = store.task_metrics(task.id)["tool_requests"]
    assert [r["tool"] for r in requests] == ["shell"]
    assert requests[0]["status"] == "pending"
    assert requests[0]["blocking"] == 1
    # JSON-serializable: this SELECT is the only tool data the dashboard gets.
    json.dumps(requests)


def test_run_metrics_counts_pending_tool_requests(store):
    task = add_task(store)
    assert store.run_metrics()["pending_tool_requests"] == 0
    a = _add(store, task.id, "shell")
    _add(store, task.id, "git")
    _add(store, task.id, "file_read", status=ToolRequestStatus.AUTO.value)
    assert store.run_metrics()["pending_tool_requests"] == 2

    store.tool_request_decide(a, approved=True, by="human")
    assert store.run_metrics()["pending_tool_requests"] == 1


def test_a_new_table_needs_no_migrate_entry(store):
    task = add_task(store)
    _add(store, task.id, "shell")
    # A whole new table needs no _migrate() entry: CREATE TABLE IF NOT EXISTS in
    # _SCHEMA covers it, on a database that already holds other work.
    store._conn.write("DROP TABLE tool_requests")

    reopened = Store(store.db_path)
    try:
        assert reopened.tool_requests(task_id=task.id) == []
        rid = _add(reopened, task.id, "git", blocking=True)
        assert reopened.tool_request_get(rid).tool == "git"
        assert [r.id for r in reopened.pending_blocking_tool_requests(task.id)] == [rid]
    finally:
        reopened.close()


# -- phase 2b: every exit from the parked state clears the flag ---------------


def test_a_redo_clears_the_parked_flags(store):
    """`human_redo` ends the parked state without deciding the request, so the
    live "the loop is holding this task on this row" fact has to go. Left standing,
    it would satisfy the release predicate on a *later*, unrelated escalation —
    and four of those (budget cap, `ESCALATE:`, empty output, infra/config error)
    fire before the park check ever runs."""
    task = add_task(store)
    rid = _add(store, task.id, "shell", blocking=True)
    store.tool_requests_mark_parked(task.id, [rid])
    loop, _ = make_loop(store, ["out"])

    loop.human_redo(task.id)
    row = store.tool_request_get(rid)
    assert row.parked is False
    # The row itself survives, undecided: the blocking need is still unmet, so
    # the next round parks again — the flag is cleared, not the request.
    assert row.status is ToolRequestStatus.PENDING
    assert row.blocking is True


def test_a_resume_from_paused_clears_the_parked_flags(store):
    """A pause and resume reaches the same "no longer parked, nothing decided"
    state as a redo, by a route that touches no `tool_requests` row."""
    task = add_task(store)
    rid = _add(store, task.id, "shell", blocking=True)
    store.tool_requests_mark_parked(task.id, [rid])
    loop, _ = make_loop(store, ["out"])
    loop.pause(task.id)

    loop.resume(task.id)
    row = store.tool_request_get(rid)
    assert row.parked is False
    assert row.status is ToolRequestStatus.PENDING
    assert row.blocking is True


def test_pausing_a_parked_task_keeps_the_flag_until_the_resume(store):
    """`pause` is the one caller `tool_requests_clear_parked` deliberately omits,
    and the omission is now asserted rather than merely implied by its absence.

    A pause is not an exit from the park, it is a suspension of it: the loop
    stopped this task on this row and has not resumed past it, which is what the
    flag means. Clearing it here would make `resume`'s clear unreachable in
    practice — PAUSED is reachable only through `pause` — and would leave
    `test_a_resume_from_paused_clears_the_parked_flags` passing by measuring
    `pause` instead of `resume`. What keeps a paused task un-releasable is the
    release predicate's own `status == NEEDS_HUMAN` term, not this flag."""
    task, rid = _parked_task(store)
    loop, _ = make_loop(store, ["out"])

    loop.pause(task.id)
    assert store.get_task(task.id).status is TaskStatus.PAUSED
    assert store.tool_request_get(rid).parked is True, (
        "a paused task is still stopped on this row; the flag records that"
    )

    # And no route out of PAUSED leaves it behind: `resume` is in the named set.
    loop.resume(task.id)
    assert store.get_task(task.id).status is TaskStatus.PENDING
    assert store.tool_request_get(rid).parked is False


def _parked_task(store, status=TaskStatus.NEEDS_HUMAN):
    """A task the loop is holding at `status` on one blocking request."""
    task = add_task(store)
    rid = _add(store, task.id, "shell", blocking=True)
    store.tool_requests_mark_parked(task.id, [rid])
    if status is not TaskStatus.PENDING:
        store.set_status(task, status, reason="parked awaiting tool approval")
    return task, rid


@pytest.mark.parametrize("exit_", ["approve", "reject", "abort"])
def test_a_terminal_exit_clears_the_parked_flags(store, exit_):
    """`parked=1` means "the loop is holding this task at NEEDS_HUMAN right now,
    on this row". On a done/failed/aborted task that sentence is simply false, so
    clearing it is correct by definition rather than defensively — and it stops
    the invariant resting entirely on a release predicate no earlier phase
    enforces."""
    task, rid = _parked_task(store)
    loop, _ = make_loop(store, ["out"])

    getattr(
        loop, {"approve": "human_approve", "reject": "human_reject"}.get(exit_, "abort")
    )(task.id)

    assert store.get_task(task.id).status in loop._TERMINAL
    row = store.tool_request_get(rid)
    assert row.parked is False
    # The ask itself is not decided by a task-level exit: a human may still
    # legitimately grant it before a redo.
    assert row.status is ToolRequestStatus.PENDING


def test_resume_on_a_parked_needs_human_task_leaves_the_flag_standing(store):
    """The control case for `resume`'s two hoisted calls. Both are deliberately
    inside the `PAUSED` branch: a NEEDS_HUMAN task is still parked *now*, so
    clearing the flag there would strip the fact the release reads while the loop
    is still holding the task. Without this test the placement of
    `tool_requests_clear_parked` is unasserted (only the lease half is pinned, by
    `test_resuming_a_live_in_flight_task_keeps_its_lease`)."""
    task, rid = _parked_task(store)
    loop, _ = make_loop(store, ["out"])

    loop.resume(task.id)

    assert store.get_task(task.id).status is TaskStatus.NEEDS_HUMAN
    assert store.tool_request_get(rid).parked is True


def test_a_resume_is_one_transaction(store, monkeypatch):
    """The release, the flag clear and the status write are one commit.

    As three commits a crash between them left `parked=0` against the old status
    — the fail-safe direction, but both accessors document "no event of its own"
    *on the grounds that their caller writes a status event in the same
    transaction*, and phase 5's release is specified to copy this shape."""
    task, rid = _parked_task(store, status=TaskStatus.PENDING)
    claimed = store.claim_next_task("loop")
    assert claimed.claimed_by == "loop"
    loop, _ = make_loop(store, ["out"])
    loop.pause(task.id)

    monkeypatch.setattr(store, "set_status", _boom)
    with pytest.raises(RuntimeError):
        loop.resume(task.id)

    assert store.tool_request_get(rid).parked is True
    assert store.get_task(task.id).claimed_by == "loop"


def test_a_redo_is_one_transaction(store, monkeypatch):
    task, rid = _parked_task(store, status=TaskStatus.PENDING)
    store.claim_next_task("loop")
    loop, _ = make_loop(store, ["out"])

    monkeypatch.setattr(store, "set_status", _boom)
    with pytest.raises(RuntimeError):
        loop.human_redo(task.id)

    assert store.tool_request_get(rid).parked is True
    assert store.get_task(task.id).claimed_by == "loop"


def _boom(*_a, **_k):
    raise RuntimeError("crash between the release and the status write")


def test_a_redo_that_takes_a_live_workers_lease_says_so(store, recwarn):
    """The residual in `human_redo`'s lease release, recorded rather than fixed.

    A redo cannot distinguish a stranded claim from a live second process — and
    `README.md` promises a redo *recovers* a stranded claim, which is in-flight by
    definition, so refusing an in-flight redo would break the recovery the fix
    exists to provide. A human redoing an in-flight task **is** the human
    asserting the worker is gone; the trail therefore records that assertion.
    """
    task = add_task(store)
    store.claim_next_task("loop")  # -> in_progress, leased
    loop, _ = make_loop(store, ["out"])

    loop.human_redo(task.id)

    forced = [
        e for e in store.events(task.id) if e["kind"] == "claim_taken_from_worker"
    ]
    assert len(forced) == 1
    assert forced[0]["payload"]["worker"] == "loop"
    assert forced[0]["payload"]["status"] == "in_progress"
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn)


def test_a_redo_of_a_parked_task_is_not_taking_a_live_lease(store):
    """The non-vacuity control: the warning is about *in-flight* work. A parked
    NEEDS_HUMAN task also holds its lease (the park does not release it), and a
    redo there takes nothing from anybody."""
    task, _ = _parked_task(store, status=TaskStatus.PENDING)
    store.claim_next_task("loop")
    store.set_status(task, TaskStatus.NEEDS_HUMAN, reason="parked")
    loop, _ = make_loop(store, ["out"])

    loop.human_redo(task.id)

    kinds = [e["kind"] for e in store.events(task.id)]
    assert "claim_taken_from_worker" not in kinds
    assert "claim_released" in kinds  # the lease was still handed back


# -- phase 3: the toolpolicy seam --------------------------------------------


def _spec(tools, role="worker"):
    return AgentSpec(role=role, model="mock", system_prompt="be brief", tools=tools)


def test_classify_readonly_is_auto():
    cfg = LoopConfig()
    for tool in cfg.tool_readonly_allowlist:
        assert classify(tool, cfg) is ToolClass.AUTO


def test_classify_side_effecting_is_gated():
    cfg = LoopConfig()
    for tool in ("file_io", "git", "shell", "web"):
        assert classify(tool, cfg) is ToolClass.GATED


def test_classify_unknown_logical_name_is_unknown():
    # A name that maps to no SDK tool: granting it would record a permission
    # that means nothing.
    assert classify("teleport", LoopConfig()) is ToolClass.UNKNOWN
    # And the allowlist is a project's judgment, so widening it moves the line.
    assert classify("shell", LoopConfig(tool_readonly_allowlist=["shell"])) is (
        ToolClass.AUTO
    )


def test_parse_marker_variants():
    text = "\n".join(
        [
            "Some preamble.",
            "TOOL_REQUEST: shell (blocking) - need to run the build",
            "tool_request: git (OPTIONAL) — commit the fix",
            "TOOL_REQUEST: file_read (Blocking): read the config",
            "  TOOL_REQUEST: web (optional) fetch the changelog",
            "TOOL_REQUEST: task_state",
            "trailing prose",
        ]
    )
    parsed = parse_tool_requests(text)

    assert [(p.tool, p.blocking, p.reason) for p in parsed] == [
        ("shell", True, "need to run the build"),
        ("git", False, "commit the fix"),
        ("file_read", True, "read the config"),
        ("web", False, "fetch the changelog"),
        ("task_state", False, ""),
    ]


@pytest.mark.parametrize(
    "newline",
    ["\n", "\r\n", "\r", " ", "\x85"],
    ids=["lf", "crlf", "cr", "line-separator", "nel"],
)
def test_parse_marker_variants_are_line_ending_agnostic(newline):
    """CRLF is a mainstream input (this project is developed on Windows), and
    under `re.MULTILINE` `$` matches before `\\n` but **not** before `\\r`. So the
    shortest form the grammar permits — no flag, no reason, the one an agent
    asking for a read-only tool actually types — parsed under LF and vanished
    under CRLF, with nothing in the ledger and nothing in the audit log: the
    silent withholding the module docstring says it must not have.

    Real expected values, not a totality assertion: `test_parse_tool_requests_is_
    total` already *contained* `"TOOL_REQUEST: shell\\r\\n"` and asserted only
    `isinstance(result, list)`, so it passed while returning `[]`. That shape is
    what hid this.

    Parametrized over the whole class, not just the two cases found by hand. The
    first fix for this was `\\r?$` in the lookahead, which is a fix per terminator
    someone remembers: a lone `\\r`, ` ` and `\\x85` are line breaks to
    `str.splitlines` and not to `$`, and each was still dropping a well-formed
    marker afterwards. `parse_tool_requests` now normalizes line boundaries
    through `splitlines` before matching, which is why every id below passes
    identically — and why a sixth terminator needs no sixth fix.
    """
    text = newline.join(
        [
            "Some preamble.",
            "TOOL_REQUEST: shell (blocking) - need to run the build",
            "TOOL_REQUEST: file_read",
            "TOOL_REQUEST: task_state (optional)",
            "trailing prose",
        ]
    )

    assert [(p.tool, p.blocking, p.reason) for p in parse_tool_requests(text)] == [
        ("shell", True, "need to run the build"),
        ("file_read", False, ""),
        ("task_state", False, ""),
    ]


@pytest.mark.parametrize("role", ["worker", "validator", "planner"])
def test_the_shipped_prompt_teaches_the_marker_grammar(role):
    """Without this the whole slice is dead on live traffic.

    Nothing told an agent it *may* ask for a tool, or how — `grep TOOL_REQUEST
    agentloop/registry.py` had no hits — so the only markers that would ever
    appear in production output are quoted ones. Every test passed regardless,
    because the tests script the replies.

    Not just "the string is present": the prompt's own worked example is fed to
    `parse_tool_requests`, so the grammar the prompt teaches is the grammar the
    parser accepts. Documented-but-unparseable is the same dead feature with a
    paragraph in front of it.
    """
    spec = DEFAULT_AGENTS[role]
    assert "TOOL_REQUEST" in spec.system_prompt
    assert [p.tool for p in parse_tool_requests(spec.system_prompt)] == ["shell"]
    # The prompt states what a blocking ask costs, since that is the one thing an
    # agent cannot discover by trying it.
    assert "blocking" in spec.system_prompt
    # v3: the roles that may author a request are told the grammar. A hand-edited
    # agents.json pinned at "2" still parses markers; it just never emits one.
    assert spec.version == "3"


def test_the_summarizer_is_never_taught_the_grammar():
    """`_MARKER_AGENT_KINDS` excludes the summarizer because its output
    compresses a transcript that may quote a marker, so parsing it would
    manufacture a request out of a quotation. Teaching it the grammar would
    invite exactly the output that is never read."""
    assert "TOOL_REQUEST" not in DEFAULT_AGENTS["summarizer"].system_prompt
    assert DEFAULT_AGENTS["summarizer"].version == "1"


@pytest.mark.parametrize(
    "prefix",
    ["", "- ", "* ", "+ ", "1. ", "2) ", "> ", "  - ", "**", "-", ">"],
    ids=[
        "plain",
        "dash-bullet",
        "star-bullet",
        "plus-bullet",
        "numbered-dot",
        "numbered-paren",
        "blockquote",
        "indented-bullet",
        "bold",
        "tight-dash",
        "tight-quote",
    ],
)
def test_a_markdown_wrapped_marker_is_still_a_request(prefix):
    """**LLM output is markdown.** An agent enumerating two capability needs
    writes them as a bullet list, and `^[ \\t]*TOOL_REQUEST` matched none of it:
    `- TOOL_REQUEST: shell (blocking) - …` parsed to `[]`, so no row, no event,
    a capability withheld with nothing in the ledger — the CRLF defect's family,
    failing in the direction the module docstring rules out.

    Under the park rule it is strictly worse than a withheld tool: a dropped
    `(blocking)` ask means the park never fires, so "I cannot finish without
    this" silently becomes "continue without it and tell no human".

    Real expected values per wrapper, not a totality assertion — the totality
    shape is exactly what hid CRLF for a cycle.
    """
    text = f"Here is what I need:\n{prefix}TOOL_REQUEST: shell (blocking) - run it\n"

    assert [(p.tool, p.blocking, p.reason) for p in parse_tool_requests(text)] == [
        ("shell", True, "run it")
    ]


def test_a_planner_blocking_request_records_a_row_and_parks_nothing(store):
    """M9: the decision rule reads as though it applied to every agent. It does
    not — the park check lives in `run_task`, which is never called on a
    `kind='plan'` row, while `Loop.plan` passes the same config and so *does*
    create the row. The direction is safe; the docstring was what was wrong.
    Pinned so a later phase that moves the check knows a rule depends on where it
    sits."""
    loop, _ = _loop(
        store,
        [PLAN_JSON + "\nTOOL_REQUEST: shell (blocking) - to inspect the repo"],
        plan_requires_approval=False,
    )
    plan = loop.plan("Ship a thing", "It works")

    # The row exists, blocking and undecided — the ask is on the record.
    (row,) = store.tool_requests(task_id=plan.id)
    assert row.blocking is True and row.status is ToolRequestStatus.PENDING
    assert row.agent_kind == "planner"
    # And nothing parked: the plan completed and its children exist.
    assert row.parked is False
    assert plan.status is TaskStatus.DONE
    assert store.plan_tasks(plan.id)


def test_a_fenced_marker_is_a_live_request():
    """The deliberate judgment, pinned positively.

    Its only appearance was one line inside `test_parse_tool_requests_is_total`,
    where the assertions are `isinstance(result, list)` — so it passed while
    returning `[]`, which is the exact shape that hid the CRLF defect for a whole
    cycle. Understanding markdown fences is not this parser's job (a fence-aware
    grammar is a second, lossy model of the reply that can drop a genuine ask), so
    a fenced marker counts. Deliberate behavior gets an assertion, not a mention.
    """
    fenced = "Here is the form:\n```\nTOOL_REQUEST: shell (blocking) - example\n```\n"

    assert [(p.tool, p.blocking) for p in parse_tool_requests(fenced)] == [
        ("shell", True)
    ]


def test_a_fenced_blocking_example_really_parks_the_task(store):
    """And what that judgment costs, at the level where it is paid: a quoted
    `(blocking)` example stops the task for a human. Stated in the park's own
    comment as the accepted price; asserted here so the price is measured rather
    than assumed."""
    task = add_task(store)
    reply = (
        "worker out\n```\nTOOL_REQUEST: shell (blocking) - just quoting the form\n```"
    )
    loop, _ = _loop(store, [reply, APPROVE])
    loop.run_task(task)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert "shell" in stored.escalation_reason
    # What the park itself preserves — the output and the revision budget. What
    # *recovering* costs is a separate question with a less comfortable answer;
    # see the park's comment in `loop.py`, and `test_pause_then_resume_is_the_
    # neutral_way_out_of_a_false_park` below.
    assert stored.output == reply
    assert stored.revision_count == 0


def test_pause_then_resume_is_the_neutral_way_out_of_a_false_park(store):
    """The recovery the park's comment now names, measured rather than claimed.

    The comment used to say a false park "keeps the output, the workspace and the
    revision budget", which was true of the park and false of every documented way
    out of it: approve *grants* the quoted tool, reject is a dead end by design,
    reject/abort on the task are terminal, and a redo wipes the workspace and
    resets output and revision_count — the three things named. `pause` +
    `resume` decides nothing and keeps all three, so it is the one that had to be
    written down.
    """
    loop, _, task, row = _parked(store)
    before = store.get_task(task.id)
    assert before.output and before.revision_count == 0

    loop.pause(task.id)
    resumed = loop.resume(task.id)

    assert resumed.status is TaskStatus.PENDING
    assert resumed.output == before.output  # kept
    assert resumed.revision_count == before.revision_count  # kept
    assert resumed.claimed_by is None
    # Nothing was decided: the request is still undecided and still on the queue.
    assert store.tool_request_get(row.id).status is ToolRequestStatus.PENDING
    assert store.tool_request_get(row.id).parked is False
    assert store.claim_next_task("loop").id == task.id


def test_prose_before_a_marker_is_still_not_a_request():
    """The control on the wrapper tolerance: it admits list, quote and emphasis
    punctuation, never arbitrary text. Without this the anchor could be dropped
    altogether and every parametrized case above would still pass — while
    `TOOL_REQUEST:` quoted mid-sentence became a live request."""
    assert parse_tool_requests("please TOOL_REQUEST: shell - not anchored") == []
    assert parse_tool_requests("see - TOOL_REQUEST: shell - still not anchored") == []
    assert parse_tool_requests("the tool: TOOL_REQUEST: shell") == []


def test_a_marker_tool_name_is_matched_case_insensitively_and_normalized():
    """The label and the flag are case-insensitive, so the *name* matches that
    way too — but `classify` and `LOGICAL_TOOL_MAP` are case-sensitive, so a
    matched `File_Read` used to be audited as a request for a tool nothing maps.
    A regex that advertises a tolerance the classifier lacks is worse than one
    that rejects: normalize what was captured."""
    (parsed,) = parse_tool_requests("TOOL_REQUEST: File_Read (Optional) - read it")
    assert parsed.tool == "file_read"
    assert classify(parsed.tool, LoopConfig()) is ToolClass.AUTO
    # And a duplicate differing only in case is the same request, not two.
    both = parse_tool_requests("TOOL_REQUEST: SHELL (optional)\nTOOL_REQUEST: shell")
    assert [p.tool for p in both] == ["shell"]


def test_malformed_markers_are_ignored():
    cases = [
        "TOOL_REQUEST:",  # no tool at all
        "TOOL_REQUEST: sh!!ell (blocking) - illegal characters in the name",
        "TOOL_REQUEST: " + "x" * 41 + " - too long a name",
        "please TOOL_REQUEST: shell - not line-anchored",
        "TOOLREQUEST: shell - missing the underscore",
        "TOOL REQUEST: shell - missing the underscore",
    ]
    for text in cases:
        assert parse_tool_requests(text) == [], text
    # The boundary of "malformed": a legal-shaped name followed by prose *is* a
    # request, for a tool nothing maps — which the ledger refuses and audits
    # rather than the parser guessing at what was meant.
    (odd,) = parse_tool_requests("TOOL_REQUEST: sh ell!! (blocking) - typo'd name")
    assert odd.tool == "sh"
    assert classify(odd.tool, LoopConfig()) is ToolClass.UNKNOWN


def test_a_missing_flag_means_optional():
    # A malformed or absent flag must never gain the power to stall a task.
    (only,) = parse_tool_requests("TOOL_REQUEST: shell - run the build")
    assert only.blocking is False
    (typo,) = parse_tool_requests("TOOL_REQUEST: shell (blokcing) - run the build")
    assert typo.blocking is False
    assert typo.reason == "(blokcing) - run the build"


def test_a_long_reason_is_bounded():
    (only,) = parse_tool_requests("TOOL_REQUEST: shell - " + "y" * 5000)
    assert len(only.reason) == 200
    assert only.reason == "y" * 200


def test_duplicates_collapse_and_blocking_beats_optional():
    parsed = parse_tool_requests(
        "TOOL_REQUEST: shell (optional) - maybe\n"
        "TOOL_REQUEST: shell (blocking) - actually I cannot proceed\n"
        "TOOL_REQUEST: git (optional) - nice to have\n"
    )

    assert [p.tool for p in parsed] == ["shell", "git"]
    assert parsed[0].blocking is True  # asked twice, the strongest wins
    assert parsed[0].reason == "maybe"  # the first statement of why
    assert parsed[1].blocking is False


def test_parse_tool_requests_is_total():
    hostile = [
        "",
        "   ",
        "\n\n\n",
        "TOOL_REQUEST",
        "TOOL_REQUEST:",
        "TOOL_REQUEST: ",
        "TOOL_REQUEST: (blocking)",
        "TOOL_REQUEST: shell (",
        "TOOL_REQUEST: shell)",
        "TOOL_REQUEST: shell\x00 - nul byte",
        "TOOL_REQUEST: \x00shell",
        "x" * 100_000,
        "TOOL_REQUEST: shell - " + "z" * 100_000,
        "```\nTOOL_REQUEST: shell (blocking) - inside a fence\n```",
        "TOOL_REQUEST: shell\r\n",
        "TOOL_REQUEST: 日本語 - non-ascii tool",
        "TOOL_REQUEST: shell - 日本語 の理由",
        'TOOL_REQUEST: shell - {"json": [1, 2]}',
        "\n".join(["TOOL_REQUEST: shell - many"] * 500),
        "TOOL_REQUEST: -",
        "TOOL_REQUEST: --- (blocking)",
    ]
    for text in hostile:
        result = parse_tool_requests(text)
        assert isinstance(result, list)
        assert all(isinstance(p, ParsedToolRequest) for p in result)
    # The one thing this function may never do is raise: it runs on the same
    # output whose attempt has already been paid for.
    assert parse_tool_requests(None) == []
    assert parse_tool_requests(12345) == []


def test_baseline_tools_is_derived_from_default_agents():
    # Derived, never transcribed: a registry edit moves the baseline with it,
    # and a hand-copied table would drift silently in the withholding direction.
    assert baseline_tools("worker") == DEFAULT_AGENTS["worker"].tools
    assert baseline_tools("planner") == DEFAULT_AGENTS["planner"].tools
    assert baseline_tools("summarizer") == []
    # A role with no shipped baseline gates everything it declares: fail-safe.
    assert baseline_tools("custom-worker") == []
    # A copy, so a caller cannot mutate the registry through it.
    baseline_tools("worker").append("shell")
    assert "shell" not in DEFAULT_AGENTS["worker"].tools


def test_tools_for_returns_spec_tools_unchanged_by_default(store):
    task = add_task(store)
    spec = _spec(["git", "file_io", "search", "task_state"])
    before = len(store.events())

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == [
        "git",
        "file_io",
        "search",
        "task_state",
    ]
    # Content *and* order: the `{kind}_prompt` event records `tools`, so order is
    # part of the byte-for-byte guarantee, not a detail.
    assert store.tool_requests(task_id=task.id) == []
    assert len(store.events()) == before


def test_tools_for_appends_granted_tools_in_request_order(store):
    task = add_task(store)
    # Granted in an order alphabetical sorting would reverse, so the assertion
    # can tell request order from a tidy sort.
    _add(store, task.id, "web", status=ToolRequestStatus.APPROVED.value)
    _add(store, task.id, "shell", status=ToolRequestStatus.AUTO.value)

    assert tools_for(store, LoopConfig(), _spec(["git"]), task.id, "worker") == [
        "git",
        "web",
        "shell",
    ]
    # A grant adds; it never duplicates a tool the role already declares.
    assert tools_for(store, LoopConfig(), _spec(["git", "web"]), task.id, "worker") == [
        "git",
        "web",
        "shell",
    ]


def test_tools_for_never_returns_an_unknown_logical_tool(store):
    task = add_task(store)
    _add(store, task.id, "teleport", status=ToolRequestStatus.APPROVED.value)

    # Whatever the row says: an unknown logical name resolves to no SDK tool, so
    # returning it would hand over a permission that means nothing.
    assert tools_for(store, LoopConfig(), _spec(["git"]), task.id, "worker") == ["git"]


def test_gated_declared_tool_is_withheld_and_queued(store):
    task = add_task(store)
    cfg = LoopConfig(gate_declared_tools=True)
    spec = _spec(["search", "shell"], role="custom-worker")

    assert tools_for(store, cfg, spec, task.id, "worker") == ["search"]
    (queued,) = store.tool_requests(task_id=task.id)
    assert queued.tool == "shell"
    assert queued.status is ToolRequestStatus.PENDING
    assert queued.source is ToolRequestSource.DECLARED
    # Once granted it reaches the runner without any registry change.
    store.tool_request_decide(queued.id, approved=True, by="human")
    assert tools_for(store, cfg, spec, task.id, "worker") == ["search", "shell"]


def test_the_shipped_baseline_is_never_gated(store):
    task = add_task(store)
    cfg = LoopConfig(gate_declared_tools=True)
    spec = _spec(list(DEFAULT_AGENTS["worker"].tools))

    # file_io and git are in the shipped worker's list and are *not* read-only;
    # gating them on a fresh install would gut the worker, so the baseline is a
    # step in the resolution order rather than an exception to it.
    assert tools_for(store, cfg, spec, task.id, "worker") == list(
        DEFAULT_AGENTS["worker"].tools
    )
    assert store.tool_requests(task_id=task.id) == []


def test_a_declared_source_request_is_never_blocking(store):
    task = add_task(store)
    cfg = LoopConfig(gate_declared_tools=True)
    # The same tool already carries a *blocking* marker row from another role.
    _add(
        store, task.id, "shell", role="validator", agent_kind="validator", blocking=True
    )

    tools_for(store, cfg, _spec(["shell"], role="custom-worker"), task.id, "worker")
    declared = [
        r for r in store.tool_requests(task_id=task.id) if r.role == "custom-worker"
    ]
    # The agent never asked for a declared tool, so it cannot have called it
    # load-bearing: `tools_for` has no path that can park a task, and the flag is
    # never copied from a peer row.
    assert [r.blocking for r in declared] == [False]
    assert store.pending_blocking_tool_requests(task.id, parked_only=True) == []


def test_a_declared_request_records_agent_kind_and_role_separately(store):
    task = add_task(store)
    cfg = LoopConfig(gate_declared_tools=True)
    spec = _spec(["shell"], role="custom-worker")

    tools_for(store, cfg, spec, task.id, "worker")
    (row,) = store.tool_requests(task_id=task.id)
    # Two distinct facts in two columns: the registry role the spec carries, and
    # the loop's own literal for which agent was running.
    assert row.role == "custom-worker"
    assert row.agent_kind == "worker"
    # `granted_tools` keys on the role, not the kind.
    store.tool_request_decide(row.id, approved=True, by="human")
    assert store.granted_tools(task.id, "custom-worker") == ["shell"]
    assert store.granted_tools(task.id, "worker") == []


# -- V1: `tool_request_add` runs inside a paid transaction and may not raise ---


def test_tool_request_add_cannot_raise_on_hostile_text_fields(store):
    """The accessor's own docstring promises it never raises, and phase 4b is
    what makes that promise load-bearing: it is then called inside `_invoke`'s
    closing transaction, over an already-paid `finish_attempt`.

    Three separately measured breakages, in one row: a `None` tool hit the
    `NOT NULL` constraint, a non-`str` reason failed to bind as a parameter, and
    a `why` `json.dumps` cannot encode raised inside `log_event`. Each rolled
    back the paid attempt, after which `_with_retry` bought the completion again.
    """
    task = add_task(store)

    class Unencodable:
        def __repr__(self):
            return "<unencodable>"

    request_id = store.tool_request_add(
        task.id,
        role=object(),
        agent_kind=object(),
        tool=None,
        status=ToolRequestStatus.PENDING.value,
        source=ToolRequestSource.MARKER.value,
        reason=Unencodable(),
        why=Unencodable(),
    )

    # The row exists, is readable, and every text field came back a `str` — an
    # `INSERT` that succeeded with a non-`str` would make `_row_to_tool_request`
    # the next thing to raise, on every later read of the whole ledger.
    (row,) = store.tool_requests(task_id=task.id)
    assert row.id == request_id
    assert isinstance(row.tool, str) and row.tool
    assert isinstance(row.reason, str)
    assert isinstance(row.role, str) and isinstance(row.agent_kind, str)
    # And its event encoded, which is what `log_event`'s one `json.dumps` does.
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_requested"]
    assert isinstance(json.loads(json.dumps(event["payload"])), dict)


def test_tool_request_add_cannot_raise_on_a_hostile_status_or_cap(store):
    """The rest of the same class, closed rather than the three measured inputs.

    An unhashable `status` raised `TypeError` from the frozenset membership test
    that was supposed to *report* a miss, and a `str` cap — reachable, since
    `loopconfig.json` is loaded without type checking — raised from the `>=`.
    """
    task = add_task(store)

    store.tool_request_add(
        task.id,
        role="worker",
        agent_kind="worker",
        tool="shell",
        status=["pending"],  # unhashable
        source=ToolRequestSource.MARKER.value,
        max_per_task="10",  # a knob loaded from JSON as a string
    )

    (row,) = store.tool_requests(task_id=task.id)
    # An out-of-enum status lands on the fail-safe status, which grants nothing
    # and parks nothing, and says so in the audit trail.
    assert row.status is ToolRequestStatus.REFUSED
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_refused"]
    assert "out-of-enum" in event["payload"]["why"]
    # The control: a well-formed call on the same accessor is still `pending`, so
    # the refusal above is the coercion's verdict and not a blanket one.
    store.tool_request_add(
        task.id,
        role="worker",
        agent_kind="worker",
        tool="web",
        status=ToolRequestStatus.PENDING.value,
        source=ToolRequestSource.MARKER.value,
        max_per_task=10,
    )
    assert [r.status for r in store.tool_requests(task_id=task.id)] == [
        ToolRequestStatus.REFUSED,
        ToolRequestStatus.PENDING,
    ]


def test_a_non_string_declared_tool_is_coerced_at_the_tools_for_boundary(store):
    """The reachable half of V1: an `agents.json` carrying `tools: [null]` loads
    unvalidated through `AgentSpec(**spec)`, and under `gate_declared_tools` the
    declared path handed that `None` straight to the ledger."""
    task = add_task(store)
    cfg = LoopConfig(gate_declared_tools=True)

    # Withheld, as any unknown logical name is — and audited under a name a human
    # can read rather than as a NULL.
    assert (
        tools_for(store, cfg, _spec([None], role="custom-worker"), task.id, "w") == []
    )
    (row,) = store.tool_requests(task_id=task.id)
    assert row.tool == "None"
    assert row.status is ToolRequestStatus.REFUSED
    # The control: a *valid* gated name on the same config is queued `pending`,
    # so the refusal above is the coercion's verdict on an unusable name and not
    # this branch refusing everything.
    assert tools_for(store, cfg, _spec(["shell"], role="other"), task.id, "w") == []
    (queued,) = [r for r in store.tool_requests(task_id=task.id) if r.role == "other"]
    assert queued.status is ToolRequestStatus.PENDING


# -- phase 4a: the gate bites at the `tools` list handed to the runner ---------


def test_default_config_leaves_every_tools_list_unchanged(store):
    """All three enforced roles, measured rather than asserted role by role.

    The `{kind}_prompt` event records `tools`, so *order* is part of the promise:
    an unconfigured run must hand each runner exactly the list the registry
    declares, in the registry's order.
    """
    loop, runner = make_loop(store, [PLAN_JSON, "worker out", APPROVE])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")
    loop.approve_plan(plan.id)
    loop.run_task(store.plan_tasks(plan.id)[0])

    assert [c["tools"] for c in runner.calls] == [
        list(DEFAULT_AGENTS["planner"].tools),
        list(DEFAULT_AGENTS["worker"].tools),
        list(DEFAULT_AGENTS["validator"].tools),
    ]
    # And the audit log agrees with what the runner was handed.
    prompts = [e for e in store.events() if e["kind"].endswith("_prompt")]
    assert [e["payload"]["tools"] for e in prompts] == [
        c["tools"] for c in runner.calls
    ]


def test_gated_declared_tool_is_withheld_from_the_runner_call(store):
    task = add_task(store)
    # A role whose declared list carries a side-effecting tool outside its own
    # shipped baseline: `web` is not read-only and the shipped worker never
    # declared it, so under gating it needs a grant.
    loop, runner = _loop(
        store,
        ["worker out", APPROVE],
        registry=_registry_with_worker_tools(["search", "web"]),
        gate_declared_tools=True,
    )
    loop.run_task(task)

    assert runner.calls[0]["tools"] == ["search"]
    (row,) = [r for r in store.tool_requests(task_id=task.id) if r.tool == "web"]
    assert row.status is ToolRequestStatus.PENDING
    assert row.source is ToolRequestSource.DECLARED


def test_the_shipped_baseline_reaches_the_runner_under_gating(store):
    """The control for the test above: gating must not gut the shipped worker.

    `file_io` and `git` are in `DEFAULT_AGENTS["worker"].tools` and are *not*
    read-only, so without the baseline step the same config that withholds `web`
    would strip the worker down to `search` + `task_state` on a fresh install.
    """
    task = add_task(store)
    loop, runner = make_loop(store, ["worker out", APPROVE], gate_declared_tools=True)
    loop.run_task(task)

    assert runner.calls[0]["tools"] == list(DEFAULT_AGENTS["worker"].tools)
    assert store.tool_requests(task_id=task.id) == []


def test_gate_declared_tools_never_parks_a_task(store):
    """The knob withholds and continues; it is not an escalation engine.

    The agent never asked for a declared tool, so it cannot have declared it
    load-bearing — every row the declared path writes is `blocking=0`, and
    nothing on that path can write `parked`.
    """
    task = add_task(store)
    loop, _ = _loop(
        store,
        ["worker out", APPROVE],
        registry=_registry_with_worker_tools(["search", "web"]),
        gate_declared_tools=True,
    )
    loop.run_task(task)

    assert task.status is TaskStatus.DONE
    rows = store.tool_requests(task_id=task.id)
    assert rows and all(not r.blocking and not r.parked for r in rows)


def test_a_none_config_parses_no_markers_and_gates_nothing(store):
    """`config=None` means the slice is absent from that invocation, not the
    slice running on defaults — which is what keeps `eval` and `run_summarizer`
    byte-identical to the pre-slice-5 loop."""
    from agentloop.agents import run_validator

    task = add_task(store)
    store.update_task(task)
    runner = MockRunner([APPROVE])
    # A store whose config *would* gate, and a reply carrying a live marker.
    run_validator(
        store,
        runner,
        Registry.load(),
        task,
        "TOOL_REQUEST: shell (blocking) - need a shell",
    )

    assert runner.calls[0]["tools"] == list(DEFAULT_AGENTS["validator"].tools)
    assert store.tool_requests() == []


def test_eval_still_runs_after_the_signature_change(store):
    """`eval` drives `run_validator` directly and has no config to give it, so the
    calibration harness must keep working *and* keep its scratch store free of a
    tool queue nobody asked for."""
    from agentloop import eval as evalmod

    runner = evalmod.mock_runner_for(evalmod.FIXTURES)
    result = evalmod.run_eval(store, runner, Registry.load())

    assert result["summary"]["n"] == len(evalmod.FIXTURES)
    assert store.tool_requests() == []


# -- phase 4b: markers become rows, inside `_invoke`'s closing transaction -----

MARK_READONLY = "worker out\nTOOL_REQUEST: file_read (optional) - need to read config"
MARK_OPTIONAL = "worker out\nTOOL_REQUEST: web (optional) - would like to look it up"
MARK_BLOCKING = "worker out\nTOOL_REQUEST: shell (blocking) - cannot build without it"
MARK_OPTIONAL_SHELL = "worker out\nTOOL_REQUEST: shell (optional) - would be handy"


def test_readonly_request_is_auto_approved_and_audited(store):
    task = add_task(store)
    loop, _ = _loop(store, [MARK_READONLY, APPROVE])
    loop.run_task(task)

    (row,) = store.tool_requests(task_id=task.id)
    assert row.tool == "file_read"
    assert row.status is ToolRequestStatus.AUTO
    assert row.source is ToolRequestSource.MARKER
    assert row.reason == "need to read config"
    assert row.agent_kind == "worker"
    # The audit trail must show the auto *decision*, not merely the absence of a
    # gate — that record is what makes the grant traceable.
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_auto_approved"]
    assert event["payload"]["tool"] == "file_read"
    assert event["payload"]["attempt_id"] == 1  # attributed to the worker attempt


def test_an_auto_row_reaches_the_next_invocations_tools(store):
    """Acceptance criterion 1, and the reason grants must *add* to the list: an
    auto-approved read-only request would change nothing if a grant could only
    unlock a tool the role already declared."""
    task = add_task(store)
    loop, runner = _loop(store, [MARK_READONLY, REVISE, "worker out v2", APPROVE])
    loop.run_task(task)

    baseline = list(DEFAULT_AGENTS["worker"].tools)
    assert runner.calls[0]["tools"] == baseline  # the asking round is unchanged
    assert runner.calls[2]["tools"] == baseline + ["file_read"]
    # Task-scoped and role-scoped: the validator asked for nothing.
    assert runner.calls[1]["tools"] == list(DEFAULT_AGENTS["validator"].tools)


def test_optional_side_effecting_request_is_queued_and_audited(store):
    task = add_task(store)
    loop, _ = _loop(store, [MARK_OPTIONAL, APPROVE])
    loop.run_task(task)

    (row,) = store.tool_requests(task_id=task.id)
    assert row.tool == "web"
    assert row.status is ToolRequestStatus.PENDING
    assert row.blocking is False
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_requested"]
    assert event["payload"]["blocking"] is False
    assert event["payload"]["upgraded"] is False


def test_optional_request_never_reaches_the_tools_list(store):
    task = add_task(store)
    loop, runner = _loop(store, [MARK_OPTIONAL, REVISE, "worker out v2", APPROVE])
    loop.run_task(task)

    # Withheld from this invocation *and* the next: a pending row is an ask, not
    # a grant, and only a human moves it.
    assert all("web" not in c["tools"] for c in runner.calls)


def test_blocking_request_is_queued_as_blocking(store):
    task = add_task(store)
    loop, _ = _loop(store, [MARK_BLOCKING, APPROVE])
    loop.run_task(task)

    (row,) = store.tool_requests(task_id=task.id)
    assert (row.tool, row.blocking, row.status, row.source) == (
        "shell",
        True,
        ToolRequestStatus.PENDING,
        ToolRequestSource.MARKER,
    )


def test_an_unknown_logical_tool_is_refused_end_to_end(store):
    task = add_task(store)
    loop, runner = _loop(
        store,
        ["worker out\nTOOL_REQUEST: teleport (blocking) - beam me up", APPROVE],
    )
    loop.run_task(task)

    (row,) = store.tool_requests(task_id=task.id)
    # Terminal and machine-made: an unknown logical name resolves to no SDK tool,
    # so granting it would record a permission that means nothing.
    assert row.status is ToolRequestStatus.REFUSED
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_refused"]
    assert "logical tool" in event["payload"]["why"]
    assert store.granted_tools(task.id, "worker") == []
    assert all("teleport" not in c["tools"] for c in runner.calls)


def test_the_marker_is_never_stripped_from_the_output(store):
    task = add_task(store)
    loop, _ = _loop(store, [MARK_BLOCKING, APPROVE])
    loop.run_task(task)

    # The `FINDINGS:` precedent — added to, never subtracted from. The output is
    # what the validator reviews and what dependents consume, so a stripped copy
    # would be a second, lossy version of what the agent said.
    assert store.get_task(task.id).output == MARK_BLOCKING
    outputs = [e for e in store.events(task.id) if e["kind"] == "worker_output"]
    assert outputs[0]["payload"]["output"] == MARK_BLOCKING


def test_marker_parsing_covers_worker_validator_and_planner(store):
    """The role allowlist, enumerated. An explicit `("worker","validator",
    "planner")` rather than `kind != "summarizer"`, so a role added later has to
    be opted in instead of silently inheriting marker parsing."""
    planner_reply = PLAN_JSON + "\nTOOL_REQUEST: web (optional) - to check the API\n"
    validator_reply = APPROVE + "\nTOOL_REQUEST: git (optional) - to read the log"
    loop, _ = _loop(store, [planner_reply, MARK_OPTIONAL, validator_reply])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")
    loop.approve_plan(plan.id)
    loop.run_task(store.plan_tasks(plan.id)[0])

    by_kind = {r.agent_kind: r.tool for r in store.tool_requests()}
    assert by_kind == {"planner": "web", "worker": "web", "validator": "git"}


def test_summarizer_output_quoting_a_marker_creates_no_request(store):
    """The control case that makes "never parse the summarizer" non-vacuous: a
    real handoff is driven, so the summarizer is genuinely invoked rather than
    merely absent. Its output compresses a transcript that may quote a marker
    verbatim, and parsing it would manufacture a request from a quotation."""
    task = add_task(store)
    agents = dict(DEFAULT_AGENTS)
    agents["worker"] = replace(agents["worker"], context_budget_tokens=40)
    loop, _ = _loop(
        store,
        [
            "worker1 raw output",
            REVISE,
            "SUMMARY: the worker asked for TOOL_REQUEST: shell (blocking) - build",
            "worker out v2",
            APPROVE,
        ],
        registry=Registry(agents),
    )
    loop.run_task(task)

    handoffs = [e for e in store.events(task.id) if e["kind"] == "context_handoff"]
    assert handoffs, "this test is only meaningful if a handoff really fired"
    kinds = [e["kind"] for e in store.events(task.id)]
    assert "summarizer_output" in kinds, "the summarizer must really have run"
    assert store.tool_requests() == []


def test_a_pathological_marker_never_rolls_back_a_paid_attempt(store):
    """P4. The write happens inside `_invoke`'s closing transaction, which holds
    an already-paid `finish_attempt`; a raise there discards tokens the provider
    billed and `_with_retry` buys them again."""
    task = add_task(store)
    reply = (
        "worker out\n"
        "TOOL_REQUEST: shell (blocking) - " + "é{}" * 2000 + "\n"
        "TOOL_REQUEST: 日本語 - a non-ascii tool name\n"
    )
    loop, _ = _loop(store, [reply, APPROVE])
    loop.run_task(task)

    m = store.task_metrics(task.id)
    # The paid attempt survived its own telemetry: `finish_attempt` committed,
    # with the tokens and the reply it was billed for. A rollback would show up as
    # `attempts == 0` — the row is only counted once `finished_at` is set. One
    # attempt, not two: the blocking ask parks the task before the validator.
    assert m["attempts"] == 1
    assert m["tokens"] > 0
    (out,) = [e for e in store.events(task.id) if e["kind"] == "worker_output"]
    assert out["payload"]["output"] == reply
    assert out["payload"]["tokens_in"] > 0
    # Exactly one row: the non-ascii name does not match the grammar at all.
    (row,) = store.tool_requests(task_id=task.id)
    assert row.tool == "shell"
    assert len(row.reason) == 200


def test_a_hostile_config_never_rolls_back_a_paid_attempt(store):
    """P4 on the *config* axis, which had no coverage at all.

    `test_a_pathological_marker_never_rolls_back_a_paid_attempt` varies only the
    reply. The other input the marker path reads inside the paid transaction is
    `LoopConfig`, which `load` builds with `cls(**data)` — so
    `{"tool_readonly_allowlist": null}`, a plausible "turn the allowlist off"
    edit, used to reach `classify`'s `tool in config.tool_readonly_allowlist` as
    a `TypeError` over an uncommitted `finish_attempt`: three paid completions,
    zero attempt rows, `task_spend` reporting $0 for money that was spent, and
    the human pointed at the network by an `infra_error`.
    """
    task = add_task(store)
    loop, runner = _loop(store, [MARK_BLOCKING, APPROVE], tool_readonly_allowlist=None)
    loop.run_task(task)

    # Paid once, recorded once. Three of either is the defect.
    assert len(runner.calls) == 1
    m = store.task_metrics(task.id)
    assert m["attempts"] == 1
    assert m["tokens"] > 0
    # And the run still *behaved*: `None` normalized to an empty allowlist (the
    # off switch the edit was asking for), so `shell` classified GATED and parked
    # the task rather than the slice going inert or failing open.
    assert store.get_task(task.id).status is TaskStatus.NEEDS_HUMAN
    (row,) = store.tool_requests(task_id=task.id)
    assert row.tool == "shell" and row.status is ToolRequestStatus.PENDING
    assert [e for e in store.events(task.id) if e["kind"] == "infra_error"] == []


def test_a_config_field_of_the_wrong_type_is_refused_at_load(tmp_path):
    """The loud write-time refusal that pays for a total read path downstream —
    `Store.charter_set`'s pattern, rendered by `cli.main` as `error: …`.

    A bare string is the case worth naming: `tool in "file_read"` is a
    *substring* test, so `tool_readonly_allowlist: "file_read"` would silently
    auto-approve any tool whose name is a substring of it. A gate that fails open
    on a plausible typo is worse than one that refuses to load.
    """
    path = tmp_path / "loopconfig.json"
    path.write_text(json.dumps({"tool_readonly_allowlist": "file_read"}), "utf-8")
    with pytest.raises(ValueError, match="tool_readonly_allowlist"):
        LoopConfig.load(path)

    path.write_text(json.dumps({"max_revisions": "three"}), "utf-8")
    with pytest.raises(ValueError, match="max_revisions"):
        LoopConfig.load(path)

    # Non-vacuity: the same loader accepts the same fields when they are typed.
    path.write_text(
        json.dumps({"tool_readonly_allowlist": ["file_read"], "max_revisions": 1}),
        "utf-8",
    )
    assert LoopConfig.load(path).max_revisions == 1


def test_a_null_list_config_field_means_the_empty_list(tmp_path):
    """`null` on a list field is the one wrong type with an unambiguous reading —
    "off" — so it normalizes rather than raising. Normalizing is what makes the
    downstream read total; raising here would only move the crash earlier for the
    honest reading of a plausible edit."""
    assert LoopConfig(tool_readonly_allowlist=None).tool_readonly_allowlist == []
    path = tmp_path / "loopconfig.json"
    path.write_text(json.dumps({"sandbox_env_allowlist": None}), "utf-8")
    assert LoopConfig.load(path).sandbox_env_allowlist == []


def test_a_decided_request_is_never_reopened_by_a_re_request(store):
    """P5: a denial cannot ping-pong. The UNIQUE row absorbs the re-ask, so the
    agent cannot spend a human's attention twice on one refusal."""
    task = add_task(store)
    loop, _ = _loop(store, [MARK_OPTIONAL, REVISE, MARK_OPTIONAL, APPROVE])
    loop.config.max_revisions = 0
    loop.run_task(task)

    (row,) = store.tool_requests(task_id=task.id)
    store.tool_request_decide(row.id, approved=False, by="human")
    requested_before = len(
        [e for e in store.events(task.id) if e["kind"] == "tool_requested"]
    )
    assert requested_before == 1  # non-vacuity: round 1 really did record one

    loop.runner.outputs = [MARK_OPTIONAL, APPROVE]
    loop.config.max_revisions = 3
    loop.human_redo(task.id)
    loop.run_task(store.get_task(task.id))

    rows = store.tool_requests(task_id=task.id)
    assert len(rows) == 1
    assert rows[0].status is ToolRequestStatus.REJECTED
    assert (
        len([e for e in store.events(task.id) if e["kind"] == "tool_requested"])
        == requested_before
    )


def test_a_marker_row_records_agent_kind_and_role_separately(store):
    """E23: `agent_kind` is the loop's literal, `role` is `spec.role`, and a
    custom `task.worker_role` makes them differ. A builder passing one for the
    other fails here rather than in a dashboard six months later."""
    agents = dict(DEFAULT_AGENTS)
    agents["custom-worker"] = replace(agents["worker"], role="custom-worker")
    task = Task(
        id=None,
        title="Add slugify util",
        goal="Write a slugify(text) function.",
        acceptance_criteria="Lowercase, hyphen-separated, tested.",
        worker_role="custom-worker",
    )
    store.add_task(task)
    loop, _ = _loop(store, [MARK_OPTIONAL, APPROVE], registry=Registry(agents))
    loop.run_task(task)

    (row,) = store.tool_requests(task_id=task.id)
    assert row.agent_kind == "worker"
    assert row.role == "custom-worker"
    assert row.agent_kind != row.role
    store.tool_request_decide(row.id, approved=True, by="human")
    assert store.granted_tools(task.id, "custom-worker") == ["web"]
    assert store.granted_tools(task.id, "worker") == []


def test_a_blocking_request_is_never_refused_by_the_queue_cap(store):
    """The over-cap **blocking** request, decided explicitly: exempt from the cap.

    Over the cap a request is stored `refused`, and
    `pending_blocking_tool_requests` reads `pending` only — so a capped blocking
    ask could never park, and "I cannot finish without this" would silently become
    "continue without it and tell no human".
    """
    task = add_task(store)
    for tool in ["file_io", "git", "web"]:
        _add(store, task.id, tool)  # a queue already at the cap
    loop, _ = _loop(store, [MARK_BLOCKING, APPROVE], max_tool_requests_per_task=3)
    loop.run_task(task)

    (shell,) = [r for r in store.tool_requests(task_id=task.id) if r.tool == "shell"]
    assert shell.status is ToolRequestStatus.PENDING
    assert shell.blocking is True
    assert store.pending_blocking_tool_requests(task.id) == [shell]


def test_an_optional_request_is_still_refused_by_the_queue_cap(store):
    """The control for the exemption above: the cap is still a cap. Asserted with
    the same fixture and the same cap, so the only difference is the flag."""
    task = add_task(store)
    for tool in ["file_io", "git", "web"]:
        _add(store, task.id, tool)
    loop, _ = _loop(store, [MARK_OPTIONAL_SHELL, APPROVE], max_tool_requests_per_task=3)
    loop.run_task(task)

    (shell,) = [r for r in store.tool_requests(task_id=task.id) if r.tool == "shell"]
    assert shell.status is ToolRequestStatus.REFUSED
    (event,) = [
        e
        for e in store.events(task.id)
        if e["kind"] == "tool_request_refused" and e["payload"]["tool"] == "shell"
    ]
    assert "per-task cap" in event["payload"]["why"]


def test_a_marker_row_is_never_created_parked(store):
    """Only the loop's park writes `parked`, so a fresh row can never be born
    holding a task at NEEDS_HUMAN.

    Asserted on a **planner** row, and that is what makes it non-vacuous: the park
    check lives in `run_task`, so a `plan` row's blocking ask is the one place a
    *blocking* marker row can be observed at the moment of creation, with nothing
    downstream able to have stamped it.
    """
    planner_reply = PLAN_JSON + "\nTOOL_REQUEST: shell (blocking) - to scaffold\n"
    loop, _ = _loop(store, [planner_reply])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    (row,) = store.tool_requests(task_id=plan.id)
    assert row.blocking is True
    assert row.parked is False


# -- phase 5: the decision rule — blocking park, approve, reject ---------------


def _parked(store, **cfg):
    """A task really parked by the loop on one blocking request."""
    task = add_task(store)
    loop, runner = _loop(store, [MARK_BLOCKING, APPROVE], **cfg)
    loop.run_task(task)
    (row,) = store.tool_requests(task_id=task.id)
    assert row.parked and store.get_task(task.id).status is TaskStatus.NEEDS_HUMAN
    return loop, runner, task, row


def test_blocking_request_parks_the_task_at_needs_human(store):
    task = add_task(store)
    loop, _ = _loop(store, [MARK_BLOCKING, APPROVE])
    loop.run_task(task)

    assert store.get_task(task.id).status is TaskStatus.NEEDS_HUMAN


def test_a_parked_task_keeps_its_partial_output_and_revision_count(store):
    _, _, task, _ = _parked(store)

    stored = store.get_task(task.id)
    # The work already done is kept: a blocking ask is "I got this far and cannot
    # go further", not a failure to produce anything.
    assert stored.output == MARK_BLOCKING
    assert stored.revision_count == 0


def test_the_validator_never_runs_on_a_parked_task(store):
    _, runner, task, _ = _parked(store)

    m = store.task_metrics(task.id)
    assert m["attempts"] == 1  # the worker only
    assert m["verdicts"] == []
    assert len(runner.calls) == 1
    # And no test run: the park sits before TESTING, so nothing was executed and
    # nothing is recorded as though it had been.
    assert store.test_runs(task.id) == []


def test_the_park_reason_names_the_tool_and_request_id(store):
    _, _, task, row = _parked(store)

    reason = store.get_task(task.id).escalation_reason
    assert "shell" in reason
    assert str(row.id) in reason


def test_the_park_stamps_the_rows_it_parked_on(store):
    task = add_task(store)
    reply = MARK_BLOCKING + "\nTOOL_REQUEST: web (optional) - would be nice"
    loop, _ = _loop(store, [reply, APPROVE])
    loop.run_task(task)

    by_tool = {r.tool: r for r in store.tool_requests(task_id=task.id)}
    # Exactly the rows named in the escalation reason, and no others: the flag is
    # "the loop is holding this task on *this* row", so an optional ask riding
    # along must not inherit it.
    assert by_tool["shell"].parked is True
    assert by_tool["web"].parked is False


def test_the_park_logs_no_new_event_kind(store):
    _, _, task, _ = _parked(store)

    kinds = [e["kind"] for e in store.events(task.id)]
    # `set_status` already audits the transition with its reason. A second event
    # for the same fact would let anyone counting escalations count them twice.
    assert "status:needs_human" in kinds
    assert [k for k in kinds if "park" in k] == []


def test_approval_is_recorded_with_released_true(store):
    loop, _, task, row = _parked(store)
    loop.approve_tool_request(row.id, note="ok, granted")

    decided = store.tool_request_get(row.id)
    assert decided.status is ToolRequestStatus.APPROVED
    assert decided.decided_by == "human"
    assert decided.decided_note == "ok, granted"
    assert decided.decided_at is not None
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert event["payload"]["released"] is True


def test_the_release_keeps_the_row_and_clears_the_reason(store):
    """What is actually true and assertable. Deliberately *not* "feedback intact":
    `Task` has no feedback field, nothing persists validator feedback, and on the
    park path the validator never ran, so there is none to restore."""
    loop, _, task, row = _parked(store)
    before = [(e["id"], e["kind"]) for e in store.events(task.id)]

    loop.approve_tool_request(row.id)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.PENDING
    assert stored.output == MARK_BLOCKING  # byte-identical to the worker's reply
    assert stored.revision_count == 0
    assert stored.escalation_reason == ""
    assert stored.claimed_by is None
    # Append-only: every pre-release event is still there with its id.
    assert before == [
        (e["id"], e["kind"]) for e in store.events(task.id) if e["id"] <= before[-1][0]
    ]
    # A later park is a fresh fact, not a residue of this one.
    assert [r.parked for r in store.tool_requests(task_id=task.id)] == [False]


def test_a_release_keeps_the_workspace_that_a_redo_wipes(store):
    """P10. The contrast is the proof: asserted alone, "the files are still there"
    would also pass if nothing ever deleted them. `clear_workspace` runs in
    exactly one place, `human_redo`."""
    from agentloop.executor import workspace_for

    loop, _, task, row = _parked(store)
    ws = workspace_for(loop.config.workspace_root, task.id, create=True)
    (ws / "partial.py").write_text("half the work", encoding="utf-8")

    loop.approve_tool_request(row.id)
    assert (ws / "partial.py").read_text(encoding="utf-8") == "half the work"

    loop.human_redo(task.id)
    assert not (ws / "partial.py").exists()


def test_approving_a_blocking_request_returns_a_claimable_task(store):
    """P6: genuinely claimable, not merely `pending`. A release that left the
    `claimed_by` lease set would produce a row the claim's compare-and-swap can
    never match, *and* would burn every claim attempt ahead of other work."""
    loop, _, task, row = _parked(store)
    other = add_task(store)  # pending, unclaimed, behind the released one
    loop.approve_tool_request(row.id)

    claimed = store.claim_next_task("loop")
    assert claimed is not None and claimed.id == task.id
    assert store.claim_next_task("loop2").id == other.id


def test_two_humans_clearing_two_blocking_rows_cannot_strand_the_task(store):
    """The release predicate has to be evaluated *inside* the transaction that
    writes the decision — `tool_request_decide`'s own docstring states the rule,
    and `approve_tool_request` was computing `others`/`released` from three reads
    taken before it.

    Two `agentloop` processes clearing a two-row queue at once therefore both read
    "another parked blocking row still stands", both concluded `released=False`,
    and both granted: nothing left pending, `parked=1` still stamped, and the task
    held at `NEEDS_HUMAN` with no route back — approve on either row now raises,
    `resume` only acts on `PAUSED`, and the only exits are a `redo` (which wipes
    the workspace the release exists to preserve) or a terminal decision on
    unfinished work. Sequential approval works, which is why the rest of the
    suite cannot see it.

    Two `Store`s, because one lock would serialize them and hide the fact that the
    SQL is what has to do the work (`test_two_connections_deciding_one_request…`'s
    reasoning). The pause hook holds the first approval open at the moment it
    reads the release predicate, which is the whole window the old shape left.
    """
    from agentloop.loop import Loop

    task = add_task(store)
    reply = (
        "worker out\n"
        "TOOL_REQUEST: shell (blocking) - cannot build without it\n"
        "TOOL_REQUEST: git (blocking) - and cannot commit without this\n"
    )
    loop, _ = _loop(store, [reply, APPROVE])
    loop.run_task(task)
    first, second = store.tool_requests(task_id=task.id)
    assert first.parked and second.parked, "both rows must really be parked"

    peer_store = Store(store.db_path)
    try:
        peer = Loop(peer_store, loop.runner, loop.registry, loop.config)
        assert peer_store._conn._lock is not store._conn._lock

        original_execute = store._conn.execute
        has_read = threading.Event()
        may_finish = threading.Event()
        paused = []

        def pause_after_reading_the_release_predicate(sql, params=()):
            result = original_execute(sql, params)
            if "FROM tool_requests WHERE task_id=? AND status=?" in sql and not paused:
                paused.append(True)
                has_read.set()
                may_finish.wait(10)
            return result

        store._conn.execute = pause_after_reading_the_release_predicate
        outcomes: dict[str, object] = {}

        def approve(name, which_loop, request_id):
            try:
                outcomes[name] = which_loop.approve_tool_request(request_id)
            except Exception as exc:
                outcomes[name] = exc

        alice = threading.Thread(target=approve, args=("alice", loop, first.id))
        alice.start()
        assert has_read.wait(10), "the first approval never read the predicate"
        bob = threading.Thread(target=approve, args=("bob", peer, second.id))
        bob.start()
        # The whole window the old shape left open. Under the fix the peer's own
        # decision blocks on sqlite's write lock instead of using it.
        time.sleep(0.2)
        may_finish.set()
        alice.join(10)
        bob.join(10)
        store._conn.execute = original_execute

        assert not [v for v in outcomes.values() if isinstance(v, Exception)], outcomes
        # Both grants recorded — the grant was never the conditional part.
        rows = store.tool_requests(task_id=task.id)
        assert all(r.status is ToolRequestStatus.APPROVED for r in rows)
        # And the queue being empty means the task is released, exactly once.
        final = store.get_task(task.id)
        assert final.status is TaskStatus.PENDING
        assert final.claimed_by is None
        assert not any(r.parked for r in rows)
        released = [
            e
            for e in store.events(task.id)
            if e["kind"] == "tool_request_decided" and e["payload"]["released"]
        ]
        assert len(released) == 1, "exactly one decision may lift one park"
        assert store.claim_next_task("loop").id == task.id
    finally:
        peer_store.close()


def test_a_grant_applies_to_the_next_invocation(store):
    loop, runner, task, row = _parked(store)
    loop.approve_tool_request(row.id)
    runner.outputs = ["worker out v2", APPROVE]
    loop.run_task(store.get_task(task.id))

    baseline = list(DEFAULT_AGENTS["worker"].tools)
    assert runner.calls[1]["tools"] == baseline + ["shell"]
    assert store.get_task(task.id).status is TaskStatus.DONE


def test_rejection_is_audited_and_leaves_the_task_parked(store):
    loop, _, task, row = _parked(store)
    loop.reject_tool_request(row.id, note="too risky")

    assert store.tool_request_get(row.id).status is ToolRequestStatus.REJECTED
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert event["payload"]["approved"] is False
    assert event["payload"]["released"] is False
    # No new release path: the human then uses approve/reject/redo on the *task*.
    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert "shell" in stored.escalation_reason


def test_approving_a_request_on_an_unparked_task_leaves_status_alone(store):
    """E12: a grant is recorded whatever the task is doing; only a park is lifted."""
    task = add_task(store)
    loop, _ = _loop(store, [MARK_OPTIONAL, APPROVE])
    loop.run_task(task)
    assert store.get_task(task.id).status is TaskStatus.DONE
    (row,) = store.tool_requests(task_id=task.id)

    loop.approve_tool_request(row.id)

    assert store.tool_request_get(row.id).status is ToolRequestStatus.APPROVED
    assert store.get_task(task.id).status is TaskStatus.DONE
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert event["payload"]["released"] is False


def test_approving_an_unrelated_request_does_not_revert_an_exhausted_escalation(store):
    """P8/E18. The stale row is `pending` and non-blocking, so it parked nothing;
    approving it must not blank the diagnosis the human was asked to act on."""
    task = add_task(store)
    loop, _ = _loop(
        store, [MARK_OPTIONAL, REVISE, "v2", REVISE, "v3", REVISE], max_revisions=2
    )
    loop.run_task(task)
    assert "Exhausted 2 revisions" in store.get_task(task.id).escalation_reason
    reason = store.get_task(task.id).escalation_reason
    (row,) = store.tool_requests(task_id=task.id)

    loop.approve_tool_request(row.id)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert stored.escalation_reason == reason  # byte-identical
    assert [e["kind"] for e in store.events(task.id)].count("status:pending") == 0
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert event["payload"]["released"] is False


def test_approving_a_blocking_request_the_task_did_not_park_on_leaves_the_escalation(
    store,
):
    """P8/E13b: the validator asked, and the round ended on *severe*. The row is
    `pending` + `blocking` with `parked=0`, which is exactly the case a predicate
    derived from `pending_blocking_tool_requests` alone would still get wrong."""
    task = add_task(store)
    validator_reply = SEVERE + "\nTOOL_REQUEST: shell (blocking) - to reproduce"
    loop, _ = _loop(store, ["worker out", validator_reply])
    loop.run_task(task)
    reason = store.get_task(task.id).escalation_reason
    assert "Severe disagreement" in reason
    (row,) = store.tool_requests(task_id=task.id)
    assert row.blocking and not row.parked

    loop.approve_tool_request(row.id)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert stored.escalation_reason == reason


def test_approving_a_request_does_not_revert_a_budget_cap_escalation(store):
    """P8/E18, the budget path — which fires *before* the park check's position,
    so a stale flag would make it revertible."""
    task = add_task(store)
    loop, _ = _loop(
        store, [MARK_OPTIONAL, REVISE, "v2", APPROVE], max_tokens_per_task=1
    )
    loop.run_task(task)
    reason = store.get_task(task.id).escalation_reason
    assert "budget" in reason.lower()
    (row,) = store.tool_requests(task_id=task.id)

    loop.approve_tool_request(row.id)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert stored.escalation_reason == reason


def test_a_task_parked_on_two_requests_is_released_only_by_the_last_one(store):
    """E19: otherwise the release pays a full worker round only to re-park at the
    next boundary."""
    task = add_task(store)
    reply = (
        "worker out\n"
        "TOOL_REQUEST: shell (blocking) - to build\n"
        "TOOL_REQUEST: web (blocking) - to read the spec\n"
    )
    loop, _ = _loop(store, [reply, APPROVE])
    loop.run_task(task)
    first, second = store.tool_requests(task_id=task.id)
    assert first.parked and second.parked

    loop.approve_tool_request(first.id)
    assert store.get_task(task.id).status is TaskStatus.NEEDS_HUMAN
    assert store.tool_request_get(second.id).parked is True

    loop.approve_tool_request(second.id)
    assert store.get_task(task.id).status is TaskStatus.PENDING


def test_release_does_not_clobber_a_pause(store):
    """E14: only `set_control` writes `control`, so a human's pause survives a
    release — the task returns to the queue and stops at the next boundary."""
    loop, _, task, row = _parked(store)
    loop.pause(task.id)
    assert store.get_control(task.id) == "pause"

    # A paused task is not at NEEDS_HUMAN, so the release does not even fire.
    loop.approve_tool_request(row.id)
    assert store.get_control(task.id) == "pause"
    assert store.get_task(task.id).status is TaskStatus.PAUSED


def test_a_validator_blocking_request_parks_only_on_the_revision_path(store):
    """E13a: the row is created mid-round, so the park can only fire on the one
    verdict path that re-enters the round — revise. The check sits after the
    worker's output is stored, so the revision's worker call is paid and then the
    round stops there: one more worker attempt, and no second validator."""
    task = add_task(store)
    validator_reply = REVISE + "\nTOOL_REQUEST: shell (blocking) - to reproduce"
    loop, runner = _loop(store, ["worker out", validator_reply, "v2", APPROVE])
    loop.run_task(task)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert "shell" in stored.escalation_reason
    # The revision was spent by the revise verdict, not by the park.
    assert stored.revision_count == 1
    # worker, validator, worker — and the second validator never ran.
    assert [c["prompt"][:14] for c in runner.calls] == [
        "# Task: Add sl",
        "# Task under r",
        "# Task: Add sl",
    ]
    assert store.task_metrics(task.id)["verdicts"] == [
        {"kind": "revise", "confidence": 0.55, "tests_passed": False, "findings": ""}
    ]


def test_the_park_reason_names_the_agent_that_asked(store):
    """The reason is the whole message the human acts on, and it used to name only
    the tool and the request id — so a validator asking to reproduce a bug read
    exactly like the worker being unable to build. `agent_kind` is already on the
    row; the difference decides whether the human is unblocking the work or the
    review."""
    task = add_task(store)
    validator_reply = REVISE + "\nTOOL_REQUEST: shell (blocking) - to reproduce"
    loop, _ = _loop(store, ["worker out", validator_reply, "v2", APPROVE])
    loop.run_task(task)
    from_validator = store.get_task(task.id).escalation_reason

    other = add_task(store)
    loop2, _ = _loop(store, [MARK_BLOCKING, APPROVE])
    loop2.run_task(other)
    from_worker = store.get_task(other.id).escalation_reason

    assert "validator" in from_validator
    assert "worker" in from_worker
    # Non-vacuity: the two messages are genuinely distinguishable, which is the
    # whole finding — asserting one substring alone would also pass on a reason
    # that happened to contain it for another reason.
    assert "validator" not in from_worker


def test_a_validator_blocking_request_on_the_approve_path_never_parks(store):
    """E13c, settled: no treatment. Nothing auto-decides a moot row — a
    machine-made `refused` would destroy a grant a human may want before a redo."""
    task = add_task(store)
    validator_reply = APPROVE + "\nTOOL_REQUEST: shell (blocking) - to reproduce"
    loop, _ = _loop(store, ["worker out", validator_reply])
    loop.run_task(task)

    assert store.get_task(task.id).status is TaskStatus.DONE
    (row,) = store.tool_requests(task_id=task.id)
    assert row.status is ToolRequestStatus.PENDING
    assert row.parked is False


def test_a_redo_of_a_parked_task_parks_again_until_the_request_is_decided(store):
    """E20: the row stays `pending` + `blocking`, so the unmet need is still
    recorded and the next round parks again — the correct answer. The flag is
    cleared by the redo and re-stamped by the re-park."""
    loop, runner, task, row = _parked(store)
    loop.human_redo(task.id)
    assert store.tool_request_get(row.id).parked is False
    assert store.tool_request_get(row.id).status is ToolRequestStatus.PENDING

    runner.outputs = ["worker out again", APPROVE]
    loop.run_task(store.get_task(task.id))

    assert store.get_task(task.id).status is TaskStatus.NEEDS_HUMAN
    assert store.tool_request_get(row.id).parked is True


@pytest.mark.parametrize("gate", ["empty_output", "escalate", "budget", "infra"])
def test_a_stale_parked_flag_cannot_release_a_pre_park_escalation(store, gate):
    """The hole P8's three tests structurally cannot reach.

    Four escalation gates fire *upstream* of the park check, so a task that left
    the parked state by another route (a redo, a pause+resume) could reach them
    with a stale `parked=1` row still standing — and approving that row would
    blank the very diagnosis the human was being asked to act on. `severe` and
    `exhausted` cannot substitute: both are only reachable *after* the park check,
    so a parked row cannot coexist with them inside one round.
    """
    loop, runner, task, row = _parked(store)
    loop.human_redo(task.id)

    if gate == "empty_output":
        runner.outputs = ["   "]
        expected = "empty output"
    elif gate == "escalate":
        runner.outputs = ["ESCALATE: the goal contradicts the criteria"]
        expected = "Worker ambiguity"
    elif gate == "budget":
        loop.config.max_tokens_per_task = 1
        runner.outputs = ["worker out again", APPROVE]
        expected = "budget"
    else:
        runner.outputs = [RuntimeError("provider down")] * 4
        loop.config.infra_max_retries = 1
        loop.config.infra_retry_backoff_s = 0.0
        expected = "infra_error"
    loop.run_task(store.get_task(task.id))

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert expected.lower() in stored.escalation_reason.lower()
    reason = stored.escalation_reason
    events_before = len(store.events(task.id))

    loop.approve_tool_request(row.id)

    after = store.get_task(task.id)
    assert after.status is TaskStatus.NEEDS_HUMAN
    assert after.escalation_reason == reason  # byte-identical
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert event["payload"]["released"] is False
    later = [e for e in store.events(task.id) if e["id"] > events_before]
    assert [e["kind"] for e in later if e["kind"] == "status:pending"] == []


def test_a_stale_parked_flag_cannot_release_a_terminal_task(store):
    """The terminal guard, with its own test rather than by inheritance.

    Every terminal exit clears `parked`, which makes the `status == NEEDS_HUMAN`
    term *redundant* — not optional. So the flag is forced back on directly, which
    is the only way to reach the branch, and the guard is what keeps a tool
    approval from reopening a finished task.
    """
    loop, _, task, row = _parked(store)
    loop.human_approve(task.id, note="good enough")
    assert store.get_task(task.id).status is TaskStatus.DONE
    store.tool_requests_mark_parked(task.id, [row.id])  # a flag that cannot be true

    loop.approve_tool_request(row.id)

    assert store.get_task(task.id).status is TaskStatus.DONE
    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert event["payload"]["released"] is False


def test_a_grant_is_scoped_to_one_task_and_one_role(store):
    """P3: a grant is a task-scoped row, not a standing capability."""
    loop, runner, task_a, row = _parked(store)
    loop.approve_tool_request(row.id)
    task_b = add_task(store)
    runner.outputs = ["a v2", APPROVE, "b out", APPROVE]
    loop.run_task(store.get_task(task_a.id))
    loop.run_task(task_b)

    a_worker, a_validator, b_worker, _ = runner.calls[1:]
    assert "shell" in a_worker["tools"]
    assert "shell" not in a_validator["tools"]  # another role, same task
    assert "shell" not in b_worker["tools"]  # same role, another task
    assert store.granted_tools(task_b.id, "worker") == []


def test_the_park_does_not_consume_a_revision(store):
    """The park is not a revision: it is a gap only a human can close, so
    re-prompting would spend the budget on a call that cannot succeed."""
    loop, runner, task, row = _parked(store)
    assert store.get_task(task.id).revision_count == 0

    loop.approve_tool_request(row.id)
    runner.outputs = ["v2", REVISE, "v3", REVISE, "v4", REVISE]
    loop.config.max_revisions = 2
    loop.run_task(store.get_task(task.id))

    # The full revision budget was still there after the release.
    stored = store.get_task(task.id)
    assert stored.revision_count == 2
    assert "Exhausted 2 revisions" in stored.escalation_reason


def test_optional_side_effecting_request_leaves_the_task_running(store):
    """Design acceptance criterion 3, and it is only non-vacuous here: phase 4b
    asserts the tool is withheld, but "the task still completes" is meaningful
    once the park exists to *not* fire."""
    task = add_task(store)
    loop, runner = _loop(store, [MARK_OPTIONAL_SHELL, APPROVE])
    loop.run_task(task)

    assert store.get_task(task.id).status is TaskStatus.DONE
    assert all("shell" not in c["tools"] for c in runner.calls)
    (row,) = store.tool_requests(task_id=task.id)
    assert row.status is ToolRequestStatus.PENDING


def test_readonly_request_does_not_change_the_outcome(store):
    task = add_task(store)
    loop, _ = _loop(store, [MARK_READONLY, APPROVE])
    loop.run_task(task)

    assert store.get_task(task.id).status is TaskStatus.DONE
    (row,) = store.tool_requests(task_id=task.id)
    assert row.status is ToolRequestStatus.AUTO
    assert [e["kind"] for e in store.events(task.id)].count("tool_auto_approved") == 1


# -- V2: a lease released mid-round must not strand the row --------------------


class _CallbackRunner(MockRunner):
    """A MockRunner that fires a callback just after the Nth call returns — how a
    test reproduces something happening *during* a round."""

    def __init__(self, outputs, at_call, callback):
        super().__init__(outputs)
        self._at_call = at_call
        self._callback = callback

    def run(self, system_prompt, prompt, model, tools=None):
        result = super().run(system_prompt, prompt, model, tools)
        if len(self.calls) == self._at_call:
            self._callback()
        return result


def test_a_transient_status_with_no_lease_is_invisible_to_the_claim(store):
    """The hazard, reproduced directly — the control that makes the fix below
    measurable rather than asserted.

    A row at a transient status with `claimed_by IS NULL` matches **neither**
    disjunct of the claim SELECT (`status='pending'` is false, and `claimed_by=?`
    cannot match NULL) and is invisible to `stranded_claims` (which requires
    `claimed_by IS NOT NULL`) — while `next_pending_task` still reports it. So
    `agentloop status` shows work the loop can never hand out.
    """
    task = add_task(store)
    claimed = store.claim_next_task("loop")
    store.set_status(claimed, TaskStatus.REVISING)
    store.release_claim(task.id)

    assert store.claim_next_task("loop") is None
    assert store.claim_next_task("peer") is None
    assert store.stranded_claims("loop", []) == []
    assert store.next_pending_task().id == task.id  # …and yet it is advertised


def test_a_worker_standing_down_leaves_a_claimable_task(store):
    """A redo landing *mid-round* is the reachable way into the state above: the
    lease goes to NULL while the worker is inside a model call, and the worker's
    own next write stamps a transient status on the row it no longer owns before
    standing down at the boundary.

    A lease *transfer* does not have this shape — the new holder claims the row
    through the in-flight disjunct — so the hazard is specific to
    release-to-NULL, which is what the two runnable release paths do.
    """
    task = add_task(store)
    other = add_task(store)
    loop, _ = _loop(store, [])
    claimed = store.claim_next_task("loop")
    loop.runner = _CallbackRunner(
        ["v1", REVISE], at_call=2, callback=lambda: loop.human_redo(task.id)
    )

    with pytest.warns(RuntimeWarning):
        loop.run_task(claimed)

    stored = store.get_task(task.id)
    assert stored.claimed_by is None
    # Claimable again, by anyone — and the pending task behind it is not starved.
    assert store.claim_next_task("peer").id == task.id
    assert store.claim_next_task("peer2").id == other.id
    # The stand-down is still audited and still writes no verdict-bearing status.
    kinds = [e["kind"] for e in store.events(task.id)]
    assert "claim_lost" in kinds


# -- V3: the ask says what it actually confers ---------------------------------


def test_a_request_event_carries_what_the_logical_name_resolves_to(store):
    """`LOGICAL_TOOL_MAP` maps **both** `git` and `shell` to `["Bash"]`, so a human
    shown "git — commit the fix" who approves it actually grants unrestricted
    Bash. The row, the decision event and the dashboard all say `git`.

    The read-only allowlist excludes both, so this is not an auto-approval bypass:
    it is the ledger's label understating the grant, on the one screen where a
    human's decision is the entire control. Fixed by making the ask
    self-describing rather than by editing the map — the map is out of scope, and
    two logical names sharing a concrete tool is a legitimate thing to express.
    """
    task = add_task(store)
    rid = _add(store, task.id, "git", reason="commit the fix")
    _add(store, task.id, "file_read", status=ToolRequestStatus.AUTO.value)
    _add(store, task.id, "task_state", status=ToolRequestStatus.AUTO.value)

    by_tool = {
        e["payload"]["tool"]: e["payload"]
        for e in store.events(task.id)
        if e["kind"] in ("tool_requested", "tool_auto_approved")
    }
    assert by_tool["git"]["resolved"] == ["Bash"]
    assert by_tool["file_read"]["resolved"] == ["Read"]
    # A logical name that maps to no SDK tool resolves to nothing, and says so
    # rather than being absent — `task_state` is served in-process.
    assert by_tool["task_state"]["resolved"] == []

    store.tool_request_decide(rid, approved=True, by="human")
    (decided,) = [
        e for e in store.events(task.id) if e["kind"] == "tool_request_decided"
    ]
    # The decision event too: that is what an audit reads to answer "what was this
    # human actually granting".
    assert decided["payload"]["resolved"] == ["Bash"]


def test_an_upgraded_request_still_says_what_it_resolves_to(store):
    task = add_task(store)
    _add(store, task.id, "shell", blocking=False)
    _add(store, task.id, "shell", blocking=True)

    upgrade = [
        e["payload"]
        for e in store.events(task.id)
        if e["kind"] == "tool_requested" and e["payload"]["upgraded"]
    ]
    assert [p["resolved"] for p in upgrade] == [["Bash"]]
