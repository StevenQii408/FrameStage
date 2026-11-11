import { describe, expect, it } from 'vitest'
import { isBlankCanvasDoubleClick } from '../src/canvasInteraction'
import { readJobIds, writeJobIds } from '../src/urlState'

describe('multi-task URL state', () => {
  it('restores job_ids and remains compatible with legacy job_id', () => {
    expect(readJobIds('?job_ids=job-a,job-b,job-a')).toEqual(['job-a', 'job-b'])
    expect(readJobIds('?job_id=legacy')).toEqual(['legacy'])
    expect(writeJobIds('http://localhost/workbench?job_id=old#x', ['a', 'b'])).toBe('/workbench?job_ids=a%2Cb#x')
  })
})

describe('canvas double-click target', () => {
  it('accepts blank pane descendants but rejects nodes and the creation menu', () => {
    const pane = document.createElement('div'); pane.className = 'vue-flow__pane'
    const background = document.createElement('span'); pane.appendChild(background)
    const node = document.createElement('div'); node.className = 'vue-flow__node'; const nodeChild = document.createElement('span'); node.appendChild(nodeChild); pane.appendChild(node)
    const menu = document.createElement('div'); menu.className = 'create-node-menu'; const menuChild = document.createElement('span'); menu.appendChild(menuChild); pane.appendChild(menu)
    document.body.appendChild(pane)
    expect(isBlankCanvasDoubleClick(background)).toBe(true)
    expect(isBlankCanvasDoubleClick(nodeChild)).toBe(false)
    expect(isBlankCanvasDoubleClick(menuChild)).toBe(false)
  })
})
