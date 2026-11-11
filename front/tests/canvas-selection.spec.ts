import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App.vue'
import { useWorkbenchStore } from '../src/stores/workbench'
import type { GenerationSequence, Shot } from '../src/types'
import { getJobResult, createVideoJob, cancelJob } from '../src/api/jobs'
import { PROMPT_WIDTH, PROMPT_HEIGHT } from '../src/promptPlacement'
import { eventPromptSize } from '../src/eventPromptLayout'
import { SEQUENCE_ROW_GAP, SEQUENCE_SOURCE_GAP, SEQUENCE_SOURCES_PER_ROW } from '../src/sequenceBranchLayout'
import { sequenceBranchTask } from './fixtures/sequenceBranches'

const flowMocks = vi.hoisted(() => ({ fitView: vi.fn(), selectionFrame: { value: false } }))

vi.mock('@vue-flow/core', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    SelectionMode: { Full: 'full', Partial: 'partial' },
    useVueFlow: () => ({ fitView: flowMocks.fitView, screenToFlowCoordinate: (point: unknown) => point, nodesSelectionActive: flowMocks.selectionFrame }),
    VueFlow: defineComponent({ name: 'VueFlow', props: ['nodes', 'edges', 'deleteKeyCode', 'selectionMode'],
      emits: ['node-click', 'edge-click', 'pane-click', 'node-drag-start', 'node-drag-stop', 'connect', 'nodes-change', 'viewport-change', 'selection-start', 'selection-end'],
      setup: () => () => h('div', { class: 'vue-flow__pane' }),
    }),
    Handle: defineComponent({ setup: () => () => null }), Position: { Left: 'left', Right: 'right' },
  }
})
vi.mock('../src/generatorStorage', () => ({
  loadLocalImage: vi.fn(async () => null),
  saveLocalImage: vi.fn(), imageDimensions: vi.fn(), validateLocalImage: vi.fn(),
}))
vi.mock('../src/api/jobs', () => ({
  getJobResult: vi.fn(), jobEventsUrl: vi.fn(), cancelJob: vi.fn(), createScriptJob: vi.fn(),
  createVideoJob: vi.fn(), downloadJobExport: vi.fn(), resumeJob: vi.fn(),
}))

let wrapper: VueWrapper
let currentPinia: ReturnType<typeof createPinia>
function mountCanvas(pinia = createPinia()) {
  currentPinia = pinia
  return mount(App, { global: { plugins: [pinia], stubs: {
    TacticalMiniMap: true, WorkflowRail: true, Background: true,
    ProjectNode: true, ShotNode: true, SceneNode: true, GeneratorNode: true,
    GeneratorInlinePanel: true, PromptNode: true, ShotInspector: true,
    GenerationSequenceNode: true, GenerationSequenceInspector: true,
    Teleport: true,
  } } })
}
const generators = (view: VueWrapper) => (view.findComponent({ name: 'VueFlow' }).props('nodes') as Array<{ id: string; type: string; selected: boolean }>).filter(node => node.type === 'generator')
const flow = () => wrapper.findComponent({ name: 'VueFlow' })
type CanvasNode = { id: string; type: string; hidden?: boolean; position: { x: number; y: number }; data: Record<string, any> }
const canvasNodes = () => flow().props('nodes') as CanvasNode[]
async function create(label: string) {
  await wrapper.get('.vue-flow__pane').trigger('dblclick', { clientX: 300, clientY: 200 })
  await wrapper.findAll('.create-node-menu button').find(button => button.text().startsWith(label))!.trigger('click')
  await flushPromises()
}
async function key(value: string, metaKey = false) {
  window.dispatchEvent(new KeyboardEvent('keydown', { key: value, metaKey, bubbles: true, cancelable: true }))
  await flushPromises()
}

