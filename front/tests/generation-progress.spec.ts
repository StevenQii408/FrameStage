import { describe, expect, it } from 'vitest'
import { generationDisplayProgress } from '../src/generationProgress'

const now = Date.parse('2026-11-11T04:00:00Z')
const input = { kind: 'image' as const, status: 'running' as const, progress: null, updatedAt: new Date(now - 30_000).toISOString() }

describe('generation presentation progress', () => {
  it('uses explicitly labeled time estimates without changing the cloud task', () => {
    const snapshot = JSON.stringify(input)
    const progress = generationDisplayProgress(input, now)
    expect(progress.estimated).toBe(true)
    expect(progress.value).toBeGreaterThan(5)
    expect(progress.value).toBeLessThan(95)
    expect(progress.text).toContain('预计')
    expect(JSON.stringify(input)).toBe(snapshot)
    expect(generationDisplayProgress(input, now)).toEqual(progress)
  })
  it('gives real progress precedence even when it is below the estimate', () => {
    const result = generationDisplayProgress({ ...input, progress: 12 }, now)
    expect(result.value).toBe(12)
    expect(result.estimated).toBe(false)
    expect(result.text).not.toContain('预计')
    expect(generationDisplayProgress({ ...input, progress: 0 }, now).value).toBe(0)
  })
  it('only reaches 100 after an actual success event', () => {
    for (const status of ['submitting', 'running', 'finalizing'] as const) {
      expect(generationDisplayProgress({ ...input, status }, now + 86_400_000).value).toBeLessThan(100)
      expect(generationDisplayProgress({ ...input, status, progress: 100 }, now).value).toBe(99)
    }
    expect(generationDisplayProgress({ ...input, status: 'succeeded' }, now)).toMatchObject({ value: 100, estimated: false })
  })
  it('does not count local queue time as cloud execution time', () => {
    const value = generationDisplayProgress({ ...input, createdAt: new Date(now - 86_400_000).toISOString(), updatedAt: new Date(now).toISOString() }, now)
    expect(value.value).toBe(5)
    expect(generationDisplayProgress({ ...input, status: 'queued' }, now).value).toBe(0)
    expect(generationDisplayProgress({ ...input, kind: 'video' }, now).value).toBeLessThan(generationDisplayProgress(input, now).value!)
  })
  it('retains an unknown value without a valid clock and stops estimates on terminal failures', () => {
    expect(generationDisplayProgress({ ...input, updatedAt: 'invalid' }, now).value).toBeNull()
    expect(generationDisplayProgress({ ...input, updatedAt: new Date(now + 3600_000).toISOString() }, now).value).toBeNull()
    expect(generationDisplayProgress(input, NaN).value).toBeNull()
    for (const status of ['blocked', 'failed', 'cancelled', 'interrupted'] as const) {
      expect(generationDisplayProgress({ ...input, status }, now)).toMatchObject({ value: null, estimated: false })
    }
  })
  it('handles invalid provider values and small clock skew conservatively', () => {
    for (const progress of [NaN, Infinity, -1, 101]) expect(generationDisplayProgress({ ...input, progress }, now).estimated).toBe(true)
    expect(generationDisplayProgress({ ...input, updatedAt: new Date(now + 20_000).toISOString() }, now).value).toBe(5)
    expect(generationDisplayProgress({ ...input, status: 'submitting' }, now).value).toBe(5)
    expect(generationDisplayProgress({ ...input, status: 'finalizing' }, now).value).toBe(99)
  })
})
