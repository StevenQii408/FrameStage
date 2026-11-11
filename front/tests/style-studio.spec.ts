import { mount, flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import StyleStudio from '../src/components/StyleStudio.vue'

const profile = {
  schema_version: 'style-profile.v1', status: 'draft', mode: 'creative', version: 1,
  global_terms: ['medium.two-d-animation'], optional_anchors: [], expanded_anchor_traits: [],
  include_anchor_names: true, negative_constraints: [], per_shot_effect_overrides: {},
  rendered_style_prompt: '整体画面风格：测试画风。', profile_hash: 'abc',
}
const catalog = {
  version: 'style-ontology.v1', anchors: { warhol: { name: '安迪·沃霍尔', type: 'artist', traits: [] } },
  terms: [
    { id: 'medium.two-d-animation', category: 'medium', zh_label: '二维手绘动画', definition: '可见平面笔触', generation_phrase: '二维手绘动画', observable_cues: ['平面笔触'], conflicts_with: [] },
    { id: 'medium.photography', category: 'medium', zh_label: '写实摄影', definition: '真实镜头', generation_phrase: '写实摄影', observable_cues: ['真实光学'], conflicts_with: [] },
    { id: 'visible_effect.film-grain', category: 'visible_effect', zh_label: '胶片颗粒', definition: '细颗粒', generation_phrase: '胶片颗粒', observable_cues: ['颗粒'], conflicts_with: [] },
  ],
}
const api = vi.hoisted(() => ({
  getJobStyle: vi.fn(), getStyleCatalog: vi.fn(), regenerateJobStyle: vi.fn(),
  saveJobStyle: vi.fn(), confirmJobStyle: vi.fn(),
}))
vi.mock('../src/api/jobs', () => api)

describe('StyleStudio', () => {
  it('does not apply a draft until explicit confirmation and keeps effects per shot', async () => {
    api.getJobStyle.mockResolvedValue({ profile: structuredClone(profile), observation: null })
    api.getStyleCatalog.mockResolvedValue(catalog)
    api.saveJobStyle.mockImplementation(async (_id, draft) => ({ ...draft, status: 'draft', version: 2, rendered_style_prompt: '整体画面风格：已保存。' }))
    api.confirmJobStyle.mockResolvedValue({ ...profile, status: 'confirmed', version: 2 })
    const wrapper = mount(StyleStudio, { props: { jobId: 'j1', inputType: 'script', shotIds: ['s1', 's2'], selectedShotId: 's1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('整体画面风格：测试画风')
    expect(api.confirmJobStyle).not.toHaveBeenCalled()
    const effect = wrapper.findAll('button').find((node) => node.text() === '胶片颗粒')!
    await effect.trigger('click')
    await wrapper.findAll('button').find((node) => node.text().includes('保存草案'))!.trigger('click')
    await flushPromises()
    expect(api.saveJobStyle).toHaveBeenCalled()
    expect(api.saveJobStyle.mock.calls[0][1].per_shot_effect_overrides).toEqual({ s1: ['visible_effect.film-grain'] })
    expect(api.confirmJobStyle).not.toHaveBeenCalled()
    await wrapper.findAll('button').find((node) => node.text().includes('确认并应用'))!.trigger('click')
    await flushPromises()
    expect(api.confirmJobStyle).toHaveBeenCalledWith('j1')
    expect(wrapper.emitted('applied')?.[0]).toEqual(['j1'])
    wrapper.unmount()
  })

  it("retains sparse frame evidence and lets a user complete a draft manually", async () => {
    api.getJobStyle.mockResolvedValue({ profile: null, observation: {
      mode: "observed", dimensions: { medium: [{ term_id: "medium.two-d-animation", confidence: .9,
        evidence: [{ frame_id: "frame-1", timestamp: 1.2, description: "平面角色" }] }] },
      uncertainties: ["画风草案需人工补全：维度不足"],
    } })
    api.getStyleCatalog.mockResolvedValue(catalog)
    const wrapper = mount(StyleStudio, { props: { jobId: "j1", inputType: "video", shotIds: ["s1"] } })
    await flushPromises()
    expect(wrapper.text()).toContain("维度不足")
    expect(wrapper.text()).toContain("frame-1")
    await wrapper.findAll("button").find((node) => node.text().includes("人工补全"))!.trigger("click")
    expect(wrapper.text()).toContain("全局视觉维度")
    expect(api.confirmJobStyle).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
