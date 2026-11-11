import { MAX_CANVAS_ZOOM, MIN_CANVAS_ZOOM, type CanvasViewport } from './composables/useCanvasGestures'

interface Size { width: number; height: number }
interface Rectangle { left: number; top: number; right: number; bottom: number }
interface LowerLeftCorner { right: number; top: number }

function fit(frame: Size, rect: Rectangle) {
  const scale = Math.min((rect.right - rect.left) / frame.width, (rect.bottom - rect.top) / frame.height)
  return Math.min(MAX_CANVAS_ZOOM, scale)
}

/** Fit the whole media inside the canvas, above or beside its bottom-left minimap. */
export function frameMediaViewport(position: { x: number; y: number }, frame: Size, canvas: Size, corner?: LowerLeftCorner): CanvasViewport | null {
  if (![frame.width, frame.height, canvas.width, canvas.height].every(value => Number.isFinite(value) && value > 0)
    || !Number.isFinite(position.x) || !Number.isFinite(position.y)) return null
  const side = Math.min(48, canvas.width * .035)
  const top = Math.min(64, canvas.height * .17)
  // Includes the collapsed minimap button and the centered canvas toolbar.
  const bottom = canvas.height - Math.min(canvas.width < 540 ? 112 : 80, canvas.height * .25)
  const available = { left: side, right: canvas.width - side, top, bottom }
  const center = { x: canvas.width / 2, y: (top + bottom) / 2 }
  let zoom = fit(frame, available)
  let region = available
  if (corner && center.x - frame.width * zoom / 2 < corner.right + 12
    && center.y + frame.height * zoom / 2 > corner.top - 12) {
    const above = { ...available, bottom: Math.min(bottom, corner.top - 12) }
    const beside = { ...available, left: Math.max(side, corner.right + 12) }
    region = fit(frame, above) > fit(frame, beside) ? above : beside
    zoom = fit(frame, region)
  }
  zoom = Math.max(MIN_CANVAS_ZOOM, zoom)
  const width = frame.width * zoom, height = frame.height * zoom
  const left = Math.max(region.left, Math.min(center.x - width / 2, region.right - width))
  const y = Math.max(region.top, Math.min(center.y - height / 2, region.bottom - height))
  return { x: left - position.x * zoom, y: y - position.y * zoom, zoom }
}
