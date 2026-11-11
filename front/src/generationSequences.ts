import type { EntityStatus, GenerationSequence, Prompt, SceneGroup, Shot, WorkbenchTask } from './types'

export function sequenceStatus(status: string): EntityStatus {
  if (['ready', 'warning', 'failed', 'cancelled', 'paused_memory_pressure', 'stale'].includes(status)) return status as EntityStatus
  return status === 'processing' ? 'processing' : 'queued'
}

export function sequenceMembers(sequence: GenerationSequence, shots: Shot[]): Shot[] {
  const byId = new Map(shots.map(shot => [shot.id, shot]))
  return sequence.source_shot_ids.map(id => byId.get(id)).filter((shot): shot is Shot => !!shot)
}

export function sequenceCards(task: WorkbenchTask): Shot[] {
  return (task.generationSequences || []).flatMap((sequence, index) => {
    const members = sequenceMembers(sequence, task.shots)
    const first = members[0]
    if (!first) return []
    const cover = members.flatMap(shot => shot.frames).find(frame => frame.id === sequence.cover_frame_id) || first.frames[0]
    const event = task.narrativeEvents?.find(item => item.id === sequence.event_id)
    return [{ ...first, id: sequence.id, number: index + 1,
      time: `${sequence.source_start_time.toFixed(2)}s–${sequence.source_end_time.toFixed(2)}s`,
      duration: sequence.generation_duration_seconds ? `${sequence.generation_duration_seconds.toFixed(2)}s` : '待设定',
      action: sequence.main_action || event?.goal || sequence.title || first.action,
      thumb: cover?.url || first.thumb, frames: cover ? [cover] : [],
      sceneGroupId: sequence.scene_group_id || first.sceneGroupId,
      analysisWarnings: sequence.warnings || [], status: sequenceStatus(sequence.status),
      generationDurationSeconds: sequence.generation_duration_seconds || undefined, generationSequence: sequence,
    }]
  })
}

export function sequencePrompts(task: WorkbenchTask): Prompt[] {
  return (task.generationSequences || []).flatMap(sequence => [sequence.first_frame_prompt, sequence.seedance_prompt].flatMap(prompt => {
    if (!prompt) return []
    const delivery = prompt.contract?.delivery as Record<string, unknown> | undefined
    const renderVersion = typeof delivery?.render_version === 'string' ? delivery.render_version : 'generation-sequence.v1'
    return [{
      id: prompt.id, shotId: sequence.id, sequenceId: sequence.id,
      type: prompt.prompt_type, platform: prompt.platform, version: prompt.version,
      text: prompt.positive_prompt, negativeText: prompt.negative_prompt,
      status: sequenceStatus(prompt.status), templateId: renderVersion, templateVersion: Number(renderVersion.match(/\.v(\d+)$/)?.[1] || 1),
      warnings: sequence.warnings || [], sequenceContract: prompt.contract,
      eventSummary: prompt.prompt_type === 'video' && prompt.platform.toLowerCase() === 'seedance'
        && sequence.source_shot_ids.length > 1 && Boolean(prompt.positive_prompt.trim()),
    }]
  }))
}

/** Reuse the canonical full sequence prompt: no aliases, concatenation or inference. */
export function eventPromptForShot(shot: Shot, prompts: Prompt[]): Prompt | undefined {
  const sequence = shot.generationSequence
  if (!sequence || sequence.source_shot_ids.length < 2) return undefined
  return sequencePromptForShot(shot, prompts)
}

/** Every sequence branch ends with its canonical total prompt, including single-source clips. */
export function sequencePromptForShot(shot: Shot, prompts: Prompt[]): Prompt | undefined {
  const sequence = shot.generationSequence
  if (!sequence?.seedance_prompt?.id) return undefined
  return prompts.find(prompt => prompt.id === sequence.seedance_prompt!.id && prompt.shotId === shot.id
    && prompt.type === 'video' && prompt.platform.toLowerCase() === 'seedance' && Boolean(prompt.text.trim()))
}

export type SequenceUi = Record<string, { open: boolean }>

/** Old scene expansion follows its clips once; explicit per-clip choices always win. */
export function migrateSequenceUi(task: WorkbenchTask, sceneUi: Record<string, { open: boolean }>, sequenceUi: SequenceUi): SequenceUi {
  const result = { ...sequenceUi }
  for (const shot of sequenceCards(task)) {
    if (Object.prototype.hasOwnProperty.call(result, shot.id)) continue
    const scene = sceneUi[`${task.key}:scene:${shot.sceneGroupId}`]
    if (scene) result[shot.id] = { open: scene.open && shot.generationSequence!.source_shot_ids.length > 1 }
  }
  return result
}

/** Map semantic membership for display, while retaining the source scene identity. */
export function sequenceSceneGroups(task: WorkbenchTask, cards: Shot[]): SceneGroup[] {
  return (task.sceneGroups || []).map(group => ({ ...group, shotIds: cards.filter(card => card.sceneGroupId === group.id).map(card => card.id) }))
}
