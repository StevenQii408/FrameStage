<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, FileText, GripHorizontal, X } from 'lucide-vue-next'
import type { GenerationSequence, Prompt, RawShot, Shot, WorkbenchTask } from '../types'
import { sequenceCards, sequencePrompts, sequenceStatus } from '../generationSequences'
import { visibleCanvasPrompts } from '../promptVisibility'
import StatusPill from './StatusPill.vue'
import { detailIdentity, detailSourceRequest, type DetailScene, type DetailSourceRequest, type DetailTab, type DetailTarget } from '../nodeDetails'

const ShotInspector = defineAsyncComponent(() => import('./ShotInspector.vue'))
const GenerationSequenceInspector = defineAsyncComponent(() => import('./GenerationSequenceInspector.vue'))
const PromptInspector = defineAsyncComponent(() => import('./PromptInspector.vue'))
const TaskReviewPanel = defineAsyncComponent(() => import('./TaskReviewPanel.vue'))
const props = withDefaults(defineProps<{ target: DetailTarget; task?: WorkbenchTask; shot?: Shot; sequence?: GenerationSequence; prompt?: Prompt;
  scene?: DetailScene; rawShots?: RawShot[]; metadata?: Record<string, unknown>; initialTab?: DetailTab }>(), { rawShots: () => [], initialTab: 'overview' })
const emit = defineEmits<{
  close: []; sourceShot: [request: DetailSourceRequest]; sequence: [request: { taskKey: string; sequenceId: string }];
  copy: [text: string]; updated: [jobId: string]; styleEffects: [request: { taskKey: string; shotId: string }];
  openEventAgent: [request: { taskKey: string; jobId: string }]; openPrompt: [request: { taskKey: string; promptId: string }];
}>()
const host = ref<HTMLElement>()
const activeTab = ref<DetailTab>(props.initialTab)
const screen = ref({ width: window.innerWidth, height: window.innerHeight })
const width = computed(() => Math.min(800, Math.max(280, screen.value.width - 32)))
const position = ref({ x: Math.max(16, (screen.value.width - width.value) / 2), y: Math.min(76, Math.max(16, screen.value.height / 10)) })
const identity = computed(() => detailIdentity(props.target))
const task = computed(() => props.task?.key === props.target.taskKey && (!props.target.jobId || props.task.jobId === props.target.jobId) ? props.task : undefined)
const scopeValid = computed(() => !props.task || Boolean(task.value))
const shot = computed(() => scopeValid.value ? (props.shot?.id === props.target.id ? props.shot : task.value?.shots.find(item => item.id === props.target.id)) : undefined)
const sequence = computed(() => scopeValid.value ? (props.sequence?.id === props.target.id ? props.sequence : task.value?.generationSequences?.find(item => item.id === props.target.id)) : undefined)
const taskPrompts = computed(() => task.value ? [...task.value.prompts, ...sequencePrompts(task.value)] : [])
const prompt = computed(() => scopeValid.value ? (props.prompt?.id === props.target.id ? props.prompt : taskPrompts.value.find(item => item.id === props.target.id)) : undefined)
const scene = computed(() => {
  if (!scopeValid.value) return undefined
  if (props.scene) return props.scene
  const prefix = props.target.taskKey + ':scene:'
  const id = props.target.id.startsWith(prefix) ? props.target.id.slice(prefix.length) : props.target.id
  return task.value?.sceneGroups?.find(item => item.id === id)
})
const sceneShots = computed(() => task.value ? [...task.value.shots, ...sequenceCards(task.value)].filter(item => scene.value?.shotIds.includes(item.id)) : [])
const event = computed(() => task.value?.narrativeEvents?.find(item => item.id === sequence.value?.event_id))
const rawShots = computed(() => props.rawShots.length ? props.rawShots : task.value?.rawShots.filter(item => shot.value?.sourceRawShotIds?.includes(item.id) || item.narrativeShotId === shot.value?.id) || [])
const title = computed(() => props.target.kind === 'task' ? '任务报告' : props.target.kind === 'scene' ? scene.value?.title || '场次详情'
  : props.target.kind === 'shot' ? `分镜 ${String(shot.value?.number || '').padStart(2, '0')}`
    : props.target.kind === 'sequence' ? sequence.value?.title || '生成片段详情' : prompt.value?.eventSummary ? '完整事件 Prompt' : 'Prompt')
