import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GeneratorHistory from '../src/components/GeneratorHistory.vue'
import type { GeneratorVersion } from '../src/generatorGraph'
import { setApiProjectId } from '../src/api/client'
import { defaultGenerationSettings, generationAssetUrl, generationVideoPosterUrl } from '../src/api/generations'

const mediaPath = (n: number) => '/api/generation-assets/genasset-' + n.toString(16).padStart(64, '0')
const versions: GeneratorVersion[] = [3, 2, 1].map(number => ({ id: 'version-' + number, jobId: 'generation-' + number, createdAt: `2026-11-10T00:00:0${number}Z`,
  prompt: `完整 Prompt ${number}\n包含原始动作与画风。`, settings: { ...defaultGenerationSettings('image'), model_id: 'actual-model-' + number },
  asset: { id: 'asset-' + number, url: mediaPath(number), mime_type: 'image/png', width: 1920, height: 1080 } }))

describe('generator version history', () => {
  it('adds the current project to legacy poster links and keeps an explicitly scoped version', () => {
    setApiProjectId('current-project')
    try {
      expect(new URL(generationVideoPosterUrl(mediaPath(1))).searchParams.get('project_id')).toBe('current-project')
      expect(new URL(generationVideoPosterUrl(mediaPath(2) + '?project_id=original')).searchParams.get('project_id')).toBe('original')
    } finally {
      setApiProjectId('')
    }
  })

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

  it('shows a distinct first-frame image per version and streams only the chosen original', async () => {
    const videoVersions = versions.map((version, i) => ({ ...version, settings: defaultGenerationSettings('video'),
      asset: { ...version.asset, url: mediaPath(i) + (i ? '' : '?project_id=example'), mime_type: 'video/mp4' } }))
    const wrapper = mount(GeneratorHistory, { props: { kind: 'video', versions: videoVersions, activeVersionId: 'version-3', anchor: { x: 0, y: 0 } }, global: { stubs: { Teleport: true } } })
    expect(wrapper.find('video').exists()).toBe(false)
    const posters = wrapper.findAll('.history-media img')
    expect(posters.map(img => img.attributes('src'))).toEqual([
      generationAssetUrl(mediaPath(0)) + '/poster?project_id=example&frame=first',
      generationAssetUrl(mediaPath(1)) + '/poster?frame=first',
      generationAssetUrl(mediaPath(2)) + '/poster?frame=first',
    ])
    expect(posters.every(img => img.attributes('loading') === 'lazy')).toBe(true)
    await wrapper.get('[aria-label="播放视频版本 3"]').trigger('click')
    expect(wrapper.get('video').attributes('src')).toBe(generationAssetUrl(videoVersions[0].asset.url))
    expect(wrapper.get('video').attributes('controls')).toBeDefined()
    // A failed thumbnail still permits loading the actual version on demand.
    await wrapper.get('[data-version-id="version-2"] img').trigger('error')
    expect(wrapper.get('[data-version-id="version-2"]').text()).toContain('播放原视频')
    await wrapper.get('[aria-label="播放视频版本 2"]').trigger('click')
    expect(wrapper.findAll('video')).toHaveLength(1)
    expect(wrapper.get('video').attributes('src')).toBe(generationAssetUrl(videoVersions[1].asset.url))
    await wrapper.get('video').trigger('error')
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.get('[data-version-id="version-2"]').text()).toContain('播放失败')
    await wrapper.get('[aria-label="播放视频版本 2"]').trigger('click')
    expect(wrapper.findAll('video')).toHaveLength(1)
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('renders twelve video versions at a time while keeping older pinned versions reachable', async () => {
    const videoVersions: GeneratorVersion[] = Array.from({ length: 25 }, (_, index) => ({ ...versions[0], id: 'video-' + (25 - index),
      settings: defaultGenerationSettings('video'), asset: { ...versions[0].asset, url: mediaPath(index), mime_type: 'video/mp4' } }))
    const wrapper = mount(GeneratorHistory, { props: { kind: 'video', versions: videoVersions, activeVersionId: 'video-1', anchor: { x: 0, y: 0 } }, global: { stubs: { Teleport: true } } })
    expect(wrapper.findAll('li')).toHaveLength(12)
    expect(wrapper.findAll('.history-play img')).toHaveLength(12)
    expect(wrapper.find('[data-version-id="video-1"]').exists()).toBe(false)
    await wrapper.get('.history-load-more').trigger('click')
    expect(wrapper.findAll('li')).toHaveLength(24)
    expect(wrapper.findAll('.history-play img')).toHaveLength(24)
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
