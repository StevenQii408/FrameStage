<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowUp, Check, ChevronDown, Expand, ImagePlus, Images, MoreHorizontal, Palette, RotateCcw, SlidersHorizontal, Square, Trash2, Unplug, X } from 'lucide-vue-next'
import { missingReferenceMentions, type GeneratorConnection, type GeneratorDraft, type ResolvedGenerator } from '../generatorGraph'
import { cloudCancellationLabel } from '../generationFailure'
import { isReferenceAsset } from '../generatorVersions'
import PromptReferenceEditor from './PromptReferenceEditor.vue'
import ReferenceThumbnailPreview from './ReferenceThumbnailPreview.vue'
import type { PromptEditorChange } from '../promptReferenceDom'
import { effectiveGenerationModelId, generationModels, generationDurationIssue, generationDurationRange, readGenerationSettings, type GenerationSettings, type GenerationJob, type GenerationCapabilities, type GenerationModelCapability } from '../api/generations'

const props = defineProps<{ generator: GeneratorDraft; resolved: ResolvedGenerator; connections?: GeneratorConnection[];
  task?: GenerationJob | null; capabilities?: GenerationCapabilities | null; stale?: boolean; busy?: boolean; taskError?: string; pendingRecovery?: boolean;
  anchor: { x: number; y: number; width?: number } }>()
const emit = defineEmits<{
  promptChange: [value: string]; settingsChange: [value: GenerationSettings]; importImage: [file: File]; removeImage: [];
  removeNode: []; disconnect: [port: 'prompt-in' | 'image-in']; analyzeStyle: [];
  submitGeneration: []; cancelGeneration: []; retryGeneration: []; close: [];
  referenceChange: [change: { id: string; selected?: boolean; firstFrame?: boolean }]; removeReference: [id: string];
}>()
const expanded = ref(false)
const popover = ref<'references' | 'settings' | 'more' | null>(null)
const composing = ref(false)
const pageVisible = ref(typeof document === 'undefined' || document.visibilityState !== 'hidden')
function updateVisibility() { pageVisible.value = document.visibilityState !== 'hidden' }
onMounted(() => document.addEventListener('visibilitychange', updateVisibility))
onBeforeUnmount(() => document.removeEventListener('visibilitychange', updateVisibility))
const promptInput = ref<InstanceType<typeof PromptReferenceEditor>>()
const modalInput = ref<InstanceType<typeof PromptReferenceEditor>>()
const referencePreview = ref<{ url: string; label: string; anchor: { left: number; top: number; bottom: number } } | null>(null)
const mentionState = ref<{ mode: 'inline' | 'modal'; start: number; end: number; query: string } | null>(null)
const mentionIndex = ref(0)
const settings = computed(() => readGenerationSettings(props.generator.generationSettings, props.generator.kind))
const references = computed(() => props.resolved.references || [])
const canvasReferences = computed(() => references.value.filter(reference => reference.sourceNodeId))
const missing = computed(() => missingReferenceMentions(props.resolved.prompt, references.value))
const mentionCandidates = computed(() => references.value.filter(reference => reference.mention.includes(mentionState.value?.query || '') && reference.url))
const kindCapability = computed(() => props.capabilities?.[props.generator.kind])
const models = computed<Array<GenerationModelCapability & { label: string }>>(() => generationModels(props.capabilities, props.generator.kind)
  .map(model => ({ ...model, label: model.label || model.name || model.id })))
const effectiveModelId = computed(() => effectiveGenerationModelId(props.capabilities, props.generator.kind, settings.value.model_id)
  || props.task?.request?.settings?.model_id || '')
const modelCapability = computed(() => models.value.find(model => model.id === effectiveModelId.value))
const executable = computed(() => props.capabilities?.configured && props.capabilities[props.generator.kind]?.available === true)
const ratios = computed(() => modelCapability.value?.aspect_ratios || kindCapability.value?.aspect_ratios || [])
const sizeOptions = computed(() => (modelCapability.value?.resolution_options || kindCapability.value?.resolution_options || [])
  .filter(option => option.aspect_ratio === settings.value.aspect_ratio))
const resolutions = computed(() => sizeOptions.value.length ? sizeOptions.value.map(option => option.value)
  : modelCapability.value?.resolutions || kindCapability.value?.resolutions || [])
