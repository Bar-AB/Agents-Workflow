"""Agent-requested tools with an auto-approval policy (slice 5).

Config knobs and domain types (phase 1), the `tool_requests` ledger and the
lease-release accessor (phase 2), the two pre-existing release paths the lease
repair fixes (phase 2b), and the `toolpolicy` seam itself (phase 3).
"""

import json
import threading
import time
from dataclasses import asdict, replace

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
from agentloop.runner import MockRunner, resolve_tools, tools_sharing_capability
from agentloop.store import Store
from agentloop.toolpolicy import (
    ParsedToolRequest,
    ToolClass,
    baseline_tools,
    classify,
    decision_effect,
    effective_tools,
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
    # Slice 6: git is off in the loop tests. A repo per workspace would
    # spawn real subprocesses in ~300 tests that are not about durability.
    cfg_overrides.setdefault("vcs_enabled", False)
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

    **The flag is forced back on after the escalation**, the way
    `test_a_stale_parked_flag_cannot_release_a_terminal_task` already does for the
    terminal case. Without that this test passed through condition 1 — `human_redo`
    clears `parked`, so the release was refused for having no parked row at all and
    the pre-park escalation in the name was never exercised. Condition 2 defends
    only *terminal* statuses, and a pre-park escalation leaves the task at
    NEEDS_HUMAN, which it accepts; so this fails until the predicate can tell a
    park from another escalation holding the same status.
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
    # The state the docstring names, which no live path reaches on its own: a
    # parked row standing against an escalation that is not the park.
    store.tool_requests_mark_parked(task.id, [row.id])
    assert store.tool_request_get(row.id).parked is True
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

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        result = super().run(system_prompt, prompt, model, tools, cwd)
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


# -- E2 remediation cycle 2 ---------------------------------------------------


@pytest.mark.parametrize("exit_", ["approve", "reject", "abort"])
def test_a_human_exit_that_does_not_land_leaves_no_stale_parked_flag(store, exit_):
    """D1: the sibling hole `update_task` becoming lease-predicated opened.

    `set_status` is conditional; `tool_requests_clear_parked` was not. With the
    lease released between the human path's read and its write, all three exits
    left `status=needs_human` with `parked=0` — the transition never happened, and
    condition 1 of the release predicate then failed, so the task was stuck with
    no route back. The park site was already gated; these three were not.

    The lease is real here, not synthetic: the task is claimed and then genuinely
    parked by the loop, so the CAS in `update_task` matches until the hook fires.
    """
    task = add_task(store)
    loop, runner = _loop(store, [MARK_BLOCKING, APPROVE])
    claimed = store.claim_next_task("worker-1")
    assert claimed is not None and claimed.claimed_by == "worker-1"
    loop.run_task(claimed)
    (row,) = store.tool_requests(task_id=task.id)
    assert row.parked and store.get_task(task.id).claimed_by == "worker-1"

    # The window: a lease release landing between the human path's `_require`
    # and its status write, which is what makes the write no-op.
    original_get_task = store.get_task

    def release_the_lease_after_the_read(task_id):
        loaded = original_get_task(task_id)
        if loaded is not None and loaded.claimed_by:
            store.release_claim(task_id)
        return loaded

    store.get_task = release_the_lease_after_the_read
    try:
        getattr(
            loop,
            {"approve": "human_approve", "reject": "human_reject"}.get(exit_, "abort"),
        )(task.id)
    finally:
        store.get_task = original_get_task

    # Fail-closed: the transition did not land, so the park is still the truth.
    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert store.tool_request_get(row.id).parked is True
    # And the route back still exists — the flag is what condition 1 reads.
    loop.approve_tool_request(row.id)
    assert store.get_task(task.id).status is TaskStatus.PENDING


def _hostile(**overrides):
    """A `RunResult` a provider could plausibly hand across the seam, with one
    field of a type nothing on the `ModelRunner` seam promises."""
    from agentloop.runner import RunResult

    fields = dict(output="worker out", tokens_in=11, tokens_out=7, model="mock")
    fields.update(overrides)
    return RunResult(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"output": object()}, id="non_str_output"),
        pytest.param({"tool_calls": None}, id="tool_calls_none"),
        pytest.param({"tool_calls": ["Read"]}, id="tool_calls_of_strings"),
        pytest.param({"tokens_in": "lots"}, id="non_numeric_tokens"),
        pytest.param({"model": {"name": "gpt"}}, id="non_str_model"),
        # E3/C1: the *type* was bounded, the *magnitude* was not. A 400-digit
        # `int` is exactly what `json.loads` yields for a provider body carrying
        # one, passes `_token_count`'s type guard, and then makes
        # `estimate_cost_usd`'s `tokens_in * pin` an `OverflowError` — above the
        # closing transaction, so still three paid calls and `$0.00` recorded.
        pytest.param({"tokens_in": 10**400}, id="astronomical_tokens_in"),
        pytest.param({"tokens_out": 10**400}, id="astronomical_tokens_out"),
        pytest.param({"cache_read_tokens": 10**400}, id="astronomical_cache_read"),
        # E3/C2: `isinstance(str)` does not imply utf-8-encodable. A lone
        # surrogate is what `json.loads` produces from a truncated emoji's high
        # half, so slice 4's shipped HTTP backend reaches this; sqlite then
        # raises *inside* the closing transaction, on `finish_attempt` itself.
        pytest.param({"output": "worker out \ud83d ok"}, id="lone_surrogate_output"),
        pytest.param({"model": "mock\ud83d"}, id="lone_surrogate_model"),
    ],
)
def test_a_hostile_run_result_never_rolls_back_a_paid_attempt(store, overrides):
    """D3: the five remaining raise-sources over an already-paid completion.

    `notes` and the tool *name* are coerced at this seam precisely because the
    `ModelRunner` protocol is a promise and not a guarantee; `output`, the shape
    of `tool_calls` and the cost call's own arguments were trusted at the same
    seam, in the same transaction, for the same money. Each of these five was
    three paid calls, zero attempt rows and an `infra_error` pointing the human
    at the network.
    """
    task = add_task(store)
    loop, runner = _loop(store, [_hostile(**overrides), APPROVE])
    loop.run_task(task)

    # Paid once, recorded once. Three worker prompts is the defect.
    prompts = [e for e in store.events(task.id) if e["kind"] == "worker_prompt"]
    assert len(prompts) == 1
    outputs = [e for e in store.events(task.id) if e["kind"] == "worker_output"]
    assert len(outputs) == 1  # the closing transaction committed
    m = store.task_metrics(task.id)
    assert m["attempts"] >= 1
    assert m["tokens"] > 0
    assert [e for e in store.events(task.id) if e["kind"] == "infra_error"] == []


def test_a_non_str_output_is_not_work_and_is_audited(store):
    """The direction the `output` coercion takes, stated rather than implied: a
    reply that is not text is not work, so it takes the empty-output rule to
    NEEDS_HUMAN instead of being `str()`-ed into a work product a validator would
    review. Not silent — it rides the `runner_warning` event that already exists
    for a degraded run rather than inventing a second event kind."""
    task = add_task(store)
    loop, _ = _loop(store, [_hostile(output=object())])
    loop.run_task(task)

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert "empty output" in stored.escalation_reason
    (warning,) = [e for e in store.events(task.id) if e["kind"] == "runner_warning"]
    assert "object" in warning["payload"]["note"]
    assert store.task_metrics(task.id)["tokens"] > 0  # the money still landed


