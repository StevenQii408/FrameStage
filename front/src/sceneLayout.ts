export type CanvasPoint = { x: number; y: number }
export type CanvasRect = CanvasPoint & { width: number; height: number }
export type BranchScene = { id: string; open: boolean; shotHeights: number[]; shotWidths?: number[] }
export type BranchPlacement = { scene: CanvasPoint; shots: CanvasPoint[]; bounds: CanvasRect }

export const SHOTS_PER_ROW = 3
const SCENE_WIDTH = 250
const SCENE_HEIGHT = 230
const SHOT_X_OFFSET = 320
export const SHOT_ROW_GAP = 40
export const SCENE_GAP = 56

function overlaps(a: CanvasRect, b: CanvasRect, padding = 28): boolean {
  return a.x < b.x + b.width + padding && a.x + a.width + padding > b.x && a.y < b.y + b.height + padding && a.y + a.height + padding > b.y
}

function shotRows(heights: number[], widths: number[] = []) {
  const positions: CanvasPoint[] = []
  const cardWidth = widths.length ? Math.max(...widths) : 250
  const step = cardWidth + 36
  let y = 0
  for (let offset = 0; offset < heights.length; offset += SHOTS_PER_ROW) {
    const row = heights.slice(offset, offset + SHOTS_PER_ROW)
    row.forEach((_, column) => positions.push({ x: SHOT_X_OFFSET + column * step, y }))
    y += Math.max(...row) + SHOT_ROW_GAP
  }
  return { positions, height: Math.max(0, y - SHOT_ROW_GAP), width: heights.length ? (Math.min(SHOTS_PER_ROW, heights.length) - 1) * step + cardWidth : 0 }
}

/** The input node stays fixed; each real scene remains a direct child. */
export function layoutSceneBranches(anchor: CanvasPoint, scenes: BranchScene[], obstacles: CanvasRect[] = []): Record<string, BranchPlacement> {
  const occupied = [...obstacles]
  const result: Record<string, BranchPlacement> = {}
  let columnX = anchor.x + 400
  let columnRight = columnX
  let nextY = anchor.y
  scenes.forEach((item, index) => {
    const column = Math.floor(index / 6)
    const columnCount = Math.min(6, scenes.length - column * 6)
    const row = index % 6 - (columnCount - 1) / 2
    if (index % 6 === 0) {
      if (column) columnX = columnRight + 120
      nextY = anchor.y - (columnCount - 1) * 160
    }
    const local = shotRows(item.shotHeights, item.shotWidths)
    let x = columnX + Math.abs(row) * 42
    const y = nextY
    let cover: CanvasRect = { x, y, width: SCENE_WIDTH, height: SCENE_HEIGHT }
    for (let attempts = 0; attempts < 60; attempts++) {
      // Place the cover using only its own footprint. An obstacle near a later
      // shot must not fling the clicked deck far away from its input node.
      const blocking = occupied.filter((other) => overlaps(cover, other))
      if (!blocking.length) break
      x = Math.max(x + 290, ...blocking.map((other) => other.x + other.width + 64))
      cover = { ...cover, x }
    }
    const scene = { x, y }
    let grid: CanvasRect = { x: x + SHOT_X_OFFSET, y, width: local.width, height: local.height }
    if (item.open && item.shotHeights.length) {
      // Keep the deck close; only move the unfolded cards down to clear another
      // task or a manual generator. Do not use the empty cover-to-grid corridor
      // as a collision rectangle.
      for (let attempts = 0; attempts < 60; attempts++) {
        const blocking = occupied.filter(other => overlaps(grid, other))
        if (!blocking.length) break
        grid = { ...grid, y: Math.max(grid.y + 64, ...blocking.map(other => other.y + other.height + 64)) }
      }
    }
    const expanded = item.open && item.shotHeights.length > 0
    const bounds: CanvasRect = { x, y, width: expanded ? grid.x + grid.width - x : SCENE_WIDTH, height: expanded ? Math.max(SCENE_HEIGHT, grid.y + grid.height - y) : SCENE_HEIGHT }
    const shots = item.open
      ? local.positions.map((point) => ({ x: x + point.x, y: grid.y + point.y }))
      : item.shotHeights.map((_, shotIndex) => ({ x: x + 18 + (shotIndex % 5) * 3, y: y + 18 + Math.floor(shotIndex / 5) * 2 }))
    result[item.id] = { scene, shots, bounds }
    occupied.push(cover)
    if (expanded) occupied.push(grid)
    // A scene's rows grow downwards. Keep the next scene below that entire
    // subtree instead of treating it as a collision and pushing it miles right.
    nextY = y + bounds.height + SCENE_GAP
    columnRight = Math.max(columnRight, bounds.x + bounds.width)
  })
  return result
}
