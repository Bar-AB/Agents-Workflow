import { useState } from 'react'
import { api } from '../api'
import type { MemoryFact } from '../types'

export function MemoryPanel({
  memory,
  onChanged,
}: {
  memory: MemoryFact[]
  onChanged: (next: MemoryFact[]) => void
}) {
  const [busy, setBusy] = useState<number | null>(null)

  const gate = async (id: number, action: 'approve' | 'reject') => {
    setBusy(id)
    try {
      onChanged(await api.gateMemory(id, action))
    } finally {
      setBusy(null)
    }
  }

  // Unapproved facts first: they're the ones that need a decision, and until
  // someone makes it they cannot reach any prompt.
  const sorted = [...memory].sort(
    (a, b) => a.approved - b.approved || a.key.localeCompare(b.key),
  )
  const pending = memory.filter((m) => !m.approved).length

  return (
    <div className="panel">
      <h2>
        Memory {pending > 0 && <span className="pending-flag">· {pending} pending</span>}
      </h2>
      {sorted.length === 0 && (
        <div className="empty">
          No facts yet. Approved facts get injected into agent prompts.
        </div>
      )}
      {sorted.map((f) => (
        <div className="fact" key={f.id}>
          <div className="head">
            <span className={`tier ${f.tier}`}>{f.tier}</span>
            <span className="key">{f.key}</span>
            <span className="pending-flag">{f.hit_count} hits</span>
          </div>
          <div className="val">{f.value}</div>
          {!f.approved && (
            <div className="actions" style={{ marginTop: 6 }}>
              <button
                className="primary"
                disabled={busy === f.id}
                onClick={() => gate(f.id, 'approve')}
              >
                Approve
              </button>
              <button
                className="danger"
                disabled={busy === f.id}
                onClick={() => gate(f.id, 'reject')}
              >
                Discard
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
