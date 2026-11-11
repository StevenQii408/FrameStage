<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { Check, Pin, Play, X } from 'lucide-vue-next'
import { generationAssetUrl, generationVideoPosterUrl } from '../api/generations'
import type { GeneratorKind, GeneratorVersion } from '../generatorGraph'

const props = defineProps<{ kind: GeneratorKind; versions: GeneratorVersion[]; activeVersionId?: string; anchor: { x: number; y: number } }>()
const emit = defineEmits<{ close: []; selectVersion: [versionId: string] }>()
const closeButton = ref<HTMLButtonElement>()
const visibleCount = ref(12)
const playingVersion = ref('')
const failedPosters = ref(new Set<string>())
const playbackError = ref('')
const visibleVersions = computed(() => props.versions.slice(0, visibleCount.value).map(version => ({
  ...version,
  mediaUrl: generationAssetUrl(version.asset.url),
  posterUrl: props.kind === 'video' ? generationVideoPosterUrl(version.asset.url) : '',
})))
function playVersion(id: string) {
  playbackError.value = ''
  playingVersion.value = id
}
function onPlaybackError(id: string) {
  if (playingVersion.value !== id) return
  playingVersion.value = ''
  playbackError.value = id
}
const position = computed(() => ({
  left: Math.max(12, Math.min(props.anchor.x, window.innerWidth - 436)) + 'px',
  top: Math.max(12, Math.min(props.anchor.y, window.innerHeight - Math.min(480, window.innerHeight - 24) - 12)) + 'px',
}))
function timestamp(value: string) {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '时间未记录'
}
function specification(version: GeneratorVersion) {
  return [version.settings.model_id, version.settings.aspect_ratio, version.settings.resolution,
    props.kind === 'video' && version.settings.duration_seconds != null ? version.settings.duration_seconds + 's' : null].filter(Boolean).join(' · ')
}
onMounted(() => { void nextTick(() => closeButton.value?.focus()) })
</script>

<template>
  <Teleport to="body">
    <section class="generator-history nodrag nowheel" data-generator-history data-canvas-gesture-lock role="dialog" aria-modal="false" aria-label="生成历史版本" :style="position" @pointerdown.stop @click.stop @dblclick.stop @wheel.stop @keydown.esc.stop.prevent="emit('close')">
      <header><div><strong>历史版本</strong><span>{{ versions.length }} 个版本 · 置顶版本用于输出连线</span></div><button ref="closeButton" type="button" aria-label="关闭生成历史" @click="emit('close')"><X :size="16" /></button></header>
      <ol>
        <li v-for="(version, index) in visibleVersions" :key="version.id" :class="{ 'is-active': version.id === activeVersionId }" :data-version-id="version.id">
          <div class="history-media">
            <img v-if="kind === 'image' && version.mediaUrl && version.asset.mime_type.startsWith('image/')" :src="version.mediaUrl" :alt="'生成图片版本 ' + (versions.length - index)" loading="lazy" decoding="async" />
            <template v-else-if="kind === 'video' && version.mediaUrl && version.asset.mime_type.startsWith('video/')">
              <video v-if="playingVersion === version.id" :src="version.mediaUrl" :poster="failedPosters.has(version.id) ? undefined : version.posterUrl || undefined"
                controls autoplay playsinline muted :aria-label="'生成视频版本 ' + (versions.length - index)" @error="onPlaybackError(version.id)" />
              <button v-else type="button" class="history-play" :aria-label="'播放视频版本 ' + (versions.length - index)" @click="playVersion(version.id)">
                <img v-if="version.posterUrl && !failedPosters.has(version.id)" :src="version.posterUrl" alt="" loading="lazy" decoding="async" @error="failedPosters.add(version.id)" />
                <span class="history-play-icon"><Play :size="14" fill="currentColor" aria-hidden="true" /></span>
                <span v-if="playbackError === version.id" class="history-media-message">播放失败，点击重试</span>
                <span v-else-if="failedPosters.has(version.id)" class="history-media-message">播放原视频</span>
              </button>
            </template>
            <span v-else>产物暂不可预览</span>
          </div>
          <div class="history-description"><div class="history-version-heading"><b>版本 {{ versions.length - index }}</b><span v-if="index === 0" class="history-latest">最新</span><button type="button" :disabled="version.id === activeVersionId" :aria-label="version.id === activeVersionId ? '当前置顶版本 ' + (versions.length - index) : '置顶版本 ' + (versions.length - index)" @click="emit('selectVersion', version.id)"><Check v-if="version.id === activeVersionId" :size="12" /><Pin v-else :size="12" />{{ version.id === activeVersionId ? '已置顶' : '置顶' }}</button></div><time :datetime="version.createdAt">{{ timestamp(version.createdAt) }}</time><p class="history-specification">{{ specification(version) }}</p></div>
          <details><summary>查看原始 Prompt</summary><p class="history-prompt">{{ version.prompt || '未记录 Prompt' }}</p></details>
        </li>
      </ol>
      <button v-if="visibleCount < versions.length" type="button" class="history-load-more" @click="visibleCount += 12">显示更多历史</button>
    </section>
  </Teleport>
