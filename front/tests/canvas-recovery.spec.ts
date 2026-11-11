import { describe, expect, it } from 'vitest'
import { canvasEqualAfterDraftRecovery, recoverCanvasDrafts } from '../src/canvasRecovery'

describe('corrupted draft recovery', () => {
  const video = { key: 'draft-video', inputType: 'video', position: { x: 80, y: 90 }, scriptText: '' }
  it('restores the real 512-copy payload as one stable input without mutating the source', () => {
    const raw = Array.from({ length: 512 }, () => ({ ...video }))
    const result = recoverCanvasDrafts(raw)
    expect(result.drafts).toEqual([video])
    expect(result.duplicates).toBe(511)
    expect(raw).toHaveLength(512)
    expect(recoverCanvasDrafts(result.drafts).duplicates).toBe(0)
  })
  it('keeps independent drafts and written text when a blank duplicate follows', () => {
    const source = { ...video, key: 'draft-script', inputType: 'script', scriptText: '人物介绍与第一场对白' }
    const result = recoverCanvasDrafts([video, source, { ...source, scriptText: '' }])
    expect(result.drafts).toHaveLength(2)
    expect(result.drafts[1].scriptText).toBe(source.scriptText)
  })
  it('ignores invalid records instead of passing non-finite geometry to Vue Flow', () => {
    expect(recoverCanvasDrafts([null, {}, { ...video, position: { x: Infinity, y: 9 } }]).drafts).toEqual([])
  })
  it('recognizes duplicate-only repairs without suppressing genuine parameter or text edits', () => {
    expect(canvasEqualAfterDraftRecovery({ drafts: Array(512).fill(video), generators: [] }, { generators: [], drafts: [video] })).toBe(true)
    expect(canvasEqualAfterDraftRecovery({ drafts: [{ ...video, scriptText: '新编辑' }] }, { drafts: [video] })).toBe(false)
    expect(canvasEqualAfterDraftRecovery({ drafts: [video], generators: [{ id: 'g', ratio: '16:9' }] }, { drafts: [video], generators: [{ id: 'g', ratio: '9:16' }] })).toBe(false)
  })
})
