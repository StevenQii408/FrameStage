import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import NodeDetailsHost from '../src/components/NodeDetailsHost.vue'
import type { DetailTarget } from '../src/nodeDetails'
import { detailIdentity, detailSourceRequest } from '../src/nodeDetails'
import { clearPerceptionDrafts } from '../src/perceptionDrafts'
import type { GenerationSequence, Prompt, Shot, WorkbenchTask } from '../src/types'

const api = vi.hoisted(() => ({ getPerception: vi.fn(), getPerceptionHistory: vi.fn(), createPerceptionOperation: vi.fn(), getPerceptionOperation: vi.fn(), cancelPerceptionOperation: vi.fn() }))
vi.mock('../src/api/perception', () => api)
const wrappers: VueWrapper[] = []
function source(job: string, number: number): Shot {
  return { id: job + '-shot-' + number, number, time: '0–3s', duration: '3s', action: '递出玻璃杯', shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .9,
    thumb: '/frame-' + number + '.jpg', frames: [{ id: job + '-frame-' + number, timestamp: .1, url: '/frame-' + number + '.jpg', width: 1600, height: 900 }], status: 'ready',
    analysisWarnings: [], sourceRawShotIds: ['raw-' + number], sceneGroupId: 'scene-a',
    actionDetails: { primarySubject: '女子', initialState: '持杯', steps: ['递出杯子'], interactionTarget: '男孩', finalState: '杯子递出', emotionChange: '', evidence: [] },
    spatialEvidence: { foreground: '近处玻璃杯', midground: '两个人物', background: '室内', relationships: '人物相对', observable: true } }
}
function fixture(job = 'job-a'): WorkbenchTask {
  const shots = [source(job, 1), source(job, 2)]
  const prompt: Prompt = { id: job + '-prompt', shotId: shots[0].id, type: 'video', platform: 'seedance', version: 3, text: '主体：女子。\n0–5秒：递出杯子。', negativeText: '无额外杯子。', status: 'ready', templateId: 'seedance-video-v3', templateVersion: 3, warnings: [],
    contract: { purpose: '递杯', firstFrameState: '持杯', primaryAction: '递杯', endState: '杯子递出', continuityAnchors: ['玻璃杯'], shotSize: '中景', lens: '标准', cameraMovement: '固定', lighting: '柔光', composition: '居中' } }
  const sequence: GenerationSequence = { id: job + '-sequence', event_id: job + '-event', title: '递杯完整事件', main_action: '女子递杯，男孩接杯', source_shot_ids: shots.map(shot => shot.id), source_start_time: 0, source_end_time: 6, source_duration_seconds: 6, generation_duration_seconds: 8, status: 'ready', warnings: [],
    timeline: shots.map((shot, index) => ({ source_shot_id: shot.id, source_start_time: index * 3, source_end_time: index * 3 + 3, generation_start_seconds: index * 4, generation_end_seconds: index * 4 + 4,
      shot_size: '中景', camera_movement: '固定', source_edit_times: [], transition: index ? 'continuation' : 'initial', action: shot.action, initial_state: '持杯', final_state: '杯子递出' })),
    seedance_prompt: { id: job + '-sequence-prompt', prompt_type: 'video', platform: 'seedance', version: 1, positive_prompt: '完整事件 Prompt 正文', status: 'ready' } }
  return { key: 'task-' + job, jobId: job, inputType: 'video', status: 'completed', stage: 'completed', progress: 100, error: null, videoFilename: job + '.mp4', provider: 'mock', model: 'mock', segmentationProfile: 'test', rawShotCount: 2, narrativeShotCount: 2,
    shots, prompts: [prompt], rawShots: [{ id: 'raw-1', number: 1, startTime: 0, endTime: 3, duration: 3, decision: '保留', reason: '动作完成', boundaryMetrics: {}, narrativeShotId: shots[0].id }],
    generationSequences: [sequence], position: { x: 10, y: 20 }, preflight: { source_shot_count: 2, model_calls: { normal_estimate: 3, bounded_maximum: 6 } },
    sequenceMetadata: { fact_review_requests: [{ source_shot_id: shots[0].id, status: 'pending', reason: '请核对杯子状态' }] } }
}
function target(task: WorkbenchTask, kind: DetailTarget['kind'] = 'shot', id = task.shots[0].id): DetailTarget {
  return { projectId: 'project-a', taskKey: task.key, jobId: task.jobId, kind, id }
}
async function open(task = fixture(), item = target(task), extra = {}) {
  const wrapper = mount(NodeDetailsHost, { props: { task, target: item, ...extra }, global: { stubs: { Teleport: true } }, attachTo: document.body })
  wrappers.push(wrapper)
  await vi.dynamicImportSettled(); await flushPromises()
  return wrapper
}
async function tab(wrapper: VueWrapper, label: string) {
  await wrapper.findAll('[role="tab"]').find(item => item.text() === label)!.trigger('click')
  await vi.dynamicImportSettled(); await flushPromises()
}
beforeEach(() => {
  clearPerceptionDrafts()
  api.getPerception.mockImplementation(async (jobId, shotId) => ({ job_id: jobId, shot_id: shotId, revision: 1, record_available: true, read_only: false, stages: {}, recoverable_stages: [], operations: [] }))
  api.getPerceptionHistory.mockResolvedValue({ revisions: [] })
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); document.body.innerHTML = ''; clearPerceptionDrafts() })

