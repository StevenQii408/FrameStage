<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Camera, LoaderCircle, Pause, Play, Volume2, VolumeX } from 'lucide-vue-next'
import { captureVideoFrame, formatMediaTime, normalizeVideoCuts, type CapturedVideoFrame, type FrameCaptureSource } from '../mediaPlayback'

const props = withDefaults(defineProps<{ src: string; cuts?: number[]; captureEnabled?: boolean; autoplay?: boolean; muted?: boolean; compact?: boolean; title?: string }>(), { cuts: () => [], captureEnabled: false, autoplay: false, muted: false, compact: false, title: '视频预览' })
const emit = defineEmits<{ capture: [frame: CapturedVideoFrame]; error: [message: string]; ready: [metadata: { duration: number; width: number; height: number }] }>()
const video = ref<HTMLVideoElement>()
const root = ref<HTMLElement>()
const captureButton = ref<HTMLButtonElement>()
const duration = ref(0), position = ref(0), playing = ref(false), isMuted = ref(props.muted), volume = ref(1)
const waiting = ref(false), menuOpen = ref(false), capturing = ref(false), error = ref('')
let captureController: AbortController | undefined
const cuts = computed(() => normalizeVideoCuts(props.cuts, duration.value))
const progress = computed(() => duration.value > 0 ? Math.max(0, Math.min(100, position.value / duration.value * 100)) : 0)

function sync() {
  const media = video.value
  if (!media) return
  duration.value = Number.isFinite(media.duration) ? media.duration : 0
  position.value = Number.isFinite(media.currentTime) ? media.currentTime : 0
  playing.value = !media.paused && !media.ended
  isMuted.value = media.muted
  volume.value = media.volume
}
function ready() { sync(); error.value = ''; if (video.value) emit('ready', { duration: duration.value, width: video.value.videoWidth, height: video.value.videoHeight }) }
function pause() { video.value?.pause(); playing.value = false }
async function play() {
  if (!video.value || capturing.value) return
  try { await video.value.play(); error.value = ''; sync() } catch { error.value = '浏览器暂未允许播放，请点击播放按钮重试。' }
}
function toggle() { if (playing.value) pause(); else void play() }
function seek(value: number) {
  if (!video.value || !duration.value || capturing.value || !Number.isFinite(value)) return
  video.value.currentTime = Math.max(0, Math.min(value, duration.value))
  position.value = video.value.currentTime
}
function setVolume(event: Event) { if (video.value) { video.value.volume = Number((event.target as HTMLInputElement).value); video.value.muted = false; sync() } }
function toggleMute() { if (video.value) { video.value.muted = !video.value.muted; sync() } }
function fail() { waiting.value = false; error.value = '视频暂时无法播放，请检查素材是否仍然可用。'; emit('error', error.value) }
function abortCapture() { captureController?.abort(); captureController = undefined; capturing.value = false; menuOpen.value = false }
async function capture(source: FrameCaptureSource) {
  if (!video.value || !props.captureEnabled || capturing.value) return
  menuOpen.value = false; capturing.value = true; error.value = ''
  captureController = new AbortController()
  const controller = captureController
  try { const frame = await captureVideoFrame(video.value, source, controller.signal); if (!controller.signal.aborted) emit('capture', frame) }
  catch (issue) { if (!controller.signal.aborted) { error.value = issue instanceof Error ? issue.message : '截帧失败，请重试。'; emit('error', error.value) } }
  finally { if (captureController === controller) { capturing.value = false; captureController = undefined; sync() } }
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && menuOpen.value) { event.preventDefault(); event.stopPropagation(); menuOpen.value = false; captureButton.value?.focus(); return }
  // Let the node own Escape-to-collapse and the canvas own Command-Z.
  if (event.key === 'Escape' || event.metaKey || event.ctrlKey || event.altKey) return
  event.stopPropagation()
  if (event.target !== root.value && event.target !== video.value) return
  if (event.code === 'Space' || event.key.toLowerCase() === 'k') { event.preventDefault(); toggle() }
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); seek(position.value + (event.key === 'ArrowRight' ? 5 : -5)) }
  else if (event.key === 'Home') { event.preventDefault(); seek(0) }
  else if (event.key === 'End') { event.preventDefault(); seek(duration.value) }
  else if (event.key.toLowerCase() === 'm') { event.preventDefault(); toggleMute() }
}
function outside(event: PointerEvent) { if (menuOpen.value && !root.value?.contains(event.target as Node)) menuOpen.value = false }
function visibility() { if (document.hidden) { pause(); abortCapture() } }
watch(() => props.src, () => { abortCapture(); pause(); duration.value = 0; position.value = 0; error.value = '' })
watch(() => props.muted, value => { if (video.value) video.value.muted = value; isMuted.value = value })
watch(() => props.autoplay, value => { if (value) void play(); else pause() })
onMounted(() => { document.addEventListener('visibilitychange', visibility); document.addEventListener('pointerdown', outside); if (props.autoplay) { root.value?.focus({ preventScroll: true }); void play() } })
onBeforeUnmount(() => { abortCapture(); pause(); document.removeEventListener('visibilitychange', visibility); document.removeEventListener('pointerdown', outside) })
defineExpose({ pause, video })
</script>

