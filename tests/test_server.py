"""Phase-2 dashboard backend: real HTTP requests against a live server on an
ephemeral port. Exercises the cross-thread store access the server depends on."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.models import Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.server import serve
from agentloop.store import Store

APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."


@pytest.fixture()
def live(tmp_path):
    store = Store(tmp_path / "srv.db")
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        allow_test_exec=False,
        stream_poll_seconds=0.05,
    )
    loop = Loop(store, MockRunner(), Registry.load(), config)
    server = serve(store, loop, Registry.load(), config, host="127.0.0.1",
                   port=0)   # port 0 -> OS picks a free one
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, store, loop, config
    finally:
        server._shutdown_flag.set()
        server.shutdown()
        server.server_close()
        store.close()


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, json.loads(r.read())


def post(base: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def seed(store: Store, title: str = "Add slugify util", risk: int = 1) -> Task:
    task = Task(id=None, title=title, goal="Write slugify(text).",
                acceptance_criteria="Lowercase, hyphenated, tested.",
                risk_level=risk)
    store.add_task(task)
    return task


# -- reads -------------------------------------------------------------------

def test_tasks_endpoint_serves_store_contents(live):
    base, store, _, _ = live
    seed(store)
    status, body = get(base, "/api/tasks")
    assert status == 200
    assert body["tasks"][0]["title"] == "Add slugify util"


def test_task_detail_includes_metrics_and_test_runs(live):
    base, store, _, _ = live
    task = seed(store)
    status, body = get(base, f"/api/tasks/{task.id}")
    assert status == 200
    assert body["task"]["id"] == task.id
    assert "metrics" in body and "test_runs" in body and "events" in body


def test_missing_task_is_404(live):
    base, _, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/tasks/9999")
    assert exc.value.code == 404


def test_agents_endpoint_exposes_the_registry(live):
    base, _, _, _ = live
    _, body = get(base, "/api/agents")
    roles = {a["role"] for a in body["agents"]}
    assert {"worker", "validator"} <= roles
    worker = next(a for a in body["agents"] if a["role"] == "worker")
    assert worker["tools"] and worker["context_budget_tokens"] > 0


def test_metrics_rollup_reflects_a_completed_run(live):
    base, store, loop, _ = live
    task = seed(store)
    loop.runner = MockRunner(["some output", APPROVE])
    loop.run_task(task)

    _, body = get(base, "/api/metrics")
    assert body["attempts"] == 2                  # worker + validator
    assert body["tokens"] > 0
    assert body["tasks_by_status"]["done"] == 1
    assert any(m["model"] == "mock" for m in body["by_model"])


def test_config_endpoint_exposes_thresholds(live):
    base, _, _, _ = live
    _, body = get(base, "/api/config")
    assert body["approve_threshold"] == 0.70
    assert body["severe_threshold"] == 0.40


# -- writes ------------------------------------------------------------------

def test_create_task_via_api(live):
    base, store, _, _ = live
    status, body = post(base, "/api/tasks", {
        "title": "From dashboard", "goal": "do a thing",
        "acceptance_criteria": "it works", "risk_level": 2})
    assert status == 201
    assert body["task"]["risk_level"] == 2
    assert store.get_task(body["task"]["id"]).title == "From dashboard"


def test_create_task_validates_required_fields(live):
    base, _, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, "/api/tasks", {"title": "no goal"})
    assert exc.value.code == 400


def test_human_decisions_work_over_http(live):
    base, store, loop, _ = live
    task = seed(store, risk=2)
    loop.runner = MockRunner(["out", APPROVE])
    loop.run_task(task)
    assert store.get_task(task.id).status == TaskStatus.NEEDS_HUMAN

    status, body = post(base, f"/api/tasks/{task.id}/approve", {"note": "LGTM"})
    assert status == 200 and body["task"]["status"] == "done"
    assert store.get_task(task.id).status == TaskStatus.DONE


def test_redo_over_http_resets_the_task(live):
    base, store, loop, _ = live
    task = seed(store)
    loop.runner = MockRunner(["out", APPROVE])
    loop.run_task(task)

    _, body = post(base, f"/api/tasks/{task.id}/redo")
    assert body["task"]["status"] == "pending"
    assert body["task"]["output"] == ""


def test_memory_gating_over_http(live):
    base, store, _, _ = live
    store.memory_write("project", "candidate", "an unvetted fact")
    mem_id = store.memory_list()[0]["id"]

    _, body = post(base, f"/api/memory/{mem_id}/approve")
    assert body["memory"][0]["approved"] == 1

    _, body = post(base, f"/api/memory/{mem_id}/reject")
    assert body["memory"] == []


def test_unknown_post_endpoint_is_404(live):
    base, _, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, "/api/nonsense")
    assert exc.value.code == 404


# -- SSE ---------------------------------------------------------------------

def read_frames(base: str, since: int, count: int, timeout: float = 6.0):
    """Read `count` SSE frames, returning parsed (id, event, data) triples."""
    frames, buf = [], ""
    with urllib.request.urlopen(
            f"{base}/api/stream?since={since}", timeout=timeout) as resp:
        assert resp.headers["Content-Type"] == "text/event-stream"
        while len(frames) < count:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                if raw.startswith(":"):        # keep-alive comment
                    continue
                fields = {}
                for line in raw.splitlines():
                    key, _, value = line.partition(": ")
                    fields[key] = value
                if "data" in fields:
                    frames.append((int(fields.get("id", 0)),
                                   fields.get("event", ""),
                                   json.loads(fields["data"])))
    return frames


def test_stream_replays_history_from_the_cursor(live):
    base, store, _, _ = live
    seed(store, "first")
    seed(store, "second")

    frames = read_frames(base, since=0, count=2)
    kinds = [f[1] for f in frames]
    assert "event" in kinds
    payload_titles = [f[2]["payload"].get("title") for f in frames
                      if f[1] == "event"]
    assert "first" in payload_titles


def test_stream_resumes_after_a_cursor_without_replaying(live):
    """The audit log's monotonic ids are what make a reconnect lossless."""
    base, store, _, _ = live
    seed(store, "before")
    cursor = store.latest_event_id()
    seed(store, "after")

    frames = read_frames(base, since=cursor, count=1)
    titles = [f[2]["payload"].get("title") for f in frames if f[1] == "event"]
    assert "after" in titles
    assert "before" not in titles


