import { useEffect, useState } from 'react'
import { api } from '../api'
import type { LoopConfigView } from '../types'

// slice 9: view/set which repository task workspaces come from. This is
// config, not a live loop knob — a save here only takes effect the next time
// `agentloop serve`/`run` starts, never the process currently answering this
// request, so a successful save must say that rather than imply it applied.
export function RepoConfigPanel() {
  const [config, setConfig] = useState<LoopConfigView | null>(null)
  const [repoRoot, setRepoRoot] = useState('')
  const [workspaceMode, setWorkspaceMode] = useState('scratch')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .config()
      .then((c) => {
        if (cancelled) return
        setConfig(c)
        setRepoRoot(c.repo_root)
        setWorkspaceMode(c.workspace_mode)
      })
      .catch((e) => !cancelled && setError(String(e)))
    return () => {
      cancelled = true
    }
  }, [])

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const next = await api.setRepoConfig(repoRoot, workspaceMode)
      setConfig((c) => (c ? { ...c, ...next } : c))
      setSaved(true)
    } catch (e) {
      // A 400 is a validation failure (relative path, or not an existing
      // directory) — shown here, never thrown past this panel.
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!config) return <div className="panel empty">Loading repo config…</div>

  const dirty =
    repoRoot !== config.repo_root || workspaceMode !== config.workspace_mode

  return (
    <div className="panel">
      <h2>Repository workspace</h2>

      <div className="empty" style={{ marginBottom: 8 }}>
        Which repository task workspaces are checked out from, and whether
        each task gets a throwaway scratch directory or a real{' '}
        <code>git worktree</code> checkout of it.
      </div>

      <div className="fact" style={{ marginBottom: 8 }}>
        <div className="head">
          <span className="key">Currently configured</span>
        </div>
        <div className="val">repo_root: {config.repo_root || '(none)'}</div>
        <div className="val">workspace_mode: {config.workspace_mode}</div>
      </div>

      {error && <div className="banner err">{error}</div>}

      {saved && (
        <div className="banner">
          Saved. This takes effect only after <code>agentloop serve</code> (or
          <code> agentloop run</code>) is restarted — the running server has
          not picked this up.
        </div>
      )}

      <label style={{ display: 'block', marginTop: 6 }}>
        repo_root (absolute path)
        <input
          value={repoRoot}
          onChange={(e) => setRepoRoot(e.target.value)}
          placeholder="/absolute/path/to/repo"
          style={{ width: '100%' }}
        />
      </label>

      <label style={{ display: 'block', marginTop: 6 }}>
        workspace_mode
        <select
          value={workspaceMode}
          onChange={(e) => setWorkspaceMode(e.target.value)}
          style={{ width: '100%' }}
        >
          <option value="scratch">scratch</option>
          <option value="worktree">worktree</option>
        </select>
      </label>

      <div className="actions" style={{ marginTop: 6 }}>
        <button className="primary" disabled={busy || !dirty} onClick={save}>
          Save
        </button>
      </div>
    </div>
  )
}