<template>
  <div ref="root" class="canvas-video-player" :class="{ 'player-compact': compact }" tabindex="0" :aria-label="title" :aria-busy="capturing" @keydown="keydown">
    <video ref="video" crossorigin="anonymous" :src="src" :muted="muted" :autoplay="autoplay" playsinline preload="metadata" :aria-label="title" draggable="false"
      @loadedmetadata="ready" @durationchange="sync" @timeupdate="sync" @play="sync" @playing="waiting = false; sync()" @pause="sync" @ended="sync" @volumechange="sync" @waiting="waiting = true" @canplay="waiting = false" @error="fail" />
    <div v-if="waiting && playing" class="player-waiting" aria-label="正在缓冲"><LoaderCircle :size="25" /></div>
    <div class="player-controls nodrag nopan nowheel" @pointerdown.stop @pointermove.stop @click.stop @dblclick.stop @wheel.stop>
      <button type="button" class="player-play" :aria-label="playing ? '暂停视频' : '播放视频'" :disabled="capturing" @click="toggle"><Pause v-if="playing" :size="21" fill="currentColor" /><Play v-else :size="21" fill="currentColor" /></button>
      <span class="player-current-time">{{ formatMediaTime(position) }}</span>
      <div class="player-scrubber" :style="{ '--player-progress': progress + '%' }">
        <input type="range" class="player-seek" aria-label="视频播放进度" :aria-valuetext="`${formatMediaTime(position)} / ${formatMediaTime(duration)}`" min="0" :max="duration || 1" step="0.001" :value="position" :disabled="!duration || capturing" @input="seek(Number(($event.target as HTMLInputElement).value))" />
        <button v-for="cut in cuts" :key="cut" type="button" class="player-cut" :style="{ left: `${cut / duration * 100}%` }" :title="`PySceneDetect 切镜点 · ${cut.toFixed(2)} 秒`" :aria-label="`跳转切镜点 ${formatMediaTime(cut, true)}`" :disabled="capturing" @click="seek(cut)"><i /></button>
      </div>
      <span class="player-total-time">{{ formatMediaTime(duration) }}</span>
      <div class="player-volume"><button type="button" :aria-label="isMuted || !volume ? '取消静音' : '静音'" @click="toggleMute"><VolumeX v-if="isMuted || !volume" :size="21" /><Volume2 v-else :size="21" /></button><input aria-label="音量" type="range" min="0" max="1" step="0.05" :value="isMuted ? 0 : volume" @input="setVolume" /></div>
      <div v-if="captureEnabled" class="player-capture">
        <button ref="captureButton" type="button" aria-label="截取视频画面" aria-haspopup="menu" :aria-expanded="menuOpen" :disabled="capturing || !duration" @click="menuOpen = !menuOpen"><LoaderCircle v-if="capturing" :size="21" /><Camera v-else :size="21" /></button>
        <div v-if="menuOpen" class="player-capture-menu" role="menu" aria-label="截帧选项">
          <button role="menuitem" type="button" @click="capture('first')">截取首帧</button><button role="menuitem" type="button" @click="capture('current')">截取当前帧</button><button role="menuitem" type="button" @click="capture('last')">截取尾帧</button>
        </div>
      </div>
    </div>
    <p v-if="error" class="player-error" role="status">{{ error }}</p>
  </div>
</template>

