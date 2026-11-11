import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CanvasVideoPlayer from '../src/components/CanvasVideoPlayer.vue'
import { captureVideoFrame } from '../src/mediaPlayback'

vi.mock('../src/mediaPlayback', async original => ({ ...await original<typeof import('../src/mediaPlayback')>(), captureVideoFrame: vi.fn() }))
const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => { vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {}); vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined) })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks(); document.body.innerHTML = '' })

async function player(props = {}) {
  const wrapper = mount(CanvasVideoPlayer, { props: { src: '/api/video.mp4', ...props }, attachTo: document.body })
  wrappers.push(wrapper)
  const video = wrapper.get('video').element as HTMLVideoElement
  Object.defineProperties(video, { duration: { configurable: true, value: 10 }, videoWidth: { value: 1280 }, videoHeight: { value: 720 } })
  await wrapper.get('video').trigger('loadedmetadata')
  return { wrapper, video }
}

describe('canvas media controls', () => {
  it('loads metadata only and displays exact local cut positions in the custom seek bar', async () => {
    const { wrapper } = await player({ cuts: [0, 3.2389, 3.2389, 10, 6] })
    expect(wrapper.get('video').attributes('controls')).toBeUndefined()
    expect(wrapper.get('video').attributes('preload')).toBe('metadata')
    expect(wrapper.get('video').attributes('crossorigin')).toBe('anonymous')
    expect(wrapper.findAll('.player-cut')).toHaveLength(2)
    expect(wrapper.findAll('.player-cut')[0].attributes('style')).toContain('32.389%')
    expect(wrapper.findAll('.player-cut')[0].attributes('title')).toContain('PySceneDetect')
    expect(wrapper.emitted('ready')?.[0]).toEqual([{ duration: 10, width: 1280, height: 720 }])
  })

  it('supports scrub and cut-point seek without quantizing source time', async () => {
    const { wrapper, video } = await player({ cuts: [3.2389] })
    await wrapper.get('.player-cut').trigger('click')
    expect(video.currentTime).toBe(3.2389)
    await wrapper.get('.player-seek').setValue('5.125')
    expect(video.currentTime).toBe(5.125)
  })

  it('does not expose capture options for a source-only viewer', async () => {
    const { wrapper } = await player()
    expect(wrapper.find('[aria-label="截取视频画面"]').exists()).toBe(false)
    await wrapper.trigger('keydown', { key: ' ', code: 'Space' })
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled()
  })

  it('captures an explicit selected frame and emits the real image data', async () => {
    const frame = { blob: new Blob(['png'], { type: 'image/png' }), width: 1280, height: 720, timestamp: 0, source: 'first' as const }
    vi.mocked(captureVideoFrame).mockResolvedValueOnce(frame)
    const { wrapper, video } = await player({ captureEnabled: true })
    await wrapper.get('[aria-label="截取视频画面"]').trigger('click')
    expect(wrapper.findAll('[role=menuitem]').map(item => item.text())).toEqual(['截取首帧', '截取当前帧', '截取尾帧'])
    await wrapper.findAll('[role=menuitem]')[0].trigger('click')
    await flushPromises()
    expect(captureVideoFrame).toHaveBeenCalledWith(video, 'first', expect.any(AbortSignal))
    expect(wrapper.emitted('capture')?.[0][0]).toBe(frame)
    expect(wrapper.find('[role=menu]').exists()).toBe(false)
  })

  it('leaves double-click on video to its node while insulating control gestures', async () => {
    const { wrapper } = await player()
    const outside = vi.fn()
    document.body.addEventListener('dblclick', outside)
    await wrapper.get('video').trigger('dblclick')
    expect(outside).toHaveBeenCalledOnce()
    await wrapper.get('.player-play').trigger('dblclick')
    expect(outside).toHaveBeenCalledOnce()
    document.body.removeEventListener('dblclick', outside)
  })

  it('preserves Escape-to-collapse when no capture popover is open', async () => {
    const { wrapper } = await player({ captureEnabled: true })
    const outside = vi.fn()
    document.body.addEventListener('keydown', outside)
    await wrapper.trigger('keydown', { key: 'Escape' })
    expect(outside).toHaveBeenCalledOnce()
    await wrapper.get('[aria-label="截取视频画面"]').trigger('click')
    await wrapper.trigger('keydown', { key: 'Escape' })
    expect(outside).toHaveBeenCalledOnce()
    expect(wrapper.find('[role=menu]').exists()).toBe(false)
    document.body.removeEventListener('keydown', outside)
  })

  it('aborts an in-flight capture when the displayed version changes', async () => {
    let signal: AbortSignal | undefined
    vi.mocked(captureVideoFrame).mockImplementationOnce((_video, _kind, selected) => { signal = selected; return new Promise(() => {}) })
    const { wrapper } = await player({ captureEnabled: true })
    await wrapper.get('[aria-label="截取视频画面"]').trigger('click')
    await wrapper.findAll('[role=menuitem]')[1].trigger('click')
    expect(signal?.aborted).toBe(false)
    await wrapper.setProps({ src: '/api/replacement.mp4' })
    expect(signal?.aborted).toBe(true)
    expect(wrapper.emitted('capture')).toBeUndefined()
    expect(wrapper.get('.canvas-video-player').attributes('aria-busy')).toBe('false')
  })
})
