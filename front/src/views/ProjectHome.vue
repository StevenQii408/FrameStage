<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, Film, FolderOpen, LoaderCircle, MoreHorizontal, Plus, RotateCcw, Search, Sparkles, Trash2, X } from 'lucide-vue-next'
import { createProject, deleteProject, listProjects, renameProject, restoreProject, type ProjectSummary } from '../api/projects'
import { migrateLegacyCanvas } from '../projectPersistence'
import { generationAssetUrl } from '../api/generations'

const router = useRouter()
const projects = ref<ProjectSummary[]>([])
const query = ref('')
const trash = ref(false)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const dialog = ref<{ mode: 'create' | 'rename' | 'delete'; project?: ProjectSummary } | null>(null)
const name = ref('')
const menuId = ref('')
const more = ref(false)
const moreLoading = ref(false)
let requestSequence = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
function openDialog(mode: 'create' | 'rename' | 'delete', project?: ProjectSummary) {
  name.value = mode === 'rename' ? project?.name || '' : ''
  dialog.value = { mode, project }; menuId.value = ''; error.value = ''
}
async function refresh(append = false) {
  const sequence = ++requestSequence
  if (append) moreLoading.value = true
  else loading.value = true
  error.value = ''
  try {
    const values = await listProjects(trash.value, append ? projects.value.length : 0, query.value.trim())
    if (disposed || sequence !== requestSequence) return
    projects.value = append ? [...projects.value, ...values.filter(value => !projects.value.some(item => item.id === value.id))] : values
    more.value = values.length === 24
  } catch (failure) { if (!disposed && sequence === requestSequence) error.value = failure instanceof Error ? failure.message : '项目读取失败，请检查后端服务' }
  finally { if (!disposed && sequence === requestSequence) { loading.value = false; moreLoading.value = false } }
}
async function toggleTrash() { trash.value = !trash.value; menuId.value = ''; await refresh() }
async function submit() {
  if (!dialog.value || busy.value) return
  if (dialog.value.mode !== 'delete' && !name.value.trim()) return
  busy.value = true; error.value = ''
  try {
    const { mode, project } = dialog.value
    if (mode === 'create') {
      const created = await createProject(name.value.trim())
      dialog.value = null
      await router.push(`/projects/${created.id}`)
    } else if (mode === 'rename' && project) {
      await renameProject(project.id, name.value.trim()); dialog.value = null; await refresh()
    } else if (project) {
      await deleteProject(project.id); dialog.value = null; notice.value = '项目已移入回收站'; await refresh()
    }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '操作失败' }
  finally { busy.value = false }
}
async function restore(project: ProjectSummary) {
  if (busy.value) return
  busy.value = true
  try { await restoreProject(project.id); notice.value = '项目已恢复'; await refresh() }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '恢复失败' }
  finally { busy.value = false }
}
function updated(value: string) {
  const date = new Date(value)
  return Number.isFinite(date.valueOf()) ? date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : ''
}
function cover(url?: string | null) {
  if (!url) return ''
  try {
    const candidate = new URL(url, window.location.origin)
    if (candidate.pathname.startsWith('/api/generation-assets/')) return generationAssetUrl(url)
    // Backend frame URLs are returned by the project summary, not user HTML.
    return candidate.protocol === 'http:' || candidate.protocol === 'https:' ? candidate.href : ''
  } catch { return '' }
}
onMounted(async () => {
  try {
    const imported = await migrateLegacyCanvas()
    if (imported) notice.value = '旧画布已迁移到“旧工作台”，原始数据和本地图片均已保留'
  } catch (failure) { notice.value = `旧画布暂未迁移，原数据已保留：${failure instanceof Error ? failure.message : '请稍后重试'}` }
  await refresh()
})
watch(query, () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { void refresh() }, 250) })
onBeforeUnmount(() => { disposed = true; requestSequence += 1; clearTimeout(searchTimer) })
</script>

