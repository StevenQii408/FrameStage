import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { GenerationJob } from '../src/api/generations'
import { defaultGenerationSettings } from '../src/api/generations'
import { connectionSources, rememberGeneratorReferences, resolveGenerator, validateGeneratorConnection, type GeneratorDraft, type GeneratorConnection } from '../src/generatorGraph'
import { activeGeneratorAsset, activeGeneratorVersion, isReferenceAsset, readGeneratorVersions, recordGeneratorSuccess, selectGeneratorVersion } from '../src/generatorVersions'
import { generatorFrameSize } from '../src/generatorGeometry'
import { readCanvasState } from '../src/canvasPersistence'
import { useGeneratorEdits } from '../src/composables/useGeneratorEdits'

const node = (): GeneratorDraft => ({ id: 'gen-image', kind: 'image', nodeMode: 'generator', position: { x: 0, y: 0 },
  manualPrompt: '当前未提交的草稿', imageAssetId: null, imageWidth: 0, imageHeight: 0, generationSettings: defaultGenerationSettings('image') })
function job(number = 1): GenerationJob {
  const id = 'genasset-' + String(number).repeat(64)
  return { id: 'generation-' + number, node_id: 'gen-image', kind: 'image', status: 'succeeded', stage: 'succeeded',
    progress: 100, revision: 5, provider: 'test-only', input_hash: 'snapshot-' + number,
    created_at: '2026-11-10T00:00:00Z', updated_at: `2026-11-10T00:00:0${number}Z`,
    request: { node_id: 'gen-image', kind: 'image', prompt: '已提交的 Prompt ' + number, settings: defaultGenerationSettings('image') },
    assets: [{ id, url: '/api/generation-assets/' + id, mime_type: 'image/png', width: number === 1 ? 1000 : 600, height: number === 1 ? 500 : 900 }], error: null }
}

