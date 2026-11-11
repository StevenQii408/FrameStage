import { describe, expect, it } from 'vitest'
import { minimapGeometry, minimapNodeKind, minimapNodeRect, minimapNodeSize } from '../src/minimapGeometry'

describe('tactical minimap projection', () => {
  it('classifies input, storyboard, image and video operations independently', () => {
    expect(minimapNodeKind({ type: 'project', data: {} })).toBe('input')
    expect(minimapNodeKind({ type: 'shot' })).toBe('shot')
    expect(minimapNodeKind({ type: 'sequence' })).toBe('shot')
    expect(minimapNodeKind({ type: 'generator', data: { generator: { kind: 'image' } } })).toBe('image-generation')
    expect(minimapNodeKind({ type: 'generator', data: { generator: { kind: 'video' } } })).toBe('video-generation')
    expect(minimapNodeKind({ type: 'scene' })).toBe('scene')
    expect(minimapNodeKind({ type: 'prompt' })).toBe('prompt')
    expect(minimapNodeKind({ type: 'eventPrompt' })).toBe('event-prompt')
  })
  it('keeps distant nodes and the current viewport in the map', () => {
    const geometry = minimapGeometry([{ position: { x: 0, y: 0 }, width: 280, height: 300 }, { position: { x: 4000, y: 2400 }, width: 250, height: 250 }], { x: -300, y: -200, zoom: .5 }, { width: 1200, height: 800 }, { width: 204, height: 112 })
    expect(geometry.x(0)).toBeGreaterThan(0)
    expect(geometry.x(4250)).toBeLessThan(204)
    expect(geometry.y(2650)).toBeLessThan(112)
    expect(geometry.view.width).toBeGreaterThan(0)
  })
  it('round-trips minimap pointer coordinates to flow coordinates', () => {
    const geometry = minimapGeometry([], { x: -100, y: 40, zoom: 1 }, { width: 1000, height: 600 }, { width: 204, height: 112 })
    const point = geometry.invert(geometry.x(120), geometry.y(350))
    expect(point.x).toBeCloseTo(120)
    expect(point.y).toBeCloseTo(350)
  })

  it.each([.25, .5, 1.6])('maps node rectangles uniformly without width/height distortion at zoom %s', zoom => {
    const node = { position: { x: 4200, y: 2300 }, width: 280, height: 600 }
    const geometry = minimapGeometry([node], { x: -200, y: -100, zoom }, { width: 1200, height: 800 }, { width: 204, height: 112 })
    const rect = minimapNodeRect(node, geometry)
    expect(rect.width / rect.height).toBeCloseTo(280 / 600)
    expect(rect.width).toBeCloseTo(280 * geometry.scale)
    expect(rect.height).toBeCloseTo(600 * geometry.scale)
    const point = geometry.invert(rect.x + rect.width / 2, rect.y + rect.height / 2)
    expect(point.x).toBeCloseTo(4340)
    expect(point.y).toBeCloseTo(2600)
  })

  it('preserves extremely wide and narrow rectangles without minimum-symbol clamps', () => {
    const projection = { x: (value: number) => value * .001, y: (value: number) => value * .001, scale: .001 }
    const wide = minimapNodeRect({ position: { x: 0, y: 0 }, width: 1600, height: 100 }, projection)
    const narrow = minimapNodeRect({ position: { x: 100, y: 100 }, width: 100, height: 1600 }, projection)
    expect(wide).toMatchObject({ width: 1.6, height: .1 })
    expect(narrow).toMatchObject({ width: .1, height: 1.6 })
  })

  it('uses measured node sizes, then layout metadata or explicit dimensions with safe fallbacks', () => {
    expect(minimapNodeSize({ dimensions: { width: 320, height: 180 }, data: { layoutWidth: 200, layoutHeight: 300 } })).toEqual({ width: 320, height: 180 })
    expect(minimapNodeSize({ dimensions: { width: 0, height: NaN }, data: { layoutWidth: 150, layoutHeight: 268 } })).toEqual({ width: 150, height: 268 })
    expect(minimapNodeSize({ width: '280', height: 400 })).toEqual({ width: 280, height: 400 })
    expect(minimapNodeSize({ data: { layoutWidth: -1, layoutHeight: Infinity } })).toEqual({ width: 230, height: 180 })
  })
})
