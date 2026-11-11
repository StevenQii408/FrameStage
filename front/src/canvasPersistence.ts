import { connectionId, type GeneratorConnection, type GeneratorDraft } from './generatorGraph'
import { readGenerationSettings } from './api/generations'
import { isReferenceAsset, readGeneratorVersions } from './generatorVersions'

export interface SavedCanvasState {
  jobKeys: Record<string, string>
  positions: Record<string, { x: number; y: number }>
  sceneUi: Record<string, { open: boolean; page: number }>
  sequenceUi?: Record<string, { open: boolean }>
  selectedId: string
  activeTaskKey: string
  generationView?: 'sequences' | 'source'
  generators?: GeneratorDraft[]
  generatorConnections?: GeneratorConnection[]
}

export function readCanvasState(raw: string | null): SavedCanvasState | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const jobKeys: SavedCanvasState['jobKeys'] = {}
    const positions: SavedCanvasState['positions'] = {}
    const sceneUi: SavedCanvasState['sceneUi'] = {}
    const sequenceUi: NonNullable<SavedCanvasState['sequenceUi']> = {}
    for (const [jobId, key] of Object.entries(value.jobKeys || {})) {
      if (typeof key === 'string' && key.length < 160) jobKeys[jobId] = key
    }
    for (const [id, point] of Object.entries(value.positions || {})) {
      const candidate = point as { x?: unknown; y?: unknown }
      if (typeof candidate?.x === 'number' && Number.isFinite(candidate.x) && typeof candidate.y === 'number' && Number.isFinite(candidate.y)) positions[id] = { x: candidate.x, y: candidate.y }
    }
    for (const [id, state] of Object.entries(value.sceneUi || {})) {
      const candidate = state as { open?: unknown; page?: unknown }
      if (typeof candidate?.open === 'boolean') sceneUi[id] = { open: candidate.open, page: 1 }
    }
    for (const [id, state] of Object.entries(value.sequenceUi || {})) {
      const candidate = state as { open?: unknown }
      if (typeof candidate?.open === 'boolean') sequenceUi[id] = { open: candidate.open }
    }
    const generators: GeneratorDraft[] = Array.isArray(value.generators) ? value.generators.flatMap((raw: unknown) => {
      if (!raw || typeof raw !== 'object') return []
      const item = raw as Record<string, unknown>
      const point = item.position as { x?: unknown; y?: unknown } | undefined
      if (typeof item.id !== 'string' || !item.id.startsWith('gen-') || (item.kind !== 'image' && item.kind !== 'video') || typeof point?.x !== 'number' || !Number.isFinite(point.x) || typeof point.y !== 'number' || !Number.isFinite(point.y)) return []
      const savedTask = item.generationTask as Record<string, unknown> | undefined
      const generationTask = savedTask && typeof savedTask.jobId === 'string' && savedTask.jobId.startsWith('generation-') && typeof savedTask.inputSignature === 'string'
        ? { jobId: savedTask.jobId, inputSignature: savedTask.inputSignature,
            eventCursor: typeof savedTask.eventCursor === 'number' && Number.isSafeInteger(savedTask.eventCursor) && savedTask.eventCursor >= 0 ? savedTask.eventCursor : 0 } : undefined
      const pending = item.pendingSubmission as Record<string, unknown> | undefined
      const pendingSubmission = pending && typeof pending.idempotencyKey === 'string' && pending.idempotencyKey.length <= 160 && typeof pending.inputSignature === 'string'
        ? { idempotencyKey: pending.idempotencyKey, inputSignature: pending.inputSignature,
            ...(typeof pending.retryOf === 'string' && pending.retryOf.startsWith('generation-') ? { retryOf: pending.retryOf } : {}) } : undefined
      const referenceImages = Array.isArray(item.referenceImages) ? item.referenceImages.flatMap((raw: unknown) => {
        if (!raw || typeof raw !== 'object') return []
        const image = raw as Record<string, unknown>
        return typeof image.assetId === 'string' && typeof image.width === 'number' && Number.isFinite(image.width) && image.width > 0
          && typeof image.height === 'number' && Number.isFinite(image.height) && image.height > 0
          ? [{ assetId: image.assetId, width: image.width, height: image.height }] : []
      }).slice(0, 8) : undefined
      const referenceAliases = Object.fromEntries(Object.entries(item.referenceAliases || {}).filter(([id, alias]) => /^ref-[a-f0-9]{12}$/.test(id) && typeof alias === 'string' && /^图片[1-9]\d{0,4}$/.test(alias))) as Record<string, string>
      const referenceSelections = Object.fromEntries(Object.entries(item.referenceSelections || {}).filter(([id, selected]) => /^ref-[a-f0-9]{12}$/.test(id) && typeof selected === 'boolean')) as Record<string, boolean>
      const override = item.connectedPromptOverride as Record<string, unknown> | undefined
      const connectedPromptOverride = override && typeof override.sourceId === 'string' && typeof override.sourceText === 'string' && typeof override.text === 'string'
        ? { sourceId: override.sourceId, sourceText: override.sourceText.slice(0, 10000), text: override.text.slice(0, 10000) } : undefined
      const versions = readGeneratorVersions(item.versions, item.kind)
      const restored: GeneratorDraft = { id: item.id, kind: item.kind, position: { x: point.x, y: point.y }, manualPrompt: typeof item.manualPrompt === 'string' ? item.manualPrompt.slice(0, 10000) : '', imageAssetId: typeof item.imageAssetId === 'string' ? item.imageAssetId : null, imageWidth: typeof item.imageWidth === 'number' && Number.isFinite(item.imageWidth) ? item.imageWidth : 0, imageHeight: typeof item.imageHeight === 'number' && Number.isFinite(item.imageHeight) ? item.imageHeight : 0,
        ...(item.generationSettings ? { generationSettings: readGenerationSettings(item.generationSettings, item.kind) } : {}),
        ...(generationTask ? { generationTask } : {}), ...(pendingSubmission ? { pendingSubmission } : {}),
        ...(referenceImages ? { referenceImages } : {}),
        ...(Object.keys(referenceAliases).length ? { referenceAliases } : {}), ...(Object.keys(referenceSelections).length ? { referenceSelections } : {}),
        ...(typeof item.firstFrameReferenceId === 'string' && /^ref-[a-f0-9]{12}$/.test(item.firstFrameReferenceId) ? { firstFrameReferenceId: item.firstFrameReferenceId } : {}),
        ...(connectedPromptOverride ? { connectedPromptOverride } : {}),
        ...(item.nodeMode === 'generator' || item.nodeMode === 'reference' ? { nodeMode: item.nodeMode } : {}),
        ...(versions.length ? { versions, activeVersionId: typeof item.activeVersionId === 'string' && versions.some(version => version.id === item.activeVersionId) ? item.activeVersionId : versions[0].id } : {}) }
      if (!restored.nodeMode && isReferenceAsset(restored)) restored.nodeMode = 'reference'
      return [restored]
    }) : []
    const generatorIds = new Set(generators.map((item) => item.id))
    const generatorConnections: GeneratorConnection[] = Array.isArray(value.generatorConnections) ? value.generatorConnections.flatMap((raw: unknown) => {
      if (!raw || typeof raw !== 'object') return []
      const item = raw as Record<string, unknown>
      if (typeof item.source !== 'string' || typeof item.target !== 'string' || !generatorIds.has(item.target) || (item.targetHandle !== 'prompt-in' && item.targetHandle !== 'image-in') || !['prompt-out', 'frame-out', 'image-out'].includes(String(item.sourceHandle))) return []
      return [{ id: connectionId(item.target, item.targetHandle, item.targetHandle === 'image-in' ? item.source : undefined), source: item.source, sourceHandle: item.sourceHandle, target: item.target, targetHandle: item.targetHandle }]
    }) : []
    return { jobKeys, positions, sceneUi, ...(value.sequenceUi && typeof value.sequenceUi === 'object' ? { sequenceUi } : {}), selectedId: typeof value.selectedId === 'string' ? value.selectedId : '', activeTaskKey: typeof value.activeTaskKey === 'string' ? value.activeTaskKey : '', ...(value.generationView === 'sequences' || value.generationView === 'source' ? { generationView: value.generationView } : {}), ...(Array.isArray(value.generators) ? { generators } : {}), ...(Array.isArray(value.generatorConnections) ? { generatorConnections } : {}) }
  } catch { return null }
}

export function encodeCanvasState(state: SavedCanvasState): string {
  return JSON.stringify(state)
}