describe('reference assets and immutable generation versions', () => {
  it('records successful deliveries newest first and normalizes preview URLs', () => {
    const item = node(), first = job(1)
    expect(recordGeneratorSuccess(item, first, 'sig-one')).toBe(true)
    const firstId = activeGeneratorVersion(item)!.id
    expect(recordGeneratorSuccess(item, job(2), 'sig-two')).toBe(true)
    expect(item.versions?.map(version => version.jobId)).toEqual(['generation-2', 'generation-1'])
    expect(item.activeVersionId).not.toBe(firstId)
    expect(activeGeneratorVersion(item)?.createdAt).toBe(job(2).updated_at)
    expect(activeGeneratorAsset(item)?.url).toMatch(/^http:\/\/127\.0\.0\.1:8000\/api\/generation-assets\//)
    first.request.prompt = '后来改变'; first.request.settings.aspect_ratio = '1:1'
    expect(item.versions?.[1].prompt).toBe('已提交的 Prompt 1')
    expect(item.versions?.[1].settings.aspect_ratio).toBe('16:9')
  })

  it('historical selection never overwrites drafts and repeated REST never steals selection', () => {
    const item = node(); recordGeneratorSuccess(item, job(1)); recordGeneratorSuccess(item, job(2))
    const first = item.versions![1]
    item.generationSettings!.aspect_ratio = '9:16'
    expect(selectGeneratorVersion(item, first.id)).toBe(true)
    expect(recordGeneratorSuccess(item, job(2))).toBe(false)
    expect(activeGeneratorVersion(item)?.id).toBe(first.id)
    expect(item.manualPrompt).toBe('当前未提交的草稿')
    expect(item.generationSettings?.aspect_ratio).toBe('9:16')
    expect(selectGeneratorVersion(item, 'unknown')).toBe(false)
  })

  it('orders deliveries within one millisecond by their full microsecond precision', () => {
    const item = node()
    const older = { ...job(2), updated_at: '2026-11-10T00:00:00.123001+00:00' }
    const newer = { ...job(1), updated_at: '2026-11-10T00:00:00.123999Z' }
    expect(Date.parse(older.updated_at)).toBe(Date.parse(newer.updated_at))
    recordGeneratorSuccess(item, older)
    recordGeneratorSuccess(item, newer)
    expect(item.versions?.map(version => version.jobId)).toEqual(['generation-1', 'generation-2'])
    expect(activeGeneratorVersion(item)?.jobId).toBe('generation-1')
    const restored = readCanvasState(JSON.stringify({ generators: [item] }))!.generators![0]
    expect(restored.versions?.map(version => version.jobId)).toEqual(['generation-1', 'generation-2'])
    selectGeneratorVersion(item, item.versions![1].id)
    expect(recordGeneratorSuccess(item, newer)).toBe(false)
    expect(activeGeneratorVersion(item)?.jobId).toBe('generation-2')
  })

  it('normalizes offsets for time ordering and preserves explicit order at exactly equal times', () => {
    const item = node()
    recordGeneratorSuccess(item, { ...job(2), updated_at: '2026-11-10T08:00:00.123900+08:00' })
    recordGeneratorSuccess(item, { ...job(1), updated_at: '2026-11-10T00:00:00.123901Z' })
    expect(activeGeneratorVersion(item)?.jobId).toBe('generation-1')
    const equal = node()
    recordGeneratorSuccess(equal, { ...job(2), updated_at: '2026-11-10T00:00:00.123000Z' })
    recordGeneratorSuccess(equal, { ...job(1), updated_at: '2026-11-10T00:00:00.123Z' })
    expect(equal.versions?.map(version => version.jobId)).toEqual(['generation-1', 'generation-2'])
    expect(activeGeneratorVersion(equal)?.jobId).toBe('generation-1')
    expect(readGeneratorVersions(JSON.parse(JSON.stringify(equal.versions)), 'image').map(version => version.jobId)).toEqual(['generation-1', 'generation-2'])
  })

  it('failed, incomplete, wrong-owner and wrong-kind jobs cannot replace a usable version', () => {
    const item = node(); recordGeneratorSuccess(item, job(1))
    const before = JSON.stringify(item)
    expect(recordGeneratorSuccess(item, { ...job(2), status: 'failed' })).toBe(false)
    expect(recordGeneratorSuccess(item, { ...job(2), status: 'running' })).toBe(false)
    expect(recordGeneratorSuccess(item, { ...job(2), node_id: 'other-node' })).toBe(false)
    expect(recordGeneratorSuccess(item, { ...job(2), kind: 'video' })).toBe(false)
    expect(JSON.stringify(item)).toBe(before)
  })

  it('only the active output feeds downstream and its stable mention tracks a changed version', () => {
    const item = node(), target: GeneratorDraft = { ...node(), id: 'gen-video', kind: 'video' }
    recordGeneratorSuccess(item, job(1)); recordGeneratorSuccess(item, job(2))
    const wires: GeneratorConnection[] = [{ id: 'wire', source: item.id, sourceHandle: 'image-out', target: target.id, targetHandle: 'image-in' }]
    const first = resolveGenerator(target.id, [item, target], wires, {}, {}, {})
    rememberGeneratorReferences(target, first.references!)
    selectGeneratorVersion(item, item.versions![1].id)
    const second = resolveGenerator(target.id, [item, target], wires, {}, {}, {})
    expect(first.imageUrl).not.toBe(second.imageUrl)
    expect(first.references![0].identity).not.toBe(second.references![0].identity)
    expect(first.references![0].id).toBe(second.references![0].id)
    expect(first.references![0].mention).toBe(second.references![0].mention)
    expect(second.imageWidth).toBe(1000)
  })

  it('uses original media ratios instead of next-attempt settings', () => {
    const item = node(); recordGeneratorSuccess(item, job(1)); recordGeneratorSuccess(item, job(2))
    item.generationSettings!.aspect_ratio = '1:1'
    expect(generatorFrameSize(item).width / generatorFrameSize(item).height).toBeCloseTo(600 / 900)
    selectGeneratorVersion(item, item.versions![1].id)
    expect(generatorFrameSize(item).width / generatorFrameSize(item).height).toBe(2)
    const reference: GeneratorDraft = { ...node(), nodeMode: 'reference', imageAssetId: 'local', imageWidth: 700, imageHeight: 1200 }
    expect(generatorFrameSize(reference, { width: 1920, height: 1080 }).width / generatorFrameSize(reference).height).toBeCloseTo(700 / 1200)
  })

  it('persists full history and selected version without serializing Blob previews', () => {
    const item = node(); recordGeneratorSuccess(item, job(1)); recordGeneratorSuccess(item, job(2)); selectGeneratorVersion(item, item.versions![1].id)
    const restored = readCanvasState(JSON.stringify({ generators: [item] }))!.generators![0]
    expect(restored.versions).toEqual(item.versions)
    expect(restored.activeVersionId).toBe(item.activeVersionId)
    expect(JSON.stringify(restored)).not.toContain('blob:')
    expect(activeGeneratorAsset(restored)).toEqual(activeGeneratorAsset(item))
  })

  it('rejects arbitrary remote or mismatched asset URLs and deduplicates successful assets', () => {
    const item = node(), delivered = job(1)
    expect(recordGeneratorSuccess(item, { ...delivered, assets: [{ ...delivered.assets[0], url: 'https://evil.example/api/generation-assets/' + delivered.assets[0].id }] })).toBe(false)
    expect(recordGeneratorSuccess(item, { ...delivered, assets: [{ ...delivered.assets[0], url: 'http://[' }] })).toBe(false)
    expect(recordGeneratorSuccess(item, { ...delivered, assets: [{ ...delivered.assets[0], mime_type: 'video/mp4' }] })).toBe(false)
    delivered.assets.push(job(2).assets[0], delivered.assets[0])
    expect(recordGeneratorSuccess(item, delivered)).toBe(true)
    expect(item.versions).toHaveLength(2)
    expect(readGeneratorVersions([null, {}, ...item.versions!, ...item.versions!], 'image')).toHaveLength(2)
  })

  it('records only real video outputs and retains their ratio across new settings', () => {
    const item: GeneratorDraft = { ...node(), kind: 'video', imageAssetId: 'local-first-frame', imageWidth: 1000, imageHeight: 500,
      generationSettings: defaultGenerationSettings('video') }
    const delivered = job(1)
    delivered.kind = 'video'; delivered.request.kind = 'video'; delivered.request.settings = defaultGenerationSettings('video')
    delivered.assets[0] = { ...delivered.assets[0], mime_type: 'video/mp4', width: 540, height: 960 }
    expect(recordGeneratorSuccess(item, delivered)).toBe(true)
    item.generationSettings!.aspect_ratio = '1:1'
    expect(activeGeneratorAsset(item)?.mime_type).toBe('video/mp4')
    expect(generatorFrameSize(item).width / generatorFrameSize(item).height).toBeCloseTo(9 / 16)
    expect(item.imageAssetId).toBe('local-first-frame')
  })

  it('migrates old upload-only nodes but preserves pending or submitted generator attempts', () => {
    const uploaded = { ...node(), nodeMode: undefined, imageAssetId: 'local', imageWidth: 400, imageHeight: 600 }
    expect(isReferenceAsset(uploaded)).toBe(true)
    expect(readCanvasState(JSON.stringify({ generators: [uploaded] }))!.generators![0].nodeMode).toBe('reference')
    expect(isReferenceAsset({ ...uploaded, generationTask: { jobId: 'generation-old', inputSignature: 'original' } })).toBe(false)
    expect(isReferenceAsset({ ...uploaded, pendingSubmission: { idempotencyKey: 'old-key', inputSignature: 'original' } })).toBe(false)
    expect(isReferenceAsset({ ...uploaded, kind: 'video', nodeMode: 'reference' })).toBe(false)
  })

  it('blocks generation inputs and parameter edits on uploaded reference assets', () => {
    const item: GeneratorDraft = { ...node(), nodeMode: 'reference', imageAssetId: 'local', imageWidth: 600, imageHeight: 900 }
    const kinds = connectionSources([{ id: 'prompt', type: 'first_frame' }], [], [item])
    expect(validateGeneratorConnection({ source: 'prompt', sourceHandle: 'prompt-out', target: item.id, targetHandle: 'input' }, [], [item], kinds).ok).toBe(false)
    const snapshot = vi.fn(), notify = vi.fn()
    const edits = useGeneratorEdits({ generators: ref([item]), connections: ref([]), sources: () => kinds, snapshot, changed: vi.fn(), notify, beforeRemove: vi.fn() })
    edits.settings(item.id, { ...defaultGenerationSettings('image'), aspect_ratio: '1:1' }); edits.prompt(item.id, '不要写入')
    expect(item.manualPrompt).toBe('当前未提交的草稿')
    expect(item.generationSettings?.aspect_ratio).toBe('16:9')
    expect(snapshot).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledTimes(2)
  })
})
