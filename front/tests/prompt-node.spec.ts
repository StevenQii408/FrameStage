import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PromptNode from '../src/components/PromptNode.vue'

const prompt = {
  id: 'prompt-a', shotId: 'shot-a', type: 'video' as const, platform: 'Kling', version: 1,
  text: '主体从画面左侧走向窗边，镜头缓慢跟随，最终停在窗前。', negativeText: '', status: 'ready' as const,
  templateId: 'kling-video-v1', templateVersion: 1, warnings: [],
}

describe('PromptNode', () => {
  it('renders a compact transient card and keeps full text available through selection', async () => {
    const wrapper = mount(PromptNode, { props: { data: { prompt, compact: true, promptIndex: 2 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.classes()).toContain('prompt-node-compact')
    expect(wrapper.text()).toContain('点击查看完整提示词')
    await wrapper.trigger('click')
    expect(wrapper.emitted('select')).toEqual([['prompt-a']])
  })

  it('copies without changing the current selection', async () => {
    const wrapper = mount(PromptNode, { props: { data: { prompt, compact: true, promptIndex: 0 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('copy')).toEqual([[prompt.text]])
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('names the first-frame card as a prompt instead of a generated image', () => {
    const firstFrame = { ...prompt, type: 'first_frame' as const, platform: 'generic-image', templateId: 'first-frame-v1' }
    const wrapper = mount(PromptNode, { props: { data: { prompt: firstFrame, compact: true, promptIndex: 0 } }, global: { stubs: { Handle: true, StatusPill: true }, directives: { motion: {} } } })
    expect(wrapper.text()).toContain('首帧图片 Prompt')
  })
})
