import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App.vue'
import { useWorkbenchStore } from '../src/stores/workbench'
import type { Shot } from '../src/types'

vi.mock('@vue-flow/core', async () => {
  const { defineComponent, h } = await import('vue')
  return { SelectionMode: { Full: 'full', Partial: 'partial' }, useVueFlow: () => ({ fitView: vi.fn(), screenToFlowCoordinate: (point: unknown) => point }),
    VueFlow: defineComponent({ name: 'VueFlow', props: ['nodes', 'edges'], setup: () => () => h('div', { class: 'vue-flow__pane' }) }),
    Handle: defineComponent({ setup: () => () => null }), Position: { Left: 'left', Right: 'right' } }
})
vi.mock('../src/generatorStorage', () => ({ loadLocalImage: vi.fn(async () => null) }))
vi.mock('../src/api/jobs', () => ({ getJobResult: vi.fn(), jobEventsUrl: vi.fn(), cancelJob: vi.fn(), createScriptJob: vi.fn(), createVideoJob: vi.fn(), downloadJobExport: vi.fn(), resumeJob: vi.fn() }))
// A generic stub for the lazy wrapper has no declared inspector props. Keep
// the real child contract so ownership and update forwarding remain testable.
const InspectorStub = defineComponent({ name: 'ShotInspector', props: ['shot', 'jobId', 'projectId', 'taskKey', 'rawShots', 'prompts', 'tab'],
  emits: ['updated', 'styleEffects'], setup: () => () => h('section', { 'data-testid': 'source-inspector-stub' }) })
const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); localStorage.clear(); vi.restoreAllMocks() })

describe('perception inspector source ownership', () => {
  it('uses the selected source job even when another task is active and refreshes that job after revision', async () => {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
    const wrapper = mount(App, { global: { plugins: [createPinia()], stubs: {
      Background: true, TacticalMiniMap: true, WorkflowRail: true,
      ShotInspector: InspectorStub, ProjectNode: true, ShotNode: true, SceneNode: true,
      Teleport: true,
    } } })
    wrappers.push(wrapper)
    await flushPromises()
    const store = useWorkbenchStore()
    store.createDraft('video', { x: 30, y: 40 }); store.createDraft('video', { x: 500, y: 40 })
    const [source, other] = store.tasks
    source.jobId = 'job-source'; source.status = 'completed'
    other.jobId = 'job-other'; other.status = 'completed'
    const shot: Shot = { id: 'shot-source', number: 1, time: '0–3', duration: '3s', action: '拿起杯子', shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .8,
      thumb: '', status: 'ready', frames: [], analysisWarnings: [], sourceRawShotIds: [], sceneGroupId: 'scene-source' }
    source.shots = [shot]
    const setup = wrapper.vm.$.setupState as unknown as { openNodeDetails: (id: string, options: { taskKey: string; tab: string }) => void }
    setup.openNodeDetails(shot.id, { taskKey: source.key, tab: 'review' })
    // App and the host each load their detail component on demand.
    await flushPromises(); await vi.dynamicImportSettled(); await flushPromises()
    store.activeTaskKey = other.key; await flushPromises()
    const inspector = wrapper.getComponent(InspectorStub)
    expect(inspector.props('jobId')).toBe('job-source')
    expect(inspector.props('taskKey')).toBe(source.key)
    expect(inspector.props('tab')).toBe('review')
    const load = vi.spyOn(store, 'loadJob').mockResolvedValue(source)
    inspector.vm.$emit('updated', 'job-source'); await flushPromises()
    expect(load).toHaveBeenCalledWith('job-source')
    expect(load).not.toHaveBeenCalledWith('job-other')
  })
})
