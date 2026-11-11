import { sequenceCards } from './generationSequences'
import type { Shot, WorkbenchTask } from './types'

export type TimelineBasis = 'source' | 'generation'
export interface TimelineClip { shot: Shot; start: number; end: number; durationKnown: boolean }
export interface TimelineLane { task: WorkbenchTask; clips: TimelineClip[]; duration: number; estimated: boolean }
const valid = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0

/** Legacy display strings are only a fallback; current jobs provide numeric times. */
function sourceRange(shot: Shot): [number, number] | undefined {
  if (valid(shot.startTime) && valid(shot.endTime) && shot.endTime > shot.startTime) return [shot.startTime, shot.endTime]
  const sequence = shot.generationSequence
  if (sequence && valid(sequence.source_start_time) && valid(sequence.source_end_time) && sequence.source_end_time > sequence.source_start_time) return [sequence.source_start_time, sequence.source_end_time]
  const times = shot.time.match(/\d+(?:\.\d+)?/g)?.map(Number)
  return times && times.length === 2 && times[1] > times[0] ? [times[0], times[1]] : undefined
}

export function timelineLanes(tasks: WorkbenchTask[], basis: TimelineBasis): TimelineLane[] {
  return tasks.filter(task => task.jobId).map(task => {
    const sources = task.shots.slice().sort((a, b) => a.number - b.number)
    const sequences = sequenceCards(task)
    // Partial projections must not hide source intervals that have not been grouped yet.
    const owned = sequences.flatMap(shot => shot.generationSequence!.source_shot_ids)
    const complete = owned.length === sources.length && new Set(owned).size === sources.length && sources.every(shot => owned.includes(shot.id))
    const items = basis === 'generation' && sequences.length && complete ? sequences : sources
    let cursor = 0
    const clips = items.map(shot => {
      if (basis === 'source') {
        const range = sourceRange(shot)
        return { shot, start: range?.[0] ?? 0, end: range?.[1] ?? 0, durationKnown: !!range }
      }
      const seconds = shot.generationSequence?.generation_duration_seconds ?? shot.generationDurationSeconds
        ?? (task.inputType === 'script' ? Number.parseFloat(shot.duration) : undefined)
      const durationKnown = valid(seconds) && seconds > 0
      const start = cursor
      if (durationKnown) cursor += seconds
      return { shot, start, end: cursor, durationKnown }
    })
    return { task, clips, duration: Math.max(0, ...clips.map(clip => clip.end)), estimated: basis === 'generation' || task.inputType === 'script' }
  })
}

export function timelineTicks(duration: number, pixelsPerSecond: number): number[] {
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]
  const step = steps.find(value => value * pixelsPerSecond >= 75 && duration / value <= 200) || Math.max(3600, Math.ceil(duration / 200))
  return Array.from({ length: Math.ceil(Math.max(1, duration) / step) + 1 }, (_, index) => index * step)
}

export function timeLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${rest.toFixed(Number.isInteger(rest) ? 0 : 1).padStart(Number.isInteger(rest) ? 2 : 4, '0')}`
}