const resolutionLabel = (value: string) => sizeOptions.value.find(option => option.value === value)?.label || value
const durationRange = computed(() => generationDurationRange(props.capabilities, settings.value.model_id))
const durationIssue = computed(() => generationDurationIssue(settings.value, props.generator.kind, props.capabilities))
const capabilityIssues = computed(() => {
  const issues = durationIssue.value ? [durationIssue.value] : []
  if (!kindCapability.value) return issues
  if (settings.value.model_id && !modelCapability.value && (models.value.length || executable.value)) issues.push('所选模型不属于此生成类型')
  if (ratios.value.length) {
    if (!ratios.value.includes(settings.value.aspect_ratio)) issues.push('所选画幅未被当前模型声明支持')
  } else if (executable.value) issues.push('生成服务尚未声明支持的画幅')
  if (resolutions.value.length) {
    if (!resolutions.value.includes(settings.value.resolution)) issues.push('所选分辨率未被当前模型声明支持')
  } else if (executable.value) issues.push('生成服务尚未声明支持的分辨率')
  const selected = references.value.filter(reference => reference.selected)
  if ((modelCapability.value?.supports_reference ?? kindCapability.value?.supports_reference) === false && selected.length) issues.push('当前模型不支持参考图片')
  const max = modelCapability.value?.max_reference_images ?? kindCapability.value?.max_reference_images
  if (typeof max === 'number' && selected.length > max) issues.push('参考图片数量超出当前模型支持范围')
  return issues
})
const active = computed(() => Boolean(props.task && ['queued', 'submitting', 'running', 'finalizing'].includes(props.task.status)))
const saved = computed(() => Boolean(props.task && !props.stale && props.task.status === 'blocked'))
const completed = computed(() => Boolean(props.task && !props.stale && props.task.status === 'succeeded'))
const canRetry = computed(() => props.task && ['failed', 'blocked', 'cancelled', 'interrupted'].includes(props.task.status) && executable.value)
const recoveringRetry = computed(() => props.task && props.generator.pendingSubmission?.retryOf === props.task.id
  && props.generator.pendingSubmission?.inputSignature === props.generator.generationTask?.inputSignature && !props.stale)
const status = computed(() => props.task ? ({ blocked: '等待接入生成 API', queued: '排队中', submitting: '正在提交', running: '正在生成', finalizing: '保存产物中', succeeded: '已完成', failed: '生成失败', cancelled: '已取消', interrupted: '已中断' }[props.task.status]) : '')
const position = computed(() => ({ left: props.anchor.x + 'px', top: props.anchor.y + 'px', width: (props.anchor.width || 680) + 'px', maxHeight: `calc(100vh - ${Math.max(0, props.anchor.y) + 12}px)`,
  '--composer-prompt-limit': Math.max(40, window.innerHeight - props.anchor.y - 121 - (references.value.length ? 73 : 0) - (feedback.value ? 25 : 0)) + 'px' }))
