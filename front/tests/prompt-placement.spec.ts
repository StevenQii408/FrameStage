import { describe, expect, it } from 'vitest'
import { placePromptDeck, PROMPT_WIDTH, PROMPT_HEIGHT } from '../src/promptPlacement'
import type { CanvasRect } from '../src/sceneLayout'

const owner = { x: 400, y: 100, width: 180, height: 180 }
const overlaps = (a: CanvasRect, b: CanvasRect) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
const assertFree = (points: { x: number; y: number }[], obstacles: CanvasRect[]) => {
  expect(points).toHaveLength(2)
  expect(points[1].x - points[0].x).toBe(226)
  expect(points[0].y).toBe(points[1].y)
  for (const point of points) expect(obstacles.some(rect => overlaps({ ...point, width: PROMPT_WIDTH, height: PROMPT_HEIGHT }, rect))).toBe(false)
}

describe('transient prompt palette', () => {
  it('uses space below the owner when free, without changing any shot position', () => {
    const original = { ...owner }
    const points = placePromptDeck(owner, [owner], [owner])
    expect(points[0]).toEqual({ x: 287, y: 304 })
    expect(owner).toEqual(original)
    assertFree(points, [owner])
  })
  it('moves the palette to the right of the scene when another row fills the lower space', () => {
    const next = { ...owner, y: 320 }
    const last = { ...owner, x: 832 }
    const obstacles = [owner, next, last]
    const points = placePromptDeck(owner, obstacles, obstacles)
    expect(points[0]).toEqual({ x: 1036, y: 100 })
    assertFree(points, obstacles)
  })
  it('avoids cross-task assets and freely dragged cards without relocating those obstacles', () => {
    const obstacles = [owner, { ...owner, y: 320 }, { x: 604, y: 80, width: 268, height: 360 }, { x: 890, y: 90, width: 250, height: 300 }]
    const snapshot = JSON.stringify(obstacles)
    const points = placePromptDeck(owner, [owner], obstacles)
    assertFree(points, obstacles)
    expect(points[0].x).toBe(1164)
    expect(JSON.stringify(obstacles)).toBe(snapshot)
    expect(placePromptDeck(owner, [owner], obstacles)).toEqual(points)
  })
})
