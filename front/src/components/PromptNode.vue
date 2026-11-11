<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import { Copy, Image, Sparkles, Video } from 'lucide-vue-next'
import StatusPill from './StatusPill.vue'
import type { Prompt } from '../types'
const props = defineProps<{ data: { prompt: Prompt; promptIndex?: number; compact?: boolean } }>()
const emit = defineEmits<{ select: [id: string]; copy: [text: string] }>()
</script>
<template>
  <div v-motion :initial="{ opacity: 0, scale: 0.78, y: 16 }" :enter="{ opacity: 1, scale: 1, y: 0, transition: { type: 'spring', stiffness: 260, damping: 22, delay: (data.promptIndex || 0) * 45 } }" class="node-card prompt-node" :class="[`prompt-${data.prompt.type}`, { 'prompt-node-compact': data.compact }]" @click="emit('select', data.prompt.id)">
    <Handle type="target" :position="Position.Left" />
    <Handle id="prompt-out" type="source" :position="Position.Right" class="prompt-output-handle magnetic-port" title="连接到生成节点的 Prompt 输入" />
    <div class="node-kicker"><span class="prompt-type"><Image v-if="data.prompt.type === 'image' || data.prompt.type === 'first_frame'" :size="13" /><Video v-else :size="13" />{{ data.prompt.type === 'first_frame' ? '首帧图片 Prompt' : data.prompt.type === 'image' ? '画面 Prompt' : 'Seedance 视频 Prompt' }}</span><StatusPill :status="data.prompt.status" /></div>
    <div class="prompt-platform">{{ data.prompt.platform }} <span>v{{ data.prompt.version }}</span></div>
    <p>{{ data.prompt.text }}</p>
    <div class="prompt-card-footer"><span v-if="data.compact"><Sparkles :size="11" /> 点击查看完整提示词</span><button class="copy-button" :aria-label="`复制 ${data.prompt.platform} Prompt`" @click.stop="emit('copy', data.prompt.text)"><Copy :size="13" /><template v-if="!data.compact">复制 Prompt</template></button></div>
  </div>
</template>
