import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setApiProjectId } from '../src/api/client'
import GeneratorNode from '../src/components/GeneratorNode.vue'
import type { GeneratorDraft, GeneratorVersion, ResolvedGenerator } from '../src/generatorGraph'
import { generatorFrameSize } from '../src/generatorGeometry'
import { defaultGenerationSettings, generationAssetUrl, type GenerationJob } from '../src/api/generations'

const generator: GeneratorDraft = { id: 'gen-a', kind: 'image', position: { x: 0, y: 0 }, manualPrompt: '', imageAssetId: 'asset-a', imageWidth: 768, imageHeight: 1366 }
const resolved: ResolvedGenerator = { prompt: '', promptSource: '手动输入', imageUrl: 'blob:local', imageSource: '本地导入图片', imageWidth: 768, imageHeight: 1366, ownImageUrl: 'blob:local', ownImageWidth: 768, ownImageHeight: 1366 }
const emptyResolved: ResolvedGenerator = { ...resolved, imageUrl: '', ownImageUrl: '' }
function failedTask(code = 'provider_capacity_exhausted'): GenerationJob {
  return { id: 'generation-failed', node_id: generator.id, kind: 'image', status: 'failed', stage: 'failed',
    progress: 20, revision: 2, provider: 'test', input_hash: 'fixture',
    request: { node_id: generator.id, kind: 'image', prompt: '测试', settings: defaultGenerationSettings('image') },
    assets: [], error: { code, message: 'raw-upstream-token-must-not-leak', retryable: true }, created_at: '', updated_at: '' }
}

