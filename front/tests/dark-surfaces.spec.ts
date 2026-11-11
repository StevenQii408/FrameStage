import { describe, expect, it } from 'vitest'
import { compileStyle, parse } from '@vue/compiler-sfc'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import postcss, { type Rule } from 'postcss'
import timelineSource from '../src/components/TimelineView.vue?raw'

const styles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

describe('dark theme surfaces', () => {
  it('defines dark semantic tokens at document scope, including teleported surfaces', () => {
    const css = postcss.parse(styles)
    const root = css.nodes.find(node => node.type === 'rule' && node.selector === ':root') as Rule
    const declarations = Object.fromEntries(root.nodes.filter(node => node.type === 'decl').map(node => [node.prop, node.value]))
    expect(declarations['color-scheme']).toBe('dark')
    expect(declarations['--surface']).toBe('#18181b')
    expect(declarations['--text']).toBe('#e4e4e7')
    expect(declarations.background).toBe('var(--surface)')
    const selectors: string[] = []
    css.walkRules(rule => { selectors.push(rule.selector) })
    expect(selectors.some(selector => selector.includes('.dark'))).toBe(false)
    expect(selectors).toContain('.prompt-modal')
  })

  it('compiles timeline backgrounds onto real component elements without ancestor-only global rules', () => {
    const { descriptor } = parse(timelineSource)
    const compiled = descriptor.styles.map(style => compileStyle({ source: style.content, filename: 'TimelineView.vue', id: 'data-v-timeline', scoped: true }))
    expect(compiled.every(style => style.errors.length === 0)).toBe(true)
    const css = postcss.parse(compiled.map(style => style.code).join('\n'))
    for (const selector of ['.timeline-editor', '.time-ruler', '.time-lane-heading']) {
      const rule = css.nodes.find(node => node.type === 'rule' && node.selector.includes(`${selector}[data-v-timeline]`)) as Rule
      expect(rule, selector).toBeDefined()
      expect(rule.nodes.some(node => node.type === 'decl' && node.prop === 'background' && node.value === 'var(--surface)')).toBe(true)
    }
    expect(timelineSource).not.toContain(':global(')
    expect(timelineSource).not.toContain('#f8f8f6')
  })

  it('removes chrome collection effects while preserving node and radar effects', () => {
    expect(styles).not.toContain('chrome-particle-absorb')
    expect(styles).not.toContain('chrome-absorb-fx')
    expect(styles).toContain('.tactical-ping')
    expect(styles).toContain('.magic-orbit')
  })
})
