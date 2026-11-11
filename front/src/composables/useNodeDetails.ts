import { computed, shallowRef, watchEffect } from 'vue'
import { sequenceCards, sequencePrompts } from '../generationSequences'
import type { DetailScene, DetailTab, DetailTarget } from '../nodeDetails'
import type { WorkbenchTask } from '../types'

interface Context {
  projectId: () => string
  tasks: () => WorkbenchTask[]
  scene: (id: string, taskKey: string) => DetailScene | undefined
}
interface OpenOptions { taskKey?: string; tab?: DetailTab; returnSequenceId?: string }

/** Details have an explicit owner; selection and activeTask never retarget them. */
export function useNodeDetails(context: Context) {
  const target = shallowRef<DetailTarget | null>(null)
  const tab = shallowRef<DetailTab>('overview')
  function describe(id: string, options: OpenOptions = {}): DetailTarget | null {
    for (const task of context.tasks()) {
      if (options.taskKey && task.key !== options.taskKey) continue
      const sequence = task.generationSequences?.find(item => item.id === id)
      const shot = task.shots.find(item => item.id === id)
      const prompt = [...task.prompts, ...sequencePrompts(task)].find(item => item.id === id)
      const scene = id.startsWith(`${task.key}:scene:`) ? context.scene(id, task.key) : undefined
      const kind = task.key === id ? 'task' : sequence ? 'sequence' : shot ? 'shot' : prompt ? 'prompt' : scene ? 'scene' : null
      if (!kind) continue
      const owner = options.returnSequenceId || (shot && task.generationSequences?.find(item => item.source_shot_ids.includes(id))?.id)
      return { projectId: context.projectId(), taskKey: task.key, jobId: task.jobId, kind, id, ...(owner ? { returnSequenceId: owner } : {}) }
    }
    return null
  }
  const resolved = computed(() => {
    const value = target.value
    if (!value || value.projectId !== context.projectId()) return null
    const task = context.tasks().find(item => item.key === value.taskKey && (!value.jobId || item.jobId === value.jobId))
    if (!task || !describe(value.id, { taskKey: task.key })) return null
    const sequence = task.generationSequences?.find(item => item.id === value.id)
    const shot = task.shots.find(item => item.id === value.id) || sequenceCards(task).find(item => item.id === value.id)
    const prompt = [...task.prompts, ...sequencePrompts(task)].find(item => item.id === value.id)
    const rawIds = new Set(shot?.sourceRawShotIds || [])
    return { task, shot, sequence, prompt, scene: context.scene(value.id, task.key),
      rawShots: task.rawShots.filter(item => rawIds.has(item.id) || item.narrativeShotId === shot?.id) }
  })
  function open(id: string, options: OpenOptions = {}) {
    const next = describe(id, options)
    if (!next) return false
    target.value = next
    tab.value = options.tab || 'overview'
    return true
  }
  function close() { target.value = null }
  // A deleted object/project cannot leave a detached, misleading detail window.
  // Observe the target as well: opening and invalidating it in one tick can
  // otherwise leave the computed value null -> null and skip a plain watch.
  watchEffect(() => { if (target.value && !resolved.value) close() }, { flush: 'post' })
  return { target, tab, resolved, describe, open, close }
}
