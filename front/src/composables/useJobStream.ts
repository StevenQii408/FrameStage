import { onBeforeUnmount, ref } from 'vue'
import { jobEventsUrl } from '../api/jobs'

export type JobStreamEvent = { type: string; payload: Record<string, unknown> }
type EventSourceFactory = (url: string) => EventSource

export const terminalEvents = new Set(['job.completed', 'job.failed', 'job.cancelled', 'job.paused_memory_pressure', 'job.interrupted'])
const eventTypes = [
  'job.started', 'source.ready', 'job.stage_changed', 'job.warning', 'shot.created', 'shot.description_ready',
  'shot.failed', 'frame.created', 'shot.analyzed', 'prompt.placeholder_created', 'prompt.ready',
  'perception.stage',
  'style.observation_ready', 'style.embedding_started', 'style.embedding_progress',
  'style.retrieval_ready', 'style.aggregation_ready', 'style.bible_draft_ready',
  'style.review_needed', 'style.bible_confirmed',
  'style.direct.baseline_ready', 'style.direct.warning',
  'sequence.ready', 'scene.regrouped', 'capacity.ready',
  'script.document_ready', 'script.window_started', 'script.window_ready', 'script.scene_ready', 'script.tool_finished',
  'job.completed', 'job.failed', 'job.cancelled', 'job.paused_memory_pressure', 'job.interrupted',
]

export function useJobStreams(factory: EventSourceFactory = (url) => new EventSource(url)) {
  const connectedIds = ref<Set<string>>(new Set())
  const sources = new Map<string, EventSource>()
  const cursors = new Map<string, number>()

  function mark(jobId: string, connected: boolean) {
    const next = new Set(connectedIds.value)
    if (connected) next.add(jobId)
    else next.delete(jobId)
    connectedIds.value = next
  }

  function close(jobId: string) {
    sources.get(jobId)?.close()
    sources.delete(jobId)
    mark(jobId, false)
  }

  function closeAll() {
    for (const jobId of [...sources.keys()]) close(jobId)
  }

  function dispatch(jobId: string, fallbackType: string, message: MessageEvent, onEvent: (event: JobStreamEvent) => void) {
    try {
      const data = JSON.parse(message.data)
      const type = data.type || fallbackType
      const sequence = Number(message.lastEventId || data.sequence || 0)
      const identified = Number.isSafeInteger(sequence) && sequence > 0
      if (identified && sequence <= (cursors.get(jobId) || 0)) return
      onEvent({ type, payload: data.payload || {} })
      if (identified) cursors.set(jobId, sequence)
      if (terminalEvents.has(type)) close(jobId)
    } catch {
      // A later REST snapshot remains authoritative if one SSE payload is malformed.
    }
  }

  function connect(jobId: string, onEvent: (event: JobStreamEvent) => void) {
    close(jobId)
    const source = factory(jobEventsUrl(jobId, cursors.get(jobId) || 0))
    sources.set(jobId, source)
    // Late events from a closed/replaced connection cannot mutate current state.
    const current = () => sources.get(jobId) === source
    source.onopen = () => { if (current()) mark(jobId, true) }
    source.onerror = () => { if (current()) mark(jobId, false) }
    source.onmessage = (message) => { if (current()) dispatch(jobId, '', message, onEvent) }
    for (const type of eventTypes) source.addEventListener(type, (message) => { if (current()) dispatch(jobId, type, message as MessageEvent, onEvent) })
  }

  onBeforeUnmount(closeAll)
  return { connectedIds, connect, close, closeAll, sourceCount: () => sources.size }
}

export const useJobStream = useJobStreams
