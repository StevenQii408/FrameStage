import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { apiUrl } from '../api/client'
import { getJobResult } from '../api/jobs'
import type { CanvasPosition, EntityStatus, GraphNodeData, Prompt, Shot, WorkbenchTask } from '../types'
import { sequenceCards, sequencePrompts, sequenceSceneGroups } from '../generationSequences'

const terminalStatuses = new Set(['completed', 'failed', 'cancelled', 'paused_memory_pressure', 'interrupted'])

function entityStatus(status: string): EntityStatus {
  if (status === 'draft') return 'draft'
  if (status === 'ready' || status === 'warning' || status === 'failed' || status === 'cancelled' || status === 'paused_memory_pressure' || status === 'interrupted' || status === 'stale') return status
  if (status === 'processing') return 'processing'
  return 'queued'
}

function defaultPosition(index: number): CanvasPosition {
  return { x: 80, y: 90 + index * 560 }
}

export const useWorkbenchStore = defineStore('workbench', () => {
  const tasks = ref<WorkbenchTask[]>([])
  const selectedId = ref('')
  const activeTaskKey = ref('')
  const activeView = ref<'canvas' | 'shots' | 'timeline'>('canvas')
  const motionReduced = ref(false)
  const query = ref('')
  const filter = ref<'all' | 'warning' | 'processing' | 'ready' | 'failed' | 'stale'>('all')
  const generationView = ref<'sequences' | 'source'>('sequences')
  const snapshotRequests = new Map<string, number>()
  let draftSequence = 0
  let workspaceEpoch = 0
  function resetWorkspace() {
    workspaceEpoch += 1
    snapshotRequests.clear()
    tasks.value = []; selectedId.value = ''; activeTaskKey.value = ''
    activeView.value = 'canvas'; query.value = ''; filter.value = 'all'; generationView.value = 'sequences'
  }

  const shots = computed(() => tasks.value.flatMap((task) => task.shots))
  const prompts = computed(() => tasks.value.flatMap((task) => task.prompts))
  const allSequenceCards = computed(() => tasks.value.flatMap(sequenceCards))
  const allPrompts = computed(() => [...prompts.value, ...tasks.value.flatMap(sequencePrompts)])
  const connectionShots = computed(() => [...shots.value, ...allSequenceCards.value])
  function taskCanvasShots(task: WorkbenchTask) {
    const cards = sequenceCards(task)
    const covered = cards.flatMap(card => card.generationSequence!.source_shot_ids)
    // Until a complete sequence projection arrives, retain ALL source cards.
    // Source inspection is an Inspector action, not a global canvas mode.
    return cards.length && covered.length === task.shots.length && new Set(covered).size === task.shots.length && task.shots.every(shot => covered.includes(shot.id)) ? cards : task.shots
  }
  function taskCanvasPrompts(task: WorkbenchTask) { return taskCanvasShots(task)[0]?.generationSequence ? sequencePrompts(task) : task.prompts }
  function taskCanvasSceneGroups(task: WorkbenchTask) { const cards = taskCanvasShots(task); return cards[0]?.generationSequence ? sequenceSceneGroups(task, cards) : task.sceneGroups || [] }
  const displayShots = computed(() => tasks.value.flatMap(taskCanvasShots))
  const hasGenerationSequences = computed(() => allSequenceCards.value.length > 0)
  const submittedTasks = computed(() => tasks.value.filter((task) => task.jobId))
  const activeTask = computed(() => tasks.value.find((task) => task.key === activeTaskKey.value) || tasks.value[0])
  const selectedShot = computed(() => connectionShots.value.find((shot) => shot.id === selectedId.value))
  const selectedPrompt = computed(() => allPrompts.value.find((prompt) => prompt.id === selectedId.value))
  const filteredShots = computed(() => shots.value.filter((shot) => {
    const matchesQuery = !query.value || `${shot.action} ${shot.number}`.toLowerCase().includes(query.value.toLowerCase())
    const matchesFilter = filter.value === 'all' || shot.status === filter.value
    return matchesQuery && matchesFilter
  }))
  const filteredDisplayShots = computed(() => displayShots.value.filter(shot => (!query.value || `${shot.action} ${shot.number}`.toLowerCase().includes(query.value.toLowerCase())) && (filter.value === 'all' || shot.status === filter.value)))

  function createDraft(inputType: 'video' | 'script', position: CanvasPosition, restoredKey?: string): WorkbenchTask {
    const existing = restoredKey && tasks.value.find(task => task.key === restoredKey)
    if (existing) return existing
    draftSequence += 1
    const task: WorkbenchTask = {
      key: restoredKey || `draft-${Date.now()}-${draftSequence}`,
      jobId: null,
      inputType,
      status: 'draft',
      stage: '等待输入',
      progress: 0,
      error: null,
      videoFilename: '',
      provider: '',
      model: '',
      segmentationProfile: 'draft',
      styleStrategy: 'qwen-direct-v3',
      rawShotCount: 0,
      narrativeShotCount: 0,
      shots: [],
      prompts: [],
      rawShots: [],
      actionAtoms: [],
      sceneGroups: [],
      position,
    }
    tasks.value.push(task)
    activeTaskKey.value = task.key
    selectedId.value = task.key
    return task
  }

  function select(id: string) {
    selectedId.value = id
    const task = tasks.value.find((item) => item.key === id || item.shots.some((shot) => shot.id === id) || item.prompts.some((prompt) => prompt.id === id) || item.generationSequences?.some(sequence => sequence.id === id || sequence.first_frame_prompt?.id === id || sequence.seedance_prompt?.id === id))
    if (task) activeTaskKey.value = task.key
  }

  function selectView(view: 'canvas' | 'shots' | 'timeline') { activeView.value = view }
  function setGenerationView(view: 'sequences' | 'source') {
    if (generationView.value === view) return
    const current = graphData(selectedId.value)
    const task = tasks.value.find(item => item.key === activeTaskKey.value)
    generationView.value = view
    if (!task || !current) return
    if (view === 'source') {
      const sequence = current.sequence || task.generationSequences?.find(item => item.id === current.prompt?.sequenceId)
      if (sequence?.source_shot_ids[0]) select(sequence.source_shot_ids[0])
    } else {
      const sourceId = current.shot?.id || current.prompt?.shotId
      const sequence = task.generationSequences?.find(item => item.source_shot_ids.includes(sourceId || ''))
      if (sequence) select(sequence.id)
    }
  }
  function updateTaskPosition(key: string, position: CanvasPosition) {
    const task = tasks.value.find((item) => item.key === key)
    if (task) task.position = { ...position }
  }

  function removeTask(key: string) {
    const index = tasks.value.findIndex((item) => item.key === key)
    if (index < 0) return undefined
    const [removed] = tasks.value.splice(index, 1)
    if (removed.jobId) snapshotRequests.set(removed.jobId, (snapshotRequests.get(removed.jobId) || 0) + 1)
    const removedIds = new Set([removed.key, ...removed.shots.map((shot) => shot.id), ...removed.prompts.map((prompt) => prompt.id), ...sequenceCards(removed).map(shot => shot.id), ...sequencePrompts(removed).map(prompt => prompt.id)])
    if (removedIds.has(selectedId.value)) selectedId.value = ''
    if (activeTaskKey.value === removed.key) activeTaskKey.value = tasks.value[index]?.key || tasks.value[index - 1]?.key || ''
    return removed
  }

  function restoreTask(task: WorkbenchTask, index = tasks.value.length) {
    if (tasks.value.some((item) => item.key === task.key || (task.jobId && item.jobId === task.jobId))) return
    tasks.value.splice(Math.max(0, Math.min(index, tasks.value.length)), 0, task)
    activeTaskKey.value = task.key
    selectedId.value = task.key
  }

  function graphData(id: string): GraphNodeData | undefined {
    const card = allSequenceCards.value.find(item => item.id === id)
    if (card) return { kind: 'sequence', title: `生成片段 ${String(card.number).padStart(2, '0')}`, status: card.status, summary: card.action, shot: card, sequence: card.generationSequence }
    const shot = shots.value.find((item) => item.id === id)
    if (shot) return { kind: 'shot', title: `分镜 ${String(shot.number).padStart(2, '0')}`, status: shot.status, summary: shot.action, shot }
    const prompt = allPrompts.value.find((item) => item.id === id)
    if (prompt) return { kind: 'prompt', title: prompt.eventSummary ? '完整事件 Prompt' : prompt.type === 'first_frame' ? '首帧图片 Prompt' : prompt.type === 'image' ? '画面 Prompt' : 'Seedance 视频 Prompt', status: prompt.status, summary: prompt.text, prompt }
  }

  async function loadJob(id: string, position?: CanvasPosition, preferredKey?: string) {
    const epoch = workspaceEpoch
    const request = (snapshotRequests.get(id) || 0) + 1
    snapshotRequests.set(id, request)
    const result = await getJobResult(id)
    if (epoch !== workspaceEpoch) throw new Error('项目已切换，任务快照已失效')
    if (snapshotRequests.get(id) !== request) {
      const current = tasks.value.find(item => item.jobId === id)
      if (current) return current
      throw new Error('任务快照已失效')
    }
    let task = tasks.value.find((item) => item.jobId === id || (preferredKey && item.key === preferredKey))
    if (!task) {
      task = {
        key: preferredKey || `task-${id}`,
        jobId: id,
        inputType: result.job.input_type === 'script' ? 'script' : 'video',
        status: result.job.status,
        stage: result.job.stage || '处理中',
        progress: result.job.progress || 0,
        error: result.job.error || null,
        videoFilename: result.job.video_filename || '',
        provider: result.job.provider || 'legacy-unknown',
        model: result.job.model || 'legacy-unknown',
        segmentationProfile: result.job.segmentation_profile || 'legacy-v1',
        styleStrategy: result.job.style_strategy || 'legacy-v1',
        rawShotCount: result.job.raw_shot_count || 0,
        narrativeShotCount: result.job.narrative_shot_count || 0,
        shots: [], prompts: [], rawShots: [],
        actionAtoms: [], sceneGroups: [],
        position: position || defaultPosition(tasks.value.length),
      }
      tasks.value.push(task)
    }
    task.jobId = id
    task.inputType = result.job.input_type === 'script' ? 'script' : 'video'
    task.status = result.job.status
    task.stage = result.job.stage || '处理中'
    task.progress = result.job.progress || 0
    task.error = result.job.error || null
    task.videoFilename = result.job.video_filename || ''
    task.provider = result.job.provider || 'legacy-unknown'
    task.model = result.job.model || 'legacy-unknown'
    task.segmentationProfile = result.job.segmentation_profile || 'legacy-v1'
    task.styleStrategy = result.job.style_strategy || 'legacy-v1'
    task.directStyleAnalysisId = result.style_analysis?.analysis_id
    task.narrativeEvents = result.narrative_events || []
    task.generationSequences = result.generation_sequences || []
    task.sequenceMetadata = result.sequence_metadata || undefined
    task.deliveryQuality = result.metadata?.delivery_quality
    task.preflight = result.metadata?.capacity_preflight
    task.sceneStyleReview = result.metadata?.scene_style_review
    task.rawShotCount = result.job.raw_shot_count || result.raw_shots?.length || 0
    task.narrativeShotCount = result.job.narrative_shot_count || result.shots.length
    task.shots = result.shots.map((shot) => {
      const frames = result.frames.filter((frame) => frame.shot_id === shot.id).sort((a, b) => a.timestamp - b.timestamp).map((frame) => ({
        id: frame.id,
        url: apiUrl(`/jobs/${encodeURIComponent(id)}/frames/${encodeURIComponent(frame.id)}`),
        timestamp: frame.timestamp,
        width: frame.width || 0,
        height: frame.height || 0,
      }))
      const presentation = result.prompts
        .filter((prompt) => prompt.shot_id === shot.id)
        .sort((a, b) => b.version - a.version || Number(b.prompt_type === 'video') - Number(a.prompt_type === 'video'))
        .map((prompt) => (prompt.contract?.delivery as import('../types').ShotContract['delivery'])?.action_presentation)
        .find((action) => action?.schema_version === 'action-presentation.v1' && typeof action.main_action === 'string' && action.main_action.trim())
      const staticSummary = [shot.action_details?.primary_subject, shot.visual_facts?.scene]
        .filter((value): value is string => typeof value === 'string' && Boolean(value.trim()) && value !== '未知').join('；')
      return {
        id: shot.id, number: shot.number, startTime: shot.start_time, endTime: shot.end_time, time: shot.time, duration: `${shot.duration.toFixed(2)}s`,
        action: shot.analysis_review?.dynamic?.status === 'pending_review' ? staticSummary || '静态内容待查看' : presentation?.main_action.trim() || shot.action || '等待视觉分析', observedAction: shot.action || '', sourceText: shot.source_text || '',
        shotSize: shot.shot_size || '—', lens: shot.lens || '—', movement: shot.camera_movement || '—', lighting: shot.lighting_vibe || '—',
        confidence: shot.confidence || 0, thumb: frames[0]?.url || '', frames,
        analysisWarnings: shot.analysis_warnings || [], analysisReview: shot.analysis_review, status: entityStatus(shot.status),
        actionDetails: {
          primarySubject: String(shot.action_details?.primary_subject || '未知'),
          initialState: String(shot.action_details?.initial_state || '未知'),
          steps: Array.isArray(shot.action_details?.steps) ? shot.action_details.steps.map(String) : [],
          interactionTarget: String(shot.action_details?.interaction_target || '无'),
          finalState: String(shot.action_details?.final_state || '未知'),
          emotionChange: String(shot.action_details?.emotion_change || '未知'),
          evidence: Array.isArray(shot.action_details?.evidence) ? shot.action_details.evidence.map(String) : [],
          accompanyingStates: Array.isArray(shot.action_details?.accompanying_states) ? shot.action_details.accompanying_states.map(String) : [],
          stepEvidence: Array.isArray(shot.action_details?.step_evidence) ? shot.action_details.step_evidence.map((references) => Array.isArray(references) ? references.filter((item): item is import('../types').ActionEvidenceRef => !!item && typeof item === 'object' && typeof item.frame_id === 'string' && typeof item.timestamp === 'number' && Number.isFinite(item.timestamp) && typeof item.description === 'string') : []) : [],
          actionPhases: Array.isArray(shot.action_details?.action_phases) ? shot.action_details.action_phases
            .filter((phase): phase is Record<string, unknown> => !!phase && typeof phase === 'object' && typeof phase.description === 'string')
            .map((phase) => ({
              description: String(phase.description),
              sourceStepIndices: Array.isArray(phase.source_step_indices) ? phase.source_step_indices.filter((index): index is number => typeof index === 'number' && Number.isInteger(index)) : [],
              evidence: Array.isArray(phase.evidence) ? phase.evidence.filter((item): item is import('../types').ActionEvidenceRef => !!item && typeof item === 'object' && typeof item.frame_id === 'string' && typeof item.timestamp === 'number' && Number.isFinite(item.timestamp) && typeof item.description === 'string') : [],
              confidence: typeof phase.confidence === 'number' ? phase.confidence : 0,
              generationStartSeconds: typeof phase.generation_start_seconds === 'number' ? phase.generation_start_seconds : null,
              generationEndSeconds: typeof phase.generation_end_seconds === 'number' ? phase.generation_end_seconds : null,
            })) : [],
        },
        spatialEvidence: {
          foreground: String(shot.spatial_evidence?.foreground || '未知'),
          midground: String(shot.spatial_evidence?.midground || '未知'),
          background: String(shot.spatial_evidence?.background || '未知'),
          relationships: String(shot.spatial_evidence?.relationships || '未知'),
          observable: shot.spatial_evidence?.observable !== false,
        },
        sourceRawShotIds: shot.source_raw_shot_ids || [],
        visualStyle: shot.visual_style_evidence ? {
          medium: String(shot.visual_style_evidence.medium || '未知'),
          characterDesign: String(shot.visual_style_evidence.character_design || '未知'),
          rendering: String(shot.visual_style_evidence.rendering || '未知'),
          colorPalette: String(shot.visual_style_evidence.color_palette || '未知'),
          texture: String(shot.visual_style_evidence.texture || '未知'),
          visibleEffects: Array.isArray(shot.visual_style_evidence.visible_effects) ? shot.visual_style_evidence.visible_effects.map(String) : [],
          evidence: Array.isArray(shot.visual_style_evidence.evidence) ? shot.visual_style_evidence.evidence.map(String) : [],
        } : undefined,
        sourceActionAtomIds: shot.source_action_atom_ids || [],
        sceneGroupId: shot.scene_group_id || '',
        cutReason: shot.cut_reason || '', sourceEditTimes: shot.source_edit_times || [],
        generationDurationSeconds: shot.generation_duration_seconds || undefined, sceneReason: shot.scene_reason || '',
        script_direction: shot.script_direction || undefined,
      }
    })
    task.prompts = result.prompts.map((prompt) => ({
      id: prompt.id, shotId: prompt.shot_id,
      type: prompt.prompt_type === 'video' ? 'video' : prompt.prompt_type === 'first_frame' ? 'first_frame' : 'image',
      platform: prompt.platform, version: prompt.version, text: prompt.positive_prompt, negativeText: prompt.negative_prompt,
      status: entityStatus(prompt.status),
      templateId: prompt.template_id || 'legacy-v1', templateVersion: prompt.template_version || 1,
      warnings: prompt.warnings || [],
      contract: prompt.contract ? {
        purpose: String(prompt.contract.purpose || ''), firstFrameState: String(prompt.contract.first_frame_state || ''),
        primaryAction: String(prompt.contract.primary_action || ''), endState: String(prompt.contract.end_state || ''),
        continuityAnchors: Array.isArray(prompt.contract.continuity_anchors) ? prompt.contract.continuity_anchors.map(String) : [],
        shotSize: String(prompt.contract.shot_size || ''), lens: String(prompt.contract.lens || ''),
        cameraMovement: String(prompt.contract.camera_movement || ''), lighting: String(prompt.contract.lighting || ''), composition: String(prompt.contract.composition || ''),
        sourceEditTimes: Array.isArray(prompt.contract.source_edit_times) ? prompt.contract.source_edit_times.map(Number) : [],
        generationDurationSeconds: Number(prompt.contract.generation_duration_seconds || 0) || undefined,
        editorialNote: String(prompt.contract.editorial_note || ''),
        aspectRatio: String(prompt.contract.aspect_ratio || '待设定'),
        observedStyle: String(prompt.contract.observed_style || '未知'),
        styleProfileVersion: String(prompt.contract.style_profile_version || ''),
        generationSuggestions: prompt.contract.generation_suggestions as Record<string, string> | undefined,
        delivery: prompt.contract.delivery as import('../types').ShotContract['delivery'],
      } : undefined,
    }))
    task.rawShots = (result.raw_shots || []).map((raw) => ({ id: raw.id, number: raw.number, startTime: raw.start_time, endTime: raw.end_time, duration: raw.duration, boundaryMetrics: raw.boundary_metrics || {}, decision: raw.decision, reason: raw.reason, narrativeShotId: raw.narrative_shot_id || undefined, generationShotIds: raw.generation_shot_ids || [] }))
    task.actionAtoms = (result.action_atoms || []).map((atom) => ({ id: atom.id, number: atom.number, startTime: atom.start_time, endTime: atom.end_time, duration: atom.duration, sourceRawShotIds: atom.source_raw_shot_ids || [], frameIds: atom.frame_ids || [], primarySubject: atom.primary_subject, initialState: atom.initial_state, action: atom.action, interactionTarget: atom.interaction_target, finalState: atom.final_state, confidence: atom.confidence, warnings: atom.warnings || [], generationShotId: atom.generation_shot_id || undefined }))
    task.sceneGroups = (result.scene_groups || []).map((group) => ({ id: group.id, order: group.order, title: group.title, location: group.location || undefined, timeOfDay: group.time_of_day || undefined, shotIds: group.shot_ids || [], coverFrameIds: group.cover_frame_ids || [], startTime: group.start_time, endTime: group.end_time, warningCount: group.warning_count, groupingMode: group.grouping_mode }))
    if (!activeTaskKey.value) activeTaskKey.value = task.key
    return task
  }

  async function attachJob(taskKey: string, id: string) {
    const task = tasks.value.find((item) => item.key === taskKey)
    if (!task) throw new Error('输入节点已不存在')
    task.jobId = id
    task.status = 'queued'
    task.stage = 'validating'
    return loadJob(id, task.position, taskKey)
  }

  function applyEvent(jobId: string, eventType: string, payload: Record<string, unknown>) {
    const task = tasks.value.find((item) => item.jobId === jobId)
    if (!task) return
    if (['sequence.ready', 'scene.regrouped', 'job.completed', 'job.failed', 'job.cancelled', 'job.paused_memory_pressure', 'job.interrupted'].includes(eventType)) {
      // A snapshot started BEFORE the final topology/state event is obsolete,
      // even if no newer REST request has started yet (coalesced refreshes).
      snapshotRequests.set(jobId, (snapshotRequests.get(jobId) || 0) + 1)
    }
    if (eventType === 'perception.stage') {
      if (payload.state === 'running') task.stage = String(payload.message || '分镜分析中')
    } else if (eventType === 'shot.created') {
      const id = String(payload.shot_id || '')
      if (!id || task.shots.some((shot) => shot.id === id)) return
      const number = Number(payload.number || task.shots.length + 1)
      task.shots.push({ id, number, time: '处理中', duration: '—', action: '等待分镜描述', sourceText: '', shotSize: '—', lens: '—', movement: '—', lighting: '—', confidence: 0, thumb: '', frames: [], analysisWarnings: [], actionDetails: { primarySubject: '未知', initialState: '未知', steps: [], interactionTarget: '无', finalState: '未知', emotionChange: '未知', evidence: [] }, spatialEvidence: { foreground: '未知', midground: '未知', background: '未知', relationships: '未知', observable: false }, sourceRawShotIds: Array.isArray(payload.source_raw_shot_ids) ? payload.source_raw_shot_ids.map((value) => `${jobId}-raw-${String(value).padStart(3, '0')}`) : [], sceneGroupId: String(payload.scene_group_id || 'scene-001'), cutReason: String(payload.cut_reason || ''), sourceEditTimes: Array.isArray(payload.source_edit_times) ? payload.source_edit_times.map(Number) : [], status: 'processing' })
    } else if (eventType === 'shot.description_ready') {
      const shot = task.shots.find((item) => item.id === String(payload.shot_id || ''))
      if (shot) { shot.action = String(payload.description || shot.action); shot.sourceText = String(payload.source_text || shot.sourceText || ''); shot.status = 'ready' }
    } else if (eventType === 'shot.analyzed') {
      const shot = task.shots.find((item) => item.id === String(payload.shot_id || ''))
      if (shot) { shot.status = entityStatus(String(payload.status || 'ready')); shot.analysisWarnings = Array.isArray(payload.analysis_warnings) ? payload.analysis_warnings.map(String) : [] }
    } else if (eventType === 'shot.failed') {
      const shot = task.shots.find((item) => item.id === String(payload.shot_id || ''))
      if (shot) { shot.status = 'failed'; shot.action = String(payload.message || '镜头分析失败') }
    } else if (eventType === 'prompt.placeholder_created') {
      const id = String(payload.prompt_id || '')
      if (!id || task.prompts.some((prompt) => prompt.id === id)) return
      const shotId = String(payload.shot_id || '')
      if (shotId && !task.shots.some((shot) => shot.id === shotId)) {
        const numberMatch = shotId.match(/(\d+)$/)
        const number = numberMatch ? Number(numberMatch[1]) : task.shots.length + 1
        task.shots.push({ id: shotId, number, time: '处理中', duration: '—', action: '等待分镜描述', sourceText: '', shotSize: '—', lens: '—', movement: '—', lighting: '—', confidence: 0, thumb: '', frames: [], analysisWarnings: [], actionDetails: { primarySubject: '未知', initialState: '未知', steps: [], interactionTarget: '无', finalState: '未知', emotionChange: '未知', evidence: [] }, spatialEvidence: { foreground: '未知', midground: '未知', background: '未知', relationships: '未知', observable: false }, sourceRawShotIds: [], sceneGroupId: 'scene-001', status: 'processing' })
      }
      const type = payload.prompt_type === 'first_frame' ? 'first_frame' : 'video'
      task.prompts.push({ id, shotId, type, platform: 'pending', version: 1, text: '等待语言模型生成', negativeText: '', status: 'queued', templateId: 'pending', templateVersion: 1, warnings: [] })
    } else if (eventType === 'job.stage_changed') {
      task.status = 'processing'; task.stage = String(payload.stage || task.stage); task.progress = Number(payload.progress ?? task.progress)
    } else if (eventType === 'job.warning') {
      task.error = String(payload.message || '任务存在警告')
    } else if (eventType === 'job.failed') {
      task.status = 'failed'; task.stage = 'failed'; task.error = String(payload.message || '任务失败')
    } else if (eventType === 'job.cancelled') {
      task.status = 'cancelled'; task.stage = 'cancelled'
    } else if (eventType === 'job.interrupted') {
      task.status = 'interrupted'; task.stage = 'interrupted'; task.error = String(payload.message || '服务重启，任务已中断，可从检查点继续')
    } else if (eventType === 'job.paused_memory_pressure') {
      task.status = 'paused_memory_pressure'; task.stage = 'analyzing'; task.error = String(payload.message || '系统可用内存低于安全阈值')
    } else if (eventType === 'job.completed') {
      task.status = 'completed'; task.progress = 100; task.stage = 'completed'
    }
  }

  function taskIsTerminal(task: WorkbenchTask) { return terminalStatuses.has(task.status) }

  return {
    tasks, submittedTasks, activeTask, activeTaskKey, selectedId, activeView, motionReduced, query, filter,
    shots, prompts, selectedShot, selectedPrompt, filteredShots, filteredDisplayShots, generationView,
    displayShots, allPrompts, connectionShots, hasGenerationSequences, taskCanvasShots, taskCanvasPrompts, taskCanvasSceneGroups,
    createDraft, attachJob, loadJob, applyEvent, taskIsTerminal, updateTaskPosition, removeTask, restoreTask,
    select, selectView, setGenerationView, graphData, resetWorkspace,
  }
})
