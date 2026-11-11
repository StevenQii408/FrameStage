<script setup lang="ts">
import { computed } from 'vue'
import type { EntityStatus } from '../types'
const props = defineProps<{ status: EntityStatus }>()
const labels: Record<EntityStatus, string> = { draft: '待完善', queued: '排队中', processing: '分析中', ready: '已完成', warning: '需复核', failed: '失败', stale: '已过期', cancelled: '已取消', paused_memory_pressure: '内存暂停', interrupted: '已中断' }
const label = computed(() => labels[props.status] || props.status)
</script>
<template><span class="status-pill" :class="`status-${status}`"><i />{{ label }}</span></template>
<style scoped>.status-draft{color:#bea475}.status-draft i{background:#bea475}</style>
