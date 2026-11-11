import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PromptInspector from '../src/components/PromptInspector.vue'
import type { Prompt } from '../src/types'

const prompt: Prompt = {
  id: 'p1', shotId: 's1', type: 'video', platform: 'seedance', version: 2, text: '主体：卷发男孩。\n场景：厨房。',
  negativeText: '', status: 'ready', templateId: 'seedance-video-v2', templateVersion: 2, warnings: [],
  contract: {
    purpose: '忠实复现', firstFrameState: '手持勺子', primaryAction: '搅拌饮料', endState: '勺子举起',
    continuityAnchors: ['条纹上衣'], shotSize: '近景', lens: '标准', cameraMovement: '固定', lighting: '柔光', composition: '居中',
    observedStyle: '3D动画；大眼睛；暖米色厨房', generationSuggestions: { focal_length: '50mm（生成建议，不是原片实测）' },
    delivery: { schema_version: 'prompt-delivery.zh-v2', first_frame_intent: '冻结动作起点',
      first_frame_prompt: '【首帧图片提示词】\n主体：男孩手持勺子。', seedance_video_prompt: '【Seedance 中文图生视频提示词】\n主体：男孩搅拌饮料。',
      settings: ['声音为生成建议，未分析原片音轨'], continuity_checks: ['先检查手中道具一致'] },
  },
}

describe('PromptInspector structured Chinese delivery', () => {
  it.each(['resolved', 'partial', 'unknown'] as const)('shows %s style independently of prompt availability', (state) => {
    const wrapper = mount(PromptInspector, { props: { prompt: { ...prompt, contract: { ...prompt.contract!,
      delivery: { ...prompt.contract!.delivery, style_provenance: { resolution_status: state } } } } } })
    const label = { resolved: '画风已确定', partial: '画风部分确定', unknown: '画风未知' }[state]
    expect(wrapper.get('[data-testid="prompt-style-status"]').text()).toContain(label)
    expect(wrapper.get('textarea').element.value).toContain('主体：卷发男孩')
    wrapper.unmount()
  })
  it('shows observed style separately from generation suggestions and copies both Chinese prompts', async () => {
    const wrapper = mount(PromptInspector, { props: { prompt } })
    expect(wrapper.text()).toContain('3D动画')
    expect(wrapper.text()).toContain('不是原片实测')
    expect(wrapper.text()).toContain('冻结动作起点')
    const button = wrapper.findAll('button').find((node) => node.text().includes('复制提示词与交付说明'))!
    await button.trigger('click')
    const text = String(wrapper.emitted('copy')?.[0]?.[0])
    const sections = ['镜头合同', '首帧意图说明', '【首帧图片提示词】', '【Seedance 中文图生视频提示词】', 'Seedance 素材与设置建议', '连续性检查']
    const positions = sections.map((section) => text.indexOf(section))
    expect(positions.every((position) => position >= 0)).toBe(true)
    expect(positions).toEqual([...positions].sort((a, b) => a - b))
    expect(text).not.toContain('English')
    expect(text).not.toContain('Negative Prompt')
    wrapper.unmount()
  })

  it('keeps legacy prompts readable without delivery or style fields', () => {
    const wrapper = mount(PromptInspector, { props: { prompt: { ...prompt, version: 1, contract: undefined } } })
    expect(wrapper.get('textarea').element.value).toContain('主体：卷发男孩')
    expect(wrapper.text()).not.toContain('复制提示词与交付说明')
    wrapper.unmount()
  })
})
