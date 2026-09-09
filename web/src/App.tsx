import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import { AgentPanel } from './components/AgentPanel'
import { CharterPanel } from './components/CharterPanel'
import { EventFeed } from './components/EventFeed'
import { MemoryPanel } from './components/MemoryPanel'
import { NewTaskForm } from './components/NewTaskForm'
import {
  ProjectSwitcher,
  readStoredProjectId,
} from './components/ProjectSwitcher'
import { RepoConfigPanel } from './components/RepoConfigPanel'
import { StatBar } from './components/StatBar'
import { ToolRequestPanel } from './components/ToolRequestPanel'
import { TaskBoard } from './components/TaskBoard'
import { TaskDetail } from './components/TaskDetail'
import { useLiveLoop } from './useLiveLoop'

type Tab = 'detail' | 'new' | 'memory' | 'tools' | 'charter' | 'repo'

export default function App() {
  // `undefined` from storage means "nothing remembered" -> start unscoped
  // and let the effect below resolve it to /api/config's default_project_id
  // once that arrives; `null` (explicitly "All projects") or a stored id are
  // used immediately and never auto-overridden.
  const [projectId, setProjectId] = useState<number | null>(() => {
    const stored = readStoredProjectId()
    return stored === undefined ? null : stored
  })

  // True the moment a real choice exists — either one was already in
  // storage at mount, or the switcher makes one via `chooseProject` below.
  // Read (never written) from inside the effect's `.then`, which is what
  // makes the guard hold even for a choice made *after* the effect started
  // but *before* `api.config()` resolved: without this, `handleChange` below
  // races the effect's own async resolution, and the effect's `.then` would
  // silently snap the switcher back to the default the instant it landed —
  // discarding the very choice this whole feature exists to let a user make.
  const userChoseProject = useRef(readStoredProjectId() !== undefined)

  useEffect(() => {
    if (userChoseProject.current) return
    let cancelled = false
    api
      .config()
      .then((c) => {
        if (!cancelled && !userChoseProject.current) {
          setProjectId(c.default_project_id)
        }
      })
      .catch(() => undefined) // stay unscoped; the header still works
    return () => {
      cancelled = true
    }
  }, [])

  const chooseProject = (id: number | null) => {
    userChoseProject.current = true
    setProjectId(id)
  }

  const {
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
  } = useLiveLoop(projectId)
  const [selected, setSelected] = useState<number | null>(null)
  const [tab, setTab] = useState<Tab>('new')

  const select = (id: number) => {
    setSelected(id)
    setTab('detail')
  }

  // A task selected under one project's scope doesn't belong to the next —
  // `TaskDetail` fetches by id regardless of scope, so without this a
  // project switch could leave the Detail tab showing another project's
  // task while everything else on screen (board, memory, tools) had already
  // moved to the new one.
  useEffect(() => {
    setSelected(null)
  }, [projectId])

  const pendingFacts = memory.filter((m) => !m.approved).length
  // A pending request may be holding a task at NEEDS_HUMAN right now, so the
  // count belongs on the tab label rather than one click away.
  const pendingTools = toolRequests.filter((r) => r.status === 'pending').length

  return (
    <div className="app">
      <header className="header">
        <h1>
          agentloop <span>· live</span>
        </h1>
        <div className={`conn ${connection}`}>
          <span className="dot" />
          {connection === 'live'
            ? 'streaming'
            : connection === 'connecting'
              ? 'connecting…'
              : 'reconnecting…'}
        </div>
        <div className="spacer" />
        <ProjectSwitcher projectId={projectId} onChange={chooseProject} />
        <button onClick={() => void refresh()}>Refresh</button>
      </header>

      {error && <div className="banner err">{error}</div>}

      <StatBar metrics={metrics} />

      <div className="grid">
        <div>
          <TaskBoard tasks={tasks} selectedId={selected} onSelect={select} />
          {tab === 'detail' && selected !== null && (
            <TaskDetail
              taskId={selected}
              // A monotonic counter, not `events.length`: the feed is capped
              // at MAX_EVENTS, so the length stops changing and this stopped
              // being a refresh trigger at all.
              version={revision}
              onChanged={() => void refresh()}
            />
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="tabs">
            <button
              className={`tab ${tab === 'new' ? 'on' : ''}`}
              onClick={() => setTab('new')}
            >
              New task
            </button>
            <button
              className={`tab ${tab === 'detail' ? 'on' : ''}`}
              onClick={() => setTab('detail')}
              disabled={selected === null}
            >
              Detail
            </button>
            <button
              className={`tab ${tab === 'memory' ? 'on' : ''}`}
              onClick={() => setTab('memory')}
            >
              Memory{pendingFacts > 0 ? ` (${pendingFacts})` : ''}
            </button>
            <button
              className={`tab ${tab === 'tools' ? 'on' : ''}`}
              onClick={() => setTab('tools')}
            >
              Tools{pendingTools > 0 ? ` (${pendingTools})` : ''}
            </button>
            <button
              className={`tab ${tab === 'charter' ? 'on' : ''}`}
              onClick={() => setTab('charter')}
            >
              Charter
            </button>
            <button
              className={`tab ${tab === 'repo' ? 'on' : ''}`}
              onClick={() => setTab('repo')}
            >
              Repo
            </button>
          </div>

          {tab === 'new' && (
            <NewTaskForm
              onCreated={() => void refresh()}
              projectId={projectId}
            />
          )}
          {tab === 'memory' && (
            <MemoryPanel memory={memory} onChanged={setMemory} />
          )}
          {tab === 'tools' && (
            <ToolRequestPanel
              requests={toolRequests}
              onChanged={setToolRequests}
            />
          )}
          {tab === 'charter' && <CharterPanel />}
          {tab === 'repo' && <RepoConfigPanel />}

          <AgentPanel agents={agents} tasks={tasks} />
          <EventFeed events={events} />
        </div>
      </div>
    </div>
  )
}
