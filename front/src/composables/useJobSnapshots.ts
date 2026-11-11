import { onBeforeUnmount, ref } from 'vue'

/** SSE carries incremental changes. After the terminal event there is no stream
 * left to trigger another GET, so explicitly recover the final snapshot. */
export function useJobSnapshots(load: (id: string) => Promise<void>, exists: (id: string) => boolean) {
  const stale = ref(new Set<string>())
  const pending = new Map<string, { dirty: boolean; terminal: boolean; promise: Promise<void> }>()
  const delays = [400, 1200]
  const waits = new Set<() => void>()
  let disposed = false

  function delay(ms: number) {
    return new Promise<void>(resolve => {
      const release = () => { clearTimeout(timer); waits.delete(release); resolve() }
      const timer = setTimeout(release, ms)
      waits.add(release)
    })
  }

  function refresh(id: string, terminal = false): Promise<void> {
    if (disposed || !exists(id)) return Promise.resolve()
    const active = pending.get(id)
    if (active) {
      // An older in-flight GET can precede the event's committed result.
      if (!terminal || !active.terminal) active.dirty = true
      active.terminal ||= terminal
      return active.promise
    }
    const state = { dirty: false, terminal, promise: Promise.resolve() }
    pending.set(id, state)
    state.promise = (async () => {
      let retries = 0
      do {
        if (disposed || !exists(id)) break
        state.dirty = false
        try {
          await load(id)
          if (!disposed) stale.value.delete(id)
        } catch {
          if (disposed || !exists(id)) break
          if (state.terminal && !state.dirty) {
            if (retries >= delays.length) { stale.value.add(id); break }
            await delay(delays[retries++])
            state.dirty = true
          }
        }
      } while (state.dirty)
    })().finally(() => pending.delete(id))
    return state.promise
  }

  onBeforeUnmount(() => { disposed = true; for (const release of waits) release() })
  return { refresh, stale }
}
