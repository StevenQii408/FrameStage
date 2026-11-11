import { describe, expect, it } from 'vitest'
import { planCanvasDeletion, type CanvasDeletionContext } from '../src/canvasSelection'

function context(): CanvasDeletionContext {
  return {
    roots: [
      { id: 'task-a', descendantIds: ['scene-a', 'shot-a', 'prompt-a', 'collapsed-source-a'] },
      { id: 'task-b', descendantIds: ['scene-b', 'shot-b', 'prompt-b'] },
    ],
    generatorIds: ['gen-image', 'gen-video'],
    nodeIds: ['task-a', 'task-b', 'scene-a', 'scene-b', 'shot-a', 'shot-b', 'prompt-a', 'prompt-b', 'gen-image', 'gen-video'],
  }
}

describe('batch canvas deletion planning', () => {
  it('removes a selected root and its entire system subtree, preserving manual generators', () => {
    const result = planCanvasDeletion(['task-a'], context())
    expect(result.taskIds).toEqual(['task-a'])
    expect(result.generatorIds).toEqual([])
    expect(result.hiddenNodeIds).toEqual([])
    expect(new Set(result.removedNodeIds)).toEqual(new Set(['task-a', 'scene-a', 'shot-a', 'prompt-a', 'collapsed-source-a']))
  })

  it('deduplicates selected descendants when their ancestor is selected', () => {
    const result = planCanvasDeletion(['shot-a', 'task-a', 'prompt-a', 'task-a'], context())
    expect(result.taskIds).toEqual(['task-a'])
    expect(result.hiddenNodeIds).toEqual([])
    expect(result.removedNodeIds).toHaveLength(5)
  })

  it('hides independent shot, scene and Prompt nodes without selecting their root', () => {
    const result = planCanvasDeletion(['shot-a', 'scene-b', 'prompt-a'], context())
    expect(result.taskIds).toEqual([])
    expect(result.generatorIds).toEqual([])
    expect(result.hiddenNodeIds).toEqual(['shot-a', 'scene-b', 'prompt-a'])
    expect(result.removedNodeIds).toEqual(result.hiddenNodeIds)
  })

  it('removes generators only when explicitly selected alongside roots', () => {
    const result = planCanvasDeletion(['task-a', 'gen-video', 'shot-b'], context())
    expect(result.taskIds).toEqual(['task-a'])
    expect(result.generatorIds).toEqual(['gen-video'])
    expect(result.hiddenNodeIds).toEqual(['shot-b'])
    expect(result.removedNodeIds).not.toContain('gen-image')
    expect(result.removedNodeIds).not.toContain('task-b')
  })

  it('does not cascade from a generator even if its output feeds another generator', () => {
    const result = planCanvasDeletion(['gen-image'], context())
    expect(result.removedNodeIds).toEqual(['gen-image'])
    expect(result.generatorIds).toEqual(['gen-image'])
  })

  it('ignores stale, invisible and non-node selections', () => {
    const result = planCanvasDeletion(['missing', 'collapsed-source-a', 'wire-a'], context())
    expect(result).toEqual({ taskIds: [], generatorIds: [], hiddenNodeIds: [], removedNodeIds: [] })
  })

  it('supports multiple roots and deduplicates the local deletion closure', () => {
    const result = planCanvasDeletion(['task-b', 'task-a', 'shot-a', 'shot-b', 'gen-image', 'gen-image'], context())
    expect(result.taskIds).toEqual(['task-b', 'task-a'])
    expect(result.generatorIds).toEqual(['gen-image'])
    expect(result.hiddenNodeIds).toEqual([])
    expect(result.removedNodeIds).toHaveLength(10)
    expect(new Set(result.removedNodeIds).size).toBe(result.removedNodeIds.length)
  })

  it('protects manually owned generators if a root descendant list accidentally includes them', () => {
    const source = context()
    source.roots = [{ id: 'task-a', descendantIds: ['shot-a', 'gen-image', 'gen-video'] }]
    expect(planCanvasDeletion(['task-a'], source).removedNodeIds).toEqual(['task-a', 'shot-a'])
    expect(planCanvasDeletion(['task-a', 'gen-image'], source).generatorIds).toEqual(['gen-image'])
  })

  it('accepts Sets and one-shot iterables without mutating the source selection', () => {
    const selected = new Set(['gen-video', 'shot-a'])
    const source = context()
    source.nodeIds = new Set(source.nodeIds)
    source.generatorIds = (function* () { yield 'gen-image'; yield 'gen-video' })()
    const result = planCanvasDeletion(selected.values(), source)
    expect(result.generatorIds).toEqual(['gen-video'])
    expect(result.hiddenNodeIds).toEqual(['shot-a'])
    expect([...selected]).toEqual(['gen-video', 'shot-a'])
  })
})
