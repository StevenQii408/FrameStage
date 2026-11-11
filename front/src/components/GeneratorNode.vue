<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { Image as ImageIcon, Images, Video, Sparkles, Play, RotateCcw, X, CircleAlert, CircleSlash } from 'lucide-vue-next'
import type { GeneratorDraft, ResolvedGenerator } from '../generatorGraph'
import type { GenerationJob, GenerationAsset } from '../api/generations'
import { generationAssetUrl } from '../api/generations'
import GenerationWaveProgress from './GenerationWaveProgress.vue'
import { generatorFrameSize } from '../generatorGeometry'
import { activeGeneratorVersion, isReferenceAsset } from '../generatorVersions'
import { generatorPreviewSize } from '../generatorPreviewGeometry'
import { cloudCancellationLabel, generationFailureFeedback } from '../generationFailure'
import type { CapturedVideoFrame } from '../mediaPlayback'

const GeneratorHistory = defineAsyncComponent(() => import('./GeneratorHistory.vue'))
const CanvasVideoPlayer = defineAsyncComponent(() => import('./CanvasVideoPlayer.vue'))
const props = defineProps<{ data: { generator: GeneratorDraft; resolved: ResolvedGenerator; reduced: boolean; visualState?: 'idle' | 'reference-ready' | 'rendering' | 'completed' | 'failed'; task?: GenerationJob; stale?: boolean; output?: GenerationAsset | null; historyOpen?: boolean; previewOpen?: boolean; previewSize?: { width: number; height: number } } }>()
const emit = defineEmits<{ historyToggle: [nodeId: string]; selectVersion: [change: { nodeId: string; versionId: string }]; previewToggle: [nodeId: string]; previewClose: [nodeId: string]; capture: [change: CapturedVideoFrame & { nodeId: string }] }>()
const surface = ref<HTMLElement>()
const previewPlayer = ref<{ pause: () => void }>()
const thumbnailVideo = ref<HTMLVideoElement>()
const previewError = ref('')
const thumbnailReady = ref(false)
const posterReady = ref(false)
const posterFailed = ref(false)
const mediaReload = ref(0)
const historyButton = ref<HTMLButtonElement>()
const historyAnchor = ref({ x: 12, y: 12 })
const referenceOnly = computed(() => isReferenceAsset(props.data.generator))
const currentTask = computed(() => {
  const task = props.data.task, generator = props.data.generator
  return task && task.node_id === generator.id && task.kind === generator.kind
    && (!generator.generationTask || generator.generationTask.jobId === task.id) ? task : null
})
const versions = computed(() => referenceOnly.value ? [] : props.data.generator.versions || [])
const activeVersion = computed(() => activeGeneratorVersion(props.data.generator))
const output = computed(() => referenceOnly.value ? null : props.data.output || activeVersion.value?.asset || null)
const albumLayers = computed(() => versions.value.filter(version => version.id !== activeVersion.value?.id).slice(0, 2).reverse())
const visible = ref(!document.hidden)
const inViewport = ref(true)
let visibilityObserver: IntersectionObserver | undefined
const impact = ref(false)
let impactTimer = 0
const visual = computed(() => {
  if (referenceOnly.value) return hasImage.value ? 'reference-ready' : 'idle'
  if (props.data.stale) return hasImage.value ? 'reference-ready' : 'idle'
  if (props.data.task && !currentTask.value) return hasImage.value ? 'reference-ready' : 'idle'
  if (failure.value) return failure.value.tone === 'error' ? 'failed' : 'idle'
  if (impact.value) return 'completed'
  if (!visible.value && props.data.visualState === 'rendering') return 'idle'
  return props.data.visualState || (hasImage.value ? 'reference-ready' : 'idle')
})
const hasImage = computed(() => Boolean(props.data.resolved.ownImageUrl))
function assetUrl(value: string) { return generationAssetUrl(value) || value }
const mediaUrl = computed(() => output.value?.url ? assetUrl(output.value.url) : props.data.resolved.ownImageUrl || '')
const videoPoster = computed(() => {
  if (!output.value?.mime_type.startsWith('video/')) return ''
  const source = generationAssetUrl(output.value.url)
  if (!source) return ''
  const url = new URL(source)
  if (!/^\/api\/generation-assets\/[^/]+$/.test(url.pathname)) return ''
  url.pathname += '/poster'
  url.hash = ''
  return url.href
})
const rendering = computed(() => !referenceOnly.value && !props.data.stale && currentTask.value
  && ['queued', 'submitting', 'running', 'finalizing'].includes(currentTask.value.status))
