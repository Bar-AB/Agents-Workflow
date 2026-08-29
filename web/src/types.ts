// Mirrors agentloop/models.py and the server's JSON shapes. Kept in one file
// so a backend change has exactly one place to land on the frontend.

export type TaskStatus =
  | 'pending'
  | 'in_progress'
  | 'testing'
  | 'validating'
  | 'revising'
  | 'needs_human'
  | 'paused'
  | 'done'
  | 'failed'
  | 'aborted'

export type TaskControl = 'run' | 'pause' | 'abort'

export type VerdictKind = 'approve' | 'revise' | 'escalate'
export type TestStatus = 'pass' | 'fail' | 'na' | 'error'

// 'task' = work a worker executes. 'plan' = the container row a planner run
// decomposed; it is never claimed by the loop and carries the plan's approval.
export type TaskKind = 'task' | 'plan'

export interface Task {
  id: number
  title: string
  goal: string
  acceptance_criteria: string
  status: TaskStatus
  risk_level: number
  revision_count: number
  worker_role: string
  validator_role: string
  output: string
  escalation_reason: string
  control: TaskControl
  kind: TaskKind
  // Which plan produced this task; null for hand-defined ones.
  plan_id: number | null
  // Task ids this one waits on. A pending task with an unfinished dependency is
  // not stuck — it is simply not claimable yet.
  depends_on: number[]
  // Only meaningful on a plan row (null elsewhere): until a human signs the plan
  // off, none of its tasks are claimable.
  plan_approved: boolean | null
}

// Mirrors models.ToolRequestStatus. `auto` and `approved` *are* the grant —
// there is no separate grant object. `refused` is machine-made (an unknown
// logical name, or the per-task cap) and never a human's to decide.
export type ToolRequestStatus =
  | 'auto'
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'refused'

// Who asked. A `declared` request is the role's registry list gated at invoke
// time, so the agent never asked for it and it is never blocking.
export type ToolRequestSource = 'marker' | 'declared'

// Mirrors models.ToolRequest plus the two fields server.py derives per read.
export interface ToolRequest {
  id: number
  task_id: number
  attempt_id: number | null
  role: string
  // Which agent asked, as the loop's own literal. NOT the same field as `role`:
  // a custom worker_role makes the two differ.
  agent_kind: string
  tool: string
  reason: string
  // What the agent asked for: blocking means "I cannot finish without this".
  blocking: boolean
  // What the loop did about it: this request is holding its task at
  // needs_human right now. A live fact, cleared by every exit from the park.
  parked: boolean
  source: ToolRequestSource
  status: ToolRequestStatus
  decided_by: string
  decided_note: string
  // REAL columns, so numbers — not date strings.
  created_at: number
  decided_at: number | null
  // Derived by the server from runner.LOGICAL_TOOL_MAP, never stored: what this
  // logical name actually confers...
  resolved: string[]
  // ...and the other logical names that share those concrete tools. The map is
  // not injective, so deciding `shell` also decides `git`. Note this is a
  // statement about the *map* only: whether that sharing actually costs or gains
  // this row's role anything depends on the role's declared list and on its
  // sibling rows' statuses, which the map knows nothing about. That is what
  // `effect` is for — do not render a consequence from `also_decides` alone.
  also_decides: Record<string, string[]>
  // Why `resolved` may be empty, which has two causes the panel must not
  // conflate: `task_state` is genuinely served in-process, while a name outside
  // LOGICAL_TOOL_MAP (the whole `refused` population) is not a tool at all.
  known: boolean
  // The computed consequence of deciding this row, from toolpolicy.decision_effect
  // — the same function the gate itself enforces.
  effect: ToolRequestEffect
}

// Every field is a difference between two evaluations of the effective tool list,
// so none of them can promise something the gate would not do. All three states
// that falsified the old `also_decides`-derived text are one of these being empty:
// approving that grants nothing, and rejecting that costs nothing (either because
// a pending row already withholds the sibling, or because this role never
// declared it).
export interface ToolRequestEffect {
  // Is this row's own tool available to the role right now? An `approved` row
  // whose capability another denial still subtracts is not in force.
  //
  // This is a **logical-name** membership test, so no sentence about the
  // *concrete* capability may be branched on it. The two diverge on the whole
  // `refused` population: such a row subtracts nothing, yet its logical name is
  // absent from the enforced list — and the panel read "Bash is not available to
  // this role" off this flag while the gate handed `Bash` over.
  in_effect: boolean
  // The concrete capability of this row's own tool that the role really has right
  // now. Empty is the only honest basis for calling a capability unavailable.
  capability_live: string[]
  // Its complement over the same footprint: this row's own concrete capability the
  // role does not have. Served because it is the *subject* of the sentence a human
  // reads as a closed gate, and the gate's coarse subtraction makes a partial split
  // reachable — so it may not be re-derived from `resolved` on each surface.
  capability_missing: string[]
  // The headline claim, computed server-side by `toolpolicy._verb` out of these
  // same fields and rendered verbatim above the consequence. Never re-derived
  // here from `status`: a verb keyed on the status alone contradicted the body two
  // lines below it in three reachable states, and `web/` has no test runner, so a
  // claim decided in this file is the one permission-screen assertion no gate
  // covers. It reads directly into the row's `resolved` chip ("would grant" Bash).
  verb: string
  // Other logical names not working *because* this row stands as it does.
  costs_now: string[]
  // Concrete capability approving would add...
  approve_grants: string[]
  // ...and the other logical names that would start working again with it.
  approve_enables: string[]
  // Other logical names rejecting would stop working...
  reject_removes: string[]
  // ...and the concrete capability that would go with them.
  reject_loses: string[]
}