const available = computed(() => props.target.kind === 'task' ? Boolean(task.value) : props.target.kind === 'scene' ? Boolean(scene.value && task.value)
  : props.target.kind === 'shot' ? Boolean(shot.value) : props.target.kind === 'sequence' ? Boolean(sequence.value && task.value) : Boolean(prompt.value))
const tabs = computed(() => props.target.kind === 'scene' ? [{ id: 'overview' as const, label: '场次镜头' }] : [
  { id: 'overview' as const, label: props.target.kind === 'task' ? '任务报告' : props.target.kind === 'prompt' ? 'Prompt' : '概览' },
  { id: 'evidence' as const, label: props.target.kind === 'prompt' ? '合同与来源' : '证据与来源' },
  { id: 'review' as const, label: props.target.kind === 'task' ? '复核与实验' : '检查与记录' },
])
const status = computed(() => props.target.kind === 'shot' ? shot.value?.status : props.target.kind === 'sequence' && sequence.value ? sequenceStatus(sequence.value.status) : props.target.kind === 'prompt' ? prompt.value?.status : undefined)
const shotPrompts = computed(() => visibleCanvasPrompts(taskPrompts.value.filter(item => item.shotId === shot.value?.id))
  .sort((a, b) => b.version - a.version).filter((item, index, all) => all.findIndex(other => other.type === item.type) === index)
  .sort((a, b) => Number(a.type === 'video') - Number(b.type === 'video')).slice(0, 2))
