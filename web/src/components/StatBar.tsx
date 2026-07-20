import type { RunMetrics } from '../types'

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const m = Math.floor(seconds / 60)
  return `${m}m ${Math.round(seconds % 60)}s`
}

export function StatBar({ metrics }: { metrics: RunMetrics | null }) {
  if (!metrics) return null
  const byStatus = metrics.tasks_by_status
  const done = byStatus.done ?? 0
  const needsHuman = byStatus.needs_human ?? 0
  const total = Object.values(byStatus).reduce((a, b) => a + (b ?? 0), 0)

  return (
    <div className="stats">
      <div className="stat">
        <div className="label">Cost</div>
        <div className="value">${metrics.cost_usd.toFixed(4)}</div>
        <div className="sub">{metrics.attempts} agent calls</div>
      </div>
      <div className="stat">
        <div className="label">Tokens</div>
        <div className="value">{fmtTokens(metrics.tokens)}</div>
        <div className="sub">
          {fmtTokens(metrics.tokens_in)} in / {fmtTokens(metrics.tokens_out)} out
        </div>
      </div>
      <div className="stat">
        <div className="label">Agent time</div>
        <div className="value">{fmtDuration(metrics.wall_seconds)}</div>
        <div className="sub">{metrics.revisions} revisions</div>
      </div>
      <div className="stat">
        <div className="label">Tasks done</div>
        <div className="value">
          {done}
          <span style={{ color: 'var(--muted)', fontSize: 14 }}>/{total}</span>
        </div>
        <div className="sub">
          {needsHuman > 0 ? `${needsHuman} awaiting you` : 'none blocked'}
        </div>
      </div>
    </div>
  )
}
