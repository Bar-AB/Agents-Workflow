import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Project } from '../types'

const STORAGE_KEY = 'agentloop.selectedProjectId'

// Reads and writes the "which project am I viewing" choice. Wrapped in
// try/catch throughout: this is a per-viewer convenience, not a source of
// truth (the actual filtering is server-side, per query param), so a
// private-mode browser or a blocked-storage setting must degrade to "no
// remembered choice" rather than crash the header.
//
// Three states, not two: `undefined` (nothing stored yet — key absent) is
// different from `null` (stored, and it was an explicit choice of "All
// projects"). Collapsing those onto one sentinel (missing key means both
// "never chosen" and "All") would make choosing "All projects" not stick —
// the next reload would read the same missing key, re-resolve the default
// project, and silently override the choice. Storing an `'all'` string for
// the explicit choice is what keeps the two apart.
function readStored(): number | null | undefined {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === null) return undefined
    if (raw === 'all') return null
    const n = Number(raw)
    return Number.isInteger(n) ? n : undefined
  } catch {
    return undefined
  }
}

function writeStored(id: number | null) {
  try {
    window.localStorage.setItem(STORAGE_KEY, id === null ? 'all' : String(id))
  } catch {
    /* per-viewer convenience only; nothing downstream depends on this */
  }
}

// A dropdown in the header. `projectId` (`null` = every project) is owned by
// `App`, not this component — this only lists projects, persists the choice,
// and calls back up. The actual scoping happens wherever `projectId` is
// threaded into `useLiveLoop`/`api.*`, never here.
export function ProjectSwitcher({
  projectId,
  onChange,
}: {
  projectId: number | null
  onChange: (id: number | null) => void
}) {
  const [projects, setProjects] = useState<Project[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .projects()
      .then((ps) => {
        if (!cancelled) setProjects(ps)
      })
      .catch((e) => !cancelled && setError(String(e)))
    return () => {
      cancelled = true
    }
    // Re-listed only on mount: a project add/rename/archive elsewhere in the
    // app does not currently push a refresh into this component (no event
    // maps to "project list changed" the way tasks/memory/tools already
    // stream). Acceptable for this phase — the switcher lists what existed
    // when it mounted, same as `AgentPanel`'s registry snapshot.
  }, [])

  const select = (raw: string) => {
    const id = raw === '' ? null : Number(raw)
    writeStored(id)
    onChange(id)
  }

  if (error) return <div className="banner err">{error}</div>

  return (
    <select
      className="project-switcher"
      value={projectId === null ? '' : String(projectId)}
      onChange={(e) => select(e.target.value)}
      title="Which project's tasks, memory and events to show"
    >
      <option value="">All projects</option>
      {projects
        .filter((p) => !p.archived || p.id === projectId)
        .map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
            {p.is_default ? ' (default)' : ''}
            {p.archived ? ' [archived]' : ''}
          </option>
        ))}
    </select>
  )
}

export { readStored as readStoredProjectId }
