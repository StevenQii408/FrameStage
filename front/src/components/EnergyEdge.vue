<script setup lang="ts">
import { computed } from 'vue'
import { energyPath, timelineReturnPath } from '../energyPath'

const props = defineProps<{ id: string; sourceX: number; sourceY: number; targetX: number; targetY: number; data?: { active?: boolean; manual?: boolean; chronological?: boolean; rowWrap?: boolean; wrapY?: number; nextLabel?: string; eventPrompt?: boolean }; selected?: boolean; flowEnabled?: boolean }>()
const flowing = computed(() => props.flowEnabled ?? Boolean(props.data?.active))
const path = computed(() => props.data?.rowWrap && props.data.wrapY !== undefined
  ? timelineReturnPath({ x: props.sourceX, y: props.sourceY }, { x: props.targetX, y: props.targetY }, props.data.wrapY)
  : energyPath({ x: props.sourceX, y: props.sourceY }, { x: props.targetX, y: props.targetY }))
const gradientId = computed(() => `beam-${props.id.replace(/[^a-z0-9_-]/gi, '-')}`)
const arrowId = computed(() => `${gradientId.value}-arrow`)
</script>

<template>
  <g class="energy-edge" :class="{ 'energy-edge-active': flowing, 'energy-edge-selected': selected && data?.manual }" aria-hidden="true">
    <defs><linearGradient :id="gradientId" x1="0%" y1="0%" x2="100%" y2="0%"><stop offset="0%" :stop-color="data?.eventPrompt ? '#64d7b5' : '#00f0ff'" /><stop offset="100%" :stop-color="data?.eventPrompt ? '#4dc4d9' : '#8a2be2'" /></linearGradient><marker v-if="data?.chronological" :id="arrowId" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 8 4 L 0 8 Z" fill="#91b9c9" /></marker></defs>
    <path class="energy-rail" :d="path" :marker-end="data?.chronological ? `url(#${arrowId})` : undefined" fill="none" />
    <path class="energy-beam" :d="path" :stroke="`url(#${gradientId})`" fill="none" />
    <circle v-if="flowing" class="energy-traveller" r="2.5" fill="#b2f4ff"><animateMotion :path="path" dur="2.6s" repeatCount="indefinite" /></circle>
    <circle v-if="flowing" class="energy-traveller energy-traveller-tail" r="1.5" fill="#b1a5ff"><animateMotion :path="path" dur="2.6s" begin="-1.3s" repeatCount="indefinite" /></circle>
    <path v-if="data?.manual" class="energy-hitbox" :d="path" fill="none" />
    <text v-if="data?.rowWrap && data.wrapY !== undefined && targetY - sourceY >= 32" class="timeline-wrap-label" :x="(sourceX + targetX) / 2" :y="data.wrapY - 7" text-anchor="middle">继续 → {{ data.nextLabel }}</text>
  </g>
</template>

<style scoped>
.energy-traveller{pointer-events:none;opacity:.92}.energy-traveller-tail{opacity:.68}
@media(prefers-reduced-motion:reduce){.energy-traveller{display:none}}
</style>
