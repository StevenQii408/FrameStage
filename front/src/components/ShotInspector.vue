<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch, type ComputedRef } from 'vue'
import { Maximize2, Play, X } from 'lucide-vue-next'
import type { ActionDetails, Prompt, RawShot, ScriptDialogue, Shot } from '../types'
import StatusPill from './StatusPill.vue'
import PerceptionReviewPanel from './PerceptionReviewPanel.vue'
import type { DetailTab } from '../nodeDetails'

const props = withDefaults(defineProps<{ shot: Shot; jobId?: string | null; projectId?: string; taskKey?: string; rawShots?: RawShot[]; prompts?: Prompt[]; tab?: DetailTab | 'all' }>(), { rawShots: () => [], prompts: () => [], tab: 'all' })
const showOverview = computed(() => props.tab === 'all' || props.tab === 'overview')
const showEvidence = computed(() => props.tab === 'all' || props.tab === 'evidence')
const showReview = computed(() => props.tab === 'all' || props.tab === 'review')
const emit = defineEmits<{ preview: []; updated: [jobId: string]; styleEffects: [shotId: string] }>()
const reviewRequests = inject<ComputedRef<{ source_shot_id: string; status: string; reason?: string; field_group?: string }[]>>('agent-review-requests', computed(() => []))
const reviewSuggestion = computed(() => {
  const rows = reviewRequests?.value
  return Array.isArray(rows) ? rows.find(item => item.source_shot_id === props.shot.id && item.status === 'pending') : undefined
})
const activeIndex = ref(0)
const lightboxOpen = ref(false)
const observationsOpen = ref(false)
const characterProfilesOpen = ref(false)
const scriptDirection = computed(() => props.shot.script_direction)
const scriptDialogue = computed(() => scriptDirection.value?.dialogue || [])
const characterProfiles = computed(() => scriptDirection.value?.character_profiles || [])
const activeFrame = computed(() => props.shot.frames[activeIndex.value] || props.shot.frames[0])
const aspect = computed(() => activeFrame.value?.width && activeFrame.value?.height ? `${activeFrame.value.width} / ${activeFrame.value.height}` : '16 / 9')
const spatialEvidence = computed(() => props.shot.spatialEvidence || { foreground: '未记录', midground: '未记录', background: '未记录', relationships: '未记录', observable: false })
const actionDetails = computed<ActionDetails>(() => props.shot.actionDetails || { primarySubject: '未知', initialState: '未记录', steps: [], interactionTarget: '未知', finalState: '未记录', emotionChange: '未知', evidence: [] })
const actionPresentation = computed(() => scriptDirection.value ? undefined : props.prompts
  .filter((prompt) => prompt.shotId === props.shot.id && prompt.contract?.delivery?.action_presentation?.schema_version === 'action-presentation.v1')
  .sort((a, b) => b.version - a.version || Number(b.type === 'video') - Number(a.type === 'video'))[0]
  ?.contract?.delivery?.action_presentation)
const mainAction = computed(() => actionPresentation.value?.main_action?.trim() || props.shot.action)
const accompanyingStates = computed(() => actionPresentation.value?.accompanying_states || actionDetails.value.accompanyingStates || [])
const actionPhases = computed(() => actionPresentation.value?.phases || [])
const phaseTimingValid = computed(() => {
  if (actionPresentation.value?.timing_mode !== 'generation-suggestion' || actionPhases.value.length < 2) return false
  const duration = props.shot.generationDurationSeconds || Number.parseFloat(props.shot.duration)
  let previousEnd = 0
  const ordered = actionPhases.value.every((phase) => {
    const start = phase.start_seconds
    const end = phase.end_seconds
    const valid = typeof start === 'number' && typeof end === 'number' && Number.isFinite(start) && Number.isFinite(end)
      && Math.abs(start - previousEnd) <= 0.001 && end > start && (!Number.isFinite(duration) || end <= duration + 0.001)
    if (valid) previousEnd = end!
    return valid
  })
  return ordered && (!Number.isFinite(duration) || Math.abs(previousEnd - duration) <= 0.001)
})
function seconds(value: number) { return Number(value.toFixed(3)).toString() }
function dialogueSpeaker(line: ScriptDialogue) {
  return line.speaker_name?.trim()
    || characterProfiles.value.find((profile) => profile.id === line.speaker_id)?.name
    || line.speaker_id
    || '未标注说话人'
}

watch(() => props.shot.id, () => { activeIndex.value = 0; lightboxOpen.value = false; observationsOpen.value = false; characterProfilesOpen.value = false })
watch(() => props.shot.frames.length, (length) => { if (activeIndex.value >= length) activeIndex.value = 0 })

