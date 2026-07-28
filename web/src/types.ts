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

export interface VerdictRow {
  kind: VerdictKind
  confidence: number
  tests_passed: number | null
}

export interface TaskMetrics {
  tokens: number
  cost_usd: number
  attempts: number
  wall_seconds: number
  verdicts: VerdictRow[]
}

export interface TestRun {
  id: number
  task_id: number
  status: TestStatus
  exit_code: number | null
  summary: string
  stdout_tail: string
  duration_s: number
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
  hit_count: number
  approved: number
  pinned: number
  created_at: number
  // Null until the fact is first read out: hit_count says how often, never how
  // recently.
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
}
