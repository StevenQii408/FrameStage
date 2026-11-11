import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import GenerationSequenceInspector from '../src/components/GenerationSequenceInspector.vue'
import ShotInspector from '../src/components/ShotInspector.vue'
import { encodeCanvasState, readCanvasState } from '../src/canvasPersistence'
import { eventPromptForShot, migrateSequenceUi, sequenceCards, sequenceMembers, sequencePromptForShot, sequencePrompts } from '../src/generationSequences'
import { connectionSources, detachConnectionsFromSources, resolveGenerator, validateGeneratorConnection, type GeneratorDraft } from '../src/generatorGraph'
import { layoutSequenceBranches, SEQUENCE_BRANCH_GAP, SEQUENCE_ROW_GAP, SEQUENCE_SOURCES_PER_ROW, type SequenceBranch, type SequenceBranchPlacement } from '../src/sequenceBranchLayout'
import type { CanvasRect } from '../src/sceneLayout'
import { useWorkbenchStore } from '../src/stores/workbench'
import { SOURCE_COUNTS, sequenceBranchFixtures, sequenceBranchTask } from './fixtures/sequenceBranches'

function rectangles(branch: SequenceBranch, layout: SequenceBranchPlacement): CanvasRect[] {
  return [
    { ...layout.cover, ...branch.coverSize },
    ...(branch.open ? layout.sources.map((position, index) => ({ ...position, ...branch.sourceSizes[index] })) : []),
    ...(layout.prompt && branch.promptSize ? [{ ...layout.prompt, ...branch.promptSize }] : []),
  ]
}

function assertDisjoint(rects: CanvasRect[]) {
  for (let index = 0; index < rects.length; index++) for (const other of rects.slice(index + 1)) {
    const rect = rects[index]
    expect(rect.x < other.x + other.width && rect.x + rect.width > other.x
      && rect.y < other.y + other.height && rect.y + rect.height > other.y).toBe(false)
  }
}

