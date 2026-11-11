import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia } from 'pinia'
import EventAgentStudio from '../src/components/EventAgentStudio.vue'
import App from '../src/App.vue'
import { useWorkbenchStore } from '../src/stores/workbench'
import type { EventAgentClip, EventAgentResult, EventAgentRun } from '../src/api/eventAgent'

const api = vi.hoisted(() => ({ listEventAgentRuns: vi.fn(), getEventAgentRun: vi.fn(), getEventAgentResult: vi.fn(), createEventAgentRun: vi.fn(), cancelEventAgentRun: vi.fn() }))
vi.mock('../src/api/eventAgent', () => api)
vi.mock('@vue-flow/core', async () => {
  const { defineComponent, h } = await import('vue')
  return { SelectionMode: { Full: 'full', Partial: 'partial' }, useVueFlow: () => ({ fitView: vi.fn(), screenToFlowCoordinate: (point: unknown) => point }),
    VueFlow: defineComponent({ name: 'VueFlow', props: ['nodes', 'edges'], setup: () => () => h('div', { class: 'vue-flow__pane' }) }),
    Handle: defineComponent({ setup: () => () => null }), Position: { Left: 'left', Right: 'right' },
  }
})
const wrappers: VueWrapper[] = []
const run = (status: EventAgentRun['status'] = 'ready', jobId = 'job-a', runId = 'run-a'): EventAgentRun => ({
  run_id: runId, job_id: jobId, status, stage: status === 'running' ? 'observing_window_2' : status,
  progress: status === 'ready' ? 100 : 25, created_at: '2026-10-07T01:00:00Z', warnings: [],
  has_result: ['ready', 'degraded'].includes(status), window_total: 4, window_completed: 1,
  usage_summary: { model_calls: 2, tool_calls: 1, extra_frames: 3, estimated_list_price_cny: null },
})
const presentation = [
  { source_shot_id: 'shot-1', disposition: 'preserve' as const, reason: '递出物体是交接动作的起点', evidence: [] },
  { source_shot_id: 'shot-2', disposition: 'omit' as const, reason: '重复特写，交接事实已由前一镜头保留', evidence: [] },
]
const clip = (id: string, mode: EventAgentClip['generation_mode']): EventAgentClip => ({
  id, event_id: 'event-1', title: id === 'clip-1' ? '递出并接住物体' : '交接后的反应',
  source_shot_ids: ['shot-1', 'shot-2'], presentation, source_start_time: 0, source_end_time: 5,
  source_duration_seconds: 5, generation_duration_seconds: 8, generation_mode: mode,
  timeline: [{ id: 'stage-1', source_shot_ids: ['shot-1'], fact_ids: ['fact-1'], generation_start_seconds: 0, generation_end_seconds: 8, transition: 'initial', action: '伸手交接' }],
  first_frame_prompt: { id: `${id}-first`, prompt_type: 'first_frame', platform: 'image', positive_prompt: '完整首帧提示词第一行\n第二行保留空间和物体位置', negative_prompt: '', status: 'ready' },
  seedance_prompt: { id: `${id}-video`, prompt_type: 'video', platform: 'seedance', positive_prompt: '完整视频提示词第一行\n第二行保留动作因果', negative_prompt: '不要重复交接', status: 'ready' },
  status: 'ready', warnings: [],
})
const result = (jobId = 'job-a', runId = 'run-a'): EventAgentResult => ({
  schema_version: 'event-agent.v1', job_id: jobId, run_id: runId, status: 'ready',
  windows: [{ id: 'window-1', start_time: 0, end_time: 5, source_shot_ids: ['shot-1', 'shot-2'], context_shot_ids: [] }],
  events: [{ id: 'event-1', anchor_shot_id: 'shot-1', title: '交接物体', goal: '对方接住物体', source_shot_ids: ['shot-1', 'shot-2'], initial_state: '物体在递出方手中', final_state: '物体被接住', causal_facts: [{ id: 'fact-1', text: '先伸手，再接住', source_shot_ids: ['shot-1'], evidence: [{ frame_id: 'frame-1', timestamp: 1, description: '物体被递出' }], critical: true, kind: 'handoff' }], presentation, generation_mode: 'continuous', confidence: .8, status: 'ready', window_id: 'window-1', warnings: [] }],
  generation_clips: [clip('clip-1', 'continuous'), clip('clip-2', 'internal_cuts')], audit: [], warnings: [],
  usage_summary: { model_calls: 2, tool_calls: 1 }, source_coverage: [{ source_shot_id: 'shot-1', event_id: 'event-1' }, { source_shot_id: 'shot-2', event_id: 'event-1' }],
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(accept => { resolve = accept })
  return { promise, resolve }
}
function open(props: Partial<{ jobId: string | null; jobStatus: string; inputType: 'video' | 'script'; sourceShotIds: string[] }> = {}) {
  const wrapper = mount(EventAgentStudio, { props: { jobId: 'job-a', jobStatus: 'completed', inputType: 'video', sourceShotIds: ['shot-1', 'shot-2', 'shot-3'], ...props } })
  wrappers.push(wrapper); return wrapper
}
beforeEach(() => {
  vi.clearAllMocks(); vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  api.listEventAgentRuns.mockImplementation(async jobId => ({ job_id: jobId, runs: [] }))
  api.getEventAgentRun.mockResolvedValue(run())
  api.getEventAgentResult.mockResolvedValue(result())
  api.createEventAgentRun.mockResolvedValue(run('queued'))
  api.cancelEventAgentRun.mockResolvedValue(run('cancelled'))
  vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn(async () => undefined) } })
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('independent event storyboard experiment', () => {
  it('opens read-only and starts cloud work only after an explicit click', async () => {
    const wrapper = open(); await flushPromises()
    expect(api.listEventAgentRuns).toHaveBeenCalledWith('job-a', expect.any(AbortSignal))
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('云端 API'); expect(wrapper.text()).toContain('可能产生费用')
    expect(wrapper.text()).toContain('画布位置保持不变')
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="event-agent-start"]').trigger('click'); await flushPromises()
    expect(api.createEventAgentRun).toHaveBeenCalledTimes(1)
    expect(api.createEventAgentRun).toHaveBeenCalledWith('job-a', expect.any(AbortSignal))
    expect(wrapper.get('[data-testid="event-agent-start"]').attributes('disabled')).toBeDefined()
  })
  it.each([
    { jobId: null, jobStatus: 'completed', inputType: 'video' as const },
    { jobId: 'job-a', jobStatus: 'processing', inputType: 'video' as const },
    { jobId: 'job-a', jobStatus: 'completed', inputType: 'script' as const },
  ])('disables start unless the selected job is a completed video: %o', async props => {
    const wrapper = open(props); await flushPromises()
    expect(wrapper.get('[data-testid="event-agent-start"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="event-agent-start"]').trigger('click')
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
  })
  it('reads existing results, labels generation modes, and exposes exactly two complete prompts per clip', async () => {
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run()] })
    const wrapper = open(); await flushPromises()
    expect(api.getEventAgentResult).toHaveBeenCalledWith('job-a', 'run-a', expect.any(AbortSignal))
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
    expect(wrapper.findAll('[data-testid="event-agent-clip"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="event-agent-mode"]').map(item => item.text())).toEqual(['连续镜头', '内部切镜'])
    expect(wrapper.findAll('[data-testid="event-agent-prompt"]')).toHaveLength(4)
    expect(wrapper.get('[data-testid="event-agent-coverage"]').text()).toContain('重复特写')
    expect(wrapper.get('[data-testid="event-agent-coverage"]').text()).toContain('shot-3未覆盖')
    expect(wrapper.text()).toContain('来源归属覆盖 2 / 3')
    expect(wrapper.get('[data-testid="event-agent-usage"]').text()).toContain('1 / 4')
    expect(wrapper.get('[data-testid="event-agent-usage"]').text()).toContain('费用估算未提供')
    const copies = wrapper.findAll('[data-testid="event-agent-copy"]')
    await copies[0].trigger('click'); await copies[1].trigger('click'); await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenNthCalledWith(1, result().generation_clips[0].first_frame_prompt.positive_prompt)
    expect(navigator.clipboard.writeText).toHaveBeenNthCalledWith(2, result().generation_clips[0].seedance_prompt.positive_prompt)
  })
  it('polls only an active selected run and stops when the cached result becomes available', async () => {
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run('running')] })
    api.getEventAgentRun.mockResolvedValueOnce(run('running')).mockResolvedValueOnce(run())
    const wrapper = open(); await flushPromises()
    expect(wrapper.get('[data-testid="event-agent-status"]').text()).toContain('observing_window_2')
    expect(api.getEventAgentResult).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(api.getEventAgentRun).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="event-agent-result"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(0)
  })
  it('shows persisted window audit without fetching a partial result or invoking a model', async () => {
    const running = { ...run('running'), audit: [{ window_id: 'window-1', source_shot_ids: ['shot-2'], context_shot_ids: ['shot-1'], status: 'fallback' as const,
      validation_errors: ['关键交接证据未齐'], warnings: ['保留原镜头以保护事实'], usage: { model_calls: 2, tool_calls: 1 },
      tool_results: [{ name: 'crop_frame', status: 'error', error: '取证时间预算耗尽' }],
    }] }
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [running] })
    api.getEventAgentRun.mockResolvedValue(running)
    const wrapper = open(); await flushPromises()
    const audit = wrapper.get('[data-testid="event-agent-window-audit"]').text()
    expect(audit).toContain('归属镜头：shot-2'); expect(audit).toContain('只读上下文：shot-1')
    expect(audit).toContain('关键交接证据未齐'); expect(audit).toContain('查看局部 · error')
    expect(api.getEventAgentResult).not.toHaveBeenCalled(); expect(api.createEventAgentRun).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="event-agent-result"]').exists()).toBe(false)
  })
  it('cancels only the selected experiment on explicit click and stops polling', async () => {
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run('running')] })
    api.getEventAgentRun.mockResolvedValue(run('running'))
    const wrapper = open(); await flushPromises()
    expect(api.cancelEventAgentRun).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="event-agent-cancel"]').trigger('click'); await flushPromises()
    expect(api.cancelEventAgentRun).toHaveBeenCalledWith('job-a', 'run-a', expect.any(AbortSignal))
    expect(wrapper.text()).toContain('已取消')
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).toHaveBeenCalledTimes(1)
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
  })
  it('aborts old requests and ignores stale results when switching jobs', async () => {
    const oldResult = deferred<EventAgentResult>()
    api.listEventAgentRuns.mockImplementation(async jobId => ({ job_id: jobId, runs: jobId === 'job-a' ? [run()] : [] }))
    api.getEventAgentResult.mockReturnValue(oldResult.promise)
    const wrapper = open(); await flushPromises()
    const signal = api.getEventAgentResult.mock.calls[0][2] as AbortSignal
    await wrapper.setProps({ jobId: 'job-b', sourceShotIds: ['different-shot'] }); await flushPromises()
    expect(signal.aborted).toBe(true)
    oldResult.resolve(result()); await flushPromises()
    expect(wrapper.find('[data-testid="event-agent-result"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('递出并接住物体')
    expect(wrapper.get('[data-testid="event-agent-start"]').attributes('disabled')).toBeUndefined()
  })
  it('ignores a pending start response after switching to another job', async () => {
    const pending = deferred<EventAgentRun>(); api.createEventAgentRun.mockReturnValue(pending.promise)
    const wrapper = open(); await flushPromises()
    await wrapper.get('[data-testid="event-agent-start"]').trigger('click')
    const signal = api.createEventAgentRun.mock.calls[0][1] as AbortSignal
    await wrapper.setProps({ jobId: 'job-b' }); await flushPromises()
    pending.resolve(run('running')); await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(wrapper.find('[data-testid="event-agent-status"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).not.toHaveBeenCalled()
  })
  it.each(['close', 'unmount'] as const)('cleans polling and aborts requests on %s', async action => {
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run('running')] })
    api.getEventAgentRun.mockResolvedValue(run('running'))
    const wrapper = open(); await flushPromises()
    const signal = api.getEventAgentRun.mock.calls[0][2] as AbortSignal
    expect(vi.getTimerCount()).toBe(1)
    if (action === 'close') await wrapper.get('[data-testid="event-agent-close"]').trigger('click')
    else wrapper.unmount()
    expect(signal.aborted).toBe(true); expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).toHaveBeenCalledTimes(1)
    expect(api.cancelEventAgentRun).not.toHaveBeenCalled()
  })
  it('keeps history selection isolated and only polls the chosen active run', async () => {
    const older = { ...run('ready', 'job-a', 'older'), created_at: '2026-10-06T01:00:00Z' }
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run('running'), older] })
    api.getEventAgentRun.mockImplementation(async (_job, id) => id === 'older' ? older : run('running'))
    api.getEventAgentResult.mockResolvedValue(result('job-a', 'older'))
    const wrapper = open(); await flushPromises()
    await wrapper.get('[data-testid="event-agent-run-select"]').setValue('older'); await flushPromises()
    expect(wrapper.find('[data-testid="event-agent-result"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="event-agent-start"]').attributes('disabled')).toBeDefined()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).toHaveBeenCalledTimes(2)
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
  })
  it.each(['cancelled', 'interrupted', 'failed', 'degraded'] as const)('never resumes cloud work or polls a terminal %s run', async status => {
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run(status)] })
    api.getEventAgentRun.mockResolvedValue(run(status))
    const wrapper = open(); await flushPromises()
    await vi.advanceTimersByTimeAsync(10000); await flushPromises()
    expect(api.getEventAgentRun).toHaveBeenCalledTimes(1)
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
    expect(wrapper.get('[data-testid="event-agent-status"]').exists()).toBe(true)
  })
})

