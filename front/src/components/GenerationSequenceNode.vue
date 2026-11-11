<script setup lang="ts">
import { computed, ref } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { ChevronDown, Film, Layers3 } from 'lucide-vue-next'
import StatusPill from './StatusPill.vue'
import { sceneCoverSize } from '../cardGeometry'
import { sequenceDeckSize } from '../sequenceBranchLayout'
import type { Shot } from '../types'
const props = defineProps<{ data: { shot: Shot; sources?: Shot[]; expanded?: boolean; sceneTitle?: string; lod?: 'overview' | 'strip' | 'detail'; dimmed?: boolean } }>()
const emit = defineEmits<{ select: [id: string]; toggle: [id: string] }>()
const deck = ref<HTMLElement>()
const lod = computed(() => props.data.lod || 'detail')
const sequence = computed(() => props.data.shot.generationSequence!)
const merged = computed(() => sequence.value.source_shot_ids.length > 1)
const dimensions = computed(() => {
  const size = sequenceDeckSize(props.data.shot, lod.value)
  return { width: `${size.width}px`, height: `${size.height}px` }
})
const covers = computed(() => {
  const members = (props.data.sources || []).filter(shot => shot.thumb)
  const frontMember = members.find(shot => shot.thumb === props.data.shot.thumb)
  return [props.data.shot, ...members.filter(shot => shot.id !== frontMember?.id)].filter(shot => shot.thumb)
    .slice(0, merged.value ? Math.min(5, sequence.value.source_shot_ids.length) : 1)
})
function coverStyle(shot: Shot, index: number) {
  const size = sceneCoverSize(shot, lod.value === 'overview')
  return { '--deck-index': index, width: `${size.width}px`, height: `${size.height}px` }
}
const internalCutCount = computed(() => new Set(sequence.value.timeline.flatMap(item => [
  ...(item.transition === 'cut' ? [item.source_start_time] : []),
  ...(item.source_edit_times || []),
]).filter(time => Number.isFinite(time)).map(time => Number(time.toFixed(6)))).size)
const generatedCutCount = computed(() => new Set(sequence.value.timeline.flatMap(item => [
  ...(item.transition === 'cut' ? [item.generation_start_seconds] : []),
  ...(item.generation_edit_times || []),
]).filter(time => Number.isFinite(time))).size)
const cameraSummary = computed(() => sequence.value.generation_mode === 'continuous' ? '一镜到底'
  : sequence.value.generation_mode === 'internal_cuts' ? `${generatedCutCount.value} 次生成切镜`
  : `${internalCutCount.value} 次内部切镜`)
