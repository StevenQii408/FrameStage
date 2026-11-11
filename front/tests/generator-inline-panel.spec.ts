import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GeneratorInlinePanel from '../src/components/GeneratorInlinePanel.vue'
import type { GeneratorDraft, GeneratorReference, ResolvedGenerator } from '../src/generatorGraph'
import { defaultGenerationSettings, type GenerationJob } from '../src/api/generations'
import { readPromptEditorText } from '../src/promptReferenceDom'
import { setPromptText } from './promptEditorTestHelpers'

const generator: GeneratorDraft = { id: 'gen-panel', kind: 'video', position: { x: 0, y: 0 }, manualPrompt: '女子走进画面', imageAssetId: null, imageWidth: 0, imageHeight: 0 }
const references: GeneratorReference[] = [1, 2].map(number => ({ id: 'ref-00000000000' + number, mention: '图片' + number,
  url: '/image-' + number + '.jpg', width: 160, height: 90, identity: 'asset-' + number, source: '参考图片', selected: true, firstFrame: number === 1 }))
const resolved: ResolvedGenerator = { prompt: '女子走进画面', promptSource: '手动输入', imageUrl: '/image-1.jpg', imageSource: '参考图片',
  imageWidth: 160, imageHeight: 90, ownImageUrl: '', ownImageWidth: 0, ownImageHeight: 0, references }
const options = { attachTo: document.body, props: { generator, resolved, anchor: { x: 120, y: 240, width: 720 } }, global: { stubs: { Teleport: true } } }

