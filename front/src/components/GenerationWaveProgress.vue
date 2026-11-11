<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { GenerationStatus } from '../api/generations'
import { generationDisplayProgress } from '../generationProgress'
import { mountGenerationStarfield } from '../generationStarfield'

const props = defineProps<{
  progress?: number | null
  status: GenerationStatus
  paused?: boolean
  reducedMotion?: boolean
  kind: 'image' | 'video'
  createdAt?: string
  updatedAt?: string
}>()
const canvas = ref<HTMLCanvasElement>()
const now = ref(Date.now())
const reduced = ref(false)
const terminal = computed(() => !['queued', 'submitting', 'running', 'finalizing'].includes(props.status))
const motionReduced = computed(() => Boolean(props.reducedMotion || reduced.value))
const suspended = computed(() => Boolean(props.paused || motionReduced.value || terminal.value))
const keepClock = computed(() => motionReduced.value && !props.paused && !terminal.value)
const display = computed(() => generationDisplayProgress(props, now.value))
const ariaText = computed(() => display.value.estimated
  ? `${display.value.phase}，预计 ${display.value.value}%，非云端实测进度`
  : display.value.value == null ? `${display.value.phase}，等待云端状态` : `${display.value.phase} ${display.value.value}%`)
let effect: ReturnType<typeof mountGenerationStarfield> | undefined
let motionQuery: MediaQueryList | undefined
const updateMotion = () => { reduced.value = motionQuery?.matches ?? false }

onMounted(() => {
  motionQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)')
  updateMotion()
  motionQuery?.addEventListener('change', updateMotion)
  if (canvas.value) effect = mountGenerationStarfield(canvas.value, {
    kind: props.kind, paused: suspended.value, clockWhenPaused: keepClock.value,
    level: display.value.value,
    onClock: value => { now.value = value },
  })
})
watch([suspended, keepClock], ([value, clock]) => { now.value = Date.now(); effect?.setPaused(value, clock) })
watch(() => props.kind, value => effect?.setKind(value))
watch(() => display.value.value, value => effect?.setLevel(value))
watch(() => [props.status, props.progress, props.createdAt, props.updatedAt], () => { now.value = Date.now() })
onBeforeUnmount(() => { effect?.destroy(); motionQuery?.removeEventListener('change', updateMotion) })
</script>

<template>
  <div class="generation-wave-progress generation-starfield-progress"
    :class="{ 'wave-paused': suspended, 'wave-unknown': display.value == null, 'wave-video': kind === 'video' }"
    role="progressbar" aria-label="生成进度" :aria-valuenow="display.value ?? undefined"
    aria-valuemin="0" aria-valuemax="100" :aria-valuetext="ariaText" :data-estimated="display.estimated" :data-tide-level="display.value ?? 'unknown'">
    <div class="generation-nebula" aria-hidden="true" />
    <canvas ref="canvas" class="generation-starfield" aria-hidden="true" />
    <div class="generation-progress-readout">
      <span class="generation-progress-phase"><i aria-hidden="true" />{{ display.phase }}</span>
      <div v-if="display.value != null" class="generation-progress-number">
        <small v-if="display.estimated">预计</small><strong>{{ display.value }}</strong><span>%</span>
      </div>
      <span v-else class="generation-progress-waiting">等待云端状态</span>
      <span v-if="display.estimated" class="generation-progress-note">以云端完成状态为准</span>
    </div>
  </div>
</template>

<style scoped>
.generation-starfield-progress { --star-rgb: 124,218,255; position: absolute; inset: 0; z-index: 3; overflow: hidden; pointer-events: none; border-radius: inherit; isolation: isolate; background: rgba(6,10,22,.64); }
.wave-video { --star-rgb: 176,160,255; }
.generation-nebula { position: absolute; inset: 0; background: radial-gradient(ellipse at 82% 18%,rgba(95,75,135,.09),transparent 59%),linear-gradient(150deg,rgba(9,22,40,.12),rgba(5,10,23,.24)); }
.generation-nebula::after { content: ''; position: absolute; inset: 0; opacity: .12; background-image: radial-gradient(circle at 12% 20%,#c3e9ff 0 .65px,transparent 1px),radial-gradient(circle at 72% 70%,#bec7ff 0 .7px,transparent 1.2px); background-size: 109px 87px,137px 109px; }
.generation-starfield { position: absolute; inset: 0; width: 100%; height: 100%; }
.generation-progress-readout { position: absolute; left: 50%; top: 50%; transform: translate(-50%,-50%); display: flex; flex-direction: column; align-items: center; gap: 4px; width: max-content; max-width: calc(100% - 20px); color: #e3efff; text-align: center; text-shadow: 0 2px 12px rgba(1,5,16,.8); }
.generation-progress-phase { display: inline-flex; align-items: center; gap: 5px; font-size: 9px; letter-spacing: .05em; color: #bfd0e8; }
.generation-progress-phase i { width: 4px; height: 4px; border-radius: 50%; background: rgb(var(--star-rgb)); box-shadow: 0 0 8px rgba(var(--star-rgb),.5); }
.generation-progress-number { display: flex; align-items: baseline; gap: 3px; line-height: 1; }
.generation-progress-number strong { font-size: 24px; font-weight: 550; letter-spacing: -.025em; font-variant-numeric: tabular-nums; }
.generation-progress-number>span { font-size: 10px; color: rgb(var(--star-rgb)); }
.generation-progress-number small { align-self: center; margin-right: 2px; font-size: 8px; color: #a7bbda; }
.generation-progress-note { margin-top: 2px; font-size: 8px; color: #91a7c4; }
.generation-progress-waiting { margin-top: 3px; font-size: 10px; color: #d1dcf0; }
@media (prefers-reduced-motion: reduce) { .generation-starfield { opacity: .6; } }
</style>
