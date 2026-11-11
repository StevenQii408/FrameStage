import { apiFetch, apiUrl } from './client'
import type { AnalysisReview, DeliveryQuality, GenerationSequence, NarrativeEvent, ScriptDirection } from '../types'

export interface BackendJob {
  id: string
  status: string
  stage: string
  progress: number
  input_type?: 'video' | 'script' | string
  video_filename?: string
  script_text: string
  error?: string | null
  provider?: string | null
  model?: string | null
  segmentation_profile?: string
  style_strategy?: string
  raw_shot_count?: number
  narrative_shot_count?: number
}

export interface BackendFrame {
  id: string
  shot_id: string
  timestamp: number
  width: number
  height: number
}

export interface BackendPrompt {
  id: string
  shot_id: string
  prompt_type: 'first_frame' | 'image' | 'video' | string
  platform: string
  version: number
  positive_prompt: string
  negative_prompt: string
  status: string
  template_id: string
  template_version: number
  warnings?: string[]
  contract?: Record<string, unknown> | null
}

export interface BackendShot {
  id: string
  number: number
  start_time: number
  end_time: number
  duration: number
  time: string
  source_text?: string
  action: string
  shot_size: string
  lens: string
  camera_movement: string
  lighting_vibe: string
  confidence: number
  status: string
  analysis_warnings?: string[]
  analysis_review?: AnalysisReview
  action_details?: Record<string, unknown>
  visual_facts?: { scene?: string; objects?: string[]; people?: string[] }
  spatial_evidence?: Record<string, unknown>
  visual_style_evidence?: Record<string, unknown>
  source_raw_shot_ids?: string[]
  source_action_atom_ids?: string[]
  scene_group_id?: string | null
  cut_reason?: string
  source_edit_times?: number[]
  generation_duration_seconds?: number | null
  scene_reason?: string
  script_direction?: ScriptDirection | null
}

export interface BackendRawShot {
  id: string
  number: number
  start_time: number
  end_time: number
  duration: number
  boundary_metrics: Record<string, unknown>
  decision: string
  reason: string
  narrative_shot_id?: string | null
  generation_shot_ids?: string[]
}

export interface BackendActionAtom {
  id: string
  number: number
  start_time: number
  end_time: number
  duration: number
  source_raw_shot_ids: string[]
  frame_ids: string[]
  primary_subject: string
  initial_state: string
  action: string
  interaction_target: string
  final_state: string
  confidence: number
  warnings: string[]
  generation_shot_id?: string | null
}

export interface BackendSceneGroup {
  id: string
  order: number
  title: string
  location?: string | null
  time_of_day?: string | null
  shot_ids: string[]
  cover_frame_ids: string[]
  start_time: number
  end_time: number
  warning_count: number
  grouping_mode: string
}

export interface StoryboardResult {
  schema_version: string
  job: BackendJob
  shots: BackendShot[]
  frames: BackendFrame[]
  prompts: BackendPrompt[]
  raw_shots?: BackendRawShot[]
  action_atoms?: BackendActionAtom[]
  scene_groups?: BackendSceneGroup[]
  style_profile?: Record<string, unknown> | null
  style_observation?: Record<string, unknown> | null
  visual_bible?: Record<string, unknown> | null
  style_analysis?: { analysis_id?: string; profile?: Record<string, unknown> } | null
  narrative_events?: NarrativeEvent[] | null
  generation_sequences?: GenerationSequence[] | null
  sequence_metadata?: { mode?: 'preview' | 'persisted'; [key: string]: unknown } | null
  metadata?: { delivery_quality?: DeliveryQuality; capacity_preflight?: Record<string, unknown>; scene_style_review?: Record<string, unknown>; [key: string]: unknown }
}

export function getJob(jobId: string) {
  return apiFetch<BackendJob>(`/jobs/${encodeURIComponent(jobId)}`)
}

export function getJobResult(jobId: string) {
  return apiFetch<StoryboardResult>(`/jobs/${encodeURIComponent(jobId)}/result`)
}

export interface CapacityPreflight {
  accepted: boolean
  reasons: string[]
  model_calls?: { normal_estimate?: number; bounded_maximum?: number }
  estimated_list_price_cny?: number | null
}

