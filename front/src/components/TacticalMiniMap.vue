<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useVueFlow, type Node } from '@vue-flow/core'
import { minimapGeometry, minimapNodeKind, minimapNodeRect, minimapNodeSize, type MiniNodeKind } from '../minimapGeometry'

const props = defineProps<{ nodes: Node[]; viewport: { x: number; y: number; zoom: number }; activeKeys: string[]; reduced: boolean }>()
const { dimensions, setCenter, zoomTo } = useVueFlow()
const svg = ref<SVGSVGElement>()
const dragging = ref(false)
const W = 204
const H = 112
const markerColors: Record<MiniNodeKind, string> = { input: '#b9c8d9', shot: '#75c9e8', scene: '#75c9e8', prompt: '#64d7b5', 'event-prompt': '#64d7b5', 'image-generation': '#e8ae76', 'video-generation': '#a6a2f5', other: '#71869b' }
let panRaf = 0
let panTarget: { x: number; y: number } | null = null
let panCurrent: { x: number; y: number } | null = null
const sizedNodes = computed(() => props.nodes.map(node => ({ node, position: node.position, ...minimapNodeSize(node) })))
const geometry = computed(() => minimapGeometry(sizedNodes.value, props.viewport, { width: dimensions.value.width || 1200, height: dimensions.value.height || 800 }, { width: W, height: H }))
const marks = computed(() => sizedNodes.value.map(({ node, ...size }) => ({
  id: node.id,
  type: node.type,
  kind: minimapNodeKind(node),
  ...minimapNodeRect(size, geometry.value),
  hidden: Boolean(node.hidden),
  active: props.activeKeys.includes(String(node.data?.taskKey || node.id)),
})))
const pings = computed(() => props.reduced ? [] : marks.value.filter((node) => node.active && (node.type === 'project' || node.type === 'scene')).slice(0, 6))
function flowPoint(event: PointerEvent) {
  const rect = svg.value?.getBoundingClientRect()
  if (!rect) return null
  return geometry.value.invert((event.clientX - rect.left) * W / rect.width, (event.clientY - rect.top) * H / rect.height)
}
function smoothPan() {
  if (!panCurrent || !panTarget) { panRaf = 0; return }
  panCurrent = { x: panCurrent.x + (panTarget.x - panCurrent.x) * .3, y: panCurrent.y + (panTarget.y - panCurrent.y) * .3 }
  void setCenter(panCurrent.x, panCurrent.y, { duration: 0, zoom: props.viewport.zoom })
  if (Math.hypot(panTarget.x - panCurrent.x, panTarget.y - panCurrent.y) > .6) panRaf = requestAnimationFrame(smoothPan)
  else { panRaf = 0; panCurrent = null; panTarget = null }
}
function pan(event: PointerEvent) {
  const target = flowPoint(event)
  if (!target) return
  if (props.reduced) { void setCenter(target.x, target.y, { duration: 0, zoom: props.viewport.zoom }); return }
  panTarget = target
  if (!panRaf) {
    const zoom = Math.max(.01, props.viewport.zoom)
    panCurrent = { x: (dimensions.value.width / 2 - props.viewport.x) / zoom, y: (dimensions.value.height / 2 - props.viewport.y) / zoom }
    panRaf = requestAnimationFrame(smoothPan)
  }
}
function pointerDown(event: PointerEvent) {
  dragging.value = true
  svg.value?.setPointerCapture(event.pointerId)
  pan(event)
}
function pointerMove(event: PointerEvent) { if (dragging.value) pan(event) }
function pointerUp(event: PointerEvent) { dragging.value = false; if (svg.value?.hasPointerCapture(event.pointerId)) svg.value.releasePointerCapture(event.pointerId) }
function wheel(event: WheelEvent) { void zoomTo(Math.max(.25, Math.min(1.6, props.viewport.zoom * (event.deltaY > 0 ? .9 : 1.1))), { duration: 120 }) }
onBeforeUnmount(() => { if (panRaf) cancelAnimationFrame(panRaf) })
function keydown(event: KeyboardEvent) {
  const zoom = Math.max(.01, props.viewport.zoom)
  const view = geometry.value.view
  const center = geometry.value.invert(view.x + view.width / 2, view.y + view.height / 2)
  const offset = 120 / zoom
  if (event.key === 'ArrowLeft') void setCenter(center.x - offset, center.y, { duration: 160, zoom: props.viewport.zoom })
  else if (event.key === 'ArrowRight') void setCenter(center.x + offset, center.y, { duration: 160, zoom: props.viewport.zoom })
  else if (event.key === 'ArrowUp') void setCenter(center.x, center.y - offset, { duration: 160, zoom: props.viewport.zoom })
  else if (event.key === 'ArrowDown') void setCenter(center.x, center.y + offset, { duration: 160, zoom: props.viewport.zoom })
  else if (event.key === '+' || event.key === '=') void zoomTo(Math.min(1.6, zoom * 1.1), { duration: 120 })
  else if (event.key === '-') void zoomTo(Math.max(.25, zoom * .9), { duration: 120 })
  else return
  event.preventDefault()
}
</script>

<template>
  <div class="tactical-minimap" role="navigation" aria-label="画布小地图" tabindex="0" @keydown="keydown">
    <svg ref="svg" :viewBox="`0 0 ${W} ${H}`" aria-hidden="true" @pointerdown.stop="pointerDown" @pointermove.stop="pointerMove" @pointerup.stop="pointerUp" @pointercancel.stop="pointerUp" @wheel.prevent.stop="wheel">
      <defs>
        <pattern id="tactical-hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(30)"><path d="M 0 0 L 0 7" stroke="#55cfe5" stroke-opacity=".18" stroke-width="1" /></pattern>
        <clipPath id="tactical-clip"><rect x="1" y="1" :width="W - 2" :height="H - 2" rx="8" /></clipPath>
      </defs>
      <rect x="1" y="1" :width="W - 2" :height="H - 2" rx="8" fill="#101923" stroke="#314351" />
      <g clip-path="url(#tactical-clip)">
        <rect :x="geometry.view.x" :y="geometry.view.y" :width="geometry.view.width" :height="geometry.view.height" rx="4" fill="url(#tactical-hatch)" stroke="#5ce5f6" stroke-width="1.4" />
        <g v-for="node in marks" :key="node.id" :data-minimap-node="node.id" :data-minimap-kind="node.kind" :opacity="node.hidden ? .45 : 1" pointer-events="none">
          <circle v-if="node.kind === 'input'" :cx="node.x + node.width / 2" :cy="node.y + node.height / 2" :r="Math.min(node.width, node.height) / 2" :fill="markerColors.input" />
          <rect v-else :x="node.x" :y="node.y" :width="node.width" :height="node.height" :fill="markerColors[node.kind]" />
        </g>
        <circle v-for="node in pings" :key="`ping-${node.id}`" class="tactical-ping" :cx="node.x + node.width / 2" :cy="node.y + node.height / 2" r="3" fill="none" stroke="#74e0ee" />
      </g>
    </svg>
    <div class="tactical-foot" aria-label="小地图图例"><span class="mini-input">● 输入</span><span class="mini-shot" title="场次与分镜">▪ 分镜</span><span class="mini-prompt">▪ Prompt</span><span class="mini-image">▪ 生图</span><span class="mini-video">▪ 视频</span></div>
  </div>
</template>

<style scoped>
.tactical-foot { flex-wrap: nowrap; white-space: nowrap; }
.tactical-foot .mini-prompt { color: #64d7b5; }
</style>
