import { apiFetch, apiUrl } from './client'

export type GenerationKind = 'image' | 'video'
export type GenerationStatus = 'blocked' | 'queued' | 'submitting' | 'running' | 'finalizing' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
export interface GenerationSettings {
  aspect_ratio: '21:9' | '16:9' | '9:16' | '1:1' | '4:3' | '3:4'
  resolution: '480p' | '512p' | '720p' | '1080p' | '1536p' | '2048p' | '2160p'
  duration_seconds: number | null
  model_id?: string | null
}
export interface GenerationAsset {
  id: string
  url: string
  mime_type: string
  width: number
  height: number
  size_bytes?: number
  kind?: GenerationKind
}
export interface GenerationRequest {
  node_id: string
  kind: GenerationKind
  prompt: string
  reference_asset_id?: string | null
  settings: GenerationSettings
  project_id?: string | null
  reference_images?: Array<{ reference_id: string; asset_id: string; mention: string; role: 'reference' | 'first_frame' }>
}
export interface GenerationJob {
  id: string
  node_id: string
  project_id?: string | null
  kind: GenerationKind
  status: GenerationStatus
  stage: string
  progress: number | null
  revision: number
  provider: string
  input_hash: string
  request: GenerationRequest
  assets: GenerationAsset[]
  error: { code: string; message: string; retryable: boolean } | null
  created_at: string
  updated_at: string
  retry_of?: string | null
}
export interface GenerationModelCapability {
  id: string; name?: string; label?: string; kind?: GenerationKind; kinds?: GenerationKind[]
  aspect_ratios?: string[]; resolutions?: string[]; min_duration_seconds?: number; max_duration_seconds?: number
  supports_reference?: boolean; max_reference_images?: number
  resolution_options?: GenerationResolutionOption[]; resolution_mode?: 'pixel-size'
}
export interface GenerationResolutionOption { value: string; aspect_ratio: string; width: number; height: number; label: string }
export interface GenerationKindCapability {
  available: boolean; models?: Array<string | GenerationModelCapability>; aspect_ratios?: string[]; resolutions?: string[]
  min_duration_seconds?: number; max_duration_seconds?: number; supports_reference?: boolean; max_reference_images?: number
  default_model_id?: string; resolution_options?: GenerationResolutionOption[]; resolution_mode?: 'pixel-size'
}
export interface GenerationCapabilities {
  configured: boolean; provider: string; reason: string; models?: Array<string | GenerationModelCapability>; image?: GenerationKindCapability; video?: GenerationKindCapability
  request_limits?: { video?: { min_duration_seconds: number; max_duration_seconds: number; duration_step_seconds: number; extensible_max_duration_seconds?: number } }
}
export interface GenerationPreflight { valid: boolean; can_submit: boolean; configured: boolean; issues: string[]; reason: string; input_hash?: string }
export interface GenerationEvent {
  sequence: number
  job_id: string
  type: string
  payload: { job?: GenerationJob }
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}
export const getGenerationCapabilities = () => apiFetch<GenerationCapabilities>('/generations/capabilities')
export const preflightGeneration = (request: GenerationRequest) => post<GenerationPreflight>('/generations/preflight', request)
export const createGeneration = (request: GenerationRequest, key: string) => post<GenerationJob>('/generations', { ...request, idempotency_key: key })
export const getGeneration = (id: string) => apiFetch<GenerationJob>(`/generations/${encodeURIComponent(id)}`)
export const cancelGeneration = (id: string) => post<GenerationJob>(`/generations/${encodeURIComponent(id)}/cancel`, {})
export const retryGeneration = (id: string, key: string) => post<GenerationJob>(`/generations/${encodeURIComponent(id)}/retry`, { idempotency_key: key })
export function uploadGenerationImage(image: Blob, projectId?: string | null): Promise<GenerationAsset> {
  const form = new FormData()
  form.append('image', image, 'reference.png')
  if (projectId) form.append('project_id', projectId)
  return apiFetch<GenerationAsset>('/generation-assets', { method: 'POST', body: form })
}
export const generationEventsUrl = (id: string, cursor = 0) => apiUrl(`/generations/${encodeURIComponent(id)}/events?after=${cursor}`)

/** One replayable event stream for all generation nodes in the selected project. */
export function projectGenerationEventsUrl(projectId?: string | null, cursor = 0): string {
  const url = new URL(apiUrl('/generations/events'), window.location.href)
  url.searchParams.set('after', String(cursor))
  // apiUrl may include the currently active project. Use the captured scope,
  // including an explicit legacy scope, rather than following later UI edits.
  if (projectId) {
    url.searchParams.set('project_id', projectId)
    url.searchParams.delete('scope')
  } else {
    url.searchParams.delete('project_id')
    url.searchParams.set('scope', 'legacy')
  }
  return url.href
}

/** Server asset paths are relative to the API host, never to the Vite host. */
export function generationAssetUrl(path: string): string {
  const base = new URL(apiUrl('/'), window.location.href)
  const value = new URL(path, base)
  return value.origin === base.origin && value.pathname.startsWith('/api/generation-assets/') ? value.href : ''
}