export function preflightJob(input: { inputType: 'script' | 'video'; scriptText?: string; duration?: number }) {
  const body = new FormData()
  body.append('input_type', input.inputType)
  if (input.scriptText) body.append('script_text', input.scriptText)
  if (input.duration !== undefined) body.append('duration_seconds', String(input.duration))
  return apiFetch<CapacityPreflight>('/jobs/preflight', { method: 'POST', body })
}

async function localDuration(file: File): Promise<number | undefined> {
  if (typeof document === 'undefined' || typeof URL.createObjectURL !== 'function') return undefined
  return new Promise(resolve => {
    const video = document.createElement('video')
    const url = URL.createObjectURL(file)
    const finish = (duration?: number) => {
      clearTimeout(timer)
      video.onloadedmetadata = null; video.onerror = null
      video.removeAttribute('src')
      URL.revokeObjectURL(url)
      resolve(duration)
    }
    const timer = setTimeout(() => finish(), 2000)
    video.preload = 'metadata'
    video.onloadedmetadata = () => finish(Number.isFinite(video.duration) && video.duration > 0 ? video.duration : undefined)
    video.onerror = () => finish()
    video.src = url
  })
}

export async function createVideoJob(video: File, scriptText: string, projectId?: string) {
  if ([...scriptText].length > 10000) throw new Error('附加文本超过10,000字，请按场次分批，原文未截断')
  const duration = await localDuration(video)
  if (duration !== undefined) {
    const capacity = await preflightJob({ inputType: 'video', duration })
    if (!capacity.accepted) throw new Error(capacity.reasons.join('；'))
  }
  const body = new FormData()
  body.append('video', video)
  body.append('script_text', scriptText)
  body.append('input_type', 'video')
  if (projectId) body.append('project_id', projectId)
  return apiFetch<{ job_id: string; status: string }>('/jobs', { method: 'POST', body })
}

export async function createScriptJob(scriptText: string, projectId?: string) {
  if ([...scriptText].length > 10000) throw new Error('剧本超过10,000字，请按场次分批，原文未截断')
  const capacity = await preflightJob({ inputType: 'script', scriptText })
  if (!capacity.accepted) throw new Error(capacity.reasons.join('；'))
  const body = new FormData()
  body.append('script_text', scriptText)
  body.append('input_type', 'script')
  if (projectId) body.append('project_id', projectId)
  return apiFetch<{ job_id: string; status: string }>('/jobs', { method: 'POST', body })
}

export function jobEventsUrl(jobId: string, after = 0) {
  return apiUrl(`/jobs/${encodeURIComponent(jobId)}/events?after=${after}`)
}

