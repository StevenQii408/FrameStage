import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App.vue'
import { defaultGenerationSettings } from '../src/api/generations'
import { saveLocalImage } from '../src/generatorStorage'
import type { GeneratorDraft } from '../src/generatorGraph'

const flow = vi.hoisted(() => ({ setViewport: vi.fn(), updateNodeInternals: vi.fn(), fitView: vi.fn() }))
const localBlobs = vi.hoisted(() => new Map<string, Blob>())

vi.mock('@vue-flow/core', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    Position: { Left: 'left', Right: 'right' }, SelectionMode: { Full: 'full', Partial: 'partial' },
    Handle: defineComponent({ setup: () => () => null }),
    useVueFlow: () => ({ ...flow, screenToFlowCoordinate: (point: unknown) => point }),
    VueFlow: defineComponent({ name: 'VueFlow', props: ['nodes', 'edges'], setup(props, { slots }) {
      return () => h('div', { class: 'vue-flow__pane' }, (props.nodes || []).filter((node: any) => node.type === 'generator').map((node: any) => h('div', {
        key: node.id, class: 'vue-flow__node', 'data-id': node.id,
      }, slots['node-generator']?.(node))))
    } }),
  }
})
vi.mock('../src/generatorStorage', () => ({
  loadLocalImage: vi.fn(async (id: string) => localBlobs.get(id) || null),
  saveLocalImage: vi.fn(async (id: string, blob: Blob) => { localBlobs.set(id, blob) }),
  validateLocalImage: vi.fn(() => null), imageDimensions: vi.fn(async () => ({ width: 1920, height: 1080 })),
}))
vi.mock('../src/api/generations', async importOriginal => ({
  ...await importOriginal<typeof import('../src/api/generations')>(),
  getGenerationCapabilities: vi.fn(async () => ({ configured: false, provider: 'disabled', reason: '测试未接入' })),
  getGeneration: vi.fn(async () => { throw new Error('离线恢复夹具') }),
}))
vi.mock('../src/api/jobs', () => ({ getJobResult: vi.fn(), jobEventsUrl: vi.fn(), cancelJob: vi.fn(),
  createScriptJob: vi.fn(), createVideoJob: vi.fn(), downloadJobExport: vi.fn(), resumeJob: vi.fn() }))
vi.mock('../src/components/CanvasVideoPlayer.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { __esModule: true, default: defineComponent({ name: 'CanvasVideoPlayer', props: ['src', 'captureEnabled', 'autoplay'], emits: ['capture'],
    setup(props, { emit, expose }) {
      expose({ pause: vi.fn() })
      return () => h('div', { 'data-testid': 'node-video-player' }, [h('video', { src: props.src }),
        h('button', { 'aria-label': '测试截取当前帧', onClick: () => emit('capture', {
          blob: new Blob(['decoded-pixels'], { type: 'image/png' }), width: 1920, height: 1080, timestamp: 2.25, source: 'current',
        }) }, '截取')])
    } }) }
})

const InlineEditorStub = defineComponent({ name: 'GeneratorInlinePanel', props: ['generator'], setup: () => () => h('div', { 'data-testid': 'inline-editor' }) })
const wrappers: ReturnType<typeof mount>[] = []
const originalViewport = { x: -20, y: 60, zoom: .4 }

function generated(id: string, kind: 'image' | 'video', digit: string, x: number): GeneratorDraft {
  const settings = { ...defaultGenerationSettings(kind), aspect_ratio: '16:9' as const }
  const assetId = 'genasset-' + digit.repeat(64)
  return { id, kind, position: { x, y: 180 }, nodeMode: 'generator', manualPrompt: '保留可编辑 Prompt',
    imageAssetId: null, imageWidth: 0, imageHeight: 0, generationSettings: settings,
    versions: [{ id: id + '-version', jobId: 'generation-' + id, createdAt: '2026-11-11T00:00:00Z', prompt: '原始生成 Prompt', settings,
      asset: { id: assetId, url: '/api/generation-assets/' + assetId, mime_type: kind === 'video' ? 'video/mp4' : 'image/png', width: 1920, height: 1080 } }],
    activeVersionId: id + '-version' }
}