def test_an_astronomical_token_count_is_clamped_toward_the_budget_cap(store):
    """E3/C1's *direction*, not merely its totality.

    A count too large to price could be zeroed or clamped, and the two are not
    symmetric: zeroing reports an unbounded number as free and lets the budget
    cap through, clamping overstates the spend and trips it toward NEEDS_HUMAN.
    So the expected values are stated per case rather than asserted as "does not
    raise" — a totality assertion with no expected value is what hid a CRLF bug
    here for a whole review cycle.
    """
    from agentloop.agents import _MAX_TOKEN_COUNT, _token_count

    assert _token_count(10**400) == _MAX_TOKEN_COUNT == 2**53
    assert _token_count(7) == 7  # an ordinary count is untouched
    assert _token_count(-3) == 0  # the existing floor still holds
    assert _token_count(float("inf")) == 0  # nan/inf are still garbage, not a cap

    task = add_task(store)
    # A revise round, so the run reaches a second iteration boundary — where the
    # budget check lives. On a single approving round there is no boundary left to
    # read the spend at, which is the pre-existing rule and not this fix's business.
    loop, _ = _loop(store, [_hostile(tokens_in=10**400), REVISE, "out 2", APPROVE])
    loop.run_task(task)

    out = [e for e in store.events(task.id) if e["kind"] == "worker_output"][0]
    assert out["payload"]["tokens_in"] == 2**53  # the recorded number is the clamp
    # Priced without raising, at the zero rate the mock model carries — the
    # token cap is what a clamped count trips, and it is the honest one to assert
    # here: inventing a nonzero cost would need a priced model and would test the
    # pricing table rather than the clamp.
    assert out["payload"]["cost_usd"] == 0.0
    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.NEEDS_HUMAN
    assert "budget" in stored.escalation_reason.lower()


def test_a_lone_surrogate_output_is_escaped_rather_than_discarded(store):
    """E3/C2's direction. A lone surrogate is a real work product carrying one
    character sqlite cannot write, not a non-text reply, so the escape keeps the
    work where blanking it would escalate a task whose worker did the job. Not
    silent: it rides the `runner_warning` event the non-`str` case already uses.
    """
    task = add_task(store)
    loop, _ = _loop(store, [_hostile(output="worker out \ud83d ok"), APPROVE])
    loop.run_task(task)

    stored = store.get_task(task.id)
    assert stored.output == "worker out \\ud83d ok"  # escaped, and still the work
    assert stored.status is TaskStatus.DONE  # not the empty-output escalation
    (warning,) = [e for e in store.events(task.id) if e["kind"] == "runner_warning"]
    assert "surrogate" in warning["payload"]["note"]
    # And the served model gets the same pass, since it lands in a column too.
    other = add_task(store)
    loop2, _ = _loop(store, [_hostile(model="mock\ud83d"), APPROVE])
    loop2.run_task(other)
    assert store.task_metrics(other.id)["attempts"] >= 1


def test_a_tool_call_that_is_not_a_record_is_not_recorded(store):
    """The seam's existing contract — "nothing recorded rather than a wrong
    record" — applied to the *shape* of `tool_calls`, not just to the values in
    it. Non-vacuous by contrast: a well-formed call on the same path is still
    recorded, so this is not "the loop stopped logging tool calls"."""
    task = add_task(store)
    good = _hostile(tool_calls=[{"tool": "Read", "input": {"path": "x"}}])
    loop, _ = _loop(store, [_hostile(tool_calls=["Read"]), APPROVE])
    loop.run_task(task)
    assert [e for e in store.events(task.id) if e["kind"] == "tool_call"] == []

    other = add_task(store)
    loop2, _ = _loop(store, [good, APPROVE])
    loop2.run_task(other)
    (call,) = [e for e in store.events(other.id) if e["kind"] == "tool_call"]
    assert call["payload"]["tool"] == "Read"


# -- phase 6: the inertness differential + the `ModelRunner` contract note ------
#
# Columns whose value is a wall clock rather than a decision. A differential that
# kept them would report a difference between any two runs of anything and prove
# nothing — which is the failure mode P1-control-a exists to rule out from the
# other side (a harness that compares nothing at all).
_VOLATILE_COLUMNS = frozenset({"created_at", "decided_at", "started_at", "finished_at"})

# Marker-free and marker-bearing versions of one script: the same task, the same
# revise-then-approve shape, differing only in the marker line. Both sides of
# every comparison below run one of these two.
_CLEAN_SCRIPT = ["worker out", REVISE, "worker out v2", APPROVE]
_MARKER_SCRIPT = [MARK_BLOCKING, REVISE, "worker out v2", APPROVE]


