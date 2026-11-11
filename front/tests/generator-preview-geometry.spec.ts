import { describe, expect, it } from 'vitest'
import { frameMediaViewport } from '../src/generatorPreviewGeometry'

describe('canvas focus on media', () => {
  it.each([[268, 151], [151, 268], [268, 50], [268, 268]])('fits a %s×%s node above the player toolbar', (width, height) => {
    for (const canvas of [{ width: 1440, height: 900 }, { width: 390, height: 650 }]) {
      const origin = { x: -512, y: 199 }
      const view = frameMediaViewport(origin, { width, height }, canvas)!
      const left = view.x + origin.x * view.zoom
      const top = view.y + origin.y * view.zoom
      expect(left).toBeGreaterThan(0)
      expect(left + width * view.zoom).toBeLessThan(canvas.width)
      expect(left + width * view.zoom / 2).toBeCloseTo(canvas.width / 2)
      expect(top).toBeGreaterThan(54)
      expect(top + height * view.zoom).toBeLessThan(canvas.height - 72)
    }
  })

  it.each([
    { canvas: { width: 1200, height: 720 }, map: { right: 240, top: 450 }, side: 'right' },
    { canvas: { width: 390, height: 650 }, map: { right: 202, top: 375 }, side: 'above' },
  ])('uses the larger available region with the map open on a $canvas.width px canvas', ({ canvas, map, side }) => {
    const frame = { width: 268, height: 151 }
    const view = frameMediaViewport({ x: 0, y: 0 }, frame, canvas, map)!
    if (side === 'right') expect(view.x).toBeGreaterThanOrEqual(map.right + 12)
    else expect(view.y + frame.height * view.zoom).toBeLessThanOrEqual(map.top - 12 + .001)
    expect(view.x + frame.width * view.zoom).toBeLessThan(canvas.width)
  })

  it('keeps a centered portrait when the map and media already clear each other', () => {
    const frame = { width: 140, height: 268 }, origin = { x: 230, y: -80 }, canvas = { width: 1400, height: 900 }
    expect(frameMediaViewport(origin, frame, canvas, { right: 240, top: 590 }))
      .toEqual(frameMediaViewport(origin, frame, canvas))
  })

  it('waits for measured geometry and bounds extremely large screens', () => {
    const point = { x: 40, y: 20 }, frame = { width: 268, height: 151 }
    expect(frameMediaViewport(point, frame, { width: 0, height: 0 })).toBeNull()
    expect(frameMediaViewport(point, { width: NaN, height: 50 }, frame)).toBeNull()
    expect(frameMediaViewport(point, frame, { width: 10000, height: 6000 })!.zoom).toBe(8)
  })
})