function chooseFrame(index: number) { activeIndex.value = index }
function openLightbox() { if (activeFrame.value) lightboxOpen.value = true }
function closeLightbox() { lightboxOpen.value = false }
function step(direction: number) {
  if (!props.shot.frames.length) return
  activeIndex.value = (activeIndex.value + direction + props.shot.frames.length) % props.shot.frames.length
}
function onKeydown(event: KeyboardEvent) {
  if (!lightboxOpen.value) return
  if (event.key === 'Escape') closeLightbox()
  if (event.key === 'ArrowLeft') step(-1)
  if (event.key === 'ArrowRight') step(1)
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="inspector-body shot-inspector">
    <div v-if="!scriptDirection && activeFrame && (showOverview || showEvidence)" class="inspector-image" :style="{ aspectRatio: aspect }">
      <img :src="activeFrame.url" :alt="`分镜 ${shot.number} · ${activeIndex + 1}`" />
      <span class="image-time">{{ activeFrame.timestamp.toFixed(2) }}s</span>
      <button type="button" aria-label="放大关键帧" @click="openLightbox"><Maximize2 :size="14" /></button>
    </div>
    <StatusPill v-if="showOverview" :status="shot.status" />
    <p v-if="showOverview" class="inspector-summary">{{ mainAction }}</p>
    <div v-if="showOverview && shot.analysisReview?.dynamic?.status === 'pending_review'" class="analysis-warning"><strong>动态待复核</strong><span>{{ shot.analysisReview.static?.status === 'ready' ? '主体与静态场景已识别，以下有效信息继续保留。' : '有效字段与帧证据已保留。' }}</span><span>视频动作方案尚未就绪。</span></div>
    <div v-if="scriptDirection && showOverview" class="inspector-section script-direction-section">
      <div class="section-title">文字剧本导演 <span>SCRIPT PLAN</span></div>
      <p class="action-timing-note">文字剧本的导演规划，时长按表演节奏估计。</p>
      <div class="field-group"><label>镜头目的</label><div class="field-value">{{ scriptDirection.shot_function || '未记录' }}</div></div>
      <div class="field-group"><label>切换理由</label><div class="field-value">{{ scriptDirection.cut_reason || '未记录' }}</div></div>
      <div class="field-group"><label>机位角度</label><div class="field-value">{{ scriptDirection.camera_angle || '未记录' }}</div></div>
      <div v-if="scriptDirection.continuity_notes?.length" class="field-group"><label>连续性提示</label><div v-for="(note, index) in scriptDirection.continuity_notes" :key="`${index}-${note}`" class="field-value">{{ note }}</div></div>
      <div v-if="scriptDirection.source_span_ids?.length" class="field-group"><label>原文段落</label><div class="field-value">{{ scriptDirection.source_span_ids.join('、') }}</div></div>
    </div>
    <div v-if="scriptDirection && showOverview" class="inspector-section script-dialogue-section">
      <div class="section-title">原文对白与拟音 <span>SCRIPT AUDIO</span></div>
      <p class="action-timing-note">对白与拟音提示用于后续声音制作。</p>
      <div v-for="(line, index) in scriptDialogue" :key="line.id || index" class="field-group script-dialogue-line">
        <label>{{ dialogueSpeaker(line) }}<span v-if="line.parenthetical"> · {{ line.parenthetical }}</span></label>
        <div class="field-value">{{ line.text }}</div>
      </div>
      <p v-if="!scriptDialogue.length" class="action-timing-note">未记录原文对白</p>
      <div v-if="scriptDirection.sound_cues?.length" class="field-group script-sound-cues"><label>拟音提示</label><div v-for="(cue, index) in scriptDirection.sound_cues" :key="`${index}-${cue}`" class="field-value">{{ cue }}</div></div>
    </div>
    <div v-if="scriptDirection && showReview" class="inspector-section script-assumptions-section">
      <div class="section-title">创作补全 <span>CREATIVE ASSUMPTIONS</span></div>
      <div v-for="(assumption, index) in scriptDirection.creative_assumptions || []" :key="`${index}-${assumption}`" class="field-value">{{ assumption }}</div>
      <p v-if="!scriptDirection.creative_assumptions?.length" class="action-timing-note">未记录创作补全</p>
    </div>
    <details v-if="scriptDirection && characterProfiles.length && showEvidence" class="inspector-section script-character-profiles" :open="characterProfilesOpen" @toggle="characterProfilesOpen = ($event.target as HTMLDetailsElement).open">
      <summary class="section-title">人物设定 <span>{{ characterProfiles.length }} CHARACTERS</span></summary>
      <div v-for="(profile, index) in characterProfiles" :key="profile.id || index" class="field-group">
        <label>{{ profile.name || '未命名人物' }}</label><div class="field-value">{{ profile.description || '未记录人物描述' }}</div>
        <div v-if="profile.aliases?.length" class="action-timing-note">别名：{{ profile.aliases.join('、') }}</div>
      </div>
    </details>
    <div v-if="accompanyingStates.length && showOverview" class="field-group action-accompaniment"><label>伴随状态</label><div v-for="state in accompanyingStates" :key="state" class="field-value">{{ state }}</div></div>
    <div v-if="actionPhases.length > 1 && showOverview" class="inspector-section action-presentation-section">
      <div class="section-title">{{ phaseTimingValid ? '动作时间安排' : '动作阶段' }} <span>{{ phaseTimingValid ? 'GENERATION PLAN' : 'ORDERED PHASES' }}</span></div>
      <p class="action-timing-note">{{ phaseTimingValid ? '生成编排建议，不是原片动作时间实测。' : '按有证据的动作顺序展示，不推定精确发生秒数。' }}</p>
      <ol class="action-steps"><li v-for="(phase, index) in actionPhases" :key="`${index}-${phase.description}`"><b>{{ index + 1 }}</b><span><strong v-if="phaseTimingValid" class="action-phase-time">{{ seconds(phase.start_seconds!) }}–{{ seconds(phase.end_seconds!) }} 秒：</strong>{{ phase.description }}</span></li></ol>
    </div>
    <div v-if="actionPresentation?.warnings?.length && showReview" class="analysis-warning"><strong>动作安排需复核</strong><span v-for="warning in actionPresentation.warnings" :key="warning">{{ warning }}</span></div>
    <div v-if="shot.analysisReview?.issues?.length && showReview" class="analysis-warning"><strong>分析状态</strong><span v-for="issue in shot.analysisReview.issues" :key="`${issue.code}-${issue.field}`">{{ issue.severity === 'review' ? '待复核' : '提示' }} · {{ issue.message }}</span></div>
    <div v-else-if="shot.analysisWarnings.length && showReview" class="analysis-warning"><strong>分析提示</strong><span v-for="warning in shot.analysisWarnings" :key="warning">{{ warning }}</span></div>
    <div v-if="showOverview" class="metric-grid"><div><span>景别</span><b>{{ shot.shotSize }}</b></div><div><span>{{ scriptDirection ? '焦段建议' : '焦段估计' }}</span><b>{{ shot.lens }}</b></div><div><span>{{ scriptDirection ? '时长依据' : '置信度' }}</span><b>{{ scriptDirection ? '表演节奏估计' : `${Math.round(shot.confidence * 100)}%` }}</b></div><div><span>{{ scriptDirection ? '表演估计时长' : '时长' }}</span><b>{{ shot.duration }}</b></div></div>
    <div v-if="showOverview" class="field-group"><label>摄像机运动</label><div class="field-value">{{ shot.movement }}</div></div>
    <div v-if="showOverview" class="field-group"><label>光线与氛围</label><div class="field-value">{{ shot.lighting }}</div></div>
    <div v-if="!scriptDirection && shot.visualStyle && showEvidence" class="inspector-section evidence-section">
      <div class="section-title">整体画面风格 <span>OBSERVED STYLE</span></div>
      <div class="evidence-grid">
        <div><span>媒介</span><p>{{ shot.visualStyle.medium }}</p></div>
        <div><span>角色与表情</span><p>{{ shot.visualStyle.characterDesign }}</p></div>
        <div><span>渲染与光影</span><p>{{ shot.visualStyle.rendering }}</p></div>
        <div><span>材质与色彩</span><p>{{ shot.visualStyle.texture }}；{{ shot.visualStyle.colorPalette }}</p></div>
      </div>
      <div class="frame-evidence"><span v-for="item in shot.visualStyle.evidence" :key="item">{{ item }}</span></div>
    </div>
    <div v-if="!scriptDirection && showEvidence" class="inspector-section evidence-section">
      <div class="section-title">空间证据 <span>VISIBLE FACTS</span></div>
      <div class="evidence-grid">
        <div><span>前景</span><p>{{ spatialEvidence.foreground }}</p></div>
        <div><span>中景</span><p>{{ spatialEvidence.midground }}</p></div>
        <div><span>背景</span><p>{{ spatialEvidence.background }}</p></div>
        <div><span>空间关系</span><p>{{ spatialEvidence.relationships }}</p></div>
      </div>
    </div>
    <details v-if="!scriptDirection && showEvidence" class="inspector-section action-evidence-section" :open="observationsOpen" @toggle="observationsOpen = ($event.target as HTMLDetailsElement).open">
      <summary class="section-title">详细动作观察 <span>{{ actionDetails.steps.length }} OBSERVATIONS</span></summary>
      <p class="action-timing-note">保留原始动作细节；帧时间戳是画面证据，不代表动作起止或完成时间。</p>
      <div class="action-state"><span>起始</span><p>{{ actionDetails.initialState }}</p></div>
      <ol v-if="actionDetails.steps.length" class="action-steps"><li v-for="(step, index) in actionDetails.steps" :key="`${index}-${step}`"><b>{{ index + 1 }}</b><div><span>{{ step }}</span><div v-if="actionDetails.stepEvidence?.[index]?.length" class="frame-evidence"><span v-for="(item, evidenceIndex) in actionDetails.stepEvidence[index]" :key="`${item.frame_id}-${evidenceIndex}`">{{ item.frame_id }} · {{ seconds(item.timestamp) }}s：{{ item.description }}</span></div></div></li></ol>
      <div class="action-state"><span>结束</span><p>{{ actionDetails.finalState }}</p></div>
      <div v-if="actionDetails.evidence.length" class="frame-evidence"><span v-for="item in actionDetails.evidence" :key="item">{{ item }}</span></div>
      <div v-for="(phase, index) in actionDetails.actionPhases || []" :key="`evidence-${index}`" class="action-phase-evidence">
        <p>{{ phase.description }}</p>
        <div class="frame-evidence"><span v-for="(item, evidenceIndex) in phase.evidence" :key="`${item.frame_id}-${evidenceIndex}`">{{ item.frame_id }} · {{ seconds(item.timestamp) }}s：{{ item.description }}</span></div>
      </div>
    </details>
    <div v-if="!scriptDirection && shot.frames.length && showEvidence" class="inspector-section">
      <div class="section-title">关键帧 <span>{{ String(shot.frames.length).padStart(2, '0') }} FRAMES</span></div>
      <div class="frame-strip">
        <button v-for="(frame, index) in shot.frames" :key="frame.id" type="button" :class="{ active: index === activeIndex }" :aria-label="`查看第 ${index + 1} 帧`" @click="chooseFrame(index)">
          <img :src="frame.url" :alt="`${frame.timestamp.toFixed(2)} 秒`" />
        </button>
      </div>
    </div>
    <div v-if="!scriptDirection && showEvidence && (shot.cutReason || shot.sourceEditTimes?.length)" class="field-group"><label>生成镜头切分</label><div class="field-value">{{ shot.cutReason || '连续动作' }}<span v-if="shot.sourceEditTimes?.length"> · 源片内部切点 {{ shot.sourceEditTimes.map((time) => `${time.toFixed(2)}s`).join('、') }}</span></div></div>
    <details v-if="!scriptDirection && rawShots.length && showEvidence" class="raw-cuts">
      <summary>原始切点与合并理由 <span>{{ rawShots.length }} RAW SHOTS</span></summary>
      <div v-for="rawShot in rawShots" :key="rawShot.id" class="raw-cut-row">
        <b>R{{ String(rawShot.number).padStart(2, '0') }}</b>
        <span>{{ rawShot.startTime.toFixed(2) }}–{{ rawShot.endTime.toFixed(2) }}s</span>
        <em>{{ rawShot.decision }} · {{ rawShot.reason }}</em>
      </div>
    </details>
    <button v-if="!scriptDirection && tab === 'all'" class="full-button" type="button" @click="emit('preview')"><Play :size="14" /> 在原视频中预览</button>
    <button v-if="showReview" class="outline-button" type="button" @click="emit('styleEffects', shot.id)">调整本镜局部特效</button>
    <PerceptionReviewPanel v-if="!scriptDirection && showReview" :job-id="jobId" :project-id="projectId" :task-key="taskKey" :shot-id="shot.id" :suggestion="reviewSuggestion" @updated="emit('updated', $event)" />

    <Teleport to="body">
      <div v-if="!scriptDirection && lightboxOpen && activeFrame" class="shot-lightbox" data-canvas-modal role="dialog" aria-modal="true" aria-label="关键帧大图预览" @click.self="closeLightbox">
        <button class="lightbox-close" type="button" aria-label="关闭大图" @click="closeLightbox"><X :size="21" /></button>
        <button v-if="shot.frames.length > 1" class="lightbox-nav lightbox-prev" type="button" aria-label="上一帧" @click="step(-1)">‹</button>
        <figure><img :src="activeFrame.url" :alt="`分镜 ${shot.number} 关键帧大图`" /><figcaption>{{ activeIndex + 1 }} / {{ shot.frames.length }} · {{ activeFrame.timestamp.toFixed(2) }}s · {{ activeFrame.width }}×{{ activeFrame.height }}</figcaption></figure>
        <button v-if="shot.frames.length > 1" class="lightbox-nav lightbox-next" type="button" aria-label="下一帧" @click="step(1)">›</button>
      </div>
    </Teleport>
  </div>
</template>