<template>
  <main class="project-home" @click="menuId = ''">
    <header class="projects-header"><div class="projects-brand"><Sparkles :size="22" /><b>FRAMEFORGE <span>STUDIO</span></b></div><span class="projects-kicker">CREATIVE WORKSPACE</span></header>
    <section class="projects-content">
      <div class="projects-title"><div><span class="projects-kicker">{{ trash ? 'RECENTLY DELETED' : 'YOUR FILM STARTS HERE' }}</span><h1>{{ trash ? '回收站' : '我的项目' }}</h1><p>{{ trash ? '恢复项目后可以继续编辑，已有素材会保留。' : '每个项目拥有独立画布，将创意、分镜与生成素材放在一起。' }}</p></div><button v-if="!trash" class="project-primary" @click.stop="openDialog('create')"><Plus :size="17" />新建项目</button></div>
      <div class="projects-tools"><label><Search :size="16" /><input v-model="query" aria-label="搜索项目" placeholder="搜索项目名称" /></label><button @click="toggleTrash"><RotateCcw v-if="trash" :size="15" /><Trash2 v-else :size="15" />{{ trash ? '返回项目' : '回收站' }}</button></div>
      <p v-if="notice" class="projects-notice" role="status">{{ notice }}</p>
      <p v-if="error" class="projects-error" role="alert">{{ error }} <button @click="refresh()">重试</button></p>
      <div v-if="loading" class="projects-empty"><LoaderCircle :size="24" /><span>正在读取项目…</span></div>
      <div v-else-if="!projects.length" class="projects-empty"><FolderOpen :size="40" /><h2>{{ query ? '没有找到项目' : trash ? '回收站为空' : '创建你的第一个项目' }}</h2><p>{{ query ? '试试其他关键词' : trash ? '删除的项目将在这里显示' : '从视频、剧本或一个画面开始' }}</p><button v-if="!trash && !query" class="project-primary" @click="openDialog('create')"><Plus :size="16" />创建项目</button></div>
      <div v-else class="projects-grid">
        <article v-for="project in projects" :key="project.id" class="project-tile">
          <button class="project-cover" :aria-label="`进入项目 ${project.name}`" :disabled="trash" @click="router.push(`/projects/${project.id}`)"><img v-if="cover(project.cover_url)" :src="cover(project.cover_url)" alt="" loading="lazy" decoding="async" /><div v-else class="project-cover-placeholder"><Film :size="38" :stroke-width="1" /><span>FRAMEFORGE / PROJECT</span></div><span v-if="project.active_count" class="project-running">{{ project.active_count }} 个任务进行中</span><span v-if="!trash" class="project-enter"><ArrowRight :size="19" /></span></button>
          <div class="project-tile-info"><div><h2>{{ project.name }}</h2><p>{{ updated(project.updated_at) }} · {{ project.analysis_count || 0 }} 个分析任务 · {{ project.generation_count || 0 }} 个生成任务</p></div><button v-if="trash" class="project-menu-button" :disabled="busy" :aria-label="`恢复 ${project.name}`" @click="restore(project)"><RotateCcw :size="17" /></button><div v-else class="project-menu-wrap"><button class="project-menu-button" :aria-label="`项目 ${project.name} 的操作`" :aria-expanded="menuId === project.id" @click.stop="menuId = menuId === project.id ? '' : project.id"><MoreHorizontal :size="19" /></button><div v-if="menuId === project.id" class="project-menu"><button @click.stop="openDialog('rename', project)">重命名</button><button @click.stop="openDialog('delete', project)"><Trash2 :size="13" />移入回收站</button></div></div></div>
        </article>
      </div>
      <button v-if="more && !loading" class="projects-load-more" :disabled="moreLoading" @click="refresh(true)">{{ moreLoading ? '正在加载…' : '加载更多项目' }}</button>
    </section>
    <Teleport to="body"><div v-if="dialog" class="project-dialog-backdrop" @click.self="!busy && (dialog = null)" @keydown.esc="!busy && (dialog = null)"><form class="project-dialog" role="dialog" aria-modal="true" :aria-label="dialog.mode === 'create' ? '创建项目' : dialog.mode === 'rename' ? '重命名项目' : '删除项目'" @submit.prevent="submit"><button class="project-dialog-close" type="button" aria-label="关闭" :disabled="busy" @click="dialog = null"><X :size="18" /></button><h2>{{ dialog.mode === 'create' ? '创建项目' : dialog.mode === 'rename' ? '重命名项目' : '将项目移入回收站？' }}</h2><p v-if="dialog.mode === 'delete'">“{{ dialog.project?.name }}”中的活动任务会请求取消，项目与素材仍可恢复。远端已发生的调用费用以服务商结果为准。</p><label v-else>项目名称<input v-model="name" autofocus maxlength="80" placeholder="例如：夏日短片 / 第一版" /></label><p v-if="error" class="projects-error" role="alert">{{ error }}</p><div class="project-dialog-actions"><button type="button" :disabled="busy" @click="dialog = null">取消</button><button class="project-primary" type="submit" :disabled="busy || (dialog.mode !== 'delete' && !name.trim())">{{ busy ? '正在保存…' : dialog.mode === 'delete' ? '移入回收站' : '确定' }}</button></div></form></div></Teleport>
  </main>
</template>

