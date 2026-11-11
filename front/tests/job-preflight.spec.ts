import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '../src/api/client'
import { createScriptJob, preflightJob } from '../src/api/jobs'
vi.mock('../src/api/client', () => ({ apiFetch: vi.fn(), apiUrl: (url: string) => url }))

beforeEach(() => vi.mocked(apiFetch).mockReset())

describe('non-billable input preflight', () => {
  it('rejects oversized scripts without making any request or truncating input', async () => {
    await expect(createScriptJob('甲'.repeat(10000) + '结尾')).rejects.toThrow('未截断')
    expect(apiFetch).not.toHaveBeenCalled()
  })
  it('stops before job creation when preflight rejects metadata-only input', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ accepted: false, reasons: ['没有剧情或对白'] })
    await expect(createScriptJob('人物介绍')).rejects.toThrow('没有剧情或对白')
    expect(apiFetch).toHaveBeenCalledTimes(1)
    expect(apiFetch).toHaveBeenCalledWith('/jobs/preflight', expect.any(Object))
  })
  it('preserves full script when accepted and enqueues exactly once', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({ accepted: true, reasons: [] }).mockResolvedValueOnce({ job_id: 'a' })
    const text = '女孩推门进入。\n女孩：我回来了。'
    expect(await createScriptJob(text)).toEqual({ job_id: 'a' })
    expect(apiFetch).toHaveBeenCalledTimes(2)
    const options = vi.mocked(apiFetch).mock.calls[1][1]!
    expect((options.body as FormData).get('script_text')).toBe(text)
  })
  it('sends duration estimates without uploading video frames', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ accepted: true, reasons: [] })
    await preflightJob({ inputType: 'video', duration: 31 })
    const body = vi.mocked(apiFetch).mock.calls[0][1]!.body as FormData
    expect(body.get('duration_seconds')).toBe('31')
    expect(body.has('video')).toBe(false)
  })
})
