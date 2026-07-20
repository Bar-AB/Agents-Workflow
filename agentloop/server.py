"""Phase-2 dashboard backend: REST + SSE over the stdlib http.server.

No runtime dependencies, per the project's stdlib-only core rule. That is
affordable here because the live channel is one-way: the browser needs push,
not a bidirectional session, so Server-Sent Events do the job that would
otherwise pull in an async web stack.

The change feed is the audit log itself. `events` is append-only with
monotonic ids, so the stream is just `SELECT ... WHERE id > cursor`, and a
reconnecting browser resumes from `Last-Event-ID` and replays exactly what it
missed. The dashboard therefore reads the same source of truth the loop writes
— no mirrored state, no divergent copies (spec §2).

Binds to localhost by default. Mutations are POST-only.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import LoopConfig
from .loop import Loop
from .models import Task
from .registry import Registry
from .store import Store

# Where the built frontend lands (`npm run build` in web/).
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, store: Store, loop: Loop, registry: Registry,
                 config: LoopConfig):
        self.store = store
        self.loop = loop
        self.registry = registry
        self.config = config
        self._shutdown_flag = threading.Event()
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    def log_message(self, fmt, *args):  # keep CLI output clean
        pass

    @property
    def store(self) -> Store:
        return self.server.store

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routing -------------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path == "/api/tasks":
                self._send_json({"tasks": [self._task_json(t)
                                           for t in self.store.list_tasks()]})
            elif path.startswith("/api/tasks/"):
                self._task_detail(path)
            elif path == "/api/events":
                since = int((query.get("since") or ["0"])[0])
                self._send_json({"events": self.store.events_since(since)})
            elif path == "/api/agents":
                self._send_json({"agents": [
                    {"role": s.role, "model": s.model, "tools": s.tools,
                     "context_budget_tokens": s.context_budget_tokens,
                     "version": s.version}
                    for s in self.server.registry.agents.values()]})
            elif path == "/api/memory":
                self._send_json({"memory": self.store.memory_list()})
            elif path == "/api/metrics":
                self._send_json(self.store.run_metrics())
            elif path == "/api/config":
                cfg = self.server.config
                self._send_json({
                    "approve_threshold": cfg.approve_threshold,
                    "severe_threshold": cfg.severe_threshold,
                    "max_revisions": cfg.max_revisions,
                    "max_tokens_per_task": cfg.max_tokens_per_task,
                    "max_cost_usd_per_task": cfg.max_cost_usd_per_task,
                    "human_review_risk_level": cfg.human_review_risk_level,
                    "test_command": cfg.test_command,
                })
            elif path == "/api/stream":
                self._stream(query)
            else:
                self._serve_static(path)
        except BrokenPipeError:            # client navigated away mid-response
            pass
        except Exception as exc:           # never take the server down
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        body = self._read_json()
        try:
            # /api/tasks
            if parts == ["api", "tasks"]:
                self._create_task(body)
            # /api/tasks/{id}/{approve|reject|redo}
            elif (len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks"
                    and parts[3] in ("approve", "reject", "redo")):
                task = getattr(self.server.loop, f"human_{parts[3]}")(
                    int(parts[2]), body.get("note", ""))
                self._send_json({"task": self._task_json(task)})
            # /api/memory/{id}/{approve|reject}
            elif (len(parts) == 4 and parts[0] == "api" and parts[1] == "memory"
                    and parts[3] in ("approve", "reject")):
                mem_id = int(parts[2])
                if parts[3] == "approve":
                    self.store.memory_set_approved(mem_id, True)
                else:
                    self.store.memory_delete(mem_id)
                self._send_json({"memory": self.store.memory_list()})
            else:
                self._error(404, f"No such endpoint: {url.path}")
        except KeyError as exc:
            self._error(404, str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- handlers ------------------------------------------------------------

    def _create_task(self, body: dict) -> None:
        title = (body.get("title") or "").strip()
        goal = (body.get("goal") or "").strip()
        criteria = (body.get("acceptance_criteria") or "").strip()
        if not (title and goal and criteria):
            self._error(400, "title, goal and acceptance_criteria are required")
            return
        risk = int(body.get("risk_level", 1))
        if risk not in (0, 1, 2):
            self._error(400, "risk_level must be 0, 1 or 2")
            return
        task = Task(id=None, title=title, goal=goal,
                    acceptance_criteria=criteria, risk_level=risk)
        self.store.add_task(task)
        self._send_json({"task": self._task_json(task)}, status=201)

    def _task_detail(self, path: str) -> None:
        try:
            task_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self._error(400, "Bad task id")
            return
        task = self.store.get_task(task_id)
        if task is None:
            self._error(404, f"No task {task_id}")
            return
        self._send_json({
            "task": self._task_json(task),
            "metrics": self.store.task_metrics(task_id),
            "test_runs": self.store.test_runs(task_id),
            "events": self.store.events(task_id),
        })

    def _stream(self, query: dict) -> None:
        """SSE: replay everything after the cursor, then tail the audit log."""
        cursor = int(self.headers.get("Last-Event-ID")
                     or (query.get("since") or ["0"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        interval = self.server.config.stream_poll_seconds
        last_beat = time.time()
        while not self.server._shutdown_flag.is_set():
            rows = self.store.events_since(cursor)
            for row in rows:
                cursor = row["id"]
                self._emit(row["id"], "event", row)
            if rows:
                # State changed; push the rollup so tiles update in step.
                self._emit(cursor, "metrics", self.store.run_metrics())
            elif time.time() - last_beat > 15:
                # Comment frame keeps proxies and idle sockets from timing out.
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                last_beat = time.time()
            time.sleep(interval)

    def _emit(self, event_id: int, name: str, data) -> None:
        payload = json.dumps(data, default=str)
        frame = f"id: {event_id}\nevent: {name}\ndata: {payload}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _serve_static(self, path: str) -> None:
        """Serve the built frontend, falling back to index.html for client
        routes. Paths are resolved and confined to the dist directory."""
        if not _WEB_DIST.is_dir():
            self._send_json({
                "error": "Frontend not built.",
                "hint": "cd web && npm install && npm run build",
            }, status=503)
            return

        rel = path.lstrip("/") or "index.html"
        target = (_WEB_DIST / rel).resolve()
        if not str(target).startswith(str(_WEB_DIST.resolve())):
            self._error(403, "Forbidden")          # path traversal attempt
            return
        if not target.is_file():
            target = _WEB_DIST / "index.html"
            if not target.is_file():
                self._error(404, "Not found")
                return

        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _task_json(task: Task) -> dict:
        return {
            "id": task.id, "title": task.title, "goal": task.goal,
            "acceptance_criteria": task.acceptance_criteria,
            "status": task.status.value, "risk_level": task.risk_level,
            "revision_count": task.revision_count,
            "worker_role": task.worker_role,
            "validator_role": task.validator_role,
            "output": task.output,
            "escalation_reason": task.escalation_reason,
        }


def serve(store: Store, loop: Loop, registry: Registry, config: LoopConfig,
          host: str | None = None, port: int | None = None) -> DashboardServer:
    """Start the dashboard server. Returns it so callers (and tests) can
    shut it down."""
    addr = (host or config.server_host, port if port is not None
            else config.server_port)
    return DashboardServer(addr, store, loop, registry, config)


def serve_forever(store: Store, loop: Loop, registry: Registry,
                  config: LoopConfig, host: str | None = None,
                  port: int | None = None) -> None:
    server = serve(store, loop, registry, config, host, port)
    h, p = server.server_address[0], server.server_address[1]
    print(f"agentloop dashboard on http://{h}:{p}")
    if not _WEB_DIST.is_dir():
        print("  (frontend not built — run: cd web && npm install "
              "&& npm run build)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server._shutdown_flag.set()
        server.shutdown()
        server.server_close()