def _stable_rows(store, table, task_id):
    """Every column of `table` for one task, in id order, minus the clocks."""
    return [
        {k: r[k] for k in r.keys() if k not in _VOLATILE_COLUMNS}
        for r in store._conn.execute(
            f"SELECT * FROM {table} WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
    ]


def _observable_state(store, runner) -> dict:
    """Everything a run leaves behind that anything downstream can act on.

    A state *diff*, not a list of mirrored assertions: an assertion per field
    only proves the fields somebody thought to name, and the inertness claim is
    about the fields nobody thought of. So this captures whole rows (`SELECT *`
    minus the clocks) rather than a hand-picked projection, and the two dicts are
    compared with `==`.

    **What it deliberately leaves out, and why: the system prompt.** Slice 5's
    E2 taught the `TOOL_REQUEST` grammar to the `worker`, `validator` and
    `planner` system prompts and moved those three roles to version "3" — without
    it nothing ever told an agent it could ask for a tool, so the marker half of
    the slice was dead code that still passed every test. That is the same
    disclosed change slice 4's charter made at version "2", and it means the
    system prompt is **not** byte-for-byte the pre-slice-5 one and is not claimed
    to be. Measuring it here would also be tautological: both sides of every
    comparison below read the same registry, so the field can only ever be equal,
    and a reader would take its presence as proof of something that is false.
    The system prompt's content is pinned deliberately instead, by
    `test_the_shipped_prompt_teaches_the_marker_grammar` and
    `test_the_summarizer_is_never_taught_the_grammar`.

    What *is* measured is the surface an unconfigured project can observe: the
    task row, every verdict and attempt column, the spend, the ordered event-kind
    sequence, every **user** prompt, every `tools` list **with its order**, and
    the `tool_requests` ledger itself.
    """
    state = {"calls": [], "tasks": []}
    for call in runner.calls:
        state["calls"].append(
            {
                "prompt": call["prompt"],
                "model": call["model"],
                # Order, not membership: the `{kind}_prompt` event records the
                # list as a list, so a reordering is a visible change.
                "tools": list(call["tools"]),
            }
        )
    for task in store.list_tasks():
        metrics = store.task_metrics(task.id)
        events = store.events(task.id)
        state["tasks"].append(
            {
                "task": {
                    **{k: v for k, v in asdict(task).items() if k != "status"},
                    "status": task.status.value,
                },
                "attempts": metrics["attempts"],
                "tokens": metrics["tokens"],
                "cost_usd": metrics["cost_usd"],
                "verdicts": _stable_rows(store, "verdicts", task.id),
                "attempt_rows": _stable_rows(store, "attempts", task.id),
                "tool_requests": _stable_rows(store, "tool_requests", task.id),
                "event_kinds": [e["kind"] for e in events],
                "prompt_events": [
                    (
                        e["kind"],
                        e["payload"]["role"],
                        e["payload"]["prompt"],
                        tuple(e["payload"]["tools"]),
                    )
                    for e in events
                    if e["kind"].endswith("_prompt")
                ],
                "output_events": [
                    (e["kind"], e["payload"]["role"], e["payload"]["output"])
                    for e in events
                    if e["kind"].endswith("_output")
                ],
            }
        )
    return state


def _run_and_observe(tmp_path, tag, script, *, neutered, monkeypatch):
    """One task through the `Loop` in its own db, sharing one `workspace_root`.

    Two fresh dbs mean both tasks are id 1, so every input to every prompt is
    identical by construction and only the script and the patch can differ — the
    `tests/test_charter.py:96` construction.

    The neutering patches **`agentloop.agents`**, not `agentloop.toolpolicy`:
    `agents.py` binds its imports as names, so the live call resolves
    `agents.tools_for`, and a patch on the `toolpolicy` module object would be
    inert. That is not a stylistic preference — an inert patch would make the
    "neutered" run *be* the live run, and P1 would pass by comparing a run to
    itself. `test_the_neutering_patch_provably_takes_effect` is what detects it.
    """
    from agentloop import agents

    store = Store(tmp_path / f"{tag}.db")
    try:
        task = add_task(store)
        loop, runner = _loop(store, list(script), workspace_root=str(tmp_path / "ws"))
        if neutered:
            with monkeypatch.context() as mp:
                mp.setattr(
                    agents,
                    "tools_for",
                    lambda store, config, spec, task_id, agent_kind: list(spec.tools),
                )
                mp.setattr(agents, "parse_tool_requests", lambda text: [])
                loop.run_task(task)
        else:
            loop.run_task(task)
        return _observable_state(store, runner)
    finally:
        store.close()


def test_the_neutering_patch_targets_the_names_the_live_path_resolves():
    """The mechanical half of ADVISORY 6, checked rather than trusted.

    Both hooks must be attributes *of `agentloop.agents`* and must be the
    `toolpolicy` functions themselves, because that is what makes patching the
    consumer's namespace equivalent to removing the slice. If a later refactor
    changed `agents.py` to `import toolpolicy` and call `toolpolicy.tools_for`,
    every patch below would go silently inert — so this asserts the import style
    the differential depends on, at the one place that depends on it.
    """
    from agentloop import agents, toolpolicy

    assert agents.tools_for is toolpolicy.tools_for
    assert agents.parse_tool_requests is toolpolicy.parse_tool_requests


def test_marker_free_default_run_is_byte_for_byte_the_pre_slice_5_run(
    tmp_path, monkeypatch
):
    """P1. On default config with a marker-free reply, slice 5 is not merely
    quiet — it is absent, measured by neutering it and comparing the whole
    observable state of the two runs.

    **Scope, narrowed deliberately — do not "fix" this back to comparing system
    prompts.** The claim is about the *user* prompt, the `tools` lists and the
    persisted state. The three agent system prompts *did* change for every
    project, chartered or not: E2 taught them the `TOOL_REQUEST` grammar and
    moved `worker`/`validator`/`planner` to version "3", because injecting a
    marker parser while telling no agent the marker exists is a feature that
    cannot fire. Slice 4 made the same disclosed change at version "2". See
    `_observable_state`'s docstring for why measuring it here would also be
    tautological.

    A revise round is used rather than a single approving one so the differential
    covers a second worker invocation, a verdict row and the iteration boundary
    where the park check sits.
    """
    live = _run_and_observe(
        tmp_path, "live", _CLEAN_SCRIPT, neutered=False, monkeypatch=monkeypatch
    )
    neutered = _run_and_observe(
        tmp_path, "neutered", _CLEAN_SCRIPT, neutered=True, monkeypatch=monkeypatch
    )

    assert live == neutered
    # And the run really exercised something: an empty state would compare equal.
    assert live["tasks"][0]["task"]["status"] == TaskStatus.DONE.value
    assert len(live["calls"]) == 4  # worker, validator, worker, validator
    assert live["tasks"][0]["tool_requests"] == []
    assert live["tasks"][0]["verdicts"]  # a verdict row was written and compared


def test_the_differential_harness_detects_a_difference_when_one_exists(
    tmp_path, monkeypatch
):
    """P1-control-a: the harness compares something. Live on both sides, the only
    difference the *script* — so a harness that captured an empty or constant
    state would fail here while letting P1 pass on nothing."""
    clean = _run_and_observe(
        tmp_path, "clean", _CLEAN_SCRIPT, neutered=False, monkeypatch=monkeypatch
    )
    marked = _run_and_observe(
        tmp_path, "marked", _MARKER_SCRIPT, neutered=False, monkeypatch=monkeypatch
    )

    assert clean != marked
    # Named, not just "different": the marker parks the task on a queued request.
    assert marked["tasks"][0]["task"]["status"] == TaskStatus.NEEDS_HUMAN.value
    assert len(marked["tasks"][0]["tool_requests"]) == 1
    assert clean["tasks"][0]["tool_requests"] == []


def test_the_neutering_patch_provably_takes_effect(tmp_path, monkeypatch):
    """P1-control-b: the *patch* is what varies, on one fixed marker-bearing
    script. It is the only one of the three that can catch an inert
    `parse_tool_requests` patch — P1-control-a varies the script, so it would
    still pass with both runs live, and P1 itself would then be comparing a run
    to itself and passing for the wrong reason.

    Measured limit, stated rather than implied: it does **not** catch an inert
    `tools_for` patch. On the marker-free script there is no request row, so a
    live `tools_for` already returns `spec.tools` and the two runs agree either
    way; on the marker script the task parks before a second worker invocation,
    so no invocation ever sees a granted tool. What pins that half is
    `test_the_neutering_patch_targets_the_names_the_live_path_resolves` (the
    patch target is the identity `agents.tools_for is toolpolicy.tools_for`) plus
    P1's own reliance on a `tools_for` that would fail P1 if it reversed its
    output. The guarantee holds; this test is not what holds it.

    The project's own non-vacuity rule (the seam AST walk proved by also running
    without its exclusion: 0 hits vs 7) applied to this slice's flagship proof.
    """
    live = _run_and_observe(
        tmp_path, "live", _MARKER_SCRIPT, neutered=False, monkeypatch=monkeypatch
    )
    neutered = _run_and_observe(
        tmp_path, "neutered", _MARKER_SCRIPT, neutered=True, monkeypatch=monkeypatch
    )

    assert live != neutered
    # The specific difference the patch removes: the marker became a row and a
    # park when live, and nothing at all when neutered.
    assert len(live["tasks"][0]["tool_requests"]) == 1
    assert neutered["tasks"][0]["tool_requests"] == []
    assert live["tasks"][0]["task"]["status"] == TaskStatus.NEEDS_HUMAN.value
    assert neutered["tasks"][0]["task"]["status"] == TaskStatus.DONE.value


def test_model_runner_documents_the_tools_contract():
    """Enforcement is only as good as a backend honoring its `tools` argument.

    The gate's entire bite is the list handed to `runner.run`, and the seam
    cannot check that an implementation obeyed it — so the requirement is stated
    at the seam that owns it, in the same register as `sandbox_isolation='strict'`
    degrading with a warning: a documented residual risk, recorded where an
    implementer reads it rather than in a plan nobody will open.
    """
    from agentloop.runner import ModelRunner

    doc = ModelRunner.run.__doc__ or ""
    assert "must honor" in doc
    # Both shipped backends' actual behavior is named, so "honor" is not abstract.
    assert "allowed_tools" in doc  # ClaudeSDKRunner passes it through
    assert "OpenAICompatRunner" in doc  # drops it with a warning, executes none
    # And the consequence of ignoring it, which is the reason the note exists.
    assert "silently" in doc


# -- E3/G1: a denial removes the concrete capability, not merely the name -------
#
# The gate's currency is the logical name, but what reaches the SDK is
# `resolve_tools(tools)`. Two logical names share one concrete tool (`git` and
# `shell` both resolve to `Bash`), and the shipped worker declares `git` — so a
# human rejecting `shell` used to leave `Bash` in the SDK's `allowed_tools` while
# the ledger and the `worker_prompt` event both recorded `shell` as withheld.
# That is a gate a human believes is closed and is not. The decision was to fail
# closed: the denial removes the concrete capability, even when that also disables
# another logical tool the role already held.


def test_a_rejected_tool_takes_down_the_capability_it_shares(store):
    """The design fix, at the enforcement seam. A rejected `shell` removes `Bash`,
    so the `git` the role declared stops working — and the *control* is the same
    row approved, which leaves `git` in place. Asserted through `resolve_tools` as
    well as the logical list, because the SDK sees only the former."""
    from agentloop.runner import resolve_tools

    task = add_task(store)
    spec = _spec(["git", "search", "task_state"])
    request_id = _add(store, task.id, "shell")
    store.tool_request_decide(request_id, approved=False, by="human")

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == [
        "search",
        "task_state",
    ]
    assert "Bash" not in resolve_tools(
        tools_for(store, LoopConfig(), spec, task.id, "worker")
    )

    # Control: the identical row, approved, subtracts nothing at all.
    other = add_task(store)
    approved = _add(store, other.id, "shell")
    store.tool_request_decide(approved, approved=True, by="human")
    assert tools_for(store, LoopConfig(), spec, other.id, "worker") == [
        "git",
        "search",
        "task_state",
        "shell",
    ]


def test_a_pending_tool_request_withholds_a_capability_the_role_lacks(store):
    """A pending ask subtracts the concrete capability **it alone would confer**.

    Rewritten in slice 8, and the reasoning matters because changing a test to
    match code is normally the wrong move. This test predates the E3/G1
    remediation block below and was never reconciled with it. That block, and
    CLAUDE.md's decision-rules section, both state the rule in terms of
    *capability*: "an **undecided** (pending) request for a capability the role
    already holds ... takes nothing away, because nobody was ever asked", and
    the reason given is that otherwise an agent revokes its own baseline simply
    by asking for it.

    The old assertion used `spec=["git", "search"]` with a pending `shell`, and
    `git` and `shell` both resolve to `Bash` — so the role *already held* the
    capability, and the row was subtracting it anyway. `LOGICAL_TOOL_MAP` is not
    injective, so a name-level test of "already holds" and a capability-level
    subtraction disagree on exactly the collision pairs, and the shipped worker
    prompt teaches `TOOL_REQUEST: shell` by worked example.

    So the case is split. Here the role holds **no** `Bash` at all, which is the
    scenario the original name was describing, and the pending row still
    withholds — the audit trail and the resolution agree.
    """
    task = add_task(store)
    _add(store, task.id, "shell")
    spec = _spec(["search"])  # no `git`: the role holds no Bash of its own

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == ["search"]


def test_a_pending_ask_for_a_capability_the_role_already_holds_takes_nothing(store):
    """The other half of the split above, and the defect it was hiding."""
    task = add_task(store)
    _add(store, task.id, "shell")
    spec = _spec(["git", "search"])  # `git` already confers Bash

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == [
        "git",
        "search",
    ]


def test_an_auto_row_never_subtracts_anything(store):
    """`auto` is a grant, and a grant adds. Non-vacuous next to the two above: the
    same `shell` name, the same shared `Bash`, the opposite outcome."""
    task = add_task(store)
    _add(store, task.id, "shell", status=ToolRequestStatus.AUTO.value)

    assert tools_for(store, LoopConfig(), _spec(["git"]), task.id, "worker") == [
        "git",
        "shell",
    ]


def test_a_machine_refused_row_does_not_subtract(store):
    """The line the denial set is drawn at, stated rather than left to be found.

    The set is the asks a *human's* decision governs — `pending` (awaiting one)
    and `rejected` (made). A `refused` row is the machine declining to queue: an
    unknown name confers nothing concrete anyway, and an over-cap refusal would
    otherwise let an agent's own chattiness strip its role's shipped baseline with
    no human in the loop, which is a capability loss nobody decided.
    """
    task = add_task(store)
    _add(store, task.id, "shell", status=ToolRequestStatus.REFUSED.value)

    assert tools_for(store, LoopConfig(), _spec(["git"]), task.id, "worker") == ["git"]


def test_a_tool_sharing_nothing_concrete_is_untouched(store):
    """The subtraction is over the concrete set, so it is exactly as narrow as that
    set is. A rejected `web` (WebFetch/WebSearch) takes down nothing else, and
    `task_state` — which resolves to no SDK tool at all — can never be collateral.
    """
    task = add_task(store)
    rejected = _add(store, task.id, "web")
    store.tool_request_decide(rejected, approved=False, by="human")
    spec = _spec(["git", "file_io", "task_state"])

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == [
        "git",
        "file_io",
        "task_state",
    ]


def test_an_optional_ask_never_costs_the_worker_a_capability_it_already_had(store):
    """End-to-end, and slice 8's inversion of this test.

    It used to assert the opposite: that round 2's `tools` had *lost* the `git`
    the shipped role declares, because the worker wrote `TOOL_REQUEST: shell`
    and `shell`/`git` both resolve to `Bash`. That is the self-revocation
    CLAUDE.md's rule exists to prevent — "an undecided (pending) request for a
    capability the role already holds ... takes nothing away, because nobody was
    ever asked" — and it fired on an **optional** ask, which never reaches a
    human queue at all. The shipped worker prompt teaches `TOOL_REQUEST: shell`
    by worked example, so this was the taught path.

    The audit half of the old test has not been dropped, only moved to where the
    loss is real: a human's **rejection**, which still subtracts unconditionally
    (see `test_a_rejected_baseline_capability_loss_is_audited`).
    """
    task = add_task(store)
    loop, runner = _loop(store, [MARK_OPTIONAL_SHELL, REVISE, "worker out v2", APPROVE])
    loop.run_task(task)

    worker_calls = [c for c in runner.calls if "# Task:" in c["prompt"]]
    assert "git" in worker_calls[0]["tools"]  # round 1: no row existed yet
    assert "git" in worker_calls[1]["tools"]  # round 2: still held, nobody decided
    assert "shell" not in worker_calls[1]["tools"]  # ...and still not granted

    # Nothing was taken, so nothing is announced as taken. A withholding event
    # here would be the mirror defect: telling a human a capability was lost
    # when the resolution kept it.
    assert not [
        e for e in store.events(task.id) if e["kind"] == "tool_capability_withheld"
    ]
    # The request itself is still recorded — the row and its audit are not what
    # changed, only what the undecided row costs.
    assert [e for e in store.events(task.id) if e["kind"] == "tool_requested"]
    prompts = [e for e in store.events(task.id) if e["kind"] == "worker_prompt"]
    assert "git" in prompts[1]["payload"]["tools"]


def test_a_rejected_baseline_capability_loss_is_audited(store):
    """The surprising half of the design, made visible rather than discovered —
    kept from the test above and re-aimed at the decision that really causes it.

    A human rejects `shell`; `git` shares its `Bash` and stops working. That is
    the fail-closed direction (a human who believes they closed a gate must find
    it closed), and it is exactly the side effect that must not be left to be
    discovered from the source.
    """
    task = add_task(store)
    spec = _spec(["file_io", "git", "search", "task_state"])
    rid = _add(store, task.id, "shell")
    store.tool_request_decide(rid, approved=False, by="human")

    assert "git" not in tools_for(store, LoopConfig(), spec, task.id, "worker")

    (event,) = [
        e for e in store.events(task.id) if e["kind"] == "tool_capability_withheld"
    ]
    payload = event["payload"]
    assert payload["withheld"] == ["shell"]
    assert payload["also_lost"] == ["git"]
    assert payload["capability"] == ["Bash"]
    # And as a sentence, so the fact does not depend on a reader joining two lists.
    assert "git" in payload["message"] and "shell" in payload["message"]


def test_the_queued_request_says_what_deciding_it_also_decides(store):
    """The second surface: the row a human is shown when they approve or reject.

    Which other logical names a decision on this one also settles is a pure fact
    about the tool map, so it is recorded on the request's own event at creation —
    the text an approve prompt is built from — rather than left to be joined out of
    a later withholding event.
    """
    task = add_task(store)
    loop, _ = _loop(store, [MARK_BLOCKING, APPROVE])
    loop.run_task(task)

    (event,) = [e for e in store.events(task.id) if e["kind"] == "tool_requested"]
    assert event["payload"]["also_decides"] == {"git": ["Bash"]}
    # The decision event carries it too, so "what did this click actually do" is
    # answerable from the decision alone.
    (row,) = store.tool_requests(task_id=task.id)
    loop.reject_tool_request(row.id)
    (decided,) = [
        e for e in store.events(task.id) if e["kind"] == "tool_request_decided"
    ]
    assert decided["payload"]["also_decides"] == {"git": ["Bash"]}


def test_the_subtraction_is_a_property_of_the_whole_resolution(store):
    """Per-request, or once over the final list? Once.

    A grant is appended *after* the declared loop, so subtracting per request would
    let a later grant re-introduce a concrete capability an earlier denial removed
    — and the answer would depend on which loop ran last. Here `git` is granted
    (which adds `Bash`) while `shell` is rejected (which removes it): fail closed,
    in either order.
    """
    task = add_task(store)
    granted = _add(store, task.id, "git")
    store.tool_request_decide(granted, approved=True, by="human")
    rejected = _add(store, task.id, "shell")
    store.tool_request_decide(rejected, approved=False, by="human")

    assert tools_for(store, LoopConfig(), _spec(["search"]), task.id, "worker") == [
        "search"
    ]


def test_the_denial_survives_the_gate_being_off(store):
    """`gate_declared_tools` decides whether a *declared* tool needs a grant. It
    has nothing to do with honoring a decision already made, so a rejected tool
    subtracts on the default config too — the marker path creates rows with the
    knob off, which is the shipped configuration."""
    task = add_task(store)
    rejected = _add(store, task.id, "shell")
    store.tool_request_decide(rejected, approved=False, by="human")
    cfg = LoopConfig()

    assert cfg.gate_declared_tools is False
    assert tools_for(store, cfg, _spec(["git", "search"]), task.id, "worker") == [
        "search"
    ]


def test_the_model_runner_note_names_the_residual_it_still_carries():
    """The note claims the `tools` list is the entire enforcement surface. After
    the fix that is true of the *concrete* capability, so the note must say which
    set the guarantee is over — and name the residual it keeps (a backend that
    ignores the argument) rather than dropping the claim."""
    from agentloop.runner import ModelRunner

    doc = ModelRunner.run.__doc__ or ""
    assert "concrete" in doc
    assert "collateral" in doc  # the surprising consequence, stated at the seam


# -- E3/G2: a clamped count is a substitution, and says so ---------------------


def test_a_clamped_token_count_is_audited_as_a_substitution(store):
    """A degraded number must not land in `attempts` indistinguishable from a
    measured one — slice 4's whole reason for `runner_warning`. The surrogate
    branch in the same diff fires it; the clamp did not, so a single approving
    round reached DONE carrying 9.0e15 tokens with an empty warning feed.
    """
    from agentloop.agents import _MAX_TOKEN_COUNT, _clamped_count

    assert _clamped_count(10**400) is True
    assert _clamped_count(_MAX_TOKEN_COUNT) is False  # the ceiling itself is a number
    assert _clamped_count(7) is False
    assert _clamped_count("lots") is False  # not a count at all; the type guard owns it
    assert _clamped_count(float("nan")) is False  # garbage, zeroed rather than clamped

    task = add_task(store)
    # One approving round: the task reaches DONE, so nothing about the escalation
    # direction rescues this — the audit event is the only signal there is.
    loop, _ = _loop(store, [_hostile(tokens_in=10**400), APPROVE])
    loop.run_task(task)

    assert store.get_task(task.id).status is TaskStatus.DONE
    (warning,) = [e for e in store.events(task.id) if e["kind"] == "runner_warning"]
    assert "tokens_in" in warning["payload"]["note"]
    assert "ceiling" in warning["payload"]["note"]
    # The substituted number really is what landed, so the warning is the only
    # thing distinguishing it from a measurement. Read off the worker's own event
    # rather than `task_metrics`, which sums the validator's real tokens on top.
    out = [e for e in store.events(task.id) if e["kind"] == "worker_output"][0]
    assert out["payload"]["tokens_in"] == 2**53
    assert store.task_metrics(task.id)["tokens"] >= 2**53


def test_the_clamp_docstring_states_the_caveat_its_test_does():
    """The docstring claimed the clamp "trips the budget cap toward NEEDS_HUMAN".
    That holds only when a later iteration boundary is reached; on an approving
    single round the task reaches DONE carrying the ceiling. The test named the
    caveat honestly and the docstring stated the guarantee without it."""
    from agentloop.agents import _token_count

    doc = _token_count.__doc__ or ""
    assert "iteration boundary" in doc


# -- E3/G4: the neutering must cover every `toolpolicy` name `agents` binds -----


def test_the_neutering_covers_every_toolpolicy_name_agents_binds():
    """The patch-target guard pins the import *style* of two names; this pins the
    *set*.

    `agents` also binds `classify` and `ToolClass` and calls `classify` live in
    `_invoke` — harmless today only because the patched `parse_tool_requests`
    returns `[]`, which makes that call dead in the neutered arm. A future entry
    point not downstream of those two would stay live in **both** arms, and the
    differential would compare live-to-live across it. Read from the source rather
    than from the module namespace, so a rebinding cannot hide from it.
    """
    import ast
    from pathlib import Path

    from agentloop import agents

    tree = ast.parse(Path(agents.__file__).read_text(encoding="utf-8"))
    bound = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "toolpolicy"
        for alias in node.names
    }
    assert bound == {
        # Patched by the differential.
        "tools_for",
        "parse_tool_requests",
        # Reachable only through `parse_tool_requests`' output, so neutering that
        # one neuters these: `classify` runs over the parsed list, `ToolClass` keys
        # `_TOOL_CLASS_STATUS`, and `MAX_TOOL_REASON_CHARS` bounds a parsed reason.
        # Adding a name here forces a decision about whether the neutering has to
        # cover it too.
        "classify",
        "ToolClass",
        "MAX_TOOL_REASON_CHARS",
    }


