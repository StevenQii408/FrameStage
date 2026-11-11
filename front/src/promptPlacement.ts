import type { CanvasPoint, CanvasRect } from './sceneLayout'

export const PROMPT_WIDTH = 210
export const PROMPT_HEIGHT = 132
const STEP = 226
const GAP = 24

function intersects(a: CanvasRect, b: CanvasRect) {
  return a.x < b.x + b.width + 12 && a.x + a.width + 12 > b.x && a.y < b.y + b.height + 12 && a.y + a.height + 12 > b.y
}

/** A transient two-card palette finds free space instead of reserving a lane in
 * every row. Never move the task, scene or shots just to show a prompt. */
export function placePromptDeck(shot: CanvasRect, sceneCards: CanvasRect[], obstacles: CanvasRect[]): CanvasPoint[] {
  const right = Math.max(shot.x + shot.width, ...sceneCards.map(card => card.x + card.width)) + GAP
  const candidates = [
    { x: shot.x - STEP / 2, y: shot.y + shot.height + GAP },
    { x: right, y: shot.y },
  ]
  const free = (point: CanvasPoint) => !obstacles.some(rect => intersects({ ...point, width: PROMPT_WIDTH + STEP, height: PROMPT_HEIGHT }, rect))
  for (const point of candidates) if (free(point)) return [point, { x: point.x + STEP, y: point.y }]
  let point = { ...candidates[1] }
  // Finite obstacles: every blocking rectangle advances the pair past its right
  // edge. This also works for manually dragged cards and cross-task assets.
  for (let attempt = 0; attempt <= obstacles.length; attempt++) {
    const blockers = obstacles.filter(rect => intersects({ ...point, width: PROMPT_WIDTH + STEP, height: PROMPT_HEIGHT }, rect))
    if (!blockers.length) break
    point = { ...point, x: Math.max(...blockers.map(rect => rect.x + rect.width)) + GAP }
  }
  return [point, { x: point.x + STEP, y: point.y }]
}
