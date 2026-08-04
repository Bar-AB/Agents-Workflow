import { useEffect, useState } from 'react'
import { api } from '../api'
import type { CharterView } from '../types'

// The human write surface for project-wide rules. Agents have none: they only
// ever read the charter out of a prompt, so this panel and the CLI are the only
// two ways it can change.
export function CharterPanel() {
  const [view, setView] = useState<CharterView | null>(null)
  const [draft, setDraft] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .charter()
      .then((v) => {
        if (cancelled) return
        setView(v)
        setDraft(v.active?.body ?? '')
      })
      .catch((e) => !cancelled && setError(String(e)))
    return () => {
      cancelled = true
    }
  }, [])

  const publish = async () => {
    setBusy(true)
    setError(null)
    try {
      const next = await api.setCharter(draft, note)
      setView(next)
      setDraft(next.active?.body ?? '')
      setNote('')
    } catch (e) {
      // Oversize or empty is a 400 from the store, on purpose: the charter is
      // refused loudly here rather than truncated inside a prompt later.
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!view) return <div className="panel empty">Loading charter…</div>

  const active = view.active
  const dirty = draft !== (active?.body ?? '')

  return (
    <div className="panel">
      <h2>
        Project charter{' '}
        {active ? (
          <span className="pending-flag">· v{active.id} in effect</span>
        ) : (
          <span className="pending-flag">· none set</span>
        )}
      </h2>

      {error && <div className="banner err">{error}</div>}

      <div className="empty" style={{ marginBottom: 8 }}>
        Rules every worker, validator and planner prompt carries. Evidence for
        the validator, not a gate: a violation reaches the loop only as its
        ordinary revise/escalate verdict. Clearing the charter is a CLI
        operation (<code>agentloop charter clear</code>).
      </div>

      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={10}
        spellCheck={false}
        placeholder="e.g. Errors are raised, never returned as None."
        style={{ width: '100%', fontFamily: 'inherit' }}
      />
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Why this edit was made (optional)"
        style={{ width: '100%', marginTop: 6 }}
      />

      <div className="actions" style={{ marginTop: 6 }}>
        <button className="primary" disabled={busy || !dirty} onClick={publish}>
          Publish new version
        </button>
        <button onClick={() => setShowHistory((s) => !s)}>
          {showHistory ? 'Hide' : 'Show'} history ({view.history.length})
        </button>
      </div>

      {showHistory &&
        view.history
          .slice()
          .reverse()
          .map((v) => (
            <div className="fact" key={v.id}>
              <div className="head">
                <span className="key">v{v.id}</span>
                {/* "When did the rules change" is the question a history
                    answers, so the date belongs here and not only in the CLI. */}
                <span className="pending-flag">
                  {new Date(v.created_at * 1000).toLocaleString()} ·{' '}
                  {v.body.trim() ? `${v.body.length} chars` : 'cleared'}
                </span>
              </div>
              {v.note && <div className="val">{v.note}</div>}
            </div>
          ))}
    </div>
  )
}