# -- E3/G1 remediation: asking for what you already hold must be neutral --------
#
# G1 made a withheld tool's *concrete* capability subtract from the whole resolved
# list, and `withheld_tools` is `pending` + `rejected`. The marker path queues a
# `pending` row for **any** gated logical name, including one the role already
# holds — so a worker writing `TOOL_REQUEST: file_io` revoked its own baseline
# `Read`/`Write`/`Edit`, and did it unauditably (the requested tool never entered
# `lost`, so the withholding event's guard never fired).
#
# The distinction is *who decided*: a `pending` row is nobody's decision, so on a
# tool the role already holds it is a no-op; a `rejected` row is a human's denial
# and still subtracts, which is the whole point of G1.


def test_a_pending_row_for_a_tool_the_role_already_holds_is_a_no_op(store):
    """Nobody decided anything: the row is non-blocking, no human is ever prompted
    (`pending_blocking_tool_requests` cannot see it), and `classify` promises the
    ask is harmless. So the resolution is exactly what it was without the row."""
    task = add_task(store)
    spec = _spec(["file_io", "git", "search", "task_state"])
    before = tools_for(store, LoopConfig(), spec, task.id, "worker")

    _add(store, task.id, "file_io")  # the agent asks for what it already has

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == before
    assert before == ["file_io", "git", "search", "task_state"]
    # ...and a second baseline name behaves the same way, including the one that
    # shares `Bash` with `shell`.
    _add(store, task.id, "git")
    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == before