export function cancelJob(jobId: string) {
  return apiFetch<{ job_id: string; status: string }>(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
}

export function resumeJob(jobId: string) {
  return apiFetch<{ job_id: string; status: string }>(`/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' })
}

export async function downloadJobExport(jobId: string): Promise<Blob> {
  const response = await fetch(apiUrl(`/jobs/${encodeURIComponent(jobId)}/export`), { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try { detail = (await response.json()).detail || detail } catch { /* preserve HTTP status */ }
    throw new Error(detail)
  }
  return response.blob()
}

export interface StyleCatalogTerm {
  id: string
  category: string
  zh_label: string
  definition: string
  generation_phrase: string
  observable_cues: string[]
  conflicts_with: string[]
}
export interface StyleCatalog {
  version: string
  terms: StyleCatalogTerm[]
  anchors: Record<string, { name: string; type: string; traits: string[] }>
}
export interface StyleProfileData {
  schema_version: string
  status: 'draft' | 'confirmed'
  mode: 'observed' | 'creative' | 'hybrid'
  version: number
  global_terms: string[]
  optional_anchors: string[]
  expanded_anchor_traits: string[]
  include_anchor_names: boolean
  negative_constraints: string[]
  per_shot_effect_overrides: Record<string, string[]>
  rendered_style_prompt: string
  profile_hash: string
}
export interface StyleObservationData {
  mode: string
  dimensions: Record<string, Array<{ term_id: string; confidence: number; evidence: Array<{ frame_id: string; timestamp: number; description: string }> }>>
  uncertainties: string[]
}
export function getStyleCatalog() {
  return apiFetch<StyleCatalog>('/jobs/style/catalog')
}
export function getJobStyle(jobId: string) {
  return apiFetch<{ profile: StyleProfileData | null; observation: StyleObservationData | null }>(`/jobs/${encodeURIComponent(jobId)}/style`)
}
export function regenerateJobStyle(jobId: string) {
  return apiFetch<{ profile: StyleProfileData | null; observation: StyleObservationData | null }>(`/jobs/${encodeURIComponent(jobId)}/style/draft`, { method: 'POST' })
}
export function saveJobStyle(jobId: string, profile: StyleProfileData) {
  return apiFetch<StyleProfileData>(`/jobs/${encodeURIComponent(jobId)}/style`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profile) })
}
export function confirmJobStyle(jobId: string) {
  return apiFetch<StyleProfileData>(`/jobs/${encodeURIComponent(jobId)}/style/confirm`, { method: 'POST' })
}
export interface VisualBibleTerm {
  id: string
  category: string
  name_cn: string
  definition: string
  generation_phrase: string
  conflicts_with?: string[]
}
export interface VisualBibleRecipe {
  id: string
  name: string
  suitable_for: string[]
  global_prompt: string
  term_ids: string[]
  optional_anchors?: string[]
}
export interface VisualBibleCatalog {
  version: string
  ontology_hash: string
  categories: string[]
  terms: VisualBibleTerm[]
  recipes: VisualBibleRecipe[]
}
export interface VisualBibleData {
  schema_version: string
  status: 'draft' | 'confirmed'
  mode: 'observed' | 'creative'
  version: number
  ontology_version: string
  index_manifest: Record<string, unknown>
  global_core_terms: string[]
  global_secondary_terms: string[]
  scene_overrides: Record<string, string[]>
  shot_overrides: Record<string, string[]>
  local_effects: Record<string, string[]>
  forbidden_drift: string[]
  recommended_recipes: string[]
  recipe_recommendations?: Array<{
    recipe_id: string
    status: 'candidate' | 'supported' | 'partial' | 'rejected'
    supported_term_ids: string[]
    missing_term_ids: string[]
    reason: string
    ranks: Record<string, number>
    evidence: Array<{ frame_id: string; timestamp: number; description: string }>
    optional_anchors: string[]
  }>
  selected_recipe_id: string | null
  selected_recipe_term_ids?: string[]
  coverage_profiles: Record<string, { supporting_shots: number; observable_shots: number; retrieval_channels: string[] }>
  unresolved_features: string[]
  rendered_global_prompt: string
  model_provenance: Record<string, unknown>
  bible_hash: string
}
export interface VisualBibleMatch {
  term_id: string
  category: string
  scope: string
  shot_ids: string[]
  evidence: Array<{ frame_id: string; timestamp: number; description: string }>
  ranks: Record<string, number>
  confidence: number
  reason: string
}
export interface VisualBibleResponse {
  bible: VisualBibleData | null
  retrieval: { matches?: VisualBibleMatch[]; unresolved?: Array<{ shot_id: string; category: string; description: string; image_missing?: boolean; candidates: Array<{ term_id: string; ranks: Record<string, number> }> }>; errors?: Array<{ error: string }> } | null
}
export const getVisualBibleCatalog = () => apiFetch<VisualBibleCatalog>('/style/catalog/v2')
export const getVisualBibleIndexHealth = () => apiFetch<{ ready: boolean; reason: string; manifest: Record<string, unknown> | null }>('/style/index/health')
export const getVisualBible = (jobId: string) => apiFetch<VisualBibleResponse>(`/jobs/${encodeURIComponent(jobId)}/visual-bible`)
export const draftVisualBible = (jobId: string) => apiFetch<VisualBibleResponse>(`/jobs/${encodeURIComponent(jobId)}/visual-bible/draft`, { method: 'POST' })
export const saveVisualBible = (jobId: string, bible: VisualBibleData) => apiFetch<VisualBibleData>(`/jobs/${encodeURIComponent(jobId)}/visual-bible`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bible) })
export const confirmVisualBible = (jobId: string) => apiFetch<VisualBibleData>(`/jobs/${encodeURIComponent(jobId)}/visual-bible/confirm`, { method: 'POST' })
