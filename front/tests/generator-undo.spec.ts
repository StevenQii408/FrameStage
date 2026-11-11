import { describe, expect, it } from 'vitest'
import { restoreGeneratorSnapshot } from '../src/generatorUndo'
import { defaultGenerationSettings } from '../src/api/generations'
import type { GeneratorDraft, GeneratorVersion } from '../src/generatorGraph'

const draft = (): GeneratorDraft => ({ id: 'gen-b', kind: 'image', position: { x: 50, y: 80 },
  manualPrompt: '原提示词', imageAssetId: null, imageWidth: 0, imageHeight: 0,
  generationSettings: defaultGenerationSettings('image') })
function version(id: string): GeneratorVersion {
  return { id, jobId: 'generation-' + id, createdAt: '2026-11-11T00:00:00Z', prompt: '已生成提示词',
    settings: defaultGenerationSettings('image'), asset: { id: 'asset-' + id, url: '/asset-' + id,
      mime_type: 'image/png', width: 1280, height: 720 } }
}

describe('local canvas undo and remote generation state', () => {
  it('restores editable settings while retaining a later remote handle, cursor and delivered version', () => {
    const saved = draft()
    const delivered = version('new')
    const live = { ...saved, manualPrompt: '手动修改', generationSettings: { ...saved.generationSettings!, aspect_ratio: '9:16' as const },
      generationTask: { jobId: 'generation-new', inputSignature: 'submitted snapshot', eventCursor: 8 },
      versions: [delivered], activeVersionId: delivered.id }
    const [restored] = restoreGeneratorSnapshot([saved], [live])
    expect(restored.manualPrompt).toBe(saved.manualPrompt)
    expect(restored.generationSettings).toEqual(saved.generationSettings)
    expect(restored.generationTask).toEqual(live.generationTask)
    expect(restored.versions).toEqual([delivered])
    expect(restored.activeVersionId).toBe(delivered.id)
    expect(restored.nodeMode).toBe('generator')
    expect(restored.generationTask).not.toBe(live.generationTask)
  })

  it('retains a lost-response idempotency key and does not revive a settled pending submission', () => {
    const saved = draft()
    const pending = { idempotencyKey: 'once-only', inputSignature: 'pending' }
    expect(restoreGeneratorSnapshot([saved], [{ ...saved, pendingSubmission: pending }])[0].pendingSubmission).toEqual(pending)
    expect(restoreGeneratorSnapshot([{ ...saved, pendingSubmission: pending }], [{ ...saved,
      generationTask: { jobId: 'generation-settled', inputSignature: 'pending' } }])[0].pendingSubmission).toBeUndefined()
  })

  it('allows undo of a chosen history cover when no new server version arrived', () => {
    const versions = [version('new'), version('old')]
    const saved = { ...draft(), versions, activeVersionId: 'new' }
    expect(restoreGeneratorSnapshot([saved], [{ ...saved, activeVersionId: 'old' }])[0].activeVersionId).toBe('new')
  })

  it('restores removed nodes with saved history and never reintroduces a newly created node', () => {
    const saved = { ...draft(), versions: [version('kept')], activeVersionId: 'kept' }
    const restored = restoreGeneratorSnapshot([saved], [{ ...draft(), id: 'gen-created-later' }])
    expect(restored).toEqual([saved])
    expect(restored[0]).not.toBe(saved)
  })
})
