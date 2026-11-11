import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PerceptionReviewPanel from '../src/components/PerceptionReviewPanel.vue'
import ShotInspector from '../src/components/ShotInspector.vue'
import type { PerceptionOperation, PerceptionRevision, PerceptionStatus } from '../src/api/perception'
import type { Shot } from '../src/types'

const api = vi.hoisted(() => ({ getPerception: vi.fn(), getPerceptionHistory: vi.fn(), createPerceptionOperation: vi.fn(), getPerceptionOperation: vi.fn(), cancelPerceptionOperation: vi.fn() }))
vi.mock('../src/api/perception', () => api)
const wrappers: VueWrapper[] = []
function operation(state: PerceptionOperation['status'] = 'running'): PerceptionOperation {
  return { id: 'operation-a', job_id: 'job-a', shot_id: 'shot-a', mode: 'recover', status: state, source_revision: 2,
    created_at: '2026-10-10T00:00:00Z', request: { stages: ['motion'] }, result: {}, usage: [] }
}
function saved(): PerceptionStatus {
  return { job_id: 'job-a', shot_id: 'shot-a', revision: 2, record_available: true, read_only: false,
    stages: { static: { state: 'succeeded', input_frames: [{ frame_id: 'frame-a' }], usage: [{ estimated_list_price_cny: .001 }] },
      motion: { state: 'failed', error: { message: '网络请求中断' }, input_frames: [], usage: [] } },
    recoverable_stages: ['motion'], operations: [] }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(accept => { resolve = accept })
  return { promise, resolve }
}
function open(props: Partial<{ jobId: string | null; shotId: string }> = {}) {
  const wrapper = mount(PerceptionReviewPanel, { props: { jobId: 'job-a', shotId: 'shot-a', ...props } })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  api.getPerception.mockResolvedValue(saved())
  api.getPerceptionHistory.mockResolvedValue({ revisions: [] })
  api.createPerceptionOperation.mockResolvedValue(operation('queued'))
  api.getPerceptionOperation.mockResolvedValue(operation())
  api.cancelPerceptionOperation.mockResolvedValue(operation('cancelled'))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })

