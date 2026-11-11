/** Source media time stays exact; these helpers never quantize generation time. */
export type FrameCaptureSource = 'first' | 'current' | 'last'
export interface CapturedVideoFrame {
  blob: Blob
  width: number
  height: number
  timestamp: number
  source: FrameCaptureSource
}

export function formatMediaTime(seconds: number, fractional = false): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const minutes = Math.floor(seconds / 60)
  const remaining = Math.floor(seconds % 60)
  return `${minutes}:${String(remaining).padStart(2, '0')}${fractional ? '.' + String(Math.floor((seconds % 1) * 100)).padStart(2, '0') : ''}`
}

export function normalizeVideoCuts(cuts: number[], duration: number): number[] {
  if (!Number.isFinite(duration) || duration <= 0) return []
  return [...new Set(cuts.filter(value => typeof value === 'number' && Number.isFinite(value) && value > 0 && value < duration))].sort((a, b) => a - b)
}

export function frameCapturePosition(source: FrameCaptureSource, duration: number, currentTime: number): number {
  if (!Number.isFinite(duration) || duration <= 0) throw new Error('视频尚未加载完成，请稍候重试。')
  // Stay inside the last decoded frame for normal 24/30/60fps assets; seeking
  // exactly to duration can display an ended/black surface in Safari.
  const last = Math.max(0, duration - Math.min(1 / 120, duration / 4))
  return source === 'first' ? 0 : source === 'last' ? last : Math.max(0, Math.min(Number.isFinite(currentTime) ? currentTime : 0, last))
}

function cancelled(): Error { return new DOMException('截帧已取消', 'AbortError') }

function waitForMedia(video: HTMLVideoElement, event: string, valid: () => boolean, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(cancelled())
  if (valid()) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const events = [...new Set([event, 'loadeddata', 'canplay'])]
    const clear = () => { clearTimeout(timer); for (const name of events) video.removeEventListener(name, finish); video.removeEventListener('error', fail); signal?.removeEventListener('abort', abort) }
    const finish = () => { if (valid()) { clear(); resolve() } }
    const fail = () => { clear(); reject(new Error('视频无法解码，不能截取画面。')) }
    const abort = () => { clear(); reject(cancelled()) }
    const timer = setTimeout(() => { clear(); reject(new Error('等待视频画面超时，请稍后重试。')) }, 8000)
    for (const name of events) video.addEventListener(name, finish)
    video.addEventListener('error', fail)
    signal?.addEventListener('abort', abort, { once: true })
  })
}

async function seekVideo(video: HTMLVideoElement, target: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) throw cancelled()
  if (Math.abs(video.currentTime - target) < .00001 && !video.seeking && video.readyState >= 2) return
  // Register before assigning currentTime: cached/local videos may seek quickly.
  const settled = waitForMedia(video, 'seeked', () => !video.seeking && Math.abs(video.currentTime - target) < .02 && video.readyState >= 2, signal)
  video.currentTime = target
  await settled
}

async function decodedVideoFrame(video: HTMLVideoElement, signal?: AbortSignal): Promise<number> {
  if (signal?.aborted) throw cancelled()
  if (typeof video.requestVideoFrameCallback !== 'function') {
    await waitForMedia(video, 'loadeddata', () => video.readyState >= 2, signal)
    return video.currentTime
  }
  return new Promise((resolve, reject) => {
    let callback = 0
    const clear = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); if (callback) video.cancelVideoFrameCallback?.(callback) }
    const abort = () => { clear(); reject(cancelled()) }
    const timer = setTimeout(() => {
      clear()
      // A paused seek can already have presented its frame before registration.
      video.readyState >= 2 && !video.seeking ? resolve(video.currentTime) : reject(new Error('当前视频画面尚未就绪。'))
    }, 200)
    signal?.addEventListener('abort', abort, { once: true })
    callback = video.requestVideoFrameCallback((_now, metadata) => { clear(); resolve(metadata.mediaTime) })
  })
}

function canvasBlob(canvas: HTMLCanvasElement, signal?: AbortSignal): Promise<Blob> {
  if (signal?.aborted) return Promise.reject(cancelled())
  return new Promise((resolve, reject) => {
    const clear = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
    const abort = () => { clear(); reject(cancelled()) }
    const timer = setTimeout(() => { clear(); reject(new Error('图片保存超时，请重试。')) }, 8000)
    signal?.addEventListener('abort', abort, { once: true })
    try {
      canvas.toBlob(blob => { clear(); if (signal?.aborted) reject(cancelled()); else if (blob?.size) resolve(blob); else reject(new Error('当前画面无法保存为图片。')) }, 'image/png')
    } catch (error) {
      clear()
      reject(error instanceof DOMException && error.name === 'SecurityError'
        ? new Error('该视频没有允许跨域截帧，请使用项目中的视频资产。') : error)
    }
  })
}

/** Capture real decoded pixels, then restore playback; never fabricates assets. */
export async function captureVideoFrame(video: HTMLVideoElement, source: FrameCaptureSource, signal?: AbortSignal): Promise<CapturedVideoFrame> {
  await waitForMedia(video, 'loadedmetadata', () => Number.isFinite(video.duration) && video.duration > 0 && video.videoWidth > 0 && video.videoHeight > 0, signal)
  const previous = video.currentTime
  const wasPlaying = !video.paused && !video.ended
  const canvas = document.createElement('canvas')
  video.pause()
  try {
    const target = frameCapturePosition(source, video.duration, previous)
    await seekVideo(video, target, signal)
    const timestamp = await decodedVideoFrame(video, signal)
    const width = video.videoWidth, height = video.videoHeight
    if (!width || !height || width * height > 24_000_000) throw new Error('视频画面尺寸超出当前截帧范围。')
    canvas.width = width; canvas.height = height
    const context = canvas.getContext('2d', { alpha: false })
    if (!context) throw new Error('浏览器无法创建图片画布。')
    context.drawImage(video, 0, 0, width, height)
    return { blob: await canvasBlob(canvas, signal), width, height, timestamp, source }
  } finally {
    canvas.width = 0; canvas.height = 0
    if (!signal?.aborted) {
      try { await seekVideo(video, previous, signal) } catch { /* Keep the real capture even if playback restoration fails. */ }
      if (wasPlaying && !signal?.aborted) await video.play().catch(() => undefined)
    }
  }
}