async function mounted() {
  const originals = [generated('gen-image', 'image', 'a', 120), generated('gen-video', 'video', 'b', 540)]
  const wrapper = mount(App, { props: { projectId: 'preview-project', initialCanvas: {
    jobKeys: {}, positions: {}, sceneUi: {}, generators: originals, generatorConnections: [], selectedId: '', activeTaskKey: '',
    viewport: originalViewport, activeView: 'canvas', layoutVersion: 'project-canvas.v2',
  } }, global: { plugins: [createPinia()], stubs: { Background: true, TacticalMiniMap: true, WorkflowRail: true,
    ProjectNode: true, ShotNode: true, SceneNode: true, GeneratorInlinePanel: InlineEditorStub, Teleport: true } } })
  wrappers.push(wrapper)
  await flushPromises(); await vi.dynamicImportSettled(); await flushPromises()
  const setup = wrapper.vm.$.setupState as unknown as {
    generatorPreviewId: string; generators: GeneratorDraft[]; nodes: any[]; viewport: typeof originalViewport;
    history: unknown[]; generatorEditorId: string;
    select: (id: string) => void; persistCanvasState: () => void; undoCanvasAction: () => void;
    closeGeneratorPreview: () => void; buildGraph: () => void;
  }
  return { wrapper, setup, originals }
}

beforeEach(() => {
  localStorage.clear(); localBlobs.clear(); vi.clearAllMocks(); window.history.replaceState({}, '', '/')
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => window.setTimeout(() => callback(0), 0))
  vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id))
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0,
    width: 1280, height: 900, right: 1280, bottom: 900, toJSON: () => ({}) })
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:' + crypto.randomUUID()) })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear() })

