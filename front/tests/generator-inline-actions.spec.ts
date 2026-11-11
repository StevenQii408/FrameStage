import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import GeneratorInlinePanel from '../src/components/GeneratorInlinePanel.vue'
import type { GeneratorDraft, GeneratorConnection, ResolvedGenerator } from '../src/generatorGraph'
import type { GenerationJob } from '../src/api/generations'
import { setPromptText } from './promptEditorTestHelpers'

const generator: GeneratorDraft = { id: 'gen-a', kind: 'video', position: { x: 0, y: 0 }, manualPrompt: '手填内容', imageAssetId: 'image-a', imageWidth: 768, imageHeight: 1366 }
const resolved: ResolvedGenerator = { prompt: '已连接内容', promptSource: '已连接 Prompt', imageUrl: 'blob:preview', imageSource: '本地导入图片', imageWidth: 768, imageHeight: 1366, ownImageUrl: 'blob:preview', ownImageWidth: 768, ownImageHeight: 1366 }
const connections: GeneratorConnection[] = [{ id: 'wire-gen-a-prompt-in', source: 'prompt-a', sourceHandle: 'prompt-out', target: 'gen-a', targetHandle: 'prompt-in' }]
const setup = { attachTo: document.body, props: { generator, resolved, connections, anchor: { x: 100, y: 200 } }, global: { stubs: { Teleport: true } } }

