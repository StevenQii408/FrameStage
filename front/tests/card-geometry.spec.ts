import { describe, expect, it } from 'vitest'
import { estimateCardSize, sceneCoverSize } from '../src/cardGeometry'
import type { Shot } from '../src/types'

const shot = (width: number, height: number) => ({ thumb: '/frame.png', frames: [{ width, height }] }) as Shot

describe('semantic card geometry', () => {
  it('bounds portrait and ultra-wide scene covers without distorting or cropping them', () => {
    for (const lod of [true, false]) for (const [width, height] of [[768, 1366], [2400, 600]]) {
      const size = sceneCoverSize(shot(width, height), lod)
      expect(size.width).toBeLessThanOrEqual(lod ? 160 : 184)
      expect(size.height).toBeLessThanOrEqual(lod ? 90 : 138)
      expect(size.width / size.height).toBeCloseTo(width / height, 6)
    }
  })
  it('does not assign detailed portrait height to overview thumbnails', () => {
    const portrait = shot(768, 1366)
    const overview = estimateCardSize(portrait, 'overview')
    expect(overview.width).toBe(180)
    expect(overview.height).toBeLessThan(220)
    expect(estimateCardSize(portrait, 'detail').height).toBeGreaterThan(550)
  })
  it('preserves aspect ratio for full cards and handles missing frame metadata', () => {
    expect(estimateCardSize(shot(1920, 600), 'strip').height).toBeLessThan(estimateCardSize(shot(600, 1920), 'strip').height)
    const description = { frames: [], thumb: '' } as unknown as Shot
    expect(estimateCardSize(description, 'overview')).toEqual({ width: 180, height: 138 })
    expect(Number.isFinite(estimateCardSize({ frames: [], thumb: '/unknown.png' } as unknown as Shot, 'detail').height)).toBe(true)
  })
})
