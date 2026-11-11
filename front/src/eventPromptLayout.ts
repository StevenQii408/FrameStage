import type { CardLod, CardSize } from './cardGeometry'
import type { CanvasPoint, CanvasRect } from './sceneLayout'

export const EVENT_PROMPT_GAP = 24

/** Matches EventPromptNode's bounded DOM, including its full-text scroll region. */
export function eventPromptSize(lod: CardLod): CardSize {
  return lod === 'overview' ? { width: 180, height: 100 }
    : { width: 280, height: lod === 'strip' ? 200 : 280 }
}

export function eventSlotSize(card: CardSize, lod: CardLod, hasPrompt: boolean): CardSize {
  if (!hasPrompt) return card
  const prompt = eventPromptSize(lod)
  return { width: card.width + EVENT_PROMPT_GAP + prompt.width, height: Math.max(card.height, prompt.height) }
}

/** Auto-layout reserves paired cells; manual positions may need a local free-space search.
 * Never move an existing card/task just to attach its prompt. */
export function placeEventPrompt(card: CanvasRect, size: CardSize, obstacles: CanvasRect[]): CanvasPoint {
  let point = { x: card.x + card.width + EVENT_PROMPT_GAP, y: card.y }
  for (let attempt = 0; attempt <= obstacles.length; attempt++) {
    const blockers = obstacles.filter(other => point.x < other.x + other.width + 12
      && point.x + size.width + 12 > other.x && point.y < other.y + other.height + 12
      && point.y + size.height + 12 > other.y)
    if (!blockers.length) break
    point = { ...point, y: Math.max(...blockers.map(other => other.y + other.height)) + EVENT_PROMPT_GAP }
  }
  return point
}
