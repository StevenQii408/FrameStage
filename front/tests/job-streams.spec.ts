import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { useJobStreams } from '../src/composables/useJobStream'

class FakeEventSource {
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  closed = false
  listeners = new Map<string, Array<(event: MessageEvent) => void>>()
  constructor(public url: string) {}
  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const callback = listener as (event: MessageEvent) => void
    this.listeners.set(type, [...(this.listeners.get(type) || []), callback])
  }
  close() { this.closed = true }
  emit(type: string, payload: Record<string, unknown> = {}, sequence = 0) {
    const event = new MessageEvent(type, { data: JSON.stringify({ type, payload, sequence }), lastEventId: sequence ? String(sequence) : '' })
    for (const listener of this.listeners.get(type) || []) listener(event)
  }
}

describe('multi-job SSE manager', () => {
  it('deduplicates replay and resumes from each job cursor, ignoring replaced connections', () => {
    const created: FakeEventSource[] = []
    let streams!: ReturnType<typeof useJobStreams>
    const wrapper = mount(defineComponent({ setup() { streams = useJobStreams(url => { const source = new FakeEventSource(url); created.push(source); return source as unknown as EventSource }); return () => h('div') } }))
    const seen: string[] = []
    streams.connect('a', event => seen.push(`a:${event.type}`))
    streams.connect('b', event => seen.push(`b:${event.type}`))
    created[0].emit('shot.analyzed', {}, 4)
    created[0].emit('shot.analyzed', {}, 4)
    created[1].emit('shot.analyzed', {}, 4)
    streams.connect('a', event => seen.push(`a:${event.type}`))
    expect(created[2].url).toContain('after=4')
    created[0].emit('job.completed', {}, 9)
    expect(created[2].closed).toBe(false)
    created[2].emit('shot.analyzed', {}, 3)
    created[2].emit('prompt.ready', {}, 5)
    expect(seen).toEqual(['a:shot.analyzed', 'b:shot.analyzed', 'a:prompt.ready'])
    created[2].emit('job.interrupted', {}, 6)
    expect(created[2].closed).toBe(true)
    wrapper.unmount()
  })
  it('delivers script document, window, scene and tool events while keeping the stream open', () => {
    let source!: FakeEventSource
    let streams!: ReturnType<typeof useJobStreams>
    const wrapper = mount(defineComponent({ setup() { streams = useJobStreams((url) => { source = new FakeEventSource(url); return source as unknown as EventSource }); return () => h('div') } }))
    const seen: string[] = []
    streams.connect('job-script', ({ type }) => seen.push(type))
    const events = ['script.document_ready', 'script.window_started', 'script.window_ready', 'script.scene_ready', 'script.tool_finished']
    for (const type of events) source.emit(type, { scene_id: 'scene-1' })
    expect(seen).toEqual(events)
    expect(source.closed).toBe(false)
    expect(streams.sourceCount()).toBe(1)
    wrapper.unmount()
  })

  it('delivers the early style baseline and per-shot prompts without closing a running job', () => {
    let source!: FakeEventSource
    let streams!: ReturnType<typeof useJobStreams>
    const wrapper = mount(defineComponent({ setup() { streams = useJobStreams((url) => { source = new FakeEventSource(url); return source as unknown as EventSource }); return () => h('div') } }))
    const seen: string[] = []
    streams.connect('job-early', (event) => seen.push(event.type))
    source.emit('style.direct.baseline_ready')
    source.emit('prompt.ready', { shot_id: 'shot-1' })
    source.emit('sequence.ready', { sequence_id: 'sequence-1' })
    source.emit('scene.regrouped', { scene_count: 2 })
    expect(seen).toEqual(['style.direct.baseline_ready', 'prompt.ready', 'sequence.ready', 'scene.regrouped'])
    expect(source.closed).toBe(false)
    wrapper.unmount()
  })
  it('closes only the terminal job connection', () => {
    const created: FakeEventSource[] = []
    let streams!: ReturnType<typeof useJobStreams>
    const Harness = defineComponent({ setup() { streams = useJobStreams((url) => { const source = new FakeEventSource(url); created.push(source); return source as unknown as EventSource }); return () => h('div') } })
    const wrapper = mount(Harness)
    const seen: string[] = []
    streams.connect('job-a', ({ type }) => seen.push(`a:${type}`))
    streams.connect('job-b', ({ type }) => seen.push(`b:${type}`))
    expect(streams.sourceCount()).toBe(2)
    created[0].emit('job.completed')
    expect(created[0].closed).toBe(true)
    expect(created[1].closed).toBe(false)
    expect(streams.sourceCount()).toBe(1)
    expect(seen).toEqual(['a:job.completed'])
    wrapper.unmount()
    expect(created[1].closed).toBe(true)
  })
})