const failure = computed(() => {
  const task = currentTask.value
  if (referenceOnly.value || props.data.stale || !task) return null
  return generationFailureFeedback(task)
})
const previewOpen = computed(() => Boolean(props.data.previewOpen && mediaUrl.value))
const frameSize = computed(() => {
  const width = output.value?.width || props.data.resolved.ownImageWidth
  const height = output.value?.height || props.data.resolved.ownImageHeight
  if (previewOpen.value) {
    const preview = props.data.previewSize
    if (preview && Number.isFinite(preview.width) && preview.width > 0 && Number.isFinite(preview.height) && preview.height > 0) return preview
    return generatorPreviewSize({ width, height }, { width: window.innerWidth, height: window.innerHeight })
  }
  return generatorFrameSize(props.data.generator, { width, height })
})
const taskLabel = computed(() => referenceOnly.value ? '参考资产' : props.data.stale ? (output.value ? '输入已更改 · 保留置顶版本' : '输入已变化 · 旧任务待更新') : currentTask.value
  ? ({ blocked: '等待接入生成 API', queued: '等待执行', submitting: '正在提交', running: currentTask.value.progress == null ? '生成中' : `生成中 ${currentTask.value.progress}%`, finalizing: '正在保存产物', succeeded: '生成完成', failed: '生成失败', cancelled: cloudCancellationLabel(currentTask.value) || '任务已取消', interrupted: '执行已中断' }[currentTask.value.status]) : '')