def test_a_rejected_row_for_a_tool_the_role_already_holds_still_subtracts(store):
    """The other direction, and the control for the test above: a human said no.
    Making the baseline exempt from *that* would re-open the cosmetic-denial hole
    G1 closed — rejecting `file_io` would achieve nothing."""
    task = add_task(store)
    spec = _spec(["file_io", "git", "search", "task_state"])
    rid = _add(store, task.id, "file_io")
    store.tool_request_decide(rid, approved=False, by="human")

    assert tools_for(store, LoopConfig(), spec, task.id, "worker") == [
        "git",
        "search",
        "task_state",
    ]


def test_a_denied_baseline_tool_is_audited_even_with_no_collateral(store):
    """The audit gap, independent of the no-op rule. `lost` is *collateral* only,
    so a denial that removes nothing but the requested tool itself fired no event
    — the one case with no other signal. The guard is now "a capability was
    actually lost", not "somebody else lost one"."""
    task = add_task(store)
    rid = _add(store, task.id, "file_io")
    store.tool_request_decide(rid, approved=False, by="human")

    tools_for(store, LoopConfig(), _spec(["file_io", "search"]), task.id, "worker")

    (event,) = [
        e for e in store.events(task.id) if e["kind"] == "tool_capability_withheld"
    ]
    payload = event["payload"]
    assert payload["removed"] == ["file_io"]
    assert payload["also_lost"] == []  # nothing collateral: it was the ask itself
    assert "Read" in payload["capability"]
    assert "file_io" in payload["message"]


