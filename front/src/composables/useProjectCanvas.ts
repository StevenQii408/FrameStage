import { onBeforeUnmount, ref } from 'vue'
import { ApiError } from '../api/client'
import { getProject, getProjectCanvas, saveProjectCanvas, type ProjectSummary } from '../api/projects'
import { readProjectCache, writeProjectCache } from '../projectPersistence'
import { canvasEqualAfterDraftRecovery } from '../canvasRecovery'

/** Server revisions prevent two open workspaces from overwriting one another. */
export function useProjectCanvas(projectId: string) {
  const project = ref<ProjectSummary | null>(null)
  const initialState = ref<Record<string, unknown>>({})
  const ready = ref(false)
  const error = ref('')
  const conflict = ref(false)
  const unavailable = ref(false)
  const status = ref<'loading' | 'saved' | 'pending' | 'saving' | 'local' | 'memory' | 'conflict'>('loading')
  let revision = 0
  let dirty = false
  let latest: Record<string, unknown> = {}
  let change = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let saving: Promise<boolean> | undefined
  let disposed = false
  let downloadedChange = -1
  function cache() {
    if (writeProjectCache(projectId, { revision, state: latest, dirty, savedAt: new Date().toISOString() })) return true
    const previous = readProjectCache(projectId)
    return previous?.revision === revision && previous.dirty === dirty && JSON.stringify(previous.state) === JSON.stringify(latest)
  }
  async function load(serverOnly = false) {
    error.value = ''; conflict.value = false; unavailable.value = false; ready.value = false; status.value = 'loading'
    try {
      const [summary, canvas] = await Promise.all([getProject(projectId), getProjectCanvas(projectId)])
      if (disposed) return
      project.value = summary
      const local = readProjectCache(projectId)
      const repairedMatch = local?.dirty && local.revision !== canvas.revision && canvasEqualAfterDraftRecovery(local.state, canvas.state)
      const useLocal = !serverOnly && local?.dirty && !repairedMatch
      revision = useLocal ? local.revision : canvas.revision
      latest = useLocal ? local.state : canvas.state
      dirty = Boolean(useLocal)
      if (useLocal && local.revision !== canvas.revision) {
        conflict.value = true; error.value = '该项目已在其他窗口更新，请先选择恢复方式。'; status.value = 'conflict'
      } else {
        status.value = dirty ? 'pending' : 'saved'
      }
      initialState.value = latest; ready.value = true
      if (!conflict.value) { cache(); if (dirty) schedule() }
    } catch (failure) {
      if (!disposed) { error.value = failure instanceof Error ? failure.message : '项目读取失败'; unavailable.value = failure instanceof ApiError && [404, 410].includes(failure.status) }
    }
  }
  function update(state: Record<string, unknown>) {
    if (!ready.value || disposed) return
    // Route guards can emit the same canvas again immediately after a draft download.
    if (downloadedChange === change && JSON.stringify(latest) === JSON.stringify(state)) return
    latest = state; dirty = true; change += 1
    const cached = cache()
    if (conflict.value || unavailable.value) status.value = cached ? (conflict.value ? 'conflict' : 'local') : 'memory'
    else { status.value = 'pending'; schedule() }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(() => { void flush() }, 650) }
  async function flush(): Promise<boolean> {
    clearTimeout(timer)
    if (unavailable.value) return status.value !== 'memory' || downloadedChange === change
    if (conflict.value) return false
    if (saving) { const ok = await saving; return ok && dirty ? flush() : ok }
    if (!dirty) return true
    const sequence = change
    const snapshot = JSON.parse(JSON.stringify(latest)) as Record<string, unknown>
    status.value = 'saving'; error.value = ''
    saving = (async () => {
      try {
        const saved = await saveProjectCanvas(projectId, revision, snapshot)
        revision = saved.revision
        dirty = sequence !== change
        cache()
        if (!disposed) status.value = dirty ? 'pending' : 'saved'
        return true
      } catch (failure) {
        const cached = cache()
        if (!disposed) {
          error.value = failure instanceof Error ? failure.message : '服务端保存失败'
          conflict.value = failure instanceof ApiError && failure.status === 409
          unavailable.value = failure instanceof ApiError && [404, 410].includes(failure.status)
          status.value = cached ? (conflict.value ? 'conflict' : 'local') : 'memory'
        }
        return false
      } finally { saving = undefined }
    })()
    const ok = await saving
    return ok && dirty ? flush() : ok
  }
  function downloadLocal() {
    const blob = new Blob([JSON.stringify({ project_id: projectId, revision, state: latest }, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${project.value?.name || 'project'}-local-draft.json`; anchor.click()
    downloadedChange = change
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
  async function reloadServer() {
    // Keep a separate recovery copy before replacing the active local cache.
    try { localStorage.setItem(`frameforge-project:${projectId}:recovery-v1`, JSON.stringify({ revision, state: latest, dirty, savedAt: new Date().toISOString() })) } catch {
      if (dirty && downloadedChange !== change) { status.value = 'memory'; error.value = '请先下载当前草稿，再加载服务端版本。'; return }
    }
    await load(true)
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (!dirty) return
    cache(); event.preventDefault(); event.returnValue = ''
  }
  window.addEventListener('beforeunload', beforeUnload)
  onBeforeUnmount(() => { disposed = true; clearTimeout(timer); window.removeEventListener('beforeunload', beforeUnload) })
  return { project, initialState, ready, error, conflict, unavailable, status, load, update, flush, downloadLocal, reloadServer }
}
