import { apiFetch } from './client'

export type EventAgentStatus = 'queued' | 'running' | 'ready' | 'completed' | 'degraded' | 'cancelled' | 'interrupted' | 'failed'

export interface EventAgentUsage {
  model_calls?: number
  tool_calls?: number
  extra_frames?: number
  windows?: number
  elapsed_seconds?: number
  request_count?: number
  prompt_tokens?: number | null
  completion_tokens?: number | null
  total_tokens?: number | null
  estimated_list_price_cny?: number | null
  cost_estimate_complete?: boolean
  unknown_cost_count?: number
}

export interface EventAgentRun {
  run_id: string
  job_id: string
  status: EventAgentStatus
  stage: string
  progress: number
  provider?: string
  model?: string
  created_at: string
  updated_at?: string
  started_at?: string | null
  completed_at?: string | null
  cancel_requested?: boolean
  error?: string | null
  warnings: string[]
  usage_summary?: EventAgentUsage
  checkpoint_count?: number
  window_total?: number
  window_completed?: number
  has_result: boolean
  checkpoints?: EventAgentWindowAudit[]
  audit?: EventAgentWindowAudit[]
}

export interface EventAgentEvidence {
  frame_id: string
  timestamp: number
  description: string
  bbox?: number[] | null
  crop_artifact_id?: string | null
}
export interface EventAgentPresentation {
  source_shot_id: string
  disposition: 'preserve' | 'integrate' | 'omit'
  reason: string
  evidence: EventAgentEvidence[]
}
export interface EventAgentFact {
  id: string
  text: string
  source_shot_ids: string[]
  evidence: EventAgentEvidence[]
  critical: boolean
  kind: string
}
export interface EventAgentEvent {
  id: string
  anchor_shot_id: string
  title: string
  goal: string
  source_shot_ids: string[]
  initial_state: string
  final_state: string
  causal_facts: EventAgentFact[]
  presentation: EventAgentPresentation[]
  generation_mode: 'continuous' | 'internal_cuts'
  confidence: number
  status: 'ready' | 'fallback'
  window_id: string
  warnings: string[]
}
export interface EventAgentPrompt {
  id: string
  prompt_type: 'first_frame' | 'video'
  platform: string
  positive_prompt: string
  negative_prompt: string
  status: string
}
export interface EventAgentClip {
  id: string
  event_id: string
  title: string
  source_shot_ids: string[]
  presentation: EventAgentPresentation[]
  source_start_time: number
  source_end_time: number
  source_duration_seconds: number
  generation_duration_seconds: number
  generation_mode: 'continuous' | 'internal_cuts'
  timeline: { id: string; source_shot_ids: string[]; fact_ids: string[]; generation_start_seconds: number; generation_end_seconds: number; transition: 'initial' | 'continuation' | 'cut'; action: string }[]
  first_frame_prompt: EventAgentPrompt
  seedance_prompt: EventAgentPrompt
  status: string
  warnings: string[]
}
export interface EventAgentWindow {
  id: string
  start_time: number
  end_time: number
  source_shot_ids: string[]
  context_shot_ids: string[]
}
export interface EventAgentWindowAudit {
  window_id: string
  source_shot_ids: string[]
  context_shot_ids: string[]
  status: 'ready' | 'fallback' | 'cancelled'
  validation_errors: string[]
  warnings: string[]
  usage: EventAgentUsage
  tool_results: { name: string; status: string; error?: string | null }[]
}
export interface EventAgentResult {
  schema_version: 'event-agent.v1'
  job_id: string
  run_id: string
  status: 'ready' | 'degraded' | 'cancelled'
  windows: EventAgentWindow[]
  events: EventAgentEvent[]
  generation_clips: EventAgentClip[]
  audit: EventAgentWindowAudit[]
  warnings: string[]
  usage_summary: EventAgentUsage
  source_coverage: { source_shot_id: string; event_id: string }[]
}

const runsPath = (jobId: string) => `/jobs/${encodeURIComponent(jobId)}/event-agent/runs`
const runPath = (jobId: string, runId: string) => `${runsPath(jobId)}/${encodeURIComponent(runId)}`

export const listEventAgentRuns = (jobId: string, signal?: AbortSignal) => apiFetch<{ job_id: string; runs: EventAgentRun[] }>(runsPath(jobId), { signal })
export const getEventAgentRun = (jobId: string, runId: string, signal?: AbortSignal) => apiFetch<EventAgentRun>(runPath(jobId, runId), { signal })
export const getEventAgentResult = (jobId: string, runId: string, signal?: AbortSignal) => apiFetch<EventAgentResult>(`${runPath(jobId, runId)}/result`, { signal })
export const createEventAgentRun = (jobId: string, signal?: AbortSignal) => apiFetch<EventAgentRun>(runsPath(jobId), {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: {} }), signal,
})
export const cancelEventAgentRun = (jobId: string, runId: string, signal?: AbortSignal) => apiFetch<EventAgentRun>(`${runPath(jobId, runId)}/cancel`, { method: 'POST', signal })
