import { apiFetch, apiUrl } from './client'

export interface StyleQualityState {
  structure: string
  observation: string
  interpretation: string
  style: 'resolved' | 'partial' | 'unknown'
  anchor: 'supported' | 'unknown'
  reference?: 'supported' | 'unknown'
  descriptor?: 'usable' | 'unknown'
  family?: 'supported' | 'partial' | 'unknown'
  anchor_validation?: string
  specificity?: { sufficient: boolean; dimensions: string[]; distinctive_dimensions: string[]; note: string }
}

export interface StyleProfileV3 {
  schema_version: string
  descriptive_name: string
  reference_scope: 'reference' | 'project'
  rendered_prompt: string
  detailed_prompt: string
  scene_prompts: Record<string, string>
  local_prompts?: Record<string, string>
  application_status: 'ready' | 'partial' | 'user-edited'
  uncertainties: string[]
  version: number
  profile_hash: string
  source?: StyleRecipeSource | null
  anchor?: { id: string; confidence: number; support_claim_ids: string[]; distinguishing_claim_ids: string[]; reason: string } | null
  reference?: { id: string; confidence: number; selection_basis?: 'distinctive' | 'visual_fit'; reason: string; matched_features: { claim_id: string; quote: string; relevance: string }[]; distinguishing_claim_ids: string[]; alternative_assessments: { id: string; verdict: string; support_claim_ids: string[]; difference: string }[] } | null
}
export interface StyleRecipeSource {
  kind: 'library-recipe'
  recipe_id: string
  source_hash: string
}
export interface StyleRecommendation {
  recipe_id: string
  label: string
  fit_level: 'high' | 'medium'
  reason: string
  controls: { claim_id: string; quote: string }[]
  differences: string[]
  overlap: string[]
  library_prompt: string
  detailed_description: string
  library_source_hash: string
}
export interface StyleDiagnosticDecision {
  field?: string
  index?: number
  level?: string
  reason?: string
  repair?: string
  verified_feature_groups?: Record<string, string[]>
}
export interface StyleDiagnostics {
  schema_version: 'style-diagnostics.v1'
  status?: string
  note?: string
  stages: { name: string; label: string; elapsed_seconds?: number | null; model_requests?: number | null; status: string }[]
  candidate_snapshot: { id: string; label: string; selection_eligible: boolean; semantic_review_allowed?: boolean; matched_group_ids: string[]; missing_group_ids: string[]; retrieval_basis: string }[]
  decisions: StyleDiagnosticDecision[]
  timing: { total_seconds?: number | null; model_seconds?: number | null; retrieval_seconds?: number | null; validation_seconds?: number | null; render_seconds?: number | null }
  calls: { model_requests?: number | null; max_model_requests?: number | null; correction_used?: boolean | null; supplement_rounds?: number | null; crop_count?: number | null }
  quality: { state?: StyleQualityState | null; evidence_claims?: number; approved_descriptions?: number; selected_anchor_id?: string | null; selected_reference_id?: string | null; rejected_or_repaired_fields?: number; human_accuracy?: number | null; semantic_validation?: string; note?: string }
  usage: { prompt_tokens?: number | null; completion_tokens?: number | null; estimated_list_price_cny?: number | null; known_cost_subtotal_cny?: number | null; complete?: boolean; token_usage_complete?: boolean; cost_estimate_complete?: boolean; record_count?: number; failed_requests?: number }
  provenance?: Record<string, unknown>
}
export interface StyleAnalysis {
  analysis_id: string
  status: string
  stage: string
  error: string | null
  created_at: string
  recommendations?: StyleRecommendation[]
  recommendations_derived?: boolean
  result: {
    recommendations?: StyleRecommendation[]
    recommendations_derived?: boolean
    quality_state?: StyleQualityState
    profile?: StyleProfileV3
    evidence?: { claims: { id: string; dimension: string; observation: string; images: string[]; confidence: number }[] }
    assets?: { alias: string; asset_id: string; width: number; height: number; parent_alias?: string }[]
    budget?: { model_requests: number; crop_count: number; elapsed_seconds: number }
    estimated_list_price_cny?: number | null
    cost_estimate_complete?: boolean
    tools?: unknown[]
    diagnostics?: StyleDiagnostics | null
  }
}

export const getStyleAnalysis = (id: string) => apiFetch<StyleAnalysis>(`/style/analyses/${encodeURIComponent(id)}`)
export const getProjectDirectStyle = (job: string) => apiFetch<{ state: { analysis_id?: string }; analysis: StyleAnalysis | null }>(`/jobs/${encodeURIComponent(job)}/style/direct`)
export function analyzeStyleImage(blob: Blob, force = false) {
  const body = new FormData(); body.append('image', blob, 'reference.png')
  if (force) body.append('force', 'true')
  return apiFetch<StyleAnalysis>('/style/analyses', { method: 'POST', body })
}
export const analyzeProjectStyle = (job: string) => apiFetch<StyleAnalysis>(`/jobs/${encodeURIComponent(job)}/style/direct/analyze`, { method: 'POST' })
export const editDirectStyle = (id: string, rendered_prompt: string, expected_version: number, source?: StyleRecipeSource) => apiFetch<StyleAnalysis>(`/style/analyses/${encodeURIComponent(id)}/profile`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rendered_prompt, expected_version, ...(source ? { source } : {}) }) })
export const applyDirectStyle = (job: string, id: string, expected_version: number) => apiFetch(`/jobs/${encodeURIComponent(job)}/style/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ analysis_id: id, expected_version }) })
export const styleImageUrl = (id: string, alias: string) => apiUrl(`/style/analyses/${encodeURIComponent(id)}/images/${encodeURIComponent(alias)}`)
export const styleEventsUrl = (id: string) => apiUrl(`/style/analyses/${encodeURIComponent(id)}/events`)
export const styleDiagnosticsUrl = (id: string) => apiUrl(`/style/analyses/${encodeURIComponent(id)}/diagnostics?format=markdown`)