describe('inline generation actions', () => {
  it('only emits style analysis after an explicit user click on a local reference', async () => {
    const wrapper = mount(GeneratorInlinePanel, setup)
    expect(wrapper.emitted('analyzeStyle')).toBeUndefined()
    expect(wrapper.find('[aria-label="分析本地参考图画风"]').exists()).toBe(false)
    await wrapper.get('[aria-label="更多节点操作"]').trigger('click')
    await wrapper.get('[aria-label="分析本地参考图画风"]').trigger('click')
    expect(wrapper.emitted('analyzeStyle')).toHaveLength(1)
    await wrapper.setProps({ generator: { ...generator, imageAssetId: null } })
    await wrapper.get('[aria-label="更多节点操作"]').trigger('click')
    expect(wrapper.find('[aria-label="分析本地参考图画风"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('keeps editing below the card and retains disconnect/delete actions', async () => {
    const wrapper = mount(GeneratorInlinePanel, setup)
    expect(wrapper.find('button[type="submit"]').exists()).toBe(false)
    await setPromptText(wrapper.get('.inline-prompt'), '修改后的本地内容')
    expect(wrapper.emitted('promptChange')?.[0]).toEqual(['修改后的本地内容'])
    await wrapper.get('[aria-label="更多节点操作"]').trigger('click')
    await wrapper.get('[aria-label="断开 Prompt 来源"]').trigger('click')
    expect(wrapper.emitted('disconnect')?.[0]).toEqual(['prompt-in'])
    await wrapper.get('[aria-label="更多节点操作"]').trigger('click')
    await wrapper.get('[aria-label="删除此生成节点"]').trigger('click')
    expect(wrapper.emitted('removeNode')).toHaveLength(1)
    wrapper.unmount()
  })
  it('saves an unconfigured task without presenting executable generation', async () => {
    const wrapper = mount(GeneratorInlinePanel, setup)
    await wrapper.setProps({ capabilities: { configured: false, provider: 'bailian-generation', reason: '未启用',
      video: { available: false, models: [{ id: 'wan3.0-video', name: '万相 Wan 3.0' }],
        aspect_ratios: ['16:9', '9:16'], resolutions: ['720p', '1080p'] } } })
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    await wrapper.get('[aria-label="生成画幅"]').setValue('9:16')
    expect(wrapper.emitted('settingsChange')?.[0][0]).toMatchObject({ aspect_ratio: '9:16' })
    await wrapper.get('.composer-submit').trigger('click')
    expect(wrapper.emitted('submitGeneration')).toHaveLength(1)
    expect(wrapper.get('.composer-submit').attributes('aria-label')).toBe('校验并保存任务')
    expect(wrapper.get('.composer-submit').attributes('title')).toContain('未调用生成 API')
    expect(wrapper.get('.composer-submit').text()).toBe('')
    wrapper.unmount()
  })
  it('allows an explicit same-input generation after success while keeping blocked saves disabled', async () => {
    const task = { id: 'generation-a', status: 'succeeded', progress: 100, request: { prompt: '原始 Prompt' } } as GenerationJob
    const capabilities = { configured: true, provider: 'test', reason: '', video: { available: true, aspect_ratios: ['16:9'], resolutions: ['1080p'] } }
    const before = JSON.stringify({ generator, task })
    const wrapper = mount(GeneratorInlinePanel, { ...setup, props: { ...setup.props, task, capabilities } })
    expect(wrapper.get('.composer-submit').attributes('aria-label')).toBe('再次生成')
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeUndefined()
    await wrapper.get('.composer-submit').trigger('click')
    expect(wrapper.emitted('submitGeneration')).toHaveLength(1)
    expect(JSON.stringify({ generator, task })).toBe(before)
    await wrapper.setProps({ stale: true })
    expect(wrapper.get('.composer-submit').attributes('aria-label')).toBe('生成')
    await wrapper.setProps({ stale: false, task: { ...task, status: 'blocked' } })
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    await wrapper.get('.composer-submit').trigger('click')
    expect(wrapper.emitted('submitGeneration')).toHaveLength(1)
    await wrapper.setProps({ task, capabilities: { ...capabilities, configured: false } })
    expect(wrapper.get('.composer-submit').attributes('aria-label')).toBe('生成服务未接入')
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('allows local validation and save with an empty disabled provider while requiring declarations from an available provider', async () => {
    const wrapper = mount(GeneratorInlinePanel, { ...setup, props: { ...setup.props,
      capabilities: { configured: false, provider: 'disabled', reason: '未接入',
        image: { available: false, models: [] }, video: { available: false, models: [] } } } })
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    await wrapper.get('[aria-label="生成规格"]').trigger('click')
    expect(wrapper.get('[aria-label="生成画幅"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[aria-label="生成分辨率"]').attributes('disabled')).toBeDefined()
    await wrapper.get('.composer-submit').trigger('click')
    expect(wrapper.emitted('submitGeneration')).toHaveLength(1)
    await wrapper.setProps({ capabilities: { configured: true, provider: 'available-but-incomplete', reason: '',
      video: { available: true, models: [] } } })
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[role="alert"]').attributes('title')).toContain('尚未声明支持的画幅')
    expect(wrapper.get('[role="alert"]').attributes('title')).toContain('尚未声明支持的分辨率')
    wrapper.unmount()
  })
  it('shows indeterminate server progress and no percent for blocked tasks', async () => {
    const task = { id: 'generation-a', status: 'running', progress: null } as GenerationJob
    const wrapper = mount(GeneratorInlinePanel, { ...setup, props: { ...setup.props, task } })
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    expect(wrapper.get('[role="progressbar"]').attributes('aria-label')).toBe('等待服务端进度')
    expect(wrapper.get('.composer-submit').attributes('aria-label')).toBe('取消生成任务')
    await wrapper.get('.composer-submit').trigger('click')
    expect(wrapper.emitted('cancelGeneration')).toHaveLength(1)
    await wrapper.setProps({ task: { ...task, progress: 37 } })
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('37')
    expect(wrapper.get('.composer-task-state').text()).toContain('37%')
    await wrapper.setProps({ task: { ...task, status: 'blocked', error: { code: 'provider_unconfigured', message: '生成服务未配置', retryable: true } } })
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('等待接入生成 API')
    expect(wrapper.text()).not.toContain('100%')
    expect(wrapper.get('.composer-submit').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('.inline-feedback')).toHaveLength(1)
    wrapper.unmount()
  })
  it('keeps retry explicit and prevents retrying a provider rejection without edited inputs', async () => {
    const task = { id: 'generation-a', status: 'failed', progress: null, error: { code: 'content_rejected', message: '请修改输入', retryable: false } } as GenerationJob
    const wrapper = mount(GeneratorInlinePanel, { ...setup, props: { ...setup.props, task,
      capabilities: { configured: true, provider: 'test', reason: '', video: { available: true, aspect_ratios: ['16:9'], resolutions: ['1080p'] } } } })
    await wrapper.get('[aria-label="更多节点操作"]').trigger('click')
    const retry = wrapper.findAll('[role="menuitem"]').find(item => item.text() === '重试原任务')!
    expect(retry.attributes('disabled')).toBeDefined()
    await retry.trigger('click')
    expect(wrapper.emitted('retryGeneration')).toBeUndefined()
    await wrapper.setProps({ task: { ...task, error: { ...task.error!, retryable: true } } })
    const enabledRetry = wrapper.findAll('[role="menuitem"]').find(item => item.text() === '重试原任务')!
    expect(enabledRetry.attributes('disabled')).toBeUndefined()
    await enabledRetry.trigger('click')
    expect(wrapper.emitted('retryGeneration')).toHaveLength(1)
    wrapper.unmount()
  })
  it('pauses the real active-task accent when the page is hidden and removes progress on completion', async () => {
    const original = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    let visibility = 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
    const task = { id: 'generation-a', status: 'running', progress: 20 } as GenerationJob
    const wrapper = mount(GeneratorInlinePanel, { ...setup, props: { ...setup.props, task } })
    try {
      expect(wrapper.get('.generator-inline-panel').classes()).not.toContain('is-page-hidden')
      visibility = 'hidden'
      document.dispatchEvent(new Event('visibilitychange'))
      await wrapper.vm.$nextTick()
      expect(wrapper.get('.generator-inline-panel').classes()).toContain('is-page-hidden')
      await wrapper.setProps({ task: { ...task, status: 'succeeded', progress: 100 } })
      expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
      expect(wrapper.get('.generator-inline-panel').classes()).not.toContain('is-processing')
    } finally {
      wrapper.unmount()
      if (original) Object.defineProperty(document, 'visibilityState', original)
      else delete (document as unknown as Record<string, unknown>).visibilityState
    }
  })
})
