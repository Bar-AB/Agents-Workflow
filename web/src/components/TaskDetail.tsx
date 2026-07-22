import { useEffect, useState } from 'react'
import { api } from '../api'
import type { TaskDetail as Detail } from '../types'

export function TaskDetail({
  taskId,
  version,
  onChanged,
}: {
  taskId: number
  // Bumped by the parent whenever the loop emits an event, so the open detail
  // refetches in step with the live stream instead of going stale.
  version: number
  onChanged: () => void
}) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .task(taskId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(String(e)))
    return () => {
      cancelled = true
    }
  }, [taskId, version])

  if (error) return <div className="panel detail banner err">{error}</div>
  if (!detail) return <div className="panel detail empty">Loading…</div>

  const { task, metrics, test_runs } = detail
  const decide = async (action: 'approve' | 'reject' | 'redo') => {
    setBusy(true)
    try {
      await api.decide(task.id, action)
      onChanged()
      setDetail(await api.task(task.id))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const control = async (action: 'pause' | 'resume' | 'abort') => {
    setBusy(true)
    try {
      await api.control(task.id, action)
      onChanged()
      setDetail(await api.task(task.id))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const latestTest = test_runs[test_runs.length - 1]
  // A task is "in flight" while the loop can still be working it — those are
  // the states where pause/abort are meaningful.
  const inFlight = ['pending', 'in_progress', 'testing', 'validating',
    'revising'].includes(task.status)
  const terminal = ['done', 'failed', 'aborted'].includes(task.status)

  return (
    <div className="panel detail">
      <h2>
        Task #{task.id} — {task.title}
      </h2>

      {task.escalation_reason && (
        <div className="banner">{task.escalation_reason}</div>
      )}

      <dl className="kv">
        <dt>Status</dt>
        <dd>{task.status}</dd>
        <dt>Risk</dt>
        <dd>{task.risk_level}</dd>
        <dt>Revisions</dt>
        <dd>{task.revision_count}</dd>
        <dt>Cost / tokens</dt>
        <dd>
          ${metrics.cost_usd.toFixed(4)} · {metrics.tokens}
        </dd>
        <dt>Agent time</dt>
        <dd>{metrics.wall_seconds}s over {metrics.attempts} calls</dd>
        <dt>Tests</dt>
        <dd className={latestTest ? `t-${latestTest.status}` : 't-na'}>
          {latestTest ? `${latestTest.status} — ${latestTest.summary}` : 'not run'}
        </dd>
      </dl>

      {metrics.verdicts.length > 0 && (
        <>
          <h2>Verdict history</h2>
          <div className="verdicts">
            {metrics.verdicts.map((v, i) => (
              <span className={`verdict v-${v.kind}`} key={i}>
                {v.kind} {v.confidence.toFixed(2)}
              </span>
            ))}
          </div>
        </>
      )}

      {test_runs.length > 0 && (
        <>
          <h2>Executed test runs</h2>
          {test_runs.map((r) => (
            <div className={`test-row t-${r.status}`} key={r.id}>
              {r.status} · exit {r.exit_code ?? '—'} · {r.duration_s}s ·{' '}
              {r.summary}
            </div>
          ))}
        </>
      )}

      <h2 style={{ marginTop: 16 }}>Goal</h2>
      <pre>{task.goal}</pre>
      <h2>Acceptance criteria</h2>
      <pre>{task.acceptance_criteria}</pre>
      {task.output && (
        <>
          <h2>Latest output</h2>
          <pre>{task.output}</pre>
        </>
      )}

      {(inFlight || task.status === 'paused') && (
        <div className="actions" style={{ marginBottom: 8 }}>
          {inFlight && (
            <button disabled={busy} onClick={() => control('pause')}>
              Pause
            </button>
          )}
          {task.status === 'paused' && (
            <button className="primary" disabled={busy} onClick={() => control('resume')}>
              Resume
            </button>
          )}
          <button
            className="danger"
            disabled={busy}
            onClick={() => control('abort')}
          >
            Abort
          </button>
        </div>
      )}

      <div className="actions">
        <button
          className="primary"
          disabled={busy || task.status === 'done'}
          onClick={() => decide('approve')}
        >
          Approve
        </button>
        <button disabled={busy} onClick={() => decide('redo')}>
          Redo (fresh start)
        </button>
        <button
          className="danger"
          disabled={busy || terminal}
          onClick={() => decide('reject')}
        >
          Reject
        </button>
      </div>
    </div>
  )
}