watch(() => props.generator.id, () => { expanded.value = false; mentionState.value = null; popover.value = null; referencePreview.value = null })
watch(expanded, value => { mentionState.value = null; popover.value = null; void nextTick(() => (value ? modalInput.value : promptInput.value)?.focus()) })
function togglePopover(value: typeof popover.value) { popover.value = popover.value === value ? null : value; mentionState.value = null }
function setting(key: keyof GenerationSettings, event: Event) {
  const value = (event.target as HTMLInputElement).value
  emit('settingsChange', readGenerationSettings({ ...settings.value, [key]: key === 'duration_seconds' ? Number(value) : key === 'model_id' ? value || null : value }, props.generator.kind))
}
function chooseImages(event: Event) {
  const input = event.target as HTMLInputElement
  for (const file of Array.from(input.files || [])) emit('importImage', file)
  input.value = ''
}
function mention(id: string, name: string) {
  emit('referenceChange', { id, selected: true })
  const input = expanded.value ? modalInput.value : promptInput.value
  const selection = input?.getSelection()
  const start = mentionState.value?.start ?? selection?.start ?? props.resolved.prompt.length
  const end = mentionState.value?.end ?? selection?.end ?? start
  input?.replaceRange(start, end, '@' + name + ' ')
  mentionState.value = null
  popover.value = null
}
function promptChanged(change: PromptEditorChange, mode: 'inline' | 'modal') {
  emit('promptChange', change.value)
  if (change.composing) { mentionState.value = null; return }
  const cursor = change.start
  const match = change.value.slice(0, cursor).match(/@([^\s@，。；、]*)$/)
  mentionState.value = match ? { mode, start: cursor - match[0].length, end: cursor, query: match[1] } : null
  mentionIndex.value = 0
  popover.value = null
}
function previewReference(event: MouseEvent | FocusEvent, reference: { url: string; mention: string }) {
  if (!reference.url) return
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  referencePreview.value = { url: reference.url, label: reference.mention, anchor: { left: rect.left, top: rect.top, bottom: rect.bottom } }
}
function promptKey(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229 || composing.value || !mentionState.value) return
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); mentionState.value = null; return }
  if (!mentionCandidates.value.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault(); event.stopPropagation()
    mentionIndex.value = (mentionIndex.value + (event.key === 'ArrowDown' ? 1 : -1) + mentionCandidates.value.length) % mentionCandidates.value.length
  } else if (event.key === 'Enter') {
    event.preventDefault(); event.stopPropagation()
    const reference = mentionCandidates.value[mentionIndex.value]
    mention(reference.id, reference.mention)
  }
}
function closeEscape(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229 || composing.value) return
  if (popover.value) popover.value = null
  else if (expanded.value) expanded.value = false
  else emit('close')
}
const submitLabel = computed(() => active.value ? '取消生成任务' : props.busy ? '正在校验并保存任务' : saved.value ? (executable.value ? '任务已保存' : '任务已保存，等待接入生成 API') : completed.value ? (executable.value ? '再次生成' : '生成服务未接入') : executable.value ? '生成' : '校验并保存任务')
const submitDescription = computed(() => props.pendingRecovery ? '复用原始参数和同一提交标识确认上次提交' : executable.value || active.value ? submitLabel.value : '仅校验并保存任务，未调用生成 API')
const submitDisabled = computed(() => Boolean(props.busy || !active.value && (saved.value || completed.value && !executable.value || !props.pendingRecovery && (missing.value.length || capabilityIssues.value.length))))
const specification = computed(() => [settings.value.aspect_ratio, resolutionLabel(settings.value.resolution), props.generator.kind === 'video' ? settings.value.duration_seconds + 's' : null].filter(Boolean).join(' · '))
// Show one actionable line; retain the complete validation details in its title.
const feedback = computed(() => {
  if (props.pendingRecovery) return { text: '上次提交待确认，将复用原始参数和同一提交标识。', detail: '', warning: true }
  if (missing.value.length) return { text: missing.value.join('、') + ' 未选择或已断开，请恢复引用或修改 Prompt。', detail: '', warning: true }
  if (capabilityIssues.value.length) return { text: capabilityIssues.value[0] + (capabilityIssues.value.length > 1 ? `（另 ${capabilityIssues.value.length - 1} 项）` : ''), detail: capabilityIssues.value.join('；'), warning: true }
  if (props.taskError) return { text: props.taskError, detail: '', warning: true }
  const cancellation = cloudCancellationLabel(props.task)
  if (cancellation) return { text: cancellation, detail: '', warning: false }
  if (props.task?.error && props.task.error.code !== 'provider_unconfigured') return { text: props.task.error.message, detail: '', warning: true }
  if (props.generator.pendingSubmission) return { text: '上次提交待确认，再次保存将复用同一提交标识。', detail: '', warning: true }
  if (props.stale) return { text: '输入已更改，保存为新任务。', detail: '旧任务及产物保留原始快照。', warning: false }
  if (props.task?.status === 'blocked') return { text: '任务已保存，等待接入生成 API。', detail: props.task.error?.message || '', warning: false }
  return null
})
</script>