def test_asking_for_a_declared_tool_changes_nothing_end_to_end(store):
    """The blind spot the suite had, closed deliberately: every marker-path test
    used `web` (shares no concrete tool) or `shell` (never declared), so none
    exercised a marker naming a tool the role already holds. Two arms, identical
    scripts but for the marker line — the tools the runner is handed must match."""
    marker = "worker out\nTOOL_REQUEST: file_io (optional) - want to write files"
    script = [REVISE, "worker out v2", APPROVE]

    def worker_tools(r):
        return [c["tools"] for c in r.calls if "# Task:" in c["prompt"]]

    control = add_task(store)
    loop_c, runner_c = _loop(store, ["worker out", *script])
    loop_c.run_task(control)

    task = add_task(store)
    loop, runner = _loop(store, [marker, *script])
    loop.run_task(task)

    assert worker_tools(runner) == worker_tools(runner_c)
    assert "file_io" in worker_tools(runner)[1]
    # And the ask itself is still audited — neutral is not silent.
    assert any(e["kind"] == "tool_requested" for e in store.events(task.id))


def test_a_non_string_declared_tool_never_raises_with_the_gate_off(store):
    """`agents.json` loads unvalidated through `AgentSpec(**spec)`, and with the
    gate off `allowed` carried raw `spec.tools` entries into a `set` membership
    test. `tools: [["file_io"]]` raised `TypeError: unhashable type` — during
    prompt construction, inside `_with_retry`, so three retries reported as
    `infra_error` pointing the human at the network instead of at the registry."""
    task = add_task(store)
    _add(store, task.id, "shell")  # a withheld row: what made the branch reachable

    assert tools_for(
        store, LoopConfig(), _spec([["file_io"], "search"]), task.id, "worker"
    ) == ["['file_io']", "search"]


def test_the_withholding_message_names_only_the_tools_that_withheld(store):
    """`task_state` resolves to no concrete tool, so it confers nothing and cannot
    withhold anything. Interpolating the whole withheld list read "…the same
    capability task_state and shell confers", naming a tool that did nothing."""
    task = add_task(store)
    for tool in ("task_state", "shell"):
        store.tool_request_decide(
            _add(store, task.id, tool), approved=False, by="human"
        )

    tools_for(store, LoopConfig(), _spec(["git"]), task.id, "worker")

    (event,) = [
        e for e in store.events(task.id) if e["kind"] == "tool_capability_withheld"
    ]
    message = event["payload"]["message"]
    assert "shell" in message
    assert "task_state" not in message


def test_the_classify_docstring_no_longer_promises_a_harmless_row():
    """H1. `classify`'s rationale for not consulting `baseline_tools` was that a
    marker naming a baseline tool "reaches the runner anyway — audited, harmless".
    G1 invalidated that premise; the premise is a code dependency, not prose."""
    doc = classify.__doc__ or ""
    assert "harmless" not in doc
    assert "tools_for" in doc  # it says where the distinction actually lives


def test_the_tools_for_docstring_states_what_the_gate_off_path_really_does():
    """H1, second half. It claimed that with the gate off "every declared tool
    passes through untouched and this returns `spec.tools`' content and order,
    writing nothing". Both halves are false since G1: it can return less, and it
    can write (the withholding event)."""
    doc = tools_for.__doc__ or ""
    assert "untouched" not in doc
    assert "no `tool_requests` row" in doc


def test_the_model_runner_note_states_the_real_blast_radius():
    """L2. The note said the subtraction removes "a withheld or rejected tool's
    concrete footprint". The code is coarser: *any* overlap drops the whole logical
    name, so a denied `Read` also removes `Write` and `Edit` via `file_io`."""
    from agentloop.runner import ModelRunner

    doc = ModelRunner.run.__doc__ or ""
    assert "whole logical name" in doc


# -- E4/F1: the previewed consequence is the enforced one --------------------
#
# The read-only surfaces (CLI `tools list`, the dashboard panel) used to state the
# consequence of a decision from `LOGICAL_TOOL_MAP` alone, which knows neither the
# row's role nor its siblings' statuses. These tests pin the two halves of the fix:
# the preview is computed by the *same* function the gate enforces, and computing
# it writes nothing.


def _effect(store, tool, spec, config=None):
    """`decision_effect` for the row named `tool`, against `spec`'s declared list."""
    request = next(
        r for r in store.tool_requests() if r.tool == tool and r.role == spec.role
    )
    return decision_effect(
        store, config or LoopConfig(), spec.role, list(spec.tools), request
    )


def test_a_previewed_grant_of_nothing_is_what_the_gate_then_enforces(store):
    """F1 state 1 — and the anti-drift proof, which is the point of the extraction.

    `git` rejected, `shell` pending: the preview says approving grants no
    capability. The assertion is not that the preview says so, but that
    `tools_for` — the gate — then agrees, having been given the approval. Two
    implementations of "what would this role get" could not both be checked by one
    test; one implementation is why this test is possible at all.
    """
    task = add_task(store)
    spec = _spec(["file_io", "git", "search", "task_state"])
    store.tool_request_decide(_add(store, task.id, "git"), approved=False, by="human")
    shell = _add(store, task.id, "shell")

    effect = _effect(store, "shell", spec)
    assert effect.approve_grants == []  # what the screen promises
    assert effect.approve_enables == []

    store.tool_request_decide(shell, approved=True, by="human")
    enforced = tools_for(store, LoopConfig(), spec, task.id, "worker")
    # ...and what the gate does. `Bash` is still gone, on account of the rejected
    # `git`, exactly as the preview said.
    assert "Bash" not in resolve_tools(enforced)


def test_a_previewed_grant_of_bash_is_also_what_the_gate_enforces(store):
    """The non-vacuity control for the test above: same shape, no rejected sibling,
    and now the preview promises `Bash` and the gate delivers it. Without this,
    the pair above would pass on a preview that always promised nothing.

    Slice 8 dropped `git` from the spec. It used to be declared, and the control
    got its non-vacuity from the *defect*: the pending `shell` stripped the
    role's own `git`, so approving appeared to "grant Bash" when it was only
    handing back what the ask had just taken. With a role that genuinely has no
    Bash, the promise and the delivery are both about a real new capability."""
    task = add_task(store)
    spec = _spec(["file_io", "search", "task_state"])  # no `git`: no Bash held
    shell = _add(store, task.id, "shell")

    effect = _effect(store, "shell", spec)
    assert effect.approve_grants == ["Bash"]
    assert effect.costs_now == []  # an undecided ask costs nothing
    assert effect.reject_removes == []  # and there is no sibling to take down

    store.tool_request_decide(shell, approved=True, by="human")
    assert "Bash" in resolve_tools(tools_for(store, LoopConfig(), spec, task.id, "w"))


def test_the_sharing_map_alone_gets_every_one_of_the_three_states_wrong(store):
    """The targeted-revert control, in-suite. `tools_sharing_capability` is
    non-empty in all three states, so the text it alone can produce — "approving
    grants it too; rejecting stops it working" — is false in each: no grant in the
    first, nothing lost in the second (`git` is already withheld) and nothing lost
    in the third (the validator never declared `git`)."""
    worker = _spec(["file_io", "git", "search", "task_state"])
    validator = _spec(["file_io", "search", "task_state"], role="validator")

    # The map's answer is the same in all three states — that is the defect.
    assert tools_sharing_capability("shell") == {"git": ["Bash"]}

    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "git"), approved=False, by="human")
    _add(store, task.id, "shell")
    state_1 = _effect(store, "shell", worker)

    other = add_task(store)
    _add(store, other.id, "shell")
    state_2 = _effect(store, "shell", worker)

    third = add_task(store)
    _add(store, third.id, "shell", role="validator", agent_kind="validator")
    state_3 = _effect(store, "shell", validator)

    assert state_1.approve_grants == []  # "approving grants it too" — false
    assert state_2.reject_removes == []  # "rejecting stops it working" — false
    assert state_3.reject_removes == []  # ...and false for a different reason
    assert state_3.costs_now == []  # this role never held `git` at all