describe('event agent HTTP contract', () => {
  it('keeps GET reads separate from creation and cancellation and encodes job/run scopes', async () => {
    const actual = await vi.importActual<typeof import('../src/api/eventAgent')>('../src/api/eventAgent')
    const client = await vi.importActual<typeof import('../src/api/client')>('../src/api/client')
    client.clearApiSession()
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => ({ ok: true,
      json: async () => url.endsWith('/session') ? { enabled: true, token: 'isolated-test-session' } : {} }))
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal
    await actual.listEventAgentRuns('job/a', signal)
    await actual.getEventAgentRun('job/a', 'run/b', signal)
    await actual.getEventAgentResult('job/a', 'run/b', signal)
    await actual.createEventAgentRun('job/a', signal)
    await actual.cancelEventAgentRun('job/a', 'run/b', signal)
    const requests = fetchMock.mock.calls.filter(([url]) => !url.endsWith('/session')) as [string, RequestInit][]
    expect(requests.map(([url]) => url.replace(/^.*\/api/, ''))).toEqual([
      '/jobs/job%2Fa/event-agent/runs', '/jobs/job%2Fa/event-agent/runs/run%2Fb', '/jobs/job%2Fa/event-agent/runs/run%2Fb/result',
      '/jobs/job%2Fa/event-agent/runs', '/jobs/job%2Fa/event-agent/runs/run%2Fb/cancel',
    ])
    expect(requests.slice(0, 3).every(([, init]) => !init.method || init.method === 'GET')).toBe(true)
    expect(requests[3][1].method).toBe('POST'); expect(JSON.parse(requests[3][1].body as string)).toEqual({ config: {} })
    expect(requests[4][1].method).toBe('POST')
    expect(requests.every(([, init]) => init.signal === signal)).toBe(true)
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/session'))).toHaveLength(1)
    expect(new Headers(requests[3][1].headers).get('X-FrameForge-Session')).toBe('isolated-test-session')
    client.clearApiSession()
  })
})

