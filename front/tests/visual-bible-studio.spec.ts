import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import VisualBibleStudio from '../src/components/VisualBibleStudio.vue'

const api = vi.hoisted(() => ({
  getVisualBible: vi.fn(), getVisualBibleCatalog: vi.fn(), getVisualBibleIndexHealth: vi.fn(),
  draftVisualBible: vi.fn(), saveVisualBible: vi.fn(), confirmVisualBible: vi.fn(),
}))
vi.mock('../src/api/jobs', () => api)

const bible = {
  schema_version: 'project-visual-bible.v2', status: 'draft', mode: 'observed',
  version: 1, ontology_version: 'style-ontology.v2', index_manifest: {},
  global_core_terms: ['medium.paper-cut'], global_secondary_terms: [],
  scene_overrides: {}, shot_overrides: {}, local_effects: {}, forbidden_drift: [],
  recommended_recipes: [], selected_recipe_id: null, coverage_profiles: {},
  unresolved_features: [], rendered_global_prompt: '整体画面风格：多层手工剪纸。',
  model_provenance: {}, bible_hash: 'hash',
}
const catalog = {
  version: 'style-ontology.v2', ontology_hash: 'hash',
  categories: ['medium_rendering', 'color_tone'],
  terms: [
    { id: 'medium.paper-cut', category: 'medium_rendering', name_cn: '剪纸拼贴', definition: '可见纸层', generation_phrase: '多层剪纸拼贴' },
    { id: 'color.desaturated-cool', category: 'color_tone', name_cn: '低饱和青灰色调', definition: '青灰低彩度', generation_phrase: '低饱和青灰色调' },
  ],
  recipes: [{ id: 'recipe.paper-cut-uncanny-geometry', name: '剪纸贴图与诡异几何风',
    suitable_for: ['梦境'], global_prompt: '多层手工剪纸拼贴动画。' }],
}

describe('VisualBibleStudio', () => {
  it('shows automatic evidence application and refreshes canvas prompts after redraft', async () => {
    vi.clearAllMocks()
    api.getVisualBible.mockResolvedValue({ bible: structuredClone(bible), retrieval: {} })
    api.getVisualBibleCatalog.mockResolvedValue(catalog)
    api.getVisualBibleIndexHealth.mockResolvedValue({ ready: true, reason: '', manifest: {} })
    api.draftVisualBible.mockResolvedValue({ bible: { ...structuredClone(bible), status: 'confirmed',
      model_provenance: { application_mode: 'auto-evidence' } }, retrieval: {} })
    const wrapper = mount(VisualBibleStudio, { props: { jobId: 'job-auto', inputType: 'video', shotIds: ['shot-1'], sceneIds: [] } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('重新分析'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('系统已识别并应用')
    expect(wrapper.emitted('applied')).toEqual([['job-auto']])
    expect(api.confirmVisualBible).not.toHaveBeenCalled()
    wrapper.unmount()
    vi.clearAllMocks()
  })
  it('distinguishes sparse observed facts from unobserved creative recipe traits', async () => {
    api.getVisualBible.mockResolvedValue({ bible: { ...structuredClone(bible),
      recipe_recommendations: [{ recipe_id: 'recipe.paper-cut-uncanny-geometry', status: 'partial',
        supported_term_ids: ['medium.paper-cut'], missing_term_ids: ['color.desaturated-cool'],
        ranks: { text: 1, image: 2 }, evidence: [{ frame_id: 'frame-1', timestamp: 1.2, description: '可见纸层' }],
        reason: '纸层可见，配色待确认', optional_anchors: ['创作参考，不代表原片来源'] }],
    }, retrieval: { matches: [] } })
    api.getVisualBibleCatalog.mockResolvedValue(catalog)
    api.getVisualBibleIndexHealth.mockResolvedValue({ ready: true, reason: '', manifest: {} })
    const wrapper = mount(VisualBibleStudio, { props: { jobId: 'job-3', inputType: 'video', shotIds: ['shot-1'], sceneIds: [] } })
    await flushPromises()
    expect(wrapper.text()).toContain('已识别部分特征 · 可调整应用')
    expect(wrapper.text()).toContain('不是原片来源识别')
    expect(wrapper.text()).toContain('未观察/需创作确认：低饱和青灰色调')
    expect(api.draftVisualBible).not.toHaveBeenCalled()
    expect(api.confirmVisualBible).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('keeps V2 draft out of prompts until explicit confirmation, and scopes local effects', async () => {
    api.getVisualBible.mockResolvedValue({ bible: structuredClone(bible), retrieval: {
      matches: [{ term_id: 'medium.paper-cut', category: 'medium_rendering', scope: 'GLOBAL',
        shot_ids: ['shot-1'], evidence: [{ frame_id: 'frame-1', timestamp: 1.2, description: '纸层' }],
        ranks: { text: 2, image: 1 }, confidence: .91, reason: '双通道一致' }],
    } })
    api.getVisualBibleCatalog.mockResolvedValue(catalog)
    api.getVisualBibleIndexHealth.mockResolvedValue({ ready: true, reason: '', manifest: { version: 'style-vector.v2' } })
    api.saveVisualBible.mockImplementation(async (_id, value) => ({ ...value, version: 2, rendered_global_prompt: '整体画面风格：已保存。' }))
    api.confirmVisualBible.mockResolvedValue({ ...bible, status: 'confirmed', version: 2 })
    const wrapper = mount(VisualBibleStudio, { props: { jobId: 'job-1', inputType: 'video',
      shotIds: ['shot-1', 'shot-2'], sceneIds: ['scene-1'], selectedShotId: 'shot-1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('整体画面风格：多层手工剪纸')
    expect(wrapper.text()).toContain('文本排名 2 / 图像排名 1')
    expect(wrapper.text()).toContain('frame-1')
    expect(api.confirmVisualBible).not.toHaveBeenCalled()
    await wrapper.find('input[placeholder="仅本镜可见的局部效果"]').setValue('一张纸影向左横移')
    await wrapper.findAll('button').find(node => node.text() === '追加')!.trigger('click')
    await wrapper.findAll('button').find(node => node.text().includes('保存草案'))!.trigger('click')
    await flushPromises()
    expect(api.saveVisualBible.mock.calls[0][1].local_effects).toEqual({ 'shot-1': ['一张纸影向左横移'] })
    expect(api.confirmVisualBible).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(node => node.text().includes('确认并应用'))!.trigger('click')
    await flushPromises()
    expect(api.confirmVisualBible).toHaveBeenCalledWith('job-1')
    expect(wrapper.emitted('applied')?.[0]).toEqual(['job-1'])
    wrapper.unmount()
  })

  it('allows manual Bible creation when index is missing', async () => {
    api.getVisualBible.mockResolvedValue({ bible: null, retrieval: null })
    api.getVisualBibleCatalog.mockResolvedValue(catalog)
    api.getVisualBibleIndexHealth.mockResolvedValue({ ready: false, reason: '请运行构建命令', manifest: null })
    const wrapper = mount(VisualBibleStudio, { props: { jobId: 'job-2', inputType: 'script', shotIds: [], sceneIds: [] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('请运行构建命令')
    await wrapper.findAll('button').find(node => node.text() === '人工创建草案')!.trigger('click')
    expect(wrapper.text()).toContain('六类全局核心特征')
    wrapper.unmount()
  })
})
