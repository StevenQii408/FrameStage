import { afterEach, describe, expect, it, vi } from 'vitest'
import { captureVideoFrame, formatMediaTime, frameCapturePosition, normalizeVideoCuts } from '../src/mediaPlayback'

afterEach(() => vi.restoreAllMocks())

function movie(wasPlaying = false) {
  const video = document.createElement('video')
  let time = 3, paused = !wasPlaying, seeking = false
  Object.defineProperties(video, {
    duration: { configurable: true, value: 10 }, videoWidth: { value: 1920 }, videoHeight: { value: 1080 }, readyState: { value: 2 },
    paused: { get: () => paused }, ended: { value: false }, seeking: { get: () => seeking },
    currentTime: { configurable: true, get: () => time, set: value => { time = value; seeking = true; queueMicrotask(() => { seeking = false; video.dispatchEvent(new Event('seeked')) }) } },
  })
  const pause = vi.spyOn(video, 'pause').mockImplementation(() => { paused = true })
  const play = vi.spyOn(video, 'play').mockImplementation(async () => { paused = false })
  const captured: number[] = []
  const draw = vi.fn(() => captured.push(video.currentTime))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draw } as never)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['real-frame'], { type: 'image/png' })))
  return { video, pause, play, captured, draw }
}

describe('source media playback helpers', () => {
  it('filters real interior cut timestamps without rounding or inventing markers', () => {
    expect(normalizeVideoCuts([3.2389, 0, NaN, 10, 12, 3.2389, 1.25, -1], 10)).toEqual([1.25, 3.2389])
    expect(normalizeVideoCuts([2], NaN)).toEqual([])
    expect(formatMediaTime(63.239, true)).toBe('1:03.23')
    expect(formatMediaTime(Infinity)).toBe('0:00')
  })

  it('keeps the tail capture within media rather than seeking to its ended surface', () => {
    expect(frameCapturePosition('first', 10, 3)).toBe(0)
    expect(frameCapturePosition('last', 10, 3)).toBeLessThan(10)
    expect(frameCapturePosition('last', 10, 3)).toBeGreaterThan(9.98)
    expect(frameCapturePosition('current', 10, -1)).toBe(0)
    expect(() => frameCapturePosition('first', NaN, 0)).toThrow('尚未加载')
  })

  it.each(['first', 'current', 'last'] as const)('captures decoded %s pixels and restores the previous playback position/state', async source => {
    const { video, captured, play } = movie(true)
    const frame = await captureVideoFrame(video, source)
    expect(frame.source).toBe(source)
    expect(frame.width).toBe(1920); expect(frame.height).toBe(1080)
    expect(frame.blob.size).toBeGreaterThan(0)
    expect(frame.timestamp).toBe(captured[0])
    expect(captured[0]).toBeCloseTo(frameCapturePosition(source, 10, 3), 6)
    expect(video.currentTime).toBe(3)
    expect(play).toHaveBeenCalledOnce()
    expect(video.paused).toBe(false)
  })

  it('preserves a paused viewer after a capture', async () => {
    const { video, play } = movie()
    await captureVideoFrame(video, 'first')
    expect(video.currentTime).toBe(3)
    expect(play).not.toHaveBeenCalled()
  })

  it('reports CORS capture failure without losing playback position', async () => {
    const { video, play } = movie(true)
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(() => { throw new DOMException('tainted', 'SecurityError') })
    await expect(captureVideoFrame(video, 'first')).rejects.toThrow('跨域截帧')
    expect(video.currentTime).toBe(3)
    expect(play).toHaveBeenCalledOnce()
  })

  it('cancels before drawing and never starts playback on an aborted replacement', async () => {
    const { video, play, draw } = movie(true)
    const controller = new AbortController()
    controller.abort()
    await expect(captureVideoFrame(video, 'first', controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(draw).not.toHaveBeenCalled()
    expect(play).not.toHaveBeenCalled()
  })
})
