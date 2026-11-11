import type { CardLod, CardSize } from './cardGeometry'
import type { CanvasPoint, CanvasRect } from './sceneLayout'
import type { Shot } from './types'

export const SEQUENCE_SOURCES_PER_ROW = 4
export const SEQUENCE_SOURCE_GAP = 36
export const SEQUENCE_ROW_GAP = 48
export const SEQUENCE_BRANCH_GAP = 64

export interface SequenceBranch {
  id: string
  open: boolean
  coverSize: CardSize
  sourceSizes: CardSize[]
  promptSize?: CardSize
  promptPosition?: CanvasPoint
  coverPosition?: CanvasPoint
  sourcePositions?: Array<CanvasPoint | undefined>
}
export interface SequenceBranchPlacement {
  cover: CanvasPoint
  sources: CanvasPoint[]
  prompt?: CanvasPoint
  bounds: CanvasRect
}

export function sequenceDeckSize(_shot: Shot, lod: CardLod): CardSize {
  return lod === 'overview' ? { width: 180, height: 174 }
    : { width: 250, height: lod === 'strip' ? 230 : 274 }
}

function overlaps(a: CanvasRect, b: CanvasRect, padding = 20) {
  return a.x < b.x + b.width + padding && a.x + a.width + padding > b.x
    && a.y < b.y + b.height + padding && a.y + a.height + padding > b.y
}

function union(rectangles: CanvasRect[]): CanvasRect {
  const x = Math.min(...rectangles.map(rect => rect.x))
  const y = Math.min(...rectangles.map(rect => rect.y))
  return { x, y, width: Math.max(...rectangles.map(rect => rect.x + rect.width)) - x,
    height: Math.max(...rectangles.map(rect => rect.y + rect.height)) - y }
}

/** Lay out one sequence per input branch. Source rows follow source_shot_ids order.
 * Cached manual positions and other tasks are obstacles, never relocation targets. */
export function layoutSequenceBranches(anchor: CanvasPoint, branches: SequenceBranch[], obstacles: CanvasRect[] = []): Record<string, SequenceBranchPlacement> {
  const occupied = [...obstacles]
  // A later, manually moved cover/source is already occupied while placing an
  // earlier branch. This also prevents opening one branch over a dragged sibling.
  const fixedRects = branches.flatMap(branch => [
    ...(branch.coverPosition ? [{ ...branch.coverPosition, ...branch.coverSize, branch: branch.id }] : []),
    ...(branch.promptPosition && branch.promptSize ? [{ ...branch.promptPosition, ...branch.promptSize, branch: branch.id }] : []),
    ...(branch.open ? branch.sourceSizes.flatMap((size, index) => branch.sourcePositions?.[index]
      ? [{ ...branch.sourcePositions[index]!, ...size, branch: branch.id, source: index }] : []) : []),
  ])
  const result: Record<string, SequenceBranchPlacement> = {}
  let nextY = anchor.y
  for (const branch of branches) {
    const elsewhere = fixedRects.filter(rect => rect.branch !== branch.id)
    let cover = branch.coverPosition ? { ...branch.coverPosition } : { x: anchor.x + 400, y: nextY }
    if (!branch.coverPosition) {
      for (let attempt = 0; attempt <= occupied.length + elsewhere.length; attempt++) {
        const blocking = [...occupied, ...elsewhere].filter(rect => overlaps({ ...cover, ...branch.coverSize }, rect))
        if (!blocking.length) break
        cover = { ...cover, x: Math.max(...blocking.map(rect => rect.x + rect.width)) + SEQUENCE_BRANCH_GAP }
      }
    }
    const coverRect = { ...cover, ...branch.coverSize }
    const sizes = branch.sourceSizes
    const cellWidth = sizes.length ? Math.max(...sizes.map(size => size.width)) : 0
    let rowY = cover.y
    const calculated: CanvasPoint[] = []
    for (let offset = 0; offset < sizes.length; offset += SEQUENCE_SOURCES_PER_ROW) {
      const row = sizes.slice(offset, offset + SEQUENCE_SOURCES_PER_ROW)
      row.forEach((_, column) => calculated.push({ x: cover.x + branch.coverSize.width + SEQUENCE_SOURCE_GAP
        + column * (cellWidth + SEQUENCE_SOURCE_GAP), y: rowY }))
      rowY += Math.max(...row.map(size => size.height)) + SEQUENCE_ROW_GAP
    }
    let sources = sizes.map((_, index) => branch.sourcePositions?.[index] || calculated[index])
    if (branch.open && sources.length) {
      const fixed = fixedRects.filter(rect => rect.branch === branch.id && 'source' in rect)
      const sourceObstacles = [...occupied, ...elsewhere, coverRect, ...fixed]
      let offsetY = 0
      for (let attempt = 0; attempt <= sourceObstacles.length; attempt++) {
        const automatic = calculated.flatMap((point, index) => branch.sourcePositions?.[index] ? []
          : [{ ...point, y: point.y + offsetY, ...sizes[index] }])
        const blocking = automatic.flatMap(rect => sourceObstacles.filter(other => overlaps(rect, other)).map(other => ({ rect, other })))
        if (!blocking.length) break
        offsetY += Math.max(...blocking.map(({ rect, other }) => other.y + other.height + SEQUENCE_ROW_GAP - rect.y))
      }
      sources = calculated.map((point, index) => branch.sourcePositions?.[index] || { ...point, y: point.y + offsetY })
    }
    const visibleSources = branch.open ? sources.map((point, index) => ({ ...point, ...sizes[index] })) : []
    let prompt: CanvasPoint | undefined
    if (branch.promptSize) {
      const tail = visibleSources.at(-1) || coverRect
      prompt = branch.promptPosition ? { ...branch.promptPosition } : { x: tail.x + tail.width + SEQUENCE_SOURCE_GAP, y: tail.y }
      const promptObstacles = [...occupied, ...elsewhere, coverRect, ...visibleSources]
      for (let attempt = 0; !branch.promptPosition && attempt <= promptObstacles.length; attempt++) {
        const blocking = promptObstacles.filter(rect => overlaps({ ...prompt!, ...branch.promptSize! }, rect))
        if (!blocking.length) break
        prompt = { ...prompt, y: Math.max(...blocking.map(rect => rect.y + rect.height)) + SEQUENCE_ROW_GAP }
      }
    }
    const rectangles = [coverRect, ...visibleSources, ...(prompt && branch.promptSize ? [{ ...prompt, ...branch.promptSize }] : [])]
    const bounds = union(rectangles)
    result[branch.id] = { cover, sources, ...(prompt ? { prompt } : {}), bounds }
    occupied.push(...rectangles)
    nextY = Math.max(nextY, bounds.y + bounds.height + SEQUENCE_BRANCH_GAP)
  }
  return result
}
