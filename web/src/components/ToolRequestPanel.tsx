// The tool queue: the one screen the whole slice exists for.
//
// Everything behind it — the ledger, the policy seam, the park, the release
// predicate — is machinery for a single human decision: approve or deny a tool
// an agent asked for. Two things this panel has to get right, or the machinery
// is wasted:
//
// 1. The consequence is visible *before* the decision — and it has to be the
//    consequence that will actually happen. `LOGICAL_TOOL_MAP` is not injective
//    and the gate enforces the *concrete* list, so a decision on `shell` lands
//    on `git` too. But the sharing map alone cannot say whether that *matters
//    here*: the real effect also depends on the row's **role** and on its
//    **sibling rows' statuses**. This panel used to render "approving grants it
//    too; rejecting stops it working" from the map alone, and three reachable
//    states falsify it — a rejected sibling means approving grants nothing at
//    all; a *pending* row has already withheld `git`, so rejecting takes nothing
//    away and approving is what restores it; and on a role that never declared
//    `git` a denial costs nothing. The last is the worst direction: an invented
//    cost attached to *denial* pushes a human toward granting.
//    So the text comes from `request.effect`, computed server-side by
//    `toolpolicy.decision_effect` out of the same `effective_tools` the gate
//    enforces. `also_decides` stays, as the statement of sharing it truthfully
//    is — never as a consequence.
// 2. A logical name understates what it grants: `git` confers `Bash`. So the
//    resolved concrete tools are rendered beside the label, not instead of it.
//    An *empty* resolved list has two causes and the panel must not conflate
//    them: `task_state` really is served in-process, while a name outside the
//    map is not a tool at all.
//
// All of it comes from the server (`resolved`, `also_decides`, `known`,
// `effect`), derived on each read. Mirroring that table or that computation here
// would be a second source of truth that drifts from the gate.
//
// Modeled on MemoryPanel deliberately — same gating shape, same classes, no new
// abstraction. Roadmap slice 7 reworks the dashboard; this is not the place to
// invent a pattern.

import { useState } from 'react'
import { api } from '../api'
import type { ToolRequest } from '../types'

const list = (names: string[]) =>
  names.length > 1
    ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
    : names.join('')

// The verb used to be keyed on `status` alone, which is what a human *did* and
// cannot say what the gate *will do*. It therefore contradicted the body two lines
// under it in three reachable states: `would grant [Bash]` over "Approving does not
// deliver Bash", `grants [Bash]` over "NOT in force", and `denied [Bash]` over a
// live capability. It now comes from `effect.verb`, computed in
// `toolpolicy._verb` — because `web/` has no test runner, a claim decided here is
// the one assertion on this permission screen that no gate covers.
const verb = (names: string[], singular: string, plural: string) =>
  names.length > 1 ? plural : singular

