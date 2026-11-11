<script setup lang="ts">
import { computed } from 'vue'
import { Check, Circle, Sparkles } from 'lucide-vue-next'
import type { WorkbenchTask } from '../types'

const props = defineProps<{ task?: WorkbenchTask }>()
const steps = computed(() => {
  const task = props.task
  const stage = task?.stage || ''
  const done = task?.status === 'completed'
  const failed = ['failed', 'paused_memory_pressure', 'interrupted'].includes(task?.status || '')
  const grouped = !!task?.sequenceMetadata && (task.generationSequences?.length || 0) > 0
  const quality = task?.deliveryQuality
  return [
    { label: '导入素材', state: task?.jobId ? 'done' : task ? 'active' : 'waiting' },
    { label: '拆解场次', state: grouped ? quality?.aggregation.state === 'stale' || quality?.aggregation.state === 'partial' ? 'review' : 'done' : done ? 'review' : task?.jobId ? 'active' : 'waiting' },
    { label: '检查分镜', state: done ? quality?.overall === 'partial' ? 'review' : 'done' : ['analyzing', 'prompting', 'event_planning', 'finalizing'].includes(stage) ? 'active' : 'waiting' },
    { label: '生成首帧', state: 'future' },
    { label: '生成视频', state: 'future' },
    { label: '导出', state: done ? 'available' : 'waiting' },
  ].map((step) => ({ ...step, state: failed && step.state === 'active' ? 'failed' : step.state }))
})
</script>

<template>
  <nav class="workflow-rail" aria-label="当前任务工作流程">
    <div v-for="(step, index) in steps" :key="step.label" class="workflow-step" :class="`workflow-${step.state}`" :title="step.state === 'future' ? '待接入生成服务' : undefined">
      <span class="workflow-step-icon"><Check v-if="step.state === 'done'" :size="12" /><Sparkles v-else-if="step.state === 'active'" :size="12" /><Circle v-else :size="10" /></span>
      <span class="workflow-step-label">{{ step.label }}</span>
      <small v-if="step.state === 'future'">待接入</small>
      <small v-if="step.state === 'review'">需复核</small>
      <i v-if="index < steps.length - 1" class="workflow-step-line" />
    </div>
  </nav>
</template>