function scopedMediaUrl(path: string): URL | null {
  const source = generationAssetUrl(path)
  if (!source) return null
  const url = new URL(source)
  if (!/^\/api\/generation-assets\/genasset-[a-f0-9]{64}$/.test(url.pathname)) return null
  if (!url.searchParams.has('project_id')) {
    const scoped = new URL(apiUrl(url.pathname.slice('/api'.length)), window.location.href)
    const project = scoped.searchParams.get('project_id')
    if (project) url.searchParams.set('project_id', project)
  }
  return url
}

/** Resolve a first-frame JPEG on the source asset's API origin and project scope. */
export function generationVideoPosterUrl(path: string): string {
  const url = scopedMediaUrl(path)
  if (!url) return ''
  url.pathname += '/poster'
  url.hash = ''
  url.searchParams.delete('download')
  // Previously cached previews used a seek offset instead of the first frame.
  url.searchParams.set('frame', 'first')
  return url.href
}

/** Use a native attachment link so Safari can stream full-size video from the API origin. */
export function generationAssetDownloadUrl(asset: GenerationAsset): string {
  const url = scopedMediaUrl(asset.url)
  if (!url || url.pathname !== '/api/generation-assets/' + asset.id) return ''
  url.searchParams.set('download', 'true')
  return url.href
}

export const generationTerminal = (status: GenerationStatus) => ['blocked', 'succeeded', 'failed', 'cancelled', 'interrupted'].includes(status)
export function defaultGenerationSettings(kind: GenerationKind): GenerationSettings {
  return { aspect_ratio: '16:9', resolution: '1080p', duration_seconds: kind === 'video' ? 5 : null }
}
export function readGenerationSettings(value: unknown, kind: GenerationKind): GenerationSettings {
  const defaults = defaultGenerationSettings(kind)
  if (!value || typeof value !== 'object') return defaults
  const item = value as Record<string, unknown>
  return {
    aspect_ratio: ['21:9', '16:9', '9:16', '1:1', '4:3', '3:4'].includes(String(item.aspect_ratio)) ? item.aspect_ratio as GenerationSettings['aspect_ratio'] : defaults.aspect_ratio,
    resolution: ['480p', '512p', '720p', '1080p', '1536p', '2048p', '2160p'].includes(String(item.resolution)) ? item.resolution as GenerationSettings['resolution'] : defaults.resolution,
    // Reading a saved draft must not rewrite its snapshot or invalidate its idempotency key.
    duration_seconds: kind === 'video' && typeof item.duration_seconds === 'number' && Number.isFinite(item.duration_seconds) ? item.duration_seconds : defaults.duration_seconds,
    ...('model_id' in item ? { model_id: typeof item.model_id === 'string' && item.model_id.length <= 160 ? item.model_id : null } : {}),
  }
}

export function generationModels(capabilities: GenerationCapabilities | null | undefined, kind: GenerationKind): GenerationModelCapability[] {
  return (capabilities?.[kind]?.models || capabilities?.models || [])
    .filter(model => typeof model === 'string' || (!model.kind || model.kind === kind) && (!model.kinds || model.kinds.includes(kind)))
    .map(model => typeof model === 'string' ? { id: model, name: model } : model)
}

/** Resolve presentation without mutating saved inputs or their idempotency signature. */
export function effectiveGenerationModelId(capabilities: GenerationCapabilities | null | undefined, kind: GenerationKind, selected?: string | null): string {
  if (selected) return selected
  const models = generationModels(capabilities, kind)
  return capabilities?.[kind]?.default_model_id || (models.length === 1 ? models[0].id : '')
}

/** Intersect the backend request contract with the selected model's capabilities. */
export function generationDurationRange(capabilities?: GenerationCapabilities | null, modelId?: string | null): { min: number; max: number; step: number } {
  const limits = capabilities?.request_limits?.video
  const kind = capabilities?.video
  const candidates = kind?.models || capabilities?.models || []
  const effectiveId = effectiveGenerationModelId(capabilities, 'video', modelId)
  const model = candidates.find(item => typeof item !== 'string' && item.id === effectiveId
    && (!item.kind || item.kind === 'video') && (!item.kinds || item.kinds.includes('video')))
  const specific = typeof model === 'object' ? model : undefined
  const min = Math.max(1, Math.ceil(limits?.min_duration_seconds ?? 5), Math.ceil(specific?.min_duration_seconds ?? kind?.min_duration_seconds ?? 1))
  const max = Math.min(30, Math.floor(limits?.max_duration_seconds ?? 15), Math.floor(specific?.max_duration_seconds ?? kind?.max_duration_seconds ?? 30))
  return { min, max, step: 1 }
}

export function generationDurationIssue(settings: GenerationSettings, kind: GenerationKind, capabilities?: GenerationCapabilities | null): string {
  if (kind !== 'video') return ''
  const range = generationDurationRange(capabilities, settings.model_id)
  if (range.min > range.max) return '当前模型与后端时长范围不兼容，请更换模型。'
  return typeof settings.duration_seconds === 'number' && Number.isInteger(settings.duration_seconds)
    && settings.duration_seconds >= range.min && settings.duration_seconds <= range.max
    ? '' : `视频时长须为 ${range.min}–${range.max} 秒的整数，请修改后保存新任务。`
}
