<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { AlertTriangle, Film, Layers3 } from 'lucide-vue-next'
import type { Shot } from '../types'
import { sceneCoverSize } from '../cardGeometry'

const props = defineProps<{ data: { sceneId: string; index: number; title: string; shots: Shot[]; sequenceMode?: boolean; timeRange: string; warningCount: number; progress: number; expanded: boolean; lod: 'overview' | 'strip' | 'detail'; groupingMode: string; analysisPaused?: boolean } }>()
const emit = defineEmits<{ toggle: [sceneId: string] }>()
const deck = ref<HTMLElement>()
const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const covers = computed(() => props.data.shots.filter((shot) => shot.thumb).slice(0, 5))
function coverStyle(shot: Shot, index: number) {
  const size = sceneCoverSize(shot, props.data.lod === 'overview')
  return { '--deck-index': index, width: `${size.width}px`, height: `${size.height}px` }
}
let timeline: ReturnType<(typeof import('gsap'))['gsap']['timeline']> | undefined
let disposed = false

watch(() => props.data.expanded, async (expanded) => {
  await nextTick()
  timeline?.kill()
  if (!deck.value || reduced) return
  const { gsap } = await import('gsap')
  if (disposed || !deck.value) return
  const cards = deck.value.querySelectorAll<HTMLElement>('.scene-deck-card')
  timeline = gsap.timeline({ defaults: { duration: 0.38, ease: 'power2.out' } })
  cards.forEach((card, index) => {
    timeline?.to(card, { x: expanded ? index * 11 : index * 8, rotationY: expanded ? -7 + index * 1.4 : -16 + index * 2.2, rotationZ: expanded ? index * 1.2 : index * 1.6, z: expanded ? index * 4 : -index * 12, opacity: 1 }, index * 0.035)
  })
})
function tilt(event: PointerEvent) {
  if (reduced || event.buttons || !deck.value || (event.target as HTMLElement).closest('input,textarea')) return
  const rect = deck.value.getBoundingClientRect()
  const x = (event.clientX - rect.left) / rect.width - .5
  const y = (event.clientY - rect.top) / rect.height - .5
  deck.value.style.setProperty('--tilt-x', `${(-y * 8).toFixed(2)}deg`)
  deck.value.style.setProperty('--tilt-y', `${(x * 8).toFixed(2)}deg`)
  deck.value.style.setProperty('--spot-x', `${((x + .5) * 100).toFixed(1)}%`)
  deck.value.style.setProperty('--spot-y', `${((y + .5) * 100).toFixed(1)}%`)
}
function resetTilt() { deck.value?.style.removeProperty('--tilt-x'); deck.value?.style.removeProperty('--tilt-y') }
onBeforeUnmount(() => { disposed = true; timeline?.kill() })
</script>

<template>
  <div class="scene-deck-node" @pointermove="tilt" @pointerleave="resetTilt" :class="{ 'scene-deck-expanded': data.expanded, 'scene-deck-overview': data.lod === 'overview' }">
    <Handle type="target" :position="Position.Left" />
    <Handle type="source" :position="Position.Right" />
    <button class="scene-deck-main" type="button" :aria-expanded="data.expanded" :aria-label="`${data.expanded ? '收拢' : '展开'}${data.title}`" @click.stop="emit('toggle', data.sceneId)">
      <div ref="deck" class="scene-deck-images" aria-hidden="true">
        <div v-for="(shot, index) in covers" :key="shot.id" class="scene-deck-card" :style="coverStyle(shot, index)">
          <img :src="shot.thumb" alt="" loading="lazy" />
        </div>
        <div v-if="!covers.length" class="scene-deck-placeholder"><Film :size="26" /></div>
      </div>
      <div class="scene-deck-copy">
        <span class="scene-deck-kicker"><Layers3 :size="12" /> SCENE {{ String(data.index).padStart(2, '0') }} <em v-if="data.groupingMode === 'legacy-presentation-group'">历史展示分组</em><em v-else-if="data.groupingMode === 'visual-fallback'">视觉分组 · 待复核</em><em v-if="data.analysisPaused">分析暂停 · 场次未核验</em></span>
        <strong>{{ data.title }}</strong>
        <small>{{ data.timeRange }} · {{ data.shots.length }} {{ data.sequenceMode ? '个生成片段' : '镜' }}</small>
        <span v-if="data.warningCount" class="scene-warning"><AlertTriangle :size="11" /> {{ data.warningCount }} 处待复核</span>
        <span class="scene-deck-action">{{ data.expanded ? '收拢场次' : data.sequenceMode ? '展开生成片段' : '展开分镜' }}</span>
      </div>
    </button>
    <i v-if="data.progress > 0 && data.progress < 100" class="scene-progress" :style="{ width: `${data.progress}%` }" />
  </div>
</template>
