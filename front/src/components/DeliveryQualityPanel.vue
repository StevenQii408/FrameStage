<script setup lang="ts">
import type { DeliveryQuality } from '../types'
defineProps<{ quality?: DeliveryQuality; status: string }>()
const labels: Record<string, string> = { ready: '可用', partial: '部分可用', pending: '待完成', failed: '未完成', stale: '待重新编排', unavailable: '未建立', unknown: '未确定', not_requested: '未请求' }
</script>
<template>
  <section v-if="quality" class="delivery-quality" aria-label="交付质量" :class="`quality-${quality.overall}`">
    <strong>{{ status === 'completed' ? '处理已结束' : status === 'interrupted' ? '处理已中断' : '交付状态' }} · {{ labels[quality.overall] || quality.overall }}</strong>
    <div><span>视觉分析 {{ quality.visual.usable }} / {{ quality.visual.total }} 可用</span><span>事件聚合 {{ labels[quality.aggregation.state] || quality.aggregation.state }}</span><span>画风 {{ labels[quality.style.state] || quality.style.state }}</span><span>Prompt {{ quality.prompts.ready }} / {{ quality.prompts.total }} 可用</span></div>
    <p v-if="quality.visual.static_only">{{ quality.visual.static_only }} 镜静态内容已保留 · 动态待复核</p>
    <details v-if="quality.issues.length"><summary>{{ quality.issues.length }} 类待处理项</summary><p v-for="issue in quality.issues" :key="issue.code">{{ issue.message }}{{ issue.count ? `（${issue.count}）` : '' }}</p></details>
  </section>
</template>
<style scoped>
.delivery-quality{padding:12px 16px;border:1px solid #7194aa33;border-radius:8px;margin:0 0 15px;font-size:11px;line-height:1.8}.delivery-quality strong{color:#79b6a6}.delivery-quality>div{display:flex;flex-wrap:wrap;gap:3px 20px;color:#8a9daa}.quality-partial strong{color:#c4a071}.delivery-quality details{margin-top:6px}.delivery-quality p{margin:5px 0}
</style>