describe('single on-demand node details host', () => {
  it('starts with a nonmodal summary and defers visual review requests until the review tab', async () => {
    const task = fixture(), wrapper = await open(task)
    const host = wrapper.get('[data-canvas-details]')
    expect(host.attributes()).toMatchObject({ role: 'dialog', 'aria-modal': 'false', 'data-detail-id': task.shots[0].id })
    expect(wrapper.text()).toContain('递出玻璃杯')
    expect(wrapper.text()).not.toContain('近处玻璃杯')
    expect(wrapper.find('[data-testid="perception-panel"]').exists()).toBe(false)
    expect(api.getPerception).not.toHaveBeenCalled()
    await tab(wrapper, '证据与来源')
    expect(wrapper.text()).toContain('近处玻璃杯')
    expect(wrapper.text()).toContain('原始切点与合并理由')
    expect(api.getPerception).not.toHaveBeenCalled()
    await tab(wrapper, '检查与记录')
    expect(api.getPerception).toHaveBeenCalledWith(task.jobId, task.shots[0].id, expect.any(AbortSignal))
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
    const signal = api.getPerception.mock.calls[0][2] as AbortSignal
    await tab(wrapper, '概览')
    expect(signal.aborted).toBe(true)
    expect(api.cancelPerceptionOperation).not.toHaveBeenCalled()
  })
  it('routes sequence evidence and Prompt entry using an explicit owning task and return context', async () => {
    const task = fixture(), sequence = task.generationSequences![0]
    const wrapper = await open(task, target(task, 'sequence', sequence.id))
    expect(wrapper.find('[data-testid="sequence-prompts"]').exists()).toBe(false)
    await wrapper.get('.sequence-prompt-links button').trigger('click')
    expect(wrapper.emitted('openPrompt')?.[0]).toEqual([{ taskKey: task.key, promptId: sequence.seedance_prompt!.id }])
    await tab(wrapper, '证据与来源')
    await wrapper.get('[data-testid="sequence-timeline"] button').trigger('click')
    expect(wrapper.emitted('sourceShot')?.[0]).toEqual([{ taskKey: task.key, shotId: task.shots[0].id, returnSequenceId: sequence.id }])
    await wrapper.setProps({ target: { ...target(task), returnSequenceId: sequence.id } })
    await flushPromises()
    await wrapper.get('.node-details-return').trigger('click')
    expect(wrapper.emitted('sequence')?.[0]).toEqual([{ taskKey: task.key, sequenceId: sequence.id }])
  })
  it('keeps task reports, pending reviews and the explicit event experiment entry accessible', async () => {
    const task = fixture(), wrapper = await open(task, target(task, 'task', task.key))
    expect(wrapper.text()).toContain('处理范围与调用预算')
    expect(wrapper.text()).not.toContain('Agent 建议复核')
    await tab(wrapper, '复核与实验')
    expect(wrapper.text()).toContain('Agent 建议复核')
    await wrapper.get('.task-review-panel button').trigger('click')
    expect(wrapper.emitted('sourceShot')?.[0]).toEqual([{ taskKey: task.key, shotId: task.shots[0].id }])
    await wrapper.get('.node-details-experiment').trigger('click')
    expect(wrapper.emitted('openEventAgent')?.[0]).toEqual([{ taskKey: task.key, jobId: task.jobId }])
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
  })
  it('renders explicit historical scene presentation groups without inventing a semantic scene', async () => {
    const task = fixture(), wrapper = await open(task, target(task, 'scene', 'legacy-group'), {
      scene: { id: 'legacy-group', title: '历史展示分组', shotIds: task.shots.map(item => item.id), groupingMode: 'legacy-presentation-group' },
    })
    expect(wrapper.text()).toContain('不代表已识别的剧情场次')
    await wrapper.get('.node-details-source-list button').trigger('click')
    expect(wrapper.emitted('sourceShot')?.[0]).toEqual([{ taskKey: task.key, shotId: task.shots[0].id }])
  })
  it('opens a projected sequence from a scene as a sequence rather than a nonexistent raw source', async () => {
    const task = fixture(), sequence = task.generationSequences![0]
    const wrapper = await open(task, target(task, 'scene', 'scene-display'), { scene: { id: 'scene-display', title: '事件编排', shotIds: [sequence.id] } })
    expect(wrapper.findAll('[role="tab"]')).toHaveLength(1)
    await wrapper.get('.node-details-source-list button').trigger('click')
    expect(wrapper.emitted('sequence')?.[0]).toEqual([{ taskKey: task.key, sequenceId: sequence.id }])
    expect(wrapper.emitted('sourceShot')).toBeUndefined()
  })
  it('shares a read-only Prompt reader and marks its full preview as a true child modal', async () => {
    const task = fixture(), prompt = task.prompts[0], wrapper = await open(task, target(task, 'prompt', prompt.id))
    const input = wrapper.get('textarea[aria-label="AIGC Prompt"]')
    expect(input.attributes('readonly')).toBeDefined()
    expect((input.element as HTMLTextAreaElement).value).toBe(prompt.text + '\n\nNegative Prompt:\n' + prompt.negativeText)
    await wrapper.findAll('button').find(item => item.text() === '全部预览')!.trigger('click')
    expect(wrapper.get('[data-canvas-modal][role="dialog"]').attributes('aria-modal')).toBe('true')
    await wrapper.get('[data-canvas-modal][role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[data-canvas-modal][role="dialog"]').exists()).toBe(false)
    expect(wrapper.emitted('close')).toBeUndefined()
    await wrapper.findAll('button').find(item => item.text() === '全部预览')!.trigger('click')
    const changed = { ...prompt, id: 'another-prompt', text: '另一个镜头' }
    await wrapper.setProps({ prompt: changed, target: target(task, 'prompt', changed.id) })
    await flushPromises()
    expect(wrapper.find('[data-canvas-modal][role="dialog"]').exists()).toBe(false)
    expect((wrapper.get('textarea[aria-label="AIGC Prompt"]').element as HTMLTextAreaElement).value).toContain('另一个镜头')
  })
  it('preserves review drafts across tab unmounts and isolates them by project, task and shot', async () => {
    const task = fixture(), first = target(task), wrapper = await open(task)
    await tab(wrapper, '检查与记录')
    await wrapper.get('[data-testid="perception-field-group"]').setValue('subject')
    await wrapper.get('[data-testid="perception-reason"]').setValue('杯子还在手中，请检查')
    await tab(wrapper, '证据与来源')
    await tab(wrapper, '检查与记录')
    expect((wrapper.get('[data-testid="perception-reason"]').element as HTMLTextAreaElement).value).toBe('杯子还在手中，请检查')
    await wrapper.setProps({ target: target(task, 'shot', task.shots[1].id), initialTab: 'review' })
    await flushPromises()
    expect((wrapper.get('[data-testid="perception-reason"]').element as HTMLTextAreaElement).value).toBe('')
    await wrapper.setProps({ target: first })
    await flushPromises()
    expect((wrapper.get('[data-testid="perception-reason"]').element as HTMLTextAreaElement).value).toBe('杯子还在手中，请检查')
    expect((wrapper.get('[data-testid="perception-field-group"]').element as HTMLSelectElement).value).toBe('subject')
    const otherTask = { ...task, key: 'another-task' }
    await wrapper.setProps({ task: otherTask, target: { ...first, taskKey: otherTask.key } })
    await flushPromises()
    expect((wrapper.get('[data-testid="perception-reason"]').element as HTMLTextAreaElement).value).toBe('')
    await wrapper.setProps({ task, target: first })
    await flushPromises()
    expect((wrapper.get('[data-testid="perception-reason"]').element as HTMLTextAreaElement).value).toBe('杯子还在手中，请检查')
    await wrapper.setProps({ target: { ...first, projectId: 'project-b' } })
    await flushPromises()
    expect((wrapper.get('[data-testid="perception-reason"]').element as HTMLTextAreaElement).value).toBe('')
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
  })
  it('does not intercept canvas undo, supports tab keys, and closes without changing domain state', async () => {
    const wrapper = await open(), host = wrapper.get('[data-canvas-details]')
    const undo = new KeyboardEvent('keydown', { key: 'z', metaKey: true, bubbles: true, cancelable: true })
    host.element.dispatchEvent(undo)
    expect(undo.defaultPrevented).toBe(false)
    await wrapper.get('[role="tab"][aria-selected="true"]').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.get('[role="tab"][aria-selected="true"]').text()).toBe('证据与来源')
    await host.trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
    expect(api.cancelPerceptionOperation).not.toHaveBeenCalled()
  })
  it('moves only its presentation coordinates from the title and rejects a mismatched task scope', async () => {
    const task = fixture(), wrapper = await open(task), header = wrapper.get('.node-details-header')
    const before = (wrapper.get('[data-canvas-details]').element as HTMLElement).style.left
    await header.trigger('pointerdown', { button: 0, pointerId: 1, clientX: 100, clientY: 100 })
    await header.trigger('pointermove', { pointerId: 1, clientX: 160, clientY: 130 })
    await header.trigger('pointerup', { pointerId: 1 })
    expect((wrapper.get('[data-canvas-details]').element as HTMLElement).style.left).not.toBe(before)
    expect(task.position).toEqual({ x: 10, y: 20 })
    await wrapper.setProps({ target: { ...target(task), taskKey: 'another-task' } })
    await flushPromises()
    expect(wrapper.text()).toContain('数据暂不可用')
    expect(wrapper.find('[data-testid="perception-panel"]').exists()).toBe(false)
  })
  it('keeps the model-free detail identity and source context independent of active workspace state', () => {
    const task = fixture(), item = { ...target(task), returnSequenceId: 'old-sequence' }
    expect(detailIdentity(item)).not.toBe(detailIdentity({ ...item, projectId: 'other-project' }))
    expect(detailSourceRequest(item, task.shots[1].id)).toEqual({ taskKey: task.key, shotId: task.shots[1].id, returnSequenceId: 'old-sequence' })
  })
})
