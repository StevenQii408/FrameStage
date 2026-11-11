import { apiFetch } from './client'

export interface ProjectSummary {
  id: string
  name: string
  created_at: string
  updated_at: string
  deleted_at?: string | null
  analysis_count?: number
  generation_count?: number
  active_count?: number
  revision?: number
  job_ids?: string[]
  generation_ids?: string[]
  cover_url?: string | null
}

export interface ProjectCanvas {
  project_id: string
  revision: number
  state: Record<string, unknown>
  updated_at?: string
}

export const listProjects = (deleted = false, offset = 0, query = '', limit = 24) => {
  const search = new URLSearchParams({ limit: String(limit), offset: String(offset), ...(deleted ? { deleted: 'true' } : {}), ...(query ? { q: query } : {}) })
  return apiFetch<ProjectSummary[]>(`/projects?${search}`)
}
export const createProject = (name: string, idempotencyKey?: string) => apiFetch<ProjectSummary>('/projects', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}) }),
})
export const getProject = (id: string) => apiFetch<ProjectSummary>(`/projects/${encodeURIComponent(id)}`)
export const renameProject = (id: string, name: string) => apiFetch<ProjectSummary>(`/projects/${encodeURIComponent(id)}`, {
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
})
export const deleteProject = (id: string) => apiFetch<ProjectSummary>(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' })
export const restoreProject = (id: string) => apiFetch<ProjectSummary>(`/projects/${encodeURIComponent(id)}/restore`, { method: 'POST' })
export const getProjectCanvas = (id: string) => apiFetch<ProjectCanvas>(`/projects/${encodeURIComponent(id)}/canvas`)
export const saveProjectCanvas = (id: string, revision: number, state: Record<string, unknown>) => apiFetch<ProjectCanvas>(`/projects/${encodeURIComponent(id)}/canvas`, {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: revision, state }),
})
export const importLegacyCanvas = (id: string, state: Record<string, unknown>, jobIds: string[], generationIds: string[], revision = 0) => apiFetch<ProjectCanvas>(`/projects/${encodeURIComponent(id)}/import-legacy`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: revision, state, job_ids: jobIds, generation_ids: generationIds }),
})