// Every branch below is a statement about `effect`, whose fields are differences
// between two evaluations of the enforced list. Nothing here reasons from
// `also_decides`, which cannot know the role or the siblings.
function outcome(request: ToolRequest): string {
  const e = request.effect
  const has = request.resolved.length > 0
  if (request.status === 'pending') {
    let approving: string
    if (e.approve_grants.length > 0) {
      approving = `Approving grants ${list(e.approve_grants)}`
      if (e.approve_enables.length > 0) {
        // "git and web starts working again": the subject is the list, not the
        // gerund, so this one has to agree with it.
        approving += `, and ${list(e.approve_enables)} ${verb(
          e.approve_enables,
          'starts',
          'start',
        )} working again`
      }
    } else if (e.capability_live.length > 0) {
      approving = `Approving changes nothing — this role already has ${list(
        e.capability_live,
      )}`
    } else if (!has) {
      approving = 'Approving grants no capability — this name confers none'
    } else {
      // approve_grants empty, not already held, and the name does confer
      // something: the only remaining reason is that another withheld request on
      // this task confers the same concrete tool and still subtracts it.
      approving = `Approving does not deliver ${list(
        request.resolved,
      )} — another withheld request on this task confers it too`
    }
    let rejecting: string
    if (e.reject_removes.length > 0) {
      rejecting = `rejecting also stops ${list(e.reject_removes)} working`
    } else if (e.reject_loses.length > 0) {
      rejecting = `rejecting withdraws ${list(e.reject_loses)}`
    } else {
      rejecting = 'rejecting takes nothing further away'
    }
    const already =
      e.costs_now.length > 0
        ? ` ${list(e.costs_now)} ${verb(
            e.costs_now,
            'is',
            'are',
          )} already withheld while this request stands.`
        : ''
    return `${approving}; ${rejecting}.${already}`
  }
  if (request.status === 'approved' || request.status === 'auto') {
    if (e.in_effect) {
      return `In force${has ? `: the role has ${list(request.resolved)}.` : '.'}`
    }
    // An empty `resolved` is not a withheld capability, it is no capability at
    // all: a name in `tool_readonly_allowlist` that `LOGICAL_TOOL_MAP` lacks is
    // an `auto` row conferring nothing, and this branch rendered it as
    // `NOT in force:  is withheld on account of another request on this task` —
    // an empty list and a withholding that never happened, in one sentence.
    if (!has) return 'Confers no capability — nothing to be in force.'
    // "another *request*", not "another decision": a sibling that subtracts the
    // capability may be `pending`, which is nobody's decision.
    return `NOT in force: ${list(
      request.resolved,
    )} is withheld on account of another request on this task.`
  }
  if (!has) return 'Nothing to withhold — this name confers no capability.'
  // `capability_live`, never `in_effect`: this sentence is about the *concrete*
  // capability, and for a `refused` row the two disagree — the row subtracts
  // nothing while its logical name is still absent from the enforced list. Keyed
  // on the logical test, this told a human "Bash is not available to this role"
  // about a `Bash` the gate was handing to the runner through the worker's own
  // declared `git`, under a header reading `denied [Bash]`.
  // Served, not derived here: the sentence's subject is a computed field, and a
  // partial split is reachable (a `refused` `file_io` on a role holding
  // `file_read` keeps `Read` and loses `Write` and `Edit`).
  const missing = e.capability_missing
  if (missing.length === 0) {
    return `No effect: the role has ${list(e.capability_live)} regardless.`
  }
  const still =
    e.capability_live.length > 0
      ? ` It still has ${list(e.capability_live)}.`
      : e.costs_now.length > 0
        ? ` ${list(e.costs_now)} ${verb(
            e.costs_now,
            'is',
            'are',
          )} withheld with it — it shares that capability.`
        : ''
  // "not available", not "withheld by this": a `refused` row (an unknown name, or
  // the per-task cap) subtracts nothing at all, and `withheld_tools` never sees
  // it — so this states the outcome without claiming this row caused it.
  return `${list(missing)} ${verb(
    missing,
    'is',
    'are',
  )} not available to this role.${still}`
}

function Consequence({ request }: { request: ToolRequest }) {
  const shared = Object.entries(request.also_decides)
  return (
    <>
      <div className="val">
        {request.effect.verb}{' '}
        <span className="tools" style={{ display: 'inline-flex' }}>
          {request.resolved.length === 0 ? (
            <span className="tool">
              {request.known
                ? 'nothing (served in-process)'
                : 'nothing (not a known tool name)'}
            </span>
          ) : (
            request.resolved.map((t) => (
              <span className="tool" key={t}>
                {t}
              </span>
            ))
          )}
        </span>
      </div>
      <div className="consequence">
        {shared.length > 0 && (
          <>
            Shares its capability with{' '}
            {shared.map(([name, concrete]) => (
              <span key={name}>
                <code>{name}</code> ({concrete.join(', ')}){' '}
              </span>
            ))}
            —{' '}
          </>
        )}
        {outcome(request)}
      </div>
    </>
  )
}