describe('sequence branch regression fixtures', () => {
  it.each(SOURCE_COUNTS)('lays out %i original sources in four-card rows with a visible final Prompt', count => {
    const task = sequenceBranchTask(`count-${count}`, count)
    const branch = sequenceBranchFixtures(task)[0]
    const original = JSON.stringify(task)
    const result = layoutSequenceBranches(task.position, [branch])[branch.id]
    expect(SEQUENCE_SOURCES_PER_ROW).toBe(4)
    expect(result.sources).toHaveLength(count > 1 ? count : 0)
    expect(result.prompt).toBeDefined()
    expect(new Set(result.sources.map(point => `${point.x}:${point.y}`)).size).toBe(branch.sourceSizes.length)
    expect(new Set(result.sources.map(point => point.y)).size).toBe(Math.ceil(branch.sourceSizes.length / 4))
    for (let offset = 0; offset < result.sources.length; offset += 4) {
      expect(new Set(result.sources.slice(offset, offset + 4).map(point => point.y)).size).toBe(1)
      if (offset + 4 < result.sources.length) {
        const rowHeight = Math.max(...branch.sourceSizes.slice(offset, offset + 4).map(size => size.height))
        expect(result.sources[offset + 4].y - result.sources[offset].y).toBe(rowHeight + SEQUENCE_ROW_GAP)
      }
    }
    assertDisjoint(rectangles(branch, result))
    const tail = result.sources.at(-1) || result.cover
    expect(result.prompt!.x).toBeGreaterThan(tail.x)
    expect(JSON.stringify(task)).toBe(original)
  })

  it.each(['overview', 'strip', 'detail'] as const)('keeps the complete Prompt visible when a %s branch closes', lod => {
    const task = sequenceBranchTask('collapsed', 11)
    const [opened] = sequenceBranchFixtures(task, true, lod)
    const closed = { ...opened, open: false }
    const openLayout = layoutSequenceBranches(task.position, [opened])[opened.id]
    const closedLayout = layoutSequenceBranches(task.position, [closed])[closed.id]
    expect(closedLayout.cover).toEqual(openLayout.cover)
    expect(closedLayout.sources).toHaveLength(11)
    expect(closedLayout.prompt).toBeDefined()
    expect(closedLayout.bounds.height).toBeLessThan(openLayout.bounds.height)
    assertDisjoint(rectangles(closed, closedLayout))
    expect(rectangles(closed, closedLayout)).toHaveLength(2)
  })

  it('keeps separate clips of one event independently addressable and avoids another task', () => {
    const task = sequenceBranchTask('multi-clips', 11, [4, 6, 1])
    const other = sequenceBranchTask('other', 10)
    const branches = sequenceBranchFixtures(task)
    const otherBranch = sequenceBranchFixtures(other, false)[0]
    const occupiedLayout = layoutSequenceBranches({ x: 0, y: 0 }, [otherBranch])[otherBranch.id]
    const obstacles = rectangles(otherBranch, occupiedLayout)
    const before = JSON.stringify(obstacles)
    const layout = layoutSequenceBranches(task.position, branches, obstacles)
    expect(new Set(task.generationSequences!.map(sequence => sequence.event_id)).size).toBe(1)
    expect(Object.keys(layout)).toEqual(task.generationSequences!.map(sequence => sequence.id))
    expect(Object.values(layout).map(branch => branch.sources.length)).toEqual([4, 6, 0])
    assertDisjoint([...branches.flatMap(branch => rectangles(branch, layout[branch.id])), ...obstacles])
    expect(layout[branches[1].id].bounds.y).toBeGreaterThanOrEqual(layout[branches[0].id].bounds.y + layout[branches[0].id].bounds.height + SEQUENCE_BRANCH_GAP)
    expect(JSON.stringify(obstacles)).toBe(before)
  })

  it('preserves dragged positions while moving only automatic sources and Prompt around occupied space', () => {
    const task = sequenceBranchTask('manual', 11)
    const [branch] = sequenceBranchFixtures(task)
    branch.coverPosition = { x: 300, y: 200 }
    branch.sourcePositions = [{ x: 1700, y: 300 }]
    const obstacles = [{ x: 600, y: 200, width: 760, height: 470 }]
    const before = JSON.stringify({ branch, obstacles })
    const placement = layoutSequenceBranches(task.position, [branch], obstacles)[branch.id]
    expect(placement.cover).toEqual(branch.coverPosition)
    expect(placement.sources[0]).toEqual(branch.sourcePositions[0])
    expect(placement.sources[1].y).toBeGreaterThan(obstacles[0].y + obstacles[0].height)
    assertDisjoint([...rectangles(branch, placement), ...obstacles])
    expect(JSON.stringify({ branch, obstacles })).toBe(before)
  })
})

