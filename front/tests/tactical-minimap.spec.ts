import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import TacticalMiniMap from '../src/components/TacticalMiniMap.vue'
import type { Node } from '@vue-flow/core'

const mocks = vi.hoisted(() => ({ setCenter: vi.fn(), zoomTo: vi.fn() }))
vi.mock('@vue-flow/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@vue-flow/core')>()
  return { ...actual, useVueFlow: () => ({ dimensions: ref({ width: 1200, height: 800 }), setCenter: mocks.setCenter, zoomTo: mocks.zoomTo }) }
})

describe('tactical minimap zoom preservation', () => {
  it('renders only inputs as circles and all other nodes as color-coded proportional rectangles', () => {
    const fixtures = [
      ['video-input', 'project', { inputType: 'video' }],
      ['script-input', 'project', { inputType: 'script' }],
      ['shot', 'shot', {}],
      ['sequence', 'sequence', {}],
      ['image', 'generator', { generator: { kind: 'image' }, layoutWidth: 280, layoutHeight: 157.5 }],
      ['video', 'generator', { generator: { kind: 'video' }, layoutWidth: 150, layoutHeight: 268 }],
      ['scene', 'scene', {}],
      ['prompt', 'prompt', {}],
    ] as const
    const nodes = fixtures.map(([id, type, data], index) => ({ id, type, position: { x: index * 300, y: 0 }, data })) as Node[]
    const wrapper = mount(TacticalMiniMap, { props: { nodes, viewport: { x: 0, y: 0, zoom: 1 }, activeKeys: [], reduced: true } })
    const mark = (id: string) => wrapper.get(`[data-minimap-node="${id}"]`)
    expect(mark('video-input').attributes('data-minimap-kind')).toBe('input')
    expect(mark('script-input').find('circle').exists()).toBe(true)
    expect(mark('video-input').find('circle').exists()).toBe(true)
    expect(mark('shot').find('rect').exists()).toBe(true)
    expect(mark('sequence').attributes('data-minimap-kind')).toBe('shot')
    for (const id of ['shot', 'sequence', 'image', 'video', 'scene', 'prompt']) {
      expect(mark(id).find('rect').exists()).toBe(true)
      expect(mark(id).find('circle,polygon,path').exists()).toBe(false)
      expect(mark(id).attributes('pointer-events')).toBe('none')
    }
    const image = mark('image').get('rect')
    const video = mark('video').get('rect')
    expect(Number(image.attributes('width')) / Number(image.attributes('height'))).toBeCloseTo(280 / 157.5)
    expect(Number(video.attributes('width')) / Number(video.attributes('height'))).toBeCloseTo(150 / 268)
    expect(new Set([image.attributes('fill'), video.attributes('fill'), mark('prompt').get('rect').attributes('fill')]).size).toBe(3)
    expect(Number(mark('prompt').get('rect').attributes('width')) / Number(mark('prompt').get('rect').attributes('height'))).toBeCloseTo(230 / 180)
    expect(wrapper.get('[aria-label="小地图图例"]').text()).toContain('输入')
    expect(wrapper.get('[aria-label="小地图图例"]').text()).toContain('生图')
    expect(wrapper.get('[aria-label="小地图图例"]').text()).toContain('视频')
    expect(wrapper.findAll('.tactical-foot')).toHaveLength(1)
    expect(wrapper.get('.tactical-foot').findAll('span')).toHaveLength(5)
    expect(wrapper.find('.tactical-head,.hud-corner,.tactical-aux-legend,[data-minimap-icon]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('TACTICAL MAP')
    expect(wrapper.findAll('svg')).toHaveLength(1)
    wrapper.unmount()
  })

  it('keeps hidden and distant rectangles at their true proportions instead of shrinking them to fixed marks', () => {
    const nodes = [
      { id: 'hidden-image', type: 'generator', hidden: true, position: { x: 4200, y: 2300 }, data: { generator: { kind: 'image' }, layoutWidth: 280, layoutHeight: 600 } },
      { id: 'visible-image', type: 'generator', position: { x: 4200, y: 2300 }, data: { generator: { kind: 'image' }, layoutWidth: 280, layoutHeight: 600 } },
    ]
    const wrapper = mount(TacticalMiniMap, { props: { nodes, viewport: { x: -200, y: -100, zoom: .25 }, activeKeys: [], reduced: true } })
    const marker = wrapper.get('[data-minimap-node="hidden-image"]')
    const rect = marker.get('rect')
    const visible = wrapper.get('[data-minimap-node="visible-image"] rect')
    expect(marker.attributes('opacity')).toBe('0.45')
    expect(Number(rect.attributes('width')) / Number(rect.attributes('height'))).toBeCloseTo(280 / 600)
    expect(rect.attributes('width')).toBe(visible.attributes('width'))
    expect(rect.attributes('height')).toBe(visible.attributes('height'))
    expect(Number(rect.attributes('x'))).toBeGreaterThan(0)
    expect(Number(rect.attributes('y'))).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('retains at most six active radar pings and removes them in reduced motion', async () => {
    const nodes = Array.from({ length: 8 }, (_, i) => ({ id: `job-${i}`, type: 'project', position: { x: i * 300, y: 0 }, data: { taskKey: `job-${i}` } }))
    const wrapper = mount(TacticalMiniMap, { props: { nodes, viewport: { x: 0, y: 0, zoom: .5 }, activeKeys: nodes.map(node => node.id), reduced: false } })
    expect(wrapper.findAll('.tactical-ping')).toHaveLength(6)
    await wrapper.setProps({ reduced: true })
    expect(wrapper.findAll('.tactical-ping')).toHaveLength(0)
    expect(wrapper.findAll('[data-minimap-kind="input"]')).toHaveLength(8)
    wrapper.unmount()
  })

  it('keeps the current zoom during pointer navigation and keyboard panning', async () => {
    mocks.setCenter.mockClear()
    const wrapper = mount(TacticalMiniMap, { props: { nodes: [], viewport: { x: -240, y: -100, zoom: .44 }, activeKeys: [], reduced: true } })
    const svg = wrapper.get('svg').element as SVGSVGElement
    Object.defineProperty(svg, 'getBoundingClientRect', { value: () => ({ left: 0, top: 0, width: 204, height: 112 }) })
    Object.defineProperty(svg, 'setPointerCapture', { value: vi.fn() })
    Object.defineProperty(svg, 'hasPointerCapture', { value: () => true })
    Object.defineProperty(svg, 'releasePointerCapture', { value: vi.fn() })
    await wrapper.get('svg').trigger('pointerdown', { clientX: 102, clientY: 56, pointerId: 1 })
    expect(mocks.setCenter).toHaveBeenCalledWith(expect.any(Number), expect.any(Number), { duration: 0, zoom: .44 })
    await wrapper.get('svg').trigger('pointermove', { clientX: 140, clientY: 75, pointerId: 1 })
    expect(mocks.setCenter).toHaveBeenLastCalledWith(expect.any(Number), expect.any(Number), { duration: 0, zoom: .44 })
    await wrapper.get('svg').trigger('pointerup', { pointerId: 1 })
    const calls = mocks.setCenter.mock.calls.length
    await wrapper.get('svg').trigger('pointermove', { clientX: 150, clientY: 80, pointerId: 1 })
    expect(mocks.setCenter).toHaveBeenCalledTimes(calls)
    await wrapper.trigger('keydown', { key: 'ArrowRight' })
    expect(mocks.setCenter).toHaveBeenLastCalledWith(expect.any(Number), expect.any(Number), { duration: 160, zoom: .44 })
    wrapper.unmount()
  })
})
