import { beforeEach, describe, expect, it, vi } from 'vitest'
const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', () => ({ apiFetch }))
import { cancelPerceptionOperation, createPerceptionOperation, getPerception, getPerceptionHistory, getPerceptionOperation } from '../src/api/perception'

describe('scoped perception HTTP requests', () => {
  beforeEach(() => vi.clearAllMocks())
  it('keeps status/history reads separate from explicit writes and preserves scope, revision and idempotency key', () => {
    const signal = new AbortController().signal
    getPerception('job/a', 'shot/b', signal)
    getPerceptionHistory('job/a', 'shot/b', signal)
    getPerceptionOperation('job/a', 'shot/b', 'op/c', signal)
    createPerceptionOperation('job/a', 'shot/b', { mode: 'recover', stages: ['motion'], expected_revision: 3, idempotency_key: 'unique-request-1' }, signal)
    cancelPerceptionOperation('job/a', 'shot/b', 'op/c', signal)
    const base = '/jobs/job%2Fa/shots/shot%2Fb/perception'
    expect(apiFetch.mock.calls.slice(0, 3).map(([url]) => url)).toEqual([base, `${base}/history`, `${base}/operations/op%2Fc`])
    expect(apiFetch.mock.calls.slice(0, 3).every(([, request]) => !request.method && request.signal === signal)).toBe(true)
    expect(apiFetch.mock.calls[3][0]).toBe(`${base}/operations`)
    expect(apiFetch.mock.calls[3][1].method).toBe('POST')
    expect(JSON.parse(apiFetch.mock.calls[3][1].body)).toEqual({ mode: 'recover', stages: ['motion'], expected_revision: 3, idempotency_key: 'unique-request-1' })
    expect(apiFetch.mock.calls[4]).toEqual([`${base}/operations/op%2Fc/cancel`, { method: 'POST', signal }])
  })
})
