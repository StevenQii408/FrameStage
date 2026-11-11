import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, clearApiSession, setApiProjectId } from '../src/api/client'

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
afterEach(() => { clearApiSession(); setApiProjectId(''); vi.unstubAllGlobals() })

describe('local session authentication', () => {
  it('bootstraps once in memory and authenticates writes, without persisting credentials', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ token: 'local-session', enabled: true }))
      .mockResolvedValueOnce(json({ ok: true })).mockResolvedValueOnce(json({ ok: true }))
    vi.stubGlobal('fetch', fetcher)
    await apiFetch('/projects', { method: 'POST', body: '{}' })
    await apiFetch('/projects/p', { method: 'DELETE' })
    expect(fetcher).toHaveBeenCalledTimes(3)
    expect(fetcher.mock.calls[0][0]).toMatch(/\/session$/)
    expect(fetcher.mock.calls[1][1].headers.get('X-FrameForge-Session')).toBe('local-session')
    expect(fetcher.mock.calls[2][1].headers.get('X-FrameForge-Session')).toBe('local-session')
    expect(localStorage.getItem('local-session')).toBeNull()
  })
  it('refreshes once only when the server rejected a stale token before dispatch', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ token: 'old', enabled: true }))
      .mockResolvedValueOnce(json({ detail: 'expired', error: { code: 'session_token_invalid' } }, 403))
      .mockResolvedValueOnce(json({ token: 'new', enabled: true })).mockResolvedValueOnce(json({ id: 'saved-once' }))
    vi.stubGlobal('fetch', fetcher)
    const body = JSON.stringify({ idempotency_key: 'same-key' })
    expect(await apiFetch('/generations', { method: 'POST', body })).toEqual({ id: 'saved-once' })
    expect(fetcher.mock.calls[1][1].body).toBe(fetcher.mock.calls[3][1].body)
    expect(fetcher.mock.calls[3][1].headers.get('X-FrameForge-Session')).toBe('new')
    expect(fetcher).toHaveBeenCalledTimes(4)
  })
  it.each([403, 409, 502])('does not replay arbitrary rejected writes (%s)', async status => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ token: 'token', enabled: true }))
      .mockResolvedValueOnce(json({ detail: 'rejected', error: { code: 'origin_not_allowed' } }, status))
    vi.stubGlobal('fetch', fetcher)
    await expect(apiFetch('/jobs', { method: 'POST', body: new FormData() })).rejects.toMatchObject({ status })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('never replays an ambiguous network failure after sending a write', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ token: 'token', enabled: true }))
      .mockRejectedValueOnce(new TypeError('network gone'))
    vi.stubGlobal('fetch', fetcher)
    await expect(apiFetch('/jobs', { method: 'POST' })).rejects.toThrow('无法连接后端')
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('captures the project before asynchronous session initialization', async () => {
    setApiProjectId('project-a')
    let complete!: (value: Response) => void
    const fetcher = vi.fn().mockReturnValueOnce(new Promise<Response>(resolve => { complete = resolve }))
      .mockResolvedValueOnce(json({ ok: true }))
    vi.stubGlobal('fetch', fetcher)
    const request = apiFetch('/jobs', { method: 'POST' })
    setApiProjectId('project-b')
    complete(json({ token: 'token', enabled: true }))
    await request
    expect(fetcher.mock.calls[1][1].headers.get('X-Project-ID')).toBe('project-a')
  })
  it('keeps public reads independent from session bootstrap', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ ok: true }))
    vi.stubGlobal('fetch', fetcher)
    await apiFetch('/health')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
