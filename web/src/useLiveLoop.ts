// Live state for the dashboard, driven by the backend's SSE stream.
//
// The stream carries the audit log itself, so every frame is both a UI update
// and a durable row someone can go read later. EventSource handles reconnect
// and replays from the last id it saw, which is why a dropped connection
// cannot silently desynchronize the view.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Agent, EventRow, MemoryFact, RunMetrics, Task } from './types'

export type Connection = 'connecting' | 'live' | 'offline'

const MAX_EVENTS = 300 // bound memory on a long-running loop

export function useLiveLoop() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [metrics, setMetrics] = useState<RunMetrics | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const [memory, setMemory] = useState<MemoryFact[]>([])
  const [events, setEvents] = useState<EventRow[]>([])
  const [connection, setConnection] = useState<Connection>('connecting')
  const [error, setError] = useState<string | null>(null)

  // Tasks and memory aren't pushed field-by-field; an event just tells us
  // they may have changed, and we re-read the source of truth.
  const refresh = useCallback(async () => {
    try {
      const [t, m, mem] = await Promise.all([
        api.tasks(),
        api.metrics(),
        api.memory(),
      ])
      setTasks(t)
      setMetrics(m)
      setMemory(mem)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  // Coalesce bursts: a single loop step emits several events in a few ms, and
  // refetching per event would hammer the backend for no visible gain.
  const pending = useRef<number | null>(null)
  const scheduleRefresh = useCallback(() => {
    if (pending.current !== null) return
    pending.current = window.setTimeout(() => {
      pending.current = null
      void refresh()
    }, 120)
  }, [refresh])

  useEffect(() => {
    void refresh()
    api.agents().then(setAgents).catch(() => undefined)

    const source = new EventSource('/api/stream')
    source.onopen = () => setConnection('live')
    source.onerror = () => setConnection('offline') // EventSource self-retries

    source.addEventListener('event', (e) => {
      setConnection('live')
      try {
        const row = JSON.parse((e as MessageEvent).data) as EventRow
        setEvents((prev) => [row, ...prev].slice(0, MAX_EVENTS))
        scheduleRefresh()
      } catch {
        /* a malformed frame shouldn't take the dashboard down */
      }
    })

    source.addEventListener('metrics', (e) => {
      try {
        setMetrics(JSON.parse((e as MessageEvent).data) as RunMetrics)
      } catch {
        /* ignore */
      }
    })

    return () => {
      source.close()
      if (pending.current !== null) window.clearTimeout(pending.current)
    }
  }, [refresh, scheduleRefresh])

  return {
    tasks,
    metrics,
    agents,
    memory,
    events,
    connection,
    error,
    refresh,
    setMemory,
  }
}
