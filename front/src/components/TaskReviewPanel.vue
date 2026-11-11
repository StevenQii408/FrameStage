<script setup lang="ts">
import { computed } from 'vue'
import type { WorkbenchTask } from '../types'
import DeliveryQualityPanel from './DeliveryQualityPanel.vue'
const props = withDefaults(defineProps<{ task: WorkbenchTask; section?: 'report' | 'review' | 'all' }>(), { section: 'all' })
const emit = defineEmits<{ sourceShot: [id: string] }>()
const reviews = computed(() => {
  const rows = props.task.sequenceMetadata?.fact_review_requests
  return Array.isArray(rows) ? rows.filter(row => row && typeof row.source_shot_id === 'string' && row.status === 'pending') : []
})
const styles = computed(() => {
  const rows = props.task.sceneStyleReview?.scene_overrides
  return Array.isArray(rows) ? rows.filter(row => row?.status === 'review_required') : []
})
const requests = computed(() => props.task.preflight?.model_calls as { normal_estimate?: number; bounded_maximum?: number } | undefined)
</script>
<template>
  <div class="task-review-panel">
    <DeliveryQualityPanel v-if="section !== 'review'" :quality="task.deliveryQuality" :status="task.status" />
    <details v-if="section !== 'review' && task.preflight"><summary>处理范围与调用预算</summary><p v-if="task.inputType === 'script'">{{ task.preflight.content_span_count }} 个剧情片段 · {{ task.preflight.window_count }} 个导演窗口</p><p v-else>来源镜头：{{ task.preflight.source_shot_count ?? '检测后确定' }} · 事件窗口：{{ task.preflight.event_window_count ?? '分析后确定' }}</p><p>常规约 {{ requests?.normal_estimate ?? '待估算' }} 次模型请求 · 有界估算上限 {{ requests?.bounded_maximum ?? '待估算' }} 次</p><p>{{ task.preflight.cost_note || '费用取决于实际图片与输出 Token，不把未知费用显示为零。' }}</p></details>
    <details v-if="section !== 'report' && reviews.length"><summary>Agent 建议复核 {{ reviews.length }} 项事实</summary><article v-for="(review, index) in reviews" :key="index"><p>{{ review.reason }}</p><button type="button" @click="emit('sourceShot', review.source_shot_id)">查看来源并填写复核</button></article><small>打开记录不会调用模型；确认复核操作后才产生请求。</small></details>
    <details v-if="section !== 'report' && styles.length"><summary>{{ styles.length }} 个场次存在画风变化</summary><article v-for="(style, index) in styles" :key="index"><p>{{ style.scene_group_id }}：{{ style.description || style.observed_family }}</p><button v-for="id in (style.supporting_shot_ids || []).slice(0, 3)" :key="id" type="button" @click="emit('sourceShot', id)">查看支持镜头</button></article><small>候选与基础画风分开保存，尚未覆盖全局 Prompt。</small></details>
    <p v-if="section === 'review' && !reviews.length && !styles.length">此任务暂未记录待处理的事实或画风复核项。</p>
  </div>
</template>
<style scoped>
.task-review-panel{padding:14px 21px 0;font-size:11px;line-height:1.8}.task-review-panel details{border:1px solid #7194aa33;border-radius:8px;padding:9px;margin-bottom:12px}.task-review-panel p{margin:6px 0;color:#93a5b0}.task-review-panel button{border:1px solid #7194aa55;border-radius:5px;background:#7194aa18;padding:6px;color:inherit;margin:3px;cursor:pointer}.task-review-panel small{color:#a1937f}
</style>