<style scoped>
.projects-load-more{display:block;margin:28px auto 0;padding:11px 22px;border:1px solid #6f899c55;border-radius:10px;background:#212c36;color:#b5c8d5;cursor:pointer}
.project-home{min-height:100vh;background:#121519;color:#e4e9ed}.projects-header{height:84px;display:flex;align-items:center;justify-content:space-between;padding:0 5vw;border-bottom:1px solid #ffffff0d}.projects-brand{display:flex;gap:12px;align-items:center;color:#acd3dc;letter-spacing:1.5px;font-size:14px}.projects-brand span{font-size:10px;color:#71818e;margin-left:8px}.projects-kicker{font:10px ui-monospace,monospace;color:#7a8d9c;letter-spacing:2px}.projects-content{max-width:1500px;margin:auto;padding:64px 5vw}.projects-title{display:flex;align-items:center;justify-content:space-between;gap:24px}.projects-title h1{font-size:34px;letter-spacing:-1px;margin:12px 0}.projects-title p{font-size:13px;color:#8997a2;line-height:1.8}.project-primary{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:#b4dce2!important;color:#17232d!important;border:1px solid #c8edf1!important;border-radius:10px;padding:11px 16px;font-weight:600;cursor:pointer}.projects-tools{display:flex;align-items:center;justify-content:space-between;margin:38px 0 24px;gap:16px}.projects-tools label{display:flex;align-items:center;gap:10px;border:1px solid #ffffff14;background:#1b2027;border-radius:10px;padding:10px 13px;max-width:420px;flex:1;color:#8397a6}.projects-tools input{background:transparent;border:0;outline:0;color:inherit;width:100%;font-size:12px}.projects-tools button,.project-dialog-actions>button{display:flex;align-items:center;gap:7px;background:#20262e;border:1px solid #ffffff12;border-radius:9px;color:#a1adb7;padding:10px 13px;cursor:pointer}.projects-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:24px}.project-tile{background:#1a1f26;border:1px solid #ffffff0e;border-radius:15px;min-width:0;content-visibility:auto;contain-intrinsic-size:auto 280px}.project-cover{position:relative;display:block;width:100%;aspect-ratio:16/9;background:#202832;border:0;border-radius:14px 14px 0 0;overflow:hidden;cursor:pointer;padding:0;color:#9ab7c6}.project-cover img{width:100%;height:100%;object-fit:cover}.project-cover-placeholder{display:grid;place-content:center;justify-items:center;height:100%;gap:15px;background:radial-gradient(ellipse at 20% 25%,#39616a3d,transparent 70%),linear-gradient(135deg,#222b36,#171d25)}.project-cover-placeholder span{font:8px ui-monospace,monospace;letter-spacing:2px;color:#627985}.project-enter{position:absolute;bottom:13px;right:13px;width:33px;height:33px;background:#111b29ad;border:1px solid #ffffff18;border-radius:50%;display:grid;place-items:center;opacity:0;transition:opacity .15s}.project-cover:hover .project-enter,.project-cover:focus-visible .project-enter{opacity:1}.project-running{position:absolute;top:12px;left:12px;font-size:10px;border-radius:20px;padding:5px 9px;background:#102d2de6;color:#a7d8d0}.project-tile-info{display:flex;align-items:center;justify-content:space-between;padding:17px;gap:8px}.project-tile-info>div:first-child{min-width:0}.project-tile h2{font-size:15px;margin:0 0 9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.project-tile p{font-size:10px;color:#7f909f;margin:0;line-height:1.5}.project-menu-wrap{position:relative}.project-menu-button{display:grid;place-items:center;width:30px;height:30px;color:#9dafba;background:transparent;border:0;cursor:pointer}.project-menu{position:absolute;z-index:5;right:0;top:32px;background:#29313b;border:1px solid #ffffff18;border-radius:9px;padding:5px;min-width:130px;box-shadow:0 14px 36px #0006}.project-menu button{display:flex;width:100%;align-items:center;gap:7px;text-align:left;background:none;border:0;border-radius:5px;padding:9px;color:#cad5dd;cursor:pointer;font-size:12px}.project-menu button:hover{background:#ffffff0c}.projects-empty{display:grid;justify-items:center;place-content:center;gap:12px;min-height:360px;color:#7f96a5}.projects-empty h2{font-size:20px;color:#cad6df;margin:5px 0}.projects-empty p{font-size:13px;margin:0 0 12px}.projects-notice{padding:12px 15px;border-radius:9px;background:#1d333b;color:#a0c8d1;font-size:12px;line-height:1.7}.projects-error{font-size:12px;color:#e1ab91;line-height:1.7}.projects-error button{color:inherit;cursor:pointer;background:none;border:0;text-decoration:underline}.project-dialog-backdrop{position:fixed;inset:0;background:#0009;z-index:1200;display:grid;place-items:center;padding:24px}.project-dialog{position:relative;width:100%;max-width:430px;background:#20262e;border:1px solid #ffffff20;border-radius:16px;padding:28px;box-shadow:0 22px 90px #0009;color:#dae4ea}.project-dialog h2{font-size:20px;margin:0 0 25px}.project-dialog p{font-size:13px;line-height:1.9;color:#a6b5c2}.project-dialog label{display:grid;gap:10px;color:#a7b5be;font-size:12px}.project-dialog input{width:100%;box-sizing:border-box;background:#161b22;color:#e2edf3;border:1px solid #61748366;padding:12px;border-radius:8px}.project-dialog-close{position:absolute;right:14px;top:12px;border:0;background:none;color:#9caebc;cursor:pointer}.project-dialog-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:28px}.project-home button:disabled,.project-dialog button:disabled{opacity:.5;cursor:default}@media(max-width:650px){.projects-header{padding:0 22px}.projects-header>.projects-kicker{display:none}.projects-content{padding:34px 22px}.projects-title{align-items:flex-start;flex-direction:column}.projects-title h1{font-size:28px}.projects-tools{margin-top:28px}.projects-grid{grid-template-columns:1fr}}
</style>
