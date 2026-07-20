// Mirrors agentloop/models.py and the server's JSON shapes. Kept in one file
// so a backend change has exactly one place to land on the frontend.

export type TaskStatus =
  | 'pending'
  | 'in_progress'
  | 'testing'
  | 'validating'
  | 'revising'
  | 'needs_human'
  | 'done'
  | 'failed'

export type VerdictKind = 'approve' | 'revise' | 'escalate'
export type TestStatus = 'pass' | 'fail' | 'na' | 'error'

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
  created_at: number
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
