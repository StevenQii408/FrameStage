import type { GeneratorDraft } from './generatorGraph'

/** Undo local authoring, never rewind a server task or an asynchronously delivered asset. */
export function restoreGeneratorSnapshot(saved: GeneratorDraft[], current: GeneratorDraft[]): GeneratorDraft[] {
  const liveById = new Map(current.map(item => [item.id, item]))
  return saved.map(item => {
    const restored = JSON.parse(JSON.stringify(item)) as GeneratorDraft
    const live = liveById.get(item.id)
    if (!live) return restored // A deleted node still restores its saved remote handle.
    for (const field of ['generationTask', 'pendingSubmission', 'versions'] as const) {
      delete restored[field]
      if (live[field] !== undefined) Object.assign(restored, { [field]: JSON.parse(JSON.stringify(live[field])) })
    }
    const savedVersions = new Set((item.versions || []).map(version => version.id))
    const receivedNewAsset = live.versions?.some(version => !savedVersions.has(version.id))
    if (receivedNewAsset || (restored.activeVersionId && !live.versions?.some(version => version.id === restored.activeVersionId))) {
      restored.activeVersionId = live.activeVersionId
    }
    if (live.generationTask || live.pendingSubmission || live.versions?.length) restored.nodeMode = 'generator'
    return restored
  })
}
