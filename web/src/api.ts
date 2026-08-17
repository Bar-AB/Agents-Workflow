// Typed client for the Python backend. Every network call in the app goes
// through here, so error handling and shapes live in one place.

import type {
  Agent,
  CharterView,
  EventRow,
  MemoryFact,
  LoopConfigView,
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

export const api = {
  tasks: () => get<{ tasks: Task[] }>('/api/tasks').then((r) => r.tasks),
  task: (id: number) => get<TaskDetail>(`/api/tasks/${id}`),
  metrics: () => get<RunMetrics>('/api/metrics'),
  agents: () => get<{ agents: Agent[] }>('/api/agents').then((r) => r.agents),
  memory: () =>
    get<{ memory: MemoryFact[] }>('/api/memory').then((r) => r.memory),
  events: (since = 0) =>
    get<{ events: EventRow[] }>(`/api/events?since=${since}`).then(
      (r) => r.events,
    ),
  config: () => get<LoopConfigView>('/api/config'),

  // `taskId` narrows to one task's rows, which is what `TaskDetail` shows inline
  // when a task is parked awaiting a decision. Server-side filtering rather than
  // fetching everything and filtering here: the route already validates
  // `task_id` (a non-integer is a 400), and the queue is unbounded.
  toolRequests: (taskId?: number) =>
    get<{ tool_requests: ToolRequest[] }>(
      taskId === undefined
        ? '/api/tool_requests'
        : `/api/tool_requests?task_id=${taskId}`,
    ).then((r) => r.tool_requests),

  createTask: (body: {
    title: string
    goal: string
    acceptance_criteria: string
    risk_level: number
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

  charter: () => get<CharterView>('/api/charter'),

  // Publishing a version, never editing one: the charter table is append-only.
  // An oversize or empty body is refused with a 400 rather than trimmed.
  setCharter: (body: string, note = '') =>
    post<CharterView>('/api/charter', { body, note }),

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
