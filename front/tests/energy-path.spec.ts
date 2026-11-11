import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import EnergyEdge from '../src/components/EnergyEdge.vue'
import { energyPath, energyPoint, timelineReturnPath } from '../src/energyPath'

describe('energy beam geometry', () => {
  it('routes row returns through the gutter, with a curve fallback for freely moved cards', () => {
    const source = { x: 650, y: 100 }, target = { x: 100, y: 340 }
    const path = timelineReturnPath(source, target, 220)
    expect(path).toMatch(/^M 650 100 /)
    expect(path).toMatch(/H 100$/)
    expect(path).toContain('674 220')
    expect(timelineReturnPath(source, { ...target, y: 60 }, 220)).toContain(' C ')
  })
  it('starts and ends on the same coordinates used by the particle layer', () => {
    const source = { x: 12, y: 40 }
    const target = { x: 350, y: 210 }
    expect(energyPoint(source, target, 0)).toEqual(source)
    expect(energyPoint(source, target, 1)).toEqual(target)
    expect(energyPath(source, target)).toContain(' C ')
  })
  it('keeps an intermediate point inside the bounded route', () => {
    const point = energyPoint({ x: 0, y: 0 }, { x: 400, y: 200 }, .5)
    expect(point.x).toBeGreaterThan(0)
    expect(point.x).toBeLessThan(400)
    expect(point.y).toBeGreaterThan(0)
    expect(point.y).toBeLessThan(200)
  })
})

describe('manual energy edge selection', () => {
  it('uses its actual SVG path for active travelling lights and removes animation when disabled', async () => {
    const edge = mount(EnergyEdge, { props: { id: 'flow', sourceX: 4, sourceY: 10, targetX: 500, targetY: 40, data: { active: true }, flowEnabled: true } })
    expect(edge.findAll('animateMotion')).toHaveLength(2)
    expect(edge.get('animateMotion').attributes('path')).toBe(edge.get('.energy-rail').attributes('d'))
    await edge.setProps({ flowEnabled: false })
    expect(edge.find('animateMotion').exists()).toBe(false)
    expect(edge.classes()).not.toContain('energy-edge-active')
  })
  it('shows chronology through an arrow and a continuation label rather than a second scene branch', () => {
    const edge = mount(EnergyEdge, { props: { id: 'timeline', sourceX: 650, sourceY: 100, targetX: 100, targetY: 340, data: { chronological: true, rowWrap: true, wrapY: 220, nextLabel: 'SEQ 04' } } })
    expect(edge.find('marker').exists()).toBe(true)
    expect(edge.find('.energy-rail').attributes('marker-end')).toContain('arrow')
    expect(edge.find('.timeline-wrap-label').text()).toBe('继续 → SEQ 04')
    expect(edge.find('.energy-hitbox').exists()).toBe(false)
  })
  it('renders an enlarged hit path and selected highlight only for editable wires', () => {
    const wire = mount(EnergyEdge, { props: { id: 'wire-a', sourceX: 0, sourceY: 0, targetX: 100, targetY: 60, data: { manual: true }, selected: true } })
    expect(wire.find('.energy-hitbox').exists()).toBe(true)
    expect(wire.find('.energy-edge-selected').exists()).toBe(true)
    const system = mount(EnergyEdge, { props: { id: 'system-a', sourceX: 0, sourceY: 0, targetX: 100, targetY: 60, data: { active: false }, selected: true } })
    expect(system.find('.energy-hitbox').exists()).toBe(false)
    expect(system.find('.energy-edge-selected').exists()).toBe(false)
  })
})