describe('App canvas selection and canonical deletion', () => {
  beforeEach(async () => {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
    wrapper = mountCanvas()
    await flushPromises()
  })
  afterEach(() => { wrapper.unmount(); localStorage.clear() })

  it('creates exactly one input without creating a video generator', async () => {
    expect(wrapper.find('[aria-label="重播操作引导"], .onboarding-overlay').exists()).toBe(false)
    expect(wrapper.find('.canvas-empty').exists()).toBe(true)
    expect(flow().props('selectionMode')).toBe('full')
    await create('视频输入')
    expect(useWorkbenchStore(currentPinia).tasks).toHaveLength(1)
    expect(useWorkbenchStore(currentPinia).tasks[0].inputType).toBe('video')
    expect(generators(wrapper)).toHaveLength(0)
    expect(flow().props('deleteKeyCode')).toBeNull()
  })

  it('recovers 512 duplicate drafts and remains idempotent across shared-store remounts', async () => {
    wrapper.unmount()
    const pinia = createPinia()
    const draft = { key: 'draft-recovered-video', inputType: 'video', position: { x: 80, y: 90 }, scriptText: '' }
    localStorage.setItem('frameforge-canvas-v1', JSON.stringify({ drafts: Array(512).fill(draft) }))
    wrapper = mountCanvas(pinia); await flushPromises()
    expect(useWorkbenchStore(currentPinia).tasks.map(task => task.key)).toEqual([draft.key])
    expect(canvasNodes().filter(node => node.type === 'project')).toHaveLength(1)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).drafts).toHaveLength(1)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-recovery:legacy:v1')!).drafts).toHaveLength(512)
    wrapper.unmount(); wrapper = mountCanvas(pinia); await flushPromises()
    expect(useWorkbenchStore(currentPinia).tasks.map(task => task.key)).toEqual([draft.key])
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).drafts).toHaveLength(1)
  })

  it('does not open generator controls during a drag or its synthetic click', async () => {
    await create('图片生成')
    const setup = wrapper.vm.$.setupState as unknown as { generatorEditorId: string; beginCanvasPointer: () => void }
    const node = canvasNodes().find(node => node.type === 'generator')!
    expect(setup.generatorEditorId).toBe(node.id)
    flow().vm.$emit('node-drag-start', { node }); await flushPromises()
    expect(setup.generatorEditorId).toBe('')
    flow().vm.$emit('node-drag-stop', { node: { ...node, position: { x: node.position.x + 60, y: node.position.y + 20 } } }); await flushPromises()
    flow().vm.$emit('node-click', { node }); await flushPromises()
    expect(setup.generatorEditorId).toBe('')
    setup.beginCanvasPointer()
    flow().vm.$emit('node-click', { node }); await flushPromises()
    expect(setup.generatorEditorId).toBe(node.id)
  })

  it('updates generator layout dimensions when its ratio changes without moving the node', async () => {
    await create('视频生成')
    const first = canvasNodes().find(node => node.type === 'generator')!
    const setup = wrapper.vm.$.setupState as unknown as { changeGeneratorSettings: (id: string, settings: unknown) => void }
    setup.changeGeneratorSettings(first.id, { aspect_ratio: '9:16', resolution: '1080p', duration_seconds: 15 })
    await flushPromises()
    const changed = canvasNodes().find(node => node.id === first.id)!
    expect(changed.data.layoutWidth).toBeCloseTo(150.75)
    expect(changed.data.layoutHeight).toBeCloseTo(310)
    expect(changed.position).toEqual(first.position)
  })

  it('keeps a marquee selection across rebuilds and moves the whole group with one undo', async () => {
    await create('图片生成'); await create('视频生成')
    const originals = canvasNodes().filter(node => node.type === 'generator').map(node => ({ ...node, position: { ...node.position } }))
    flow().vm.$emit('selection-start')
    flow().vm.$emit('nodes-change', originals.map(node => ({ type: 'select', id: node.id, selected: true })))
    flow().vm.$emit('selection-end'); await flushPromises()
    const setup = wrapper.vm.$.setupState as unknown as { buildGraph: () => void; generatorEditorId: string }
    setup.buildGraph(); await flushPromises()
    expect(generators(wrapper).filter(node => node.selected)).toHaveLength(2)
    expect(flowMocks.selectionFrame.value).toBe(true)
    expect(setup.generatorEditorId).toBe('')
    const moved = originals.map(node => ({ ...node, position: { x: node.position.x + 80, y: node.position.y + 45 } }))
    flow().vm.$emit('node-drag-start', { node: originals[0], nodes: originals })
    flow().vm.$emit('node-drag-stop', { node: moved[0], nodes: moved }); await flushPromises()
    for (const node of moved) expect(canvasNodes().find(item => item.id === node.id)?.position).toEqual(node.position)
    await key('z', true)
    for (const node of originals) expect(canvasNodes().find(item => item.id === node.id)?.position).toEqual(node.position)
    expect(flowMocks.selectionFrame.value).toBe(false)
  })

  it('keeps a padded selection frame for one marquee-selected node and clears it for an ordinary click', async () => {
    await create('图片生成')
    const node = canvasNodes().find(node => node.type === 'generator')!
    flow().vm.$emit('selection-start')
    flow().vm.$emit('nodes-change', [{ type: 'select', id: node.id, selected: true }])
    flow().vm.$emit('selection-end'); await flushPromises()
    expect(flowMocks.selectionFrame.value).toBe(true)
    expect(generators(wrapper).filter(node => node.selected)).toHaveLength(1)
    flow().vm.$emit('node-click', { node }); await flushPromises()
    expect(flowMocks.selectionFrame.value).toBe(false)
  })

  it('deletes selected roots and generators in one transaction and restores wires with one undo', async () => {
    const owner = sequenceBranchTask('batch-delete', 2, [2])
    useWorkbenchStore(currentPinia).restoreTask(owner)
    await create('图片生成'); await create('视频生成')
    const [image, video] = canvasNodes().filter(node => node.type === 'generator')
    flow().vm.$emit('connect', { source: image.id, sourceHandle: 'image-out', target: video.id, targetHandle: 'image-in' }); await flushPromises()
    const setup = wrapper.vm.$.setupState as unknown as { applyMultiSelection: (ids: Set<string>) => void }
    setup.applyMultiSelection(new Set([owner.key, owner.generationSequences![0].id, image.id]))
    await key('Delete')
    expect(useWorkbenchStore(currentPinia).tasks).toHaveLength(0)
    expect(generators(wrapper).map(node => node.id)).toEqual([video.id])
    expect(flow().props('edges')).toHaveLength(0)
    vi.mocked(getJobResult).mockRejectedValueOnce(new Error('Offline fixture'))
    await key('z', true)
    expect(useWorkbenchStore(currentPinia).tasks.map(task => task.key)).toEqual([owner.key])
    expect(generators(wrapper).map(node => node.id)).toEqual([image.id, video.id])
    expect((flow().props('edges') as Array<{ source: string }>).some(edge => edge.source === image.id)).toBe(true)
  })

  it('removes a derived node only from the canvas, persists it and can restore it after reload', async () => {
    const task = sequenceBranchTask('hidden-derived', 2, [2])
    useWorkbenchStore(currentPinia).restoreTask(task)
    const setup = wrapper.vm.$.setupState as unknown as { buildGraph: () => void; applyMultiSelection: (ids: Set<string>) => void }
    setup.buildGraph(); await flushPromises()
    const id = task.generationSequences![0].id
    setup.applyMultiSelection(new Set([id]))
    await key('Delete')
    expect(canvasNodes().some(node => node.id === id)).toBe(false)
    expect(useWorkbenchStore(currentPinia).tasks[0].generationSequences).toHaveLength(1)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).hiddenNodeIds).toContain(id)
    await wrapper.get('.canvas-restore-hidden').trigger('click')
    expect(canvasNodes().some(node => node.id === id)).toBe(true)
    await key('z', true)
    expect(canvasNodes().some(node => node.id === id)).toBe(false)
  })

  it('selects all visible nodes, switches tools, and leaves text editing shortcuts alone', async () => {
    await create('图片生成'); await create('视频生成')
    await key('a', true)
    expect(generators(wrapper).filter(node => node.selected)).toHaveLength(2)
    await key('h')
    expect(wrapper.find('.canvas-tool-hand').exists()).toBe(true)
    const input = document.createElement('textarea')
    for (const name of ['Delete', 'Backspace', 'a', 'v']) {
      const event = new KeyboardEvent('keydown', { key: name, metaKey: name === 'a', bubbles: true, cancelable: true })
      Object.defineProperty(event, 'target', { value: input }); window.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(false)
    }
    expect(generators(wrapper)).toHaveLength(2)
    expect(wrapper.find('.canvas-tool-hand').exists()).toBe(true)
    await key('v')
    expect(wrapper.find('.canvas-tool-select').exists()).toBe(true)
    await key(' ')
    expect(wrapper.find('.canvas-tool-hand').exists()).toBe(true)
    window.dispatchEvent(new Event('blur')); await flushPromises()
    expect(wrapper.find('.canvas-tool-select').exists()).toBe(true)
  })

  it('handles modifier clicks before child card buttons can expand or replace the selection', async () => {
    await create('图片生成'); await create('视频生成')
    const [first, second] = canvasNodes().filter(node => node.type === 'generator')
    const setup = wrapper.vm.$.setupState as unknown as { captureCanvasClick: (event: MouseEvent) => void }
    const element = document.createElement('div'); element.className = 'vue-flow__node'; element.dataset.id = first.id
    const button = element.appendChild(document.createElement('button'))
    const click = () => { const event = new MouseEvent('click', { metaKey: true, bubbles: true, cancelable: true }); Object.defineProperty(event, 'target', { value: button }); setup.captureCanvasClick(event); return event }
    expect(click().defaultPrevented).toBe(true)
    await flushPromises()
    expect(generators(wrapper).filter(node => node.selected).map(node => node.id)).toEqual([first.id, second.id])
    click(); await flushPromises()
    expect(generators(wrapper).filter(node => node.selected).map(node => node.id)).toEqual([second.id])
  })

  it('activates temporary panning before a downstream Space handler prevents default', async () => {
    const preventSpace = (event: KeyboardEvent) => { if (event.key === ' ') event.preventDefault() }
    window.addEventListener('keydown', preventSpace)
    const button = document.body.appendChild(document.createElement('button'))
    try {
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true }))
      await flushPromises()
      expect(wrapper.find('.canvas-tool-hand').exists()).toBe(true)
      document.body.dispatchEvent(new KeyboardEvent('keyup', { key: ' ', code: 'Space', bubbles: true }))
      await flushPromises()
      expect(wrapper.find('.canvas-tool-select').exists()).toBe(true)
      button.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true }))
      await flushPromises()
      expect(wrapper.find('.canvas-tool-select').exists()).toBe(true)
    } finally { button.remove(); window.removeEventListener('keydown', preventSpace) }
  })

  it('captures canvas undo before local handlers, prevents browser fallback and never consumes text undo', async () => {
    await create('图片生成')
    const id = generators(wrapper)[0].id
    await key('Delete')
    const region = document.body.appendChild(document.createElement('div'))
    const local = vi.fn((event: KeyboardEvent) => event.stopPropagation())
    region.addEventListener('keydown', local)
    const input = region.appendChild(document.createElement('textarea'))
    const editable = region.appendChild(document.createElement('div')); editable.contentEditable = 'plaintext-only'; editable.setAttribute('contenteditable', 'plaintext-only')
    try {
      const undo = new KeyboardEvent('keydown', { key: 'z', code: 'KeyZ', metaKey: true, bubbles: true, cancelable: true })
      region.dispatchEvent(undo); await flushPromises()
      expect(undo.defaultPrevented).toBe(true)
      expect(local).not.toHaveBeenCalled()
      expect(generators(wrapper).map(node => node.id)).toEqual([id])
      for (const target of [input, editable]) {
        const textUndo = new KeyboardEvent('keydown', { key: 'z', metaKey: true, bubbles: true, cancelable: true })
        target.dispatchEvent(textUndo); await flushPromises()
        expect(textUndo.defaultPrevented).toBe(false)
        expect(generators(wrapper).map(node => node.id)).toEqual([id])
      }
      await key('z', true) // undo creation
      expect(generators(wrapper)).toHaveLength(0)
      const emptyUndo = new KeyboardEvent('keydown', { key: 'z', metaKey: true, bubbles: true, cancelable: true })
      region.dispatchEvent(emptyUndo); await flushPromises()
      expect(emptyUndo.defaultPrevented).toBe(true)
      expect(wrapper.get('.toast').text()).toContain('没有可撤回')
    } finally { region.remove() }
  })

  it('allows a selection-frame click to reach its underlying node and clears the frame for single selection', async () => {
    await create('图片生成'); await create('视频生成')
    const [first, second] = canvasNodes().filter(node => node.type === 'generator')
    const setup = wrapper.vm.$.setupState as unknown as { captureCanvasClick: (event: MouseEvent) => void; applyMultiSelection: (ids: Set<string>) => void }
    setup.applyMultiSelection(new Set([first.id, second.id]))
    const node = document.createElement('div'); node.className = 'vue-flow__node'; node.dataset.id = first.id
    const frame = document.createElement('div'); frame.className = 'vue-flow__nodesselection'
    const rect = frame.appendChild(document.createElement('div')); rect.className = 'vue-flow__nodesselection-rect'
    const original = Object.getOwnPropertyDescriptor(document, 'elementsFromPoint')
    Object.defineProperty(document, 'elementsFromPoint', { configurable: true, value: () => [rect, node] })
    try {
      const event = new MouseEvent('click', { bubbles: true, cancelable: true }); Object.defineProperty(event, 'target', { value: rect })
      setup.captureCanvasClick(event); await flushPromises()
      expect(generators(wrapper).filter(node => node.selected).map(node => node.id)).toEqual([first.id])
      expect(flowMocks.selectionFrame.value).toBe(false)
    } finally { if (original) Object.defineProperty(document, 'elementsFromPoint', original); else delete (document as any).elementsFromPoint }
  })

  it('deduplicates pending submissions and cancels a late-created job when its input was deleted', async () => {
    await create('视频输入')
    const store = useWorkbenchStore(currentPinia)
    const key = store.tasks[0].key
    let finish!: (value: { job_id: string; status: string }) => void
    vi.mocked(createVideoJob).mockReset().mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    vi.mocked(cancelJob).mockReset().mockResolvedValue({ job_id: 'late', status: 'cancelled' })
    const setup = (wrapper.vm.$ as unknown as { setupState: { startJob: (key: string, input: unknown) => Promise<void> } }).setupState
    const payload = { inputType: 'video', file: new File(['fixture'], 'v.mp4'), scriptText: '' }
    const first = setup.startJob(key, payload)
    await setup.startJob(key, payload)
    expect(createVideoJob).toHaveBeenCalledTimes(1)
    store.tasks.splice(0)
    finish({ job_id: 'late', status: 'queued' })
    await first
    expect(cancelJob).toHaveBeenCalledWith('late')
    expect(store.tasks).toHaveLength(0)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).hiddenJobIds).toContain('late')
  })

  it('requests cancellation when a running analysis root is removed', async () => {
    await create('视频输入')
    const store = useWorkbenchStore(currentPinia)
    const task = store.tasks[0]; task.jobId = 'job-delete-running'; task.status = 'processing'
    vi.mocked(cancelJob).mockReset().mockResolvedValue({ job_id: task.jobId, status: 'cancelled' })
    const setup = wrapper.vm.$.setupState as unknown as { deleteTask: (id: string) => void }
    setup.deleteTask(task.key); await flushPromises()
    expect(cancelJob).toHaveBeenCalledTimes(1)
    expect(cancelJob).toHaveBeenCalledWith('job-delete-running')
    expect(store.tasks).toHaveLength(0)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).hiddenJobIds).toContain('job-delete-running')
  })

  it('cancels and persistently hides a submitted input on undo, including project reload membership', async () => {
    await create('视频输入')
    const store = useWorkbenchStore(currentPinia)
    store.tasks[0].jobId = 'job-undo-submitted'; store.tasks[0].status = 'queued'
    vi.mocked(cancelJob).mockReset().mockResolvedValue({ job_id: 'job-undo-submitted', status: 'cancelled' })
    await key('z', true)
    expect(cancelJob).toHaveBeenCalledWith('job-undo-submitted')
    expect(store.tasks).toHaveLength(0)
    const saved = JSON.parse(localStorage.getItem('frameforge-canvas-v1')!)
    expect(saved.hiddenJobIds).toContain('job-undo-submitted')
    expect(saved.jobKeys).toEqual({})
    wrapper.unmount(); vi.mocked(getJobResult).mockClear()
    wrapper = mount(App, { props: { projectId: 'audit-project', initialCanvas: { ...saved, backendJobIds: ['job-undo-submitted'] } },
      global: { plugins: [createPinia()], stubs: { VueFlow: true, Background: true, TacticalMiniMap: true, WorkflowRail: true, Teleport: true } } })
    await flushPromises()
    expect(getJobResult).not.toHaveBeenCalled()
  })

  it('cancels all selected running roots but leaves completed analysis alone', async () => {
    await create('视频输入'); await create('剧本输入'); await create('视频输入')
    const store = useWorkbenchStore(currentPinia)
    store.tasks.forEach((task, index) => { task.jobId = 'job-batch-' + index; task.status = index === 2 ? 'completed' : 'processing' })
    vi.mocked(cancelJob).mockReset().mockImplementation(async id => ({ job_id: id, status: 'cancelled' }))
    const setup = wrapper.vm.$.setupState as unknown as { applyMultiSelection: (ids: Set<string>) => void; buildGraph: () => void }
    setup.buildGraph(); setup.applyMultiSelection(new Set(store.tasks.map(task => task.key)))
    await key('Delete')
    expect(cancelJob).toHaveBeenCalledTimes(2)
    expect(vi.mocked(cancelJob).mock.calls.map(call => call[0]).sort()).toEqual(['job-batch-0', 'job-batch-1'])
    expect(store.tasks).toHaveLength(0)
  })

  it('keeps a task visible if its cancellation fails so running work is not orphaned', async () => {
    await create('视频输入')
    const store = useWorkbenchStore(currentPinia)
    const task = store.tasks[0]; task.jobId = 'job-cancel-error'; task.status = 'processing'
    vi.mocked(cancelJob).mockReset().mockRejectedValue(new Error('offline'))
    vi.mocked(getJobResult).mockRejectedValueOnce(new Error('offline'))
    vi.stubGlobal('EventSource', class { close() {} addEventListener() {} })
    try {
      const setup = wrapper.vm.$.setupState as unknown as { deleteTask: (id: string) => void }
      setup.deleteTask(task.key); await flushPromises()
      expect(store.tasks.map(item => item.jobId)).toEqual(['job-cancel-error'])
      expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).hiddenJobIds).not.toContain('job-cancel-error')
      expect(wrapper.get('.toast').text()).toContain('取消失败，已恢复节点')
    } finally { vi.unstubAllGlobals() }
  })

  it('migrates old source-view persistence to the default merged display', async () => {
    wrapper.unmount()
    localStorage.setItem('frameforge-canvas-v1', JSON.stringify({ jobKeys: {}, positions: {}, sceneUi: {}, generationView: 'source' }))
    wrapper = mountCanvas(); await flushPromises()
    expect(useWorkbenchStore(currentPinia).generationView).toBe('sequences')
    expect(wrapper.find('.generation-view-toggle').exists()).toBe(false)
  })

  it('discards a legacy far-away automatic scene position without resetting its input node', async () => {
    wrapper.unmount()
    const key = 'task-legacy-layout'; const sceneId = `${key}:scene:scene-001`
    localStorage.setItem('frameforge-canvas-v1', JSON.stringify({ jobKeys: { 'job-legacy-layout': key }, positions: {
      [key]: { x: 100, y: 200 }, [sceneId]: { x: 9000, y: 200 },
    }, sceneUi: { [sceneId]: { open: false, page: 1 } } }))
    vi.mocked(getJobResult).mockResolvedValue({ schema_version: 'storyboard.v1', job: { id: 'job-legacy-layout', status: 'completed', progress: 100, script_text: '' }, shots: [
      { id: 'source-legacy', number: 1, duration: 2, time: '0s–2s', action: '饮用', scene_group_id: 'scene-001', status: 'ready' },
    ], frames: [], prompts: [] } as never)
    window.history.replaceState({}, '', '/?job_ids=job-legacy-layout')
    wrapper = mountCanvas(); await flushPromises()
    const root = canvasNodes().find(node => node.id === key)!
    const scene = canvasNodes().find(node => node.id === sceneId)!
    expect(root.position).toEqual({ x: 100, y: 200 })
    expect(scene.position).toEqual({ x: 500, y: 200 })
    const setup = wrapper.vm.$.setupState as unknown as { focusScene: (id: string) => void }
    setup.focusScene(sceneId); await flushPromises()
    expect(canvasNodes().find(node => node.id === sceneId)!.position).toEqual(scene.position)
    expect(canvasNodes().find(node => node.id === 'source-legacy')!.position.x - scene.position.x).toBe(320)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).positions[sceneId]).toBeUndefined()
  })

  it('coalesces an SSE burst and fetches the final snapshot without restoring an old processing state', async () => {
    await create('视频输入')
    const store = useWorkbenchStore(currentPinia); store.tasks[0].jobId = 'job-burst'
    let resolve!: (value: unknown) => void
    const result = { schema_version: 'storyboard.v1', job: { id: 'job-burst', status: 'completed', progress: 100, script_text: '' }, shots: [], frames: [], prompts: [] }
    vi.mocked(getJobResult).mockClear()
    vi.mocked(getJobResult).mockImplementationOnce(() => new Promise(done => { resolve = done as never }) as never).mockResolvedValue(result as never)
    const setup = wrapper.vm.$.setupState as unknown as { handleStreamEvent: (job: string, event: { type: string; payload: Record<string, unknown> }) => Promise<void> }
    const early = setup.handleStreamEvent('job-burst', { type: 'prompt.ready', payload: {} })
    const sequence = setup.handleStreamEvent('job-burst', { type: 'sequence.ready', payload: {} })
    const final = setup.handleStreamEvent('job-burst', { type: 'job.completed', payload: { progress: 100 } })
    expect(getJobResult).toHaveBeenCalledTimes(1)
    resolve({ ...result, job: { ...result.job, status: 'processing', progress: 80 } })
    await Promise.all([early, sequence, final]); await flushPromises()
    expect(getJobResult).toHaveBeenCalledTimes(2)
    expect(store.tasks[0].status).toBe('completed'); expect(store.tasks[0].progress).toBe(100)
  })

  it('replaces sources with input branches on completion, keeps four-source rows and leaves other tasks fixed', async () => {
    await create('视频输入'); await create('视频输入')
    const store = useWorkbenchStore(currentPinia); const task = store.tasks[0]
    task.jobId = 'job-layout'; task.status = 'processing'
    const other = store.tasks[1]; other.position = { x: 60, y: 3000 }
    const source: Shot = { id: '', number: 1, time: '0s–1s', duration: '1s', action: '连贯动作', shotSize: '近景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .8, thumb: '/frame.png', frames: [{ id: 'frame', url: '/frame.png', timestamp: .1, width: 768, height: 1366 }], analysisWarnings: [], sourceRawShotIds: [], sceneGroupId: 'scene-001', status: 'ready', actionDetails: { primarySubject: '人物', initialState: '持杯', steps: ['动作'], interactionTarget: '杯子', finalState: '饮用', emotionChange: '', evidence: [] }, spatialEvidence: { foreground: '', midground: '', background: '', relationships: '', observable: false } }
    task.shots = Array.from({ length: 9 }, (_, index) => ({ ...source, id: `source-${index + 1}`, number: index + 1, sceneGroupId: index === 8 ? 'scene-002' : 'scene-001', frames: [{ ...source.frames[0], id: `frame-${index + 1}`, width: index === 3 ? 1366 : 768, height: index === 3 ? 768 : 1366 }] }))
    other.shots = [{ ...source, id: 'other-source' }]
    flow().vm.$emit('pane-click'); await flushPromises()
    const setup = wrapper.vm.$.setupState as unknown as { focusScene: (id: string) => void; toggleSequence: (id: string) => void; handleStreamEvent: (job: string, event: { type: string; payload: Record<string, unknown> }) => Promise<void> }
    setup.focusScene(`${task.key}:scene:scene-001`)
    setup.focusScene(`${task.key}:scene:scene-002`); await flushPromises()
    const otherBefore = canvasNodes().filter(node => node.data.taskKey === other.key).map(node => ({ id: node.id, position: { ...node.position } }))
    flow().vm.$emit('node-click', { node: canvasNodes().find(node => node.id === 'source-4') }); await flushPromises()
    const groups = [[1, 2, 3, 4, 5, 6], [7], [8, 9]]
    const sequences: GenerationSequence[] = groups.map((ids, index) => ({
      id: `sequence-${index + 1}`, event_id: 'shared-event', scene_group_id: index === 2 ? 'scene-002' : 'scene-001',
      source_shot_ids: ids.map(id => `source-${id}`), source_start_time: ids[0] - 1, source_end_time: ids.at(-1)!, source_duration_seconds: ids.length,
      generation_duration_seconds: 5, timeline: [], status: 'ready', main_action: '递杯与饮用',
      first_frame_prompt: { id: `seq-${index + 1}-first`, prompt_type: 'first_frame', platform: 'generic-image', version: 1, positive_prompt: '完整首帧 Prompt', status: 'ready' },
      seedance_prompt: { id: `seq-${index + 1}-video`, prompt_type: 'video', platform: 'seedance', version: 1, positive_prompt: '完整时序 Prompt', status: 'ready' },
    }))
    const completed = { schema_version: 'storyboard.v1', job: { id: task.jobId, status: 'completed', progress: 100, input_type: 'video', script_text: '' },
      shots: task.shots.map(shot => ({ id: shot.id, number: shot.number, duration: 1, time: shot.time, action: shot.action, scene_group_id: shot.sceneGroupId, status: 'ready' })),
      frames: task.shots.flatMap(shot => shot.frames.map(frame => ({ ...frame, shot_id: shot.id }))), prompts: [], generation_sequences: sequences,
    }
    vi.mocked(getJobResult).mockResolvedValue(completed as never)
    await setup.handleStreamEvent('job-layout', { type: 'job.completed', payload: { progress: 100 } }); await flushPromises()
    const visible = canvasNodes().filter(node => !node.hidden)
    expect(visible.filter(node => node.type === 'sequence')).toHaveLength(3)
    expect(visible.filter(node => node.type === 'shot' && node.data.taskKey === task.key)).toHaveLength(8)
    expect(store.selectedId).toBe('sequence-1')
    expect(visible.filter(node => node.type === 'eventPrompt')).toHaveLength(3)
    expect(visible.filter(node => node.type === 'scene' && node.data.taskKey === task.key)).toHaveLength(0)
    expect(canvasNodes().find(node => node.id === 'source-7')?.hidden).toBe(true)
    const sourceRows = visible.filter(node => node.type === 'shot' && node.data.branchId === 'sequence-1')
    expect(new Set(sourceRows.slice(0, SEQUENCE_SOURCES_PER_ROW).map(node => node.position.y)).size).toBe(1)
    expect(new Set(sourceRows.map(node => node.position.y)).size).toBe(2)
    const inputEdges = (flow().props('edges') as Array<{ source: string; target: string }>).filter(edge => edge.source === task.key)
    expect(inputEdges.map(edge => edge.target)).toEqual(['sequence-1', 'sequence-2', 'sequence-3'])
    expect(canvasNodes().filter(node => node.data.taskKey === other.key).map(node => ({ id: node.id, position: node.position }))).toEqual(otherBefore)
    const rootBefore = { ...canvasNodes().find(node => node.id === task.key)!.position }
    flow().vm.$emit('viewport-change', { x: 0, y: 0, zoom: .35 }); await flushPromises()
    flow().vm.$emit('nodes-change', sourceRows.map(node => ({ id: node.id, type: 'dimensions', dimensions: { width: 180, height: 180 } }))); await flushPromises()
    const compact = canvasNodes().filter(node => !node.hidden && node.type === 'shot' && node.data.branchId === 'sequence-1')
    expect(compact[4].position.y - compact[0].position.y).toBe(180 + SEQUENCE_ROW_GAP)
    expect(compact[1].position.x - compact[0].position.x).toBe(180 + SEQUENCE_SOURCE_GAP)
    expect(canvasNodes().find(node => node.id === task.key)!.position).toEqual(rootBefore)
    expect(canvasNodes().filter(node => node.data.taskKey === other.key).map(node => ({ id: node.id, position: node.position }))).toEqual(otherBefore)
    const edge = (flow().props('edges') as { source: string; target: string; data: { rowWrap: boolean; wrapY: number } }[]).find(edge => edge.target === 'source-5')!
    expect(edge.source).toBe('source-4')
    expect(edge.data.rowWrap).toBe(true)
    expect(edge.data.wrapY).toBe(compact[0].position.y + 180 + SEQUENCE_ROW_GAP / 2)
    const beforeInvalid = canvasNodes().map(node => ({ id: node.id, position: { ...node.position } }))
    flow().vm.$emit('nodes-change', [{ id: 'sequence-1', type: 'dimensions', dimensions: { width: 0, height: 400 } }, { id: 'source-7', type: 'dimensions', dimensions: { width: 250, height: 900 } }]); await flushPromises()
    expect(canvasNodes().map(node => ({ id: node.id, position: node.position }))).toEqual(beforeInvalid)
    const positions = canvasNodes().filter(node => !node.hidden && node.data.taskKey === task.key && node.type !== 'prompt').map(node => ({ id: node.id, position: { ...node.position } }))
    flow().vm.$emit('node-click', { node: visible.find(node => node.id === 'sequence-2') }); await flushPromises()
    expect(canvasNodes().filter(node => !node.hidden && node.data.taskKey === task.key && node.type !== 'prompt').map(node => ({ id: node.id, position: node.position }))).toEqual(positions)
    const selected = canvasNodes().find(node => node.id === 'sequence-2')!
    const moved = { x: selected.position.x + 90, y: selected.position.y + 40 }
    flow().vm.$emit('node-drag-start', { node: selected }); flow().vm.$emit('node-drag-stop', { node: { ...selected, position: moved } })
    flow().vm.$emit('node-click', { node: canvasNodes().find(node => node.id === 'sequence-2') }); await flushPromises()
    const deck = canvasNodes().filter(node => node.type === 'prompt')
    expect(deck).toHaveLength(1)
    expect(deck[0].id).toBe('seq-2-first')
    for (const prompt of deck) for (const card of canvasNodes().filter(node => !node.hidden && node.type !== 'prompt')) {
      expect(prompt.position.x < card.position.x + card.data.layoutWidth && prompt.position.x + PROMPT_WIDTH > card.position.x && prompt.position.y < card.position.y + card.data.layoutHeight && prompt.position.y + PROMPT_HEIGHT > card.position.y).toBe(false)
    }
    expect(wrapper.find('.generation-view-toggle').exists()).toBe(false)
    const otherSnapshot = canvasNodes().filter(node => node.data.taskKey === other.key).map(node => ({ id: node.id, position: { ...node.position } }))
    setup.toggleSequence('sequence-1'); await flushPromises()
    expect(canvasNodes().filter(node => !node.hidden && node.data.branchId === 'sequence-1' && node.type === 'shot')).toHaveLength(0)
    expect(canvasNodes().find(node => node.id === 'seq-1-video')?.hidden).not.toBe(true)
    setup.toggleSequence('sequence-1'); await flushPromises()
    expect(canvasNodes().filter(node => !node.hidden && node.data.branchId === 'sequence-1' && node.type === 'shot')).toHaveLength(6)
    expect(canvasNodes().find(node => node.id === 'sequence-2')!.position).toEqual(moved)
    expect(canvasNodes().filter(node => node.data.taskKey === other.key).map(node => ({ id: node.id, position: node.position }))).toEqual(otherSnapshot)
  })

  it('Backspace removes only the selected generator, does not resurrect it, and supports undo', async () => {
    await create('视频生成')
    const old = generators(wrapper)[0].id
    await create('图片生成')
    const current = generators(wrapper).find(node => node.id !== old)!.id
    await key('Backspace')
    expect(generators(wrapper).map(node => node.id)).toEqual([old])
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).generators.map((node: { id: string }) => node.id)).toEqual([old])
    await create('视频输入')
    expect(generators(wrapper).map(node => node.id)).toEqual([old])
    await key('z', true) // undo the new task
    await key('z', true) // undo the generator deletion
    expect(generators(wrapper).map(node => node.id)).toContain(current)
  })

  it('opens and measures a nearby task locally while preserving another branch and its saved clip state', async () => {
    const store = useWorkbenchStore(currentPinia)
    const a = sequenceBranchTask('near-a', 11, [6, 4, 1])
    const b = sequenceBranchTask('near-b', 1)
    a.position = { x: 80, y: 120 }; b.position = { x: 80, y: 560 }
    store.restoreTask(a); store.restoreTask(b)
    const setup = wrapper.vm.$.setupState as unknown as { buildGraph: () => void; toggleSequence: (id: string) => void }
    setup.buildGraph(); await flushPromises()
    const otherBefore = canvasNodes().filter(node => node.data.taskKey === b.key).map(node => ({ id: node.id, hidden: node.hidden, position: { ...node.position } }))
    const rootBefore = { ...canvasNodes().find(node => node.id === a.key)!.position }
    const deckBefore = { ...canvasNodes().find(node => node.id === 'near-a-clip-1')!.position }
    setup.toggleSequence('near-a-clip-1'); await flushPromises()
    const sourceNodes = canvasNodes().filter(node => node.type === 'shot' && node.data.branchId === 'near-a-clip-1' && !node.hidden)
    expect(sourceNodes).toHaveLength(6)
    flow().vm.$emit('nodes-change', sourceNodes.map(node => ({ id: node.id, type: 'dimensions', dimensions: { width: 250, height: node.data.layoutHeight + 100 } })))
    await flushPromises()
    setup.buildGraph(); await flushPromises()
    expect(canvasNodes().filter(node => node.data.taskKey === b.key).map(node => ({ id: node.id, hidden: node.hidden, position: node.position }))).toEqual(otherBefore)
    expect(canvasNodes().find(node => node.id === a.key)!.position).toEqual(rootBefore)
    expect(canvasNodes().find(node => node.id === 'near-a-clip-1')!.position).toEqual(deckBefore)
    const saved = JSON.parse(localStorage.getItem('frameforge-canvas-v1')!)
    expect(saved.sequenceUi['near-a-clip-1']).toEqual({ open: true })
    expect(saved.sequenceUi['near-b-clip-1']).toBeUndefined()
    setup.toggleSequence('near-a-clip-1'); await flushPromises()
    expect(canvasNodes().find(node => node.id === 'near-a-clip-1-video')?.hidden).not.toBe(true)
    expect(canvasNodes().filter(node => node.data.taskKey === b.key).map(node => ({ id: node.id, hidden: node.hidden, position: node.position }))).toEqual(otherBefore)
    setup.toggleSequence('near-a-clip-1'); await flushPromises()
    flow().vm.$emit('node-click', { node: canvasNodes().find(node => node.id === a.key) }); await flushPromises()
    await key('Delete')
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).sequenceUi['near-a-clip-1']).toBeUndefined()
    vi.mocked(getJobResult).mockRejectedValueOnce(new Error('Offline fixture snapshot'))
    await key('z', true)
    expect(canvasNodes().filter(node => node.type === 'shot' && node.data.branchId === 'near-a-clip-1' && !node.hidden)).toHaveLength(6)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!).sequenceUi['near-a-clip-1']).toEqual({ open: true })
  })

  it('clicking blank canvas disarms deletion and dragging another root changes its target', async () => {
    await create('视频输入')
    const first = useWorkbenchStore(currentPinia).tasks[0].key
    await create('剧本输入')
    const second = useWorkbenchStore(currentPinia).tasks[1].key
    flow().vm.$emit('pane-click')
    await key('Delete')
    expect(useWorkbenchStore(currentPinia).tasks).toHaveLength(2)
    flow().vm.$emit('node-drag-start', { node: { id: first, type: 'project', position: { x: 0, y: 0 }, data: { taskKey: first } } })
    await key('Delete')
    expect(useWorkbenchStore(currentPinia).tasks.map(task => task.key)).toEqual([second])
  })

  it('refresh restores remaining generators but never rearms an old selection', async () => {
    await create('图片生成')
    const kept = generators(wrapper)[0].id
    await create('视频生成')
    await key('Backspace')
    const saved = JSON.parse(localStorage.getItem('frameforge-canvas-v1')!)
    saved.selectedId = kept // state written by an older version of the app
    localStorage.setItem('frameforge-canvas-v1', JSON.stringify(saved))
    wrapper.unmount()
    wrapper = mountCanvas()
    await flushPromises()
    expect(useWorkbenchStore(currentPinia).selectedId).toBe('')
    expect(generators(wrapper).map(node => node.id)).toEqual([kept])
    await key('Delete')
    expect(generators(wrapper).map(node => node.id)).toEqual([kept])
  })

  it('a selected wire never deletes its nodes and selecting a new input disarms the wire', async () => {
    await create('图片生成')
    const source = generators(wrapper)[0].id
    await create('视频生成')
    const target = generators(wrapper).find(node => node.id !== source)!.id
    flow().vm.$emit('connect', { source, sourceHandle: 'image-out', target, targetHandle: 'image-in' })
    await flushPromises()
    expect(flow().props('edges')).toHaveLength(1)
    await create('视频输入')
    await key('Delete')
    expect(useWorkbenchStore(currentPinia).tasks).toHaveLength(0)
    expect(generators(wrapper)).toHaveLength(2)
    const edge = (flow().props('edges') as Array<{ id: string }>)[0]
    flow().vm.$emit('edge-click', { edge })
    await key('Backspace')
    expect(flow().props('edges')).toHaveLength(0)
    expect(generators(wrapper)).toHaveLength(2)
    await key('z', true)
    expect(flow().props('edges')).toHaveLength(1)
  })
  it('inspects sources without changing the merged canvas and preserves dragged fragment positions', async () => {
    await create('视频输入'); await create('视频输入')
    const store = useWorkbenchStore(currentPinia)
    store.tasks.forEach((task, index) => {
      const sourceId = `source-${index}`
      const source: Shot = { id: sourceId, number: 1, time: '0s–4s', duration: '4.00s', action: '来源动作', shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .8, thumb: '', frames: [], analysisWarnings: [], sourceRawShotIds: [], sceneGroupId: 'scene-001', status: 'ready', actionDetails: { primarySubject: '人物', initialState: '起始', steps: ['动作'], interactionTarget: '', finalState: '结束', emotionChange: '', evidence: [] }, spatialEvidence: { foreground: '', midground: '', background: '', relationships: '', observable: false } }
      const sequence: GenerationSequence = { id: `sequence-${index}`, event_id: `event-${index}`, scene_group_id: 'scene-001', source_shot_ids: [sourceId], source_start_time: 0, source_end_time: 4, source_duration_seconds: 4, generation_duration_seconds: 6, timeline: [], status: 'ready', main_action: '连贯动作' }
      task.shots = [source]; task.generationSequences = [sequence]
    })
    flow().vm.$emit('pane-click'); await flushPromises()
    type CanvasNode = { id: string; type: string; hidden?: boolean; position: { x: number; y: number }; data: { taskKey: string } }
    const canvasNodes = () => flow().props('nodes') as CanvasNode[]
    expect(canvasNodes().find(node => node.id === 'source-1')?.hidden).toBe(true)
    const owner = store.tasks[1].key
    const setup = wrapper.vm.$.setupState as unknown as { inspectSourceShot: (id: string) => void }
    const selected = canvasNodes().find(node => node.id === 'sequence-1')!
    flow().vm.$emit('node-click', { node: selected }); await flushPromises()
    expect(store.activeTaskKey).toBe(owner)
    await key('Delete'); expect(store.tasks).toHaveLength(2)
    expect(canvasNodes().find(node => node.id === selected.id)).toBeUndefined()
    await key('z', true)
    const moved = { x: selected.position.x + 113, y: selected.position.y + 51 }
    flow().vm.$emit('node-drag-start', { node: selected }); flow().vm.$emit('node-drag-stop', { node: { ...selected, position: moved } }); await flushPromises()
    expect(wrapper.find('.generation-view-toggle').exists()).toBe(false)
    setup.inspectSourceShot('source-1'); await vi.dynamicImportSettled(); await flushPromises()
    expect(store.selectedId).toBe('source-1'); expect(canvasNodes().find(node => node.id === 'source-1')?.hidden).toBe(true)
    await wrapper.get('.node-details-return').trigger('click'); await flushPromises()
    expect(store.selectedId).toBe('sequence-1'); expect(canvasNodes().find(node => node.id === 'sequence-1')?.position).toEqual(moved)
    expect(store.tasks[0].shots[0].duration).toBe('4.00s'); expect(canvasNodes().filter(node => node.type === 'scene')).toHaveLength(0)
  })

  it('attaches one complete event prompt to each merged fragment and keeps collapse, drag, wiring and deletion isolated', async () => {
    await create('视频输入'); await create('视频输入')
    const store = useWorkbenchStore(currentPinia), owner = store.tasks[0], other = store.tasks[1]
    other.position = { x: 50, y: 3500 }
    const original: Shot = { id: 'event-source-1', number: 1, time: '0s–2s', duration: '2s', action: '男孩递杯',
      shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .8, thumb: '', frames: [],
      analysisWarnings: [], sourceRawShotIds: [], sceneGroupId: 'event-scene', status: 'ready',
      actionDetails: { primarySubject: '男孩和女子', initialState: '男孩持杯', steps: ['递杯'], interactionTarget: '杯子',
        finalState: '女子接杯', emotionChange: '', evidence: [] },
      spatialEvidence: { foreground: '', midground: '', background: '', relationships: '', observable: false } }
    owner.status = 'completed'
    owner.shots = [original, { ...original, id: 'event-source-2', number: 2, time: '2s–4s', action: '女子饮用' }]
    const text = '主体：男孩与女子。\n起点：男孩持杯。\n0–2秒：递杯与接杯。\n2–4秒：女子饮用。\n收束：杯子离开面部。'
    owner.generationSequences = [{ id: 'merged-event', event_id: 'event-one', scene_group_id: 'event-scene',
      source_shot_ids: owner.shots.map(shot => shot.id), source_start_time: 0, source_end_time: 4,
      source_duration_seconds: 4, generation_duration_seconds: 4, timeline: [], status: 'ready',
      first_frame_prompt: { id: 'merged-first', prompt_type: 'first_frame', platform: 'generic-image', version: 1,
        positive_prompt: '男孩持杯的初始状态', status: 'ready' },
      seedance_prompt: { id: 'merged-video', prompt_type: 'video', platform: 'seedance', version: 1,
        positive_prompt: text, negative_prompt: '无文字、无水印', status: 'ready' } }]
    flow().vm.$emit('pane-click'); await flushPromises()
    const setup = wrapper.vm.$.setupState as unknown as { toggleSequence: (id: string) => void }
    expect(canvasNodes().find(node => node.id === 'merged-video')?.hidden).not.toBe(true)
    const event = canvasNodes().find(node => node.id === 'merged-video')!, card = canvasNodes().find(node => node.id === 'merged-event')!
    expect(event.type).toBe('eventPrompt'); expect(event.hidden).not.toBe(true)
    expect(event.data.prompt.text).toBe(text)
    expect(event.position).toEqual({ x: card.position.x + card.data.layoutWidth + SEQUENCE_SOURCE_GAP, y: card.position.y })
    expect(event.data.layoutHeight).toBe(eventPromptSize('detail').height)
    expect(canvasNodes().map(node => node.id).length).toBe(new Set(canvasNodes().map(node => node.id)).size)
    flow().vm.$emit('node-click', { node: card }); await flushPromises()
    expect(canvasNodes().filter(node => node.id === 'merged-video')).toHaveLength(1)
    expect(canvasNodes().filter(node => node.type === 'prompt').map(node => node.id)).toEqual(['merged-first'])
    flow().vm.$emit('node-click', { node: event }); await flushPromises()
    expect(store.graphData(store.selectedId)?.title).toBe('完整事件 Prompt')
    expect(wrapper.find('.inspector').exists()).toBe(false)
    expect(wrapper.find('[data-canvas-details]').exists()).toBe(false)
    await key('i'); await vi.dynamicImportSettled(); await flushPromises()
    expect(wrapper.get('.prompt-inspector-body textarea').element.value).toBe(`${text}\n\nNegative Prompt:\n无文字、无水印`)
    await wrapper.get('[aria-label="关闭详情"]').trigger('click'); await flushPromises()
    await key('Delete'); expect(store.tasks).toHaveLength(2)
    expect(canvasNodes().find(node => node.id === event.id)).toBeUndefined()
    await key('z', true)
    const moved = { x: card.position.x + 75, y: card.position.y + 45 }
    flow().vm.$emit('node-drag-start', { node: card })
    flow().vm.$emit('node-drag-stop', { node: { ...card, position: moved } }); await flushPromises()
    expect(canvasNodes().find(node => node.id === 'merged-video')!.position).toEqual({ x: moved.x + card.data.layoutWidth + SEQUENCE_SOURCE_GAP, y: moved.y })
    setup.toggleSequence('merged-event'); await flushPromises()
    expect(canvasNodes().find(node => node.id === 'merged-video')?.hidden).not.toBe(true)
    expect(canvasNodes().filter(node => node.type === 'shot' && node.data.branchId === 'merged-event' && !node.hidden)).toHaveLength(2)
    const tail = canvasNodes().find(node => node.id === 'event-source-2')!
    expect(canvasNodes().find(node => node.id === 'merged-video')!.position).toEqual({ x: tail.position.x + tail.data.layoutWidth + SEQUENCE_SOURCE_GAP, y: tail.position.y })
    setup.toggleSequence('merged-event'); await flushPromises()
    expect(canvasNodes().find(node => node.id === 'merged-event')!.position).toEqual(moved)
    await create('视频生成')
    const generatorId = generators(wrapper)[0].id
    flow().vm.$emit('connect', { source: 'merged-video', sourceHandle: 'prompt-out', target: generatorId, targetHandle: 'prompt-in' })
    await flushPromises()
    expect((flow().props('edges') as Array<{ source: string; target: string }>).some(edge => edge.source === 'merged-video' && edge.target === generatorId)).toBe(true)
    setup.toggleSequence('merged-event'); await flushPromises()
    expect((flow().props('edges') as Array<{ source: string; target: string }>).some(edge => edge.source === 'merged-video' && edge.target === generatorId)).toBe(true)
    flow().vm.$emit('node-click', { node: canvasNodes().find(node => node.id === owner.key) })
    await key('Delete')
    expect(store.tasks.map(task => task.key)).toEqual([other.key])
    expect(canvasNodes().find(node => node.id === 'merged-video')).toBeUndefined()
    expect(generators(wrapper)).toHaveLength(1)
    await key('z', true)
    expect(canvasNodes().find(node => node.id === 'merged-video')).toBeDefined()
    expect((flow().props('edges') as Array<{ target: string }>).some(edge => edge.target === generatorId)).toBe(true)
  })
})
