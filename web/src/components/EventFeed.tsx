import type {
  EventRow,
  RetrievalPayload,
  VcsCommitPayload,
  VcsRollbackPayload,
  VcsUnavailablePayload,
} from '../types'

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
      return `${p.key} → loop (${p.hit_count} tasks)`
    // Not a promotion: duplicate project/loop rows left by the pre-transition
    // build, collapsed once when an older database is opened.
    case 'memory_duplicates_merged':
      return `${p.key}: duplicate rows merged on upgrade`
    case 'memory_hit_counts_reset':
      return `${p.rows} fact(s) reset — old counts were prompts, not tasks`
    case 'memory_write':
      return `${p.tier}/${p.key}${p.approved ? '' : ' (pending)'}${p.pinned ? ' 📌' : ''}`
    case 'memory_pinned':
    case 'memory_unpinned':
      return `${p.tier}/${p.key}`
    case 'retrieval': {
      // Provenance: which facts memory put in front of which agent, best first.
      // A task retrieves once per agent per round, so the agent is what makes
      // the rows tellable apart.
      const r = ev.payload as unknown as RetrievalPayload
      const top = (r.facts ?? [])
        .slice(0, 3)
        .map((f) => `${f.key} ${Number(f.score).toFixed(2)}`)
        .join(', ')
      return `${r.agent_kind} · ${r.backend}: ${r.n_selected}/${r.n_candidates} facts — ${top}`
    }
    case 'tool_call':
      return `${p.agent_kind} · ${p.tool}`
    case 'eval_run':
      // `kind` discriminates validator calibration from a whole-loop batch run:
      // both write this row, and "agreement" means a different measurement
      // under each, so the number is unreadable without it.
      return `${p.kind ?? 'verdict'} · ${p.runner}: ${p.n_fixtures} fixtures, agreement ${p.agreement}`
    // The three slice-6 durability events. Each renders **only** the fields its
    // payload actually carries: the optional keys are absent, not null, when
    // they do not apply, so an unconditional render would assert a fact the
    // event does not prove.
    case 'vcs_commit': {
      const c = ev.payload as unknown as VcsCommitPayload
      // A round snapshot carries `round`; the approved-ref move carries `ref`.
      const what = c.ref ?? (c.round !== undefined ? `round ${c.round}` : '')
      const sha = c.sha ? c.sha.slice(0, 7) : ''
      return [sha, what].filter(Boolean).join(' · ')
    }
    case 'vcs_rollback': {
      const r = ev.payload as unknown as VcsRollbackPayload
      const parts = [`→ ${r.ref} · ${r.files_removed} file(s) removed`]
      // Where the discarded round survives — omitted entirely when HEAD was
      // already at the target and nothing was discarded.
      if (r.discarded_sha) parts.push(`kept ${r.discarded_sha.slice(0, 7)}`)
      // `degraded` on an ok result means the rollback ran and something
      // survived it; saying nothing would report a partial as clean.
      if (r.degraded) parts.push(`degraded: ${r.degraded}`)
      // Named, never softened: a nested repo's objects are recorded as a bare
      // gitlink, so the discarded ref does NOT hold them and there is no
      // recovery surface to point at.
      if (r.unrecoverable_nested_repos?.length)
        parts.push(
          `unrecoverable nested repo(s): ${r.unrecoverable_nested_repos.join(', ')}`,
        )
      return parts.join(' · ')
    }
    case 'vcs_unavailable': {
      const u = ev.payload as unknown as VcsUnavailablePayload
      const parts = [`${u.op}: ${u.reason}`]
      // Only a failed rollback reports this, and the two values mean opposite
      // things to whoever is deciding whether to wipe the workspace.
      if (u.history_preserved !== undefined)
        parts.push(
          u.history_preserved
            ? `history kept${u.discarded_sha ? ` at ${u.discarded_sha.slice(0, 7)}` : ''}`
            : 'no history recorded',
        )
      return parts.join(' · ')
    }
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
