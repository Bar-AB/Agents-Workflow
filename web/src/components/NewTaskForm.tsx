import { useState } from 'react'
import { api } from '../api'

// Task definition is the first human-in-the-loop step (spec §4.1), so it
// belongs in the dashboard rather than being CLI-only.
export function NewTaskForm({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState('')
  const [goal, setGoal] = useState('')
  const [criteria, setCriteria] = useState('')
  const [risk, setRisk] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.createTask({
        title,
        goal,
        acceptance_criteria: criteria,
        risk_level: risk,
      })
      setTitle('')
      setGoal('')
      setCriteria('')
      setRisk(1)
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const ready = title.trim() && goal.trim() && criteria.trim()

  return (
    <div className="panel">
      <h2>Define a task</h2>
      {error && <div className="banner err">{error}</div>}
      <form onSubmit={submit}>
        <label htmlFor="nt-title">Title</label>
        <input
          id="nt-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Add slugify util"
        />
        <label htmlFor="nt-goal">Goal</label>
        <textarea
          id="nt-goal"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Write slugify(text) in utils.py"
        />
        <label htmlFor="nt-criteria">Acceptance criteria</label>
        <textarea
          id="nt-criteria"
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
          placeholder="Lowercase, hyphen-separated, handles unicode, has tests"
        />
        <label htmlFor="nt-risk">Risk level</label>
        <select
          id="nt-risk"
          value={risk}
          onChange={(e) => setRisk(Number(e.target.value))}
        >
          <option value={0}>0 — low</option>
          <option value={1}>1 — normal</option>
          <option value={2}>2 — high (requires your sign-off)</option>
        </select>
        <button className="primary" type="submit" disabled={busy || !ready}>
          {busy ? 'Adding…' : 'Add task'}
        </button>
      </form>
    </div>
  )
}
