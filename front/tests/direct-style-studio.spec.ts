import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DirectStyleStudio from '../src/components/DirectStyleStudio.vue'
import { canvasShortcut } from '../src/canvasKeyboard'
import type { StyleAnalysis, StyleRecommendation } from '../src/api/styleDirect'

const api = vi.hoisted(() => ({ analyzeProjectStyle: vi.fn(), analyzeStyleImage: vi.fn(), applyDirectStyle: vi.fn(), editDirectStyle: vi.fn(), getProjectDirectStyle: vi.fn(), getStyleAnalysis: vi.fn(), styleEventsUrl: vi.fn(() => '/events'), styleImageUrl: vi.fn((id, alias) => `/images/${id}/${alias}`), styleDiagnosticsUrl: vi.fn(id => `/diagnostics/${id}?format=markdown`) }))
vi.mock('../src/api/styleDirect', () => api)
const completed = () => ({ analysis_id: 'style-a', status: 'completed', stage: 'completed', error: null, created_at: new Date().toISOString(), result: { profile: { schema_version: 'style-profile.v3', descriptive_name: '圆润柔光三维视觉', reference_scope: 'reference', rendered_prompt: '整体画面风格：圆润曲线主导，阴影宽缓过渡，高光受控。', detailed_prompt: '细腻平滑曲面，五官结构简化，保留表情可读性。', scene_prompts: {}, uncertainties: ['具体工作室无法确认，已保留可见视觉特征'], version: 1, profile_hash: 'hash', application_status: 'ready' }, assets: [{ alias: 'I1', asset_id: 'a', width: 100, height: 200 }], evidence: { claims: [{ id: 'C1', dimension: 'form_language', observation: '圆弧轮廓与简化五官', images: ['I1'], confidence: .85 }] }, budget: { model_requests: 2, crop_count: 0, elapsed_seconds: 12 } } })
const recipe = (index: number): StyleRecommendation => ({ recipe_id: `recipe-${index}`, label: `库内方向 ${index}`, fit_level: index === 1 ? 'high' : 'medium', reason: '轮廓和光影有观察支持', controls: [{ claim_id: 'C1', quote: '圆弧轮廓' }], overlap: ['圆润造型'], differences: ['更强调纸张肌理'], library_prompt: `完整配方 ${index}：主体采用圆润造型。\n保留完整的光影、材质、色彩和表面控制，不只是候选名称。`, detailed_description: '可编辑的专业画风说明', library_source_hash: `source-${index}` })
const withRecipes = (count: number): StyleAnalysis => ({ ...completed(), recommendations: Array.from({ length: count }, (_, index) => recipe(index + 1)) } as StyleAnalysis)
class Stream {
  static instances: Stream[] = []
  listeners: Record<string, () => void> = {}
  close = vi.fn()
  onerror: (() => void) | null = null
  constructor(_url: string) { Stream.instances.push(this) }
  addEventListener(type: string, callback: () => void) { this.listeners[type] = callback }
}
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); Stream.instances = []; vi.stubGlobal('EventSource', Stream); api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: null }) })
afterEach(() => { vi.unstubAllGlobals() })

