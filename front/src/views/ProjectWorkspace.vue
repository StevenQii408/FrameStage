<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRouter } from 'vue-router'
import { ArrowLeft, Clapperboard, Download, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import { useProjectCanvas } from '../composables/useProjectCanvas'

const props = defineProps<{ projectId: string }>()
const Workbench = defineAsyncComponent(() => import('../App.vue'))
const router = useRouter()
const canvas = useProjectCanvas(props.projectId)
const mountKey = ref(0)
const workbench = ref<{ prepareLeave: () => boolean }>()
const initialCanvas = computed(() => ({ ...canvas.initialState.value, backendJobIds: canvas.project.value?.job_ids || [] }))
const pageVisible = ref(document.visibilityState !== 'hidden')
const reducedMotion = ref(false)
const savedFlash = ref(false)
const animateNavigation = computed(() => pageVisible.value && !reducedMotion.value)
let motionQuery: MediaQueryList | undefined
function visibilityChanged() { pageVisible.value = document.visibilityState !== 'hidden'; if (!pageVisible.value) savedFlash.value = false }
function motionChanged(event?: MediaQueryListEvent) { reducedMotion.value = event?.matches ?? motionQuery?.matches ?? false; if (reducedMotion.value) savedFlash.value = false }
function finishSaveFlash(event: AnimationEvent) { if (event.animationName.startsWith('project-nav-saved')) savedFlash.value = false }
watch(() => canvas.status.value, (status, previous) => { savedFlash.value = status === 'saved' && previous === 'saving' && animateNavigation.value })
onMounted(() => canvas.load())
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged)
  motionQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)')
  motionChanged()
  if (motionQuery?.addEventListener) motionQuery.addEventListener('change', motionChanged)
  else motionQuery?.addListener?.(motionChanged)
})
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', visibilityChanged)
  if (motionQuery?.removeEventListener) motionQuery.removeEventListener('change', motionChanged)
  else motionQuery?.removeListener?.(motionChanged)
})
const beforeLeave = () => workbench.value?.prepareLeave() === false ? false : canvas.flush()
onBeforeRouteLeave(beforeLeave)
onBeforeRouteUpdate(beforeLeave)
async function reload() { await canvas.reloadServer(); if (!canvas.conflict.value) mountKey.value += 1 }
const labels = { loading: '正在读取', saved: '已保存', pending: '待保存', saving: '保存中', local: '仅保存在本机 · 点击重试', memory: '仅在当前窗口 · 点击重试', conflict: '存在版本冲突' }
</script>
<template>
  <section class="project-workspace">
    <Workbench v-if="canvas.ready.value" ref="workbench" :inert="canvas.unavailable.value" :key="`${projectId}-${mountKey}`" :project-id="projectId" :project-name="canvas.project.value?.name || ''" :initial-canvas="initialCanvas" @canvas-change="canvas.update" @exit-project="router.push('/projects')">
      <template #project-navigation>
        <nav class="project-nav project-canvas-navigation" :class="{ 'project-nav-animated': animateNavigation, 'project-nav-saved-flash': savedFlash }" :data-save-state="canvas.status.value" aria-label="项目导航与保存状态" @animationend="finishSaveFlash">
          <button class="project-navigation-back" type="button" aria-label="返回项目列表" title="返回项目列表" @click="router.push('/projects')"><ArrowLeft :size="16" /></button>
          <span class="project-navigation-emblem" aria-hidden="true"><Clapperboard :size="14" :stroke-width="1.6" /></span>
          <span class="project-navigation-name" :title="canvas.project.value?.name">{{ canvas.project.value?.name }}</span>
          <button class="project-save-state" :class="canvas.status.value" :disabled="canvas.status.value === 'saving'" :aria-busy="canvas.status.value === 'saving'" :aria-label="'画布保存状态：' + labels[canvas.status.value]" :title="canvas.error.value || '画布自动保存；点击立即保存'" @click="canvas.flush()"><i class="project-save-dot" aria-hidden="true" />{{ labels[canvas.status.value] }}</button>
          <button v-if="canvas.status.value === 'memory'" type="button" aria-label="下载当前草稿" title="下载当前草稿" @click="canvas.downloadLocal()"><Download :size="15" /></button>
        </nav>
      </template>
    </Workbench>
    <div v-else class="project-loading"><LoaderCircle v-if="!canvas.error.value" :size="25" /><h2>{{ canvas.error.value ? '暂时无法打开项目' : '正在打开画布…' }}</h2><p v-if="canvas.error.value" role="alert">{{ canvas.error.value }}</p><div><button @click="router.push('/projects')"><ArrowLeft :size="15" />返回项目列表</button><button v-if="canvas.error.value" @click="canvas.load()"><RefreshCw :size="15" />重试</button></div></div>
    <div v-if="canvas.conflict.value" class="project-conflict-backdrop"><section role="dialog" aria-modal="true" aria-label="画布版本冲突" class="project-conflict"><h2>画布已在其他窗口更新</h2><p>为避免覆盖其他窗口的修改，自动保存已暂停。{{ canvas.status.value === 'memory' ? '最新草稿仅在当前窗口，请先下载，再加载服务端版本。' : '本机草稿仍然保留，可以先下载备份，再重新加载服务端版本。' }}</p><p class="project-conflict-error">{{ canvas.error.value }}</p><div><button @click="canvas.downloadLocal()"><Download :size="15" />{{ canvas.status.value === 'memory' ? '下载当前草稿' : '下载本机草稿' }}</button><button @click="reload"><RefreshCw :size="15" />加载服务端版本</button></div></section></div>
    <div v-if="canvas.unavailable.value && canvas.ready.value" class="project-conflict-backdrop"><section role="dialog" aria-modal="true" aria-label="项目已删除" class="project-conflict"><h2>项目已移入回收站或不存在</h2><p>{{ canvas.status.value === 'memory' ? '最新草稿仅在当前窗口，请先下载。' : '本机草稿已保留，当前画布停止编辑。' }}可以返回项目列表，从回收站恢复后继续。</p><div><button @click="canvas.downloadLocal()"><Download :size="15" />{{ canvas.status.value === 'memory' ? '下载当前草稿' : '下载本机草稿' }}</button><button @click="router.push('/projects')"><ArrowLeft :size="15" />返回项目列表</button></div></section></div>
  </section>
