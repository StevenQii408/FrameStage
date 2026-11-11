import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import GenerationWaveProgress from '../src/components/GenerationWaveProgress.vue'

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

describe('generation starfield progress', () => {
  it('renders actual provider percentages as accessible numeric progress', async () => {
    const wrapper = mount(GenerationWaveProgress, { props: { progress: 12, status: 'running', kind: 'image' } })
    expect(wrapper.attributes('aria-valuenow')).toBe('12')
    expect(wrapper.classes()).toContain('generation-starfield-progress')
    expect(wrapper.find('canvas').attributes('aria-hidden')).toBe('true')
    expect(wrapper.attributes('data-estimated')).toBe('false')
    expect(wrapper.attributes('data-tide-level')).toBe('12')
    await wrapper.setProps({ progress: 64 })
    expect(wrapper.attributes('aria-valuenow')).toBe('64')
    expect(wrapper.text()).toContain('64%')
    expect(wrapper.text()).not.toContain('预计')
    expect(wrapper.attributes('data-tide-level')).toBe('64')
    wrapper.unmount()
  })

  it('keeps stage-only responses with missing timestamps indeterminate', async () => {
    const wrapper = mount(GenerationWaveProgress, { props: { progress: null, status: 'submitting', kind: 'video' } })
    expect(wrapper.attributes('aria-valuenow')).toBeUndefined()
    expect(wrapper.attributes('data-tide-level')).toBe('unknown')
    expect(wrapper.text()).toContain('等待云端状态')
    expect(wrapper.text()).not.toContain('%')
    await wrapper.setProps({ status: 'finalizing' })
    expect(wrapper.attributes('aria-valuetext')).toContain('等待云端状态')
    expect(wrapper.find('.generation-waterline').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clearly distinguishes estimated progress and never calls it completed', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-11T10:01:00Z'))
    const wrapper = mount(GenerationWaveProgress, { props: {
      progress: null, status: 'running', kind: 'image', updatedAt: '2026-11-11T10:00:00Z',
    } })
    expect(wrapper.attributes('data-estimated')).toBe('true')
    expect(Number(wrapper.attributes('aria-valuenow'))).toBeGreaterThan(5)
    expect(Number(wrapper.attributes('aria-valuenow'))).toBeLessThan(100)
    expect(wrapper.text()).toContain('预计')
    expect(wrapper.attributes('aria-valuetext')).toContain('非云端实测进度')
    await wrapper.setProps({ progress: 31 })
    expect(wrapper.attributes('aria-valuenow')).toBe('31')
    expect(wrapper.text()).not.toContain('预计')
    wrapper.unmount()
  })

  it('suspends animation when the card is hidden or reduced motion is requested', async () => {
    const wrapper = mount(GenerationWaveProgress, { props: { progress: 30, status: 'running', kind: 'video', paused: true } })
    expect(wrapper.classes()).toContain('wave-paused')
    expect(wrapper.classes()).toContain('wave-video')
    await wrapper.setProps({ paused: false })
    expect(wrapper.classes()).not.toContain('wave-paused')
    wrapper.unmount()
  })

  it('updates text with a shared clock while reduced motion keeps the canvas static', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-11T10:00:00Z'))
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    const request = vi.spyOn(window, 'requestAnimationFrame')
    const wrapper = mount(GenerationWaveProgress, { props: {
      progress: null, status: 'running', kind: 'image', updatedAt: '2026-11-11T10:00:00Z',
    } })
    const initial = Number(wrapper.attributes('aria-valuenow'))
    await vi.advanceTimersByTimeAsync(5_000)
    expect(Number(wrapper.attributes('aria-valuenow'))).toBeGreaterThan(initial)
    expect(request).not.toHaveBeenCalled()
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
    request.mockRestore()
  })

  it.each([NaN, Infinity])('treats invalid progress %s as unknown', progress => {
    const wrapper = mount(GenerationWaveProgress, { props: { progress, status: 'running', kind: 'image' } })
    expect(wrapper.attributes('aria-valuenow')).toBeUndefined()
    expect(wrapper.classes()).toContain('wave-unknown')
    wrapper.unmount()
  })

  it('stops the reduced-motion text clock offscreen and resumes from current time', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-11T10:00:00Z'))
    const wrapper = mount(GenerationWaveProgress, { props: {
      status: 'running', kind: 'image', reducedMotion: true, updatedAt: '2026-11-11T10:00:00Z',
    } })
    await vi.advanceTimersByTimeAsync(2_000)
    expect(vi.getTimerCount()).toBe(1)
    await wrapper.setProps({ paused: true })
    expect(vi.getTimerCount()).toBe(0)
    const paused = wrapper.attributes('aria-valuenow')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.attributes('aria-valuenow')).toBe(paused)
    await wrapper.setProps({ paused: false })
    expect(Number(wrapper.attributes('aria-valuenow'))).toBeGreaterThan(Number(paused))
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
