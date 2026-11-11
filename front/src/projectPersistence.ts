import { createProject, importLegacyCanvas } from './api/projects'
import { readCanvasState } from './canvasPersistence'
import { readJobIds } from './urlState'
import { scopeLegacyGeneratorSignature } from './composables/useGeneratorTasks'
import { recoverCanvasDrafts } from './canvasRecovery'

export interface CachedProjectCanvas {
  revision: number
  state: Record<string, unknown>
  dirty: boolean
  savedAt: string
}

export const projectCacheKey = (id: string) => `frameforge-project:${id}:canvas-v1`
export function readProjectCache(id: string): CachedProjectCanvas | null {
  try {
    const item = JSON.parse(localStorage.getItem(projectCacheKey(id)) || 'null')
    return item && Number.isSafeInteger(item.revision) && item.revision >= 0 && item.state && typeof item.state === 'object' && !Array.isArray(item.state)
      ? item : null
  } catch { return null }
}
export function writeProjectCache(id: string, value: CachedProjectCanvas) {
  try {
    localStorage.setItem(projectCacheKey(id), JSON.stringify(value))
    return true
  } catch { return false }
}

/** Keep the original canvas and all IndexedDB blobs until migration is acknowledged. */
export async function migrateLegacyCanvas(search = window.location.search): Promise<string | null> {
  const completed = localStorage.getItem('frameforge-project-migration-v1')
  if (completed) return null
  const raw = localStorage.getItem('frameforge-canvas-v1')
  const legacy = readCanvasState(raw)
  let source: Record<string, unknown> = {}
  try { const value = JSON.parse(raw || '{}'); if (value && typeof value === 'object' && !Array.isArray(value)) source = value } catch { /* invalid data stays untouched */ }
  const requested = readJobIds(search)
  if (!legacy && !requested.length) return null
  if (!requested.length && !Object.keys(legacy?.jobKeys || {}).length && !legacy?.generators?.length && !(Array.isArray(source.drafts) && source.drafts.length)) return null
  const state: Record<string, unknown> = legacy ? { ...source, ...legacy } : { jobKeys: {}, positions: {}, sceneUi: {}, selectedId: '', activeTaskKey: '' }
  if (Array.isArray(source.drafts)) state.drafts = recoverCanvasDrafts(source.drafts).drafts
  state.jobKeys = { ...(legacy?.jobKeys || {}), ...Object.fromEntries(requested.map(id => [id, legacy?.jobKeys[id] || `task-${id}`])) }
  let projectId = localStorage.getItem('frameforge-project-migration-pending-v1')
  if (!projectId) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(state)))
    const key = 'legacy-' + Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
    const project = await createProject('旧工作台', key)
    projectId = project.id
    localStorage.setItem('frameforge-project-migration-pending-v1', projectId)
  }
  const generationIds = [...new Set((legacy?.generators || []).flatMap(item => [item.generationTask?.jobId, item.pendingSubmission?.retryOf].filter((id): id is string => Boolean(id))))]
  if (legacy?.generators) state.generators = legacy.generators.map(item => ({ ...item,
    ...(item.generationTask ? { generationTask: { ...item.generationTask, inputSignature: scopeLegacyGeneratorSignature(item.generationTask.inputSignature, projectId!) } } : {}),
    ...(item.pendingSubmission ? { pendingSubmission: { ...item.pendingSubmission, inputSignature: scopeLegacyGeneratorSignature(item.pendingSubmission.inputSignature, projectId!) } } : {}),
  }))
  const imported = await importLegacyCanvas(projectId, state, Object.keys(state.jobKeys as object), generationIds)
  writeProjectCache(projectId, { revision: imported.revision, state: imported.state, dirty: false, savedAt: new Date().toISOString() })
  localStorage.setItem('frameforge-project-migration-v1', projectId)
  localStorage.removeItem('frameforge-project-migration-pending-v1')
  return projectId
}
