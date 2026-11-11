import { afterEach, describe, expect, it, vi } from 'vitest'
import { createStarfieldScheduler, generationTideGeometry, generationTideSurface, mountGenerationStarfield, STARFIELD_LIMITS, type StarfieldFrame } from '../src/generationStarfield'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

function harness() {
  let id = 0
  let wallClock = 10_000
  let hidden = false
  let visibility: (() => void) | undefined
  const frames = new Map<number, FrameRequestCallback>()
  const timers = new Map<number, () => void>()
  const request = vi.fn((callback: FrameRequestCallback) => { frames.set(++id, callback); return id })
  const cancel = vi.fn((key: number) => { frames.delete(key) })
  const unlisten = vi.fn()
  const scheduler = createStarfieldScheduler({
    request, cancel, now: () => wallClock, hidden: () => hidden,
    listen: callback => { visibility = callback; return unlisten },
    delay: callback => { timers.set(++id, callback); return id },
    clearDelay: key => { timers.delete(key) },
  })
  return {
    scheduler, frames, timers, request, cancel, unlisten,
    step(timestamp: number) {
      wallClock = 10_000 + timestamp
      const pending = [...frames.values()]
      frames.clear()
      pending.forEach(callback => callback(timestamp))
    },
    timer() { wallClock += 1000; const pending = [...timers.values()]; timers.clear(); pending.forEach(callback => callback()) },
    setWallClock(value: number) { wallClock = value },
    frame(timestamp: number) { const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback(timestamp)) },
    setHidden(value: boolean) { hidden = value; visibility?.() },
  }
}

describe('shared generation starfield scheduler', () => {
  it('uses one RAF for many cards and caps the combined particle budget', () => {
    const test = harness()
    const rendered: StarfieldFrame[] = []
    const cards = Array.from({ length: 32 }, () => test.scheduler.subscribe(frame => rendered.push(frame)))
    expect(test.frames.size).toBe(1)
    expect(test.request).toHaveBeenCalledTimes(1)
    test.step(0)
    expect(rendered).toHaveLength(32)
    expect(rendered.reduce((total, frame) => total + frame.particles, 0)).toBeLessThanOrEqual(STARFIELD_LIMITS.total)
    expect(test.frames.size).toBe(1)
    cards.forEach(card => card.destroy())
    expect(test.frames.size).toBe(0)
    expect(test.unlisten).toHaveBeenCalledTimes(1)
  })

  it('throttles drawing to 30fps and progress clocks to once per second', () => {
    const test = harness()
    const draw = vi.fn()
    const card = test.scheduler.subscribe(draw)
    test.step(0)
    test.step(16)
    expect(draw).toHaveBeenCalledTimes(1)
    test.step(34)
    expect(draw).toHaveBeenCalledTimes(2)
    expect(draw.mock.calls[1]![0].updateClock).toBe(false)
    test.step(1000)
    expect(draw.mock.calls[2]![0].updateClock).toBe(true)
    expect(draw.mock.calls[2]![0].delta).toBeLessThanOrEqual(.05)
    card.destroy()
  })

  it('stops all animation while hidden or paused and resumes with one frame', () => {
    const test = harness()
    const first = test.scheduler.subscribe(vi.fn())
    const second = test.scheduler.subscribe(vi.fn())
    test.setHidden(true)
    expect(test.frames.size).toBe(0)
    test.setHidden(false)
    expect(test.frames.size).toBe(1)
    first.setPaused(true)
    expect(test.frames.size).toBe(1)
    second.setPaused(true)
    expect(test.frames.size).toBe(0)
    first.setPaused(false)
    expect(test.frames.size).toBe(1)
    first.destroy()
    second.destroy()
    expect(test.frames.size).toBe(0)
  })

  it('uses one shared low-frequency text timer instead of RAF in reduced motion', () => {
    const test = harness()
    const draw = vi.fn()
    const first = test.scheduler.subscribe(draw, true, true)
    const second = test.scheduler.subscribe(draw, true, true)
    expect(test.frames.size).toBe(0)
    expect(test.timers.size).toBe(1)
    test.timer()
    expect(draw).toHaveBeenCalledTimes(2)
    expect(draw.mock.calls.every(([frame]) => frame.particles === 0 && frame.updateClock)).toBe(true)
    expect(test.timers.size).toBe(1)
    test.setHidden(true)
    expect(test.timers.size).toBe(0)
    test.setHidden(false)
    expect(test.timers.size).toBe(1)
    first.destroy()
    second.destroy()
    expect(test.timers.size).toBe(0)
  })

  it('keeps text clocks responsive after the system clock moves backwards', () => {
    const test = harness()
    const animated = vi.fn(), reduced = vi.fn()
    const first = test.scheduler.subscribe(animated)
    const second = test.scheduler.subscribe(reduced, true, true)
    test.setWallClock(100_000); test.frame(0)
    test.setWallClock(40_000); test.frame(40)
    expect(animated.mock.calls[1]![0]).toMatchObject({ now: 40_000, updateClock: true })
    expect(reduced.mock.calls[1]![0]).toMatchObject({ now: 40_000, updateClock: true })
    first.destroy(); second.destroy()
  })
})

