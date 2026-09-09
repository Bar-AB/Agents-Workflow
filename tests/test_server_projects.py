"""Phase 6 of the multi-project dashboard slice: `/api/projects` CRUD and
`?project=` scoping on every listed REST/SSE endpoint.

Real HTTP requests against a live server on an ephemeral port, matching
`test_server.py`'s existing seam exactly.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.models import Task
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.server import serve
from agentloop.store import Store


@pytest.fixture()
def live(tmp_path):
    store = Store(tmp_path / "srv.db")
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        allow_test_exec=False,
        vcs_enabled=False,
        stream_poll_seconds=0.05,
    )
    loop = Loop(store, MockRunner(), Registry.load(), config)
    server = serve(store, loop, Registry.load(), config, host="127.0.0.1", port=0)
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
        base + path,
        method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def seed(store: Store, project_id, title: str = "Task") -> Task:
    task = Task(
        id=None,
        title=title,
        goal="Write slugify(text).",
        acceptance_criteria="Lowercase, hyphenated, tested.",
        project_id=project_id,
    )
    store.add_task(task)
    return task


def two_projects(store, tmp_path):
    a_dir = tmp_path / "repo-a"
    b_dir = tmp_path / "repo-b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = store.create_project("A", str(a_dir))
    b = store.create_project("B", str(b_dir))
    return a, b


def read_frames(base: str, since: int, count: int, project=None, timeout: float = 6.0):
    qs = f"?since={since}" + (f"&project={project}" if project is not None else "")
    frames, buf = [], ""
    with urllib.request.urlopen(f"{base}/api/stream{qs}", timeout=timeout) as resp:
        while len(frames) < count:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                if raw.startswith(":"):
                    continue
                fields = {}
                for line in raw.splitlines():
                    key, _, value = line.partition(": ")
                    fields[key] = value
                if "data" in fields:
                    frames.append(
                        (
                            int(fields.get("id", 0)),
                            fields.get("event", ""),
                            json.loads(fields["data"]),
                        )
                    )
    return frames


# -- /api/projects CRUD ------------------------------------------------------


def test_get_projects_lists_default(live):
    base, store, _, _ = live
    status, body = get(base, "/api/projects")
    assert status == 200
    assert any(p["name"] == "Default" for p in body["projects"])


def test_post_projects_creates_one(live, tmp_path):
    base, store, _, _ = live
    repo = tmp_path / "repo-x"
    repo.mkdir()
    status, body = post(base, "/api/projects", {"name": "X", "repo_root": str(repo)})
    assert status == 201
    assert body["project"]["name"] == "X"

    status, body = get(base, "/api/projects")
    assert any(p["name"] == "X" for p in body["projects"])


def test_post_projects_rejects_missing_repo_root(live):
    base, _, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, "/api/projects", {"name": "NoRepo"})
    assert exc.value.code == 400


def test_project_rename_repoint_archive_use(live, tmp_path):
    base, store, _, _ = live
    repo1 = tmp_path / "repo-y1"
    repo1.mkdir()
    repo2 = tmp_path / "repo-y2"
    repo2.mkdir()
    _, body = post(base, "/api/projects", {"name": "Y", "repo_root": str(repo1)})
    pid = body["project"]["id"]

    _, body = post(base, f"/api/projects/{pid}/rename", {"name": "Y2"})
    assert body["project"]["name"] == "Y2"

    _, body = post(
        base,
        f"/api/projects/{pid}/repoint",
        {"repo_root": str(repo2), "workspace_mode": "worktree"},
    )
    assert body["project"]["repo_root"] == str(repo2)
    assert body["project"]["workspace_mode"] == "worktree"

    _, body = post(base, f"/api/projects/{pid}/use")
    assert body["project"]["is_default"] == 1

    # Switch default back to Default before archiving Y2 (archive refuses
    # the current default).
    default_id = next(
        p["id"]
        for p in get(base, "/api/projects")[1]["projects"]
        if p["name"] == "Default"
    )
    post(base, f"/api/projects/{default_id}/use")
    _, body = post(base, f"/api/projects/{pid}/archive")
    assert body["project"]["archived"] == 1


def test_project_repoint_omitted_mode_preserves_current(live, tmp_path):
    """Same regression as the CLI's own `project repoint` fix."""
    base, store, _, _ = live
    repo1 = tmp_path / "repo-z1"
    repo1.mkdir()
    repo2 = tmp_path / "repo-z2"
    repo2.mkdir()
    _, body = post(
        base,
        "/api/projects",
        {"name": "Z", "repo_root": str(repo1), "workspace_mode": "worktree"},
    )
    pid = body["project"]["id"]

    _, body = post(base, f"/api/projects/{pid}/repoint", {"repo_root": str(repo2)})
    assert body["project"]["workspace_mode"] == "worktree"


def test_unknown_project_action_is_404(live):
    base, _, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, "/api/projects/999999/use")
    assert exc.value.code == 404


# -- the cross-project-leak differential -------------------------------------


def test_tasks_scoped_by_project(live, tmp_path):
    base, store, _, _ = live
    a, b = two_projects(store, tmp_path)
    task_a = seed(store, a, "Task A")
    task_b = seed(store, b, "Task B")

    _, body = get(base, f"/api/tasks?project={a}")
    ids = [t["id"] for t in body["tasks"]]
    assert task_a.id in ids
    assert task_b.id not in ids


