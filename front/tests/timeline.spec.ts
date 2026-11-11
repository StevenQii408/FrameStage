import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TimelineView from '../src/components/TimelineView.vue'
import { timelineLanes, timelineTicks, timeLabel } from '../src/timelineGeometry'
import type { GenerationSequence, Shot, WorkbenchTask } from '../src/types'

const shot = (id: string, number: number, start: number, end: number, suggested?: number) => ({ id, number, startTime: start, endTime: end, time: `${start}–${end}`, duration: `${end - start}s`, action: id, status: 'ready', thumb: '', frames: [], generationDurationSeconds: suggested }) as Shot
function task(key = 'a'): WorkbenchTask {
  return { key, jobId: key, inputType: 'video', videoFilename: `${key}.mp4`, shots: [shot(`${key}1`, 1, 0, 2, 3), shot(`${key}2`, 2, 20, 24, 5)], generationSequences: [], position: { x: 0, y: 0 } } as unknown as WorkbenchTask
}
const sequence = (id: string, ids: string[], start: number, end: number, suggested: number) => ({ id, event_id: id, source_shot_ids: ids, source_start_time: start, source_end_time: end, source_duration_seconds: end - start, generation_duration_seconds: suggested, timeline: [], status: 'ready' }) as GenerationSequence

describe('time-based timeline geometry', () => {
  it('keeps numeric source gaps and actual durations without a minimum fake duration', () => {
    const item = task()
    item.shots.push(shot('a3', 3, 24, 24.1))
    const lane = timelineLanes([item], 'source')[0]
    expect(lane.clips.map(clip => [clip.start, clip.end])).toEqual([[0, 2], [20, 24], [24, 24.1]])
    expect(lane.duration).toBe(24.1)
    expect(lane.estimated).toBe(false)
  })
  it('uses suggested sequence duration cumulatively and keeps every task in its own lane', () => {
    const first = task()
    first.generationSequences = [sequence('s1', ['a1'], 0, 2, 4), sequence('s2', ['a2'], 20, 24, 7)]
    const lanes = timelineLanes([first, task('b')], 'generation')
    expect(lanes).toHaveLength(2)
    expect(lanes[0].clips.map(clip => [clip.shot.id, clip.start, clip.end])).toEqual([['s1', 0, 4], ['s2', 4, 11]])
    expect(lanes[1].clips.map(clip => [clip.start, clip.end])).toEqual([[0, 3], [3, 8]])
  })
  it('does not hide uncovered sources in a partial sequence projection', () => {
    const item = task()
    item.generationSequences = [sequence('s1', ['a1'], 0, 2, 4)]
    expect(timelineLanes([item], 'generation')[0].clips.map(clip => clip.shot.id)).toEqual(['a1', 'a2'])
  })
  it('labels unknown duration without inventing generation seconds', () => {
    const item = task()
    delete item.shots[0].generationDurationSeconds
    const lane = timelineLanes([item], 'generation')[0]
    expect(lane.clips[0]).toMatchObject({ start: 0, end: 0, durationKnown: false })
    expect(lane.clips[1]).toMatchObject({ start: 0, end: 5 })
  })
  it('marks script source time as estimated and extends the ruler beyond fifteen seconds', () => {
    const item = { ...task(), inputType: 'script' } as WorkbenchTask
    expect(timelineLanes([item], 'source')[0].estimated).toBe(true)
    const ticks = timelineTicks(600, 52)
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(600)
    expect(ticks.length).toBeLessThanOrEqual(202)
    expect(timeLabel(65)).toBe('01:05')
  })
  it('selects clips and changes time basis and zoom using accessible controls', async () => {
    const wrapper = mount(TimelineView, { props: { tasks: [task()], selectedId: '' } })
    expect(wrapper.findAll('.time-lane')).toHaveLength(1)
    const source = wrapper.findAll('.time-clip')[1]
    expect(source.attributes('style')).toContain('left: 1040px')
    await source.trigger('click')
    expect(wrapper.emitted('select')?.[0]).toEqual(['a2'])
    await wrapper.get('button[aria-pressed="false"]').trigger('click')
    expect(wrapper.findAll('.time-clip')[1].attributes('style')).toContain('left: 156px')
    await wrapper.get('input[aria-label="时间轴缩放"]').setValue(100)
    expect(wrapper.findAll('.time-clip')[1].attributes('style')).toContain('left: 300px')
    wrapper.unmount()
  })
})
