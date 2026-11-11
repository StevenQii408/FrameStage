import { describe, expect, it } from 'vitest'
import { defaultGenerationSettings, generationDurationIssue, generationDurationRange, readGenerationSettings, type GenerationCapabilities } from '../src/api/generations'

describe('generation duration policy', () => {
  it.each([.83, 4, 4.5, 5, 12.5, 15, 30, 0])('reads saved %ss without silently changing the original snapshot', duration => {
    expect(readGenerationSettings({ duration_seconds: duration }, 'video').duration_seconds).toBe(duration)
  })
  it.each([4, 5.5, 16, 30, 0, -1])('rejects %ss for a new default video request', duration => {
    expect(generationDurationIssue({ ...defaultGenerationSettings('video'), duration_seconds: duration }, 'video')).toContain('5–15 秒的整数')
  })
  it.each([5, 6, 10, 15])('accepts %ss and never imposes video duration on images', duration => {
    expect(generationDurationIssue({ ...defaultGenerationSettings('video'), duration_seconds: duration }, 'video')).toBe('')
    expect(generationDurationIssue(defaultGenerationSettings('image'), 'image')).toBe('')
  })
  it('requires both request limits and model capability before opening 30s', () => {
    const capabilities: GenerationCapabilities = { configured: true, provider: 'test', reason: '', video: { available: true, models: [{ id: 'video-a', min_duration_seconds: 6, max_duration_seconds: 30 }] } }
    expect(generationDurationRange(capabilities, 'video-a')).toEqual({ min: 6, max: 15, step: 1 })
    capabilities.request_limits = { video: { min_duration_seconds: 5, max_duration_seconds: 30, duration_step_seconds: 1, extensible_max_duration_seconds: 30 } }
    expect(generationDurationRange(capabilities, 'video-a')).toEqual({ min: 6, max: 30, step: 1 })
    expect(generationDurationIssue({ ...defaultGenerationSettings('video'), model_id: 'video-a', duration_seconds: 30 }, 'video', capabilities)).toBe('')
  })
  it('uses the server-declared Wan range even while generation is disabled', () => {
    const capabilities: GenerationCapabilities = { configured: false, provider: 'bailian-generation', reason: '',
      request_limits: { video: { min_duration_seconds: 2, max_duration_seconds: 30, duration_step_seconds: 1 } },
      video: { available: false, default_model_id: 'wan3.0-video', models: [{ id: 'wan3.0-video' }],
        min_duration_seconds: 2, max_duration_seconds: 30 } }
    expect(generationDurationRange(capabilities)).toEqual({ min: 2, max: 30, step: 1 })
    expect(generationDurationIssue({ ...defaultGenerationSettings('video'), duration_seconds: 2 }, 'video', capabilities)).toBe('')
    expect(readGenerationSettings({ aspect_ratio: '21:9', resolution: '480p' }, 'video')).toMatchObject({ aspect_ratio: '21:9', resolution: '480p' })
    expect(readGenerationSettings({ resolution: '2048p' }, 'image').resolution).toBe('2048p')
  })
})
