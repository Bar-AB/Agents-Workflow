// Typed client for the Python backend. Every network call in the app goes
// through here, so error handling and shapes live in one place.

import type {
  Agent,
  CharterView,
  EventRow,
  MemoryFact,
  LoopConfigView,
  Project,
  RepoConfigUpdate,
  RunMetrics,
  Task,
  TaskDetail,
  ToolRequest,
} from './types'

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`GET ${path} failed (${res.status}): ${detail.slice(0, 200)}`)
  }
  return (await res.json()) as T
}

async function post<T>(path: string, body: unknown = {}): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`POST ${path} failed (${res.status}): ${detail.slice(0, 200)}`)
  }
  return (await res.json()) as T
}

// Appends `?k=v&...` for whichever params are present, omitting the ones
// that are `undefined` rather than sending `k=undefined` — one place for the
// pattern every project-scoped GET below needs, instead of a hand-rolled
// ternary at each call site.
function qs(params: Record<string, number | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}=${v}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

export const api = {
  // `projectId` omitted -> unscoped, matching every listed endpoint's own
  // `?project=` contract (Phase 6): absent means "every project", not "the
  // default one".
  tasks: (projectId?: number) =>
    get<{ tasks: Task[] }>(`/api/tasks${qs({ project: projectId })}`).then(
      (r) => r.tasks,
    ),
  task: (id: number) => get<TaskDetail>(`/api/tasks/${id}`),
  metrics: (projectId?: number) =>
    get<RunMetrics>(`/api/metrics${qs({ project: projectId })}`),
  agents: () => get<{ agents: Agent[] }>('/api/agents').then((r) => r.agents),
  memory: (projectId?: number) =>
    get<{ memory: MemoryFact[] }>(`/api/memory${qs({ project: projectId })}`).then(
      (r) => r.memory,
    ),
  // `/api/events` never gained a `?project=` filter in Phase 6 (only
  // `/api/tasks`, `/api/metrics`, `/api/memory`, `/api/tool_requests` and
  // `/api/stream` did) — and nothing in this app calls this method today;
  // the live event feed comes from the SSE stream below, which
  // `useLiveLoop`/`ProjectSwitcher` scope directly. Adding a `projectId`
  // param the server would silently ignore would be worse than leaving it
  // unscoped, so this stays as it was.
  events: (since = 0) =>
    get<{ events: EventRow[] }>(`/api/events?since=${since}`).then(
      (r) => r.events,
    ),
  config: () => get<LoopConfigView>('/api/config'),

  projects: () =>
    get<{ projects: Project[] }>('/api/projects').then((r) => r.projects),
  createProject: (name: string, repo_root: string, workspace_mode = 'scratch') =>
    post<{ project: Project }>('/api/projects', {
      name,
      repo_root,
      workspace_mode,
    }).then((r) => r.project),
  renameProject: (id: number, name: string) =>
    post<{ project: Project }>(`/api/projects/${id}/rename`, { name }).then(
      (r) => r.project,
    ),
  // Omitted `workspace_mode` preserves the project's current one — never a
  // silent reset to scratch (server.py's own `_project_action` contract).
  repointProject: (id: number, repo_root: string, workspace_mode?: string) =>
    post<{ project: Project }>(`/api/projects/${id}/repoint`, {
      repo_root,
      ...(workspace_mode === undefined ? {} : { workspace_mode }),
    }).then((r) => r.project),
  archiveProject: (id: number) =>
    post<{ project: Project }>(`/api/projects/${id}/archive`).then(
      (r) => r.project,
    ),
  useProject: (id: number) =>
    post<{ project: Project }>(`/api/projects/${id}/use`).then(
      (r) => r.project,
    ),

  // Takes effect only on the next `agentloop serve`/`run` process — never the
  // one that served this request, so the panel must say so rather than imply
  // it already applied.
  setRepoConfig: (repo_root: string, workspace_mode: string) =>
    post<RepoConfigUpdate>('/api/config/repo', { repo_root, workspace_mode }),

  // `taskId` narrows to one task's rows, which is what `TaskDetail` shows inline
  // when a task is parked awaiting a decision. Server-side filtering rather than
  // fetching everything and filtering here: the route already validates
  // `task_id` (a non-integer is a 400), and the queue is unbounded.
  // `projectId` is the trailing param slice 10 adds — combining both filters
  // is meaningful (task_id already narrows to one task, so `project` there
  // only matters as a consistency check the server itself makes).
  toolRequests: (taskId?: number, projectId?: number) =>
    get<{ tool_requests: ToolRequest[] }>(
      `/api/tool_requests${qs({ task_id: taskId, project: projectId })}`,
    ).then((r) => r.tool_requests),

  // `project_id` omitted (undefined, never null — the JSON body must not
  // carry the key at all) resolves server-side to the default project, same
  // as every CLI call site that never sets it. Undefined, not optional-only
  // in the type, because `JSON.stringify` drops an `undefined` value's key
  // but would send a literal `null` for the field if it were typed to
  // accept one, and the server has no "explicit null" case for this field.
  createTask: (body: {
    title: string
    goal: string
    acceptance_criteria: string
    risk_level: number
    project_id?: number
  }) => post<{ task: Task }>('/api/tasks', body).then((r) => r.task),

  decide: (id: number, action: 'approve' | 'reject' | 'redo', note = '') =>
    post<{ task: Task }>(`/api/tasks/${id}/${action}`, { note }).then(
      (r) => r.task,
    ),

  // Mid-run control: pause/resume/abort a task while the loop is working it.
  control: (id: number, action: 'pause' | 'resume' | 'abort', note = '') =>
    post<{ task: Task }>(`/api/tasks/${id}/${action}`, { note }).then(
      (r) => r.task,
    ),

  charter: (projectId?: number) =>
    get<CharterView>(`/api/charter${qs({ project: projectId })}`),

  // Publishing a version, never editing one: the charter table is append-only.
  // An oversize or empty body is refused with a 400 rather than trimmed.
  // project_id rides the body, not a query param -- POST /api/tasks already
  // reads it that way, and an omitted one resolves to the default project.
  setCharter: (body: string, note = '', projectId?: number) =>
    post<CharterView>('/api/charter', { body, note, project_id: projectId }),

  // A decision on one request can change other rows (a release clears every
  // `parked` flag on the task), so the server returns the refreshed list —
  // /api/memory's precedent. An already-decided row is a 400, surfaced as an
  // error rather than swallowed: the store's compare-and-swap lets exactly one
  // of two humans win, and the loser has to be told.
  decideToolRequest: (id: number, action: 'approve' | 'reject', note = '') =>
    post<{ tool_requests: ToolRequest[] }>(
      `/api/tool_requests/${id}/${action}`,
      { note },
    ).then((r) => r.tool_requests),

  gateMemory: (id: number, action: 'approve' | 'reject' | 'pin' | 'unpin') =>
    post<{ memory: MemoryFact[] }>(`/api/memory/${id}/${action}`).then(
      (r) => r.memory,
    ),
}