let frame = 0
let currentX = 0
let currentY = 0
let targetX = 0
let targetY = 0
let active = false
let dragStart: { x: number; y: number } | null = null
let suppressPreviewUntil = 0
let closingRequested = false
function clearTilt() {
  if (frame) cancelAnimationFrame(frame)
  frame = 0
  active = false
  currentX = currentY = targetX = targetY = 0
  surface.value?.classList.remove('generator-tilting')
  surface.value?.style.removeProperty('--generator-tilt-x')
  surface.value?.style.removeProperty('--generator-tilt-y')
}
function pauseMedia() {
  previewPlayer.value?.pause()
  if (thumbnailVideo.value && !thumbnailVideo.value.paused) thumbnailVideo.value.pause()
}
function closePreview() {
  pauseMedia()
  if (previewOpen.value && !closingRequested) { closingRequested = true; emit('previewClose', props.data.generator.id) }
}
function pointerDown(event: PointerEvent) {
  if ((event.target as HTMLElement)?.closest('button,input,select,textarea,[role="menu"],[role="slider"]')) return
  dragStart = { x: event.clientX, y: event.clientY }
  clearTilt()
}
function pointerUp(event: PointerEvent) {
  if (dragStart && Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) > 5) suppressPreviewUntil = Date.now() + 350
  dragStart = null
}
function doubleClick(event: MouseEvent) {
  if ((event.target as HTMLElement)?.closest('button,input,select,textarea,[role="menu"],[role="slider"],[data-video-controls],.player-controls,.player-capture')) return
  event.stopPropagation()
  if (!mediaUrl.value || Date.now() < suppressPreviewUntil) return
  previewError.value = ''
  clearTilt()
  if (previewOpen.value) closePreview()
  else emit('previewToggle', props.data.generator.id)
}
function forwardCapture(value: CapturedVideoFrame) { emit('capture', { ...value, nodeId: props.data.generator.id }) }
function primeThumbnail() {
  const media = thumbnailVideo.value
  if (!media || !Number.isFinite(media.duration) || media.duration <= 0) return
  // Safari may fetch only metadata for a paused video. Request one decoded
  // frame without autoplay; the server poster remains visible in the meantime.
  try { media.currentTime = Math.min(.04, media.duration / 2) } catch { /* Poster still provides a complete still frame. */ }
}
function thumbnailLoaded() { thumbnailReady.value = true; previewError.value = '' }
function mediaFailed() { previewError.value = output.value?.mime_type.startsWith('video/') ? '视频预览加载失败，请重试或双击查看。' : '图片预览加载失败，请重试。' }
function retryMedia() { previewError.value = ''; thumbnailReady.value = posterReady.value = posterFailed.value = false; mediaReload.value++ }
function playPreview() { clearTilt(); previewError.value = ''; emit('previewToggle', props.data.generator.id) }
function paint() {
  frame = 0
  const card = surface.value
  if (!card || props.data.reduced || previewOpen.value) return
  currentX += (targetX - currentX) * .23
  currentY += (targetY - currentY) * .23
  card.style.setProperty('--generator-tilt-x', currentX.toFixed(2) + 'deg')
  card.style.setProperty('--generator-tilt-y', currentY.toFixed(2) + 'deg')
  if (Math.abs(targetX - currentX) + Math.abs(targetY - currentY) > .05) frame = requestAnimationFrame(paint)
  else if (!active) card.classList.remove('generator-tilting')
}
function schedule() { if (!frame) frame = requestAnimationFrame(paint) }
function pointerMove(event: PointerEvent) {
  if (dragStart && event.buttons && Math.hypot(event.clientX - dragStart.x, event.clientY - dragStart.y) > 5) suppressPreviewUntil = Date.now() + 350
  if (props.data.reduced || previewOpen.value || event.buttons || event.pointerType === 'touch' || !surface.value
    || (event.target as HTMLElement)?.closest('button,input,select,textarea,[role="menu"],[role="slider"]')) return
  const rect = surface.value.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const x = Math.max(-1, Math.min(1, ((event.clientX - rect.left) / rect.width - .5) * 2))
  const y = Math.max(-1, Math.min(1, ((event.clientY - rect.top) / rect.height - .5) * 2))
  active = true
  surface.value.classList.add('generator-tilting')
  targetX = -y * 4
  targetY = x * 4
  surface.value.style.setProperty('--generator-spot-x', String((x + 1) * 50) + '%')
  surface.value.style.setProperty('--generator-spot-y', String((y + 1) * 50) + '%')
  schedule()
}
function pointerLeave() {
  if (previewOpen.value) { clearTilt(); return }
  active = false
  targetX = 0
  targetY = 0
  schedule()
}
watch(() => props.data.task?.status, (status, previous) => {
  if (status !== 'succeeded') { impact.value = false; clearTimeout(impactTimer) }
  if (status !== 'succeeded' || !previous || !['submitting', 'running', 'finalizing'].includes(previous)
    || !output.value || referenceOnly.value || props.data.stale || props.data.reduced || !visible.value) return
  impact.value = true
  clearTimeout(impactTimer)
  impactTimer = window.setTimeout(() => { impact.value = false }, 650)
})
watch(() => props.data.historyOpen, open => {
  if (open) void nextTick(() => {
    const rect = historyButton.value?.getBoundingClientRect()
    if (rect) historyAnchor.value = { x: rect.left, y: rect.bottom + 10 }
  })
}, { immediate: true })
watch(previewOpen, open => { closingRequested = false; clearTilt(); if (!open) pauseMedia() })
watch(mediaUrl, (url, previous) => { if (url !== previous) { previewError.value = ''; thumbnailReady.value = posterReady.value = posterFailed.value = false; closePreview() } })
function visibilityChanged() { visible.value = !document.hidden; if (!visible.value) { impact.value = false; closePreview(); clearTilt() } }
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged)
  if (typeof IntersectionObserver !== 'undefined' && surface.value) {
    visibilityObserver = new IntersectionObserver(entries => { inViewport.value = entries.some(entry => entry.isIntersecting) }, { rootMargin: '120px' })
    visibilityObserver.observe(surface.value)
  }
})
onBeforeUnmount(() => { pauseMedia(); visibilityObserver?.disconnect(); if (frame) cancelAnimationFrame(frame); clearTimeout(impactTimer); document.removeEventListener('visibilitychange', visibilityChanged) })
</script>

