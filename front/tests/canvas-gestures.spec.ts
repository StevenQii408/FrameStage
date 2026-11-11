import { defineComponent, h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { canvasGestureSurface, canvasWheelPixels, canvasWheelViewport, useCanvasGestures, zoomCanvasAt, type CanvasViewport } from '../src/composables/useCanvasGestures'

const bounds = { left: 100, top: 50, width: 800, height: 600 }
const start: CanvasViewport = { x: 20, y: -10, zoom: .8 }
const wheelInput = { deltaX: 0, deltaY: 100, deltaMode: 0, clientX: 500, clientY: 350 }
const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks() })

function harness(nativeGestures = true) {
  const canvas = ref<HTMLElement>(), viewport = ref({ ...start }), blocked = ref(false)
  let time = 0
  const setViewport = vi.fn((next: CanvasViewport) => { viewport.value = next })
  const wrapper = mount(defineComponent({ setup() {
    useCanvasGestures({ canvasWrap: canvas, getViewport: () => viewport.value, setViewport, blocked: () => blocked.value, nativeGestures, now: () => time })
    return () => h('div', { ref: canvas }, [h('div', { class: 'vue-flow' }, [h('div', { class: 'vue-flow__pane' }),
      h('textarea'), h('input'), h('select'), h('div', { class: 'nowheel' }, [h('span')]),
      h('div', { role: 'dialog' }, [h('span')]), h('div', { class: 'tactical-minimap' }, [h('svg')]),
      h('div', { class: 'vue-flow__panel' }), h('div', { class: 'vue-flow__handle' }), h('div', { class: 'toolbar-actions' }),
    ])])
  } }))
  wrappers.push(wrapper)
  const flow = wrapper.get('.vue-flow').element as HTMLElement
  Object.defineProperty(flow, 'getBoundingClientRect', { value: () => new DOMRect(bounds.left, bounds.top, bounds.width, bounds.height) })
  const pane = wrapper.get('.vue-flow__pane').element
  function wheel(extra: Partial<WheelEventInit> = {}, target: Element = pane) {
    const event = new WheelEvent('wheel', { ...wheelInput, bubbles: true, cancelable: true, ...extra })
    target.dispatchEvent(event)
    return event
  }
  function gesture(type: string, scale = 1, target: Element = pane) {
    const event = Object.assign(new Event(type, { bubbles: true, cancelable: true }), { scale, clientX: 500, clientY: 350 })
    target.dispatchEvent(event)
    return event
  }
  return { wrapper, canvas, viewport, blocked, setViewport, flow, pane, wheel, gesture, clock: (next: number) => { time = next } }
}

describe('cross-platform canvas wheel geometry', () => {
  it('uses deltaMode strictly for CSS pixel, line and page units', () => {
    expect(canvasWheelPixels({ deltaX: 2, deltaY: 3, deltaMode: 0 }, bounds)).toEqual({ x: 2, y: 3 })
    expect(canvasWheelPixels({ deltaX: 2, deltaY: 3, deltaMode: 1 }, bounds)).toEqual({ x: 32, y: 48 })
    expect(canvasWheelPixels({ deltaX: 2, deltaY: 3, deltaMode: 2 }, bounds)).toEqual({ x: 1600, y: 1800 })
  })
  it('pans every ordinary wheel event without changing zoom, including horizontal trackpad movement', () => {
    expect(canvasWheelViewport(start, { ...wheelInput, deltaX: 40 }, bounds)).toEqual({ x: -20, y: -110, zoom: .8 })
    expect(canvasWheelViewport(start, { ...wheelInput, shiftKey: true }, bounds)).toEqual({ x: -80, y: -10, zoom: .8 })
  })
  it.each(['ctrlKey', 'metaKey'] as const)('zooms %s wheel around the pointer without an OS branch', key => {
    const next = canvasWheelViewport(start, { ...wheelInput, [key]: true }, bounds)!
    expect(next.zoom).toBeLessThan(start.zoom)
    expect((400 - next.x) / next.zoom).toBeCloseTo((400 - start.x) / start.zoom, 10)
    expect((300 - next.y) / next.zoom).toBeCloseTo((300 - start.y) / start.zoom, 10)
  })
  it('clamps zoom, retains the anchor at bounds, and rejects corrupt numeric inputs', () => {
    expect(zoomCanvasAt(start, { x: 400, y: 300 }, 100)?.zoom).toBe(1.6)
    expect(zoomCanvasAt(start, { x: 400, y: 300 }, .001)?.zoom).toBe(.25)
    expect(canvasWheelViewport(start, { ...wheelInput, deltaY: NaN }, bounds)).toBeNull()
    expect(zoomCanvasAt({ ...start, zoom: 0 }, { x: 400, y: 300 }, 1)).toBeNull()
  })
})