describe('generator media card', () => {
  it('shows a static video poster and decodes a first frame only if the poster fails', async () => {
    const path = '/api/generation-assets/genasset-' + 'a'.repeat(64)
    const output = { id: 'output-video', url: path, mime_type: 'video/mp4', width: 1920, height: 1080 }
    const data = { generator: { ...generator, kind: 'video' as const }, resolved, reduced: true, output }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    expect(wrapper.find('video').exists()).toBe(false)
    expect(wrapper.get('.generator-video-poster').attributes('src')).toBe(generationAssetUrl(path) + '/poster?frame=first')
    await wrapper.get('.generator-video-poster').trigger('load')
    expect(wrapper.find('.generator-media-loading').exists()).toBe(false)
    await wrapper.get('.generator-video-poster').trigger('error')
    const media = wrapper.get('video')
    expect(media.attributes('src')).toBe(generationAssetUrl(path))
    const element = media.element as HTMLVideoElement
    Object.defineProperty(element, 'duration', { value: 5 })
    await media.trigger('loadedmetadata')
    expect(element.currentTime).toBeCloseTo(.04)
    await media.trigger('seeked')
    expect(wrapper.find('.generator-video-poster').exists()).toBe(false)
    expect(wrapper.find('.generator-media-loading').exists()).toBe(false)
    await wrapper.get('[aria-label="播放生成视频"]').trigger('click')
    expect(wrapper.emitted('previewToggle')).toEqual([[generator.id]])
    wrapper.unmount()
  })

  it('preserves project scope when deriving a generated video poster URL', () => {
    const path = '/api/generation-assets/genasset-' + 'a'.repeat(64) + '?project_id=project-a'
    const data = { generator: { ...generator, kind: 'video' as const }, resolved, reduced: true,
      output: { id: 'output-video', url: path, mime_type: 'video/mp4', width: 1920, height: 1080 } }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    const poster = new URL(wrapper.get('.generator-video-poster').attributes('src'))
    expect(poster.pathname).toContain('/poster')
    expect(poster.searchParams.get('project_id')).toBe('project-a')
    wrapper.unmount()
  })

  it('reports a failed media preview and allows an explicit reload', async () => {
    const data = { generator: { ...generator, kind: 'video' as const }, resolved, reduced: true,
      output: { id: 'output-video', url: '/actual.mp4', mime_type: 'video/mp4', width: 1920, height: 1080 } }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    await wrapper.get('video').trigger('error')
    expect(wrapper.get('[role="alert"]').text()).toContain('视频预览加载失败')
    const before = wrapper.get('video').element
    await wrapper.get('[aria-label="重新加载媒体预览"]').trigger('click')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('video').element).not.toBe(before)
    wrapper.unmount()
  })

  it('mounts the waterline inside the card and removes it at a terminal state', async () => {
    const task: GenerationJob = { id: 'generation-wave', node_id: generator.id, kind: 'image', status: 'running',
      stage: 'running', progress: 37, revision: 1, provider: 'test', input_hash: 'fixture',
      request: { node_id: generator.id, kind: 'image', prompt: '测试', settings: defaultGenerationSettings('image') },
      assets: [], error: null, created_at: '', updated_at: '' }
    const data = { generator: { ...generator, nodeMode: 'generator' as const }, resolved, reduced: true, task }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('.generator-card [role="progressbar"]').attributes('aria-valuenow')).toBe('37')
    expect(wrapper.find('.generator-node-progress').exists()).toBe(false)
    expect(wrapper.get('.generation-wave-progress').classes()).toContain('wave-paused')
    await wrapper.setProps({ data: { ...data, task: { ...task, progress: null } } })
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    await wrapper.setProps({ data: { ...data, task: { ...task, status: 'cancelled' } } })
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('leaves the card and its ports in place throughout camera preview', async () => {
    const item: GeneratorDraft = { ...generator, nodeMode: 'reference', generationSettings: defaultGenerationSettings('image') }
    const before = JSON.stringify(item)
    const wrapper = mount(GeneratorNode, { props: { data: { generator: item, resolved, reduced: true } }, global: { stubs: { Handle: true } } })
    await wrapper.get('.generator-card').trigger('dblclick')
    expect(wrapper.emitted('previewToggle')).toEqual([[item.id]])
    await wrapper.setProps({ data: { generator: item, resolved, reduced: true, previewOpen: true } })
    expect(wrapper.classes()).toContain('generator-preview-open')
    expect(wrapper.get('.generator-card').attributes('style')).toContain('height: 268px')
    expect(wrapper.get('.generator-handle-output').attributes('style')).toContain('top: 134px')
    expect(wrapper.find('.source-video-overlay').exists()).toBe(false)
    await wrapper.get('.generator-card').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('previewClose')).toEqual([[item.id]])
    await wrapper.setProps({ data: { generator: item, resolved, reduced: true, previewOpen: false } })
    expect(wrapper.get('.generator-card').attributes('style')).toContain('height: 268px')
    expect(JSON.stringify(item)).toBe(before)
    wrapper.unmount()
  })

  it('paints one original image at screen resolution outside the transformed pane and follows the camera', async () => {
    const flow = document.createElement('div'), pane = document.createElement('div')
    flow.className = 'vue-flow'; pane.className = 'vue-flow__transformationpane'
    flow.append(pane); document.body.append(flow)
    const data = { generator: { ...generator, position: { x: 20, y: 30 } }, resolved, reduced: true }
    const wrapper = mount(GeneratorNode, { attachTo: pane, props: { data }, global: { stubs: { Handle: true } } })
    try {
      const image = wrapper.get('.generator-card img').element
      const size = generatorFrameSize(data.generator)
      await wrapper.setProps({ data: { ...data, previewOpen: true }, previewViewport: { x: 11, y: 12, zoom: 4 } })
      const card = flow.querySelector<HTMLElement>(':scope > .generator-preview-surface')!
      expect(card.parentElement).toBe(flow)
      expect(card.querySelector('img')).toBe(image)
      expect(pane.querySelector('.generator-card')).toBeNull()
      expect(pane.querySelector('.generator-preview-footprint')).toBeTruthy()
      expect([card.style.left, card.style.top, card.style.width, card.style.height]).toEqual(['91px', '132px', Math.round(size.width * 4) + 'px', '1072px'])
      await wrapper.setProps({ previewViewport: { x: 20, y: 40, zoom: 3 } })
      expect([card.style.left, card.style.top, card.style.height]).toEqual(['80px', '130px', '804px'])
      expect(card.querySelectorAll('.generator-media-actions > *')).toHaveLength(1)
      expect(card.querySelector('.generator-media-actions > .generator-download')).toBeTruthy()
      await wrapper.get('.generator-preview-footprint').trigger('dblclick')
      expect(wrapper.emitted('previewClose')).toEqual([[generator.id]])
      await wrapper.setProps({ data })
      expect(wrapper.get('.generator-card img').element).toBe(image)
      expect(flow.querySelector('.generator-preview-surface')).toBeNull()
    } finally { wrapper.unmount(); flow.remove() }
  })

  it.each(['image', 'video'] as const)('downloads the selected %s version from both entry points', async kind => {
    const versions: GeneratorVersion[] = ['a', 'b'].map((digit) => {
      const id = 'genasset-' + digit.repeat(64)
      return { id: digit, jobId: 'generation-' + digit, createdAt: '', prompt: '', settings: defaultGenerationSettings(kind),
        asset: { id, mime_type: kind === 'video' ? 'video/mp4' : 'image/png', url: '/api/generation-assets/' + id,
          width: 1920, height: 1080 } }
    })
    const item = { ...generator, nodeMode: 'generator' as const, kind, versions, activeVersionId: 'b' }
    const data = { generator: item, resolved, reduced: true }
    setApiProjectId('project-a')
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true, CanvasVideoPlayer: { template: '<div />', methods: { pause: vi.fn() } } } } })
    try {
      expect(wrapper.find('.generator-download').exists()).toBe(false)
      await wrapper.setProps({ selected: true })
      const url = new URL(wrapper.get('.generator-download').attributes('href'))
      expect(url.pathname).toBe('/api/generation-assets/' + versions[1].asset.id)
      expect(url.searchParams.get('project_id')).toBe('project-a')
      expect(url.searchParams.get('download')).toBe('true')
      await wrapper.get('.generator-download').trigger('dblclick')
      expect(wrapper.emitted('previewToggle')).toBeUndefined()
      await wrapper.setProps({ selected: false, data: { ...data, previewOpen: true } })
      expect(wrapper.get('.generator-media-actions').element.children).toHaveLength(1)
      expect(wrapper.get('.generator-download').attributes('href')).toBe(url.href)
      await wrapper.setProps({ selected: true, data: { ...data, generator: { ...item, activeVersionId: 'a' } } })
      expect(new URL(wrapper.get('.generator-download').attributes('href')).pathname).toBe('/api/generation-assets/' + versions[0].asset.id)
    } finally { wrapper.unmount(); setApiProjectId('') }
  })

  it('does not open an empty card or a connected input as a media result', async () => {
    const wrapper = mount(GeneratorNode, { props: { data: { generator: { ...generator, kind: 'video' }, resolved: { ...resolved, ownImageUrl: '', imageUrl: 'blob:connected' }, reduced: true } }, global: { stubs: { Handle: true } } })
    await wrapper.get('.generator-card').trigger('dblclick')
    expect(wrapper.emitted('previewToggle')).toBeUndefined()
    expect(wrapper.find('.generator-card img').exists()).toBe(false)
    await wrapper.setProps({ selected: true })
    expect(wrapper.find('.generator-download').exists()).toBe(false)
    wrapper.unmount()
  })

  it('does not turn a drag release into a preview', async () => {
    const wrapper = mount(GeneratorNode, { props: { data: { generator, resolved, reduced: true } }, global: { stubs: { Handle: true } } })
    const card = wrapper.get('.generator-card')
    await card.trigger('pointerdown', { clientX: 20, clientY: 30, buttons: 1 })
    await card.trigger('pointermove', { clientX: 70, clientY: 90, buttons: 1 })
    await card.trigger('pointerup', { clientX: 70, clientY: 90 })
    await card.trigger('dblclick')
    expect(wrapper.emitted('previewToggle')).toBeUndefined()
    wrapper.unmount()
  })

  it('lazy-loads controls only in the enlarged real video and forwards actual captured assets', async () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    const data = { generator: { ...generator, kind: 'video' as const }, resolved, reduced: true,
      output: { id: 'output-a', url: '/actual.mp4', mime_type: 'video/mp4', width: 1920, height: 1080 } }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('video').attributes('controls')).toBeUndefined()
    expect(wrapper.find('.canvas-video-player').exists()).toBe(false)
    await wrapper.get('.generator-card').trigger('dblclick')
    expect(wrapper.emitted('previewToggle')).toEqual([[generator.id]])
    await wrapper.setProps({ data: { ...data, previewOpen: true } })
    await vi.dynamicImportSettled()
    await flushPromises()
    const player = wrapper.findComponent({ name: 'CanvasVideoPlayer' })
    expect(player.exists()).toBe(true)
    expect(player.props('src')).toBe('/actual.mp4')
    expect(wrapper.find('.generator-media-actions').exists()).toBe(false)
    expect(player.props('captureEnabled')).toBe(true)
    const captured = { blob: new Blob(['frame'], { type: 'image/png' }), width: 1920, height: 1080, timestamp: 2.25, source: 'current' as const }
    player.vm.$emit('capture', captured)
    expect(wrapper.emitted('capture')).toEqual([[{ ...captured, nodeId: generator.id }]])
    await wrapper.get('.player-controls').trigger('dblclick')
    expect(wrapper.emitted('previewClose')).toBeUndefined()
    await wrapper.setProps({ data: { ...data, previewOpen: false } })
    expect(wrapper.find('.canvas-video-player').exists()).toBe(false)
    expect(pause).toHaveBeenCalled()
    wrapper.unmount()
    pause.mockRestore(); play.mockRestore()
  })

  it('stops tilt and explicitly closes a preview when the active media changes', async () => {
    const raf = vi.fn()
    vi.stubGlobal('requestAnimationFrame', raf)
    const wrapper = mount(GeneratorNode, { props: { data: { generator, resolved, reduced: false, previewOpen: true } }, global: { stubs: { Handle: true } } })
    await wrapper.get('.generator-card').trigger('pointermove', { clientX: 30, clientY: 40 })
    expect(raf).not.toHaveBeenCalled()
    await wrapper.setProps({ data: { generator, resolved: { ...resolved, ownImageUrl: 'blob:replacement' }, reduced: false, previewOpen: true } })
    expect(wrapper.emitted('previewClose')).toEqual([[generator.id]])
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('shows imported portrait image as the only card content without cropping', () => {
    const wrapper = mount(GeneratorNode, { props: { data: { generator, resolved, reduced: false } }, global: { stubs: { Handle: true } } })
    expect(wrapper.find('.generator-card img').attributes('src')).toBe('blob:local')
    const size = generatorFrameSize(generator)
    expect((wrapper.get('.generator-card').element as HTMLElement).style.width).toBe(size.width + 'px')
    expect((wrapper.get('.generator-card').element as HTMLElement).style.height).toBe(size.height + 'px')
    expect(wrapper.find('.generator-card strong').exists()).toBe(false)
    expect(wrapper.findAll('.magnetic-port')).toHaveLength(1)
    expect(wrapper.find('.generator-handle-input').exists()).toBe(false)
    expect(wrapper.get('.generator-handle-output').attributes('id')).toBe('output')
    expect(wrapper.find('.generator-external-label').text()).toContain('参考资产')
    expect(wrapper.attributes('aria-label')).toBe('图片参考节点')
    expect(wrapper.classes()).toContain('generator-state-reference-ready')
    wrapper.unmount()
  })

  it('does not mirror a connected reference onto a downstream card', () => {
    const downstream = { ...resolved, imageUrl: 'blob:upstream', ownImageUrl: '', imageSource: '已连接图片节点' }
    const wrapper = mount(GeneratorNode, { props: { data: { generator: { ...generator, kind: 'video' }, resolved: downstream, reduced: false } }, global: { stubs: { Handle: true } } })
    expect(wrapper.find('.generator-card img').exists()).toBe(false)
    expect(wrapper.find('.generator-external-label').text()).toContain('参考输入已连接')
    wrapper.unmount()
  })

  it('tilts up to four degrees toward the pointer and returns to center', async () => {
    const queue: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { queue.push(callback); return queue.length })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const wrapper = mount(GeneratorNode, { props: { data: { generator, resolved, reduced: false } }, global: { stubs: { Handle: true } } })
    const card = wrapper.get('.generator-card')
    Object.defineProperty(card.element, 'getBoundingClientRect', { value: () => ({ left: 0, top: 0, width: 268, height: 200 }) })
    await card.trigger('pointermove', { clientX: 268, clientY: 0, pointerType: 'mouse', buttons: 0 })
    queue.shift()?.(0)
    expect(card.classes()).toContain('generator-tilting')
    expect(card.attributes('style')).toContain('--generator-tilt-x: 0.92deg')
    expect(card.attributes('style')).toContain('--generator-tilt-y: 0.92deg')
    await card.trigger('pointerleave')
    queue.shift()?.(16)
    expect(card.attributes('style')).toContain('--generator-tilt-x:')
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('keeps a static placeholder and supports reduced-motion pointer behavior', async () => {
    const wrapper = mount(GeneratorNode, { props: { data: { generator: { ...generator, kind: 'video' }, resolved: { ...resolved, imageUrl: '', ownImageUrl: '' }, reduced: true } }, global: { stubs: { Handle: true } } })
    await wrapper.get('.generator-card').trigger('pointermove')
    expect(wrapper.get('.generator-card').attributes('style')).not.toContain('--generator-tilt')
    expect(wrapper.find('.generator-placeholder').text()).toContain('视频生成')
    wrapper.unmount()
  })

  it.each(['image', 'video'] as const)('updates the %s frame and ports from settings while retaining the original media', async kind => {
    const wrapper = mount(GeneratorNode, { props: { data: { generator: { ...generator, nodeMode: 'generator', kind, generationSettings: defaultGenerationSettings(kind) }, resolved, reduced: true } }, global: { stubs: { Handle: true } } })
    expect((wrapper.get('.generator-card').element as HTMLElement).style.height).toBe('150.75px')
    for (const ratio of ['9:16', '1:1', '4:3', '3:4', '16:9'] as const) {
      const updated = { ...generator, nodeMode: 'generator' as const, kind, generationSettings: { ...defaultGenerationSettings(kind), aspect_ratio: ratio } }
      await wrapper.setProps({ data: { generator: updated, resolved, reduced: true } })
      const size = generatorFrameSize(updated)
      expect((wrapper.get('.generator-shell').element as HTMLElement).style.width).toBe(size.width + 'px')
      expect((wrapper.get('.generator-card').element as HTMLElement).style.height).toBe(size.height + 'px')
      expect(wrapper.get('.generator-handle-input').attributes('style')).toContain('top: ' + size.height / 2 + 'px')
      expect(wrapper.get('.generator-card img').attributes('src')).toBe('blob:local')
    }
    wrapper.unmount()
  })

  it('uses the selected frame for real video output instead of the output file dimensions', () => {
    const wrapper = mount(GeneratorNode, { props: { data: { generator: { ...generator, kind: 'video', generationSettings: { ...defaultGenerationSettings('video'), aspect_ratio: '9:16' } }, resolved, reduced: true,
      output: { id: 'output-a', url: '/api/output.mp4', mime_type: 'video/mp4', width: 1920, height: 1080 } } }, global: { stubs: { Handle: true } } })
    expect((wrapper.get('.generator-card').element as HTMLElement).style.width).toBe('150.75px')
    expect((wrapper.get('.generator-card').element as HTMLElement).style.height).toBe('268px')
    expect(wrapper.get('video').attributes('src')).toBe('/api/output.mp4')
    wrapper.unmount()
  })

  it('keeps a manual reference in its original ratio despite saved generation settings', () => {
    const item: GeneratorDraft = { ...generator, nodeMode: 'reference', generationSettings: defaultGenerationSettings('image') }
    const wrapper = mount(GeneratorNode, { props: { data: { generator: item, resolved, reduced: true } }, global: { stubs: { Handle: true } } })
    const card = wrapper.get('.generator-card').element as HTMLElement
    expect(card.style.height).toBe('268px')
    expect(parseFloat(card.style.width) / parseFloat(card.style.height)).toBeCloseTo(768 / 1366)
    expect(wrapper.find('[aria-label="查看生成历史"]').exists()).toBe(false)
    expect(wrapper.find('.generator-node-progress').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the pinned result with at most two album layers and emits controlled history selection', async () => {
    const versions: GeneratorVersion[] = [4, 3, 2, 1].map(number => ({ id: 'version-' + number, jobId: 'generation-' + number,
      createdAt: `2026-11-10T00:00:0${number}Z`, prompt: '原始 Prompt ' + number,
      settings: defaultGenerationSettings('image'), asset: { id: 'asset-' + number, url: '/generated-' + number + '.png', mime_type: 'image/png', width: 1920, height: 1080 } }))
    const item: GeneratorDraft = { ...generator, nodeMode: 'generator', manualPrompt: '正在编辑的新 Prompt', versions, activeVersionId: 'version-2' }
    const wrapper = mount(GeneratorNode, { props: { data: { generator: item, resolved, reduced: true } }, global: { stubs: { Handle: true, Teleport: true } } })
    expect(wrapper.get('.generator-card img').attributes('src')).toBe('/generated-2.png')
    expect(wrapper.findAll('.generator-album-layer')).toHaveLength(2)
    await wrapper.get('[aria-label="查看生成历史"]').trigger('click')
    expect(wrapper.emitted('historyToggle')).toEqual([[generator.id]])
    expect(wrapper.find('[data-generator-history]').exists()).toBe(false)
    await wrapper.setProps({ data: { generator: item, resolved, reduced: true, historyOpen: true } })
    await vi.dynamicImportSettled()
    await flushPromises()
    expect(wrapper.get('[data-generator-history]').findAll('li')).toHaveLength(4)
    await wrapper.get('[aria-label="置顶版本 1"]').trigger('click')
    expect(wrapper.emitted('selectVersion')).toEqual([[{ nodeId: generator.id, versionId: 'version-1' }]])
    expect(item.manualPrompt).toBe('正在编辑的新 Prompt')
    expect(wrapper.get('.generator-card img').attributes('src')).toBe('/generated-2.png')
    await wrapper.setProps({ data: { generator: { ...item, activeVersionId: 'version-1' }, resolved, reduced: true, historyOpen: true } })
    expect(wrapper.get('.generator-card img').attributes('src')).toBe('/generated-1.png')
    wrapper.unmount()
  })

  it('retains a true video result while its next editable task has not completed', () => {
    const version: GeneratorVersion = { id: 'video-version', jobId: 'generation-old', createdAt: '2026-11-10T00:00:00Z', prompt: '旧视频 Prompt',
      settings: defaultGenerationSettings('video'), asset: { id: 'asset-video', url: '/actual-result.mp4', mime_type: 'video/mp4', width: 1080, height: 1920 } }
    const item: GeneratorDraft = { ...generator, kind: 'video', nodeMode: 'generator', versions: [version], manualPrompt: '新视频 Prompt' }
    const wrapper = mount(GeneratorNode, { props: { data: { generator: item, resolved, reduced: true, stale: true } }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('.generator-card video').attributes('src')).toBe('/actual-result.mp4')
    expect(wrapper.find('.generator-card img').exists()).toBe(false)
    expect(wrapper.get('.generator-external-label').text()).toContain('保留置顶版本')
    expect(wrapper.find('.generator-node-progress').exists()).toBe(false)
    wrapper.unmount()
  })

  it('replaces running progress with the specific safe failure inside the card', async () => {
    const task = failedTask()
    const item: GeneratorDraft = { ...generator, nodeMode: 'generator', generationTask: { jobId: task.id, inputSignature: 'same' } }
    const data = { generator: item, resolved: emptyResolved, reduced: true, visualState: 'rendering' as const, task: { ...task, status: 'running' as const, error: null } }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(true)
    await wrapper.setProps({ data: { ...data, visualState: 'failed', task } })
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.find('.generation-starfield-progress').exists()).toBe(false)
    const feedback = wrapper.get('[data-generation-feedback]')
    expect(feedback.attributes('role')).toBe('alert')
    expect(feedback.attributes('data-error-code')).toBe('provider_capacity_exhausted')
    expect(feedback.text()).toContain('生成失败：上游算力不足')
    expect(wrapper.text()).not.toContain('raw-upstream')
    expect(wrapper.text()).not.toContain('100%')
    expect(wrapper.classes()).toContain('generator-state-failed')
    expect(wrapper.emitted('submitGeneration')).toBeUndefined()
    wrapper.unmount()
  })

  it('retains the last delivered image and history after a failed new attempt', () => {
    const version: GeneratorVersion = { id: 'kept-version', jobId: 'generation-old', createdAt: '2026-11-10T00:00:00Z', prompt: '旧图',
      settings: defaultGenerationSettings('image'), asset: { id: 'kept-asset', url: '/kept-result.png', mime_type: 'image/png', width: 1280, height: 720 } }
    const item: GeneratorDraft = { ...generator, nodeMode: 'generator', versions: [version], activeVersionId: version.id }
    const wrapper = mount(GeneratorNode, { props: { data: { generator: item, resolved: emptyResolved, reduced: true, task: failedTask('content_rejected') } }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('.generator-card img').attributes('src')).toBe('/kept-result.png')
    expect(wrapper.get('[data-generation-feedback]').classes()).toContain('feedback-over-media')
    expect(wrapper.get('[data-generation-feedback]').text()).toContain('内容审核未通过')
    expect(wrapper.find('[aria-label="查看生成历史"]').exists()).toBe(true)
    expect(item.versions).toHaveLength(1)
    wrapper.unmount()
  })

  it('does not put a stale, foreign, or reference-only failure on this node', async () => {
    const task = failedTask()
    const item: GeneratorDraft = { ...generator, nodeMode: 'generator' }
    const data = { generator: item, resolved: emptyResolved, reduced: true, visualState: 'failed' as const, task }
    const wrapper = mount(GeneratorNode, { props: { data: { ...data, stale: true } }, global: { stubs: { Handle: true } } })
    expect(wrapper.find('[data-generation-feedback]').exists()).toBe(false)
    expect(wrapper.classes()).not.toContain('generator-state-failed')
    for (const patch of [
      { task: { ...task, node_id: 'other-node' } },
      { task: { ...task, kind: 'video' as const } },
      { generator: { ...item, generationTask: { jobId: 'another-task', inputSignature: 'same' } } },
      { generator: { ...item, nodeMode: 'reference' as const } },
    ]) {
      await wrapper.setProps({ data: { ...data, ...patch } })
      expect(wrapper.find('[data-generation-feedback]').exists()).toBe(false)
      expect(wrapper.classes()).not.toContain('generator-state-failed')
    }
    wrapper.unmount()
  })

  it.each([
    ['cancelled', 'provider_cancelled', '云端已取消'],
    ['cancelled', 'user_cancelled', '生成已取消'],
    ['interrupted', 'worker_interrupted', '生成已中断'],
  ] as const)('shows %s / %s on the card as %s', (status, code, title) => {
    const task = { ...failedTask(code), status }
    const wrapper = mount(GeneratorNode, { props: { data: { generator: { ...generator, nodeMode: 'generator' }, resolved: emptyResolved, reduced: true, task } }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('[data-generation-feedback]').attributes('role')).toBe('status')
    expect(wrapper.get('[data-generation-feedback] strong').text()).toBe(title)
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(wrapper.classes()).not.toContain('generator-state-failed')
    wrapper.unmount()
  })

  it('keeps a delivered video playable when the next video attempt fails', async () => {
    const task = { ...failedTask('provider_network_error'), kind: 'video' as const }
    const item: GeneratorDraft = { ...generator, kind: 'video', nodeMode: 'generator' }
    const data = { generator: item, resolved: emptyResolved, reduced: true, task,
      output: { id: 'kept-video', url: '/kept-result.mp4', mime_type: 'video/mp4', width: 1280, height: 720 } }
    const wrapper = mount(GeneratorNode, { props: { data }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('.generator-card video').attributes('src')).toBe('/kept-result.mp4')
    expect(wrapper.get('[aria-label="播放生成视频"]').classes()).toContain('play-above-feedback')
    expect(wrapper.get('[data-generation-feedback]').text()).toContain('云端网络连接失败')
    await wrapper.get('[aria-label="播放生成视频"]').trigger('click')
    expect(wrapper.emitted('previewToggle')).toEqual([[generator.id]])
    wrapper.unmount()
  })
})