<template>
  <Teleport to="body">
    <section v-if="!isReferenceAsset(generator)" class="generator-inline-panel nodrag nowheel" :class="{ 'is-processing': active, 'is-page-hidden': !pageVisible }" :style="position" :aria-label="generator.kind === 'image' ? '图片生成参数' : '视频生成参数'" @pointerdown.stop @click.stop @dblclick.stop @wheel.stop @keydown.esc.stop="closeEscape">
      <header class="composer-toolbar">
        <label class="composer-tool composer-upload" title="上传参考图片"><ImagePlus :size="15" /><span>上传</span><input aria-label="上传参考图片" type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif,image/avif" @change="chooseImages" /></label>
        <button class="composer-tool" type="button" aria-label="选择画布参考" :aria-expanded="popover === 'references'" @click="togglePopover('references')"><Images :size="15" />画布参考</button>
        <span class="composer-toolbar-spacer" />
        <button class="composer-icon" type="button" aria-label="更多节点操作" title="更多操作" :aria-expanded="popover === 'more'" @click="togglePopover('more')"><MoreHorizontal :size="18" /></button>
        <button class="composer-icon" type="button" aria-label="展开完整 Prompt" title="展开完整 Prompt" @click="expanded = true"><Expand :size="17" /></button>
        <button class="composer-icon composer-close" type="button" aria-label="关闭生成参数" title="关闭" @click="emit('close')"><X :size="15" /></button>
      </header>
      <div v-if="popover === 'references'" class="composer-popover composer-reference-picker" role="group" aria-label="画布参考图片" @keydown.esc.stop="popover = null">
        <p v-if="!canvasReferences.length">将画布图片连到此节点，即可选择引用。</p>
        <label v-for="reference in canvasReferences" :key="reference.id"><img :src="reference.url" alt="" /><span>@{{ reference.mention }}</span><input type="checkbox" :checked="reference.selected" :aria-label="'选择画布' + reference.mention" @change="emit('referenceChange', { id: reference.id, selected: ($event.target as HTMLInputElement).checked })" /></label>
      </div>
      <div v-if="popover === 'more'" class="composer-popover composer-more" role="menu" aria-label="节点操作" @keydown.esc.stop="popover = null">
        <button v-if="canRetry" type="button" role="menuitem" :disabled="busy || stale || task?.error?.retryable === false || Boolean(durationIssue) && !recoveringRetry" :title="recoveringRetry ? '复用同一提交标识确认上次重试' : durationIssue || (task?.error?.retryable === false ? '请修改输入后保存为新任务' : '使用原始输入重试')" @click="emit('retryGeneration'); popover = null"><RotateCcw :size="14" />重试原任务</button>
        <button v-if="connections?.some(wire => wire.targetHandle === 'prompt-in')" type="button" role="menuitem" aria-label="断开 Prompt 来源" @click="emit('disconnect', 'prompt-in'); popover = null"><Unplug :size="14" />断开 Prompt 来源</button>
        <button v-if="connections?.some(wire => wire.targetHandle === 'image-in')" type="button" role="menuitem" aria-label="断开参考图来源" @click="emit('disconnect', 'image-in'); popover = null"><Unplug :size="14" />断开参考图来源</button>
        <button v-if="generator.imageAssetId" type="button" role="menuitem" aria-label="分析本地参考图画风" @click="emit('analyzeStyle'); popover = null"><Palette :size="14" />分析参考图画风</button>
        <button class="composer-delete" type="button" role="menuitem" aria-label="删除此生成节点" @click="emit('removeNode'); popover = null"><Trash2 :size="14" />删除节点</button>
      </div>
      <div v-if="references.length" class="inline-reference-strip" aria-label="可选参考图片">
        <div v-for="reference in references" :key="reference.id" class="inline-reference" :class="{ selected: reference.selected }">
          <button class="inline-reference-mention" type="button" :aria-label="'插入 @' + reference.mention" :title="'插入 @' + reference.mention" @mousedown.prevent @mouseenter="previewReference($event, reference)" @mouseleave="referencePreview = null" @focus="previewReference($event, reference)" @blur="referencePreview = null" @click="mention(reference.id, reference.mention)"><img v-if="reference.url" :src="reference.url" :alt="reference.mention" loading="lazy" /><span v-else class="inline-reference-empty">未就绪</span><b class="inline-reference-number">{{ reference.mention.replace(/^图片/, '') }}</b></button>
          <input class="inline-reference-check" type="checkbox" :checked="reference.selected" :aria-label="'使用' + reference.mention" @change="emit('referenceChange', { id: reference.id, selected: ($event.target as HTMLInputElement).checked })" />
          <button v-if="generator.kind === 'video'" class="inline-reference-role" type="button" :aria-label="reference.mention + (reference.firstFrame ? '是首帧' : '设为首帧')" :title="reference.firstFrame ? '当前首帧' : '设为首帧'" :aria-pressed="reference.firstFrame" @click="emit('referenceChange', { id: reference.id, firstFrame: true })">{{ reference.firstFrame ? '首帧' : '首' }}</button>
          <button class="inline-reference-remove" type="button" :aria-label="'移除' + reference.mention" @click="emit('removeReference', reference.id)"><X :size="10" /></button>
        </div>
      </div>
      <div class="composer-prompt-area">
        <PromptReferenceEditor :key="generator.id + '-inline'" ref="promptInput" class="inline-prompt" aria-label="生成 Prompt" :model-value="resolved.prompt" :references="references" :aria-expanded="mentionState?.mode === 'inline'" aria-controls="inline-reference-suggestions" placeholder="描述画面或动作，输入 @ 引用参考图…" @change="promptChanged($event, 'inline')" @keydown="promptKey" @composing="composing = $event" />
        <div v-if="mentionState?.mode === 'inline'" id="inline-reference-suggestions" class="inline-mention-menu" role="listbox" aria-label="选择参考图片">
          <button v-for="(reference, index) in mentionCandidates" :key="reference.id" type="button" role="option" :aria-selected="index === mentionIndex" @mousedown.prevent @click="mention(reference.id, reference.mention)"><img :src="reference.url" alt="" /><span>@{{ reference.mention }}</span><small>{{ reference.source }}</small></button>
          <small v-if="!mentionCandidates.length">没有可用参考图，请先上传或连接。</small>
        </div>
      </div>
      <p v-if="feedback" class="inline-feedback" :class="{ warning: feedback.warning }" :role="feedback.warning ? 'alert' : 'status'" :title="feedback.detail || feedback.text">{{ feedback.text }}</p>
      <footer class="composer-footer">
        <label class="composer-model" :title="effectiveModelId || '正在读取后端模型信息'"><span>模型</span><select aria-label="生成模型" :disabled="!models.length" :value="effectiveModelId" @change="setting('model_id', $event)"><option v-if="effectiveModelId && !modelCapability" :value="effectiveModelId" disabled>{{ effectiveModelId }}（不可用）</option><option v-if="!effectiveModelId" value="" disabled>正在读取模型信息</option><option v-for="model in models" :key="model.id" :value="model.id">{{ model.label }}</option></select><ChevronDown :size="12" /></label>
        <div class="composer-specification">
          <button class="composer-specification-button" type="button" aria-label="生成规格" :aria-expanded="popover === 'settings'" @click="togglePopover('settings')"><SlidersHorizontal :size="13" /><span>{{ specification }}</span><ChevronDown :size="12" /></button>
          <div v-if="popover === 'settings'" class="composer-popover inline-generation-settings" role="group" aria-label="生成规格" @keydown.esc.stop="popover = null">
            <label>画幅<select aria-label="生成画幅" :disabled="!ratios.length" :value="settings.aspect_ratio" @change="setting('aspect_ratio', $event)"><option v-if="!ratios.includes(settings.aspect_ratio)" :value="settings.aspect_ratio" disabled>{{ settings.aspect_ratio }}（{{ kindCapability ? '不支持' : '待核对' }}）</option><option v-for="ratio in ratios" :key="ratio">{{ ratio }}</option></select></label>
            <label>{{ generator.kind === 'image' ? '图片尺寸' : '分辨率' }}<select aria-label="生成分辨率" :disabled="!resolutions.length" :value="settings.resolution" @change="setting('resolution', $event)"><option v-if="!resolutions.includes(settings.resolution)" :value="settings.resolution" disabled>{{ settings.resolution }}（{{ kindCapability ? '不支持' : '待核对' }}）</option><option v-for="resolution in resolutions" :key="resolution" :value="resolution">{{ resolutionLabel(resolution) }}</option></select></label>
            <label v-if="generator.kind === 'video'">时长（秒）<input aria-label="生成时长" type="number" :min="durationRange.min" :max="durationRange.max" step="1" :value="settings.duration_seconds" @change="setting('duration_seconds', $event)" /></label>
          </div>
        </div>
        <small v-if="active" class="composer-task-state" role="status" :title="task?.progress == null ? '等待服务端进度' : status">{{ task?.progress == null ? status : `${status} ${task.progress}%` }}</small>
        <span class="composer-footer-spacer" />
        <button class="composer-submit" type="button" :aria-label="submitLabel" :aria-description="submitDescription" :title="submitDescription" :disabled="submitDisabled" :aria-busy="busy || active" @click="active ? emit('cancelGeneration') : emit('submitGeneration')"><Square v-if="active" :size="16" /><Check v-else-if="saved || completed && !executable" :size="20" /><ArrowUp v-else :size="22" /></button>
      </footer>
      <div v-if="active" class="inline-generation-progress" role="progressbar" :aria-label="task?.progress == null ? '等待服务端进度' : '生成进度'" :aria-valuenow="task?.progress ?? undefined" aria-valuemin="0" aria-valuemax="100"><i :style="{ width: task?.progress == null ? '100%' : task.progress + '%' }" :class="{ indeterminate: task?.progress == null }" /></div>
    </section>
    <div v-if="expanded && !isReferenceAsset(generator)" class="generator-prompt-modal-backdrop" @pointerdown.stop @click.self="expanded = false" @keydown.esc.stop="closeEscape">
      <section class="generator-prompt-modal" role="dialog" aria-modal="true" aria-labelledby="generator-full-prompt-title" @click.stop>
        <header><strong id="generator-full-prompt-title">完整 Prompt</strong><button class="composer-icon" type="button" aria-label="收起完整 Prompt" @click="expanded = false"><X :size="18" /></button></header>
        <div class="composer-modal-editor">
          <PromptReferenceEditor :key="generator.id + '-modal'" ref="modalInput" class="full-prompt-editor" aria-label="完整 Prompt" :model-value="resolved.prompt" :references="references" :aria-expanded="mentionState?.mode === 'modal'" aria-controls="modal-reference-suggestions" @change="promptChanged($event, 'modal')" @keydown="promptKey" @composing="composing = $event" />
          <div v-if="mentionState?.mode === 'modal'" id="modal-reference-suggestions" class="inline-mention-menu" role="listbox" aria-label="选择参考图片">
            <button v-for="(reference, index) in mentionCandidates" :key="reference.id" type="button" role="option" :aria-selected="index === mentionIndex" @mousedown.prevent @click="mention(reference.id, reference.mention)"><img :src="reference.url" alt="" /><span>@{{ reference.mention }}</span><small>{{ reference.source }}</small></button>
            <small v-if="!mentionCandidates.length">没有可用参考图，请先上传或连接。</small>
          </div>
        </div>
        <footer><small>{{ resolved.prompt.length }}/10000</small><button type="button" @click="expanded = false">完成编辑</button></footer>
      </section>
    </div>
    <ReferenceThumbnailPreview v-if="referencePreview" v-bind="referencePreview" @close="referencePreview = null" />
  </Teleport>
