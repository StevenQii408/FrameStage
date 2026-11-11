import { describe, expect, it } from 'vitest'
import { makeSceneLanes, semanticZoomLevel } from '../src/graph'
import type { Shot } from '../src/types'

function shots(count: number): Shot[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `shot-${index + 1}`, number: index + 1, time: `00:${String(index).padStart(2, '0')}–00:${String(index + 1).padStart(2, '0')}`,
    duration: '1.0s', action: `动作 ${index + 1}`, shotSize: '中景', lens: '标准', movement: '固定', lighting: '自然柔光', confidence: .8,
    thumb: '', frames: [], analysisWarnings: index % 7 === 0 ? ['复核'] : [], status: index % 7 === 0 ? 'warning' : 'ready',
    sourceRawShotIds: [`raw-${index + 1}`], sceneGroupId: `scene-${Math.floor(index / 4) + 1}`,
    spatialEvidence: { foreground: '无', midground: '主体', background: '环境', relationships: '主体居中', observable: true },
    actionDetails: { primarySubject: '主体', initialState: '静止', steps: ['移动'], interactionTarget: '无', finalState: '停止', emotionChange: '无', evidence: ['Frame 1→4'] },
  }))
}

describe('cinematic movie map layout', () => {
  it.each([[1, 1], [10, 1], [11, 2], [30, 3], [53, 6]])('splits %i shots into %i horizontal lanes', (count, expected) => {
    const lanes = makeSceneLanes(shots(count))
    expect(lanes).toHaveLength(expected)
    expect(lanes.every((lane) => lane.shots.length <= 10)).toBe(true)
    expect(lanes.flatMap((lane) => lane.shots).map((shot) => shot.number)).toEqual(Array.from({ length: count }, (_, index) => index + 1))
  })

  it('switches information density at stable semantic zoom thresholds', () => {
    expect(semanticZoomLevel(.3)).toBe('overview')
    expect(semanticZoomLevel(.6)).toBe('strip')
    expect(semanticZoomLevel(1)).toBe('detail')
  })
})
