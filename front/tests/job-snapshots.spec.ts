import { defineComponent, h } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { useJobSnapshots } from '../src/composables/useJobSnapshots'

function harness(load: (id: string) => Promise<void>, exists = () => true) {
  let sync!: ReturnType<typeof useJobSnapshots>
  const wrapper = mount(defineComponent({ setup() { sync = useJobSnapshots(load, exists); return () => h('div') } }))
  return { sync, wrapper }
}
afterEach(() => vi.useRealTimers())

it('reads the final result even if an earlier GET is already running', async () => {
  let finish!: () => void
  const load = vi.fn().mockImplementationOnce(() => new Promise<void>(done => { finish = done })).mockResolvedValue(undefined)
  const { sync, wrapper } = harness(load)
  const reading = sync.refresh('a')
  const terminal = sync.refresh('a', true)
  expect(terminal).toBe(reading)
  finish()
  await terminal
  expect(load).toHaveBeenCalledTimes(2)
  expect(sync.stale.value.size).toBe(0)
  wrapper.unmount()
})

it('bounds retries per job and recovers with a manual snapshot read', async () => {
  vi.useFakeTimers()
  const load = vi.fn(async (id: string) => { if (id === 'a') throw Error('offline') })
  const { sync, wrapper } = harness(load)
  const final = sync.refresh('a', true)
  await sync.refresh('b', true)
  await vi.runAllTimersAsync()
  await final
  expect(load.mock.calls.filter(([id]) => id === 'a')).toHaveLength(3)
  expect([...sync.stale.value]).toEqual(['a'])
  load.mockResolvedValue(undefined)
  await sync.refresh('a', true)
  expect(sync.stale.value.size).toBe(0)
  wrapper.unmount()
})

it('does not schedule new reads after leaving the workspace', async () => {
  vi.useFakeTimers()
  const load = vi.fn().mockRejectedValue(Error('offline'))
  const { sync, wrapper } = harness(load)
  const final = sync.refresh('a', true)
  await flushPromises()
  wrapper.unmount()
  await final
  expect(vi.getTimerCount()).toBe(0)
  expect(load).toHaveBeenCalledOnce()
})
