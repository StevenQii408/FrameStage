import { onBeforeUnmount, watch, type Ref } from 'vue'

export interface CanvasViewport { x: number; y: number; zoom: number }
interface Point { x: number; y: number }
interface Bounds { left: number; top: number; width: number; height: number }
interface WheelInput {
  deltaX: number; deltaY: number; deltaMode: number; clientX: number; clientY: number
  ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean
}
interface NativeGesture extends Event { scale?: number; clientX?: number; clientY?: number }
interface CanvasGestureOptions {
  canvasWrap: Ref<HTMLElement | null | undefined>
  getViewport: () => CanvasViewport
  setViewport: (viewport: CanvasViewport) => void | Promise<unknown>
  blocked?: () => boolean
  /** Feature detection by default; the override also permits deterministic event tests. */
  nativeGestures?: boolean
  now?: () => number
}

const MIN_ZOOM = .25
const MAX_ZOOM = 1.6
const NON_CANVAS_SCROLL = [
  '.nowheel', 'input', 'textarea', 'select', '[contenteditable="true"]', 'video', 'audio',
  '[role="dialog"]', '[role="listbox"]', '[role="menu"]', '[role="toolbar"]',
  '[data-canvas-gesture-lock]', '[data-canvas-overlay]', '.generator-inline-panel', '.generator-prompt-modal-backdrop',
  '.tactical-minimap', '.vue-flow__minimap', '.vue-flow__controls', '.vue-flow__panel', '.vue-flow__handle',
  '.page-toolbar', '.toolbar-actions', '.chrome-controls', '.create-node-menu', '.movie-breadcrumb', '.canvas-legend',
].join(',')

function validViewport(view: CanvasViewport) {
  return [view.x, view.y, view.zoom].every(Number.isFinite) && view.zoom > 0
}

/** deltaMode is a unit, never a heuristic for mouse versus trackpad hardware. */
export function canvasWheelPixels(event: Pick<WheelInput, 'deltaX' | 'deltaY' | 'deltaMode'>, bounds: Pick<Bounds, 'width' | 'height'>): Point {
  const xUnit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.width : 1
  const yUnit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.height : 1
  return { x: event.deltaX * xUnit, y: event.deltaY * yUnit }
}

/** Keep the same world point under a canvas-local cursor when zoom changes. */
export function zoomCanvasAt(view: CanvasViewport, anchor: Point, requestedZoom: number): CanvasViewport | null {
  if (!validViewport(view) || ![anchor.x, anchor.y, requestedZoom].every(Number.isFinite) || requestedZoom <= 0) return null
  const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, requestedZoom))
  return { x: anchor.x - (anchor.x - view.x) / view.zoom * zoom, y: anchor.y - (anchor.y - view.y) / view.zoom * zoom, zoom }
}

/** Identical wheel behavior on macOS, Windows and Linux. */
export function canvasWheelViewport(view: CanvasViewport, event: WheelInput, bounds: Bounds): CanvasViewport | null {
  if (!validViewport(view)) return null
  const delta = canvasWheelPixels(event, bounds)
  if (![delta.x, delta.y, event.clientX, event.clientY].every(Number.isFinite)) return null
  if (event.ctrlKey || event.metaKey) {
    // Bound each multiplier; a malformed or very large wheel delta cannot overflow exp().
    const factor = Math.exp(Math.max(-1, Math.min(1, -delta.y * .002)))
    return zoomCanvasAt(view, { x: event.clientX - bounds.left, y: event.clientY - bounds.top }, view.zoom * factor)
  }
  if (event.shiftKey && !delta.x) { delta.x = delta.y; delta.y = 0 }
  return { x: view.x - delta.x, y: view.y - delta.y, zoom: view.zoom }
}

export function canvasGestureSurface(target: EventTarget | null, root: HTMLElement): HTMLElement | null {
  const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null
  if (!element || !root.contains(element) || element.closest(NON_CANVAS_SCROLL)) return null
  const flow = element.closest<HTMLElement>('.vue-flow')
  return flow && root.contains(flow) ? flow : null
}

