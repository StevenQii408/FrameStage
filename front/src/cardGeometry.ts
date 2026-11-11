import type { Shot } from './types'

export type CardLod = 'overview' | 'strip' | 'detail'
export type CardSize = { width: number; height: number }

/** Bound scene covers in both axes. Aspect ratio alone lets portrait posters
 * overflow an absolutely positioned deck and cover its own caption. */
export function sceneCoverSize(shot: Shot, overview: boolean): CardSize {
  const frame = shot.frames[0]
  const width = frame?.width && frame?.height ? frame.width : 16
  const height = frame?.width && frame?.height ? frame.height : 9
  const scale = Math.min((overview ? 160 : 184) / width, (overview ? 90 : 138) / height)
  return { width: width * scale, height: height * scale }
}

/** Conservative initial dimensions; Vue Flow's measured DOM size supersedes
 * these estimates. Thumbnail mode must not reserve hidden detail sections. */
export function estimateCardSize(shot: Shot, lod: CardLod): CardSize {
  const frame = shot.frames[0]
  const width = lod === 'overview' ? 180 : 250
  const innerWidth = lod === 'overview' ? 162 : 222
  const ratio = frame?.width && frame?.height ? frame.height / frame.width : 9 / 16
  const mediaHeight = shot.thumb ? (lod === 'overview' ? Math.min(118, innerWidth * ratio) : innerWidth * ratio) : (lod === 'overview' ? 74 : 150)
  const chrome = lod === 'detail' ? (shot.generationSequence ? 180 : 208) : lod === 'strip' ? (shot.generationSequence ? 90 : 76) : (shot.generationSequence ? 84 : 64)
  return { width, height: Math.ceil(mediaHeight + chrome) }
}
