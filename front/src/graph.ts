import type { SceneGroup, Shot } from './types'

export interface SceneLane {
  id: string
  index: number
  shots: Shot[]
  startTime: string
  endTime: string
  warningCount: number
}

export interface SceneGroupView extends SceneLane {
  title: string
  groupingMode: string
  pageCount: number
  startSeconds: number
  endSeconds: number
}

export function semanticZoomLevel(zoom: number): 'overview' | 'strip' | 'detail' {
  if (zoom < 0.43) return 'overview'
  if (zoom < 0.78) return 'strip'
  return 'detail'
}

/**
 * A scene lane is a presentation unit rather than a new analysis boundary. Keeping
 * at most ten chronological shots in a row makes 30–50 shot jobs readable while
 * the shot's sceneGroupId remains available as semantic metadata.
 */
export function makeSceneLanes(shots: Shot[], maximum = 10): SceneLane[] {
  const ordered = [...shots].sort((a, b) => a.number - b.number)
  const size = Math.max(1, maximum)
  const lanes: SceneLane[] = []
  for (let offset = 0; offset < ordered.length; offset += size) {
    const chunk = ordered.slice(offset, offset + size)
    lanes.push({
      id: `scene-${chunk[0]?.id || offset}`,
      index: lanes.length + 1,
      shots: chunk,
      startTime: chunk[0]?.time.split('–')[0]?.trim() || '00:00',
      endTime: chunk[chunk.length - 1]?.time.split('–').slice(-1)[0]?.trim() || '00:00',
      warningCount: chunk.filter((shot) => shot.status === 'warning' || shot.analysisWarnings.length > 0).length,
    })
  }
  return lanes
}

/** A real scene remains one group even when its cards use multiple pages. */
export function makeSceneGroups(shots: Shot[], groups: SceneGroup[] = [], pageSize = 10): SceneGroupView[] {
  const ordered = [...shots].sort((a, b) => a.number - b.number)
  const byId = new Map(ordered.map((shot) => [shot.id, shot]))
  const explicit = groups.filter((group) => group.shotIds.some((id) => byId.has(id)))
  if (explicit.length) {
    const used = new Set<string>()
    const result = explicit.sort((a, b) => a.order - b.order).map((group, index) => {
      const members = group.shotIds.map((id) => byId.get(id)).filter((shot): shot is Shot => shot !== undefined && !used.has(shot.id))
      members.forEach((shot) => used.add(shot.id))
      return sceneView(group.id, index + 1, group.title, group.groupingMode, members, pageSize, group.startTime, group.endTime)
    }).filter((group) => group.shots.length)
    const missing = ordered.filter((shot) => !used.has(shot.id))
    if (missing.length) result.push(...makeSceneGroups(missing, [], pageSize).map((group, index) => ({ ...group, id: `unassigned-${group.id}`, index: result.length + index + 1, groupingMode: 'unassigned' })))
    return result
  }
  if (ordered.every((shot) => !shot.sceneGroupId)) {
    return makeSceneLanes(ordered, pageSize).map((lane) => sceneView(lane.id, lane.index, `历史分组 ${lane.index}`, 'legacy-presentation-group', lane.shots, pageSize))
  }
  const buckets: { id: string; shots: Shot[] }[] = []
  ordered.forEach((shot) => {
    const id = shot.sceneGroupId || `unassigned-${shot.id}`
    const current = buckets[buckets.length - 1]
    if (current?.id === id) current.shots.push(shot)
    else buckets.push({ id, shots: [shot] })
  })
  return buckets.map((bucket, index) => sceneView(bucket.id, index + 1, `场次 ${String(index + 1).padStart(2, '0')}`, 'scene-group-id', bucket.shots, pageSize))
}

function sceneView(id: string, index: number, title: string, groupingMode: string, shots: Shot[], pageSize: number, startSeconds?: number, endSeconds?: number): SceneGroupView {
  const startTime = shots[0]?.time.split(/[–—]/)[0]?.trim() || '00:00'
  const endTime = shots[shots.length - 1]?.time.split(/[–—]/).slice(-1)[0]?.trim() || '00:00'
  return {
    id, index, title, groupingMode, shots, startTime, endTime,
    startSeconds: startSeconds ?? 0, endSeconds: endSeconds ?? 0,
    warningCount: shots.filter((shot) => shot.status === 'warning' || shot.analysisWarnings.length > 0).length,
    pageCount: Math.max(1, Math.ceil(shots.length / Math.max(1, pageSize))),
  }
}