export interface VerdictRow {
  kind: VerdictKind
  confidence: number
  tests_passed: number | null
  // What the validator says it checked and what it found. Evidence, not a gate:
  // nothing in the loop reads it, and an empty section is a legitimate clean
  // review rather than a reason to revise.
  findings: string
}

export interface TaskMetrics {
  tokens: number
  cost_usd: number
  attempts: number
  wall_seconds: number
  verdicts: VerdictRow[]
  // Charter versions this task's agents actually ran under — so "was this
  // approved under the old rules" is answerable next to the approve button.
  // Empty when no charter was in effect.
  charter_versions: number[]
  // Every tool request on this task, oldest first — raw rows, as task_metrics
  // serves them.
  tool_requests: Record<string, unknown>[]
}

// One version of the project charter. The table is append-only and `id` *is*
// the version, so a past attempt's recorded version stays readable forever.
export interface CharterVersion {
  id: number
  body: string
  note: string
  created_at: number
}

export interface CharterView {
  // Null when the charter was never set or has been explicitly cleared — the
  // two are deliberately indistinguishable.
  active: CharterVersion | null
  history: CharterVersion[]
}

export interface TestRun {
  id: number
  task_id: number
  status: TestStatus
  exit_code: number | null
  summary: string
  stdout_tail: string
  duration_s: number
  // Null when the test command reported no coverage at all — not 0%.
  coverage_percent: number | null
  created_at: number
}

export interface EventRow {
  id: number
  task_id: number | null
  ts: number
  kind: string
  payload: Record<string, unknown>
}

// Payload of a `retrieval` event: what memory put in front of one agent, on one
// attempt, and why. Mirrors MemoryService._provenance plus the attribution
// agents._invoke adds — attempt_id/agent_kind/role, exactly as `tool_call`
// carries, since a task retrieves once per agent per round.
export interface RetrievalPayload {
  attempt_id: number
  agent_kind: string
  role: string
  query: string
  backend: string
  n_candidates: number
  n_selected: number
  facts: {
    id: number
    tier: string
    key: string
    pinned: boolean
    score: number
  }[]
}

// Payloads of the three slice-6 durability events. Mirrors what `loop.py`
// actually writes, and the optionality is the load-bearing part: the optional
// keys are **absent**, not null, when they do not apply, so every one is
// declared `?` and must be tested for before it is rendered. A field rendered
// as present when it is absent would assert a recovery surface that does not
// exist — the same "never assert more than the inputs prove" rule the tool
// panel's chip colour is governed by.
export interface VcsCommitPayload {
  sha: string
  // A round snapshot carries `round`; the approved-ref move carries
  // `ref: 'approved'`. Exactly one of the two is present.
  round?: number
  ref?: string
}

export interface VcsRollbackPayload {
  ref: string
  files_removed: number
  // Present only when something was actually discarded (HEAD was not already at
  // the target). Together they name where the rolled-back round survives.
  discarded_sha?: string
  discarded_ref?: string
  // A non-empty `reason` on an ok result: the rollback ran and degraded — e.g.
  // `'residue'`, meaning files survived it.
  degraded?: string
  // Directories that held a repository of their own. A commit records those as
  // bare gitlinks, so their objects are NOT in the discarded ref: naming them
  // is the whole point, and the UI must not imply they are recoverable.
  unrecoverable_nested_repos?: string[]
}

export interface VcsUnavailablePayload {
  op: string
  reason: string
  stderr: string
  // Set only on a failed rollback: whether the discarded tip was recorded
  // before the failure.
  history_preserved?: boolean
  discarded_sha?: string
}

export interface ModelRollup {
  model: string
  attempts: number
  tokens_in: number
  tokens_out: number
  cost_usd: number
}

export interface RunMetrics {
  tokens_in: number
  tokens_out: number
  cache_creation_tokens: number
  cache_read_tokens: number
  tokens: number
  cost_usd: number
  attempts: number
  wall_seconds: number
  revisions: number
  // Run-wide count of requests awaiting a human: the queue is only useful if it
  // is visible without opening a task.
  pending_tool_requests: number
  tasks_by_status: Partial<Record<TaskStatus, number>>
  by_model: ModelRollup[]
}

export interface Agent {
  role: string
  model: string
  tools: string[]
  context_budget_tokens: number
  version: string
}

export interface MemoryFact {
  id: number
  tier: 'project' | 'loop'
  key: string
  value: string
  // Distinct *tasks* this fact was ranked into a prompt for, not prompts: one
  // task's worker, validator and revision are three injections and one hit.
  hit_count: number
  approved: number
  pinned: number
  created_at: number
  // Null until the fact is first read out: hit_count says how many tasks, never
  // how recently.
  last_used_at: number | null
}

export interface TaskDetail {
  task: Task
  metrics: TaskMetrics
  test_runs: TestRun[]
  events: EventRow[]
}

export interface LoopConfigView {
  approve_threshold: number
  severe_threshold: number
  max_revisions: number
  max_tokens_per_task: number
  max_cost_usd_per_task: number
  human_review_risk_level: number
  test_command: string
  // Why a request is gated rather than merely that it is: a tool outside this
  // allowlist is side-effecting and needs a human.
  tool_readonly_allowlist: string[]
  gate_declared_tools: boolean
}