describe('inline generator controls', () => {
  it('shows a neutral status in the composer after a cloud cancellation', () => {
    const task = { status: 'cancelled', error: { code: 'provider_cancelled', message: 'raw-upstream', retryable: true } } as GenerationJob
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props, task } })
    const notice = wrapper.get('.inline-feedback')
    expect(notice.text()).toBe('云端已取消')
    expect(notice.attributes('role')).toBe('status')
    wrapper.unmount()
  })

  it('never opens generation controls for a manual image reference asset', () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props, generator: { ...generator, kind: 'image', nodeMode: 'reference', imageAssetId: 'local-image' } } })
    expect(wrapper.find('.generator-inline-panel').exists()).toBe(false)
    expect(wrapper.find('.generator-prompt-modal').exists()).toBe(false)
    expect(wrapper.emitted('submitGeneration')).toBeUndefined()
    wrapper.unmount()
  })

  it('retains a successful node’s Prompt and parameters for editing a new generation', async () => {
    const settings = { ...defaultGenerationSettings('video'), duration_seconds: 8, aspect_ratio: '9:16' as const }
    const item: GeneratorDraft = { ...generator, nodeMode: 'generator', manualPrompt: '之前生成的完整 Prompt', generationSettings: settings,
      versions: [{ id: 'version-existing', jobId: 'generation-existing', createdAt: '2026-11-10T00:00:00Z', prompt: '原始生成 Prompt', settings,
        asset: { id: 'existing-asset', url: '/actual-existing.mp4', mime_type: 'video/mp4', width: 1080, height: 1920 } }] }
    const task = { id: 'generation-existing', status: 'succeeded', assets: [item.versions![0].asset] } as GenerationJob
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props, generator: item, resolved: { ...resolved, prompt: item.manualPrompt }, task } })
    expect(readPromptEditorText(wrapper.get('.inline-prompt').element)).toBe('之前生成的完整 Prompt')
    expect(wrapper.get('[aria-label="生成规格"]').text()).toBe('9:16 · 1080p · 8s')
    await setPromptText(wrapper.get('.inline-prompt'), '修改后重新生成的 Prompt')
    expect(wrapper.emitted('promptChange')?.at(-1)).toEqual(['修改后重新生成的 Prompt'])
    await wrapper.setProps({ stale: true, resolved: { ...resolved, prompt: '修改后重新生成的 Prompt' } })
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeUndefined()
    await wrapper.get('.composer-submit').trigger('click')
    expect(wrapper.emitted('submitGeneration')).toHaveLength(1)
    expect(item.versions![0].prompt).toBe('原始生成 Prompt')
    expect(item.generationSettings).toEqual(settings)
    wrapper.unmount()
  })

  it('places selected reference thumbnails above editable Prompt and exposes declared model capabilities only', async () => {
    const wrapper = mount(GeneratorInlinePanel, options)
    const images = wrapper.findAll('.inline-reference img')
    expect(images).toHaveLength(2)
    expect(images[0].element.compareDocumentPosition(wrapper.get('.inline-prompt').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(wrapper.get('select[aria-label="生成模型"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.generator-inline-panel').attributes('style')).toContain('left: 120px')
    expect(wrapper.get('.generator-inline-panel').attributes('style')).toContain('width: 720px')
    expect(wrapper.find('[aria-label="生成画幅"]').exists()).toBe(false)
    expect(wrapper.get('[aria-label="生成规格"]').text()).toBe('16:9 · 1080p · 5s')
    expect(wrapper.find('.inline-prompt-heading').exists()).toBe(false)
    expect(wrapper.find('.inline-source').exists()).toBe(false)
    await wrapper.setProps({ capabilities: { configured: false, provider: 'bailian-generation', reason: '未启用',
      video: { available: false, default_model_id: 'wan3.0-video', models: [{ id: 'wan3.0-video', name: '万相 Wan 3.0' }],
        aspect_ratios: ['16:9', '9:16'], resolutions: ['480p', '720p', '1080p'] } } })
    expect(wrapper.emitted('settingsChange')).toBeUndefined()
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    await wrapper.get('select[aria-label="生成画幅"]').setValue('9:16')
    expect(wrapper.emitted('settingsChange')?.[0][0]).toMatchObject({ aspect_ratio: '9:16' })
    expect(wrapper.get('.composer-submit').attributes('title')).toContain('未调用生成 API')
    wrapper.unmount()
  })
  it('offers thumbnail suggestions on @ and selects a reference with arrow keys/Enter', async () => {
    const wrapper = mount(GeneratorInlinePanel, options)
    const input = wrapper.get('.inline-prompt')
    await setPromptText(input, '女子拿起 @')
    await wrapper.setProps({ resolved: { ...resolved, prompt: '女子拿起 @' } })
    expect(wrapper.get('[role="listbox"]').findAll('img')).toHaveLength(2)
    await wrapper.get('.inline-prompt').trigger('keydown', { key: 'ArrowDown' })
    await wrapper.get('.inline-prompt').trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('promptChange')?.at(-1)).toEqual(['女子拿起 @图片2 '])
    expect(wrapper.emitted('referenceChange')?.at(-1)).toEqual([{ id: references[1].id, selected: true }])
    expect(wrapper.find('[role="listbox"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('shows the actual backend default model and all Wan resolutions without rewriting the draft', async () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props,
      capabilities: { configured: false, provider: 'bailian-generation', reason: '未启用',
        request_limits: { video: { min_duration_seconds: 2, max_duration_seconds: 30, duration_step_seconds: 1 } },
        video: { available: false, default_model_id: 'wan3.0-video', models: [{ id: 'wan3.0-video', name: '万相 Wan 3.0' }],
          aspect_ratios: ['21:9', '16:9', '9:16'], resolutions: ['480p', '720p', '1080p'], min_duration_seconds: 2, max_duration_seconds: 30 } } } })
    const model = wrapper.get('[aria-label="生成模型"]')
    expect((model.element as HTMLSelectElement).value).toBe('wan3.0-video')
    expect(model.text()).toContain('万相 Wan 3.0')
    expect(model.text()).not.toContain('默认')
    expect(wrapper.emitted('settingsChange')).toBeUndefined()
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    expect(wrapper.get('[aria-label="生成分辨率"]').findAll('option').map(option => option.text())).toEqual(['480p', '720p', '1080p'])
    expect(wrapper.get('[aria-label="生成时长"]').attributes()).toMatchObject({ min: '2', max: '30', step: '1' })
    expect(wrapper.get('[aria-label="生成画幅"]').findAll('option').map(option => option.text())).toContain('21:9')
    await wrapper.get('[aria-label="生成分辨率"]').setValue('480p')
    expect(wrapper.emitted('settingsChange')?.at(-1)?.[0]).toMatchObject({ resolution: '480p' })
    wrapper.unmount()
  })
  it('displays image pixel sizes from the backend and removes sizes unavailable for the chosen ratio', async () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props,
      generator: { ...generator, kind: 'image', generationSettings: { ...defaultGenerationSettings('image'), aspect_ratio: '1:1', resolution: '2048p' } },
      capabilities: { configured: false, provider: 'bailian-generation', reason: '未启用',
        image: { available: false, default_model_id: 'qwen-image-3.0-pro', models: [{ id: 'qwen-image-3.0-pro', name: '千问 Image 3.0 Pro' }],
          aspect_ratios: ['1:1', '16:9'], resolutions: ['1080p', '2048p'], resolution_options: [
            { value: '1080p', aspect_ratio: '1:1', width: 1080, height: 1080, label: '1080 × 1080' },
            { value: '2048p', aspect_ratio: '1:1', width: 2048, height: 2048, label: '2048 × 2048' },
            { value: '1080p', aspect_ratio: '16:9', width: 1920, height: 1080, label: '1920 × 1080' },
          ] } } } })
    expect(wrapper.get('[aria-label="生成规格"]').text()).toBe('1:1 · 2048 × 2048')
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    expect(wrapper.get('[aria-label="生成分辨率"]').findAll('option').map(option => option.text())).toEqual(['1080 × 1080', '2048 × 2048'])
    await wrapper.setProps({ generator: { ...generator, kind: 'image', generationSettings: { ...defaultGenerationSettings('image'), resolution: '2048p' } } })
    expect(wrapper.get('[role="alert"]').text()).toContain('分辨率')
    expect(wrapper.get('[aria-label="生成分辨率"]').findAll('option').filter(option => option.attributes('disabled') === undefined).map(option => option.text())).toEqual(['1920 × 1080'])
    expect(wrapper.emitted('settingsChange')).toBeUndefined()
    wrapper.unmount()
  })
  it('shares full Prompt edits and mention selection with the compact editor', async () => {
    const wrapper = mount(GeneratorInlinePanel, options)
    await wrapper.get('[aria-label="展开完整 Prompt"]').trigger('click')
    const full = wrapper.get('[data-prompt-editor][aria-label="完整 Prompt"]')
    await setPromptText(full, '以 @图')
    await wrapper.setProps({ resolved: { ...resolved, prompt: '以 @图' } })
    expect(wrapper.get('#modal-reference-suggestions').findAll('[role="option"]')).toHaveLength(2)
    await wrapper.get('[data-prompt-editor][aria-label="完整 Prompt"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('#modal-reference-suggestions').exists()).toBe(false)
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    await setPromptText(wrapper.get('[data-prompt-editor][aria-label="完整 Prompt"]'), '以 @')
    await wrapper.setProps({ resolved: { ...resolved, prompt: '以 @' } })
    await wrapper.get('[data-prompt-editor][aria-label="完整 Prompt"]').trigger('keydown', { key: 'Enter' })
    const updated = wrapper.emitted('promptChange')!.at(-1)![0] as string
    await wrapper.setProps({ resolved: { ...resolved, prompt: updated } })
    expect(readPromptEditorText(wrapper.get('.inline-prompt').element)).toBe('以 @图片1 ')
    await wrapper.get('[aria-label="收起完整 Prompt"]').trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('reports disconnected mentions without assigning the token to another picture', () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props,
      resolved: { ...resolved, prompt: '使用 @图片1', references: [references[1]] } } })
    expect(wrapper.get('[role="alert"]').text()).toContain('@图片1')
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('reads models from the current kind and blocks incompatible model parameters', async () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props,
      generator: { ...generator, generationSettings: { aspect_ratio: '16:9', resolution: '1080p', duration_seconds: 5, model_id: 'video-model' } },
      capabilities: { configured: true, provider: 'test', reason: '',
        image: { available: true, models: ['image-only'] },
        video: { available: true, models: [{ id: 'video-model', label: '视频模型', aspect_ratios: ['9:16'], resolutions: ['720p'], min_duration_seconds: 6, max_duration_seconds: 10 }],
          aspect_ratios: ['16:9', '9:16'], resolutions: ['720p', '1080p'] } } } })
    const names = wrapper.get('[aria-label="生成模型"]').findAll('option').map(option => option.text())
    expect(names).toContain('视频模型')
    expect(names).not.toContain('image-only')
    expect(wrapper.get('[role="alert"]').attributes('title')).toContain('所选画幅未被当前模型声明支持')
    expect(wrapper.get('[role="alert"]').attributes('title')).toContain('6–10 秒的整数')
    expect(wrapper.findAll('[role="alert"]')).toHaveLength(1)
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ generator: { ...generator, generationSettings: { aspect_ratio: '9:16', resolution: '720p', duration_seconds: 7, model_id: 'video-model' } } })
    expect(wrapper.findAll('[role="alert"]')).toHaveLength(0)
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
  it('keeps reference numbers, first-frame selection and canvas selection without opening an inspector', async () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props, resolved: { ...resolved,
      references: [{ ...references[0], sourceNodeId: 'source-a' }, { ...references[1], sourceNodeId: 'source-b' }] } } })
    expect(wrapper.findAll('.inline-reference-number').map(item => item.text())).toEqual(['1', '2'])
    await wrapper.get('[aria-label="图片2设为首帧"]').trigger('click')
    expect(wrapper.emitted('referenceChange')?.at(-1)).toEqual([{ id: references[1].id, firstFrame: true }])
    await wrapper.get('[aria-label="选择画布参考"]').trigger('click')
    expect(wrapper.get('[aria-label="画布参考图片"]').findAll('img')).toHaveLength(2)
    await wrapper.get('[aria-label="选择画布图片1"]').setValue(false)
    expect(wrapper.emitted('referenceChange')?.at(-1)).toEqual([{ id: references[0].id, selected: false }])
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    expect(wrapper.find('[aria-label="画布参考图片"]').exists()).toBe(false)
    await wrapper.get('[aria-label="生成规格"][role="group"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('.inline-generation-settings').exists()).toBe(false)
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })
  it.each(['inline', 'modal'] as const)('does not intercept IME confirmation in the %s Prompt editor', async mode => {
    const wrapper = mount(GeneratorInlinePanel, options)
    if (mode === 'modal') await wrapper.get('[aria-label="展开完整 Prompt"]').trigger('click')
    const selector = mode === 'modal' ? '[data-prompt-editor][aria-label="完整 Prompt"]' : '.inline-prompt'
    await setPromptText(wrapper.get(selector), '使用 @')
    await wrapper.setProps({ resolved: { ...resolved, prompt: '使用 @' } })
    await wrapper.get(selector).trigger('keydown', { key: 'Enter', isComposing: true })
    await wrapper.get(selector).trigger('keydown', { key: 'Enter', keyCode: 229 })
    expect(wrapper.emitted('referenceChange')).toBeUndefined()
    await wrapper.get(selector).trigger('compositionstart')
    await wrapper.get(selector).trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('referenceChange')).toBeUndefined()
    await wrapper.get(selector).trigger('compositionend')
    await wrapper.get(selector).trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('referenceChange')?.at(-1)).toEqual([{ id: references[0].id, selected: true }])
    wrapper.unmount()
  })
  it('uploads files explicitly and leaves unavailable features out of the composer', async () => {
    const wrapper = mount(GeneratorInlinePanel, options)
    const input = wrapper.get('input[type="file"]')
    const files = [new File(['a'], 'a.png', { type: 'image/png' }), new File(['b'], 'b.jpg', { type: 'image/jpeg' })]
    Object.defineProperty(input.element, 'files', { value: files })
    await input.trigger('change')
    expect(wrapper.emitted('importImage')).toEqual(files.map(file => [file]))
    expect(wrapper.text()).not.toMatch(/角色库|特效|运镜|积分|预计费用/)
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('displays old fractional durations without normalization and requires an integer for new submissions', async () => {
    const wrapper = mount(GeneratorInlinePanel, { ...options, props: { ...options.props,
      generator: { ...generator, generationSettings: { aspect_ratio: '16:9', resolution: '1080p', duration_seconds: 4.5 } } } })
    expect(wrapper.get('[aria-label="生成规格"]').text()).toContain('4.5s')
    expect(wrapper.get('[role="alert"]').text()).toContain('5–15 秒的整数')
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    const duration = wrapper.get('[aria-label="生成时长"]')
    expect(duration.attributes()).toMatchObject({ min: '5', max: '15', step: '1' })
    await duration.setValue(15)
    expect(wrapper.emitted('settingsChange')?.at(-1)?.[0]).toMatchObject({ duration_seconds: 15 })
    await wrapper.setProps({ pendingRecovery: true })
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.composer-submit').attributes('title')).toContain('同一提交标识')
    expect(wrapper.get('[role="alert"]').text()).toContain('上次提交待确认')
    wrapper.unmount()
  })
})
