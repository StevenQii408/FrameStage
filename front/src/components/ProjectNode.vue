<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { Film, FileText, Play, RotateCcw, Trash2, Type, X } from 'lucide-vue-next'
import StatusPill from './StatusPill.vue'
import { apiUrl } from '../api/client'
const CanvasVideoPlayer = defineAsyncComponent(() => import('./CanvasVideoPlayer.vue'))
const props = defineProps<{ data: any; snapshotStale?: boolean }>()
const emit = defineEmits<{ submit: [payload: { inputType: 'video' | 'script'; file?: File; scriptText: string }]; draftChange: [value: string]; cancel: []; resume: []; syncResult: []; delete: [] }>()
const inputType = computed<'video' | 'script'>(() => props.data.inputType === 'script' ? 'script' : 'video')
const recoverableNetwork = computed(() => props.data.status === 'failed'
  && props.data.provider === 'bailian'
  && String(props.data.error || '').startsWith('连续两个镜头云端连接失败'))
const scriptText = ref(typeof props.data.draftScriptText === 'string' ? props.data.draftScriptText : '')
watch(scriptText, value => emit('draftChange', value))
const inputError = ref('')
const file = ref<File>()
const playerOpen = ref(false)
const player = ref<{ pause: () => void }>()
const closePlayerButton = ref<HTMLButtonElement>()
const videoSize = ref({ width: 0, height: 0 })
const cutTimes = computed(() => Array.isArray(props.data.rawCutTimes) ? props.data.rawCutTimes : [])
const localVideoUrl = ref('')
const videoUrl = computed(() => props.data.jobId ? apiUrl(`/jobs/${encodeURIComponent(props.data.jobId)}/video`) : localVideoUrl.value)
function closePlayer() { player.value?.pause?.(); playerOpen.value = false }
watch(playerOpen, async open => { if (open) { await nextTick(); closePlayerButton.value?.focus() } })
function releaseLocalVideo() { if (localVideoUrl.value) URL.revokeObjectURL(localVideoUrl.value); localVideoUrl.value = '' }
watch(() => props.data.jobId, () => { closePlayer(); releaseLocalVideo() })
onBeforeUnmount(() => { closePlayer(); releaseLocalVideo() })
const surface = ref<HTMLElement>()
const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
function pointerMove(event: PointerEvent) {
  if (reduced || event.buttons || (event.target as HTMLElement).closest('input,textarea,button') || !surface.value) return
  const rect = surface.value.getBoundingClientRect()
  const x = (event.clientX - rect.left) / rect.width - .5
  const y = (event.clientY - rect.top) / rect.height - .5
  const length = Math.hypot(x, y) || 1
  surface.value.style.setProperty('--tilt-rotate', `${(-y / length).toFixed(3)} ${(x / length).toFixed(3)} 0 ${Math.min(4, length * 7).toFixed(2)}deg`)
  surface.value.style.setProperty('--spot-x', `${((x + .5) * 100).toFixed(1)}%`)
  surface.value.style.setProperty('--spot-y', `${((y + .5) * 100).toFixed(1)}%`)
}
function pointerLeave() { surface.value?.style.removeProperty('--tilt-rotate') }
function submit() {
  inputError.value = ''
  if ([...scriptText.value].length > 10000) { inputError.value = '文本超过 10,000 字，请按场次分批输入；原文不会截断。'; return }
  if (inputType.value === 'video' && !file.value) return
  if (inputType.value === 'script' && !scriptText.value.trim()) return
  emit('submit', { inputType: inputType.value, file: file.value, scriptText: scriptText.value })
}
function onFileChange(event: Event) {
  closePlayer()
  releaseLocalVideo()
  file.value = (event.target as HTMLInputElement).files?.[0]
  if (file.value) localVideoUrl.value = URL.createObjectURL(file.value)
}
</script>
<template>
  <div v-motion :initial="{ opacity: 0, scale: 0.92, x: -18 }" :enter="{ opacity: 1, scale: 1, x: 0, transition: { type: 'spring', stiffness: 260, damping: 22 } }" ref="surface" class="node-card project-node" :class="[`project-node-${inputType}`, { 'project-node-active': ['queued', 'processing'].includes(props.data.status), 'project-node-ready': props.data.status === 'ready' }]" @pointermove="pointerMove" @pointerleave="pointerLeave">
    <i class="magic-aurora magic-aurora-a" aria-hidden="true" /><i class="magic-aurora magic-aurora-b" aria-hidden="true" />
    <i class="magic-shimmer" aria-hidden="true" />
    <div class="magic-orbit" aria-hidden="true"><i v-for="index in 16" :key="index" class="magic-particle" :style="{ '--particle-index': String(index - 1) }" /></div>
    <i class="magic-comet magic-comet-a" aria-hidden="true" /><i class="magic-comet magic-comet-b" aria-hidden="true" />
    <Handle type="source" :position="Position.Right" />
    <div class="node-kicker"><span class="eyebrow">INPUT / {{ String(props.data.index || 1).padStart(3, '0') }}</span><div class="node-kicker-actions"><StatusPill :status="props.data.status || 'processing'" /><button class="delete-root" type="button" title="删除输入节点及其全部子节点" aria-label="删除输入节点及其全部子节点" @click.stop="emit('delete')"><Trash2 :size="12" /></button></div></div>
    <h3>{{ props.data.title || '输入 / 分析任务' }}</h3>
    <p class="node-muted">{{ props.data.summary || '等待输入素材' }}</p>
    <div class="project-inputs"><span v-if="props.data.inputType !== 'script'"><Film :size="13" /> {{ props.data.filename || '参考视频' }}</span><span v-else><Type :size="13" /> 文字剧本输入</span><span><FileText :size="13" /> {{ props.data.sequenceCount ? `${props.data.shotCount} 个来源镜头 → ${props.data.sequenceCount} 个生成片段` : `${props.data.shotCount || 0} 个分镜` }}</span></div>
    <div class="progress-line"><span :style="{ width: `${props.data.progress || 0}%` }" /></div><div class="progress-meta"><span>{{ props.data.stage || '等待处理' }}</span><b>{{ props.data.progress || 0 }}%</b></div>
    <div v-if="snapshotStale" class="project-actions" role="status"><span>最终结果尚未同步</span><button type="button" @click.stop="emit('syncResult')">重新读取结果</button></div>
    <div v-else-if="props.data.jobId" class="node-running"><RotateCcw :size="13" /> {{ props.data.error || '任务状态已同步' }}</div>
    <button v-if="inputType === 'video' && videoUrl" type="button" class="text-button nodrag nopan" @click.stop="playerOpen = true"><Play :size="13" /> 查看原视频</button>
    <div v-if="props.data.quality" class="node-running" :title="props.data.quality.issues?.map((item: any) => item.message).join('；')">交付质量：{{ props.data.quality.overall === 'ready' ? '可用' : props.data.quality.overall === 'partial' ? '部分可用，查看待处理项' : '待完成' }}</div>
    <div v-if="props.data.jobId && ['queued', 'processing'].includes(props.data.status)" class="project-actions"><button type="button" @click.stop="emit('cancel')">取消任务</button></div>
    <div v-else-if="props.data.jobId && props.data.status === 'paused_memory_pressure'" class="project-actions"><span>本地模型内存不足。切换云端不会自动迁移此任务；可释放内存后继续，或重新提交素材。</span><button v-if="props.data.provider === 'mlx'" type="button" @click.stop="emit('resume')">释放内存后继续</button></div>
    <div v-else-if="props.data.jobId && recoverableNetwork" class="project-actions"><span>已完成的镜头会保留，只继续未完成的分析。</span><button type="button" @click.stop="emit('resume')">网络恢复后继续</button></div>
    <div v-else-if="props.data.jobId && props.data.status === 'interrupted'" class="project-actions"><span>服务重启，任务已中断。已保存的阶段会保留；未确认完成的请求可能需要重新调用。</span><button type="button" @click.stop="emit('resume')">从检查点继续</button></div>
    <div v-else-if="props.data.jobId && props.data.status === 'failed'" class="node-running">可检查错误后重新创建任务</div>
    <form v-if="!props.data.jobId" class="input-form" @submit.prevent="submit">
      <div class="draft-type"><Type v-if="inputType === 'script'" :size="12" /><Film v-else :size="12" />{{ inputType === 'script' ? '剧本输入节点' : '视频输入节点' }}</div>
      <textarea v-if="inputType === 'script'" v-model="scriptText" rows="3" placeholder="粘贴文字剧本，系统会识别动作节拍并智能拆镜" @click.stop />
      <input v-else type="file" accept="video/mp4,.mp4" @change="onFileChange" @click.stop />
      <span v-if="inputError" role="alert">{{ inputError }}</span>
      <button class="text-button" type="submit"><FileText :size="13" /> 开始解析</button>
    </form>
  <Teleport to="body">
    <div v-if="playerOpen" class="source-video-overlay" role="dialog" aria-modal="true" aria-label="原视频预览" @click.self="closePlayer" @keydown.esc.stop="closePlayer">
      <div class="source-video-dialog">
        <div class="source-video-header"><span><Film :size="16" /> 原视频 · {{ props.data.filename || file?.name }}</span><small v-if="videoSize.width">{{ videoSize.width }} × {{ videoSize.height }}</small><button ref="closePlayerButton" type="button" aria-label="关闭原视频预览" @click="closePlayer"><X :size="20" /></button></div>
        <CanvasVideoPlayer ref="player" :src="videoUrl" :cuts="cutTimes" title="原视频预览" @ready="value => videoSize = value" />
        <span class="source-video-hint">{{ cutTimes.length ? '进度条圆点为本地检测的原片切镜点，点击可定位。' : '原片连续播放 · 暂无检测切点' }}</span>
      </div>
    </div>
  </Teleport>
  </div>
</template>

<style scoped>
.source-video-overlay{position:fixed;inset:0;z-index:2000;background:#05080deb;display:grid;place-items:center;padding:16px}.source-video-dialog{box-sizing:border-box;position:relative;display:flex;flex-direction:column;gap:10px;width:min(96vw,1720px);height:min(94vh,1140px);padding:14px;border:1px solid #536675;border-radius:22px;background:#10161e;color:#d1dce4;box-shadow:0 24px 100px #0009}.source-video-header{display:flex;align-items:center;gap:14px;min-height:32px}.source-video-header>span{display:flex;align-items:center;gap:8px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}.source-video-header small{color:#879ba9;font-variant-numeric:tabular-nums}.source-video-header>button{display:grid;place-items:center;border:1px solid #61708066;border-radius:50%;padding:6px;background:#1d2837;color:#fff;cursor:pointer}.source-video-dialog>:deep(.canvas-video-player){flex:1;min-height:0}.source-video-hint{font-size:11px;color:#8295a6;line-height:18px}
</style>
