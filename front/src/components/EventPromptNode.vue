<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { Copy, Video } from 'lucide-vue-next'
import StatusPill from './StatusPill.vue'
import type { Prompt } from '../types'

const props = defineProps<{
  data: {
    prompt: Prompt
    sequenceNumber: number
    sourceCount: number
    title?: string
    lod?: 'overview' | 'strip' | 'detail'
    dimmed?: boolean
  }
}>()
const emit = defineEmits<{ select: [id: string]; copy: [text: string] }>()
const lod = computed(() => props.data.lod || 'detail')
const fullText = computed(() => {
  const sections = [props.data.prompt.text.trim()]
  if (props.data.prompt.negativeText?.trim()) sections.push(`Negative Prompt:\n${props.data.prompt.negativeText.trim()}`)
  return sections.filter(Boolean).join('\n\n') || '等待模型生成 AIGC Prompt'
})
const dimensions = computed(() => ({
  width: lod.value === 'overview' ? '180px' : '280px',
  height: lod.value === 'overview' ? '100px' : lod.value === 'strip' ? '200px' : '280px',
  boxSizing: 'border-box' as const,
}))

function selectWithKeyboard(event: KeyboardEvent) {
  if (event.target !== event.currentTarget || event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  event.stopPropagation()
  emit('select', props.data.prompt.id)
}

function bodyKeydown(event: KeyboardEvent) {
  // Keep text scrolling local, but preserve global canvas shortcuts such as ⌘Z.
  if (!event.metaKey && !event.ctrlKey) event.stopPropagation()
}
</script>

<template>
  <div
    class="node-card event-prompt-node"
    :class="[`event-prompt-lod-${lod}`, { 'node-dimmed': data.dimmed }]"
    :style="dimensions"
    data-testid="event-prompt-card"
    role="button"
    tabindex="0"
    :aria-label="`选择完整事件 Prompt，SEQ ${data.sequenceNumber}`"
    @click="emit('select', data.prompt.id)"
    @keydown="selectWithKeyboard"
  >
    <Handle id="event-in" type="target" :position="Position.Left" class="event-prompt-handle magnetic-port" title="接收合并事件片段" />
    <Handle id="prompt-out" type="source" :position="Position.Right" class="event-prompt-handle magnetic-port" title="将完整 Seedance Prompt 连接到视频生成节点" />
    <div class="event-prompt-heading">
      <h3><Video :size="12" aria-hidden="true" />完整事件 Prompt</h3>
      <button class="event-prompt-copy nodrag nopan" type="button" aria-label="复制完整事件 Prompt" title="复制完整事件 Prompt" @click.stop="emit('copy', fullText)"><Copy :size="12" aria-hidden="true" /></button>
    </div>
    <div class="event-prompt-meta">
      <span class="event-prompt-index">SEQ {{ String(data.sequenceNumber).padStart(2, '0') }}</span>
      <span :title="`${data.sourceCount} 个来源镜头`">{{ data.sourceCount }} 来源</span>
      <span>Seedance</span>
      <StatusPill :status="data.prompt.status" />
    </div>
    <div class="event-prompt-body nodrag nowheel nopan" tabindex="0" role="region" aria-label="完整 Seedance 提示词，可滚动阅读" @keydown="bodyKeydown">
      <div v-if="data.title" class="event-prompt-title">{{ data.title }}</div>
      <div data-testid="event-prompt-text" class="event-prompt-text">{{ fullText }}</div>
    </div>
  </div>
</template>

<style scoped>
.event-prompt-heading { display: flex; align-items: center; justify-content: space-between; flex: 0 0 auto; gap: 6px; }
.event-prompt-meta :deep(.status-pill) { margin-left: auto; font-size: 9px; }
.event-prompt-node:focus-visible { outline: 2px solid #209d78; outline-offset: 3px; }
.event-prompt-body:focus-visible { outline: 2px solid #209d78; outline-offset: 3px; }
.event-prompt-copy:focus-visible { outline: 2px solid #209d78; outline-offset: 3px; }
.event-prompt-node :deep(.vue-flow__handle.event-prompt-handle::before) { border-color: #165e4b; background: #4bd4a3; box-shadow: 0 0 0 3px rgba(39, 158, 118, .16); }
.node-card.event-prompt-lod-strip { gap: 6px; }
.node-card.event-prompt-lod-overview { gap: 3px; padding: 6px 7px; border-radius: 8px; }
.node-card.event-prompt-lod-overview h3 { gap: 3px; font-size: 10px; line-height: 18px; }
.event-prompt-lod-overview .event-prompt-copy { width: 20px; height: 20px; }
.event-prompt-lod-overview .event-prompt-meta { gap: 4px; font-size: 8px; line-height: 11px; }
.event-prompt-lod-overview .event-prompt-meta :deep(.status-pill) { gap: 3px; font-size: 7px; }
.event-prompt-lod-overview .event-prompt-body { padding: 4px; border-radius: 4px; }
.event-prompt-lod-overview .event-prompt-text { font-size: 9px; line-height: 1.5; }
.event-prompt-lod-overview .event-prompt-title { margin-bottom: 4px; font-size: 8px; }
.node-card.event-prompt-node {
  position: relative;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: 8px;
  padding: 12px;
  border: 1px solid #5cae98;
  border-top: 3px solid #16836b;
  border-radius: 10px; border-color: #367e6b; border-top-color: #47bf96; background: #173029; color: #c9efdd; box-shadow: 0 8px 24px rgba(0, 0, 0, .25); }
.node-card.event-prompt-node h3 { display: flex; align-items: center; gap: 5px; margin: 0; font-size: 12px; line-height: 20px; font-weight: 600; color: #8bdfba; }
.event-prompt-meta { display: flex; align-items: center; gap: 7px; flex: 0 0 auto; font-size: 10px; line-height: 14px; white-space: nowrap; color: #8bbda6; }
.event-prompt-index { font-family: 'DM Mono', monospace; font-weight: 600; color: #6ed6ac; }
.event-prompt-body { flex: 1 1 auto; min-width: 0; min-height: 0; padding: 8px; overflow: auto; overscroll-behavior: contain; border: 1px solid #c7e1d6; border-radius: 6px; cursor: text; user-select: text; scrollbar-width: thin; border-color: #305b49; background: #10231d; scrollbar-color: #3e8065 transparent; }
.event-prompt-text { font-size: 11px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; color: #c5e4d5; }
.event-prompt-title { margin-bottom: 7px; font-size: 10px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; color: #8cc4a9; }
.event-prompt-copy { display: grid; place-items: center; flex: 0 0 auto; width: 24px; height: 24px; padding: 0; border: 1px solid #a2cbbb; border-radius: 5px; cursor: pointer; border-color: #427a62; background: #214734; color: #a0e9c2; }
.event-prompt-copy:hover { background: #2b5a42; color: #d6ffe8; }
</style>
