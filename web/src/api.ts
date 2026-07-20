// Typed client for the Python backend. Every network call in the app goes
// through here, so error handling and shapes live in one place.

import type {
  Agent,
  EventRow,
  MemoryFact,
  LoopConfigView,
  RunMetrics,
  Task,
  TaskDetail,
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

  gateMemory: (id: number, action: 'approve' | 'reject') =>
    post<{ memory: MemoryFact[] }>(`/api/memory/${id}/${action}`).then(
      (r) => r.memory,
    ),
}