describe('canvas gesture ownership and lifecycle', () => {
  it('consumes eligible wheel once before Vue Flow while preserving ordinary pan', async () => {
    const view = harness()
    await nextTick()
    const downstream = vi.fn()
    view.flow.addEventListener('wheel', downstream)
    expect(view.wheel({ deltaX: 5, deltaY: 10 }).defaultPrevented).toBe(true)
    expect(view.viewport.value).toEqual({ x: 15, y: -20, zoom: .8 })
    expect(downstream).not.toHaveBeenCalled()
    view.wheel({ ctrlKey: true, deltaY: -10 })
    expect(view.viewport.value.zoom).toBeGreaterThan(.8)
    expect(view.setViewport).toHaveBeenCalledTimes(2)
  })
  it('leaves local scroll controls, minimap, overlays and inputs untouched', async () => {
    const view = harness()
    await nextTick()
    for (const selector of ['textarea', 'input', 'select', '.nowheel span', '[role="dialog"] span', '.tactical-minimap svg', '.vue-flow__panel', '.vue-flow__handle', '.toolbar-actions']) {
      const target = view.wrapper.get(selector).element
      expect(canvasGestureSurface(target, view.canvas.value!)).toBeNull()
      expect(view.wheel({ ctrlKey: true }, target).defaultPrevented).toBe(false)
      expect(view.gesture('gesturestart', 1, target).defaultPrevented).toBe(false)
    }
    view.blocked.value = true
    expect(view.wheel().defaultPrevented).toBe(false)
    expect(view.setViewport).not.toHaveBeenCalled()
  })
  it('uses native Safari scale relative to gesture start and suppresses duplicate wheel pinch', async () => {
    const view = harness()
    await nextTick()
    expect(view.gesture('gesturestart').defaultPrevented).toBe(true)
    view.gesture('gesturechange', 1.5)
    expect(view.viewport.value.zoom).toBeCloseTo(1.2)
    expect(view.setViewport).toHaveBeenCalledTimes(1)
    view.wheel({ ctrlKey: true, deltaY: -50 })
    view.wheel({ deltaX: 10, deltaY: -5 })
    expect(view.viewport.value.zoom).toBeCloseTo(1.2)
    expect(view.setViewport).toHaveBeenCalledTimes(1)
    view.gesture('gesturechange', 1.75)
    expect(view.viewport.value.zoom).toBeCloseTo(1.4)
    view.gesture('gestureend', 1.75)
    view.clock(50)
    view.wheel({ ctrlKey: true, deltaY: -50 })
    expect(view.viewport.value.zoom).toBeCloseTo(1.4)
    view.clock(150)
    view.wheel({ ctrlKey: true, deltaY: -10 })
    expect(view.viewport.value.zoom).toBeGreaterThan(1.4)
  })
  it('keeps wheel ownership if a browser supplies both streams in the opposite order', async () => {
    const view = harness()
    await nextTick()
    view.wheel({ ctrlKey: true, deltaY: -10 })
    const once = view.viewport.value.zoom
    view.clock(10)
    view.gesture('gesturestart')
    view.gesture('gesturechange', 2)
    expect(view.viewport.value.zoom).toBe(once)
    view.wheel({ ctrlKey: true, deltaY: -10 })
    expect(view.viewport.value.zoom).toBeGreaterThan(once)
    expect(view.setViewport).toHaveBeenCalledTimes(2)
  })
  it('recovers from a missing native gesture end and clears ownership on blur', async () => {
    const view = harness()
    await nextTick()
    view.gesture('gesturestart')
    view.clock(800)
    view.wheel({ metaKey: true, deltaY: -10 })
    expect(view.setViewport).toHaveBeenCalledOnce()
    view.gesture('gesturestart')
    window.dispatchEvent(new Event('blur'))
    view.wheel({ metaKey: true, deltaY: -10 })
    expect(view.setViewport).toHaveBeenCalledTimes(2)
  })
  it('only installs native gesture fallback where available and removes all handlers on canvas replacement/unmount', async () => {
    const view = harness(false)
    await nextTick()
    expect(view.gesture('gesturestart').defaultPrevented).toBe(false)
    view.wheel({ ctrlKey: true, deltaY: -10 })
    expect(view.setViewport).toHaveBeenCalledOnce()
    view.canvas.value = undefined
    await nextTick()
    expect(view.wheel().defaultPrevented).toBe(false)
    expect(view.setViewport).toHaveBeenCalledOnce()
    view.wrapper.unmount()
    expect(view.wheel({ metaKey: true }).defaultPrevented).toBe(false)
    expect(view.setViewport).toHaveBeenCalledOnce()
  })
})
