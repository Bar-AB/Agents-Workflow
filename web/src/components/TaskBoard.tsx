import type { Task, TaskStatus } from '../types'

// Columns follow the lifecycle order in the spec, so the board reads left to
// right as a task's journey. in_progress/testing/validating/revising are
// collapsed into one "working" column: they're all "an agent has it".
const COLUMNS: { key: string; label: string; statuses: TaskStatus[] }[] = [
  { key: 'pending', label: 'Pending', statuses: ['pending'] },
  {
    key: 'working',
    label: 'Working',
    statuses: ['in_progress', 'testing', 'validating', 'revising'],
  },
  { key: 'needs_human', label: 'Needs you', statuses: ['needs_human'] },
  { key: 'done', label: 'Done', statuses: ['done'] },
  { key: 'failed', label: 'Failed', statuses: ['failed'] },
]

const ACTIVE: TaskStatus[] = ['in_progress', 'testing', 'validating', 'revising']

export function TaskBoard({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: Task[]
  selectedId: number | null
  onSelect: (id: number) => void
}) {
  return (
    <div className="panel">
      <h2>Task board</h2>
      <div className="board">
        {COLUMNS.map((col) => {
          const items = tasks.filter((t) => col.statuses.includes(t.status))
          return (
            <div className="column" key={col.key}>
              <div className="column-head">
                <span>{col.label}</span>
                <span className="count">{items.length}</span>
              </div>
              {items.length === 0 && <div className="empty">—</div>}
              {items.map((t) => (
                <div
                  key={t.id}
                  className={[
                    'card',
                    `s-${t.status}`,
                    ACTIVE.includes(t.status) ? 'active-card' : '',
                    selectedId === t.id ? 'selected' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onClick={() => onSelect(t.id)}
                >
                  <div className="id">#{t.id}</div>
                  <div className="title">{t.title}</div>
                  <div className="meta">
                    <span>{t.status}</span>
                    {t.revision_count > 0 && <span>rev {t.revision_count}</span>}
                    {t.risk_level >= 2 && <span className="risk-2">high risk</span>}
                  </div>
                </div>
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}