describe('generator preview canvas integration', () => {
  it('double-clicks a real node open and shut while restoring its original viewport and authoring data', async () => {
    const { wrapper, setup } = await mounted()
    const saved = JSON.stringify(setup.generators)
    const originalPosition = { ...setup.nodes.find(node => node.id === 'gen-image').position }
    await wrapper.get('[data-id="gen-image"] .generator-card').trigger('dblclick')
    expect(setup.generatorPreviewId).toBe('gen-image')
    expect(setup.viewport.zoom).toBe(1)
    expect(setup.nodes.find(node => node.id === 'gen-image').draggable).toBe(false)
    expect(wrapper.findAll('.generator-preview-open')).toHaveLength(1)
    expect(wrapper.get('[data-id="gen-image"] .generator-card').attributes('style')).not.toContain('width: 268px')
    expect(JSON.stringify(setup.generators)).toBe(saved)
    await wrapper.get('[data-id="gen-image"] .generator-card').trigger('dblclick')
    expect(setup.generatorPreviewId).toBe('')
    expect(setup.viewport).toEqual(originalViewport)
    expect(setup.nodes.find(node => node.id === 'gen-image').position).toEqual(originalPosition)
    expect(JSON.stringify(setup.generators)).toBe(saved)
    expect(flow.updateNodeInternals).toHaveBeenCalled()
  })

  it('shows one enlarged node at a time and closes its inline generation editor', async () => {
    const { wrapper, setup } = await mounted()
    setup.select('gen-image'); await flushPromises(); await vi.dynamicImportSettled(); await flushPromises()
    expect(wrapper.find('[data-testid="inline-editor"]').exists()).toBe(true)
    await wrapper.get('[data-id="gen-image"] .generator-card').trigger('dblclick')
    expect(wrapper.find('[data-testid="inline-editor"]').exists()).toBe(false)
    await wrapper.get('[data-id="gen-video"] .generator-card').trigger('dblclick')
    await vi.dynamicImportSettled(); await flushPromises()
    expect(setup.generatorPreviewId).toBe('gen-video')
    expect(wrapper.findAll('.generator-preview-open')).toHaveLength(1)
    expect(wrapper.get('[data-id="gen-video"]').find('[data-testid="node-video-player"]').exists()).toBe(true)
    expect(wrapper.get('[data-id="gen-image"]').find('.generator-preview-open').exists()).toBe(false)
    await wrapper.get('[aria-label="收起节点预览"]').trigger('click')
    expect(setup.generatorPreviewId).toBe('')
    expect(setup.viewport).toEqual(originalViewport)
  })

  it('saves the original viewport and dimensions during a transient preview', async () => {
    const { wrapper, setup } = await mounted()
    const original = JSON.parse(JSON.stringify(setup.generators))
    await wrapper.get('[data-id="gen-image"] .generator-card').trigger('dblclick')
    setup.persistCanvasState()
    const state = wrapper.emitted('canvasChange')!.at(-1)![0] as any
    expect(state.viewport).toEqual(originalViewport)
    expect(state.generators).toEqual(original)
    expect(state.generators.every((node: any) => node.previewOpen === undefined && node.previewSize === undefined)).toBe(true)
  })

  it('creates one image reference from the node capture event and removes only that new node on undo', async () => {
    const { wrapper, setup } = await mounted()
    const original = JSON.parse(JSON.stringify(setup.generators))
    await wrapper.get('[data-id="gen-video"] .generator-card').trigger('dblclick')
    await vi.dynamicImportSettled(); await flushPromises()
    expect(setup.history).toHaveLength(0)
    await wrapper.get('[aria-label="测试截取当前帧"]').trigger('click')
    await flushPromises()
    expect(vi.mocked(saveLocalImage)).toHaveBeenCalledTimes(1)
    expect(setup.generators).toHaveLength(3)
    const reference = setup.generators.find(node => !original.some((item: GeneratorDraft) => item.id === node.id))!
    expect(reference).toMatchObject({ kind: 'image', nodeMode: 'reference', imageWidth: 1920, imageHeight: 1080 })
    expect(reference.imageAssetId).toMatch(/^image-/)
    expect(setup.history).toHaveLength(1)
    expect(setup.generators.slice(0, 2)).toEqual(original)
    setup.undoCanvasAction(); await flushPromises()
    expect(setup.generators).toEqual(original)
    expect(setup.history).toHaveLength(0)
  })

  it('undoes an unrelated node creation without discarding a later delivered generation version', async () => {
    const { wrapper, setup } = await mounted()
    await wrapper.get('.vue-flow__pane').trigger('dblclick', { clientX: 1000, clientY: 600 })
    await wrapper.findAll('.create-node-menu button').find(button => button.text().startsWith('图片生成'))!.trigger('click')
    expect(setup.generators).toHaveLength(3)
    const live = setup.generators.find(item => item.id === 'gen-image')!
    const latest = generated('gen-image', 'image', 'c', 120).versions![0]
    const delivered = { ...latest, id: 'generation-latest-version', jobId: 'generation-latest' }
    live.versions = [delivered, ...live.versions!]
    live.activeVersionId = delivered.id
    live.generationTask = { jobId: 'generation-latest', inputSignature: 'submitted', eventCursor: 6 }
    setup.undoCanvasAction(); await flushPromises()
    const restored = setup.generators.find(item => item.id === 'gen-image')!
    expect(setup.generators).toHaveLength(2)
    expect(restored.generationTask).toEqual(live.generationTask)
    expect(restored.versions).toEqual(live.versions)
    expect(restored.activeVersionId).toBe(delivered.id)
    setup.persistCanvasState()
    const saved = wrapper.emitted('canvasChange')!.at(-1)![0] as { generators: GeneratorDraft[] }
    expect(saved.generators.find(item => item.id === 'gen-image')!.generationTask!.jobId).toBe('generation-latest')
  })
})