describe('direct style workflow', () => {
  it.each([0, 1, 5])('renders at most three dedicated recommendations with %s available', async (count) => {
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: withRecipes(count) })
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1' } }); await flushPromises()
    expect(w.findAll('[data-testid="style-recipe-card"]')).toHaveLength(Math.min(count, 3))
    expect(w.find('[data-testid="style-no-recommendations"]').exists()).toBe(count === 0)
    expect(w.get('[data-testid="style-observations"]').text()).toContain('圆弧轮廓与简化五官')
    expect(w.get('[data-testid="style-recommendations"]').text()).not.toContain('90%')
    w.unmount()
  })
  it('fills the complete recipe draft without backend writes, then saves source and exact version', async () => {
    const data = withRecipes(1); data.recommendations_derived = true
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: data })
    const saved = withRecipes(1); saved.result.profile!.version = 2; saved.result.profile!.rendered_prompt = recipe(1).library_prompt
    api.editDirectStyle.mockResolvedValue(saved)
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1' } }); await flushPromises()
    expect(w.get('[data-testid="style-derived-recommendations"]').text()).toContain('历史观察只读整理')
    await w.get('[data-testid="style-choose-recipe"]').trigger('click')
    expect(w.get('textarea').element.value).toBe(recipe(1).library_prompt)
    expect(w.get('[data-testid="style-library-prompt"]').text()).toBe(recipe(1).library_prompt)
    expect(api.editDirectStyle).not.toHaveBeenCalled(); expect(api.applyDirectStyle).not.toHaveBeenCalled()
    await w.findAll('button').find(button => button.text() === '保存修改')!.trigger('click'); await flushPromises()
    expect(api.editDirectStyle).toHaveBeenCalledWith('style-a', recipe(1).library_prompt, 1, { kind: 'library-recipe', recipe_id: 'recipe-1', source_hash: 'source-1' })
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    w.unmount()
  })
  it('applies a selected recipe only to the current target after saving and retains failed apply state', async () => {
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: withRecipes(1) })
    const saved = withRecipes(1); saved.result.profile!.version = 2; saved.result.profile!.rendered_prompt = recipe(1).library_prompt
    api.editDirectStyle.mockResolvedValue(saved); api.applyDirectStyle.mockRejectedValue(new Error('目标项目更新失败'))
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1', targets: [{ id: 'j1', label: '项目一' }, { id: 'j2', label: '项目二' }] } }); await flushPromises()
    await w.get('[data-testid="style-choose-recipe"]').trigger('click'); await w.get('select').setValue('j2')
    await w.findAll('button').find(button => button.text().includes('更新首帧'))!.trigger('click'); await flushPromises()
    expect(api.editDirectStyle).toHaveBeenCalledWith('style-a', recipe(1).library_prompt, 1, expect.objectContaining({ recipe_id: 'recipe-1' }))
    expect(api.applyDirectStyle).toHaveBeenCalledWith('j2', 'style-a', 2)
    expect(w.get('textarea').element.value).toBe(recipe(1).library_prompt)
    expect(w.text()).toContain('目标项目更新失败'); expect(w.emitted('applied')).toBeUndefined()
    w.unmount()
  })
  it('keeps a manually customized recipe draft as a manual edit', async () => {
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: withRecipes(1) }); api.editDirectStyle.mockResolvedValue(withRecipes(1))
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1' } }); await flushPromises()
    await w.get('[data-testid="style-choose-recipe"]').trigger('click'); await w.get('textarea').setValue('手工修改后的创作方向')
    await w.findAll('button').find(button => button.text() === '保存修改')!.trigger('click'); await flushPromises()
    expect(api.editDirectStyle).toHaveBeenCalledWith('style-a', '手工修改后的创作方向', 1)
    w.unmount()
  })
  it('preserves a manual draft during same-analysis refresh and discards it on a new task', async () => {
    const running = withRecipes(1); running.status = 'running'; running.stage = 'rendering'
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: running }); api.getStyleAnalysis.mockResolvedValue(withRecipes(1))
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1' } }); await flushPromises()
    Stream.instances[0].listeners['style.direct.finished'](); await flushPromises()
    await w.get('textarea').setValue('手工草稿')
    Stream.instances[0].listeners['style.direct.finished'](); await flushPromises()
    expect(w.get('textarea').element.value).toBe('手工草稿')
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: { ...withRecipes(0), analysis_id: 'style-b' } })
    await w.setProps({ jobId: 'j2' }); await flushPromises()
    expect(w.get('textarea').element.value).toBe(completed().result.profile.rendered_prompt)
    expect(w.find('[data-testid="style-recipe-draft"]').exists()).toBe(false)
    w.unmount()
  })
  it('identifies failed style analysis without presenting a substitute or buying another call', async () => {
    const data = completed(); data.status = 'failed'; data.stage = 'failed'
    data.result.profile.application_status = 'partial'
    data.result.profile.rendered_prompt = '画风未能确定，未添加未经证实的特征。'
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: data })
    const w = mount(DirectStyleStudio, { props: { jobId: 'failed' } })
    await flushPromises()
    expect(w.get('[data-testid="style-incomplete"]').text()).toContain('画风分析未完成')
    expect(w.get('[data-testid="style-incomplete"]').text()).toContain('不会用其他风格替代')
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    expect(Stream.instances).toHaveLength(0)
    w.unmount()
  })
  it('shows a reviewed generation reference separately from studio attribution', async () => {
    const data = { ...completed(), result: { ...completed().result,
      quality_state: { structure: 'valid', observation: 'available', interpretation: 'completed',
        style: 'resolved', anchor: 'unknown', reference: 'supported' },
      profile: { ...completed().result.profile, reference: { id: 'pixar-character-3d', confidence: .93,
        reason: '体量梯级与分区表面组织支持这个生成视觉方向，不认定制作来源。' } } } }
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: data })
    const w = mount(DirectStyleStudio, { props: { jobId: 'reference' } })
    await flushPromises()
    expect(w.get('[data-testid="style-quality-state"]').text()).toContain('具体视觉参考：有证据支持')
    expect(w.get('[data-testid="style-reference"]').text()).toContain('非出品方认定')
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    w.unmount()
  })
  it('separates usable family features from unknown studio anchor and local effects', async () => {
    const data = { ...completed(), result: { ...completed().result,
      quality_state: { structure: 'valid', observation: 'available', interpretation: 'completed',
        style: 'resolved', anchor: 'unknown', descriptor: 'usable', family: 'supported' },
      profile: { ...completed().result.profile, local_prompts: { I1: '本镜局部效果：湿润反光。' } } } }
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: data })
    const w = mount(DirectStyleStudio, { props: { jobId: 'density' } })
    await flushPromises()
    expect(w.get('[data-testid="style-quality-state"]').text()).toContain('视觉特征已确定')
    expect(w.get('[data-testid="style-quality-state"]').text()).toContain('具体风格锚点：未知')
    expect(w.get('[data-testid="style-family-state"]').text()).toContain('风格家族：有证据支持')
    expect(w.get('[data-testid="style-local-effect"]').text()).toContain('不继承到全局画风')
    expect(w.get('textarea').element.value).not.toContain('湿润')
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    w.unmount()
  })
  it('separates preserved observations from partial professional interpretation', async () => {
    const data = { ...completed(), status: 'partial', stage: 'partial', result: { ...completed().result,
      quality_state: { structure: 'partial', observation: 'available', interpretation: 'partial', style: 'partial', anchor: 'unknown' } } }
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: data })
    const w = mount(DirectStyleStudio, { props: { jobId: 'partial' } })
    await flushPromises()
    expect(w.get('[data-testid="style-quality-state"]').text()).toContain('观察已保留')
    expect(w.get('[data-testid="style-quality-state"]').text()).toContain('专业转译：部分确定')
    expect(w.get('[data-testid="style-quality-state"]').text()).toContain('具体风格锚点：未知')
    expect(w.get('textarea').element.value).toContain('圆润曲线')
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    w.unmount()
  })
  it('shows legacy diagnostics as unrecorded and never re-analyzes them', async () => {
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: completed() })
    const w = mount(DirectStyleStudio, { props: { jobId: 'legacy' } })
    await flushPromises()
    expect(w.get('[data-testid="style-diagnostics"]').text()).toContain('历史分析未记录诊断')
    expect(w.get('[data-testid="style-diagnostics"] a').attributes('href')).toContain('format=markdown')
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    w.unmount()
  })

  it('shows timings, candidate rejection, and missing cost without claiming accuracy', async () => {
    const data = { ...completed(), result: { ...completed().result, diagnostics: {
      schema_version: 'style-diagnostics.v1', status: 'available',
      timing: { total_seconds: 12.5, model_seconds: 10, retrieval_seconds: .1, validation_seconds: .2, render_seconds: .01 },
      calls: { model_requests: 2, max_model_requests: 4, correction_used: false, supplement_rounds: 0 },
      stages: [{ name: 'retrieving', label: '候选召回', elapsed_seconds: .1, model_requests: 0, status: 'completed' }],
      candidate_snapshot: [{ id: 'candidate-a', label: '未成立候选', selection_eligible: false, matched_group_ids: ['medium'], missing_group_ids: ['form'], retrieval_basis: 'partial-signature-review' }],
      decisions: [{ field: 'descriptions', reason: '冷暖控制点矛盾已恢复观察' }],
      quality: { note: '结构校验不代表真实语义正确', human_accuracy: null },
      usage: { complete: false, prompt_tokens: null, completion_tokens: null, estimated_list_price_cny: null, known_cost_subtotal_cny: .01 },
    } } }
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: data })
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1' } })
    await flushPromises()
    const diagnostics = w.get('[data-testid="style-diagnostics"]').text()
    expect(diagnostics).toContain('12.50 秒')
    expect(diagnostics).toContain('文字组合未齐 · 可进行语义比较')
    expect(diagnostics).toContain('造型')
    expect(diagnostics).toContain('冷暖控制点矛盾已恢复观察')
    expect(diagnostics).toContain('其余用量未知')
    expect(diagnostics).toContain('结构校验不代表真实语义正确')
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    w.unmount()
  })
  it('does not call a paid model on open or require choosing a style', async () => {
    const w = mount(DirectStyleStudio, { props: { imageBlob: new Blob(['image']), imageAssetId: 'asset' } })
    await flushPromises()
    expect(api.analyzeStyleImage).not.toHaveBeenCalled()
    expect(w.text()).toContain('不会要求你挑选专业风格')
    expect(w.text()).toContain('API 费用')
    w.unmount()
  })

  it('analyzes explicitly and keeps an uncertain name separate from known traits', async () => {
    api.analyzeStyleImage.mockResolvedValue(completed())
    const blob = new Blob(['image'])
    const w = mount(DirectStyleStudio, { props: { imageBlob: blob, imageAssetId: 'asset' } })
    await w.findAll('button').find(b => b.text() === '分析画风')!.trigger('click')
    await flushPromises()
    expect(api.analyzeStyleImage).toHaveBeenCalledWith(blob, false)
    expect(w.get('textarea').element.value).toContain('阴影宽缓过渡')
    expect(w.text()).toContain('具体工作室无法确认')
    expect(w.find('img').attributes('src')).toBe('/images/style-a/I1')
    expect(localStorage.getItem('frameforge-style-analysis:asset')).toBe('style-a')
    expect(Stream.instances).toHaveLength(0)
    w.unmount()
  })

  it('restores the saved analysis without another cloud call', async () => {
    localStorage.setItem('frameforge-style-analysis:asset', 'style-a')
    api.getStyleAnalysis.mockResolvedValue(completed())
    const w = mount(DirectStyleStudio, { props: { imageBlob: new Blob(['image']), imageAssetId: 'asset' } })
    await flushPromises()
    expect(api.getStyleAnalysis).toHaveBeenCalledWith('style-a')
    expect(api.analyzeStyleImage).not.toHaveBeenCalled()
    expect(w.get('textarea').element.value).toContain('圆润曲线')
    w.unmount()
  })

  it('follows one isolated stream and closes it on completion', async () => {
    const data = completed(); data.status = 'running'; data.stage = 'observing'; data.result = {} as typeof data.result
    api.analyzeStyleImage.mockResolvedValue(data)
    api.getStyleAnalysis.mockResolvedValue(completed())
    const w = mount(DirectStyleStudio, { props: { imageBlob: new Blob(['image']) } })
    await w.findAll('button').find(b => b.text() === '分析画风')!.trigger('click'); await flushPromises()
    expect(w.text()).toContain('提取视觉证据')
    expect(Stream.instances).toHaveLength(1)
    Stream.instances[0].listeners['style.direct.finished']()
    await flushPromises()
    expect(Stream.instances[0].close).toHaveBeenCalled()
    expect(w.text()).toContain('画风已生成')
    w.unmount()
  })

  it('saves an edit and applies that exact version without reanalysis', async () => {
    api.getProjectDirectStyle.mockResolvedValue({ state: {}, analysis: completed() })
    const saved = completed(); saved.result.profile.version = 2; saved.result.profile.rendered_prompt = '整体画面风格：手绘曲线与限定色块。'; saved.result.profile.application_status = 'user-edited'
    api.editDirectStyle.mockResolvedValue(saved); api.applyDirectStyle.mockResolvedValue({})
    const w = mount(DirectStyleStudio, { props: { jobId: 'j1', targets: [{ id: 'j1', label: '样片' }] } })
    await flushPromises(); await w.get('textarea').setValue(saved.result.profile.rendered_prompt)
    await w.findAll('button').find(b => b.text().includes('更新首帧'))!.trigger('click'); await flushPromises()
    expect(api.editDirectStyle).toHaveBeenCalledWith('style-a', saved.result.profile.rendered_prompt, 1)
    expect(api.applyDirectStyle).toHaveBeenCalledWith('j1', 'style-a', 2)
    expect(api.analyzeProjectStyle).not.toHaveBeenCalled()
    expect(w.emitted('applied')?.[0]).toEqual(['j1'])
    w.unmount()
  })

  it('does not attach an SSE after an unmounted pending upload', async () => {
    let resolve: (value: ReturnType<typeof completed>) => void = () => {}
    api.analyzeStyleImage.mockReturnValue(new Promise(r => { resolve = r }))
    const w = mount(DirectStyleStudio, { props: { imageBlob: new Blob(['image']) } })
    await w.findAll('button').find(b => b.text() === '分析画风')!.trigger('click')
    w.unmount()
    const data = completed(); data.status = 'running'; data.stage = 'observing'; resolve(data)
    await flushPromises()
    expect(Stream.instances).toHaveLength(0)
  })

  it('does not let modal keyboard actions delete canvas nodes', async () => {
    const w = mount(DirectStyleStudio, { props: {} })
    const event = new KeyboardEvent('keydown', { key: 'Delete' })
    Object.defineProperty(event, 'target', { value: w.get('[role="dialog"]').element })
    expect(canvasShortcut(event, true)).toBeNull()
    await w.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })
})