</template>

<style scoped>
.generator-history { position: fixed; z-index: 105; box-sizing: border-box; width: min(424px,calc(100vw - 24px)); max-height: min(480px,calc(100vh - 24px)); overflow: auto; border: 1px solid #9bbed330; border-radius: 16px; padding: 13px; color: #e0e6ee; background: #1b2028fa; box-shadow: 0 18px 48px #0007; font-family: inherit; font-size: 12px; line-height: 1.5; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding-bottom: 11px; }header strong { display: block; font-size: 13px; }header span { color: #94a4b5; font-size: 10px; }button { display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 5px 7px; border: 0; border-radius: 6px; color: #c8dbe7; background: #7095b01c; font: inherit; font-size: 10px; cursor: pointer; }button:disabled { cursor: default; color: #a2dac9; background: #72c5a514; }button:focus-visible,summary:focus-visible { outline: 2px solid #94c5dd; outline-offset: 2px; }header > button { width: 27px; height: 27px; padding: 0; background: transparent; }
ol { display: grid; gap: 9px; padding: 0; margin: 0; list-style: none; }li { display: grid; grid-template-columns: 116px minmax(0,1fr); gap: 8px 11px; padding: 9px; border: 1px solid #ffffff0f; border-radius: 10px; background: #11172066; }li.is-active { border-color: #8dbfce55; }.history-media { display: grid; place-items: center; height: 82px; overflow: hidden; border-radius: 7px; background: #080d14; }.history-media img,.history-media video { width: 100%; height: 100%; max-height: 82px; object-fit: contain; }.history-media > span { color: #9aa6b6; font-size: 10px; }.history-description { min-width: 0; }.history-version-heading { display: flex; align-items: center; gap: 5px; }.history-version-heading b { font-size: 11px; }.history-version-heading button { margin-left: auto; white-space: nowrap; }.history-latest { color: #98abbf; font-size: 9px; }time { display: block; margin-top: 8px; color: #8d9aab; font-size: 9px; }.history-specification { overflow-wrap: anywhere; margin: 6px 0 0; color: #b0bdcb; font-size: 10px; }details { grid-column: 1 / -1; min-width: 0; }summary { width: fit-content; color: #a8c3d6; font-size: 10px; cursor: pointer; }.history-prompt { max-height: 200px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 0; padding: 8px; border-radius: 6px; color: #bdcad7; background: #0b111977; font-size: 11px; line-height: 1.7; }
@media (max-width: 440px) { li { grid-template-columns: 94px minmax(0,1fr); gap: 7px; }.history-latest { display: none; } }
.history-play { position: relative; width: 100%; height: 100%; padding: 0; overflow: hidden; background: transparent; }
.history-play:focus-visible { outline-offset: -2px; }
.history-play-icon { position: absolute; display: grid; place-items: center; width: 27px; height: 27px; border-radius: 50%; background: #111820ac; color: white; }
.history-media-message { position: absolute; bottom: 3px; padding: 0 3px; border-radius: 3px; color: #dbe2eb; background: #111820cc; font-size: 9px; }
.history-load-more { display: flex; width: 100%; margin-top: 11px; padding: 8px; color: #bdd4e5; }
</style>
