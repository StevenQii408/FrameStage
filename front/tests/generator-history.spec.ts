import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GeneratorHistory from '../src/components/GeneratorHistory.vue'
import type { GeneratorVersion } from '../src/generatorGraph'
import { defaultGenerationSettings } from '../src/api/generations'

const versions: GeneratorVersion[] = [3, 2, 1].map(number => ({ id: 'version-' + number, jobId: 'generation-' + number, createdAt: `2026-11-10T00:00:0${number}Z`,
  prompt: `完整 Prompt ${number}\n包含原始动作与画风。`, settings: { ...defaultGenerationSettings('image'), model_id: 'actual-model-' + number },
  asset: { id: 'asset-' + number, url: '/actual-' + number + '.png', mime_type: 'image/png', width: 1920, height: 1080 } }))

describe('generator version history', () => {
  it('lists every saved result and exact read-only snapshots without replacing editable inputs', async () => {
    const before = JSON.stringify(versions)
    const wrapper = mount(GeneratorHistory, { props: { kind: 'image', versions, activeVersionId: 'version-2', anchor: { x: 10, y: 20 } }, global: { stubs: { Teleport: true } } })
    expect(wrapper.get('[role="dialog"]').attributes('aria-modal')).toBe('false')
    expect(wrapper.findAll('li')).toHaveLength(3)
    expect(wrapper.get('li').text()).toContain('最新')
    expect(wrapper.get('[data-version-id="version-2"]').classes()).toContain('is-active')
    expect(wrapper.get('[data-version-id="version-2"] .history-prompt').text()).toBe(versions[1].prompt)
    expect(wrapper.get('[data-version-id="version-2"] .history-specification').text()).toBe('actual-model-2 · 16:9 · 1080p')
    expect(wrapper.find('textarea,input').exists()).toBe(false)
    await wrapper.get('[aria-label="置顶版本 1"]').trigger('click')
    expect(wrapper.emitted('selectVersion')).toEqual([['version-1']])
    expect(JSON.stringify(versions)).toBe(before)
    await wrapper.get('[aria-label="关闭生成历史"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('uses actual video previews and closes Escape locally without any simulated progress', async () => {
    const videoVersions: GeneratorVersion[] = [{ ...versions[0], settings: defaultGenerationSettings('video'), asset: { ...versions[0].asset, url: '/actual-video.mp4', mime_type: 'video/mp4' } }]
    const wrapper = mount(GeneratorHistory, { props: { kind: 'video', versions: videoVersions, activeVersionId: 'version-3', anchor: { x: 0, y: 0 } }, global: { stubs: { Teleport: true } } })
    expect(wrapper.get('video').attributes('src')).toBe('/actual-video.mp4')
    expect(wrapper.get('video').attributes('controls')).toBeDefined()
    expect(wrapper.find('img,[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.get('.history-specification').text()).toContain('5s')
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('renders twelve video versions at a time while keeping older pinned versions reachable', async () => {
    const videoVersions: GeneratorVersion[] = Array.from({ length: 25 }, (_, index) => ({ ...versions[0], id: 'video-' + (25 - index),
      settings: defaultGenerationSettings('video'), asset: { ...versions[0].asset, url: '/actual-video-' + index + '.mp4', mime_type: 'video/mp4' } }))
    const wrapper = mount(GeneratorHistory, { props: { kind: 'video', versions: videoVersions, activeVersionId: 'video-1', anchor: { x: 0, y: 0 } }, global: { stubs: { Teleport: true } } })
    expect(wrapper.findAll('li')).toHaveLength(12)
    expect(wrapper.findAll('video')).toHaveLength(12)
    expect(wrapper.find('[data-version-id="video-1"]').exists()).toBe(false)
    await wrapper.get('.history-load-more').trigger('click')
    expect(wrapper.findAll('li')).toHaveLength(24)
    expect(wrapper.findAll('video')).toHaveLength(24)
    await wrapper.get('.history-load-more').trigger('click')
    expect(wrapper.findAll('li')).toHaveLength(25)
    expect(wrapper.get('[data-version-id="video-1"]').classes()).toContain('is-active')
    expect(wrapper.get('[aria-label="当前置顶版本 1"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.history-load-more').exists()).toBe(false)
    await wrapper.get('[aria-label="置顶版本 2"]').trigger('click')
    expect(wrapper.emitted('selectVersion')).toEqual([['video-2']])
    expect(videoVersions.map(version => version.id)).toEqual(Array.from({ length: 25 }, (_, index) => 'video-' + (25 - index)))
    wrapper.unmount()
  })
})
