import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ProjectNode from '../src/components/ProjectNode.vue'

describe('ProjectNode', () => {
  it('plays the persisted original video on demand and pauses when closed', async () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'video', jobId: 'saved-video', filename: '原片.mp4', status: 'ready' } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(document.querySelector('video')).toBeNull()
    const open = wrapper.findAll('button').find(button => button.text().includes('查看原视频'))!
    await open.trigger('click')
    await flushPromises()
    await vi.waitFor(() => expect(document.querySelector('video')).not.toBeNull())
    const video = document.querySelector('video')!
    expect(video.src).toContain('/jobs/saved-video/video')
    expect(video.preload).toBe('metadata')
    ;(document.querySelector('[aria-label="关闭原视频预览"]') as HTMLButtonElement).click()
    await wrapper.vm.$nextTick()
    expect(pause).toHaveBeenCalled()
    expect(document.querySelector('video')).toBeNull()
    wrapper.unmount()
    pause.mockRestore()
  })
  it('places native source cut markers without introducing artificial generation boundaries', async () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'video', jobId: 'cut-video', status: 'ready', rawCutTimes: [2.1, 4.7] } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    await wrapper.findAll('button').find(button => button.text().includes('查看原视频'))!.trigger('click')
    await vi.waitFor(() => expect(document.querySelector('video')).not.toBeNull())
    const video = document.querySelector('video')!
    Object.defineProperty(video, 'duration', { configurable: true, value: 6 })
    video.dispatchEvent(new Event('loadedmetadata'))
    await wrapper.vm.$nextTick()
    expect(document.querySelectorAll('.player-cut')).toHaveLength(2)
    expect(document.querySelectorAll('.player-cut')[0].getAttribute('style')).toContain('35%')
    expect(document.querySelector('.source-video-dialog')?.textContent).toContain('本地检测的原片切镜点')
    wrapper.unmount(); pause.mockRestore()
  })
  it('offers checkpoint recovery for interrupted jobs', async () => {
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'video', status: 'interrupted', provider: 'bailian', jobId: 'interrupted-job' } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.text()).toContain('已保存的阶段会保留')
    await wrapper.get('.project-actions button').trigger('click')
    expect(wrapper.emitted('resume')).toHaveLength(1)
    wrapper.unmount()
  })
  it('rejects an oversized script without truncating the original textarea', async () => {
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'script', status: 'queued' } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    const original = '甲'.repeat(10000) + '结尾'
    await wrapper.get('textarea').setValue(original)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.get('[role=alert]').text()).toContain('原文不会截断')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe(original)
    expect(wrapper.emitted('submit')).toBeUndefined()
    wrapper.unmount()
  })
  it('shows the actual source-to-sequence summary inside the task card rather than a global toolbar', () => {
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'video', status: 'ready', jobId: 'job-a', progress: 100, shotCount: 9, sequenceCount: 7 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.get('.project-inputs').text()).toContain('9 个来源镜头 → 7 个生成片段')
    wrapper.unmount()
  })
  it('exposes one explicit cascading root delete action', async () => {
    const wrapper = mount(ProjectNode, { props: { data: { index: 1, inputType: 'script', title: '剧本输入', status: 'ready', jobId: 'job-a', progress: 100, shotCount: 11 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.findAll('.magic-particle')).toHaveLength(16)
    expect(wrapper.findAll('.magic-aurora')).toHaveLength(2)
    expect(wrapper.findAll('.magic-comet')).toHaveLength(2)
    expect(wrapper.findAll('.magic-shimmer')).toHaveLength(1)
    expect(wrapper.classes()).toContain('project-node-script')
    await wrapper.get('[aria-label="删除输入节点及其全部子节点"]').trigger('click')
    expect(wrapper.emitted('delete')).toHaveLength(1)
  })

  it('raises particle intensity while a task is running', () => {
    const wrapper = mount(ProjectNode, { props: { data: { index: 1, inputType: 'video', status: 'processing', progress: 42 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.classes()).toContain('project-node-active')
    expect(wrapper.classes()).toContain('project-node-video')
  })
  it('explains that a paused MLX task does not migrate to cloud automatically', () => {
    const wrapper = mount(ProjectNode, { props: { data: { index: 2, inputType: 'video', status: 'paused_memory_pressure', provider: 'mlx', jobId: 'job-old', progress: 30 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.text()).toContain('切换云端不会自动迁移此任务')
    expect(wrapper.text()).toContain('释放内存后继续')
  })
  it('offers explicit cloud-network recovery without deleting successful shots', async () => {
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'video', status: 'failed', provider: 'bailian', jobId: 'network-job', error: '连续两个镜头云端连接失败，已停止后续付费分析' } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.text()).toContain('已完成的镜头会保留')
    await wrapper.get('.project-actions button').trigger('click')
    expect(wrapper.emitted('resume')).toHaveLength(1)
    wrapper.unmount()
  })
  it('does not offer network recovery for ordinary failures', () => {
    const wrapper = mount(ProjectNode, { props: { data: { inputType: 'video', status: 'failed', provider: 'bailian', jobId: 'bad-video', error: '视频格式错误' } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.text()).not.toContain('网络恢复后继续')
    wrapper.unmount()
  })
})
