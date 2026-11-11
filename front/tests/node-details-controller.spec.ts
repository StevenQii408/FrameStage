import { effectScope, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import { useNodeDetails } from '../src/composables/useNodeDetails'
import { sequenceBranchTask } from './fixtures/sequenceBranches'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })
function context() {
  const tasks = ref([sequenceBranchTask('owner-a', 2, [2]), sequenceBranchTask('owner-b', 1)])
  const project = ref('project-a')
  const scope = effectScope(); scopes.push(scope)
  const details = scope.run(() => useNodeDetails({ projectId: () => project.value, tasks: () => tasks.value,
    scene: (id, taskKey) => id === `${taskKey}:scene:legacy` ? { id, title: '历史展示分组', shotIds: tasks.value.find(task => task.key === taskKey)?.shots.map(shot => shot.id) || [] } : undefined }))!
  return { tasks, project, details }
}

describe('scoped node details controller', () => {
  it('starts closed, resolves raw and derived objects without an activeTask fallback', () => {
    const { tasks, details } = context(), a = tasks.value[0], b = tasks.value[1]
    expect(details.target.value).toBeNull()
    expect(details.open(a.generationSequences![0].id, { taskKey: b.key })).toBe(false)
    expect(details.open(a.generationSequences![0].id, { taskKey: a.key })).toBe(true)
    expect(details.resolved.value?.task.key).toBe(a.key)
    expect(details.resolved.value?.sequence?.source_shot_ids).toEqual(a.shots.map(shot => shot.id))
  })
  it('retains source return context and reads refreshed snapshots from the same owner', () => {
    const { tasks, details } = context(), a = tasks.value[0], sequenceId = a.generationSequences![0].id
    details.open(a.shots[0].id, { taskKey: a.key, tab: 'evidence', returnSequenceId: sequenceId })
    expect(details.target.value).toMatchObject({ taskKey: a.key, returnSequenceId: sequenceId })
    expect(details.tab.value).toBe('evidence')
    tasks.value = tasks.value.map(task => task.key === a.key ? { ...task, shots: task.shots.map(shot => ({ ...shot, action: '新快照' })) } : task)
    expect(details.resolved.value?.shot?.action).toBe('新快照')
    details.open(sequenceId, { taskKey: a.key })
    expect(details.target.value?.kind).toBe('sequence')
  })
  it('supports task reports, canonical Prompt and legacy scene presentation', () => {
    const { tasks, details } = context(), a = tasks.value[0]
    details.open(a.key)
    expect(details.target.value?.kind).toBe('task')
    const id = a.generationSequences![0].seedance_prompt!.id
    expect(details.open(id, { taskKey: a.key })).toBe(true)
    expect(details.resolved.value?.prompt?.id).toBe(id)
    details.open(`${a.key}:scene:legacy`, { taskKey: a.key })
    expect(details.resolved.value?.scene?.shotIds).toEqual(a.shots.map(shot => shot.id))
  })
  it('closes stale project or deleted-object windows and never modifies the analysis', async () => {
    const { tasks, project, details } = context(), before = JSON.stringify(tasks.value)
    details.open(tasks.value[0].shots[0].id)
    details.close()
    expect(JSON.stringify(tasks.value)).toBe(before)
    details.open(tasks.value[0].key)
    project.value = 'project-b'; await nextTick()
    expect(details.target.value).toBeNull()
    project.value = 'project-a'
    details.open(tasks.value[0].key)
    tasks.value.shift(); await nextTick()
    expect(details.target.value).toBeNull()
  })
})
