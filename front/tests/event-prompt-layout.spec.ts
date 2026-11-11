import { describe, expect, it } from 'vitest'
import { eventPromptSize, eventSlotSize, placeEventPrompt, EVENT_PROMPT_GAP } from '../src/eventPromptLayout'
import { layoutSceneBranches, SHOTS_PER_ROW, SHOT_ROW_GAP } from '../src/sceneLayout'

describe('merged event prompt geometry', () => {
  it.each(['overview', 'strip', 'detail'] as const)('reserves a %s paired footprint instead of covering the next sequence', lod => {
    const card = { width: lod === 'overview' ? 180 : 250, height: 180 }
    const prompt = eventPromptSize(lod)
    const slot = eventSlotSize(card, lod, true)
    expect(slot.width).toBe(card.width + EVENT_PROMPT_GAP + prompt.width)
    expect(slot.height).toBe(Math.max(card.height, prompt.height))
    const branch = layoutSceneBranches({ x: 20, y: 40 }, [{ id: 'scene', open: true,
      shotWidths: Array(6).fill(slot.width), shotHeights: Array(6).fill(slot.height) }]).scene
    const rectangles = branch.shots.flatMap(position => [
      { ...position, ...card }, { x: position.x + card.width + EVENT_PROMPT_GAP, y: position.y, ...prompt },
    ])
    for (let i = 0; i < rectangles.length; i++) for (let j = i + 1; j < rectangles.length; j++) {
      const a = rectangles[i], b = rectangles[j]
      expect(a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y).toBe(false)
    }
    expect(branch.shots[SHOTS_PER_ROW].y - branch.shots[0].y).toBe(slot.height + SHOT_ROW_GAP)
  })

  it('keeps ordinary source and unmerged cards at the existing size', () => {
    const card = { width: 250, height: 410 }
    expect(eventSlotSize(card, 'detail', false)).toBe(card)
  })

  it('locally finds free space for a user-positioned sequence without moving any obstacle', () => {
    const card = { x: 300, y: 200, width: 250, height: 280 }
    const size = eventPromptSize('detail')
    const obstacles = [card, { x: 570, y: 200, width: 250, height: 300 },
      { x: 570, y: 530, width: 280, height: 200 }]
    const before = JSON.stringify(obstacles)
    const point = placeEventPrompt(card, size, obstacles)
    expect(point.x).toBe(card.x + card.width + EVENT_PROMPT_GAP)
    expect(point.y).toBeGreaterThanOrEqual(754)
    expect(JSON.stringify(obstacles)).toBe(before)
    const normal = placeEventPrompt(card, size, [card])
    expect(normal).toEqual({ x: 574, y: 200 })
  })

  it.each([1, 10, 11, 30, 53])('keeps %i paired slots addressable in compact rows', count => {
    const slot = eventSlotSize({ width: 250, height: 220 }, 'detail', true)
    const branch = layoutSceneBranches({ x: 0, y: 0 }, [{ id: 'scene', open: true,
      shotWidths: Array(count).fill(slot.width), shotHeights: Array(count).fill(slot.height) }]).scene
    expect(branch.shots).toHaveLength(count)
    expect(new Set(branch.shots.map(point => `${point.x}:${point.y}`)).size).toBe(count)
    expect(new Set(branch.shots.map(point => point.y)).size).toBe(Math.ceil(count / SHOTS_PER_ROW))
  })
})
