import { describe, expect, it } from 'vitest'
import { generatorPreviewSize } from '../src/generatorPreviewGeometry'

describe('transient generator preview geometry', () => {
  it.each([[1920, 1080], [1080, 1920], [3200, 600], [1000, 1000]])('fits %s×%s media without cropping or changing its proportions', (width, height) => {
    const viewport = { width: 1440, height: 1000 }
    const preview = generatorPreviewSize({ width, height }, viewport)
    expect(preview.width / preview.height).toBeCloseTo(width / height)
    expect(preview.width).toBeLessThanOrEqual(1280.00001)
    expect(preview.height).toBeLessThanOrEqual(860.00001)
    expect(Math.max(preview.width, preview.height)).toBeGreaterThan(268)
  })

  it('keeps the same visible extent at a zoomed-out canvas without mutating media dimensions', () => {
    const media = { width: 1920, height: 1080 }, viewport = { width: 1440, height: 1000 }
    const one = generatorPreviewSize(media, viewport, 1)
    const half = generatorPreviewSize(media, viewport, .5)
    expect(half.width * .5).toBeCloseTo(one.width)
    expect(half.height * .5).toBeCloseTo(one.height)
    expect(media).toEqual({ width: 1920, height: 1080 })
  })

  it('produces finite dimensions for missing metadata and invalid zoom', () => {
    const size = generatorPreviewSize({ width: 0, height: Number.NaN }, { width: 0, height: 0 }, 0)
    expect(size.width).toBeGreaterThan(0)
    expect(size.height).toBeGreaterThan(0)
    expect(Number.isFinite(size.width)).toBe(true)
    expect(size.width / size.height).toBeCloseTo(16 / 9)
  })
})
