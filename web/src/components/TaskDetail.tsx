import { useEffect, useState } from 'react'
import { api } from '../api'
import type { TaskDetail as Detail, ToolRequest } from '../types'
import { ToolRequestRow, sortRequests } from './ToolRequestPanel'

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
  const [requests, setRequests] = useState<ToolRequest[]>([])
  const [busy, setBusy] = useState(false)
  const [toolBusy, setToolBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .task(taskId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(String(e)))
    // A separate request rather than a field on the task payload: the rows carry
    // a `DecisionEffect` computed per read, so they cannot be cached alongside a
    // task whose other fields change on a different schedule.
    api
      .toolRequests(taskId)
      .then((r) => !cancelled && setRequests(r))
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

  // Deciding a request can change this task's status (approving the last blocking
  // one releases it to `pending`), so the detail is refetched too — and
  // `onChanged` so the board and the tools tab move with it. The server returns
  // the whole refreshed list because one decision can clear `parked` on sibling
  // rows; this narrows it back to the task on screen.
  const decideTool = async (id: number, action: 'approve' | 'reject') => {
    setToolBusy(id)
    setError(null)
    try {
      const all = await api.decideToolRequest(id, action)
      setRequests(all.filter((r) => r.task_id === task.id))
      onChanged()
      setDetail(await api.task(task.id))
    } catch (e) {
      // A decided row is final (400). Two humans, or one double-click: the loser
      // is told rather than shown a success.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setToolBusy(null)
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
  const pendingTools = requests.filter((r) => r.status === 'pending').length
  // `parked` is the store's own flag for "this row is holding its task right
  // now", so the warning below appears exactly when it is true — never inferred
  // from the status text, which cannot distinguish this park from any other
  // escalation.
  const parkedHere = requests.some((r) => r.parked && r.status === 'pending')

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
        {/* Which project charter this task's agents actually ran under, shown
            next to the approve button so "approved under the old rules" is
            visible where the decision is made. */}
        <dt>Charter</dt>
        <dd>
          {metrics.charter_versions.length
            ? metrics.charter_versions.map((v) => `v${v}`).join(', ')
            : 'none in effect'}
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
          {/* What each validator says it checked. Collapsed by default: this is
              evidence behind a verdict, not the verdict. An empty section is a
              clean review, never a reason to revise. */}
          {metrics.verdicts.map((v, i) =>
            v.findings ? (
              <details key={i}>
                <summary>
                  Findings · round {i + 1} ({v.kind})
                </summary>
                <pre>{v.findings}</pre>
              </details>
            ) : null,
          )}
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

      {/* A task parked awaiting tool approval used to show its escalation reason
          — "approving the request is the only decision that releases the task" —
          above the task-level Approve button, which does something else entirely:
          it marks the task DONE on partial output and, under the slice-3 graph,
          releases its dependents. The instruction and the nearest button shared a
          word and not a meaning, and the control the reason asks for was on a
          different tab. Rendering the rows here puts the asked-for decision within
          reach of the sentence that asks for it. The rows come from
          `ToolRequestRow`, not a copy of it: two renderings of one permission
          claim is the drift this slice spent four phases removing. */}
      {requests.length > 0 && (
        <>
          <h2 style={{ marginTop: 16 }}>
            Tool requests{' '}
            {pendingTools > 0 && (
              <span className="pending-flag">· {pendingTools} awaiting you</span>
            )}
          </h2>
          {parkedHere && (
            <div className="banner">
              This task is held at <code>needs_human</code> by the request marked
              PARKED. Approving it here releases the task. The{' '}
              <strong>Approve</strong> button at the bottom of this page is a
              different decision — it signs off the partial output above and marks
              the whole task done, leaving the request undecided.
            </div>
          )}
          {sortRequests(requests).map((r) => (
            <ToolRequestRow
              key={r.id}
              request={r}
              busy={toolBusy === r.id}
              onDecide={decideTool}
            />
          ))}
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