const canExperiment = computed(() => task.value?.jobId && task.value.status === 'completed' && task.value.inputType === 'video')
let opener: HTMLElement | null = null
let drag: { id: number; x: number; y: number; originX: number; originY: number } | null = null
function constrain(x: number, y: number) { return { x: Math.max(12, Math.min(screen.value.width - width.value - 12, x)), y: Math.max(12, Math.min(screen.value.height - 180, y)) } }
function resize() { screen.value = { width: window.innerWidth, height: window.innerHeight }; position.value = constrain(position.value.x, position.value.y) }
function close() { emit('close'); void nextTick(() => { if (opener?.isConnected) opener.focus({ preventScroll: true }) }) }
function escape(event: KeyboardEvent) {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || event.target instanceof HTMLSelectElement) return
  event.preventDefault(); event.stopPropagation(); close()
}
function startDrag(event: PointerEvent) {
  if (event.button !== 0 || (event.target as Element).closest('button,a,input,textarea,select')) return
  drag = { id: event.pointerId, x: event.clientX, y: event.clientY, originX: position.value.x, originY: position.value.y }
  ;(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId)
  event.preventDefault()
}
function moveDrag(event: PointerEvent) { if (drag?.id === event.pointerId) position.value = constrain(drag.originX + event.clientX - drag.x, drag.originY + event.clientY - drag.y) }
function endDrag() { drag = null }
function tabKey(event: KeyboardEvent) {
  const index = tabs.value.findIndex(tab => tab.id === activeTab.value)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.value.length - 1 : event.key === 'ArrowRight' ? (index + 1) % tabs.value.length : event.key === 'ArrowLeft' ? (index + tabs.value.length - 1) % tabs.value.length : -1
  if (next < 0) return
  event.preventDefault(); event.stopPropagation(); activeTab.value = tabs.value[next].id
  void nextTick(() => host.value?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')?.focus())
}
function sourceShot(id: string) { if (!task.value?.shots.some(item => item.id === id)) return; emit('sourceShot', detailSourceRequest(props.target, id)) }
function openPrompt(id: string) { emit('openPrompt', { taskKey: props.target.taskKey, promptId: id }) }
function updated(jobId: string) { if (jobId === (props.target.jobId || task.value?.jobId)) emit('updated', jobId) }
function restoreTab(value: DetailTab) { activeTab.value = tabs.value.some(tab => tab.id === value) ? value : 'overview' }
restoreTab(props.initialTab)
watch(identity, () => { restoreTab(props.initialTab) })
watch(() => props.initialTab, restoreTab)
onMounted(() => { opener = document.activeElement instanceof HTMLElement ? document.activeElement : null; window.addEventListener('resize', resize); window.addEventListener('blur', endDrag); void nextTick(() => host.value?.focus({ preventScroll: true })) })
onBeforeUnmount(() => { window.removeEventListener('resize', resize); window.removeEventListener('blur', endDrag); endDrag() })
</script>

<template>
  <Teleport to="body">
    <section ref="host" class="node-details-host nowheel nodrag nopan" role="dialog" aria-modal="false" aria-labelledby="node-details-title" data-canvas-details data-canvas-gesture-lock tabindex="-1" :data-detail-kind="target.kind" :data-detail-id="target.id" :style="{ left: position.x + 'px', top: position.y + 'px', width: width + 'px', maxHeight: Math.max(160, screen.height - position.y - 16) + 'px' }" @pointerdown.stop @click.stop @dblclick.stop @wheel.stop @keydown.esc="escape">
      <header class="node-details-header" @pointerdown="startDrag" @pointermove="moveDrag" @pointerup="endDrag" @pointercancel="endDrag" @lostpointercapture="endDrag">
        <GripHorizontal :size="15" aria-hidden="true" /><div><small>{{ task?.videoFilename || (task?.inputType === 'script' ? '文字剧本' : target.jobId || '项目详情') }}</small><h2 id="node-details-title">{{ title }}</h2></div><StatusPill v-if="status" :status="status" />
        <button type="button" aria-label="关闭详情" title="关闭详情，保留节点选中" @click="close"><X :size="18" /></button>
      </header>
      <div class="node-details-tabs" role="tablist" aria-label="详情内容">
        <button v-for="tab in tabs" :id="'node-details-tab-' + tab.id" :key="tab.id" type="button" role="tab" :aria-selected="activeTab === tab.id" :tabindex="activeTab === tab.id ? 0 : -1" aria-controls="node-details-content" @click="activeTab = tab.id" @keydown="tabKey">{{ tab.label }}</button>
      </div>
      <div id="node-details-content" class="node-details-content" role="tabpanel" :aria-labelledby="'node-details-tab-' + activeTab">
        <p v-if="!available" class="node-details-empty">此对象的数据暂不可用，任务更新后可以重新打开查看。</p>
        <template v-else-if="target.kind === 'shot' && shot">
          <button v-if="target.returnSequenceId" class="node-details-return" type="button" @click="emit('sequence', { taskKey: target.taskKey, sequenceId: target.returnSequenceId! })"><ArrowLeft :size="13" />返回生成片段</button>
          <ShotInspector :shot="shot" :job-id="target.jobId || task?.jobId" :project-id="target.projectId" :task-key="target.taskKey" :raw-shots="rawShots" :prompts="taskPrompts" :tab="activeTab" @updated="updated" @style-effects="id => emit('styleEffects', { taskKey: target.taskKey, shotId: id })" />
          <div v-if="activeTab === 'overview'" class="node-details-prompt-links"><button v-for="item in shotPrompts" :key="item.id" type="button" @click="openPrompt(item.id)"><FileText :size="13" />{{ item.type === 'video' ? 'Seedance Prompt' : '首帧图片 Prompt' }}</button></div>
        </template>
        <GenerationSequenceInspector v-else-if="target.kind === 'sequence' && sequence && task" :sequence="sequence" :shots="task.shots" :event="event" :metadata="metadata || task.sequenceMetadata" :preview="task.sequenceMetadata?.mode === 'preview'" :tab="activeTab" @copy="text => emit('copy', text)" @source-shot="sourceShot" @open-prompt="openPrompt" />
        <PromptInspector v-else-if="target.kind === 'prompt' && prompt" :prompt="prompt" :tab="activeTab" embedded @copy="text => emit('copy', text)" />
        <template v-else-if="target.kind === 'task' && task">
          <template v-if="activeTab === 'overview'"><p class="node-details-summary">{{ task.inputType === 'script' ? '文字剧本' : '视频分析' }} · {{ task.stage }} · {{ task.progress }}%</p><p v-if="task.error" class="node-details-warning">{{ task.error }}</p><TaskReviewPanel :task="task" section="report" /></template>
          <div v-else-if="activeTab === 'evidence'" class="node-details-source-list"><button v-for="item in task.shots" :key="item.id" type="button" @click="sourceShot(item.id)"><img v-if="item.thumb" :src="item.thumb" alt="" loading="lazy" /><span><b>SH {{ String(item.number).padStart(2, '0') }} · {{ item.time }}</b><small>{{ item.observedAction || item.action }}</small></span></button><p v-if="!task.shots.length">此任务暂未产生来源镜头。</p></div>
          <template v-else><TaskReviewPanel :task="task" section="review" @source-shot="sourceShot" /><button class="node-details-experiment" data-testid="event-agent-entry" type="button" :disabled="!canExperiment" @click="task.jobId && emit('openEventAgent', { taskKey: target.taskKey, jobId: task.jobId })">事件分镜（实验）</button><p v-if="!canExperiment" class="node-details-note">实验入口支持已完成的视频分析任务。</p></template>
        </template>
        <template v-else-if="target.kind === 'scene' && scene">
          <p class="node-details-summary">{{ scene.title }} · {{ scene.shotIds.length }} 个来源镜头</p><p v-if="scene.groupingMode === 'legacy-presentation-group'" class="node-details-note">历史展示分组，不代表已识别的剧情场次。</p>
          <div class="node-details-source-list"><button v-for="item in sceneShots" :key="item.id" type="button" @click="item.generationSequence ? emit('sequence', { taskKey: target.taskKey, sequenceId: item.id }) : sourceShot(item.id)"><img v-if="item.thumb" :src="item.thumb" alt="" loading="lazy" /><span><b>{{ item.generationSequence ? 'SEQ' : 'SH' }} {{ String(item.number).padStart(2, '0') }} · {{ item.time }}</b><small>{{ item.observedAction || item.action }}</small></span></button></div>
        </template>
      </div>
    </section>
  </Teleport>
</template>

<style scoped>
.node-details-host{position:fixed;z-index:110;box-sizing:border-box;display:flex;flex-direction:column;overflow:hidden;border:1px solid #6a8b9c59;border-radius:16px;background:#1b222afc;color:#d9e3ec;box-shadow:0 24px 70px #0007;font-family:inherit;outline:none}.node-details-header{display:flex;align-items:center;gap:11px;flex-shrink:0;padding:15px 18px 12px;border-bottom:1px solid #7993a422;cursor:grab;touch-action:none}.node-details-header>svg{color:#8093a4}.node-details-header>div{min-width:0;flex:1}.node-details-header small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#8ea0b1;font-size:10px}.node-details-header h2{margin:5px 0 0;font-size:16px;line-height:1.4;font-weight:600}.node-details-header button{display:grid;place-items:center;flex:0 0 29px;height:29px;border:0;border-radius:7px;background:#6e879b13;color:inherit;cursor:pointer}.node-details-tabs{display:flex;gap:4px;flex-shrink:0;padding:7px 16px;border-bottom:1px solid #7993a422}.node-details-tabs button{padding:8px 12px;border:0;border-radius:7px;background:transparent;color:#96a8b8;font:inherit;font-size:12px;cursor:pointer}.node-details-tabs button[aria-selected=true]{background:#7ba9bf1b;color:#c2e7f5}.node-details-host button:focus-visible{outline:2px solid #8bd6ed;outline-offset:2px}.node-details-content{min-height:0;overflow:auto;overscroll-behavior:contain;padding:18px 22px 24px}.node-details-content :deep(.inspector-body){padding:0}.node-details-content :deep(.inspector-image){max-width:100%;max-height:280px}.node-details-content :deep(.inspector-image img){object-fit:contain}.node-details-content :deep(.metric-grid){grid-template-columns:repeat(4,minmax(0,1fr))}.node-details-content :deep(.task-review-panel){padding:0}.node-details-note,.node-details-empty{color:#91a5b6;font-size:11px;line-height:1.8}.node-details-empty{padding:24px 0}.node-details-summary{font-size:12px;line-height:1.8}.node-details-warning{color:#d4ac7c;font-size:12px;line-height:1.8}.node-details-return,.node-details-prompt-links button,.node-details-experiment{display:inline-flex;align-items:center;gap:6px;padding:8px 11px;border:1px solid #7194aa44;border-radius:7px;background:#7194aa12;color:#b1d9ea;font:inherit;font-size:11px;cursor:pointer}.node-details-return{margin-bottom:16px}.node-details-prompt-links{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.node-details-experiment:disabled{opacity:.4;cursor:default}.node-details-source-list{display:grid;gap:6px}.node-details-source-list>button{display:flex;align-items:center;gap:12px;padding:10px;border:1px solid #7194aa22;border-radius:8px;background:#7194aa08;color:inherit;text-align:left;cursor:pointer}.node-details-source-list img{width:60px;height:48px;object-fit:contain;border-radius:5px}.node-details-source-list span{min-width:0}.node-details-source-list b,.node-details-source-list small{display:block;font-size:11px;line-height:1.8}.node-details-source-list small{color:#94a7b7}@media(max-width:700px){.node-details-header{padding:12px}.node-details-content{padding:14px}.node-details-tabs{padding:6px 10px}.node-details-tabs button{padding:7px 8px;font-size:11px}.node-details-content :deep(.metric-grid){grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
