import { apiFetch } from './client'

export type PerceptionStageName = 'static' | 'motion'
export type PerceptionFieldGroup = 'action' | 'subject' | 'scene' | 'spatial' | 'expression'
export interface PerceptionUsage {
  estimated_list_price_cny?: number | null
  cost_estimate_complete?: boolean
  total_tokens?: number | null
}
export interface PerceptionStage {
  state: 'succeeded' | 'partial' | 'failed' | 'deferred'
  error?: Record<string, unknown> | null
  input_frames: Record<string, unknown>[]
  attempts?: number
  usage: PerceptionUsage[]
  unknown_fields?: string[]
}
export interface PerceptionOperation {
  id: string
  job_id: string
  shot_id: string
  mode: 'recover' | 'review'
  status: 'queued' | 'running' | 'applied' | 'unchanged' | 'rejected' | 'failed' | 'cancelled' | 'interrupted' | 'conflict'
  source_revision: number
  error?: string | null
  created_at: string
  updated_at?: string
  cancel_requested?: boolean
  request: { stages?: PerceptionStageName[]; field_group?: PerceptionFieldGroup; reason?: string }
  result: { revision?: number; warning?: string; error?: string }
  usage: PerceptionUsage[]
}
export interface PerceptionStatus {
  job_id: string
  shot_id: string
  revision: number
  stages: Partial<Record<PerceptionStageName, PerceptionStage>>
  recoverable_stages: PerceptionStageName[]
  record_available: boolean
  read_only: boolean
  operations: PerceptionOperation[]
}
export interface PerceptionRevision {
  version: number
  operation_id: string
  created_at: string
  before: Record<string, unknown>
  after: Record<string, unknown>
  changes: {
    mode?: string
    stages?: PerceptionStageName[]
    field_group?: PerceptionFieldGroup
    changed?: boolean
    revisions?: { status?: string; reason?: string; rationale?: string; error?: string; patches?: { field: string; old: { value?: unknown }; new: { value?: unknown } }[] }[]
  }
}
export type PerceptionRequest = {
  mode: 'recover'
  stages: PerceptionStageName[]
  idempotency_key: string
  expected_revision: number
} | {
  mode: 'review'
  field_group: PerceptionFieldGroup
  reason: string
  idempotency_key: string
  expected_revision: number
}

function path(jobId: string, shotId: string) {
  return `/jobs/${encodeURIComponent(jobId)}/shots/${encodeURIComponent(shotId)}/perception`
}
export function getPerception(jobId: string, shotId: string, signal?: AbortSignal) {
  return apiFetch<PerceptionStatus>(path(jobId, shotId), { signal })
}
export function getPerceptionHistory(jobId: string, shotId: string, signal?: AbortSignal) {
  return apiFetch<{ revisions: PerceptionRevision[] }>(`${path(jobId, shotId)}/history`, { signal })
}
export function createPerceptionOperation(jobId: string, shotId: string, request: PerceptionRequest, signal?: AbortSignal) {
  return apiFetch<PerceptionOperation>(`${path(jobId, shotId)}/operations`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request), signal,
  })
}
export function getPerceptionOperation(jobId: string, shotId: string, operationId: string, signal?: AbortSignal) {
  return apiFetch<PerceptionOperation>(`${path(jobId, shotId)}/operations/${encodeURIComponent(operationId)}`, { signal })
}
export function cancelPerceptionOperation(jobId: string, shotId: string, operationId: string, signal?: AbortSignal) {
  return apiFetch<PerceptionOperation>(`${path(jobId, shotId)}/operations/${encodeURIComponent(operationId)}/cancel`, { method: 'POST', signal })
}
