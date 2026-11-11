import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, setApiProjectId } from '../src/api/client'

afterEach(() => { vi.unstubAllGlobals(); setApiProjectId('') })

describe('backend connection diagnostics', () => {
  it('explains a browser network/CORS failure without blaming the model or API key', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(apiFetch('/projects')).rejects.toThrow('5174')
    await expect(apiFetch('/projects')).rejects.toThrow('跨域白名单')
  })
  it('retains a backend validation error and its HTTP status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '参考图不属于当前项目' }), { status: 422 })))
    await expect(apiFetch('/generations')).rejects.toMatchObject({ name: 'ApiError', status: 422, message: '参考图不属于当前项目' })
  })
  it('preserves intentional request cancellation', async () => {
    const abort = new DOMException('cancelled', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort))
    await expect(apiFetch('/projects')).rejects.toBe(abort)
  })
})
