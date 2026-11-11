import { describe, expect, it } from 'vitest'
import { encodeCanvasState, readCanvasState } from '../src/canvasPersistence'

describe('canvas persistence', () => {
  it('restores stable task keys, positions, scene pages, and selection', () => {
    const state = { jobKeys: { abc: 'draft-123' }, positions: { 'draft-123': { x: 49, y: 12 } }, sceneUi: { 'draft-123:scene:01': { open: true, page: 2 } }, selectedId: 'shot-1', activeTaskKey: 'draft-123' }
    expect(readCanvasState(encodeCanvasState(state))).toEqual({ ...state, sceneUi: { 'draft-123:scene:01': { open: true, page: 1 } } })
  })

  it('round-trips local generator drafts and connections without persisting blobs in localStorage', () => {
    const generator = { id: 'gen-one', kind: 'image', position: { x: 42, y: -12 }, manualPrompt: '雨夜城市', imageAssetId: 'image-1', imageWidth: 768, imageHeight: 1366 }
    const connection = { id: 'wire-gen-one-prompt-in', source: 'prompt-1', sourceHandle: 'prompt-out', target: 'gen-one', targetHandle: 'prompt-in' }
    const value = readCanvasState(JSON.stringify({ jobKeys: {}, positions: {}, sceneUi: {}, selectedId: 'gen-one', activeTaskKey: '', generators: [generator], generatorConnections: [connection] }))
    expect(value?.generators).toEqual([{ ...generator, nodeMode: 'reference' }])
    expect(value?.generatorConnections).toEqual([connection])
    expect(JSON.stringify(value)).not.toContain('blob:')
  })

  it('drops invalid coordinates and malformed scene state', () => {
    const value = readCanvasState(JSON.stringify({ jobKeys: { a: 'task-a' }, positions: { good: { x: 1, y: 2 }, bad: { x: '1', y: 2 } }, sceneUi: { good: { open: false, page: 1 }, bad: { open: true, page: 0 } } }))
    expect(value?.positions).toEqual({ good: { x: 1, y: 2 } })
    expect(value?.sceneUi).toEqual({ good: { open: false, page: 1 }, bad: { open: true, page: 1 } })
    expect(readCanvasState('{broken')).toBeNull()
  })
  it('preserves multiple references, stable mention aliases, selections and model choice', () => {
    const generator = { id: 'gen-ref', kind: 'video', position: { x: 0, y: 0 }, manualPrompt: '@图片2', imageAssetId: 'image-a', imageWidth: 100, imageHeight: 80,
      referenceImages: [{ assetId: 'image-b', width: 100, height: 80 }],
      referenceAliases: { 'ref-aaaaaaaaaaaa': '图片1', 'ref-bbbbbbbbbbbb': '图片2' }, referenceSelections: { 'ref-aaaaaaaaaaaa': false }, firstFrameReferenceId: 'ref-bbbbbbbbbbbb',
      generationSettings: { aspect_ratio: '9:16', resolution: '1080p', duration_seconds: 5, model_id: 'declared-model' } }
    const restored = readCanvasState(JSON.stringify({ generators: [generator] }))!
    expect(restored.generators).toEqual([generator])
  })
  it('does not normalize historical fractional durations or pending submission identities on reload', () => {
    const generator = { id: 'gen-video', kind: 'video', position: { x: 0, y: 0 }, manualPrompt: '原始 Prompt', imageAssetId: null, imageWidth: 0, imageHeight: 0,
      generationSettings: { aspect_ratio: '16:9', resolution: '1080p', duration_seconds: 4.5 },
      pendingSubmission: { idempotencyKey: 'pending-key', inputSignature: '{"duration_seconds":4.5}' } }
    expect(readCanvasState(JSON.stringify({ generators: [generator] }))?.generators).toEqual([generator])
  })
})
