import { defineComponent, h, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../src/api/generations'
import { setApiProjectId } from '../src/api/client'
import { canonicalGeneratorIdentity, generatorInputSignature, scopeLegacyGeneratorSignature, useGeneratorTasks } from '../src/composables/useGeneratorTasks'
import { readCanvasState } from '../src/canvasPersistence'
import { createGeneratorResolutionGuard } from '../src/generatorGraph'
import { selectGeneratorVersion } from '../src/generatorVersions'
import type { GeneratorDraft, ResolvedGenerator } from '../src/generatorGraph'

vi.mock('../src/api/generations', async importOriginal => ({
  ...await importOriginal<typeof import('../src/api/generations')>(),
  getGenerationCapabilities: vi.fn(), getGeneration: vi.fn(), createGeneration: vi.fn(),
  preflightGeneration: vi.fn(), cancelGeneration: vi.fn(), retryGeneration: vi.fn(), uploadGenerationImage: vi.fn(),
}))

class Stream extends EventTarget {
  onmessage: ((message: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  close() { this.closed = true }
  emit(job: api.GenerationJob, sequence: number, type = 'generation.progress') {
    this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ job_id: job.id, sequence, type, payload: { job } }), lastEventId: String(sequence) }))
  }
}
const draft = (): GeneratorDraft => ({ id: 'gen-a', kind: 'image', position: { x: 0, y: 0 }, manualPrompt: '雨夜中的街道', imageAssetId: null, imageWidth: 0, imageHeight: 0 })
const resolved = (item: GeneratorDraft): ResolvedGenerator => ({ prompt: item.manualPrompt, promptSource: '手动输入', imageUrl: '', imageSource: '', imageWidth: 0, imageHeight: 0, ownImageUrl: '', ownImageWidth: 0, ownImageHeight: 0 })
function job(status: api.GenerationStatus = 'blocked', revision = 2, options: Partial<api.GenerationJob> = {}): api.GenerationJob {
  return { id: 'generation-a', node_id: 'gen-a', kind: 'image', status, revision, stage: status === 'blocked' ? 'awaiting_provider' : status,
    progress: null, provider: 'disabled', input_hash: 'hash', assets: [], request: { node_id: 'gen-a', kind: 'image', prompt: '雨夜中的街道', settings: api.defaultGenerationSettings('image') },
    error: status === 'blocked' ? { code: 'provider_unconfigured', message: '生成服务未配置', retryable: true } : null,
    created_at: '2026-11-10T10:00:00Z', updated_at: '2026-11-10T10:00:01Z', ...options }
}
const wrappers: ReturnType<typeof mount>[] = []
function harness(items = [draft()], resolver = resolved, options: { projectId?: () => string | null } = {}) {
  const generators = ref(items)
  const sources: Stream[] = [], urls: string[] = []
  const changed = vi.fn(), notify = vi.fn()
  let tasks!: ReturnType<typeof useGeneratorTasks>
  const wrapper = mount(defineComponent({ setup() {
    tasks = useGeneratorTasks(generators, id => resolver(generators.value.find(item => item.id === id)!), changed, notify,
      url => { urls.push(url); const stream = new Stream(); sources.push(stream); return stream as unknown as EventSource }, options)
    return () => h('div')
  } }))
  wrappers.push(wrapper)
  return { wrapper, generators, tasks, sources, urls, changed, notify }
}
beforeEach(() => {
  vi.mocked(api.getGenerationCapabilities).mockResolvedValue({ configured: false, provider: 'disabled', reason: '生成 API 待接入' })
  vi.mocked(api.preflightGeneration).mockResolvedValue({ valid: true, can_submit: false, configured: false, issues: [], reason: '生成 API 待接入' })
  vi.mocked(api.createGeneration).mockResolvedValue(job())
  vi.mocked(api.cancelGeneration).mockImplementation(async id => job('cancelled', 8, { id }))
  vi.mocked(api.getGeneration).mockResolvedValue(job('running', 4))
  vi.mocked(api.retryGeneration).mockResolvedValue(job('blocked', 2, { id: 'generation-retry', retry_of: 'generation-a' }))
  Object.defineProperty(document, 'hidden', { value: false, configurable: true })
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals(); setApiProjectId('') })

