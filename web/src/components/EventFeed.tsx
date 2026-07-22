import type { EventRow } from '../types'

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false })
}

// Each event kind has one field worth showing in a dense feed; the full
// payload is always available via `agentloop events <id>`.
function digest(ev: EventRow): string {
  const p = ev.payload as Record<string, any>
  switch (ev.kind) {
    case 'verdict':
      return `${p.kind} conf=${Number(p.confidence).toFixed(2)} tests=${p.tests_passed}`
    case 'test_run':
      return `${p.status} — ${p.summary ?? ''}`
    case 'test_disagreement':
      return `validator claimed ${p.validator_claimed}, actually ${p.actual}`
    case 'worker_output':
    case 'validator_output':
      return `${p.tokens_in}→${p.tokens_out} tok, $${Number(p.cost_usd ?? 0).toFixed(4)}`
    case 'worker_prompt':
    case 'validator_prompt':
      return `${p.role} · tools: ${(p.tools ?? []).join(', ') || 'none'}`
    case 'memory_promoted':
      return `${p.key} → loop (${p.hit_count} hits)`
    case 'memory_write':
      return `${p.tier}/${p.key}${p.approved ? '' : ' (pending)'}${p.pinned ? ' 📌' : ''}`
    case 'memory_pinned':
    case 'memory_unpinned':
      return `${p.tier}/${p.key}`
    case 'eval_run':
      return `${p.runner}: ${p.n_fixtures} fixtures, agreement ${p.agreement}`
    case 'human_abort':
      return p.note ? String(p.note) : 'aborted mid-run'
    case 'task_defined':
      return String(p.title ?? '')
    default:
      // control:pause / control:abort / status:paused etc. carry no payload
      // worth digesting; the kind itself is the message.
      if (ev.kind.startsWith('control:') || ev.kind.startsWith('status:'))
        return ''
      return p.reason ? String(p.reason) : JSON.stringify(p).slice(0, 90)
  }
}

export function EventFeed({ events }: { events: EventRow[] }) {
  return (
    <div className="panel">
      <h2>Audit trail (live)</h2>
      <div className="feed">
        {events.length === 0 && (
          <div className="empty">
            Waiting for activity. Run <code>agentloop run</code> to see the
            loop move.
          </div>
        )}
        {events.map((ev) => (
          <div className="ev" key={ev.id}>
            <span className="t">{clock(ev.ts)}</span>
            <span className={`k k-${ev.kind}`}>
              {ev.task_id ? `#${ev.task_id} ` : ''}
              {ev.kind}
            </span>
            <span className="p">{digest(ev)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
