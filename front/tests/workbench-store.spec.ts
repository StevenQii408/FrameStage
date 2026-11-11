import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getJobResult } from '../src/api/jobs'
import { useWorkbenchStore } from '../src/stores/workbench'

vi.mock('../src/api/jobs', () => ({ getJobResult: vi.fn() }))

function result(id: string) {
  return {
    schema_version: 'storyboard.v1',
    job: { id, status: 'processing', stage: 'analyzing', progress: 42, input_type: 'video', video_filename: `${id}.mp4`, script_text: '', provider: 'mlx', model: 'Qwen2.5-VL' },
    shots: [{ id: `${id}-shot-1`, number: 1, start_time: 0, end_time: 2, duration: 2, time: '00 — 02', action: '城市航拍', shot_size: '大远景', lens: '超广角', camera_movement: '航拍', lighting_vibe: '日落', confidence: 0.8, status: 'ready', analysis_warnings: [] }],
    frames: [{ id: `${id}-frame-1`, shot_id: `${id}-shot-1`, timestamp: 0.2, width: 768, height: 1366 }],
    prompts: [],
  }
}

describe('workbench multi-task state', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.mocked(getJobResult).mockImplementation(async (id) => result(id) as never) })

  it('retains a static description and draft video rather than displaying a permanently queued prompt', async () => {
    const source = result('partial')
    const payload = { ...source, shots: [{ ...source.shots[0], action: '未知',
      visual_facts: { scene: '厨房台面旁' }, action_details: { primary_subject: '女子' },
      analysis_review: { static: { status: 'ready' }, dynamic: { status: 'pending_review' }, video_ready: false } }],
      prompts: [{ id: 'partial-video', shot_id: source.shots[0].id, prompt_type: 'video',
        platform: 'seedance', version: 8, positive_prompt: '场景：厨房台面旁。', negative_prompt: '',
        status: 'draft', template_id: 'seedance-video-v8', template_version: 8 }] }
    vi.mocked(getJobResult).mockResolvedValueOnce(payload as never)
    const store = useWorkbenchStore()
    await store.loadJob('partial')
    expect(store.tasks[0].shots[0].action).toBe('女子；厨房台面旁')
    expect(store.tasks[0].shots[0].analysisReview?.dynamic?.status).toBe('pending_review')
    expect(store.tasks[0].prompts[0].status).toBe('draft')
  })

  it('isolates current perception phase messages by task', async () => {
    const store = useWorkbenchStore()
    await store.loadJob('job-a'); await store.loadJob('job-b')
    store.applyEvent('job-a', 'perception.stage', { state: 'running', message: '分镜 1 · 动态字段格式纠正' })
    expect(store.tasks[0].stage).toContain('动态字段格式纠正')
    expect(store.tasks[1].stage).toBe('analyzing')
  })

  it('ignores an older in-flight response after the completed snapshot has arrived', async () => {
    const store = useWorkbenchStore()
    await store.loadJob('job-race')
    let resolveOlder!: (value: unknown) => void
    vi.mocked(getJobResult).mockImplementationOnce(() => new Promise(resolve => { resolveOlder = resolve as never }) as never)
    const older = store.loadJob('job-race')
    const completed = result('job-race'); completed.job.status = 'completed'; completed.job.progress = 100
    vi.mocked(getJobResult).mockResolvedValueOnce(completed as never)
    await store.loadJob('job-race')
    resolveOlder(result('job-race')); await older
    expect(store.tasks[0].status).toBe('completed')
    expect(store.tasks[0].progress).toBe(100)
  })

  it('never resurrects a removed task when its pending snapshot resolves', async () => {
    const store = useWorkbenchStore(); await store.loadJob('job-removed')
    let resolve!: (value: unknown) => void
    vi.mocked(getJobResult).mockImplementationOnce(() => new Promise(done => { resolve = done as never }) as never)
    const pending = store.loadJob('job-removed')
    store.removeTask('task-job-removed')
    resolve(result('job-removed'))
    await expect(pending).rejects.toThrow('快照已失效')
    expect(store.tasks).toHaveLength(0)
  })

  it('invalidates a snapshot older than the completion event even without a newer request', async () => {
    const store = useWorkbenchStore(); await store.loadJob('job-terminal')
    let resolve!: (value: unknown) => void
    vi.mocked(getJobResult).mockImplementationOnce(() => new Promise(done => { resolve = done as never }) as never)
    const pending = store.loadJob('job-terminal')
    store.applyEvent('job-terminal', 'job.completed', { progress: 100 })
    resolve(result('job-terminal')); await pending
    expect(store.tasks[0].status).toBe('completed'); expect(store.tasks[0].progress).toBe(100)
  })

  it('keeps task positions across refreshes and isolates task events', async () => {
    const store = useWorkbenchStore()
    await store.loadJob('job-a', { x: 123, y: 456 })
    await store.loadJob('job-b', { x: 80, y: 900 })
    store.updateTaskPosition('task-job-a', { x: 321, y: 654 })
    await store.loadJob('job-a')
    expect(store.tasks.find((task) => task.jobId === 'job-a')?.position).toEqual({ x: 321, y: 654 })
    store.applyEvent('job-a', 'job.completed', { progress: 100 })
    expect(store.tasks.find((task) => task.jobId === 'job-a')?.status).toBe('completed')
    expect(store.tasks.find((task) => task.jobId === 'job-b')?.status).toBe('processing')
    expect(store.shots).toHaveLength(2)
    expect(store.shots[0].frames[0]).toMatchObject({ width: 768, height: 1366 })
  })

  it('starts empty and deleting a root cascades through its shots and prompts', async () => {
    const store = useWorkbenchStore()
    expect(store.tasks).toHaveLength(0)
    expect('inspectorOpen' in store).toBe(false)
    await store.loadJob('job-a')
    await store.loadJob('job-b')
    store.tasks[0].prompts.push({ id: 'prompt-a', shotId: 'job-a-shot-1', type: 'video', platform: 'kling', version: 1, text: 'x', status: 'ready', templateId: 'kling-video-v1', templateVersion: 1, warnings: [] })
    store.select('prompt-a')

    const removed = store.removeTask('task-job-a')

    expect(removed?.jobId).toBe('job-a')
    expect(store.tasks.map((task) => task.jobId)).toEqual(['job-b'])
    expect(store.shots.map((shot) => shot.id)).toEqual(['job-b-shot-1'])
    expect(store.prompts).toHaveLength(0)
    expect(store.selectedId).toBe('')
    expect(store.activeTaskKey).toBe('task-job-b')
    store.restoreTask(removed!, 0)
    expect(store.tasks.map((task) => task.jobId)).toEqual(['job-a', 'job-b'])
    expect(store.shots).toHaveLength(2)
    expect(store.selectedId).toBe('task-job-a')
  })

  it('preserves optional action phase evidence and generation timing across a refresh', async () => {
    const backend = result('job-actions')
    Object.assign(backend.shots[0], { action_details: {
      primary_subject: '女子', initial_state: '手持杯子', steps: ['饮用', '放下杯子'], final_state: '眉头收紧',
      accompanying_states: ['男孩抬头注视女子'],
      step_evidence: [[{ frame_id: 'job-actions-frame-1', timestamp: .2, description: '杯口贴近嘴部' }]],
      action_phases: [{ description: '饮用杯中液体', source_step_indices: [1], confidence: .85,
        generation_start_seconds: 0, generation_end_seconds: 1.2,
        evidence: [{ frame_id: 'job-actions-frame-1', timestamp: .2, description: '杯口贴近嘴部' }] }],
    } })
    vi.mocked(getJobResult).mockResolvedValue(backend as never)
    const store = useWorkbenchStore()
    await store.loadJob('job-actions')
    await store.loadJob('job-actions')
    expect(store.shots[0].actionDetails.accompanyingStates).toEqual(['男孩抬头注视女子'])
    expect(store.shots[0].actionDetails.actionPhases?.[0]).toMatchObject({
      description: '饮用杯中液体', sourceStepIndices: [1], generationStartSeconds: 0, generationEndSeconds: 1.2,
      evidence: [{ frame_id: 'job-actions-frame-1', timestamp: .2, description: '杯口贴近嘴部' }],
    })
    expect(store.shots[0].actionDetails.stepEvidence?.[0][0].frame_id).toBe('job-actions-frame-1')
  })

  it('preserves script direction across refreshes and removes it when a later video snapshot has no metadata', async () => {
    const backend = result('job-script')
    backend.job.input_type = 'script'
    const direction = {
      schema_version: 'script-director.v3', event_id: 'event-1', shot_function: '建立悬念', cut_reason: '反应镜头', camera_angle: '平视',
      source_span_ids: ['span-1'], dialogue: [{ id: 'dialogue-1', speaker_id: 'character-1', speaker_name: '阿青', text: '别开门。' }],
      sound_cues: ['门轴声'], creative_assumptions: ['冷色门缝光'],
      character_profiles: [{ id: 'character-1', name: '阿青', description: '年轻修理工', aliases: [] }], duration_kind: 'estimated-performance',
      continuity_notes: ['保持右手位置'],
    }
    Object.assign(backend.shots[0], { script_direction: direction })
    vi.mocked(getJobResult).mockResolvedValue(backend as never)
    const store = useWorkbenchStore()
    await store.loadJob('job-script')
    await store.loadJob('job-script')
    expect(store.tasks[0].inputType).toBe('script')
    expect(store.shots[0].script_direction).toEqual(direction)
    vi.mocked(getJobResult).mockResolvedValue(result('job-script') as never)
    await store.loadJob('job-script')
    expect(store.tasks[0].inputType).toBe('video')
    expect(store.shots[0].script_direction).toBeUndefined()
  })

  it('uses the latest same-shot delivery summary on canvas while preserving the original observation and edited prompt', async () => {
    const backend = { ...result('job-action-cards'), prompts: [
      { id: 'p-old', shot_id: 'job-action-cards-shot-1', prompt_type: 'video', platform: 'seedance', version: 1,
        positive_prompt: '旧提示词', negative_prompt: '', status: 'ready', template_id: 'seedance-video-v7', template_version: 7,
        contract: { delivery: { action_presentation: { schema_version: 'action-presentation.v1', main_action: '旧动作摘要' } } } },
      { id: 'p-new', shot_id: 'job-action-cards-shot-1', prompt_type: 'video', platform: 'seedance', version: 2,
        positive_prompt: '用户编辑后的提示词，保持原样', negative_prompt: '', status: 'ready', template_id: 'seedance-video-v7', template_version: 7,
        contract: { delivery: { action_presentation: { schema_version: 'action-presentation.v1', main_action: '航拍城市并缓慢下降' } } } },
      { id: 'p-other', shot_id: 'different-shot', prompt_type: 'video', platform: 'seedance', version: 5,
        positive_prompt: '另一镜', negative_prompt: '', status: 'ready', template_id: 'seedance-video-v7', template_version: 7,
        contract: { delivery: { action_presentation: { schema_version: 'action-presentation.v1', main_action: '不能串到当前镜头' } } } },
    ] }
    vi.mocked(getJobResult).mockResolvedValue(backend as never)
    const store = useWorkbenchStore()
    await store.loadJob('job-action-cards')
    expect(store.shots[0].action).toBe('航拍城市并缓慢下降')
    expect(store.shots[0].observedAction).toBe('城市航拍')
    expect(store.graphData(store.shots[0].id)?.summary).toBe('航拍城市并缓慢下降')
    expect(store.prompts.find((prompt) => prompt.id === 'p-new')?.text).toBe('用户编辑后的提示词，保持原样')
    expect(backend.shots[0].action).toBe('城市航拍')
  })
})