<template>
  <div class="generator-shell" :style="{ width: frameSize.width + 'px' }" :class="['generator-' + data.generator.kind, 'generator-state-' + visual, { 'generator-has-image': hasImage, 'generator-has-output': Boolean(output), 'generator-reduced': data.reduced, 'generator-reference-asset': referenceOnly, 'generator-has-history': versions.length > 1, 'generator-preview-open': previewOpen }]" :aria-label="referenceOnly ? '图片参考节点' : data.generator.kind === 'image' ? '图片生成节点' : '视频生成节点'" @keydown.esc.stop="closePreview">
    <Handle v-if="!referenceOnly" id="input" type="target" :position="Position.Left" class="generator-handle magnetic-port generator-handle-input" :style="{ top: frameSize.height / 2 + 'px' }" title="输入：自动识别 Prompt 或参考图片" />
    <Handle id="output" type="source" :position="Position.Right" class="generator-handle magnetic-port generator-handle-output" :style="{ top: frameSize.height / 2 + 'px' }" :title="referenceOnly ? '参考图片输出' : data.generator.kind === 'image' ? '图片输出' : output ? '视频输出' : '视频输出（待接入）'" />
    <div v-for="(version, index) in albumLayers" :key="version.id" class="generator-album-layer" aria-hidden="true" :style="{ width: frameSize.width + 'px', height: frameSize.height + 'px', '--album-offset': albumLayers.length - index }"><img v-if="data.generator.kind === 'image' && version.asset.mime_type.startsWith('image/')" :src="assetUrl(version.asset.url)" alt="" loading="lazy" decoding="async" /><Video v-else :size="28" :stroke-width="1" /></div>
    <div ref="surface" class="generator-card" :style="{ width: frameSize.width + 'px', height: frameSize.height + 'px', aspectRatio: frameSize.width + ' / ' + frameSize.height }" :class="{ 'nodrag nopan': previewOpen }" :tabindex="mediaUrl ? 0 : undefined" :title="mediaUrl ? previewOpen ? '双击收起预览' : '双击放大预览' : undefined" @pointerenter="pointerMove" @pointermove="pointerMove" @pointerleave="pointerLeave" @pointerdown="pointerDown" @pointerup="pointerUp" @dblclick.capture="doubleClick">
      <template v-if="output?.mime_type.startsWith('video/')">
        <CanvasVideoPlayer v-if="previewOpen" :key="mediaUrl" ref="previewPlayer" :src="mediaUrl" :capture-enabled="true" :autoplay="true" :muted="true" @capture="forwardCapture" @error="previewError = $event" />
        <template v-else>
          <img v-if="videoPoster && !posterFailed" :key="videoPoster + mediaReload" class="generator-video-poster" :src="videoPoster" alt="生成视频首帧预览" loading="lazy" decoding="async" @load="posterReady = true" @error="posterFailed = true; posterReady = false" />
          <video v-else :key="mediaUrl + mediaReload" ref="thumbnailVideo" :src="mediaUrl" muted playsinline preload="metadata" class="generator-video-thumbnail" @loadedmetadata="primeThumbnail" @loadeddata="thumbnailLoaded" @seeked="thumbnailLoaded" @error="mediaFailed" />
          <span v-if="!thumbnailReady && !posterReady && !previewError" class="generator-media-loading">正在加载视频预览…</span>
          <button type="button" class="generator-thumbnail-play nodrag nopan" :class="{ 'play-above-feedback': Boolean(failure) }" aria-label="播放生成视频" @pointerdown.stop @click.stop="playPreview" @dblclick.stop><Play :size="16" fill="currentColor" /></button>
        </template>
      </template>
      <img v-else-if="output?.mime_type.startsWith('image/')" :key="mediaUrl + mediaReload" :src="mediaUrl" alt="服务端生成图片" loading="lazy" decoding="async" @load="previewError = ''" @error="mediaFailed" />
      <img v-else-if="hasImage" :key="mediaUrl + mediaReload" :src="mediaUrl" alt="此节点本地导入的图片" loading="lazy" decoding="async" @load="previewError = ''" @error="mediaFailed" />
      <div v-else-if="!rendering && !failure" class="generator-placeholder">
        <span class="generator-holo"><ImageIcon v-if="data.generator.kind === 'image'" :size="33" :stroke-width="1.3" /><Video v-else :size="33" :stroke-width="1.3" /></span>
        <strong>{{ data.generator.kind === 'image' ? '图片生成' : '视频生成' }}</strong>
        <small>连接 Prompt · 导入参考图</small>
      </div>
      <GenerationWaveProgress v-if="rendering && data.task" :progress="data.task.progress" :status="data.task.status" :kind="data.generator.kind" :created-at="data.task.created_at" :updated-at="data.task.updated_at" :reduced-motion="data.reduced" :paused="!visible || !inViewport" />
      <div v-if="failure && !previewOpen" class="generator-task-feedback" :class="['feedback-' + failure.tone, { 'feedback-over-media': Boolean(mediaUrl) }]" data-generation-feedback :data-error-code="failure.code" :role="failure.tone === 'error' ? 'alert' : 'status'" aria-live="polite" :title="failure.hint">
        <CircleSlash v-if="failure.tone === 'neutral'" :size="15" aria-hidden="true" /><CircleAlert v-else :size="15" aria-hidden="true" />
        <div><strong>{{ failure.title }}</strong><small>{{ failure.hint }}</small></div>
      </div>
      <div v-if="previewOpen" class="generator-preview-heading"><span>{{ output?.mime_type.startsWith('video/') ? '视频预览' : '图片预览' }}</span><small v-if="output?.width && output?.height">{{ output.width }} × {{ output.height }}</small></div>
      <button v-if="previewOpen" type="button" class="generator-preview-close nodrag nopan" aria-label="收起节点预览" @pointerdown.stop @click.stop="closePreview" @dblclick.stop><X :size="18" /></button>
      <span v-if="previewError" class="generator-preview-error" role="alert">{{ previewError }}<button v-if="!previewOpen" type="button" class="nodrag nopan" aria-label="重新加载媒体预览" @pointerdown.stop @click.stop="retryMedia" @dblclick.stop><RotateCcw :size="13" />重试</button></span>
    </div>
    <div class="generator-external-label"><span><Sparkles :size="11" /> {{ data.generator.kind === 'image' ? 'IMAGE' : 'VIDEO' }} / {{ referenceOnly ? 'REFERENCE' : output ? 'OUTPUT' : 'LOCAL' }}</span><b>{{ taskLabel || (hasImage ? '本地图已就绪' : (data.resolved.imageSource ? '参考输入已连接' : '待接入生成 API')) }}</b><button v-if="versions.length" ref="historyButton" type="button" class="generator-history-button nodrag nopan" aria-label="查看生成历史" :aria-expanded="Boolean(data.historyOpen)" @pointerdown.stop @click.stop="emit('historyToggle', data.generator.id)" @dblclick.stop><Images :size="11" />历史 {{ versions.length }}</button></div>
    <GeneratorHistory v-if="data.historyOpen && versions.length" :kind="data.generator.kind" :versions="versions" :active-version-id="activeVersion?.id" :anchor="historyAnchor" @close="emit('historyToggle', data.generator.id)" @select-version="emit('selectVersion', { nodeId: data.generator.id, versionId: $event })" />
  </div>