describe('explicit perception recovery and review', () => {
  it('manual refresh only reads saved records and recovers from read errors without paid retries', async () => {
    api.getPerception.mockRejectedValueOnce(new Error('读取失败'))
    const wrapper = open(); await flushPromises()
    expect(wrapper.text()).toContain('读取失败')
    await wrapper.get('[data-testid="perception-refresh"]').trigger('click'); await flushPromises()
    expect(api.getPerception).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="perception-stage-static"]').text()).toContain('已完成')
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
  })
  it('reads saved stages without cloud writes and offers recovery only for the failed stage', async () => {
    const wrapper = open(); await flushPromises()
    expect(api.getPerception).toHaveBeenCalledWith('job-a', 'shot-a', expect.any(AbortSignal))
    expect(wrapper.get('[data-testid="perception-stage-static"]').text()).toContain('已完成')
    expect(wrapper.get('[data-testid="perception-stage-motion"]').text()).toContain('调用失败')
    expect(wrapper.text()).toContain('网络请求中断')
    expect(wrapper.text()).toContain('可能产生费用')
    expect(wrapper.text()).toContain('¥0.0010')
    expect(wrapper.find('[data-testid="perception-recover-static"]').exists()).toBe(false)
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.getPerceptionOperation).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="perception-recover-motion"]').trigger('click'); await flushPromises()
    expect(api.createPerceptionOperation).toHaveBeenCalledTimes(1)
    const [jobId, shotId, request] = api.createPerceptionOperation.mock.calls[0]
    expect([jobId, shotId]).toEqual(['job-a', 'shot-a'])
    expect(request).toMatchObject({ mode: 'recover', stages: ['motion'], expected_revision: 2 })
    expect(request.idempotency_key).toMatch(/^[0-9a-f-]{36}$/)
    expect(wrapper.get('[data-testid="perception-recover-motion"]').attributes('disabled')).toBeDefined()
  })
  it('requires a user reason and sends only the selected fact group on review', async () => {
    const wrapper = open(); await flushPromises()
    expect(wrapper.get('[data-testid="perception-review-start"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="perception-field-group"]').setValue('subject')
    await wrapper.get('[data-testid="perception-reason"]').setValue('请核对人物手中是否有杯子')
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="perception-review-start"]').trigger('click'); await flushPromises()
    const request = api.createPerceptionOperation.mock.calls[0][2]
    expect(request).toMatchObject({ mode: 'review', field_group: 'subject', reason: '请核对人物手中是否有杯子', expected_revision: 2 })
    expect(request.stages).toBeUndefined()
  })
  it.each([false, true])('disables writes for a historical missing record or an active job: %s', async readOnly => {
    api.getPerception.mockResolvedValue({ ...saved(), read_only: readOnly, record_available: readOnly, stages: {}, recoverable_stages: [] })
    const wrapper = open(); await flushPromises()
    expect(wrapper.get('[data-testid="perception-review-start"]').attributes('disabled')).toBeDefined()
    if (!readOnly) expect(wrapper.text()).toContain('没有可恢复的分析记录')
    await wrapper.get('[data-testid="perception-review-start"]').trigger('click')
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
  })
  it('does not fetch an unsaved shot and keeps its controls disabled', async () => {
    const wrapper = open({ jobId: null }); await flushPromises()
    expect(api.getPerception).not.toHaveBeenCalled()
    expect(api.getPerceptionHistory).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="perception-review-start"]').attributes('disabled')).toBeDefined()
  })
  it('polls saved active work without restarting it, then refreshes the shot once publication completes', async () => {
    api.getPerception.mockResolvedValueOnce({ ...saved(), operations: [operation()] }).mockResolvedValue({ ...saved(), revision: 3 })
    api.getPerceptionOperation.mockResolvedValue({ ...operation('applied'), result: { revision: 3 }, usage: [{ estimated_list_price_cny: .002 }] })
    const wrapper = open(); await flushPromises()
    expect(api.createPerceptionOperation).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1500); await flushPromises()
    expect(wrapper.emitted('updated')).toEqual([['job-a']])
    expect(wrapper.get('[data-testid="perception-operation"]').text()).toContain('已更新事实')
    expect(wrapper.get('[data-testid="perception-operation"]').text()).toContain('¥0.0020')
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.getPerceptionOperation).toHaveBeenCalledTimes(1)
  })
  it('stops polling on explicit cancellation and ignores a late in-flight result', async () => {
    api.getPerception.mockResolvedValue({ ...saved(), operations: [operation()] })
    const pending = deferred<PerceptionOperation>()
    api.getPerceptionOperation.mockReturnValue(pending.promise)
    const wrapper = open(); await flushPromises()
    await vi.advanceTimersByTimeAsync(1500)
    const signal = api.getPerceptionOperation.mock.calls[0][3] as AbortSignal
    await wrapper.get('[data-testid="perception-cancel"]').trigger('click'); await flushPromises()
    expect(api.cancelPerceptionOperation).toHaveBeenCalledWith('job-a', 'shot-a', 'operation-a', expect.any(AbortSignal))
    expect(signal.aborted).toBe(true)
    pending.resolve({ ...operation('applied'), result: { revision: 3 } }); await flushPromises()
    expect(wrapper.text()).toContain('已取消')
    expect(wrapper.emitted('updated')).toBeUndefined()
    expect(vi.getTimerCount()).toBe(0)
  })
  it('aborts old shot reads and rejects their late data after switching source scope', async () => {
    const pending = deferred<PerceptionStatus>()
    api.getPerception.mockReturnValueOnce(pending.promise).mockResolvedValue({ ...saved(), job_id: 'job-b', shot_id: 'shot-b', record_available: false, stages: {} })
    const wrapper = open(); await flushPromises()
    const signal = api.getPerception.mock.calls[0][2] as AbortSignal
    await wrapper.setProps({ jobId: 'job-b', shotId: 'shot-b' }); await flushPromises()
    pending.resolve({ ...saved(), operations: [operation()] }); await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(wrapper.find('[data-testid="perception-operation"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('没有可恢复的分析记录')
    expect(vi.getTimerCount()).toBe(0)
  })
  it('does not publish a pending operation response into another shot', async () => {
    const pending = deferred<PerceptionOperation>()
    api.createPerceptionOperation.mockReturnValue(pending.promise)
    const wrapper = open(); await flushPromises()
    await wrapper.get('[data-testid="perception-recover-motion"]').trigger('click')
    const signal = api.createPerceptionOperation.mock.calls[0][3] as AbortSignal
    await wrapper.setProps({ shotId: 'shot-b' }); await flushPromises()
    pending.resolve(operation()); await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(wrapper.find('[data-testid="perception-operation"]').exists()).toBe(false)
    expect(api.getPerceptionOperation).not.toHaveBeenCalled()
  })
  it('shows persisted before/after patches and stops polling on unmount without cancelling the server', async () => {
    const revision: PerceptionRevision = { version: 3, operation_id: 'operation-a', created_at: '', before: {}, after: {}, changes: {
      mode: 'review', field_group: 'action', changed: true, revisions: [{ rationale: '新增帧显示杯子仍在手中', patches: [{ field: 'dynamics.final_state', old: { value: '杯子已放下' }, new: { value: '杯子仍在手中' } }] }],
    } }
    api.getPerception.mockResolvedValue({ ...saved(), operations: [operation()] })
    api.getPerceptionHistory.mockResolvedValue({ revisions: [revision] })
    const wrapper = open(); await flushPromises()
    expect(wrapper.get('[data-testid="perception-history"]').text()).toContain('此前：杯子已放下')
    expect(wrapper.get('[data-testid="perception-history"]').text()).toContain('现在：杯子仍在手中')
    const signal = api.getPerception.mock.calls[0][2] as AbortSignal
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    expect(api.cancelPerceptionOperation).not.toHaveBeenCalled()
  })
  it('replaces the inactive reanalysis button and forwards the real source scope through ShotInspector', async () => {
    const shot: Shot = { id: 'shot-a', number: 1, time: '0–3', duration: '3s', action: '拿起杯子', shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .8,
      thumb: '', status: 'ready', frames: [], analysisWarnings: [], sourceRawShotIds: [], sceneGroupId: 'scene-a' }
    const wrapper = mount(ShotInspector, { props: { shot, jobId: 'job-source' } }); wrappers.push(wrapper)
    await flushPromises()
    expect(api.getPerception).toHaveBeenCalledWith('job-source', 'shot-a', expect.any(AbortSignal))
    expect(wrapper.text()).not.toContain('重新分析此镜头')
    wrapper.getComponent(PerceptionReviewPanel).vm.$emit('updated', 'job-source')
    expect(wrapper.emitted('updated')).toEqual([['job-source']])
    await wrapper.setProps({ shot: { ...shot, script_direction: { schema_version: 'script-director.v3' } } })
    expect(wrapper.findComponent(PerceptionReviewPanel).exists()).toBe(false)
  })
})
