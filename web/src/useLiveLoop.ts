// Live state for the dashboard, driven by the backend's SSE stream.
//
// The stream carries the audit log itself, so every frame is both a UI update
// and a durable row someone can go read later. EventSource handles reconnect
// and replays from the last id it saw, which is why a dropped connection
// cannot silently desynchronize the view.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type {
  Agent,
  EventRow,
  MemoryFact,
  RunMetrics,
  Task,
  ToolRequest,
} from './types'

export type Connection = 'connecting' | 'live' | 'offline'

const MAX_EVENTS = 300 // bound memory on a long-running loop

// `projectId`: `null` means unscoped (every project — `agentloop run`'s own
// default), a number scopes every fetch and the SSE subscription to it. Not
// `undefined`, deliberately — `App` needs a state value it can hold before
// `/api/config`'s `default_project_id` has resolved, and `null` is that
// "known, and known to mean 'every project' for now" value, distinct from
// "not yet decided".
export function useLiveLoop(projectId: number | null) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [metrics, setMetrics] = useState<RunMetrics | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const [memory, setMemory] = useState<MemoryFact[]>([])
  const [toolRequests, setToolRequests] = useState<ToolRequest[]>([])
  const [events, setEvents] = useState<EventRow[]>([])
  // A monotonic "something happened" counter, separate from `events.length`.
  // `TaskDetail` used the length as its refetch trigger, and the array is
  // capped at MAX_EVENTS — so after 300 events (about fifteen task rounds) the
  // length was constant forever and the detail pane stopped refetching for the
  // rest of the session. It then showed a stale status, output, verdict list
  // and tool-request rows directly above the Approve / Reject / Redo buttons,
  // with no way to force it: the header Refresh does not refetch the detail,
  // and re-clicking the same card sets `selected` to the value it already has.
  const [revision, setRevision] = useState(0)
  const [connection, setConnection] = useState<Connection>('connecting')
  const [error, setError] = useState<string | null>(null)

  // The "latest ref" pattern: always holds the `projectId` this render saw,
  // read (never written) from inside `refresh`'s async continuation below.
  // Plain assignment during render, not inside an effect — an effect would
  // still be correct, but would update one render later than a `refresh()`
  // call issued synchronously from an event handler could observe, and this
  // value is idempotent to write (it only ever mirrors the current render's
  // own argument), which is what makes writing it during render safe here.
  const latestProjectId = useRef(projectId)
  latestProjectId.current = projectId

  // Tasks, memory and the tool queue aren't pushed field-by-field; an event
  // just tells us they may have changed, and we re-read the source of truth.
  //
  // Guarded against a stale cross-project response, unlike a plain
  // `cancelled` flag closed over by one effect run: `refresh` is called
  // repeatedly for the *same* mounted instance (on mount, from every SSE
  // burst, from the manual Refresh button), not once per effect, so there is
  // no single effect cleanup to hang a flag off. Instead each call captures
  // which project it was issued for and checks, right before every
  // `setState`, that the scope hasn't moved on since — if the user switched
  // projects while this call's `Promise.all` was still in flight, its result
  // is for a project nobody is looking at anymore and is dropped rather than
  // silently overwriting the new project's already-applied data. This is the
  // one place in this codebase that fetches project-scoped data across a
  // `projectId` change; every other project-aware effect (`App`'s
  // `/api/config` load, `ProjectSwitcher`'s own list fetch) is a one-shot
  // per mount and uses the ordinary `cancelled`-flag idiom instead, which is
  // why this one needed a different shape of guard rather than copying theirs.
  const refresh = useCallback(async () => {
    const requestedFor = projectId
    try {
      const pid = projectId ?? undefined
      const [t, m, mem, tools] = await Promise.all([
        api.tasks(pid),
        api.metrics(pid),
        api.memory(pid),
        api.toolRequests(undefined, pid),
      ])
      if (latestProjectId.current !== requestedFor) return
      setTasks(t)
      setMetrics(m)
      setMemory(mem)
      setToolRequests(tools)
      setError(null)
    } catch (e) {
      if (latestProjectId.current !== requestedFor) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [projectId])

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

    // Closed and reopened (not just re-filtered client-side) whenever
    // `projectId` changes, via this effect's own dependency array — a fresh
    // `EventSource` for a new scope, matching `refresh`'s own per-project
    // re-fetch rather than filtering one unscoped connection's frames here.
    // A fresh connection carries no `Last-Event-ID`, so the server replays
    // that scope's whole history from the start — cleared here first so the
    // feed doesn't show yesterday's scope's tail mixed in with today's.
    //
    // The other four are cleared for the same reason, on the same trigger:
    // this effect re-runs exactly when `projectId` changes (its own identity
    // is unaffected by the SSE bursts that also call `refresh()` via
    // `scheduleRefresh`, since `refresh`/`scheduleRefresh` only change
    // identity when `projectId` itself does). Without this, switching
    // projects would leave the previous project's task board, stat bar,
    // memory tab and tool queue on screen — genuinely stale, not merely
    // loading — under the newly-selected project's own label, for however
    // long the new `refresh()` call below takes to resolve. The stale-scope
    // guard on `refresh` (above) stops a late response from *overwriting*
    // fresh data with old; this stops the old data from being shown as
    // current in the meantime.
    setTasks([])
    setMetrics(null)
    setMemory([])
    setToolRequests([])
    setEvents([])
    const streamUrl =
      projectId === null ? '/api/stream' : `/api/stream?project=${projectId}`
    const source = new EventSource(streamUrl)
    source.onopen = () => setConnection('live')
    source.onerror = () => setConnection('offline') // EventSource self-retries

    source.addEventListener('event', (e) => {
      setConnection('live')
      try {
        const row = JSON.parse((e as MessageEvent).data) as EventRow
        setEvents((prev) => [row, ...prev].slice(0, MAX_EVENTS))
        setRevision((n) => n + 1)
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
    // `projectId` listed explicitly even though `refresh`'s own identity
    // already changes with it: `streamUrl` above reads it directly, and this
    // effect must reopen the EventSource on that change too, not only refetch.
  }, [refresh, scheduleRefresh, projectId])

  return {
    tasks,
    metrics,
    agents,
    memory,
    toolRequests,
    events,
    revision,
    connection,
    error,
    refresh,
    setMemory,
    setToolRequests,
  }
}
