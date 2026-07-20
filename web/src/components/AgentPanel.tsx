import type { Agent, Task } from '../types'

// An agent is "busy" when some task sits in the phase that agent owns. The
// store has no agent-state table, and adding one would create a second place
// where truth about the loop lives — so this derives from task status instead.
function busyRoles(tasks: Task[]): Set<string> {
  const busy = new Set<string>()
  for (const t of tasks) {
    if (t.status === 'in_progress' || t.status === 'revising') {
      busy.add(t.worker_role)
    } else if (t.status === 'validating') {
      busy.add(t.validator_role)
    }
  }
  return busy
}

export function AgentPanel({
  agents,
  tasks,
}: {
  agents: Agent[]
  tasks: Task[]
}) {
  const busy = busyRoles(tasks)
  return (
    <div className="panel">
      <h2>Agents</h2>
      {agents.length === 0 && <div className="empty">No registry loaded.</div>}
      {agents.map((a) => (
        <div className="agent" key={a.role}>
          <div className="who">
            <div className="role">{a.role}</div>
            <div className="model">
              {a.model} · v{a.version} ·{' '}
              {(a.context_budget_tokens / 1000).toFixed(0)}k ctx
            </div>
            <div className="tools">
              {a.tools.map((t) => (
                <span className="tool" key={t}>
                  {t}
                </span>
              ))}
            </div>
          </div>
          <div className={`state ${busy.has(a.role) ? 'busy' : ''}`}>
            {busy.has(a.role) ? 'running' : 'idle'}
          </div>
        </div>
      ))}
    </div>
  )
}