def test_metrics_scoped_by_project(live, tmp_path):
    base, store, _, _ = live
    a, b = two_projects(store, tmp_path)
    seed(store, a, "Task A")
    seed(store, b, "Task B")

    status, body = get(base, f"/api/metrics?project={a}")
    assert status == 200
    assert body["tasks_by_status"].get("pending", 0) == 1


def test_memory_scoped_by_project(live, tmp_path):
    base, store, _, _ = live
    a, b = two_projects(store, tmp_path)
    store.memory_write("project", "style", "use tabs", approved=True, project_id=a)
    store.memory_write("project", "style", "use spaces", approved=True, project_id=b)

    _, body = get(base, f"/api/memory?project={a}")
    values = [r["value"] for r in body["memory"]]
    assert "use tabs" in values
    assert "use spaces" not in values


def test_stream_scoped_by_project(live, tmp_path):
    base, store, _, _ = live
    a, b = two_projects(store, tmp_path)
    seed(store, a, "Task A")
    seed(store, b, "Task B")

    # count=3: the 2 project_created events (task-less, pass through
    # regardless of the filter) plus Task A's own task_defined event --
    # Task B's task_defined must never arrive at all under project=a.
    frames = read_frames(base, since=0, count=3, project=a)
    titles = [f[2]["payload"].get("title") for f in frames if f[1] == "event"]
    assert "Task A" in titles
    assert "Task B" not in titles


def test_stream_scoped_by_project_still_passes_through_global_events(live, tmp_path):
    """A task-less event (`project_created`, from registering project A
    itself) must stay visible under a project filter -- the same rule
    `Store.events`'s own project_id filter enforces."""
    base, store, _, _ = live
    a, b = two_projects(store, tmp_path)

    frames = read_frames(base, since=0, count=2, project=a)
    kinds = [f[2].get("kind") for f in frames if f[1] == "event"]
    assert "project_created" in kinds


def test_bad_project_query_is_400_not_500(live):
    base, _, _, _ = live
    for path in (
        "/api/tasks?project=abc",
        "/api/metrics?project=abc",
        "/api/memory?project=abc",
        "/api/stream?project=abc",
    ):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(base, path)
        assert exc.value.code == 400, path


def test_config_includes_default_project_id(live):
    base, store, _, _ = live
    status, body = get(base, "/api/config")
    assert status == 200
    assert body["default_project_id"] == store.resolve_project(None)


def test_task_json_includes_project_id(live, tmp_path):
    base, store, _, _ = live
    a, _ = two_projects(store, tmp_path)
    task = seed(store, a, "Task A")

    _, body = get(base, f"/api/tasks/{task.id}")
    assert body["task"]["project_id"] == a


def test_post_tasks_with_project_id_scopes_it(live, tmp_path):
    base, store, _, _ = live
    a, _ = two_projects(store, tmp_path)
    status, body = post(
        base,
        "/api/tasks",
        {
            "title": "Scoped",
            "goal": "g",
            "acceptance_criteria": "c",
            "project_id": a,
        },
    )
    assert status == 201
    assert body["task"]["project_id"] == a


# -- hunter findings (Phase 6, second pass) -----------------------------------


def test_tool_requests_project_and_task_id_together_does_not_leak(live, tmp_path):
    """[RED-FIRST, MEDIUM regression] `?project=A&task_id=<task in B>` used
    to silently ignore the `project` filter and answer with B's task's
    requests anyway -- the project boundary this endpoint exists to enforce,
    defeated by the caller supplying both filters instead of just one."""
    base, store, _, _ = live
    a, b = two_projects(store, tmp_path)
    task_b = seed(store, b, "Task B")
    store.tool_request_add(
        task_b.id,
        role="worker",
        agent_kind="worker",
        tool="shell",
        status="pending",
        source="marker",
        reason="need to run the build",
        blocking=True,
    )

    status, body = get(base, f"/api/tool_requests?project={a}&task_id={task_b.id}")
    assert status == 200
    assert body["tool_requests"] == []


def test_unknown_project_query_is_404_not_an_empty_result(live):
    """[RED-FIRST, MEDIUM regression] `?project=999999` (well-formed, but no
    such project) used to answer 200 with an empty list on every scoped
    endpoint -- indistinguishable from "this real project just has nothing
    yet". A typo'd id in a bookmarked dashboard URL should read as the
    broken link it is."""
    base, _, _, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/tasks?project=999999")
    assert exc.value.code == 404

    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/metrics?project=999999")
    assert exc.value.code == 404


def test_project_action_rejects_an_unrecognized_verb(live, tmp_path):
    """[RED-FIRST, LOW regression] `_project_action` had no `else` branch --
    an unmatched verb fell straight through every `elif` and reached the
    200-success response at the bottom having done nothing. Unreachable
    through real HTTP today only because `do_POST`'s own routing guard
    already restricts the verb; this proves the function fails closed on
    its own, rather than depending on staying in sync with a guard three
    call-frames away. White-box (calls the handler method directly) because
    the routing guard makes this unreachable over the wire by design."""
    import agentloop.server as server_mod

    base, store, _, _ = live
    repo = tmp_path / "repo-omicron"
    repo.mkdir()
    status, body = post(
        base, "/api/projects", {"name": "Omicron", "repo_root": str(repo)}
    )
    pid = body["project"]["id"]

    class _FakeHandler:
        def __init__(self, store):
            self.store = store
            self.sent = None

        def _send_json(self, body):
            self.sent = body

    fake = _FakeHandler(store)
    with pytest.raises(ValueError, match="unknown project action"):
        server_mod._Handler._project_action(fake, pid, "bogus", {})
    assert fake.sent is None  # never reached the success response