function activate() {
  if (merged.value) emit('toggle', props.data.shot.id)
  else emit('select', props.data.shot.id)
}
function keyboardSelect(event: KeyboardEvent) {
  if (event.target !== event.currentTarget || event.repeat || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  event.stopPropagation()
  activate()
}
function tilt(event: PointerEvent) {
  if (event.buttons || !deck.value || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
  const rect = deck.value.getBoundingClientRect()
  deck.value.style.setProperty('--tilt-x', `${-((event.clientY - rect.top) / rect.height - .5) * 7}deg`)
  deck.value.style.setProperty('--tilt-y', `${((event.clientX - rect.left) / rect.width - .5) * 7}deg`)
}
function resetTilt() { deck.value?.style.removeProperty('--tilt-x'); deck.value?.style.removeProperty('--tilt-y') }
</script>

<template>
  <div class="generation-sequence-node sequence-deck-node" :class="[`sequence-deck-${lod}`, { 'sequence-deck-expanded': data.expanded, 'sequence-deck-single': !merged, 'node-dimmed': data.dimmed }]" :style="dimensions" data-testid="generation-sequence-card" role="button" tabindex="0" :aria-label="`${merged ? '合并片段' : '生成片段'} SEQ ${data.shot.number}，${sequence.source_shot_ids.length} 个来源镜头`" @click="emit('select', data.shot.id)" @keydown="keyboardSelect">
    <Handle type="target" :position="Position.Left" /><Handle type="source" :position="Position.Right" />
    <Handle v-if="data.shot.frames.length" id="frame-out" type="source" :position="Position.Right" class="frame-output-handle magnetic-port" title="输出此片段的来源参考帧" />
    <div class="sequence-kicker"><span>SEQ <b>{{ String(data.shot.number).padStart(2, '0') }}</b></span><StatusPill :status="data.shot.status" /></div>
    <button class="sequence-deck-main nodrag nopan" type="button" :aria-expanded="merged ? Boolean(data.expanded) : undefined" :aria-label="merged ? `${data.expanded ? '收拢' : '展开'} ${sequence.source_shot_ids.length} 个来源镜头` : '查看生成片段'" @click.stop="activate">
      <div ref="deck" class="sequence-deck-images" aria-hidden="true" @pointermove="tilt" @pointerleave="resetTilt">
        <div v-for="(shot, index) in covers" :key="shot.id" class="sequence-deck-image" :style="coverStyle(shot, index)"><img :src="shot.thumb" :alt="`片段来源参考帧 ${index + 1}`" loading="lazy" decoding="async" /></div>
        <div v-if="!covers.length" class="sequence-deck-placeholder"><Film :size="26" /></div>
      </div>
    </button>
    <div class="sequence-deck-copy">
      <div class="sequence-badge"><Layers3 v-if="merged" :size="11" /><Film v-else :size="11" /><strong>{{ merged ? '合并片段' : '生成片段' }}</strong><span>{{ sequence.source_shot_ids.length }} 个来源镜头</span><span v-if="data.sceneTitle" class="sequence-scene-label" :title="data.sceneTitle">{{ data.sceneTitle }}</span></div>
      <h3 :title="data.shot.action">{{ data.shot.action }}</h3>
      <p class="sequence-source-range">原片 {{ sequence.source_start_time.toFixed(2) }}–{{ sequence.source_end_time.toFixed(2) }}s<span v-if="lod === 'detail'"> · {{ sequence.generation_duration_seconds?.toFixed(2) || '待设定' }}s 建议</span></p>
      <div v-if="lod === 'detail'" class="movement">{{ cameraSummary }}</div>
      <button v-if="merged" class="sequence-toggle nodrag nopan" data-testid="sequence-toggle" type="button" :aria-expanded="Boolean(data.expanded)" @click.stop="activate"><ChevronDown :size="12" :class="{ 'sequence-chevron-open': data.expanded }" />{{ data.expanded ? '收拢来源镜头' : '展开来源镜头' }}</button>
    </div>
  </div>
</template>

<style scoped>
.sequence-deck-main{display:block;width:100%;height:146px;padding:0;border:0;background:transparent;color:inherit;cursor:pointer}
.sequence-deck-images{position:relative;width:215px;height:146px;margin-left:8px;perspective:760px;transform-style:preserve-3d;transform:rotateX(var(--tilt-x,0deg)) rotateY(var(--tilt-y,0deg));transition:transform .24s ease}
.sequence-deck-image{position:absolute;top:6px;left:2px;display:grid;place-items:center;overflow:hidden;border:0;border-radius:10px;background:transparent;box-shadow:7px 12px 21px rgba(0,0,0,.28);transform:translate3d(calc(var(--deck-index)*9px),calc(var(--deck-index)*-1.5px),calc(var(--deck-index)*-10px)) rotateY(calc(-15deg + var(--deck-index)*2deg)) rotateZ(calc(var(--deck-index)*1.4deg));transform-origin:12% 80%;transition:transform .3s ease}
.sequence-deck-image:nth-child(1){z-index:9}.sequence-deck-image:nth-child(2){z-index:8}.sequence-deck-image:nth-child(3){z-index:7}.sequence-deck-image:nth-child(4){z-index:6}.sequence-deck-image:nth-child(5){z-index:5}
.sequence-deck-image img{width:100%;height:100%;object-fit:contain;background:#151a20}
.sequence-deck-expanded .sequence-deck-image{transform:translate3d(calc(var(--deck-index)*12px),0,calc(var(--deck-index)*-5px)) rotateY(calc(-7deg + var(--deck-index)*1deg)) rotateZ(calc(var(--deck-index)*1deg))}
.sequence-deck-single .sequence-deck-image{transform:none}
.sequence-deck-placeholder{display:grid;place-items:center;width:184px;height:138px;border-radius:10px;background:rgba(105,118,133,.1);color:#899fa9}
.sequence-deck-copy{display:grid;gap:4px;padding:4px 7px;max-width:100%;overflow:hidden}.sequence-badge strong{font-weight:500}.sequence-badge span{font-size:9px}
.sequence-deck-copy h3{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin:0;color:inherit;font-size:12px;line-height:15px;font-weight:500}.sequence-toggle svg{transition:transform .25s}.sequence-chevron-open{transform:rotate(180deg)}
.sequence-deck-node:focus-visible{outline:2px solid #8bdcf2;outline-offset:4px;border-radius:10px}
.sequence-deck-main:focus-visible{outline:2px solid #8bdcf2;outline-offset:4px;border-radius:10px}
.sequence-toggle:focus-visible{outline:2px solid #8bdcf2;outline-offset:4px;border-radius:10px}
.sequence-deck-strip .sequence-deck-main{height:132px}
.sequence-deck-strip .sequence-deck-images{height:132px}.sequence-deck-strip h3{-webkit-line-clamp:1}
.sequence-deck-overview .sequence-kicker{height:18px;padding:0 4px;font-size:8px}.sequence-deck-overview .sequence-kicker :deep(.status-pill){font-size:8px}.sequence-deck-overview .sequence-deck-main{height:92px}.sequence-deck-overview .sequence-deck-images{height:92px;width:168px;margin-left:3px}.sequence-deck-overview .sequence-deck-copy{padding:3px 4px;gap:3px}.sequence-deck-overview h3{font-size:10px;line-height:13px;-webkit-line-clamp:1}.sequence-deck-overview .sequence-badge{gap:3px;font-size:8px}.sequence-deck-overview .sequence-badge span{font-size:8px}.sequence-deck-overview .sequence-scene-label{display:none}.sequence-deck-overview .sequence-source-range{font-size:8px;line-height:11px}.sequence-deck-overview .sequence-toggle{font-size:9px;line-height:12px}
.sequence-deck-node{position:relative;box-sizing:border-box;border:0;background:transparent;box-shadow:none;overflow:visible;color:#eee9e2}.sequence-kicker{display:flex;justify-content:space-between;align-items:center;height:20px;padding:0 7px;font:10px 'DM Mono',monospace;color:#d4ae81}.sequence-badge{display:flex;align-items:center;gap:5px;height:14px;font-size:10px;white-space:nowrap;color:#d4ae81}.sequence-source-range{margin:0;font:9px/13px 'DM Mono',monospace;white-space:nowrap;color:#a3aeb9}.movement{font-size:10px;line-height:13px;color:#a3aeb9}.sequence-scene-label{margin-left:auto;max-width:83px;overflow:hidden;text-overflow:ellipsis;color:#a3aeb9}.sequence-toggle{display:flex;align-items:center;gap:4px;padding:0;border:0;background:transparent;font-size:10px;line-height:15px;cursor:pointer;color:#9adbec}
@media(prefers-reduced-motion:reduce){.sequence-deck-images{transform:none!important;transition:none}.sequence-deck-image{transition:none}.sequence-toggle svg{transition:none}}
</style>
