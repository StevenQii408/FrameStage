import { estimateCardSize, type CardLod } from '../../src/cardGeometry'
import { eventPromptSize } from '../../src/eventPromptLayout'
import { sequenceCards, sequenceMembers } from '../../src/generationSequences'
import { sequenceDeckSize, type SequenceBranch } from '../../src/sequenceBranchLayout'
import type { GenerationSequence, GenerationSequenceTimeline, Shot, WorkbenchTask } from '../../src/types'

export const SOURCE_COUNTS = [1, 10, 11, 30, 53] as const

/** Saved observations only: the second raw cut has no independently sampled frame. */
export function sequenceBranchTask(job: string, count: number, clipSizes = [count]): WorkbenchTask {
  if (count < 1 || clipSizes.some(size => size < 1) || clipSizes.reduce((sum, size) => sum + size, 0) !== count) {
    throw new Error('Fixture clips must partition every source exactly once')
  }
  const shots: Shot[] = Array.from({ length: count }, (_, index) => {
    const number = index + 1
    const portrait = index % 3 === 0
    return {
      id: `${job}-source-${number}`, number, time: `${index.toFixed(2)}s–${number.toFixed(2)}s`, duration: '1.00s',
      action: `来源观察 ${number}：${index < count / 2 ? '递杯与接稳' : '饮用后的反应'}`,
      observedAction: `已保存的真实来源动作 ${number}`, shotSize: index % 2 ? '特写' : '中景', lens: '标准',
      movement: '固定', lighting: '柔光', confidence: .9, thumb: `/fixtures/${job}-frame-${number}.png`,
      frames: [{ id: `${job}-frame-${number}`, url: `/fixtures/${job}-frame-${number}.png`, timestamp: index + .1,
        width: portrait ? 768 : 1366, height: portrait ? 1366 : 768 }],
      analysisWarnings: [], sourceRawShotIds: index === 0 ? [`${job}-raw-1a`, `${job}-raw-1b`] : [`${job}-raw-${number}`],
      sourceEditTimes: index === 0 ? [.4] : [], sceneGroupId: 'scene-shared-name', status: 'ready',
      actionDetails: { primarySubject: '女子与男孩', initialState: '持杯', steps: [`真实来源步骤 ${number}`],
        interactionTarget: '杯子', finalState: '完成当前动作', emotionChange: '', evidence: [] },
      spatialEvidence: { foreground: '', midground: '人物与杯子', background: '室内', relationships: '同一桌边', observable: true },
    }
  })
  let offset = 0
  const generationSequences: GenerationSequence[] = clipSizes.map((size, index) => {
    const members = shots.slice(offset, offset + size)
    const start = offset
    offset += size
    const id = `${job}-clip-${index + 1}`
    const split = size > 1 ? Math.ceil(size / 2) : size
    const phases = [members.slice(0, split), members.slice(split)].filter(phase => phase.length)
    const timeline: GenerationSequenceTimeline[] = phases.map((phase, phaseIndex) => ({
      source_shot_id: phase[0].id, source_shot_ids: phase.map(shot => shot.id),
      source_start_time: phase[0].number - 1, source_end_time: phase.at(-1)!.number,
      generation_start_seconds: phase[0].number - 1 - start, generation_end_seconds: phase.at(-1)!.number - start,
      shot_size: phaseIndex ? '特写' : '中景', camera_movement: '固定', source_edit_times: phase.flatMap(shot => shot.sourceEditTimes || []),
      generation_edit_times: [], transition: phaseIndex ? 'continuation' : 'initial',
      action: phaseIndex ? '女子饮用，保持人物与杯子连续性，必要时突出表情特写' : '男孩递杯，女子接稳杯子',
      initial_state: '持杯', final_state: phaseIndex ? '表情发生变化' : '接稳杯子',
    }))
    const text = timeline.map(phase => `${phase.generation_start_seconds}–${phase.generation_end_seconds}秒：${phase.action}`).join('\n')
    return {
      id, event_id: `${job}-event`, scene_group_id: 'scene-shared-name', cover_frame_id: members[0].frames[0].id,
      title: `完整饮用事件·片段 ${index + 1}`, main_action: '递杯、饮用与反应', source_shot_ids: members.map(shot => shot.id),
      source_raw_shot_ids: members.flatMap(shot => shot.sourceRawShotIds), source_start_time: start, source_end_time: offset,
      source_duration_seconds: size, generation_duration_seconds: size, generation_mode: 'continuous', timeline,
      first_frame_prompt: { id: `${id}-first-frame`, prompt_type: 'first_frame', platform: 'image', version: 1,
        positive_prompt: `${id}：递杯前的状态`, status: 'ready' },
      seedance_prompt: { id: `${id}-video`, prompt_type: 'video', platform: 'seedance', version: 1,
        positive_prompt: `${id} 的完整 Prompt\n${text}`, negative_prompt: '保持杯子与人物一致', status: 'ready',
        contract: { source_shot_ids: members.map(shot => shot.id), delivery: { render_version: 'event-agent-render.v2' } } },
      status: 'ready', version: 1, warnings: [],
    }
  })
  return {
    key: `task-${job}`, jobId: job, inputType: 'video', status: 'completed', stage: 'completed', progress: 100, error: null,
    videoFilename: `${job}.mp4`, provider: 'saved-fixture', model: 'saved-fixture', segmentationProfile: 'local-cuts',
    rawShotCount: count + 1, narrativeShotCount: count, shots,
    prompts: shots.map(shot => ({ id: `${shot.id}-video`, shotId: shot.id, type: 'video', platform: 'seedance', version: 7,
      text: `${shot.id}：用户编辑的来源 Prompt`, status: 'ready', templateId: 'source-v7', templateVersion: 7, warnings: [] })),
    rawShots: shots.flatMap(shot => shot.sourceRawShotIds.map((id, index) => {
      const first = shot.number === 1
      const startTime = first ? (index ? .4 : 0) : shot.number - 1
      const endTime = first ? (index ? 1 : .4) : shot.number
      return { id, number: first ? index + 1 : shot.number + 1, startTime, endTime, duration: endTime - startTime,
        boundaryMetrics: { local_score: .8 }, decision: index ? 'keep' : 'merge_next', reason: `保存的本地切点理由 ${id}`,
        narrativeShotId: shot.id, generationShotIds: generationSequences.filter(sequence => sequence.source_shot_ids.includes(shot.id)).map(sequence => sequence.id) }
    })),
    generationSequences,
    narrativeEvents: [{ id: `${job}-event`, goal: '完整递杯与饮用反应', source_shot_ids: shots.map(shot => shot.id),
      initial_state: '男孩持杯', final_state: '女子表情发生变化', primary_subject: '女子与男孩' }],
    sceneGroups: [{ id: 'scene-shared-name', order: 1, title: '室内饮用', shotIds: shots.map(shot => shot.id),
      coverFrameIds: [shots[0].frames[0].id], startTime: 0, endTime: count, warningCount: 0, groupingMode: 'semantic' }],
    sequenceMetadata: { mode: 'persisted' }, position: { x: 80, y: 120 },
  }
}

export function sequenceBranchFixtures(task: WorkbenchTask, open = true, lod: CardLod = 'detail'): SequenceBranch[] {
  return sequenceCards(task).map(card => ({
    id: card.id, open, coverSize: sequenceDeckSize(card, lod),
    sourceSizes: card.generationSequence!.source_shot_ids.length > 1
      ? sequenceMembers(card.generationSequence!, task.shots).map(source => estimateCardSize(source, lod)) : [],
    promptSize: eventPromptSize(lod),
  }))
}