def test_stream_frames_carry_monotonic_ids(live):
    base, store, _, _ = live
    seed(store, "a")
    seed(store, "b")
    frames = [f for f in read_frames(base, since=0, count=2) if f[1] == "event"]
    ids = [f[0] for f in frames]
    assert ids == sorted(ids)


def test_live_run_streams_to_a_connected_client(live):
    """End-to-end: a loop running on another thread shows up on the stream —
    this is the whole premise of the live dashboard, and it also proves the
    store is genuinely usable from two threads at once."""
    base, store, loop, _ = live
    task = seed(store)
    cursor = store.latest_event_id()
    loop.runner = MockRunner(["worker output", APPROVE])

    result: list = []
    reader = threading.Thread(
        target=lambda: result.extend(read_frames(base, cursor, 4)),
        daemon=True)
    reader.start()
    loop.run_task(task)
    reader.join(timeout=8)

    kinds = [f[2].get("kind") for f in result if f[1] == "event"]
    assert "worker_prompt" in kinds
    assert store.get_task(task.id).status == TaskStatus.DONE


# -- static ------------------------------------------------------------------

def test_unbuilt_frontend_gives_a_helpful_hint(live, monkeypatch):
    base, _, _, _ = live
    import agentloop.server as srv
    monkeypatch.setattr(srv, "_WEB_DIST", srv._WEB_DIST / "__missing__")
    try:
        get(base, "/")
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
        assert "npm" in json.loads(exc.read())["hint"]