</template>

<style scoped>
.generator-inline-panel { position: fixed; z-index: 90; box-sizing: border-box; padding: 12px 14px 10px; color: #e1e4e8; border: 1px solid #ffffff16; border-radius: 20px; background: #1d1f23f7; box-shadow: 0 16px 48px #0006, inset 0 1px #ffffff05; font-family: inherit; }
button,select,input,textarea { font: inherit; }button { display: inline-flex; justify-content: center; align-items: center; gap: 6px; border: 0; color: inherit; cursor: pointer; background: transparent; }button:disabled,select:disabled { opacity: .42; cursor: default; }button:focus-visible,select:focus-visible,input:focus-visible,textarea:focus-visible,[data-prompt-editor]:focus-visible,.composer-upload:focus-within { outline: 2px solid #8cbbd5; outline-offset: 2px; }button svg { flex-shrink: 0; }
.composer-toolbar,.composer-footer { display: flex; align-items: center; gap: 7px; }.composer-toolbar { position: relative; margin-bottom: 10px; }.composer-toolbar-spacer,.composer-footer-spacer { flex: 1; }.composer-tool { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border-radius: 8px; background: #ffffff08; color: #b2b6be; font-size: 12px; line-height: 18px; white-space: nowrap; }.composer-tool:hover,.composer-icon:hover,.composer-specification-button:hover { color: #edf2f7; background: #ffffff0d; }.composer-upload { position: relative; cursor: pointer; }.composer-upload input { position: absolute; inset: 0; width: 100%; opacity: 0; cursor: pointer; }.composer-icon { width: 29px; height: 29px; padding: 0; border-radius: 7px; color: #9ca4ae; }.composer-close { width: 23px; }
.inline-reference-strip { display: flex; gap: 8px; overflow-x: auto; padding: 2px 2px 5px; margin: 0 -2px 8px; }.inline-reference { flex: 0 0 58px; width: 58px; height: 58px; aspect-ratio: 1; position: relative; border: 1px solid #ffffff1a; border-radius: 10px; opacity: .48; }.inline-reference.selected { border-color: #92bac66b; opacity: 1; }.inline-reference-mention { width: 100%; height: 100%; padding: 0; overflow: hidden; border-radius: 9px; background: #10151b; }.inline-reference img,.inline-reference-empty { width: 100%; height: 100%; aspect-ratio: 1; object-fit: cover; font-size: 9px; }.inline-reference-number { position: absolute; top: 3px; left: 3px; width: 17px; height: 17px; border-radius: 50%; background: #090e16da; color: #e6e9ed; font-size: 10px; line-height: 17px; font-weight: 500; }.inline-reference-check { position: absolute; bottom: 3px; left: 3px; width: 11px; height: 11px; margin: 0; accent-color: #8dbac5; cursor: pointer; }.inline-reference-role { position: absolute; bottom: 2px; right: 2px; padding: 2px 3px; border-radius: 4px; background: #101720d9; color: #aab3bc; font-size: 8px; line-height: 12px; }.inline-reference-role[aria-pressed="true"] { color: #c5edf0; }.inline-reference-remove { position: absolute; right: -4px; top: -4px; width: 16px; height: 16px; padding: 0; border-radius: 50%; background: #38404a; opacity: 0; }.inline-reference:hover .inline-reference-remove,.inline-reference:focus-within .inline-reference-remove { opacity: 1; }
.composer-prompt-area,.composer-modal-editor { position: relative; }.composer-prompt-area :deep(.inline-prompt) { display: block; box-sizing: border-box; width: 100%; height: min(110px,var(--composer-prompt-limit)); min-height: min(80px,var(--composer-prompt-limit)); max-height: min(200px,var(--composer-prompt-limit)); resize: vertical; padding: 3px 2px; border: 0; border-radius: 3px; color: #e2e5e9; background: transparent; font-size: 13px; line-height: 1.85; }.composer-prompt-area :deep(.inline-prompt)::placeholder { color: #747b85; }.composer-prompt-area :deep(.inline-prompt:focus) { outline: 0; }.inline-feedback { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin: 5px 0 2px; color: #9ba6b2; font-size: 10px; line-height: 18px; }.inline-feedback.warning { color: #dfb787; }
.composer-footer { position: relative; gap: 10px; margin-top: 9px; min-height: 36px; }.composer-model { display: flex; align-items: center; gap: 6px; min-width: 0; max-width: 42%; color: #b7bdc6; font-size: 11px; white-space: nowrap; }.composer-model > span { color: #8b939e; }.composer-model select { min-width: 55px; max-width: 180px; padding: 6px 1px; border: 0; appearance: none; background: transparent; color: #d8dce3; text-overflow: ellipsis; cursor: pointer; }.composer-model option { background: #252b33; }.composer-model > svg { margin-left: -3px; pointer-events: none; }.composer-specification { position: relative; min-width: 0; }.composer-specification-button { padding: 6px 7px; border-radius: 7px; color: #b6bec8; font-size: 11px; white-space: nowrap; }.composer-task-state { color: #94b9c5; font-size: 10px; white-space: nowrap; }.composer-submit { flex: 0 0 35px; width: 35px; height: 35px; padding: 0; border-radius: 50%; color: #151a21; background: #eff2f4; }.composer-submit:not(:disabled):hover { background: #fff; box-shadow: 0 0 16px #dbeafa1c; }.composer-submit[aria-busy="true"] { color: #cae7ef; background: #35576a; }
.composer-popover { position: absolute; z-index: 5; padding: 8px; border: 1px solid #ffffff20; border-radius: 11px; background: #252a31; box-shadow: 0 12px 32px #0008; }.composer-more { top: 44px; right: 47px; display: grid; min-width: 165px; }.composer-more button { justify-content: flex-start; padding: 9px; border-radius: 6px; color: #c5ccd5; font-size: 11px; text-align: left; }.composer-more button:hover:not(:disabled) { background: #ffffff0b; }.composer-more .composer-delete { color: #dfaaaa; }.composer-reference-picker { left: 14px; top: 49px; min-width: 240px; max-height: 215px; overflow: auto; }.composer-reference-picker p { max-width: 280px; margin: 6px; color: #a2abb6; font-size: 11px; line-height: 1.6; }.composer-reference-picker label { display: flex; align-items: center; gap: 9px; padding: 5px; font-size: 11px; }.composer-reference-picker img { width: 34px; height: 32px; object-fit: contain; border-radius: 4px; }.composer-reference-picker input { margin-left: auto; accent-color: #8dbac5; }.inline-generation-settings { bottom: calc(100% + 10px); left: 0; width: 210px; display: grid; gap: 11px; padding: 14px; }.inline-generation-settings label { display: grid; grid-template-columns: 70px 1fr; align-items: center; gap: 10px; color: #aab4bf; font-size: 11px; }.inline-generation-settings select,.inline-generation-settings input { box-sizing: border-box; width: 100%; min-width: 0; border: 1px solid #ffffff1c; border-radius: 6px; padding: 6px; color: #dbe2eb; background: #1b2129; font: inherit; }
.inline-mention-menu { position: absolute; z-index: 6; bottom: 0; left: 0; display: grid; gap: 3px; min-width: 220px; max-width: 100%; padding: 5px; max-height: 170px; overflow: auto; border: 1px solid #7897a755; border-radius: 9px; background: #242e39; box-shadow: 0 10px 24px #0006; }.inline-mention-menu button { justify-content: flex-start; padding: 5px; border-radius: 5px; font-size: 11px; }.inline-mention-menu button[aria-selected="true"] { background: #67889a44; }.inline-mention-menu img { width: 34px; height: 30px; object-fit: contain; border-radius: 4px; }.inline-mention-menu small { padding: 4px; color: #9caab8; font-size: 9px; }
.inline-generation-progress { position: absolute; overflow: hidden; left: 15px; right: 15px; bottom: 0; height: 2px; border-radius: 2px; background: #a3bbcb18; }.inline-generation-progress i { display: block; height: 100%; background: #9fcddc; transition: width .18s ease; }.inline-generation-progress i.indeterminate { background: linear-gradient(90deg,transparent,#9fcddc,transparent); animation: composer-wait 1.6s ease-in-out infinite; }.is-page-hidden .inline-generation-progress i { animation: none; transition: none; }.is-processing { border-color: #84bed24d; }@keyframes composer-wait { 0%,100% { opacity: .25; }50% { opacity: .8; } }
.generator-prompt-modal-backdrop { position: fixed; z-index: 120; inset: 0; display: grid; place-items: center; padding: 24px; background: #080c14ba; }.generator-prompt-modal { box-sizing: border-box; width: min(900px,95vw); padding: 20px; border: 1px solid #ffffff20; border-radius: 18px; color: #dce7f0; background: #20252d; box-shadow: 0 30px 90px #0008; }.generator-prompt-modal header,.generator-prompt-modal footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; }.generator-prompt-modal header { margin-bottom: 14px; }.generator-prompt-modal strong { font-size: 13px; }.generator-prompt-modal :deep(.full-prompt-editor) { box-sizing: border-box; width: 100%; height: min(55vh,600px); padding: 12px; resize: vertical; color: inherit; background: #141c25; border: 1px solid #ffffff1a; border-radius: 9px; font-size: 13px; line-height: 1.9; }.generator-prompt-modal footer { margin-top: 12px; }.generator-prompt-modal footer small { color: #889aaa; font-size: 10px; }.generator-prompt-modal footer button { padding: 7px 12px; border-radius: 7px; background: #516c7d55; font-size: 12px; }.generator-prompt-modal .inline-mention-menu { bottom: 8px; transform: none; }
@media (max-width: 640px) { .generator-inline-panel { padding: 10px; border-radius: 16px; }.composer-footer { gap: 5px; }.composer-model { max-width: 35%; }.composer-model > span,.composer-specification-button > svg:first-child { display: none; }.composer-model select { max-width: 88px; }.composer-specification-button { padding: 6px 4px; font-size: 10px; }.composer-task-state { display: none; }.composer-prompt-area :deep(.inline-prompt) { font-size: 12px; } }
@media (prefers-reduced-motion: reduce) { .inline-generation-progress i { transition: none; }.inline-generation-progress i.indeterminate { animation: none; opacity: .6; } }
</style>
