import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getJobResult } from '../src/api/jobs'
import { eventPromptForShot, sequenceCards, sequencePrompts, sequenceSceneGroups } from '../src/generationSequences'
import { useWorkbenchStore } from '../src/stores/workbench'
import { makeSceneGroups } from '../src/graph'
import { encodeCanvasState, readCanvasState } from '../src/canvasPersistence'
import GenerationSequenceInspector from '../src/components/GenerationSequenceInspector.vue'
import GenerationSequenceNode from '../src/components/GenerationSequenceNode.vue'
import type { GenerationSequence, Shot, WorkbenchTask } from '../src/types'
import { connectionSources, resolveGenerator, validateGeneratorConnection, type GeneratorDraft } from '../src/generatorGraph'

vi.mock('../src/api/jobs', () => ({ getJobResult: vi.fn() }))

function sequence(job: string): GenerationSequence {
  return { id: `${job}-sequence-001`, event_id: `${job}-event-001`, scene_group_id: 'scene-001', cover_frame_id: `${job}-frame-2`, title: '饮用后反应', main_action: '男孩递杯，女子饮用后表情变化', source_shot_ids: [`${job}-shot-1`, `${job}-shot-2`], source_raw_shot_ids: ['raw-1', 'raw-2'], source_start_time: 0, source_end_time: 9, source_duration_seconds: 9, generation_duration_seconds: 11.5, status: 'ready', version: 1, build_hash: 'stable-hash', warnings: [],
    timeline: [1, 2].map((number, index) => ({ source_shot_id: `${job}-shot-${number}`, source_start_time: index * 4.5, source_end_time: (index + 1) * 4.5, generation_start_seconds: index * 5.75, generation_end_seconds: (index + 1) * 5.75, shot_size: '中景', camera_movement: '固定', source_edit_times: index ? [6] : [], generation_edit_times: index ? [7.667] : [], transition: index ? 'cut' : 'initial', action: index ? '女子饮用后皱眉' : '男孩递杯', initial_state: '持杯', final_state: '皱眉' })),
    first_frame_prompt: { id: `${job}-sequence-001-first-frame`, prompt_type: 'first_frame', platform: 'image', version: 1, positive_prompt: '片段自己的首帧，男孩递出杯子前', status: 'ready', contract: { purpose: '接杯并饮用', first_frame_state: '递杯前', generation_duration_seconds: 11.5 } },
    seedance_prompt: { id: `${job}-sequence-001-video`, prompt_type: 'video', platform: 'seedance', version: 1, positive_prompt: '片段自己的视频，0–5.75秒递杯，随后切到饮用与反应', status: 'ready', contract: { purpose: '接杯并饮用', end_state: '女子皱眉', source_shot_ids: [`${job}-shot-1`, `${job}-shot-2`] } } }
}
function shot(job: string, number: number): Shot {
  return { id: `${job}-shot-${number}`, number, time: `${(number - 1) * 4.5}s–${number * 4.5}s`, duration: '4.50s', action: number === 1 ? '递杯' : '饮用', shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .85, thumb: `/frame-${number}.png`, frames: [{ id: `${job}-frame-${number}`, url: `/frame-${number}.png`, timestamp: (number - 1) * 4.5 + .1, width: 768, height: 1366 }], analysisWarnings: [], sourceRawShotIds: [`raw-${number}`], sceneGroupId: 'scene-001', status: 'ready', actionDetails: { primarySubject: number === 1 ? '男孩' : '女子', initialState: '持杯', steps: ['动作'], interactionTarget: '杯子', finalState: '变化', emotionChange: '', evidence: [] }, spatialEvidence: { foreground: '', midground: '', background: '室内', relationships: '', observable: true } }
}
function task(job: string): WorkbenchTask {
  return { key: `task-${job}`, jobId: job, inputType: 'video', status: 'completed', stage: 'completed', progress: 100, error: null, videoFilename: `${job}.mp4`, provider: 'mock', model: 'mock', segmentationProfile: 'legacy-v1', rawShotCount: 2, narrativeShotCount: 2, shots: [shot(job, 1), shot(job, 2)], prompts: [{ id: `${job}-source-video`, shotId: `${job}-shot-1`, type: 'video', platform: 'seedance', version: 4, text: '保留用户编辑的来源 Prompt', status: 'ready', templateId: 'source', templateVersion: 1, warnings: [] }], rawShots: [], position: { x: 30, y: 50 }, generationSequences: [sequence(job)], narrativeEvents: [{ id: `${job}-event-001`, goal: '递杯与饮用反应', source_shot_ids: [`${job}-shot-1`, `${job}-shot-2`], initial_state: '男孩持杯', final_state: '女子皱眉', primary_subject: '男孩和女子' }], sceneGroups: [{ id: 'scene-001', order: 1, title: '室内饮用', shotIds: [`${job}-shot-1`, `${job}-shot-2`], coverFrameIds: [`${job}-frame-1`], startTime: 0, endTime: 9, warningCount: 0, groupingMode: 'semantic' }], sequenceMetadata: { mode: 'preview' } }
}
function backend(job: string) {
  const source = task(job)
  return { schema_version: 'storyboard.v1', job: { id: job, status: 'completed', stage: 'completed', progress: 100, script_text: '', raw_shot_count: 2, narrative_shot_count: 2 }, shots: source.shots.map(shot => ({ id: shot.id, number: shot.number, duration: 4.5, time: shot.time, action: shot.action, shot_size: shot.shotSize, lens: shot.lens, camera_movement: shot.movement, lighting_vibe: shot.lighting, confidence: shot.confidence, status: shot.status, scene_group_id: shot.sceneGroupId })), frames: source.shots.flatMap(shot => shot.frames.map(frame => ({ ...frame, shot_id: shot.id }))), prompts: [{ id: `${job}-source-video`, shot_id: `${job}-shot-1`, prompt_type: 'video', platform: 'seedance', version: 4, positive_prompt: '保留用户编辑的来源 Prompt', status: 'ready' }], generation_sequences: source.generationSequences, narrative_events: source.narrativeEvents, sequence_metadata: source.sequenceMetadata, scene_groups: source.sceneGroups?.map(group => ({ id: group.id, order: group.order, title: group.title, shot_ids: group.shotIds, cover_frame_ids: group.coverFrameIds, start_time: group.startTime, end_time: group.endTime, warning_count: 0, grouping_mode: 'semantic' })) }
}

