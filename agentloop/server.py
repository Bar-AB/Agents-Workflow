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

import ipaddress
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import LoopConfig
from .loop import Loop
from .models import Task, ToolRequest, ToolRequestStatus
from .registry import Registry
from .runner import LOGICAL_TOOL_MAP, resolve_tools, tools_sharing_capability
from .store import Store
from .toolpolicy import declared_tools, decision_effect

_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _own_hostname() -> str:
    """This machine's own name, lowercased, or `""` if it cannot be determined.

    Resolved once at import: `gethostname` reads a local setting, it does not
    perform a DNS lookup, so this costs nothing per request and cannot hang."""
    try:
        import socket

        return socket.gethostname().strip().lower()
    except Exception:
        return ""


_OWN_HOST = _own_hostname()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr,
        store: Store,
        loop: Loop,
        registry: Registry,
        config: LoopConfig,
        config_path: str | None = None,
    ):
        self.store = store
        self.loop = loop
        self.registry = registry
        self.config = config
        self.config_path = config_path or "loopconfig.json"
        self.bound_host = str(addr[0] or "").lower()
        self._shutdown_flag = threading.Event()
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep CLI output clean
        pass

    def handle(self) -> None:
        """Swallow client disconnects for the whole persistent connection.

        A reset can surface not only mid-response but in the base class's
        keep-alive loop, where it reads the *next* request line off a socket the
        client already closed (WinError 10054). That path is outside do_GET/
        do_POST, so it must be caught here or it prints a spurious traceback."""
        try:
            super().handle()
        except ConnectionError:
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

    def _host_ok(self) -> bool:
        """Whether `Host` names this machine rather than a re-resolvable name.

        An IP literal is accepted whatever it is: rebinding needs a *name* whose
        DNS answer can be changed after the page loads, so a bare address cannot
        be the vehicle — and refusing them would break `serve --host 0.0.0.0`
        reached over the LAN, which is a supported setup."""
        raw = (self.headers.get("Host") or "").strip()
        if raw.startswith("["):
            name = raw[1:].split("]", 1)[0]
        else:
            name = raw.rsplit(":", 1)[0] if ":" in raw else raw
        name = name.lower()
        if name in _LOOPBACK_NAMES or name == self.bound_host or name == _OWN_HOST:
            return True
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return False
        return True

    def _origin_ok(self) -> bool:
        """Whether `Origin`, *if the client sent one*, is this same server.

        An **absent** `Origin` is accepted, and that is not a hole: only a
        browser sets it, and a browser cannot omit it on a cross-origin request.
        curl, the CLI and every scripted client send nothing, so requiring it
        would break every non-browser caller in order to stop nothing.

        `Origin: null` is **not** an absent one, and treating it as one was a
        hole that reopened the exact attack this guard exists to close.
        Measured on the first version of this check: a raw cross-origin `POST
        /api/charter` carrying `Origin: null` returned 200 and replaced the
        charter body. A browser sends the literal string `null` for an *opaque*
        origin — from a sandboxed iframe (`<iframe sandbox="allow-scripts">`),
        and after any redirect chain that crossed origins, which preserves
        method and body on a 307. So `null` is a real cross-origin request that
        declines to name itself, and an opaque origin can never be this server;
        the one thing it must not be is treated as "the client sent nothing"."""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and urlparse(origin).netloc.lower() == host

    def _refuse(self, why: str) -> None:
        """403, **and a row in the audit log**.

        Both halves are load-bearing and for different readers. A refusal here
        is either an attack or a misconfiguration, and this handler could
        report neither: `_safe_error` writes to the socket and returns, and
        `log_message` is overridden to a no-op to keep the CLI clean, so the
        refusal reached no event, no REST response, no SSE frame, not even
        stderr. In a project whose stated invariant is that the append-only
        audit log is load-bearing, the security control was the one thing on
        zero channels — an operator had no way to learn that a page had tried
        to rewrite their charter, and no way to see why their own dashboard was
        answering 403.

        `task_id=None`, like every other project-wide event: this is a fact
        about the server, not about a task. Total by construction — a failure
        to record must never take the refusal with it, since refusing is the
        part that matters."""
        try:
            self.server.store.log_event(
                None,
                "dashboard_refused",
                {
                    "reason": why,
                    "path": str(self.path)[:200],
                    "host": str(self.headers.get("Host") or "")[:200],
                    "origin": str(self.headers.get("Origin") or "")[:200],
                    "method": self.command,
                },
            )
        except Exception:
            pass
        self._safe_error(403, f"Forbidden: {why}")

    def _same_origin(self) -> bool:
        """Both conditions, fail-closed, checked before any routing. Applied to
        GET as well as POST because rebinding's payoff on this server is the
        *read*: `/api/tasks` carries goals and worker output, and `/api/config`
        carries `test_command`."""
        if not self._host_ok():
            self._refuse("Host is not this server")
            return False
        if not self._origin_ok():
            self._refuse("cross-origin request")
            return False
        return True

    @property
    def bound_host(self) -> str:
        return getattr(self.server, "bound_host", "")

    def _safe_error(self, status: int, message: str) -> None:
        """Send an error response, but never raise while doing so — the socket
        may already be dead (a generic handler must not blow up trying to
        report a failure on a closed connection)."""
        try:
            self._error(status, message)
        except (ConnectionError, OSError):
            pass

    def _read_json(self) -> dict:
        """Parse the request body as a JSON object. Malformed JSON or a
        non-object body raises ValueError -> the caller returns 400, rather
        than silently defaulting to {} and masking a client bug."""
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def do_GET(self) -> None:
        if not self._same_origin():
            return
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path == "/api/tasks":
                project_id = self._parse_project_query(query)
                if project_id is False:
                    return
                self._send_json(
                    {
                        "tasks": [
                            self._task_json(t)
                            for t in self.store.list_tasks(project_id=project_id)
                        ]
                    }
                )
            elif path == "/api/projects":
                self._send_json(
                    {"projects": self.store.list_projects(include_archived=True)}
                )
            elif path.startswith("/api/tasks/"):
                self._task_detail(path)
            elif path == "/api/events":
                raw = (query.get("since") or ["0"])[0]
                try:
                    since = int(raw)
                except (TypeError, ValueError):
                    self._error(400, f"Bad since: {raw!r}")
                    return
                self._send_json({"events": self.store.events_since(since)})
            elif path == "/api/agents":
                self._send_json(
                    {
                        "agents": [
                            {
                                "role": s.role,
                                "model": s.model,
                                "tools": s.tools,
                                "context_budget_tokens": s.context_budget_tokens,
                                "version": s.version,
                            }
                            for s in self.server.registry.agents.values()
                        ]
                    }
                )
            elif path == "/api/memory":
                project_id = self._parse_project_query(query)
                if project_id is False:
                    return
                self._send_json(
                    {"memory": self.store.memory_list(project_id=project_id)}
                )
            elif path == "/api/tool_requests":
                self._tool_requests_list(query)
            elif path == "/api/charter":
                project_id = self._parse_project_query(query)
                if project_id is False:
                    return
                self._send_json(self._charter_json(project_id))
            elif path == "/api/metrics":
                project_id = self._parse_project_query(query)
                if project_id is False:
                    return
                self._send_json(self.store.run_metrics(project_id=project_id))
            elif path == "/api/config":
                cfg = self.server.config
                self._send_json(
                    {
                        "approve_threshold": cfg.approve_threshold,
                        "severe_threshold": cfg.severe_threshold,
                        "max_revisions": cfg.max_revisions,
                        "max_tokens_per_task": cfg.max_tokens_per_task,
                        "max_cost_usd_per_task": cfg.max_cost_usd_per_task,
                        "human_review_risk_level": cfg.human_review_risk_level,
                        "test_command": cfg.test_command,
                        "tool_readonly_allowlist": list(cfg.tool_readonly_allowlist),
                        "gate_declared_tools": cfg.gate_declared_tools,
                        "repo_root": cfg.repo_root,
                        "workspace_mode": cfg.workspace_mode,
                        "default_project_id": self.store.default_project_id(),
                    }
                )
            elif path == "/api/stream":
                self._stream(query)
            elif path.startswith("/api/"):
                self._error(404, f"No such endpoint: {path}")
            else:
                self._serve_static(path)
        except ConnectionError:  # client navigated away mid-response
            pass
        except Exception as exc:  # never take the server down
            self._safe_error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        if not self._same_origin():
            return
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            body = self._read_json()  # inside try: malformed body -> 400 below
            if parts == ["api", "tasks"]:
                self._create_task(body)
            elif (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "tasks"
                and parts[3] in ("approve", "reject", "redo")
            ):
                task = getattr(self.server.loop, f"human_{parts[3]}")(
                    int(parts[2]), body.get("note", "")
                )
                self._send_json({"task": self._task_json(task)})
            elif (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "tasks"
                and parts[3] in ("pause", "resume", "abort")
            ):
                loop = self.server.loop
                if parts[3] == "abort":
                    task = loop.abort(int(parts[2]), body.get("note", ""))
                else:
                    task = getattr(loop, parts[3])(int(parts[2]))
                self._send_json({"task": self._task_json(task)})
            elif parts == ["api", "projects"]:
                self._create_project(body)
            elif (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "projects"
                and parts[3] in ("rename", "repoint", "archive", "use")
            ):
                self._project_action(int(parts[2]), parts[3], body)
            elif parts == ["api", "charter"]:
                self._set_charter(body)
            elif parts == ["api", "config", "repo"]:
                self._set_config_repo(body)
            elif (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "memory"
                and parts[3] in ("approve", "reject", "pin", "unpin")
            ):
                mem_id = int(parts[2])
                if parts[3] == "approve":
                    self.store.memory_set_approved(mem_id, True)
                elif parts[3] == "reject":
                    self.store.memory_delete(mem_id)
                else:
                    self.store.memory_set_pinned(mem_id, parts[3] == "pin")
                self._send_json({"memory": self.store.memory_list()})
            elif (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "tool_requests"
                and parts[3] in ("approve", "reject")
            ):
                loop = self.server.loop
                verb = "approve" if parts[3] == "approve" else "reject"
                getattr(loop, f"{verb}_tool_request")(
                    int(parts[2]), body.get("note", "")
                )
                self._send_json(self._tool_requests_json())
            else:
                self._error(404, f"No such endpoint: {url.path}")
        except ConnectionError:  # client gone; nothing to send back
            pass
        except KeyError as exc:
            self._safe_error(404, str(exc))
        except (ValueError, TypeError) as exc:
            self._safe_error(400, str(exc))
        except Exception as exc:
            self._safe_error(500, f"{type(exc).__name__}: {exc}")

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
        task = Task(
            id=None,
            title=title,
            goal=goal,
            acceptance_criteria=criteria,
            risk_level=risk,
            project_id=body.get("project_id"),
        )
        self.store.add_task(task)
        self._send_json({"task": self._task_json(task)}, status=201)

    def _tool_request_json(self, req: ToolRequest) -> dict:
        """One request, with every column served plus the two derived facts a
        human needs *before* deciding.

        Every column, because a column stored but exposed nowhere looks
        implemented and is not — the trap `task_metrics`' `findings` documents.
        Enums as `.value`; both timestamps as numbers, matching the `REAL`
        columns.

        `resolved` and `also_decides` are **derived on every read, never stored**:

        - `resolved` is what the label actually confers. `git` grants `Bash`, so
          the logical name understates the grant.
        - `also_decides` is the collateral. `LOGICAL_TOOL_MAP` is not injective
          and the gate enforces the *concrete* list, so approving `shell` hands
          over `git`'s capability and — the case the release rule turns on —
          rejecting `shell` stops `git` working. Without this at the decision
          point that consequence is legible only from the audit trail.

        Derived rather than mirrored in the frontend, because `LOGICAL_TOOL_MAP`
        is the single source of truth for both and a copy in `types.ts` would
        drift the first time a logical name gains a concrete tool. Derived rather
        than stored for the same reason, one layer down: the ledger's columns are
        provider-neutral by rule, and concrete vendor names belong in event
        payloads and this view only.

        `also_decides` states that a decision on `shell` also lands on `git`. What
        it cannot state is whether that *matters here*, because the effect depends
        on the row's **role** and on the **statuses of its sibling rows** — both of
        which this layer has and the map does not. So `effect` carries the computed
        consequence from `toolpolicy.decision_effect`, which evaluates the same
        `effective_tools` the gate itself enforces: approving may grant nothing
        (another denial still subtracts the capability), and rejecting may cost
        nothing (a pending row already withholds it, or the role never declared the
        sibling). A permission screen that invents a cost for *denial* pushes a
        human toward granting, so the field is a difference between two evaluations
        rather than a sentence about the map.

        `known` splits the two causes of an empty `resolved`: `task_state` is
        genuinely served in-process, while a name outside `LOGICAL_TOOL_MAP` — the
        whole `refused` population — is not a tool at all.
        """
        role = req.role
        declared = declared_tools(self.server.registry, role)
        effect = decision_effect(self.store, self.server.config, role, declared, req)
        return {
            "id": req.id,
            "task_id": req.task_id,
            "attempt_id": req.attempt_id,
            "role": req.role,
            "agent_kind": req.agent_kind,
            "tool": req.tool,
            "reason": req.reason,
            "blocking": req.blocking,
            "parked": req.parked,
            "source": req.source.value,
            "status": req.status.value,
            "decided_by": req.decided_by,
            "decided_note": req.decided_note,
            "created_at": req.created_at,
            "decided_at": req.decided_at,
            "resolved": resolve_tools([req.tool]),
            "also_decides": tools_sharing_capability(req.tool),
            "known": req.tool in LOGICAL_TOOL_MAP,
            "effect": {
                "in_effect": effect.in_effect,
                "capability_live": effect.capability_live,
                "capability_missing": effect.capability_missing,
                "verb": effect.verb,
                "costs_now": effect.costs_now,
                "approve_grants": effect.approve_grants,
                "approve_enables": effect.approve_enables,
                "reject_removes": effect.reject_removes,
                "reject_loses": effect.reject_loses,
            },
        }

    def _tool_requests_json(self, task_id: int | None = None, status=None) -> dict:
        """The list shape both the GET and the two POSTs return."""
        return {
            "tool_requests": [
                self._tool_request_json(r)
                for r in self.store.tool_requests(task_id=task_id, status=status)
            ]
        }

    def _parse_project_query(self, query: dict) -> int | None | bool:
        """`?project=` on a GET endpoint: absent -> `None` (unfiltered), a
        valid, EXISTING int -> that int, unparseable -> a 400, a
        well-formed id naming no project -> a 404. Both error cases send
        their own response and return `False` as a sentinel meaning "the
        caller must return immediately, the response is already sent"
        (matching the `int()`/`_error(400, ...)` pattern `_tool_requests_list`
        already uses for `task_id`).

        The 404 half matters because every caller of this (`/api/tasks`,
        `/api/memory`, `/api/metrics`, `/api/tool_requests`, `/api/stream`)
        feeds the id straight into a `project_id=` filter that answers
        "nothing matched" for both "this project has nothing yet" and "this
        project does not exist" -- a typo'd `?project=` in a bookmarked
        dashboard URL rendered as a real, empty project rather than the
        broken link it actually was."""
        raw = (query.get("project") or [None])[0]
        if raw is None:
            return None
        try:
            project_id = int(raw)
        except (TypeError, ValueError):
            self._error(400, f"Bad project: {raw!r}")
            return False
        if self.store.get_project(project_id) is None:
            self._error(404, f"No project {project_id}")
            return False
        return project_id

    def _tool_requests_list(self, query: dict) -> None:
        """GET /api/tool_requests[?task_id=&status=].

        Both filters arrive as query strings and both are refused with a 400
        rather than dropped, because dropping either serves a queue that was not
        filtered as though it had been — and on a *permission* API the failure
        direction is "nothing is waiting on you" while rows are pending.
        `do_GET` has no `ValueError` branch to fall back on (it maps every escape
        to a 500), which is why each is checked here.

        The `task_id` check is `int()` itself rather than a hand-rolled predicate.
        The predicate that stood here (`str(raw).lstrip('-').isdigit()`) admitted
        two inputs that then raised inside `int()`: `--5`, because `lstrip`
        strips *every* leading hyphen, and `²`, because `'²'.isdigit()` is True.
        Letting the converter decide is total — there is no input it accepts and
        `int()` rejects — and it is strictly less code than the guard it replaces.

        `status` is validated against `ToolRequestStatus`, not passed through:
        `store.tool_requests` binds it verbatim, so `?status=Pending` (wrong case)
        or `?status=granted` (a status this domain does not have) came back 200
        with an empty list, indistinguishable from an empty queue.
        """
        raw = (query.get("task_id") or [None])[0]
        try:
            task_id = None if raw is None else int(raw)
        except (TypeError, ValueError):
            self._error(400, f"Bad task_id: {raw!r}")
            return
        status = (query.get("status") or [None])[0]
        if status is not None and status not in {s.value for s in ToolRequestStatus}:
            self._error(400, f"Bad status: {status!r}")
            return
        project_id = self._parse_project_query(query)
        if project_id is False:
            return
        if project_id is not None and task_id is None:
            project_task_ids = {
                t.id for t in self.store.list_tasks(project_id=project_id)
            }
            body = self._tool_requests_json(task_id=None, status=status)
            body["tool_requests"] = [
                r for r in body["tool_requests"] if r["task_id"] in project_task_ids
            ]
            self._send_json(body)
            return
        if project_id is not None and task_id is not None:
            task = self.store.get_task(task_id)
            if task is None or task.project_id != project_id:
                self._send_json({"tool_requests": []})
                return
        self._send_json(self._tool_requests_json(task_id=task_id, status=status))

    def _charter_json(self, project_id: int | None = None) -> dict:
        with self.store.transaction():
            active = self.store.charter_active(project_id)
            history = self.store.charter_history(project_id)
        by_id = {r["id"]: r for r in history}
        return {
            "active": dict(by_id[active[0]]) if active else None,
            "history": history,
        }

    def _set_charter(self, body: dict) -> None:
        """POST /api/charter — publish a new charter version.

        An oversize or whitespace-only body raises `ValueError` in the store and
        `do_POST` maps it to 400. That refusal is the point: the charter is never
        trimmed or emptied by accident, so it has to fail loudly here. Clearing
        is deliberately not a side effect of submitting an empty box — it is its
        own audited operation, on the CLI (`agentloop charter clear --note ...`).

        `project_id` in the body, not a query param — POST bodies are where
        every other project-aware write already reads it from (`POST
        /api/tasks`); an unknown id raises `KeyError`, mapped to 404 by
        `do_POST`'s existing handler like any other bad reference.
        """
        text = body.get("body")
        if not isinstance(text, str):
            raise ValueError("body must be a string")
        project_id = body.get("project_id")
        self.store.charter_set(text, str(body.get("note") or ""), project_id)
        self._send_json(self._charter_json(project_id))

    def _set_config_repo(self, body: dict) -> None:
        """POST /api/config/repo — persist `repo_root`/`workspace_mode` to the
        loopconfig.json file `agentloop serve` was started with.

        Every check below runs, in order, before anything is written, and a
        failure at any of them writes nothing — the same "validate whole,
        write once" shape `agents.parse_plan` uses for the same reason: a
        half-written config is worse than no write at all.

        Deliberately does **not** touch `self.server.config` — that is the
        live `LoopConfig` the running loop and registry actually use, and
        mutating it here would apply an unreviewed edit to a task mid-run.
        Only the file changes; picking it up needs a restart, exactly like
        any other loopconfig.json edit.
        """
        repo_root = body.get("repo_root")
        if not isinstance(repo_root, str) or not repo_root.strip():
            raise ValueError("repo_root is required")
        workspace_mode = body.get("workspace_mode")
        if workspace_mode not in ("scratch", "worktree"):
            raise ValueError(
                f"workspace_mode must be 'scratch' or 'worktree', not "
                f"{workspace_mode!r}"
            )
        if not os.path.isabs(repo_root):
            raise ValueError(f"repo_root must be an absolute path: {repo_root!r}")
        if not os.path.isdir(repo_root):
            raise ValueError(
                f"repo_root does not exist or is not a directory: {repo_root!r}"
            )

        path = self.server.config_path
        if os.path.exists(path):
            raw = Path(path).read_text(encoding="utf-8-sig")
            data = json.loads(raw) if raw.strip() else {}
        else:
            data = {}
        data["repo_root"] = repo_root
        data["workspace_mode"] = workspace_mode
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._send_json({"repo_root": repo_root, "workspace_mode": workspace_mode})

    def _create_project(self, body: dict) -> None:
        """POST /api/projects — register a new project.

        `Store.create_project` itself does the "validate whole, write once"
        shape (name uniqueness, workspace_mode, an absolute existing
        repo_root) and raises `ValueError` on any failure, mapped to a clean
        400 by `do_POST`'s existing handler."""
        name = body.get("name")
        repo_root = body.get("repo_root")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required")
        if not isinstance(repo_root, str) or not repo_root.strip():
            raise ValueError("repo_root is required")
        workspace_mode = body.get("workspace_mode", "scratch")
        pid = self.store.create_project(name, repo_root, workspace_mode=workspace_mode)
        self._send_json({"project": self.store.get_project(pid)}, status=201)

    def _project_action(self, project_id: int, verb: str, body: dict) -> None:
        """POST /api/projects/{id}/{rename|repoint|archive|use}. A bad id
        raises `KeyError` -> 404; a bad name/repo_root/workspace_mode or a
        refused archive (default project, non-terminal tasks) raises
        `ValueError` -> 400, both through `do_POST`'s existing handlers."""
        if verb == "rename":
            new_name = body.get("name")
            if not isinstance(new_name, str) or not new_name.strip():
                raise ValueError("name is required")
            self.store.rename_project(project_id, new_name)
        elif verb == "repoint":
            repo_root = body.get("repo_root")
            if not isinstance(repo_root, str) or not repo_root.strip():
                raise ValueError("repo_root is required")
            workspace_mode = body.get("workspace_mode")
            if workspace_mode is None:
                current = self.store.get_project(project_id)
                if current is None:
                    raise KeyError(f"unknown project {project_id!r}")
                workspace_mode = current["workspace_mode"]
            self.store.repoint_project(project_id, repo_root, workspace_mode)
        elif verb == "archive":
            self.store.archive_project(project_id)
        elif verb == "use":
            self.store.set_default_project(project_id)
        else:
            raise ValueError(f"unknown project action {verb!r}")
        self._send_json({"project": self.store.get_project(project_id)})

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
        self._send_json(
            {
                "task": self._task_json(task),
                "metrics": self.store.task_metrics(task_id),
                "test_runs": self.store.test_runs(task_id),
                "events": self.store.events(task_id),
            }
        )

    def _stream(self, query: dict) -> None:
        """SSE: replay everything after the cursor, then tail the audit log."""
        raw = self.headers.get("Last-Event-ID") or (query.get("since") or ["0"])[0]
        try:
            cursor = int(raw)
        except (TypeError, ValueError):
            self._error(400, f"Bad cursor: {raw!r}")
            return
        project_id = self._parse_project_query(query)
        if project_id is False:
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        interval = self.server.config.stream_poll_seconds
        last_beat = time.time()
        try:
            while not self.server._shutdown_flag.is_set():
                rows = self.store.events_since(cursor, project_id=project_id)
                for row in rows:
                    cursor = row["id"]
                    self._emit(row["id"], "event", row)
                if rows:
                    self._emit(
                        cursor, "metrics", self.store.run_metrics(project_id=project_id)
                    )
                elif time.time() - last_beat > 15:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(interval)
        except (ConnectionError, OSError):
            return

    def _emit(self, event_id: int, name: str, data) -> None:
        payload = json.dumps(data, default=str)
        frame = f"id: {event_id}\nevent: {name}\ndata: {payload}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _serve_static(self, path: str) -> None:
        """Serve the built frontend, falling back to index.html for client
        routes. Paths are resolved and confined to the dist directory."""
        if not _WEB_DIST.is_dir():
            self._send_json(
                {
                    "error": "Frontend not built.",
                    "hint": "cd web && npm install && npm run build",
                },
                status=503,
            )
            return

        rel = path.lstrip("/") or "index.html"
        target = (_WEB_DIST / rel).resolve()
        try:
            contained = target.is_relative_to(_WEB_DIST.resolve())
        except (OSError, ValueError):
            contained = False  # a guard that errors is a guard that says no
        if not contained:
            self._error(403, "Forbidden")  # path traversal attempt
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

    def _task_json(self, task: Task) -> dict:
        return {
            "id": task.id,
            "title": task.title,
            "goal": task.goal,
            "acceptance_criteria": task.acceptance_criteria,
            "status": task.status.value,
            "risk_level": task.risk_level,
            "revision_count": task.revision_count,
            "worker_role": task.worker_role,
            "validator_role": task.validator_role,
            "output": task.output,
            "escalation_reason": task.escalation_reason,
            "control": task.control,
            "kind": task.kind,
            "plan_id": task.plan_id,
            "project_id": task.project_id,
            "depends_on": self.store.dependencies(task.id) if task.id else [],
            "plan_approved": (
                self.store.is_plan_approved(task.id)
                if task.kind == "plan" and task.id
                else None
            ),
        }


def serve(
    store: Store,
    loop: Loop,
    registry: Registry,
    config: LoopConfig,
    host: str | None = None,
    port: int | None = None,
    config_path: str | None = None,
) -> DashboardServer:
    """Start the dashboard server. Returns it so callers (and tests) can
    shut it down."""
    addr = (
        host or config.server_host,
        port if port is not None else config.server_port,
    )
    return DashboardServer(addr, store, loop, registry, config, config_path)


def serve_forever(
    store: Store,
    loop: Loop,
    registry: Registry,
    config: LoopConfig,
    host: str | None = None,
    port: int | None = None,
    config_path: str | None = None,
) -> None:
    server = serve(store, loop, registry, config, host, port, config_path)
    h, p = server.server_address[0], server.server_address[1]
    print(f"agentloop dashboard on http://{h}:{p}")
    if not _WEB_DIST.is_dir():
        print("  (frontend not built — run: cd web && npm install && npm run build)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server._shutdown_flag.set()
        server.shutdown()
        server.server_close()