def test_previewing_a_decision_writes_nothing(store):
    """`tools_for` writes rows and logs events, so the preview could not be built
    on it: a GET would become a mutation, which would be the fourth time this
    slice fixed a hole by opening one. `decision_effect` is read-only, and that is
    asserted rather than intended."""
    task = add_task(store)
    _add(store, task.id, "shell")
    spec = _spec(["file_io", "git", "search", "task_state"])
    rows_before = [asdict(r) for r in store.tool_requests()]
    events_before = store.latest_event_id()

    # Gate on, which is the configuration under which `tools_for` writes most.
    _effect(store, "shell", spec, LoopConfig(gate_declared_tools=True))

    assert [asdict(r) for r in store.tool_requests()] == rows_before
    assert store.latest_event_id() == events_before


def test_effective_tools_is_pure_over_its_arguments(store):
    """The seam's own contract: given the same rows it returns the same answer, and
    it takes no store at all — which is what lets the read paths call it."""
    config = LoopConfig()
    # A *rejected* `shell` (withheld, not pending), so the subtraction is real
    # and the equality below is not comparing two empty answers. Slice 8: with a
    # pending row this pair became a no-op, because an undecided ask no longer
    # costs the role a capability it already holds.
    args = (config, "worker", ["file_io", "git"], [], ["shell"], [])
    first = effective_tools(*args)
    assert first == effective_tools(*args)
    assert first.allowed == ["file_io"]  # git lost its Bash to the rejected shell
    assert first.removed == ["git"]

    # ...and the same arguments with the row merely *pending* take nothing, which
    # is what makes the assertion above about the decision rather than the name.
    pending = effective_tools(
        config, "worker", ["file_io", "git"], [], ["shell"], ["shell"]
    )
    assert pending.allowed == ["file_io", "git"]


# -- E4 cycle 2 / H1 + H2: the rendered claim is computed, not narrated --------
#
# Two HIGH findings, one root cause: the surfaces stated something about the
# *concrete* capability while branching on `in_effect`, which is a **logical-name**
# membership test (`tool in now.allowed`). For a `refused` row the two diverge —
# `withheld_tools` deliberately never sees `refused`, so the row subtracts
# nothing, yet its logical name is absent from `allowed`. The screen then read
# "Bash is not available to this role" while the gate handed `Bash` over through
# the worker's declared `git`, which is the exact direction this module's own
# docstring rules out. `capability_live` is that concrete truth, and `verb` moves
# the headline claim into Python where these tests can pin it: `web/` has no test
# runner, so a fix landing its logic here and its text in TSX would move the
# defect to the one surface no gate covers.


def _cap_refused(store, tool="shell"):
    """A `refused`-over-the-per-task-cap row: the half of the `refused`
    population that carries a real capability. One `pending` row fills a cap of
    one, so the second ask is refused by the machine rather than queued."""
    task = add_task(store)
    assert _add(store, task.id, "web", max_per_task=1) is not None
    assert _add(store, task.id, tool, max_per_task=1) is None  # over the cap
    return task


def test_a_cap_refused_row_reports_the_capability_the_role_still_holds(store):
    """H1. The row is `refused`, so it subtracts nothing; the worker declares
    `git`, so `Bash` is fully live. The concrete claim must say so, and the
    logical-name test cannot: `shell` is genuinely absent from `allowed`."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = _cap_refused(store)

    effect = _effect(store, "shell", spec)
    # What the gate actually hands the runner.
    assert "Bash" in resolve_tools(tools_for(store, LoopConfig(), spec, task.id, "w"))
    assert effect.capability_live == ["Bash"]  # ...and the screen's basis for it
    assert effect.in_effect is False  # the logical name really is absent
    assert effect.verb == "does not withhold"


def test_a_genuine_denial_still_reads_as_a_denial(store):
    """The non-vacuity control for the test above: same tool, same role, but a
    human's `rejected` row — which `withheld_tools` does see. `Bash` is gone,
    `capability_live` is empty, and the verb is a denial."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "shell"), approved=False, by="human")

    effect = _effect(store, "shell", spec)
    assert "Bash" not in resolve_tools(
        tools_for(store, LoopConfig(), spec, task.id, "w")
    )
    assert effect.capability_live == []
    assert effect.verb == "denied"


def test_a_pending_row_that_can_deliver_nothing_does_not_promise_a_grant(store):
    """H2 state 1 — F1's exact state, at the headline. `git` rejected and `shell`
    pending: `approve_grants` is empty, the body says approving does not deliver
    `Bash`, and the verb above it must not still be making that promise."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "git"), approved=False, by="human")
    _add(store, task.id, "shell")

    effect = _effect(store, "shell", spec)
    assert effect.approve_grants == []
    assert effect.verb == "would not deliver"


def test_a_pending_row_that_can_deliver_something_says_it_would(store):
    """The non-vacuity control: no rejected sibling, so approving really does
    grant `Bash` and the verb is allowed to promise it.

    Slice 8 dropped `git` from the spec for the same reason as
    `test_a_previewed_grant_of_bash_is_also_what_the_gate_enforces`: with `git`
    declared the role already holds Bash, so "would grant" was never the honest
    verb — "already has" is."""
    spec = _spec(["file_io", "search", "task_state"])  # no `git`: no Bash held
    task = add_task(store)
    _add(store, task.id, "shell")
    assert _effect(store, "shell", spec).verb == "would grant"


def test_a_pending_row_for_a_capability_the_role_holds_says_already_has(store):
    """The other side of the verb, and the display half of slice 8's fix: with
    `git` declared the role already has `Bash`, so approving `shell` delivers no
    new capability and the headline must not claim it would."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    _add(store, task.id, "shell")
    assert _effect(store, "shell", spec).verb == "already has"


def test_an_approved_row_not_in_force_does_not_claim_to_grant(store):
    """H2 state 2, pinned as an API fact by `tests/test_server.py` and never
    checked at the headline: the row is `approved` and the body reads "NOT in
    force", so the verb cannot read "grants"."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "git"), approved=False, by="human")
    shell = _add(store, task.id, "shell")
    store.tool_request_decide(shell, approved=True, by="human")

    effect = _effect(store, "shell", spec)
    assert effect.in_effect is False
    assert effect.capability_live == []
    assert effect.verb == "does not deliver"


def test_an_approved_row_in_force_does_claim_to_grant(store):
    """The control for the pair above."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "shell"), approved=True, by="human")
    effect = _effect(store, "shell", spec)
    assert effect.capability_live == ["Bash"]
    assert effect.verb == "grants"


