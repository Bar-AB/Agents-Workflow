import { useState } from 'react'
import { AgentPanel } from './components/AgentPanel'
import { CharterPanel } from './components/CharterPanel'
import { EventFeed } from './components/EventFeed'
import { MemoryPanel } from './components/MemoryPanel'
import { NewTaskForm } from './components/NewTaskForm'
import { StatBar } from './components/StatBar'
import { ToolRequestPanel } from './components/ToolRequestPanel'
import { TaskBoard } from './components/TaskBoard'
import { TaskDetail } from './components/TaskDetail'
import { useLiveLoop } from './useLiveLoop'

type Tab = 'detail' | 'new' | 'memory' | 'tools' | 'charter'

export default function App() {
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
  } = useLiveLoop()
  const [selected, setSelected] = useState<number | null>(null)
  const [tab, setTab] = useState<Tab>('new')

  const select = (id: number) => {
    setSelected(id)
    setTab('detail')
  }

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
          </div>

          {tab === 'new' && <NewTaskForm onCreated={() => void refresh()} />}
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

          <AgentPanel agents={agents} tasks={tasks} />
          <EventFeed events={events} />
        </div>
      </div>
    </div>
  )
}