describe('backend-owned generation node tasks', () => {
  it('scopes a saved legacy signature without turning modified inputs into a clean snapshot', () => {
    const item = draft()
    const original = { ...resolved(item), imageUrl: 'http://127.0.0.1:8000/api/jobs/j/frames/f?download=1#focus', imageIdentity: 'http://127.0.0.1:8000/api/jobs/j/frames/f?download=1#focus' }
    const saved = generatorInputSignature(item, original)
    const migrated = scopeLegacyGeneratorSignature(saved, 'project-a')
    const current = { ...original, imageUrl: 'http://127.0.0.1:8000/api/jobs/j/frames/f?project_id=project-a&download=1#focus', imageIdentity: 'http://127.0.0.1:8000/api/jobs/j/frames/f?project_id=project-a&download=1#focus' }
    expect(migrated).toBe(generatorInputSignature(item, current, 'project-a'))
    expect(migrated).not.toBe(generatorInputSignature(item, { ...current, prompt: '用户后来修改的 Prompt' }, 'project-a'))
    expect(JSON.parse(migrated).prompt).toBe(item.manualPrompt)
    expect(scopeLegacyGeneratorSignature(migrated, 'project-b')).toBe(migrated)
    expect(scopeLegacyGeneratorSignature('not-json', 'project-a')).toBe('not-json')
    expect(canonicalGeneratorIdentity('/api/jobs/j/frames/f?foo=a%20b&project_id=p&size=2#x')).toBe('/api/jobs/j/frames/f?foo=a%20b&size=2#x')
  })
  it('normalizes project query scope for every saved reference identity without changing selections', () => {
    const item = draft()
    const references = [1, 2].map(index => ({ id: 'ref-00000000000' + index, mention: '图片' + index, url: '/frame-' + index,
      width: 100, height: 100, identity: '/api/jobs/j/frames/f' + index + '?project_id=project-a', source: '参考帧', selected: index === 1, firstFrame: false }))
    const observed = { ...resolved(item), imageIdentity: '/api/jobs/j/frames/f1?project_id=project-a', references }
    const snapshot = JSON.parse(generatorInputSignature(item, observed))
    snapshot.image += '?project_id=project-a'
    snapshot.references[0].identity += '?project_id=project-a'
    const scoped = scopeLegacyGeneratorSignature(JSON.stringify(snapshot), 'project-a')
    expect(scoped).toBe(generatorInputSignature(item, observed, 'project-a'))
    expect(JSON.parse(scoped).references[1].selected).toBe(false)
    expect(scoped).not.toBe(generatorInputSignature(item, { ...observed, references: references.map(reference => ({ ...reference, selected: true })) }, 'project-a'))
  })
  it('replays a scoped legacy pending single-image request with its original key and payload shape', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => new Blob(['png'], { type: 'image/png' }) })))
    const assetId = 'genasset-' + 'c'.repeat(64)
    vi.mocked(api.uploadGenerationImage).mockResolvedValue({ id: assetId, mime_type: 'image/png', url: '/api/generation-assets/' + assetId, width: 100, height: 100 })
    vi.mocked(api.createGeneration).mockResolvedValue(job('blocked', 2, { project_id: 'project-a' }))
    const item = { ...draft(), imageAssetId: 'legacy-image' }
    const before = { ...resolved(item), imageUrl: 'blob:old', imageIdentity: 'local:legacy-image' }
    item.pendingSubmission = { idempotencyKey: 'original-pending-key', inputSignature: scopeLegacyGeneratorSignature(generatorInputSignature(item, before), 'project-a') }
    const current = { ...before, imageUrl: 'blob:restored', references: [{ id: 'ref-cccccccccccc', mention: '图片1', url: 'blob:restored', width: 100, height: 100,
      identity: 'local:legacy-image', source: '本地导入图片', selected: true }] }
    const view = harness([item], () => current, { projectId: () => 'project-a' })
    await view.tasks.submit(item.id)
    const [payload, key] = vi.mocked(api.createGeneration).mock.calls[0]
    expect(key).toBe('original-pending-key')
    expect(payload.reference_asset_id).toBe(assetId)
    expect(payload).not.toHaveProperty('reference_images')
    expect(payload.project_id).toBe('project-a')
    expect(item.pendingSubmission).toBeUndefined()
  })
  it('terminates corrupt cyclic output enrichment and clears the guard after errors', () => {
    const guard = createGeneratorResolutionGuard()
    const a = (): string => guard('a', 'raw-a', () => b())
    const b = (): string => guard('b', 'raw-b', () => a())
    expect(a()).toBe('raw-a')
    expect(() => guard('a', '', () => { throw new Error('invalid') })).toThrow('invalid')
    expect(guard('a', '', () => 'recovered')).toBe('recovered')
  })
  it('saves a blocked task without synthetic progress, media or a second submit', async () => {
    const view = harness()
    await view.tasks.submit('gen-a')
    expect(api.preflightGeneration).toHaveBeenCalledOnce()
    expect(api.createGeneration).toHaveBeenCalledOnce()
    expect(view.tasks.jobs.value['gen-a']).toMatchObject({ status: 'blocked', progress: null, assets: [] })
    expect(view.tasks.outputFor('gen-a')).toBeNull()
    expect(view.generators.value[0].imageAssetId).toBeNull()
    expect(view.sources).toHaveLength(0) // Terminal snapshots need no live stream.
    expect(view.tasks.streamCount()).toBe(0)
    await view.tasks.submit('gen-a')
    expect(api.createGeneration).toHaveBeenCalledOnce()
  })
  it('rejects empty Prompt and missing video first frame before upload or request', async () => {
    const view = harness([{ ...draft(), kind: 'video', manualPrompt: '' }])
    await view.tasks.submit('gen-a')
    view.generators.value[0].manualPrompt = '女子走入画面'
    await view.tasks.submit('gen-a')
    expect(api.createGeneration).not.toHaveBeenCalled()
    expect(api.uploadGenerationImage).not.toHaveBeenCalled()
    expect(view.tasks.errors.value['gen-a']).toContain('首帧')
  })
  it.each([4, 5.5, 16])('rejects a new %ss video before uploading its first frame', async duration => {
    const item: GeneratorDraft = { ...draft(), kind: 'video', generationSettings: { ...api.defaultGenerationSettings('video'), duration_seconds: duration } }
    const view = harness([item], current => ({ ...resolved(current), imageUrl: 'blob:first-frame' }))
    await view.tasks.submit(item.id)
    expect(api.uploadGenerationImage).not.toHaveBeenCalled()
    expect(api.preflightGeneration).not.toHaveBeenCalled()
    expect(api.createGeneration).not.toHaveBeenCalled()
    expect(view.tasks.errors.value[item.id]).toContain('5–15 秒的整数')
    expect(item.generationSettings?.duration_seconds).toBe(duration)
  })
  it('recovers a historical fractional pending submit with its unchanged key and snapshot', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => new Blob(['png'], { type: 'image/png' }) })))
    const assetId = 'genasset-' + 'e'.repeat(64)
    vi.mocked(api.uploadGenerationImage).mockResolvedValue({ id: assetId, mime_type: 'image/png', url: '/api/generation-assets/' + assetId, width: 100, height: 100 })
    const item: GeneratorDraft = { ...draft(), kind: 'video', imageAssetId: 'old-image', generationSettings: { ...api.defaultGenerationSettings('video'), duration_seconds: 4.5 } }
    const reference = (current: GeneratorDraft) => ({ ...resolved(current), imageUrl: 'blob:restored', imageIdentity: 'local:old-image' })
    const signature = generatorInputSignature(item, reference(item))
    item.pendingSubmission = { idempotencyKey: 'old-fractional-key', inputSignature: signature }
    const oldJob = job('blocked', 2, { kind: 'video', request: { node_id: item.id, kind: 'video', prompt: item.manualPrompt, reference_asset_id: assetId, settings: item.generationSettings! } })
    vi.mocked(api.createGeneration).mockResolvedValue(oldJob)
    const view = harness([item], reference)
    expect(view.tasks.canRecoverSubmission(item.id)).toBe(true)
    await view.tasks.submit(item.id)
    expect(api.preflightGeneration).not.toHaveBeenCalled()
    expect(api.createGeneration).toHaveBeenCalledWith(oldJob.request, 'old-fractional-key')
    expect(item.generationTask?.inputSignature).toBe(signature)
    expect(item.generationSettings?.duration_seconds).toBe(4.5)
    expect(item.pendingSubmission).toBeUndefined()
  })
  it('does not treat changed legacy pending inputs as a recoverable request or replace its key', async () => {
    const item: GeneratorDraft = { ...draft(), kind: 'video', generationSettings: { ...api.defaultGenerationSettings('video'), duration_seconds: 4.5 } }
    const reference = (current: GeneratorDraft) => ({ ...resolved(current), imageUrl: 'blob:first-frame' })
    item.pendingSubmission = { idempotencyKey: 'unchanged-key', inputSignature: generatorInputSignature(item, reference(item)) }
    item.manualPrompt = '用户已修改'
    const view = harness([item], reference)
    expect(view.tasks.canRecoverSubmission(item.id)).toBe(false)
    await view.tasks.submit(item.id)
    expect(api.createGeneration).not.toHaveBeenCalled()
    expect(item.pendingSubmission.idempotencyKey).toBe('unchanged-key')
  })
  it('uploads selected reference bindings to the current project and preserves the chosen first frame', async () => {
    const blob = new Blob(['image'], { type: 'image/png' })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => blob })))
    const assetA = 'genasset-' + 'a'.repeat(64), assetB = 'genasset-' + 'b'.repeat(64)
    vi.mocked(api.createGeneration).mockResolvedValue(job('blocked', 2, { project_id: 'project-a' }))
    vi.mocked(api.uploadGenerationImage).mockResolvedValueOnce({ id: assetA, mime_type: 'image/png', url: '/api/generation-assets/' + assetA, width: 100, height: 100 })
    vi.mocked(api.uploadGenerationImage).mockResolvedValueOnce({ id: assetB, mime_type: 'image/png', url: '/api/generation-assets/' + assetB, width: 100, height: 100 })
    const item = { ...draft(), kind: 'video' as const, manualPrompt: '使用 @图片2 作为首帧' }
    const references = [1, 2].map(index => ({ id: 'ref-00000000000' + index, mention: '图片' + index, url: 'blob:ref-' + index,
      width: 100, height: 100, identity: 'local:ref-' + index, source: '本地导入图片', selected: true, firstFrame: index === 2 }))
    const view = harness([item], current => ({ ...resolved(current), imageUrl: 'blob:ref-2', references }), { projectId: () => 'project-a' })
    await view.tasks.submit(item.id)
    expect(api.uploadGenerationImage).toHaveBeenNthCalledWith(1, blob, 'project-a')
    expect(vi.mocked(api.createGeneration).mock.calls[0][0]).toMatchObject({ project_id: 'project-a', reference_asset_id: assetB,
      reference_images: [{ reference_id: references[0].id, mention: '图片1', asset_id: assetA, role: 'reference' },
        { reference_id: references[1].id, mention: '图片2', asset_id: assetB, role: 'first_frame' }] })
  })
  it('never restores, outputs or cancels a cached attempt from a different project', async () => {
    const item = draft()
    item.generationTask = { jobId: 'generation-a', inputSignature: 'old-project' }
    vi.mocked(api.getGeneration).mockResolvedValue(job('running', 4, { project_id: 'project-a' }))
    const view = harness([item], resolved, { projectId: () => 'project-b' })
    await view.tasks.restore()
    expect(view.tasks.jobs.value[item.id]).toBeUndefined()
    expect(view.tasks.errors.value[item.id]).toContain('不属于当前项目')
    view.tasks.detach(item.id)
    expect(api.cancelGeneration).not.toHaveBeenCalled()
    expect(view.tasks.outputFor(item.id)).toBeNull()
  })
  it('does not upload or submit when a referenced picture was removed or deselected', async () => {
    const item = { ...draft(), manualPrompt: '使用 @图片1' }
    const view = harness([item], current => ({ ...resolved(current), references: [] }))
    await view.tasks.submit(item.id)
    expect(api.uploadGenerationImage).not.toHaveBeenCalled()
    expect(api.createGeneration).not.toHaveBeenCalled()
    expect(view.tasks.errors.value[item.id]).toContain('@图片1')
  })
  it('uses job-scoped revision and cursor, ignoring cross-node, duplicate and stale events', async () => {
    vi.mocked(api.createGeneration).mockResolvedValue(job('queued', 1))
    const view = harness()
    await view.tasks.submit('gen-a')
    const stream = view.sources[0]
    stream.emit(job('running', 3, { node_id: 'gen-b', progress: 25 }), 3)
    expect(view.tasks.jobs.value['gen-a'].status).toBe('queued')
    stream.emit(job('running', 3, { progress: 25 }), 3)
    stream.emit(job('running', 2, { progress: 10 }), 4)
    stream.emit(job('failed', 2), 5, 'generation.failed')
    expect(view.tasks.jobs.value['gen-a'].progress).toBe(25)
    expect(stream.closed).toBe(false)
    stream.emit(job('succeeded', 7, { progress: 100 }), 7, 'generation.succeeded')
    expect(view.tasks.jobs.value['gen-a'].status).toBe('succeeded')
    expect(stream.closed).toBe(true)
  })
  it('keeps null progress indeterminate and accepts submitting/finalizing snapshots', async () => {
    vi.mocked(api.createGeneration).mockResolvedValue(job('queued', 1))
    const view = harness()
    await view.tasks.submit('gen-a')
    view.sources[0].emit(job('submitting', 2), 2, 'generation.submitting')
    expect(view.tasks.jobs.value['gen-a'].progress).toBeNull()
    view.sources[0].emit(job('finalizing', 3, { progress: 94 }), 3, 'generation.finalizing')
    expect(view.tasks.jobs.value['gen-a'].status).toBe('finalizing')
  })
  it('uses one project stream for multiple nodes and keeps it open until every active job finishes', async () => {
    vi.mocked(api.createGeneration).mockImplementation(async request => job('queued', 1, {
      id: 'generation-' + request.node_id, node_id: request.node_id, request, project_id: 'project-a',
    }))
    const view = harness([draft(), { ...draft(), id: 'gen-b' }], resolved, { projectId: () => 'project-a' })
    await Promise.all([view.tasks.submit('gen-a'), view.tasks.submit('gen-b')])
    expect(view.sources).toHaveLength(1)
    expect(view.tasks.streamCount()).toBe(1)
    expect(view.urls[0]).toContain('/generations/events?')
    expect(new URL(view.urls[0]).searchParams.get('project_id')).toBe('project-a')
    const source = view.sources[0]
    const a = view.tasks.jobs.value['gen-a'], b = view.tasks.jobs.value['gen-b']
    source.emit({ ...a, status: 'running', revision: 2, progress: 10 }, 1)
    source.emit({ ...b, status: 'running', revision: 2, progress: 40 }, 2)
    expect(view.tasks.jobs.value['gen-a'].progress).toBe(10)
    expect(view.tasks.jobs.value['gen-b'].progress).toBe(40)
    source.emit({ ...a, status: 'succeeded', revision: 3, progress: 100 }, 3, 'generation.succeeded')
    expect(source.closed).toBe(false)
    source.emit({ ...b, status: 'failed', revision: 3 }, 4, 'generation.failed')
    expect(source.closed).toBe(true)
    expect(view.tasks.streamCount()).toBe(0)
  })
  it('captures an explicit project or legacy scope instead of inheriting an unrelated active project', () => {
    setApiProjectId('project-from-global-ui')
    const project = new URL(api.projectGenerationEventsUrl('project-request', 14))
    expect(project.searchParams.getAll('project_id')).toEqual(['project-request'])
    expect(project.searchParams.get('after')).toBe('14')
    const legacy = new URL(api.projectGenerationEventsUrl(null, 7))
    expect(legacy.searchParams.has('project_id')).toBe(false)
    expect(legacy.searchParams.get('scope')).toBe('legacy')
    expect(legacy.searchParams.get('after')).toBe('7')
  })
  it('replays from the oldest saved node cursor when restoring image and video jobs together', async () => {
    const a = { ...draft(), generationTask: { jobId: 'generation-a', inputSignature: 'a', eventCursor: 9 } }
    const b = { ...draft(), id: 'gen-b', kind: 'video' as const, generationTask: { jobId: 'generation-b', inputSignature: 'b', eventCursor: 3 } }
    vi.mocked(api.getGeneration).mockImplementation(async id => job('running', 4, {
      id, node_id: id === 'generation-a' ? a.id : b.id, kind: id === 'generation-a' ? 'image' : 'video', project_id: 'project-a',
    }))
    const view = harness([a, b], resolved, { projectId: () => 'project-a' })
    await view.tasks.restore()
    expect(view.sources).toHaveLength(1)
    expect(new URL(view.urls[0]).searchParams.get('after')).toBe('3')
    expect(api.createGeneration).not.toHaveBeenCalled()
    view.sources[0].emit({ ...view.tasks.jobs.value[b.id], revision: 5, progress: 65 }, 10)
    expect(view.tasks.jobs.value[b.id].progress).toBe(65)
    expect(view.tasks.jobs.value[a.id].progress).toBeNull()
  })
  it('repairs all active snapshots after a stream error without opening more connections or resubmitting', async () => {
    vi.mocked(api.createGeneration).mockImplementation(async request => job('queued', 1, {
      id: 'generation-' + request.node_id, node_id: request.node_id, request,
    }))
    const view = harness([draft(), { ...draft(), id: 'gen-b' }])
    await Promise.all([view.tasks.submit('gen-a'), view.tasks.submit('gen-b')])
    vi.mocked(api.getGeneration).mockImplementation(async id => job('running', 4, {
      id, node_id: id.replace('generation-', ''), progress: 60,
    }))
    view.sources[0].onerror!()
    view.sources[0].onerror!()
    await flushPromises()
    expect(api.getGeneration).toHaveBeenCalledTimes(2)
    expect(view.sources).toHaveLength(1)
    expect(api.createGeneration).toHaveBeenCalledTimes(2)
    expect(Object.values(view.tasks.jobs.value).map(item => item.progress)).toEqual([60, 60])
  })
  it('keeps a fast completion published before a second node receives its POST response', async () => {
    vi.mocked(api.createGeneration).mockResolvedValueOnce(job('running', 2))
    const view = harness([draft(), { ...draft(), id: 'gen-b' }])
    await view.tasks.submit('gen-a')
    let finish!: (job: api.GenerationJob) => void
    vi.mocked(api.createGeneration).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const submitting = view.tasks.submit('gen-b')
    await flushPromises()
    const b = job('queued', 1, { id: 'generation-b', node_id: 'gen-b' })
    view.sources[0].emit({ ...b, status: 'succeeded', revision: 5, progress: 100 }, 5, 'generation.succeeded')
    expect(view.tasks.jobs.value['gen-b']).toBeUndefined()
    finish(b)
    await submitting
    expect(view.sources).toHaveLength(1)
    expect(view.tasks.jobs.value['gen-b'].status).toBe('succeeded')
    expect(view.generators.value[1].generationTask?.eventCursor).toBe(5)
    expect(view.tasks.jobs.value['gen-a'].status).toBe('running')
  })
  it('rejects cross-project events without consuming the valid project cursor', async () => {
    vi.mocked(api.createGeneration).mockResolvedValue(job('queued', 1, { project_id: 'project-a' }))
    const view = harness([draft()], resolved, { projectId: () => 'project-a' })
    await view.tasks.submit('gen-a')
    view.sources[0].emit(job('running', 9, { project_id: 'project-b', progress: 95 }), 999)
    view.sources[0].emit(job('running', 2, { project_id: 'project-a', progress: 20 }), 2)
    expect(view.tasks.jobs.value['gen-a'].progress).toBe(20)
    expect(view.generators.value[0].generationTask?.eventCursor).toBe(2)
  })
  it('discards late REST and SSE from the previous project after switching projects', async () => {
    const project = ref('project-a')
    const a = { ...draft(), generationTask: { jobId: 'generation-a', inputSignature: 'a', eventCursor: 2 } }
    vi.mocked(api.createGeneration).mockResolvedValueOnce(job('running', 3, { project_id: 'project-a' }))
    const view = harness([a], resolved, { projectId: () => project.value })
    await view.tasks.submit(a.id)
    let finish!: (job: api.GenerationJob) => void
    vi.mocked(api.getGeneration).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const restoring = view.tasks.restore()
    project.value = 'project-b'
    await flushPromises()
    expect(view.sources[0].closed).toBe(true)
    finish(job('succeeded', 9, { project_id: 'project-a', progress: 100 }))
    await restoring
    view.sources[0].emit(job('succeeded', 10, { project_id: 'project-a', progress: 100 }), 10, 'generation.succeeded')
    expect(view.tasks.jobs.value).toEqual({})
    expect(view.tasks.errors.value).toEqual({})
    expect(view.tasks.streamCount()).toBe(0)
  })
  it('reuses a persisted pending idempotency key after lost response and refresh', async () => {
    vi.mocked(api.createGeneration).mockRejectedValueOnce(new Error('响应丢失'))
    const first = harness()
    await first.tasks.submit('gen-a')
    const key = vi.mocked(api.createGeneration).mock.calls[0][1]
    expect(first.generators.value[0].pendingSubmission?.idempotencyKey).toBe(key)
    const stored = readCanvasState(JSON.stringify({ generators: first.generators.value }))!
    first.wrapper.unmount()
    const next = harness(stored.generators)
    await flushPromises()
    expect(api.createGeneration).toHaveBeenCalledTimes(1) // restoration never submits
    await next.tasks.submit('gen-a')
    expect(vi.mocked(api.createGeneration).mock.calls[1][1]).toBe(key)
    expect(next.generators.value[0].pendingSubmission).toBeUndefined()
    expect(next.generators.value[0].generationTask?.jobId).toBe('generation-a')
  })
  it('marks edited inputs stale without changing prior task snapshot or reference images', async () => {
    const view = harness()
    await view.tasks.submit('gen-a')
    view.generators.value[0].manualPrompt = '改为白天'
    expect(view.tasks.isStale('gen-a')).toBe(true)
    expect(view.tasks.jobs.value['gen-a'].request.prompt).toBe('雨夜中的街道')
    await view.tasks.retry('gen-a')
    expect(api.retryGeneration).not.toHaveBeenCalled()
    expect(api.createGeneration).toHaveBeenCalledOnce()
  })
  it('explicit retry creates a separate attempt and rejects old project-stream events', async () => {
    vi.mocked(api.createGeneration).mockResolvedValue(job('running', 3))
    const view = harness()
    await view.tasks.submit('gen-a')
    view.sources[0].emit(job('failed', 4), 4, 'generation.failed')
    const oldStream = view.sources[0]
    await view.tasks.retry('gen-a')
    expect(api.retryGeneration).toHaveBeenCalledOnce()
    expect(view.generators.value[0].generationTask?.jobId).toBe('generation-retry')
    expect(oldStream.closed).toBe(true)
    oldStream.emit(job('succeeded', 90), 90, 'generation.succeeded')
    expect(view.tasks.jobs.value['gen-a'].id).toBe('generation-retry')
  })
  it('can cancel a saved blocked task without claiming generation success', async () => {
    const view = harness()
    await view.tasks.submit('gen-a')
    await view.tasks.cancel('gen-a')
    expect(view.tasks.jobs.value['gen-a'].status).toBe('cancelled')
    expect(view.tasks.outputFor('gen-a')).toBeNull()
  })
  it('reuses the explicit retry key after a lost retry response', async () => {
    const view = harness()
    await view.tasks.submit('gen-a')
    vi.mocked(api.retryGeneration).mockRejectedValueOnce(new Error('response lost'))
    await view.tasks.retry('gen-a')
    const key = view.generators.value[0].pendingSubmission?.idempotencyKey
    expect(key).toBeTruthy()
    await view.tasks.retry('gen-a')
    expect(vi.mocked(api.retryGeneration).mock.calls.slice(-2).map(call => call[1])).toEqual([key, key])
  })
  it('deleting a node during submission cancels late active work and never resurrects it', async () => {
    let finish!: (value: api.GenerationJob) => void
    vi.mocked(api.createGeneration).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const view = harness()
    const waiting = view.tasks.submit('gen-a')
    await flushPromises()
    view.tasks.detach('gen-a')
    view.generators.value = []
    finish(job('queued', 1))
    await waiting
    expect(api.cancelGeneration).toHaveBeenCalledWith('generation-a')
    expect(view.tasks.jobs.value['gen-a']).toBeUndefined()
    expect(view.tasks.streamCount()).toBe(0)
  })
  it('undo restoration reads the cancelled backend attempt without resubmitting it', async () => {
    vi.mocked(api.createGeneration).mockResolvedValue(job('running', 3))
    const view = harness()
    await view.tasks.submit('gen-a')
    const saved = JSON.parse(JSON.stringify(view.generators.value))
    view.tasks.detach('gen-a')
    view.generators.value = []
    await flushPromises()
    vi.mocked(api.getGeneration).mockResolvedValue(job('cancelled', 8))
    view.generators.value = saved
    await view.tasks.restore()
    expect(api.createGeneration).toHaveBeenCalledOnce()
    expect(view.tasks.jobs.value['gen-a'].status).toBe('cancelled')
    expect(view.tasks.streamCount()).toBe(0)
  })
  it('restores via REST and cursor, pauses hidden streams, and disposes listeners', async () => {
    const item = draft()
    item.generationTask = { jobId: 'generation-a', inputSignature: generatorInputSignature(item, resolved(item)), eventCursor: 4 }
    const view = harness([item])
    await view.tasks.restore()
    expect(api.createGeneration).not.toHaveBeenCalled()
    expect(view.urls[0]).toContain('after=4')
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    expect(view.sources[0].closed).toBe(true)
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(view.tasks.streamCount()).toBe(1)
    view.wrapper.unmount()
    expect(view.tasks.streamCount()).toBe(0)
  })
  it('keeps a delivered output while editing the next draft and rejects external artifact URLs', async () => {
    const id = 'genasset-' + 'a'.repeat(64)
    const asset = { id, mime_type: 'image/png', url: '/api/generation-assets/' + id, width: 1920, height: 1080 }
    vi.mocked(api.createGeneration).mockResolvedValue(job('succeeded', 3, { assets: [asset] }))
    const view = harness()
    await view.tasks.submit('gen-a')
    expect(view.tasks.outputFor('gen-a')?.url).toContain('/api/generation-assets/' + id)
    view.generators.value[0].manualPrompt = '新的意图'
    expect(view.tasks.isStale('gen-a')).toBe(true)
    expect(view.tasks.outputFor('gen-a')?.id).toBe(id)
    expect(view.generators.value[0].versions?.[0].prompt).toBe('雨夜中的街道')
    expect(api.generationAssetUrl('https://untrusted.example/image.png')).toBe('')
  })

  it('never submits an uploaded reference asset as a generation task', async () => {
    const view = harness([{ ...draft(), nodeMode: 'reference', imageAssetId: 'image-upload', imageWidth: 100, imageHeight: 150 }])
    await view.tasks.submit('gen-a')
    expect(api.createGeneration).not.toHaveBeenCalled()
    expect(api.uploadGenerationImage).not.toHaveBeenCalled()
    expect(api.preflightGeneration).not.toHaveBeenCalled()
    expect(view.notify).toHaveBeenCalledWith(expect.stringContaining('参考资产'))
    expect(view.tasks.outputFor('gen-a')).toBeNull()
  })

  it('adds only delivered SSE versions, keeps old media during rendering and preserves the editable draft when pinning history', async () => {
    const asset = (letter: string) => ({ id: 'genasset-' + letter.repeat(64), mime_type: 'image/png',
      url: '/api/generation-assets/genasset-' + letter.repeat(64), width: 1920, height: 1080 })
    const first = job('succeeded', 3, { assets: [asset('a')] })
    vi.mocked(api.createGeneration).mockResolvedValueOnce(first)
    const view = harness()
    await view.tasks.submit('gen-a')
    const initialVersion = view.generators.value[0].activeVersionId!
    view.generators.value[0].manualPrompt = '改为白天的街道'
    const nextRequest = { ...first.request, prompt: '改为白天的街道' }
    const second = job('running', 2, { id: 'generation-b', request: nextRequest, updated_at: '2026-11-10T10:01:00Z' })
    vi.mocked(api.createGeneration).mockResolvedValueOnce(second)
    await view.tasks.submit('gen-a')
    expect(view.generators.value[0].versions).toHaveLength(1)
    expect(view.tasks.outputFor('gen-a')?.id).toBe(asset('a').id)
    const delivered = { ...second, revision: 3, status: 'succeeded' as const, assets: [asset('b')], updated_at: '2026-11-10T10:02:00Z' }
    view.sources.at(-1)!.emit(delivered, 3, 'generation.succeeded')
    expect(view.generators.value[0].versions).toHaveLength(2)
    expect(view.tasks.outputFor('gen-a')?.id).toBe(asset('b').id)
    expect(view.generators.value[0].versions?.[0].prompt).toBe(nextRequest.prompt)
    selectGeneratorVersion(view.generators.value[0], initialVersion)
    expect(view.tasks.outputFor('gen-a')?.id).toBe(asset('a').id)
    expect(view.generators.value[0].manualPrompt).toBe(nextRequest.prompt)
    vi.mocked(api.getGeneration).mockResolvedValue(delivered)
    await view.tasks.restore()
    expect(view.generators.value[0].versions).toHaveLength(2)
    expect(view.generators.value[0].activeVersionId).toBe(initialVersion)
    const stored = readCanvasState(JSON.stringify({ generators: view.generators.value }))!
    const restored = harness(stored.generators)
    await restored.tasks.restore()
    expect(restored.tasks.outputFor('gen-a')?.id).toBe(asset('a').id)
    expect(restored.generators.value[0].manualPrompt).toBe(nextRequest.prompt)
    expect(api.createGeneration).toHaveBeenCalledTimes(2)
  })

  it('does not replace delivered media when the following attempt fails', async () => {
    const id = 'genasset-' + 'c'.repeat(64)
    vi.mocked(api.createGeneration).mockResolvedValueOnce(job('succeeded', 3, { assets: [{ id, mime_type: 'image/png', url: '/api/generation-assets/' + id, width: 100, height: 200 }] }))
    const view = harness()
    await view.tasks.submit('gen-a')
    vi.mocked(api.createGeneration).mockResolvedValueOnce(job('failed', 4, { id: 'generation-failed', error: { code: 'test', message: '服务暂不可用', retryable: true } }))
    await view.tasks.submit('gen-a')
    expect(view.generators.value[0].versions).toHaveLength(1)
    expect(view.tasks.outputFor('gen-a')?.id).toBe(id)
  })
})
