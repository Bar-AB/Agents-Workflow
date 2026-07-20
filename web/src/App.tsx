import { useState } from 'react'
import { AgentPanel } from './components/AgentPanel'
import { EventFeed } from './components/EventFeed'
import { MemoryPanel } from './components/MemoryPanel'
import { NewTaskForm } from './components/NewTaskForm'
import { StatBar } from './components/StatBar'
import { TaskBoard } from './components/TaskBoard'
import { TaskDetail } from './components/TaskDetail'
import { useLiveLoop } from './useLiveLoop'

type Tab = 'detail' | 'new' | 'memory'

export default function App() {
  const {
    tasks,
    metrics,
    agents,
    memory,
    events,
    connection,
    error,
    refresh,
    setMemory,
  } = useLiveLoop()
  const [selected, setSelected] = useState<number | null>(null)
  const [tab, setTab] = useState<Tab>('new')

  const select = (id: number) => {
    setSelected(id)
    setTab('detail')
  }

  const pendingFacts = memory.filter((m) => !m.approved).length

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
              version={events.length}
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
          </div>

          {tab === 'new' && <NewTaskForm onCreated={() => void refresh()} />}
          {tab === 'memory' && (
            <MemoryPanel memory={memory} onChanged={setMemory} />
          )}

          <AgentPanel agents={agents} tasks={tasks} />
          <EventFeed events={events} />
        </div>
      </div>
    </div>
  )
}