def test_a_row_that_confers_no_capability_says_only_that(store):
    """M2's row, at the seam. `classify` consults the read-only allowlist before
    the map, so a name in the allowlist that `LOGICAL_TOOL_MAP` lacks is an
    `auto` row conferring nothing. Neither "grants" nor "not in force" is a true
    headline for it, and the empty resolved list must not be rendered into a
    sentence about a withheld capability."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    _add(store, task.id, "kubectl", status=ToolRequestStatus.AUTO.value)
    effect = _effect(
        store,
        "kubectl",
        spec,
        LoopConfig(tool_readonly_allowlist=["file_read", "search", "kubectl"]),
    )
    assert effect.in_effect is False  # the granted loop drops an unmapped name
    assert effect.capability_live == []
    assert effect.verb == "confers"


def test_the_rejection_fields_are_empty_for_a_row_that_cannot_be_rejected(store):
    """LOW. `tool_request_decide` accepts a rejection only for a `pending` row, so
    `reject_removes`/`reject_loses` on any other status describe a decision that
    can never be taken — dead today and a trap the moment a surface renders them.
    They are computed for `pending` only, which is also the only branch that
    reads them."""
    spec = _spec(["file_io", "git", "search", "task_state"])
    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "shell"), approved=True, by="human")
    effect = _effect(store, "shell", spec)
    assert effect.reject_removes == []
    assert effect.reject_loses == []

    # Non-vacuity: the same shape while the row is still decidable does compute
    # them, so the emptiness above is the status and not the plumbing.
    other = add_task(store)
    _add(store, other.id, "git")
    assert _effect(store, "git", spec).reject_loses == ["Bash"]


# -- E4 verification / two false claims on human-facing text -------------------
#
# Both were found by the phase-exit verifier driving the real surfaces, and both
# are the slice's recurring defect one more time: a sentence asserting more than
# its inputs prove. Neither is read by a decision rule, which is exactly why
# neither had a test — and why the audit log and the escalation reason are the two
# places a false sentence is least recoverable, since a human debugging later has
# nothing else to go on.


def test_the_collateral_loss_message_does_not_claim_a_decision_never_happened(store):
    """`lost` is "removed and not itself withheld", which is NOT "never
    requested" — a collateral name can be an *approved* row. With `git` rejected
    and `shell` approved, both stop working, `shell` lands in `lost`, and the
    event used to tell a human that the request they had personally approved was
    never made."""
    spec = _spec(["file_io", "git", "shell", "search"])
    task = add_task(store)
    store.tool_request_decide(_add(store, task.id, "git"), approved=False, by="human")
    shell = _add(store, task.id, "shell")
    store.tool_request_decide(shell, approved=True, by="human")

    tools_for(store, LoopConfig(), spec, task.id, "worker")
    withheld = [
        e for e in store.events(task.id) if e["kind"] == "tool_capability_withheld"
    ]
    assert len(withheld) == 1
    payload = withheld[0]["payload"]
    # The state that falsifies the old sentence: the collateral name is a row a
    # human decided, and decided the *other* way.
    assert payload["also_lost"] == ["shell"]
    assert store.tool_request_get(shell).status is ToolRequestStatus.APPROVED
    assert "never requested" not in payload["message"]
    # What `lost` does prove is still said, so the fix is not a deletion.
    assert "shares that capability" in payload["message"]
    assert "no way to deny one and keep the other" in payload["message"]


def test_the_park_reason_does_not_offer_rejection_as_a_way_out(store):
    """`reject_tool_request` deliberately does not release a parked task, and
    neither `pause`+`resume` nor `redo` decides the row — each requeues the task
    with the blocking request still standing, so the next round pays a worker
    call and parks again. The reason said "approve or reject", which reads as two
    symmetrical exits and sent a human to the only one that is a dead end."""
    task = add_task(store)
    loop, _ = _loop(
        store, ["worker out\nTOOL_REQUEST: shell (blocking) - need to run the build"]
    )
    loop.run_task(task)

    reason = store.get_task(task.id).escalation_reason
    assert reason.startswith(
        "Awaiting tool approval: shell (request 1, asked by the worker)."
    )
    assert "approve|reject" not in reason
    assert "only decision that releases the task" in reason
    # And it states the true consequence of each route it names, rather than
    # leaving the human to discover it by taking one.
    assert "leaves it parked" in reason
    assert "parks on the same request" in reason


# ---------------------------------------------------------------------------
# Slice 8: the pending exemption has to speak the same language as the
# subtraction.
#
# `held` was a set of logical NAMES; `subtract_withheld` works over the
# CONCRETE footprint from `resolve_tools`. `LOGICAL_TOOL_MAP` is not injective,
# so the two disagreed on exactly the collision pairs — and the pair that
# collides is the one the shipped system prompt teaches agents to ask for.
# ---------------------------------------------------------------------------

WORKER_DECLARES = ["file_io", "git", "search", "task_state"]


def test_a_pending_ask_for_a_sibling_name_costs_the_role_nothing():
    """`shell` and `git` both resolve to `Bash`. An undecided, non-blocking ask
    is nobody's decision — it must not silently revoke a held capability."""
    config = LoopConfig()
    effect = effective_tools(
        config, "worker", WORKER_DECLARES, [], ["shell"], ["shell"]
    )
    assert effect.allowed == WORKER_DECLARES
    assert effect.lost == []


def test_a_pending_ask_for_its_own_name_still_costs_nothing():
    """The case that already worked, kept as a regression."""
    config = LoopConfig()
    effect = effective_tools(config, "worker", WORKER_DECLARES, [], ["git"], ["git"])
    assert effect.allowed == WORKER_DECLARES
    assert effect.lost == []


def test_a_rejected_sibling_name_still_subtracts_unconditionally():
    """The control, and the load-bearing half: widening the *pending* exemption
    must not widen the *rejected* one. A human who believes they closed a gate
    must find it closed, even though `git` was in the role's baseline."""
    config = LoopConfig()
    effect = effective_tools(
        config,
        "worker",
        WORKER_DECLARES,
        [],
        ["shell"],
        [],  # decided, not pending
    )
    assert "git" not in effect.allowed
    assert effect.lost == ["git"]


def test_a_pending_ask_for_an_unheld_capability_is_not_granted_by_the_exemption():
    """The other control: exempting a pending row from subtraction must not
    hand over a capability the role never had. `web` shares nothing with the
    worker's baseline, so it is neither granted nor taken."""
    config = LoopConfig()
    effect = effective_tools(config, "worker", WORKER_DECLARES, [], ["web"], ["web"])
    assert effect.allowed == WORKER_DECLARES
    assert "web" not in effect.allowed


def test_a_pending_ask_whose_footprint_only_overlaps_still_costs_nothing():
    """The boundary the first version of this exemption got wrong, and the one
    its tests could not see.

    `LOGICAL_TOOL_MAP` has *partial* overlaps as well as exact collisions:
    `file_io` -> [Read, Write, Edit] and `file_read` -> [Read], and the shipped
    **planner** declares `file_read`. The first fix tested `subset`, so
    `{Read, Write, Edit} <= {Read, ...}` was False, the row was not exempt, and
    `subtract_withheld` removed every name overlapping `Read` — measured:

        planner declares:          ['file_read', 'search', 'task_state']
        pending optional file_io -> ['search']   lost ['file_read']

    Same self-revocation as the `git`/`shell` case, one collision pair over.
    """
    config = LoopConfig()
    declared = ["file_read", "search", "task_state"]
    effect = effective_tools(config, "planner", declared, [], ["file_io"], ["file_io"])
    assert effect.allowed == declared
    assert effect.lost == []


def test_a_rejected_partially_overlapping_ask_still_subtracts():
    """The control, and the half that must not widen: a human's denial of
    `file_io` still takes `file_read` down with it, because that is the
    fail-closed direction."""
    config = LoopConfig()
    declared = ["file_read", "search", "task_state"]
    effect = effective_tools(config, "planner", declared, [], ["file_io"], [])
    assert "file_read" not in effect.allowed
    assert effect.lost == ["file_read"]


def test_a_pending_ask_sharing_nothing_is_still_withheld():
    """The other control, and the one that makes the exemption non-vacuous: a
    suite that passed with the exemption removed entirely would prove nothing,
    so pin the case where the row genuinely *is* withheld."""
    config = LoopConfig()
    declared = ["search", "task_state"]  # Glob/Grep only: shares nothing with Bash
    effect = effective_tools(config, "worker", declared, [], ["shell"], ["shell"])
    assert "shell" not in effect.allowed
    assert effect.withheld == ["shell"]


def test_a_pending_ask_cannot_revoke_a_capability_a_human_just_granted():
    """`held` was built only from the declared paths, so a granted capability
    was not exempt. With `gate_declared_tools=True` a human approves `git`, and
    the agent's next `TOOL_REQUEST: shell` — the registry's own worked example,
    undecided and never shown to anyone — subtracted the `Bash` that human had
    just granted. A grant is the strongest form of "already holds" there is."""
    config = LoopConfig(gate_declared_tools=True)
    effect = effective_tools(
        config, "worker", ["search"], ["git"], ["shell"], ["shell"]
    )
    assert "git" in effect.allowed
    assert effect.lost == []