describe('generation sequence presentation', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.mocked(getJobResult).mockImplementation(async job => backend(job) as never) })
  it('projects stable virtual cards and a separate sequence contract without changing source shots or prompts', () => {
    const source = task('a'); const before = JSON.stringify(source)
    const cards = sequenceCards(source); const prompts = sequencePrompts(source)
    expect(cards).toHaveLength(1); expect(cards[0].id).toBe('a-sequence-001')
    expect(cards[0].frames[0].id).toBe('a-frame-2')
    expect(prompts.map(prompt => prompt.text)).toEqual([sequence('a').first_frame_prompt!.positive_prompt, sequence('a').seedance_prompt!.positive_prompt])
    expect(prompts[0].contract).toBeUndefined(); expect(prompts[0].sequenceContract?.generation_duration_seconds).toBe(11.5)
    expect(JSON.stringify(source)).toBe(before)
    const groups = makeSceneGroups(cards, sequenceSceneGroups(source, cards))
    expect(groups).toHaveLength(1); expect(groups[0].id).toBe('scene-001'); expect(groups[0].title).toBe('室内饮用')
  })
  it('displays versioned event and script renderer profiles without rewriting their bodies', () => {
    const source = task('versioned')
    for (const version of ['event-agent-render.v4', 'script-sequence.v2', 'sequence-delivery.v2']) {
      const prompt = source.generationSequences![0].seedance_prompt!
      prompt.contract = { ...prompt.contract, delivery: { render_version: version } }
      const shown = sequencePrompts(source).find(item => item.type === 'video')!
      expect(shown.templateVersion).toBe(Number(version.split('.v')[1]))
      expect(shown.text).toBe(prompt.positive_prompt)
    }
  })
  it('features the complete merged event video prompt under its canonical ID without rewriting any source content', () => {
    const source = task('a')
    const video = source.generationSequences![0].seedance_prompt!
    video.positive_prompt = '完整事件：男孩递杯，女子饮用后皱眉。\n0–5.75秒：递杯与接杯。\n5.75–11.5秒：切到饮用，保留杯子与人物连续性。\n结束状态：女子皱眉。'
    video.negative_prompt = '无额外人物，无重复杯子，无缺失动作。'
    const before = JSON.stringify(source)
    const cards = sequenceCards(source)
    const prompts = sequencePrompts(source)
    const featured = eventPromptForShot(cards[0], prompts)
    expect(featured?.id).toBe(video.id)
    expect(featured?.shotId).toBe(source.generationSequences![0].id)
    expect(featured?.sequenceId).toBe(source.generationSequences![0].id)
    expect(featured?.text).toBe(video.positive_prompt)
    expect(featured?.negativeText).toBe(video.negative_prompt)
    expect(featured?.sequenceContract).toEqual(video.contract)
    expect(featured?.sequenceContract?.source_shot_ids).toEqual(['a-shot-1', 'a-shot-2'])
    expect(prompts.filter(prompt => prompt.eventSummary).map(prompt => prompt.id)).toEqual([video.id])
    expect(prompts.filter(prompt => prompt.id === video.id)).toHaveLength(1)
    expect(JSON.stringify(source)).toBe(before)
  })
  it('features only merged Seedance video prompts that have complete nonempty canonical content', () => {
    const source = task('a')
    const card = sequenceCards(source)[0]
    const prompts = sequencePrompts(source)
    const video = prompts.find(prompt => prompt.type === 'video')!
    expect(eventPromptForShot(source.shots[0], prompts)).toBeUndefined()
    expect(eventPromptForShot(card, [{ ...video, id: 'unrelated-video' }])).toBeUndefined()
    expect(eventPromptForShot(card, [{ ...video, type: 'first_frame' }])).toBeUndefined()
    expect(eventPromptForShot(card, [{ ...video, platform: 'kling' }])).toBeUndefined()
    expect(eventPromptForShot(card, [{ ...video, text: '  \n  ' }])).toBeUndefined()
    expect(prompts.find(prompt => prompt.type === 'first_frame')?.eventSummary).not.toBe(true)
    source.generationSequences![0].source_shot_ids = ['a-shot-1']
    const unmergedPrompts = sequencePrompts(source)
    expect(eventPromptForShot(sequenceCards(source)[0], unmergedPrompts)).toBeUndefined()
    expect(unmergedPrompts.some(prompt => prompt.eventSummary)).toBe(false)
    source.generationSequences![0].source_shot_ids = ['a-shot-1', 'a-shot-2']
    source.generationSequences![0].seedance_prompt!.positive_prompt = '  \n  '
    expect(sequencePrompts(source).some(prompt => prompt.eventSummary)).toBe(false)
  })
  it('shows the delivered v2 template and forwards its complete timed prompt without rewriting it', () => {
    const source = task('a')
    const video = source.generationSequences![0].seedance_prompt!
    video.positive_prompt = '主体：男孩与女子，固定道具为杯子。\n场景：厨房。\n视频规格（生成设置）：时长 6 秒；分辨率 1080p；横屏 16:9。\n0–2 秒：男孩递杯。\n2–4 秒：女子接稳杯子。\n4–6 秒：女子饮用。\n整体画面风格：皮克斯式角色三维动画。'
    video.contract = { ...video.contract, delivery: { render_version: 'sequence-delivery.v2' } }
    const projected = sequencePrompts(source).find(prompt => prompt.id === video.id)!
    expect(projected.templateId).toBe('sequence-delivery.v2')
    expect(projected.templateVersion).toBe(2)
    expect(projected.text).toBe(video.positive_prompt)
    expect(eventPromptForShot(sequenceCards(source)[0], sequencePrompts(source))?.text).toBe(video.positive_prompt)
    expect(sequencePrompts(task('legacy'))[0].templateId).toBe('generation-sequence.v1')
  })
  it('selects and removes featured event prompts through their existing task and canonical prompt IDs', async () => {
    const store = useWorkbenchStore()
    await store.loadJob('a'); await store.loadJob('b')
    const owner = store.tasks.find(item => item.jobId === 'a')!
    const featured = eventPromptForShot(sequenceCards(owner)[0], sequencePrompts(owner))!
    store.select(featured.id)
    expect(store.activeTaskKey).toBe(owner.key)
    expect(store.selectedPrompt?.id).toBe(sequence('a').seedance_prompt!.id)
    expect(store.selectedPrompt?.text).toBe(sequence('a').seedance_prompt!.positive_prompt)
    expect(store.graphData(store.selectedId)?.kind).toBe('prompt')
    expect(store.graphData(store.selectedId)?.title).toBe('完整事件 Prompt')
    expect(store.allPrompts.filter(prompt => prompt.id === featured.id)).toHaveLength(1)
    expect(connectionSources(store.allPrompts, store.connectionShots, [])[featured.id]?.kind).toBe('prompt-video')
    const removed = store.removeTask(owner.key)!
    expect(store.selectedId).toBe('')
    expect(store.activeTaskKey).toBe('task-b')
    expect(store.allPrompts.some(prompt => prompt.id === featured.id)).toBe(false)
    expect(store.allPrompts.some(prompt => prompt.id === 'b-sequence-001-video')).toBe(true)
    expect(removed.prompts[0].text).toBe('保留用户编辑的来源 Prompt')
    expect(removed.generationSequences![0].seedance_prompt!.positive_prompt).toBe(sequence('a').seedance_prompt!.positive_prompt)
  })
  it('always projects complete sequences and keeps source inspection isolated from the canvas', async () => {
    const store = useWorkbenchStore(); await store.loadJob('a'); await store.loadJob('b')
    expect(store.generationView).toBe('sequences'); expect(store.displayShots.map(shot => shot.id)).toEqual(['a-sequence-001', 'b-sequence-001'])
    expect(store.shots).toHaveLength(4); expect(store.prompts[1].text).toBe('保留用户编辑的来源 Prompt')
    store.select('b-sequence-001'); expect(store.activeTaskKey).toBe('task-b'); expect(store.graphData(store.selectedId)?.kind).toBe('sequence')
    store.setGenerationView('source'); expect(store.selectedId).toBe('b-shot-1'); expect(store.displayShots).toHaveLength(2)
    expect(store.displayShots.every(shot => !!shot.generationSequence)).toBe(true)
    store.select('b-shot-2'); store.setGenerationView('sequences'); expect(store.selectedId).toBe('b-sequence-001')
    store.select('a-sequence-001-video'); expect(store.activeTaskKey).toBe('task-a'); expect(store.selectedPrompt?.sequenceId).toBe('a-sequence-001')
    store.setGenerationView('source'); expect(store.selectedId).toBe('a-shot-1')
    expect(store.tasks).toHaveLength(2)
  })
  it('preserves virtual IDs, semantic scene membership and task positions through refresh and position persistence', async () => {
    const store = useWorkbenchStore(); await store.loadJob('a', { x: 123, y: 456 }); store.select('a-sequence-001')
    const before = store.taskCanvasShots(store.tasks[0]).map(shot => shot.id)
    const state = readCanvasState(encodeCanvasState({ jobKeys: { a: 'task-a' }, positions: { 'a-sequence-001': { x: 987, y: 654 }, 'a-shot-1': { x: 432, y: 876 } }, sceneUi: { 'task-a:scene:scene-001': { open: true, page: 1 } }, selectedId: store.selectedId, activeTaskKey: 'task-a', generationView: 'sequences' }))!
    await store.loadJob('a')
    expect(store.taskCanvasShots(store.tasks[0]).map(shot => shot.id)).toEqual(before)
    expect(store.tasks[0].position).toEqual({ x: 123, y: 456 }); expect(store.selectedId).toBe('a-sequence-001')
    expect(state.positions['a-sequence-001']).toEqual({ x: 987, y: 654 }); expect(state.positions['a-shot-1']).toEqual({ x: 432, y: 876 })
    expect(store.taskCanvasSceneGroups(store.tasks[0])[0].id).toBe('scene-001')
    expect(store.shots.every(shot => shot.duration === '4.50s')).toBe(true)
  })
  it('falls back to original shots and prompts when sequences are absent', async () => {
    const result = backend('old'); result.generation_sequences = undefined
    vi.mocked(getJobResult).mockResolvedValue(result as never)
    const store = useWorkbenchStore(); await store.loadJob('old')
    expect(store.hasGenerationSequences).toBe(false); expect(store.displayShots).toEqual(store.shots)
    expect(store.taskCanvasPrompts(store.tasks[0])).toEqual(store.tasks[0].prompts)
  })
  it('does not hide uncovered source shots when a partial sequence snapshot arrives', async () => {
    const result = backend('partial')
    result.generation_sequences![0].source_shot_ids = ['partial-shot-1']
    vi.mocked(getJobResult).mockResolvedValue(result as never)
    const store = useWorkbenchStore(); await store.loadJob('partial')
    expect(store.displayShots.map(shot => shot.id)).toEqual(['partial-shot-1', 'partial-shot-2'])
    expect(store.taskCanvasPrompts(store.tasks[0])).toEqual(store.tasks[0].prompts)
  })
  it('resolves typed sequence connections from the selected task without mixing another task or inventing a generated image', async () => {
    const store = useWorkbenchStore(); await store.loadJob('a'); await store.loadJob('b')
    const generator: GeneratorDraft = { id: 'gen-video', kind: 'video', position: { x: 0, y: 0 }, manualPrompt: '', imageAssetId: null, imageWidth: 0, imageHeight: 0 }
    const sources = connectionSources(store.allPrompts, store.connectionShots, [generator])
    const promptWire = validateGeneratorConnection({ source: 'b-sequence-001-video', sourceHandle: 'prompt-out', target: generator.id, targetHandle: 'prompt-in' }, [], [generator], sources)
    const coverWire = validateGeneratorConnection({ source: 'b-sequence-001', sourceHandle: 'frame-out', target: generator.id, targetHandle: 'image-in' }, [promptWire.connection!], [generator], sources)
    expect(promptWire.ok).toBe(true); expect(coverWire.ok).toBe(true)
    expect(validateGeneratorConnection({ source: 'b-sequence-001-video', sourceHandle: 'prompt-out', target: generator.id, targetHandle: 'image-in' }, [], [generator], sources).ok).toBe(false)
    const frames = Object.fromEntries(store.connectionShots.filter(shot => shot.frames.length).map(shot => [shot.id, { url: `${shot.id}/${shot.frames[0].id}`, width: shot.frames[0].width, height: shot.frames[0].height }]))
    const resolved = resolveGenerator(generator.id, [generator], [promptWire.connection!, coverWire.connection!], Object.fromEntries(store.allPrompts.map(prompt => [prompt.id, prompt.text])), frames, {})
    expect(resolved.prompt).toBe(sequence('b').seedance_prompt!.positive_prompt)
    expect(resolved.imageUrl).toBe('b-sequence-001/b-frame-2'); expect(resolved.ownImageUrl).toBe('')
    store.setGenerationView('source')
    expect(connectionSources(store.allPrompts, store.connectionShots, [generator])['b-sequence-001-video']).toBeDefined()
  })
  it('shows source counts and proposed duration without treating the cover as a generated image', async () => {
    const card = sequenceCards(task('a'))[0]
    const wrapper = mount(GenerationSequenceNode, { props: { data: { shot: card } }, global: { stubs: { Handle: true } } })
    expect(wrapper.text()).toContain('2 个来源镜头'); expect(wrapper.text()).toContain('11.50s 建议'); expect(wrapper.text()).toContain('9.00s')
    expect(wrapper.get('img').attributes('alt')).toContain('来源参考帧')
    await wrapper.trigger('click'); expect(wrapper.emitted('select')?.[0]).toEqual(['a-sequence-001']); wrapper.unmount()
  })
  it('counts an internal source cut within one member and deduplicates repeated joins and timestamps', async () => {
    const source = task('a'); const sequence = source.generationSequences![0]
    const first = { ...sequence.timeline[0], source_edit_times: [1.5] }
    sequence.timeline = [first]
    const wrapper = mount(GenerationSequenceNode, { props: { data: { shot: sequenceCards(source)[0] } }, global: { stubs: { Handle: true } } })
    expect(wrapper.get('.movement').text()).toContain('1 次内部切镜')
    sequence.timeline = [{ ...first, source_edit_times: [1.5, 1.5] }, { ...first, source_start_time: 1.5, transition: 'cut', source_edit_times: [1.5, 1.5000000001] }]
    await wrapper.setProps({ data: { shot: sequenceCards(source)[0] } })
    expect(wrapper.get('.movement').text()).toContain('1 次内部切镜')
    wrapper.unmount()
  })
  it('shows only the selected sequence prompts with the source timeline and drilldown', async () => {
    const source = task('a'); const wrapper = mount(GenerationSequenceInspector, { props: { sequence: sequence('a'), shots: source.shots, event: source.narrativeEvents![0], preview: true } })
    expect(wrapper.get('[data-testid="sequence-prompts"]').text()).not.toContain('保留用户编辑的来源 Prompt')
    expect(wrapper.findAll('textarea').map(textarea => textarea.element.value)).toEqual([sequence('a').first_frame_prompt!.positive_prompt, sequence('a').seedance_prompt!.positive_prompt])
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain('切镜 · 生成 5.75–11.5s')
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain('来源原切点：6s')
    expect(wrapper.text()).toContain('原片总时长'); expect(wrapper.text()).toContain('建议生成时长')
    await wrapper.get('[data-testid="sequence-timeline"] button').trigger('click'); expect(wrapper.emitted('sourceShot')?.[0]).toEqual(['a-shot-1'])
    await wrapper.get('[data-testid="sequence-prompts"] button').trigger('click'); expect(wrapper.emitted('copy')?.[0]).toEqual([sequence('a').first_frame_prompt!.positive_prompt])
    await flushPromises(); wrapper.unmount()
  })
  it('shows model-selected semantic phases and keeps their real sources and generation times distinct', async () => {
    const source = task('phases')
    const seq = source.generationSequences![0]
    seq.timeline[0].semantic_phase = '递交与接稳'
    seq.timeline[1].semantic_phase = '饮用与反馈'
    const wrapper = mount(GenerationSequenceInspector, { props: { sequence: seq, shots: source.shots } })
    expect(wrapper.findAll('[data-testid="semantic-phase"]').map(item => item.text()))
      .toEqual(['阶段 1 · 递交与接稳', '阶段 2 · 饮用与反馈'])
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain('生成 0–5.75s')
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain('来源 0–4.5s')
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain('不代表每张都要生成一次切镜')
    await wrapper.findAll('[data-testid="sequence-timeline"] button')[1].trigger('click')
    expect(wrapper.emitted('sourceShot')?.[0]).toEqual(['phases-shot-2'])
    expect(wrapper.findAll('textarea')[1].element.value).toBe(seq.seedance_prompt!.positive_prompt)
    seq.timeline.forEach(item => { delete item.semantic_phase })
    await wrapper.setProps({ sequence: { ...seq } })
    expect(wrapper.findAll('[data-testid="semantic-phase"]')).toHaveLength(0)
    wrapper.unmount()
  })
  it('shows partial relation status and actual merge decisions without implying all shots must be separate', () => {
    const source = task('a')
    const wrapper = mount(GenerationSequenceInspector, { props: { sequence: sequence('a'), shots: source.shots,
      metadata: { relation_analysis_status: 'partial', merge_decisions: [
        { left_shot_id: 'a-shot-1', right_shot_id: 'a-shot-2', merged: true, reason: '状态衔接且保留手部特写' },
        { left_shot_id: 'b-shot-1', right_shot_id: 'b-shot-2', merged: false, reason: '其他任务' },
      ] } } })
    expect(wrapper.get('[data-testid="merge-incomplete"]').text()).toContain('有效合并已保留')
    expect(wrapper.get('[data-testid="merge-decisions"]').text()).toContain('状态衔接且保留手部特写')
    expect(wrapper.text()).not.toContain('其他任务')
    wrapper.unmount()
  })
  it('keeps an Agent semantic stage and full event prompt while showing all of its sources', async () => {
    const source = task('a')
    const seq = source.generationSequences![0]
    seq.generation_mode = 'continuous'
    seq.timeline = [{ ...seq.timeline[0], source_shot_ids: ['a-shot-1', 'a-shot-2'],
      generation_end_seconds: 11.5, source_end_time: 9, source_edit_times: [4.5],
      action: '男孩递杯，女子接过并饮用，男孩保持注视' }]
    seq.seedance_prompt!.contract = { delivery: { render_version: 'event-agent-render.v2' } }
    expect(sequencePrompts(source).find(prompt => prompt.type === 'video')?.templateVersion).toBe(2)
    expect(sequencePrompts(source).find(prompt => prompt.type === 'video')?.text).toBe(seq.seedance_prompt!.positive_prompt)
    const wrapper = mount(GenerationSequenceInspector, { props: { sequence: seq, shots: source.shots } })
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain('1 STAGES')
    expect(wrapper.get('[data-testid="sequence-timeline"]').text()).toContain(seq.timeline[0].action)
    const buttons = wrapper.findAll('[data-testid="sequence-timeline"] button')
    expect(buttons).toHaveLength(2)
    await buttons[1].trigger('click')
    expect(wrapper.emitted('sourceShot')?.[0]).toEqual(['a-shot-2'])
    expect(wrapper.text()).toContain('不要求生成时逐次切镜')
    wrapper.unmount()
    const card = mount(GenerationSequenceNode, { props: { data: { shot: sequenceCards(source)[0] } }, global: { stubs: { Handle: true } } })
    expect(card.get('.movement').text()).toContain('一镜到底')
    expect(card.get('.movement').text()).not.toContain('内部切镜')
    card.unmount()
  })
})