/** Own wheel input once, ahead of D3; native Safari gestures are an exclusive fallback. */
export function useCanvasGestures(options: CanvasGestureOptions) {
  const now = options.now || (() => performance.now())
  let lastWheelZoomAt = -Infinity
  let suppressWheelUntil = -Infinity
  let native: { owner: 'wheel' | 'native'; base: CanvasViewport; scale: number; anchor: Point; flow: HTMLElement; updatedAt: number } | null = null
  let lastPointer: Point | null = null
  function reset() { native = null; lastPointer = null; lastWheelZoomAt = -Infinity; suppressWheelUntil = -Infinity }
  function consume(event: Event) { event.preventDefault(); event.stopImmediatePropagation() }
  function apply(view: CanvasViewport | null) {
    if (!view) return
    const current = options.getViewport()
    if (current.x === view.x && current.y === view.y && current.zoom === view.zoom) return
    const result = options.setViewport(view)
    if (result && typeof result.then === 'function') void result.catch(reset)
  }
  function point(event: NativeGesture, flow: HTMLElement): Point {
    const rect = flow.getBoundingClientRect()
    const inside = (value: Point) => value.x >= rect.left && value.x <= rect.right && value.y >= rect.top && value.y <= rect.bottom
    const given = { x: event.clientX!, y: event.clientY! }
    if (Number.isFinite(given.x) && Number.isFinite(given.y) && inside(given)) return given
    return lastPointer && inside(lastPointer) ? lastPointer : { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
  }
  const stop = watch(options.canvasWrap, (root, _previous, onCleanup) => {
    reset()
    if (!root) return
    const onWheel = (event: WheelEvent) => {
      if (event.defaultPrevented || options.blocked?.()) return
      const flow = canvasGestureSurface(event.target, root)
      if (!flow) return
      const time = now(), zooming = event.ctrlKey || event.metaKey
      if (native && time - native.updatedAt > 750) native = null
      consume(event)
      if (native?.owner === 'native') return
      if (zooming) {
        // Some WebKit versions emit both streams. The first stream owns this pinch.
        if (time < suppressWheelUntil) return
        lastWheelZoomAt = time
      }
      apply(canvasWheelViewport(options.getViewport(), event, flow.getBoundingClientRect()))
    }
    const onPointer = (event: PointerEvent) => {
      if (canvasGestureSurface(event.target, root)) lastPointer = { x: event.clientX, y: event.clientY }
    }
    const onStart = (event: NativeGesture) => {
      if (event.defaultPrevented || options.blocked?.()) return
      const flow = canvasGestureSurface(event.target, root)
      if (!flow) return
      const scale = event.scale ?? 1
      if (!Number.isFinite(scale) || scale <= 0) return
      consume(event)
      const time = now()
      native = { owner: time - lastWheelZoomAt < 160 ? 'wheel' : 'native', base: { ...options.getViewport() }, scale,
        anchor: point(event, flow), flow, updatedAt: time }
    }
    const onChange = (event: NativeGesture) => {
      if (!native) return
      if (options.blocked?.()) { reset(); return }
      consume(event)
      native.updatedAt = now()
      if (native.owner !== 'native' || !Number.isFinite(event.scale) || event.scale! <= 0) return
      const rect = native.flow.getBoundingClientRect()
      apply(zoomCanvasAt(native.base, { x: native.anchor.x - rect.left, y: native.anchor.y - rect.top }, native.base.zoom * event.scale! / native.scale))
    }
    const onEnd = (event: Event) => {
      if (!native) return
      consume(event)
      if (native.owner === 'native') suppressWheelUntil = now() + 100
      native = null
    }
    const visibility = () => { if (document.hidden) reset() }
    const nativeSupported = options.nativeGestures ?? ('ongesturestart' in window || typeof (window as unknown as { GestureEvent?: unknown }).GestureEvent === 'function')
    root.addEventListener('wheel', onWheel, { capture: true, passive: false })
    root.addEventListener('pointermove', onPointer, { capture: true, passive: true })
    if (nativeSupported) {
      root.addEventListener('gesturestart', onStart, { capture: true, passive: false })
      root.addEventListener('gesturechange', onChange, { capture: true, passive: false })
      root.addEventListener('gestureend', onEnd, { capture: true, passive: false })
    }
    window.addEventListener('blur', reset)
    document.addEventListener('visibilitychange', visibility)
    onCleanup(() => {
      root.removeEventListener('wheel', onWheel, true)
      root.removeEventListener('pointermove', onPointer, true)
      root.removeEventListener('gesturestart', onStart, true)
      root.removeEventListener('gesturechange', onChange, true)
      root.removeEventListener('gestureend', onEnd, true)
      window.removeEventListener('blur', reset)
      document.removeEventListener('visibilitychange', visibility)
      reset()
    })
  }, { immediate: true, flush: 'post' })
  onBeforeUnmount(stop)
  return { resetGesture: reset }
}
