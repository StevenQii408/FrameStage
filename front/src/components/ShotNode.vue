<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { Camera, Sparkles } from 'lucide-vue-next'
import StatusPill from './StatusPill.vue'
import type { Shot } from '../types'
import { sourceRepresentativeFrame } from '../sourceRepresentativeFrame'
const props = defineProps<{ data: { shot: Shot; sourceEvidence?: boolean; lod?: 'overview' | 'strip' | 'detail'; dimmed?: boolean } }>()
const emit = defineEmits<{ select: [id: string] }>()
const root = ref<HTMLElement>()
const mediaVisible = ref(false)
let observer: IntersectionObserver | undefined
onMounted(() => {
  if (!root.value || typeof IntersectionObserver === 'undefined') { mediaVisible.value = true; return }
  observer = new IntersectionObserver((entries) => { if (entries[0]?.isIntersecting) { mediaVisible.value = true; observer?.disconnect() } }, { rootMargin: '260px' })
  observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
const representativeFrame = computed(() => props.data.sourceEvidence ? sourceRepresentativeFrame(props.data.shot) : undefined)
const thumbnail = computed(() => representativeFrame.value?.url || props.data.shot.thumb)
const isEvidenceFrame = computed(() => representativeFrame.value && representativeFrame.value.id !== props.data.shot.frames[0]?.id)
const frameAspect = computed(() => {
  const frame = representativeFrame.value || props.data.shot.frames[0]
  return frame?.width && frame?.height ? `${frame.width} / ${frame.height}` : '16 / 9'
})
</script>
<template>
  <div v-motion :initial="{ opacity: 0, scale: 0.86, x: -22 }" :enter="{ opacity: 1, scale: 1, x: 0, transition: { type: 'spring', stiffness: 220, damping: 20 } }" ref="root" class="node-card shot-node" :class="[`shot-lod-${data.lod || 'detail'}`, { 'node-dimmed': data.dimmed }]" :data-testid="data.sourceEvidence ? 'source-shot-card' : undefined" @click="emit('select', props.data.shot.id)">
    <Handle type="target" :position="Position.Left" />
    <Handle type="source" :position="Position.Right" />
    <Handle v-if="!data.shot.script_direction" id="frame-out" type="source" :position="Position.Right" class="frame-output-handle magnetic-port" title="输出此分镜的首帧图片" />
    <div class="node-kicker"><span class="shot-index">SH <b>{{ String(data.shot.number).padStart(2, '0') }}</b></span><span v-if="data.sourceEvidence" class="source-evidence-label">{{ data.shot.script_direction ? '剧本分镜' : '原片来源' }}</span><StatusPill :status="data.shot.status" /></div>
    <div v-if="thumbnail" class="thumb-wrap" :style="{ aspectRatio: frameAspect }"><img v-if="mediaVisible" :src="thumbnail" :alt="isEvidenceFrame ? '来源动作证据帧' : '镜头缩略图'" loading="lazy" decoding="async" /><span v-else class="thumb-lazy-placeholder" aria-hidden="true" /><span v-if="isEvidenceFrame" class="source-frame-evidence">动作证据 {{ representativeFrame!.timestamp.toFixed(2) }}s</span><span class="timecode">{{ data.sourceEvidence ? data.shot.time : data.shot.duration }}</span></div>
    <div v-else class="description-wrap"><span class="description-label">分镜描述</span><p>{{ data.shot.sourceText || data.shot.action }}</p></div>
    <template v-if="data.lod !== 'strip' && data.lod !== 'overview'">
      <h3>{{ data.shot.action }}</h3>
      <span v-if="data.shot.analysisReview?.dynamic?.status === 'pending_review'" class="node-muted">动态待复核 · {{ data.shot.analysisReview.static?.status === 'ready' ? '静态场景已保留' : '查看有效证据' }}</span>
      <div class="shot-tags"><span><Camera :size="12" />{{ data.shot.shotSize }}</span><span>{{ data.shot.lens }}</span></div>
      <div class="node-divider" /><div class="movement"><Sparkles :size="13" />{{ data.shot.movement }}</div>
      <div class="confidence"><span>{{ data.shot.script_direction ? '表演估计时长' : '视觉置信度' }}</span><b>{{ data.shot.script_direction ? data.shot.duration : `${Math.round(data.shot.confidence * 100)}%` }}</b></div>
    </template>
    <div v-else class="strip-meta"><b>{{ data.shot.time }}</b><span>{{ data.shot.script_direction ? '建议时序' : `${Math.round(data.shot.confidence * 100)}%` }}</span></div>
  </div>
</template>

<style scoped>
.source-evidence-label{margin-left:auto;margin-right:6px;color:var(--text-muted,#91998f);font-size:8px;letter-spacing:0}
.source-frame-evidence{position:absolute;top:5px;left:5px;padding:2px 4px;border-radius:3px;background:rgba(18,34,26,.78);color:#d5f0df;font:8px 'DM Mono',monospace}
</style>