</template>

<style scoped>
.generator-card { z-index: 2; box-sizing: border-box; min-height: 0; max-height: none; }
.generator-card img,.generator-card video { position: absolute; inset: 0; width: 100%; height: 100%; max-height: none; object-fit: contain; }
.generator-video-thumbnail { pointer-events: none; }
.generator-video-poster { z-index: 1; pointer-events: none; }
.generator-has-output .generator-card { backdrop-filter: none; transform-style: flat; }
.generator-has-output .generator-card:not(.generator-tilting) { transform: none; }
.generator-state-rendering .generator-card::before,.generator-state-rendering .generator-card::after { display: none; animation: none; }
.generator-task-feedback { position: absolute; inset: 0; z-index: 4; display: flex; align-items: center; justify-content: center; gap: 8px; box-sizing: border-box; padding: 16px; color: #e1b6b0; pointer-events: none; }
.generator-task-feedback > svg { flex-shrink: 0; }
.generator-task-feedback > div { min-width: 0; }
.generator-task-feedback strong { display: block; font-size: 11px; font-weight: 550; line-height: 1.5; overflow-wrap: anywhere; }
.generator-task-feedback small { display: block; margin-top: 3px; color: #b7a5a3; font-size: 9px; line-height: 1.5; }
.generator-task-feedback.feedback-over-media { inset: auto 8px 8px; justify-content: flex-start; padding: 8px 9px; border: 1px solid #c8837855; border-radius: 9px; background: #211719ed; }
.generator-task-feedback.feedback-over-media small { overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.generator-task-feedback.feedback-warning { color: #dfbf94; }
.generator-task-feedback.feedback-neutral { color: #aebcca; }
.generator-task-feedback.feedback-neutral small { color: #8f9da9; }
.generator-task-feedback.feedback-neutral.feedback-over-media { border-color: #9aaaba44; background: #17202bed; }
.generator-thumbnail-play { position: absolute; bottom: 10px; right: 10px; z-index: 2; display: grid; place-items: center; width: 32px; height: 32px; padding: 0; border: 1px solid #c3d6e34d; border-radius: 50%; background: #0b141dbd; color: #e2edf3; cursor: pointer; }
.generator-thumbnail-play:focus-visible { outline: 2px solid #9bd7ed; outline-offset: 2px; }
.generator-thumbnail-play.play-above-feedback { bottom: auto; top: 10px; }
.generator-media-loading { position: absolute; left: 12px; bottom: 12px; z-index: 2; color: #aec1ce; font-size: 10px; pointer-events: none; }
.generator-preview-open .generator-card { transform: none; background: #080b10; border-color: #abc9db80; box-shadow: 0 20px 80px #0009, 0 0 0 1px #b7d3e414; cursor: default; }
.generator-preview-open .generator-card::before,.generator-preview-open .generator-card::after { display: none; }
.generator-preview-open .generator-card img { animation: none; }
.generator-preview-open .generator-album-layer { opacity: .25; }
.generator-preview-heading { position: absolute; top: 12px; left: 14px; z-index: 4; display: flex; align-items: center; gap: 10px; padding: 5px 8px; border-radius: 7px; background: #0c131bbb; color: #e4edf4; font-size: 12px; pointer-events: none; }
.generator-preview-heading small { color: #9fb6c6; font-size: 10px; }
.generator-preview-close { position: absolute; top: 12px; right: 12px; z-index: 5; display: grid; place-items: center; width: 34px; height: 34px; padding: 0; border: 1px solid #b3cede40; border-radius: 10px; background: #101924d9; color: #e6eff5; cursor: pointer; }
.generator-preview-close:focus-visible { outline: 2px solid #a0d2e7; outline-offset: 3px; }
.generator-preview-error { position: absolute; top: 58px; left: 14px; right: 14px; z-index: 5; color: #e1b8a4; background: #231714dd; border-radius: 7px; padding: 8px; font-size: 12px; }
.generator-preview-error button { display: inline-flex; align-items: center; gap: 4px; margin-top: 5px; border: 1px solid #c4a08055; border-radius: 5px; background: #35271d; color: #e9c8b1; font: inherit; cursor: pointer; }
.generator-placeholder { box-sizing: border-box; max-width: 100%; gap: 7px; padding: 12px; }
.generator-holo { width: 44px; height: 44px; border-radius: 13px; }
.generator-external-label { flex-wrap: wrap; row-gap: 3px; }
.generator-album-layer { display: grid; place-items: center; position: absolute; inset: 0 auto auto 0; z-index: 0; box-sizing: border-box; overflow: hidden; pointer-events: none; border: 1px solid #92acc833; border-radius: 16px; background: #1a2632; transform: translate(calc(var(--album-offset) * 6px),calc(var(--album-offset) * -4px)) rotate(calc(var(--album-offset) * 1.5deg)); color: #94b0c8; }
.generator-album-layer img { width: 100%; height: 100%; object-fit: contain; opacity: .7; }
.generator-history-button { display: inline-flex; align-items: center; justify-content: center; gap: 4px; margin-left: auto; padding: 3px 5px; border: 1px solid #9ab9d12b; border-radius: 5px; color: #b3c8dc; background: #233345aa; font: inherit; font-size: 10px; cursor: pointer; white-space: nowrap; }.generator-history-button:focus-visible { outline: 2px solid #97cddd; outline-offset: 2px; }
</style>