<style scoped>
.canvas-video-player{position:relative;width:100%;height:100%;min-height:0;overflow:hidden;border-radius:inherit;background:#080c11;color:#fff;isolation:isolate;outline:none}.canvas-video-player:focus-visible{outline:2px solid #9ccddc;outline-offset:3px}.canvas-video-player>video{display:block;position:absolute;inset:0;width:100%;height:100%;max-height:none;object-fit:contain}.player-controls{position:absolute;z-index:2;inset:auto 0 0;display:flex;align-items:center;gap:12px;min-height:68px;padding:22px 18px 12px;box-sizing:border-box;background:linear-gradient(transparent,#060a12c7);font:13px ui-monospace,SFMono-Regular,monospace;touch-action:none}.player-controls button{display:inline-flex;align-items:center;justify-content:center;flex:none;min-width:30px;min-height:32px;padding:3px;border:0;border-radius:7px;background:transparent;color:#f5f8fb;cursor:pointer}.player-controls button:hover{background:#e6f5ff20}.player-controls button:focus-visible,.player-controls input:focus-visible{outline:2px solid #a4d9e8;outline-offset:3px}.player-controls button:disabled,.player-controls input:disabled{cursor:default;opacity:.45}.player-current-time,.player-total-time{flex:none;font-variant-numeric:tabular-nums;white-space:nowrap;user-select:none}.player-scrubber{position:relative;flex:1;min-width:32px;height:30px;display:flex;align-items:center}.player-seek{display:block;width:100%;height:24px;margin:0;appearance:none;-webkit-appearance:none;background:transparent;cursor:pointer;touch-action:none}.player-seek::-webkit-slider-runnable-track{height:4px;border-radius:5px;background:linear-gradient(to right,#f6faff var(--player-progress),#f0f5ff45 var(--player-progress))}.player-seek::-moz-range-track{height:4px;border-radius:5px;background:#f0f5ff45}.player-seek::-moz-range-progress{height:4px;background:#f6faff}.player-seek::-webkit-slider-thumb{appearance:none;-webkit-appearance:none;width:12px;height:12px;border-radius:50%;margin-top:-4px;background:#fff;box-shadow:0 0 0 1px #15233233}.player-seek::-moz-range-thumb{width:12px;height:12px;border:0;border-radius:50%;background:#fff}.player-cut{position:absolute!important;top:50%;transform:translate(-50%,-50%);min-width:16px!important;min-height:24px!important;padding:0!important}.player-cut i{width:5px;height:5px;border-radius:50%;border:1px solid #071520;background:#6fd8ed;box-shadow:0 0 0 2px #0a132470;pointer-events:none}.player-cut:hover i{width:7px;height:7px;background:#fff}.player-volume{display:flex;align-items:center;gap:4px}.player-volume>input{width:0;opacity:0;transition:width .15s,opacity .15s;accent-color:#eaf8ff}.player-volume:hover>input,.player-volume:focus-within>input{width:55px;opacity:1}.player-capture{position:relative;flex:none}.player-capture-menu{position:absolute;bottom:calc(100% + 10px);right:0;min-width:140px;padding:5px;border:1px solid #7790a755;border-radius:10px;background:#151e2bf5;box-shadow:0 12px 30px #0007;backdrop-filter:blur(12px)}.player-capture-menu>button{width:100%;justify-content:flex-start;padding:7px 12px;font:12px system-ui,sans-serif;white-space:nowrap}.player-waiting{position:absolute;inset:0;display:grid;place-items:center;pointer-events:none}.player-waiting svg,.player-capture>button>svg.lucide-loader-circle{animation:player-spin 1s linear infinite}.player-error{position:absolute;z-index:3;left:12px;right:12px;top:10px;margin:0;padding:8px 10px;border:1px solid #d09e6c55;border-radius:7px;background:#181b24ed;color:#f0d0a7;font:12px/1.5 system-ui,sans-serif;pointer-events:none}.player-compact .player-controls{gap:5px;min-height:38px;padding:12px 8px 4px;font-size:10px}.player-compact .player-controls button{min-width:23px;min-height:25px}.player-compact .player-controls svg{width:15px;height:15px}.player-compact .player-current-time,.player-compact .player-total-time,.player-compact .player-volume>input{display:none}.player-compact .player-seek::-webkit-slider-thumb{width:8px;height:8px;margin-top:-2px}.player-compact .player-seek::-moz-range-thumb{width:8px;height:8px}.player-compact .player-error{font-size:10px;padding:5px}.player-compact .player-cut i{width:4px;height:4px}@keyframes player-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.player-volume>input{transition:none}.player-waiting svg,.player-capture>button>svg{animation:none}}
</style>