describe('workbench event-agent entry', () => {
  async function workbench() {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
    const wrapper = mount(App, { global: { plugins: [createPinia()], stubs: { Background: true, TacticalMiniMap: true, WorkflowRail: true, Teleport: true } } })
    wrappers.push(wrapper); await flushPromises(); return wrapper
  }
  it('opens the experiment from the explicit task report without altering canvas/task data', async () => {
    const wrapper = await workbench(); const store = useWorkbenchStore()
    store.createDraft('video', { x: 160, y: 240 })
    const task = store.tasks[0]; task.jobId = 'job-a'; task.status = 'completed'; task.videoFilename = '交接.mp4'
    store.select(task.key)
    const setup = wrapper.vm.$.setupState as unknown as { buildGraph: () => void; openNodeDetails: (id: string, options: { tab: string }) => void }
    setup.buildGraph(); await flushPromises()
    expect(wrapper.find('[data-testid="event-agent-entry"]').exists()).toBe(false)
    setup.openNodeDetails(task.key, { tab: 'review' })
    await flushPromises(); await vi.dynamicImportSettled(); await flushPromises()
    expect(wrapper.get('[data-testid="event-agent-entry"]').element.closest('[data-canvas-details]')).not.toBeNull()
    expect(wrapper.find('.page-toolbar [data-testid="event-agent-entry"]').exists()).toBe(false)
    const originalTask = JSON.stringify(task)
    const originalNodes = JSON.stringify(wrapper.findComponent({ name: 'VueFlow' }).props('nodes'))
    api.listEventAgentRuns.mockResolvedValue({ job_id: 'job-a', runs: [run()] })
    await wrapper.get('[data-testid="event-agent-entry"]').trigger('click')
    await flushPromises(); await vi.dynamicImportSettled(); await flushPromises()
    expect(wrapper.find('[data-testid="event-agent-result"]').exists()).toBe(true)
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
    expect(JSON.stringify(task)).toBe(originalTask)
    expect(JSON.stringify(wrapper.findComponent({ name: 'VueFlow' }).props('nodes'))).toBe(originalNodes)
  })
  it('disables the task report experiment for unfinished video tasks and script tasks', async () => {
    const wrapper = await workbench(); const store = useWorkbenchStore()
    store.createDraft('video', { x: 20, y: 20 }); const task = store.tasks[0]
    task.jobId = 'job-a'; task.status = 'processing'; store.select(task.key); await flushPromises()
    const setup = wrapper.vm.$.setupState as unknown as { openNodeDetails: (id: string, options: { tab: string }) => void }
    setup.openNodeDetails(task.key, { tab: 'review' })
    await flushPromises(); await vi.dynamicImportSettled(); await flushPromises()
    expect(wrapper.get('[data-testid="event-agent-entry"]').attributes('disabled')).toBeDefined()
    task.status = 'completed'; task.inputType = 'script'; await flushPromises()
    expect(wrapper.get('[data-testid="event-agent-entry"]').attributes('disabled')).toBeDefined()
    expect(api.listEventAgentRuns).not.toHaveBeenCalled()
    expect(api.createEventAgentRun).not.toHaveBeenCalled()
  })
})