describe('star sea tide progress', () => {
  it('maps percent to a rising bounded surface without inventing unknown progress', () => {
    const empty = generationTideGeometry(200, 0), full = generationTideGeometry(200, 100)
    expect(empty).toMatchObject({ level: 0, surfaceY: 200, amplitude: 0 })
    expect(full).toMatchObject({ level: 100, surfaceY: 0, amplitude: 0 })
    expect(generationTideGeometry(200, 20).surfaceY).toBe(160)
    expect(generationTideGeometry(200, 80).surfaceY).toBeCloseTo(40)
    const unknown = generationTideGeometry(200, null)
    expect(unknown.level).toBeNull()
    expect(unknown.surfaceY).toBe(180)
    expect(generationTideGeometry(200, -50)).toEqual(empty)
    expect(generationTideGeometry(200, 200)).toEqual(full)
    expect(generationTideGeometry(200, NaN)).toEqual(unknown)
  })

  it('keeps both waves inside the frame at extreme progress and tiny card sizes', () => {
    for (const height of [0, 1, 16, 150, 500]) for (const progress of [0, .1, 10, 50, 99.9, 100]) {
      const tide = generationTideGeometry(height, progress)
      for (let frame = 0; frame < 30; frame++) for (const layer of [0, 1]) {
        const y = generationTideSurface(frame / 30, tide, frame * .2, layer)
        expect(y).toBeGreaterThanOrEqual(0)
        expect(y).toBeLessThanOrEqual(height)
      }
    }
  })

  it('updates actual canvas wave geometry while paused without starting an animation', () => {
    vi.stubGlobal('CanvasRenderingContext2D', class {})
    const gradient = { addColorStop: vi.fn() }
    const context = { createRadialGradient: vi.fn(() => gradient), createLinearGradient: vi.fn(() => gradient),
      clearRect: vi.fn(), setTransform: vi.fn(), fillRect: vi.fn(), drawImage: vi.fn(),
      beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), closePath: vi.fn(), clip: vi.fn(),
      save: vi.fn(), restore: vi.fn(), stroke: vi.fn(), globalAlpha: 1 }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context as unknown as CanvasRenderingContext2D)
    const request = vi.spyOn(window, 'requestAnimationFrame')
    const canvas = document.createElement('canvas')
    const effect = mountGenerationStarfield(canvas, { kind: 'image', level: 20, paused: true, onClock: vi.fn() })
    expect(context.moveTo.mock.calls.some(([, y]) => y === 120)).toBe(true)
    context.moveTo.mockClear()
    effect.setLevel(80)
    expect(context.moveTo.mock.calls.some(([, y]) => Math.abs(y - 30) < .001)).toBe(true)
    expect(request).not.toHaveBeenCalled()
    context.moveTo.mockClear()
    effect.setLevel(0)
    expect(context.moveTo).not.toHaveBeenCalled()
    effect.destroy()
  })
})