// Pending first (they need a decision), and a parked one ahead of those,
// because a parked request is holding its task at needs_human right now.
export const sortRequests = (requests: ToolRequest[]): ToolRequest[] =>
  [...requests].sort(
    (a, b) =>
      Number(a.status !== 'pending') - Number(b.status !== 'pending') ||
      Number(b.parked) - Number(a.parked) ||
      a.id - b.id,
  )

// One row, rendered identically wherever a request is shown. Exported because
// `TaskDetail` shows a parked task's own requests inline, and this slice's whole
// lesson is that a second copy of a permission-surface claim drifts from the
// first: the consequence text, the green-chip discipline and the
// buttons-only-when-pending rule are decided **here**, once.
export function ToolRequestRow({
  request: r,
  busy,
  onDecide,
}: {
  request: ToolRequest
  busy: boolean
  onDecide: (id: number, action: 'approve' | 'reject') => void
}) {
  return (
    <div className="fact">
      <div className="head">
        {/* Green is reserved for a grant that is **live**, not for a grant that
            was merely *made*. The status alone is not enough: an `approved` row
            whose capability another withheld request still subtracts renders
            "NOT in force" in the body, and a green chip over that sentence is a
            claim contradicting the sentence under it — the same defect as the old
            status-keyed verb, one element to the left, and the last surviving
            place on this screen where a colour asserted something the gate does
            not do. `in_effect` rather than `capability_live`: a granted
            in-process tool (`task_state`) confers no concrete capability and is
            still genuinely in force, so keying on the concrete list would strip
            the green off a real grant. */}
        <span
          className={`tier ${
            (r.status === 'approved' || r.status === 'auto') && r.effect.in_effect
              ? 'loop'
              : ''
          }`}
        >
          {r.status}
        </span>
        <span className="key">{r.tool}</span>
        <span className="pending-flag">
          {r.blocking ? 'blocking' : 'optional'}
          {r.parked ? ' · PARKED' : ''}
        </span>
      </div>
      <Consequence request={r} />
      {r.reason && <div className="val">“{r.reason}”</div>}
      <div className="val">
        task {r.task_id} · asked by {r.agent_kind}/{r.role} · {r.source}
        {r.decided_by && ` · decided by ${r.decided_by}`}
        {r.decided_note && `: ${r.decided_note}`}
      </div>
      {r.status === 'pending' && (
        <div className="actions" style={{ marginTop: 6 }}>
          <button
            className="primary"
            disabled={busy}
            onClick={() => onDecide(r.id, 'approve')}
          >
            Approve
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() => onDecide(r.id, 'reject')}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  )
}

export function ToolRequestPanel({
  requests,
  onChanged,
}: {
  requests: ToolRequest[]
  onChanged: (next: ToolRequest[]) => void
}) {
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const decide = async (id: number, action: 'approve' | 'reject') => {
    setBusy(id)
    setError(null)
    try {
      onChanged(await api.decideToolRequest(id, action))
    } catch (e) {
      // A decided row is final and the server says so with a 400. Two humans,
      // or one double-click: the store picks the winner and the loser must be
      // told, not silently shown a success.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const sorted = sortRequests(requests)
  const pending = requests.filter((r) => r.status === 'pending').length

  return (
    <div className="panel">
      <h2>
        Tool requests{' '}
        {pending > 0 && <span className="pending-flag">· {pending} pending</span>}
      </h2>
      {error && <div className="banner err">{error}</div>}
      {sorted.length === 0 && (
        <div className="empty">
          No requests. Agents ask with a <code>TOOL_REQUEST:</code> line;
          read-only tools are auto-approved and never appear here as pending.
        </div>
      )}
      {sorted.map((r) => (
        <ToolRequestRow
          key={r.id}
          request={r}
          busy={busy === r.id}
          onDecide={decide}
        />
      ))}
    </div>
  )
}