</template>
<style scoped>
.project-workspace{min-height:100vh;background:#18181b}
.project-nav{--nav-line-one:#71dce99e;--nav-line-two:#c9a8769c;--nav-glow:#4ca5b61b;box-sizing:border-box;position:relative;isolation:isolate;display:flex;align-items:center;flex:0 1 auto;gap:8px;height:39px;min-width:0;max-width:min(390px,100%);padding:0 10px 0 4px;border:1px solid transparent;border-radius:12px;background:linear-gradient(115deg,#1b2935f7,#18212cfb) padding-box,linear-gradient(110deg,var(--nav-line-one),#5d6f7a33 46%,var(--nav-line-two)) border-box;box-shadow:inset 0 1px #d3e8f20b,0 0 19px var(--nav-glow),0 7px 18px #0003;color:#c2d0db;font-size:11px}
.project-nav::before{content:'';position:absolute;top:-1px;left:15px;right:15px;height:1px;border-radius:inherit;background:linear-gradient(90deg,transparent,var(--nav-line-one) 32%,var(--nav-line-two) 65%,transparent);background-size:200% 100%;pointer-events:none}
.project-nav::after{content:'';position:absolute;inset:-1px;border:1px solid #9de9ed;border-radius:inherit;opacity:0;pointer-events:none}
.project-navigation-emblem{display:grid;place-items:center;flex:0 0 23px;width:23px;height:23px;margin-left:-2px;border:1px solid #8cb7c63d;border-radius:7px;background:linear-gradient(145deg,#38566e6b,#2638492b);box-shadow:inset 0 1px #e0f7ff13;color:#e3c18e}
.project-navigation-name{min-width:0;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#dce9f1;font-weight:600;letter-spacing:.015em}
.project-nav button{display:flex;align-items:center;justify-content:center;flex-shrink:0;background:none;border:0;color:inherit;cursor:pointer;padding:6px;border-radius:6px}
.project-nav button:focus-visible{outline:2px solid #83d8ed;outline-offset:2px}.project-nav button:disabled{opacity:.55;cursor:default}
.project-navigation-back:hover{background:#8acddd0e;color:#b5edf6}
.project-nav .project-save-state{gap:6px;font-size:9px;color:#96b9b3;border-left:1px solid #8193a433;border-radius:0;padding-left:10px;white-space:nowrap}
.project-save-dot{flex:0 0 5px;width:5px;height:5px;border-radius:50%;background:#8fcfbd;box-shadow:0 0 0 3px #8fcfbd0b}
.project-nav[data-save-state='pending'] .project-save-dot{background:#c3af81;box-shadow:none}
.project-nav[data-save-state='saving'] .project-save-dot{background:#9ad7ed;box-shadow:0 0 0 3px #87cde116}
.project-nav[data-save-state='local']{--nav-line-one:#d6ad699e;--nav-line-two:#a17c425e;--nav-glow:#b67c2e13}.project-nav .project-save-state.local{color:#d4ab77}.project-save-state.local .project-save-dot{background:#d4ab77;box-shadow:0 0 0 3px #d4ab770c}
.project-nav[data-save-state='memory'],.project-nav[data-save-state='conflict']{--nav-line-one:#bb77758c;--nav-line-two:#864d524d;--nav-glow:#8c404015}.project-nav .project-save-state.memory,.project-nav .project-save-state.conflict{color:#c9908c}.project-save-state.conflict .project-save-dot{background:#bd7874;box-shadow:none}
.project-nav-animated[data-save-state='saving']::before{animation:project-nav-flow 2.6s linear infinite}.project-nav-animated[data-save-state='saving'] .project-save-dot{animation:project-nav-saving 2s ease-in-out infinite}.project-nav-animated.project-nav-saved-flash::after{animation:project-nav-saved .7s ease-out 1}
@keyframes project-nav-flow{from{background-position:100% 0}to{background-position:-100% 0}}@keyframes project-nav-saving{0%,100%{opacity:.45}50%{opacity:1}}@keyframes project-nav-saved{0%{opacity:.75;box-shadow:0 0 0 0 #8ed2d62c}100%{opacity:0;box-shadow:0 0 0 5px #8ed2d600}}
.project-loading{height:100vh;display:grid;place-content:center;justify-items:center;gap:12px;color:#a9becd;padding:30px}.project-loading h2{font-size:19px}.project-loading p{font-size:13px;max-width:550px;overflow-wrap:anywhere}.project-loading>div,.project-conflict>div{display:flex;gap:12px;flex-wrap:wrap}.project-loading button,.project-conflict button{display:flex;align-items:center;gap:7px;padding:10px 14px;border-radius:8px;border:1px solid #7993a355;background:#25313b;color:#c5d8e1;cursor:pointer}.project-conflict-backdrop{position:fixed;inset:0;z-index:1300;background:#000a;display:grid;place-items:center;padding:24px}.project-conflict{max-width:520px;padding:28px;border-radius:14px;background:#202832;border:1px solid #60798677;color:#d7e2ea}.project-conflict h2{font-size:19px;margin:0 0 18px}.project-conflict p{font-size:13px;line-height:1.9;color:#9bb0bf}.project-conflict-error{color:#d0a47a!important}
@media(max-width:850px){.project-nav{max-width:100%;flex-basis:auto}.project-navigation-name{max-width:140px}}
@media(prefers-reduced-motion:reduce){.project-nav::before,.project-nav::after,.project-save-dot{animation:none!important}}
</style>
