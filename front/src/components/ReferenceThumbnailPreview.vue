<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
const props = defineProps<{ url: string; label: string; anchor: { left: number; top: number; bottom: number } }>()
const emit = defineEmits<{ close: [] }>()
const position = computed(() => {
  const width = Math.min(300, window.innerWidth - 24)
  const below = props.anchor.bottom + 10
  const above = props.anchor.top - 294
  return { left: Math.max(12, Math.min(props.anchor.left, window.innerWidth - width - 12)) + 'px',
    top: Math.max(12, below + 284 <= window.innerHeight ? below : above) + 'px', width: width + 'px' }
})
function close() { emit('close') }
onMounted(() => { window.addEventListener('resize', close); window.addEventListener('scroll', close, true) })
onBeforeUnmount(() => { window.removeEventListener('resize', close); window.removeEventListener('scroll', close, true) })
</script>

<template>
  <Teleport to="body">
    <aside class="reference-thumbnail-preview" role="tooltip" :aria-label="label + '完整预览'" :style="position">
      <img :src="url" :alt="label" /><span>{{ label }}</span>
    </aside>
  </Teleport>
</template>

<style scoped>
.reference-thumbnail-preview { position: fixed; z-index: 160; box-sizing: border-box; padding: 8px; border: 1px solid #95b6ce38; border-radius: 12px; background: #151c25fa; box-shadow: 0 12px 36px #0008; pointer-events: none; }
.reference-thumbnail-preview img { display: block; width: 100%; height: 250px; object-fit: contain; border-radius: 7px; background: #0b1016; }
.reference-thumbnail-preview span { display: block; padding: 6px 2px 0; font: 11px/1.4 system-ui, sans-serif; color: #c8d8e5; }
</style>