describe('branch source selection and canonical wires', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('uses one single-source clip and its canonical total Prompt without manufacturing an extra source card', () => {
    const task = sequenceBranchTask('single', 1)
    const card = sequenceCards(task)[0]
    const original = JSON.stringify(task)
    const prompts = sequencePrompts(task)
    expect(sequencePromptForShot(card, prompts)?.id).toBe(task.generationSequences![0].seedance_prompt!.id)
    expect(sequencePromptForShot(card, prompts)?.text).toBe(task.generationSequences![0].seedance_prompt!.positive_prompt)
    expect(sequenceBranchFixtures(task)[0].sourceSizes).toEqual([])
    expect(eventPromptForShot(card, prompts)).toBeUndefined()
    expect(sequencePromptForShot(task.shots[0], prompts)).toBeUndefined()
    const store = useWorkbenchStore()
    store.restoreTask(task)
    expect(store.taskCanvasShots(task).map(shot => shot.id)).toEqual([card.id])
    store.select(task.generationSequences![0].seedance_prompt!.id)
    expect(store.activeTaskKey).toBe(task.key)
    expect(store.selectedPrompt?.shotId).toBe(card.id)
    expect(JSON.stringify(task)).toBe(original)
  })

  it('migrates the old scene expansion once, persists individual clip choices and keeps task identities isolated', () => {
    const task = sequenceBranchTask('migration', 11, [4, 6, 1])
    const other = sequenceBranchTask('other', 10)
    const sceneUi = { [`${task.key}:scene:scene-shared-name`]: { open: true, page: 1 } }
    const existing = { [task.generationSequences![1].id]: { open: false } }
    const migrated = migrateSequenceUi(task, sceneUi, existing)
    expect(migrated).toEqual({
      [task.generationSequences![0].id]: { open: true },
      [task.generationSequences![1].id]: { open: false },
      [task.generationSequences![2].id]: { open: false },
    })
    expect(migrateSequenceUi(other, sceneUi, migrated)).toEqual(migrated)
    expect(existing).toEqual({ [task.generationSequences![1].id]: { open: false } })
    const selectedId = task.shots[6].id
    const state = readCanvasState(encodeCanvasState({ jobKeys: { migration: task.key }, positions: { [selectedId]: { x: 1250, y: 700 } },
      sceneUi, sequenceUi: migrated, selectedId, activeTaskKey: task.key }))!
    expect(state.sequenceUi).toEqual(migrated)
    expect(state.positions[selectedId]).toEqual({ x: 1250, y: 700 })
    expect(state.selectedId).toBe(selectedId)
    expect(migrateSequenceUi(task, {}, state.sequenceUi!)).toEqual(migrated)
    const malformed = readCanvasState(JSON.stringify({ sequenceUi: { valid: { open: true }, bad: { open: 'true' }, empty: null } }))!
    expect(malformed.sequenceUi).toEqual({ valid: { open: true } })
  })

  it('selects actual analyzed sources with their local raw cuts, without constructing raw-cut observations', async () => {
    const task = sequenceBranchTask('source-trace', 10)
    const store = useWorkbenchStore()
    store.restoreTask(task)
    const sequence = task.generationSequences![0]
    const wrapper = mount(GenerationSequenceInspector, { props: { sequence, shots: task.shots } })
    const buttons = wrapper.findAll('[data-testid="sequence-timeline"] button')
    expect(buttons).toHaveLength(10)
    await buttons[0].trigger('click')
    const sourceId = wrapper.emitted('sourceShot')![0][0] as string
    store.select(sourceId)
    expect(store.graphData(sourceId)?.kind).toBe('shot')
    expect(store.selectedShot?.id).toBe(task.shots[0].id)
    expect(store.selectedShot?.generationSequence).toBeUndefined()
    expect(store.selectedShot?.action).toBe(task.shots[0].action)
    expect(store.selectedShot?.frames.map(frame => frame.id)).toEqual(['source-trace-frame-1'])
    expect(task.rawShots.filter(raw => raw.narrativeShotId === sourceId)).toHaveLength(2)
    expect(sequenceMembers(sequence, task.shots)).toHaveLength(10)
    const inspector = mount(ShotInspector, { props: { shot: store.selectedShot!, rawShots: task.rawShots.filter(raw => raw.narrativeShotId === sourceId), prompts: task.prompts } })
    expect(inspector.text()).toContain('source-trace-raw-1b')
    expect(inspector.text()).toContain('保存的本地切点理由')
    expect(inspector.findAll('.frame-strip button')).toHaveLength(1)
    expect(wrapper.emitted('sourceShot')!.flat()).not.toContain('source-trace-raw-1b')
    wrapper.unmount(); inspector.unmount()
  })

  it('deduplicates stage references and exposes only real members of the selected clip', async () => {
    const task = sequenceBranchTask('phase-sources', 10, [4, 6])
    const other = sequenceBranchTask('other-stage', 1)
    const sequence = task.generationSequences![1]
    const stage = sequence.timeline[0]
    stage.semantic_phase = '饮用与反馈'
    stage.source_shot_ids = [task.shots[6].id, task.shots[6].id, task.shots[0].id, other.shots[0].id, 'missing-source', task.rawShots[0].id]
    sequence.timeline = [stage]
    const wrapper = mount(GenerationSequenceInspector, { props: { sequence, shots: [...task.shots, ...other.shots] } })
    expect(wrapper.get('[data-testid="semantic-phase"]').text()).toContain('饮用与反馈')
    const buttons = wrapper.findAll('[data-testid="sequence-timeline"] button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0].text()).toContain('来源镜头 7')
    await buttons[0].trigger('click')
    expect(wrapper.emitted('sourceShot')).toEqual([[task.shots[6].id]])
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain(stage.action)
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain(`生成 ${stage.generation_start_seconds}–${stage.generation_end_seconds}s`)
    wrapper.unmount()
  })

  it('deletes and restores canonical source/sequence wires without touching another task or edited source Prompt', () => {
    const a = sequenceBranchTask('a', 10, [4, 6])
    const b = sequenceBranchTask('b', 1)
    const store = useWorkbenchStore()
    store.restoreTask(a); store.restoreTask(b)
    const generators: GeneratorDraft[] = ['a', 'b'].map(job => ({ id: `gen-${job}`, kind: 'video', position: { x: 0, y: 0 }, manualPrompt: '', imageAssetId: null, imageWidth: 0, imageHeight: 0 }))
    const sources = connectionSources(store.allPrompts, store.connectionShots, generators)
    const promptId = a.generationSequences![1].seedance_prompt!.id
    const sourceId = a.shots[3].id
    const wires = [
      validateGeneratorConnection({ source: promptId, sourceHandle: 'prompt-out', target: 'gen-a', targetHandle: 'prompt-in' }, [], generators, sources).connection!,
      validateGeneratorConnection({ source: sourceId, sourceHandle: 'frame-out', target: 'gen-a', targetHandle: 'image-in' }, [], generators, sources).connection!,
      validateGeneratorConnection({ source: b.generationSequences![0].seedance_prompt!.id, sourceHandle: 'prompt-out', target: 'gen-b', targetHandle: 'prompt-in' }, [], generators, sources).connection!,
    ]
    expect(wires.every(Boolean)).toBe(true)
    store.select(promptId)
    expect(store.activeTaskKey).toBe(a.key)
    const ids = new Set([a.key, ...a.shots.map(shot => shot.id), ...a.prompts.map(prompt => prompt.id), ...sequenceCards(a).map(shot => shot.id), ...sequencePrompts(a).map(prompt => prompt.id)])
    const { kept, detached } = detachConnectionsFromSources(wires, ids)
    const removed = store.removeTask(a.key)!
    expect(kept).toHaveLength(1); expect(detached).toHaveLength(2)
    expect(store.selectedId).toBe(''); expect(store.activeTaskKey).toBe(b.key)
    expect(connectionSources(store.allPrompts, store.connectionShots, generators)[sourceId]).toBeUndefined()
    store.restoreTask(removed, 0)
    const resolved = resolveGenerator('gen-a', generators, [...kept, ...detached], Object.fromEntries(store.allPrompts.map(prompt => [prompt.id, prompt.text])), Object.fromEntries(store.connectionShots.map(shot => [shot.id, shot.frames[0]])), {})
    expect(resolved.prompt).toBe(a.generationSequences![1].seedance_prompt!.positive_prompt)
    expect(resolved.imageUrl).toBe(a.shots[3].frames[0].url)
    expect(store.tasks[0].prompts.map(prompt => prompt.text)).toEqual(a.prompts.map(prompt => prompt.text))
    expect(store.allPrompts.filter(prompt => prompt.id === promptId)).toHaveLength(1)
    expect(eventPromptForShot(sequenceCards(a)[1], sequencePrompts(a))?.id).toBe(promptId)
  })
})
