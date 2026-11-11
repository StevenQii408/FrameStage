import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { apiUrl } from '../api/client'
import {
  cancelGeneration, createGeneration, generationAssetUrl, projectGenerationEventsUrl, generationTerminal,
  generationDurationIssue, getGeneration, getGenerationCapabilities, preflightGeneration, readGenerationSettings,
  retryGeneration, uploadGenerationImage,
  type GenerationAsset, type GenerationCapabilities, type GenerationEvent, type GenerationJob,
} from '../api/generations'
import { missingReferenceMentions, type GeneratorDraft, type ResolvedGenerator } from '../generatorGraph'
import { activeGeneratorAsset, isReferenceAsset, recordGeneratorSuccess } from '../generatorVersions'

const eventTypes = ['created', 'blocked', 'queued', 'started', 'submitting', 'progress', 'finalizing', 'succeeded', 'failed', 'cancelled', 'interrupted'].map(type => 'generation.' + type)
type SourceFactory = (url: string) => EventSource
interface GeneratorTaskOptions { projectId?: () => string | null | undefined }

/** Project ownership query parameters are transport scope, not image content. */
export function canonicalGeneratorIdentity(identity: string): string {
  const hashAt = identity.indexOf('#')
  const hash = hashAt >= 0 ? identity.slice(hashAt) : ''
  const address = hashAt >= 0 ? identity.slice(0, hashAt) : identity
  const queryAt = address.indexOf('?')
  if (queryAt < 0) return identity
  const query = address.slice(queryAt + 1).split('&').filter(part => {
    try { return decodeURIComponent(part.split('=')[0]) !== 'project_id' } catch { return true }
  }).join('&')
  return address.slice(0, queryAt) + (query ? '?' + query : '') + hash
}

/** Scope a saved snapshot without reading current inputs or cleaning real edits. */
export function scopeLegacyGeneratorSignature(signature: string, projectId: string): string {
  try {
    const value = JSON.parse(signature)
    if (!value || typeof value !== 'object' || Array.isArray(value)) return signature
    if (typeof value.image === 'string') value.image = canonicalGeneratorIdentity(value.image)
    if (Array.isArray(value.references)) value.references = value.references.map((reference: unknown) => {
      if (!reference || typeof reference !== 'object' || Array.isArray(reference)) return reference
      const item = reference as Record<string, unknown>
      return typeof item.identity === 'string' ? { ...item, identity: canonicalGeneratorIdentity(item.identity) } : reference
    })
    if (value.projectId === undefined || value.projectId === null || value.projectId === '') value.projectId = projectId
    return JSON.stringify(value)
  } catch { return signature }
}

export function generatorInputSignature(item: GeneratorDraft, resolved: ResolvedGenerator, projectId?: string | null): string {
  return JSON.stringify({ kind: item.kind, prompt: resolved.prompt.trim(),
    image: canonicalGeneratorIdentity(resolved.imageIdentity || (resolved.imageUrl.startsWith('blob:') ? 'local:' + (item.imageAssetId || '') : resolved.imageUrl)),
    settings: readGenerationSettings(item.generationSettings, item.kind),
    ...(resolved.references && (resolved.references.length > 1 || /@图片\d+/.test(resolved.prompt) || resolved.references.some(reference => !reference.selected))
      ? { references: resolved.references.map(reference => ({ id: reference.id, identity: canonicalGeneratorIdentity(reference.identity), selected: reference.selected, mention: reference.mention, firstFrame: reference.firstFrame || false })) } : {}),
    ...(projectId ? { projectId } : {}) })
}

/** Backend-owned generation tasks. No synthetic timers, media or completion. */
export function useGeneratorTasks(
  generators: Ref<GeneratorDraft[]>, resolve: (id: string) => ResolvedGenerator,
  changed: () => void, notify: (message: string) => void,
  factoryOrOptions: SourceFactory | GeneratorTaskOptions = url => new EventSource(url),
  extraOptions: GeneratorTaskOptions = {},
) {
  const factory = typeof factoryOrOptions === 'function' ? factoryOrOptions : (url: string) => new EventSource(url)
  const options = typeof factoryOrOptions === 'function' ? extraOptions : factoryOrOptions
  const jobs = ref<Record<string, GenerationJob>>({})
  const capabilities = ref<GenerationCapabilities | null>(null)
  const busy = ref<Record<string, boolean>>({})
  const errors = ref<Record<string, string>>({})
  const subscriptions = new Map<string, string>()
  const pendingEvents = new Map<string, { job: GenerationJob; cursor: number }>()
  let stream: EventSource | null = null
  let projectCursor: number | null = null
  let scopeVersion = 0
  let recovering: Promise<void> | null = null
  const operationVersions = new Map<string, number>()
  const operationProjects = new Map<string, string | null>()
  let disposed = false
  const node = (id: string) => generators.value.find(item => item.id === id)
  const sameProject = (job: GenerationJob) => (job.project_id || job.request.project_id || null) === (options.projectId?.() || null)
  const signature = (id: string) => {
    const item = node(id)
    return item ? generatorInputSignature(item, resolve(id), options.projectId?.()) : ''
  }
  function isStale(id: string): boolean {
    const item = node(id)
    return Boolean(item?.generationTask && item.generationTask.inputSignature !== signature(id))
  }
  function canRecoverSubmission(id: string): boolean {
    const pending = node(id)?.pendingSubmission
    return Boolean(pending && !pending.retryOf && pending.inputSignature === signature(id))
  }
  function closeStream() { stream?.close(); stream = null }
  function stopIfIdle() {
    if (!subscriptions.size && !Object.entries(busy.value).some(([id, pending]) => pending && node(id))) closeStream()
  }
  function close(id: string) { subscriptions.delete(id); stopIfIdle() }
  function current(id: string, jobId: string) { return !disposed && node(id)?.generationTask?.jobId === jobId }
  function accept(id: string, job: GenerationJob) {
    if (!current(id, job.id) || job.node_id !== id || node(id)?.kind !== job.kind || !sameProject(job)) return
    const before = jobs.value[id]
    if (before?.id === job.id && (job.revision <= before.revision
      || (generationTerminal(before.status) && !(before.status === 'blocked' && job.status === 'cancelled')))) return
    jobs.value = { ...jobs.value, [id]: job }
    recordGeneratorSuccess(node(id)!, job, node(id)?.generationTask?.inputSignature)
    changed()
  }
  function applyEvent(job: GenerationJob, cursor: number) {
    const id = job.node_id, draft = node(id)
    if (!draft?.generationTask || !current(id, job.id) || draft.kind !== job.kind || !sameProject(job)) return false
    if (cursor <= (draft.generationTask.eventCursor || 0)) return true
    draft.generationTask.eventCursor = cursor
    accept(id, job)
    changed()
    const latest = jobs.value[id]
    if (latest?.id === job.id && generationTerminal(latest.status)) close(id)
    return true
  }
  function connect(id: string, jobId: string) {
    const item = node(id), job = jobs.value[id]
    if (!item || !current(id, jobId) || document.hidden || (job?.id === jobId && generationTerminal(job.status))) return
    subscriptions.set(id, jobId)
    const pending = pendingEvents.get(jobId)
    if (pending) { pendingEvents.delete(jobId); applyEvent(pending.job, pending.cursor) }
    if (!subscriptions.size || stream) return
    if (projectCursor === null) {
      // Saved node cursors share the project's global sequence. Start at the
      // oldest saved cursor so restoring several nodes cannot skip one of them.
      const cursors = generators.value.flatMap(draft => draft.generationTask ? [draft.generationTask.eventCursor || 0] : [])
      projectCursor = cursors.length ? Math.min(...cursors) : 0
    }
    const projectId = options.projectId?.() || null, version = scopeVersion
    const source = factory(projectGenerationEventsUrl(projectId, projectCursor))
    stream = source
    const live = () => !disposed && stream === source && scopeVersion === version
      && (options.projectId?.() || null) === projectId
    function dispatch(message: MessageEvent) {
      if (!live()) return
      try {
        const event = JSON.parse(message.data) as GenerationEvent
        const cursor = Number(message.lastEventId || event.sequence)
        const snapshot = event.payload?.job
        if (!Number.isSafeInteger(cursor) || cursor !== event.sequence || cursor <= (projectCursor || 0) || !snapshot
          || snapshot.id !== event.job_id || !sameProject(snapshot)) return
        const binding = generators.value.find(draft => draft.generationTask?.jobId === snapshot.id)
        if (binding && (binding.id !== snapshot.node_id || binding.kind !== snapshot.kind)) return
        projectCursor = cursor
        if (applyEvent(snapshot, cursor)) return
        // A fast cloud task may publish before its POST response binds the job
        // to a node. Retain its latest snapshot, without resurrecting old work.
        const draft = node(snapshot.node_id)
        if (!draft || draft.kind !== snapshot.kind) return
        const previous = pendingEvents.get(snapshot.id)
        if (!previous || snapshot.revision > previous.job.revision) pendingEvents.set(snapshot.id, { job: snapshot, cursor })
        while (pendingEvents.size > 100) pendingEvents.delete(pendingEvents.keys().next().value!)
      } catch { /* REST recovery remains authoritative after a malformed event. */ }
    }
    source.onmessage = dispatch
    for (const type of eventTypes) source.addEventListener(type, dispatch as EventListener)
    source.onerror = () => {
      // EventSource reconnects with Last-Event-ID. REST repairs any missing
      // snapshots, but never creates a second stream or resubmits paid work.
      if (!live() || recovering) return
      const recovery = Promise.all([...subscriptions].map(([nodeId, taskId]) => refresh(nodeId, taskId, false))).then(() => undefined)
      recovering = recovery
      void recovery.finally(() => { if (recovering === recovery) recovering = null })
    }
  }
  async function refresh(id: string, jobId: string, reconnect = true) {
    const version = scopeVersion
    try {
      const job = await getGeneration(jobId)
      if (version !== scopeVersion || !current(id, jobId)) return
      if (!sameProject(job)) { close(id); throw new Error('此生成任务不属于当前项目，已停止恢复') }
      accept(id, job)
      if (generationTerminal(job.status)) close(id)
      else if (reconnect && !document.hidden) connect(id, jobId)
    } catch (error) {
      if (version === scopeVersion && current(id, jobId)) {
        errors.value = { ...errors.value, [id]: error instanceof Error ? error.message : '生成任务读取失败' }
        changed()
      }
    }
  }
  async function referenceBlob(url: string): Promise<Blob> {
    if (!url.startsWith('blob:')) {
      const source = new URL(url, window.location.href)
      const base = new URL(apiUrl('/'), window.location.href)
      if (source.origin !== base.origin || !/^\/api\/(?:jobs\/[^/]+\/frames\/[^/]+|generation-assets\/[^/]+)$/.test(source.pathname)) {
        throw new Error('参考图片必须来自本地导入或本系统帧资产')
      }
    }
    const response = await fetch(url)
    if (!response.ok) throw new Error('参考图片无法读取，请重新导入或连接')
    return response.blob()
  }
  function begin(id: string) {
    const version = (operationVersions.get(id) || 0) + 1
    operationVersions.set(id, version)
    operationProjects.set(id, options.projectId?.() || null)
    busy.value = { ...busy.value, [id]: true }
    errors.value = { ...errors.value, [id]: '' }
    changed()
    return version
  }
  const active = (id: string, version: number) => !disposed && Boolean(node(id)) && operationVersions.get(id) === version
    && operationProjects.get(id) === (options.projectId?.() || null)
  function fail(id: string, error: unknown) {
    const message = error instanceof Error ? error.message : '生成任务保存失败'
    errors.value = { ...errors.value, [id]: message }
    notify(message)
  }
  function finish(id: string, version: number) {
    if (!active(id, version)) return
    busy.value = { ...busy.value, [id]: false }
    stopIfIdle()
    changed()
  }
  async function receive(id: string, job: GenerationJob, inputSignature: string, version: number) {
    if (!active(id, version)) {
      // Deleting a node during submission must not leave paid work orphaned.
      if (!disposed && !node(id) && sameProject(job) && !generationTerminal(job.status)) await cancelGeneration(job.id).catch(() => undefined)
      return
    }
    const draft = node(id)!
    if (!sameProject(job)) throw new Error('生成任务响应不属于当前项目，已停止应用')
    close(id)
    draft.generationTask = { jobId: job.id, inputSignature, eventCursor: 0 }
    delete draft.pendingSubmission
    jobs.value = { ...jobs.value, [id]: job }
    recordGeneratorSuccess(draft, job, inputSignature)
    changed()
    connect(id, job.id)
    notify(job.status === 'blocked' ? '生成任务已保存，等待接入生成 API；未发起生成请求' : '生成任务已提交')
  }
  async function submit(id: string) {
    const item = node(id)
    if (!item || busy.value[id]) return
    if (isReferenceAsset(item)) { notify('上传图片仅作为参考资产，请连接到新的生成节点'); return }
    const previous = jobs.value[id]
    if (previous && !generationTerminal(previous.status)) { notify('该节点已有进行中的生成任务'); return }
    if (item.generationTask && !isStale(id) && previous?.status === 'blocked') { notify('任务已保存，接入生成 API 后可显式重试'); return }
    const resolved = resolve(id)
    if (!resolved.prompt.trim()) { fail(id, new Error('请填写或连接文字 Prompt')); return }
    if (item.kind === 'video' && !resolved.imageUrl) { fail(id, new Error('视频生成需要首帧参考图片')); return }
    const missing = missingReferenceMentions(resolved.prompt, resolved.references || [])
    if (missing.length) { fail(id, new Error('以下图片引用未选择或已断开：' + missing.join('、'))); return }
    if ((resolved.references || []).filter(reference => reference.selected).length > 8) { fail(id, new Error('每个任务最多选择八张参考图')); return }
    const inputSignature = signature(id)
    const settings = readGenerationSettings(item.generationSettings, item.kind)
    const recovering = canRecoverSubmission(id)
    const durationIssue = generationDurationIssue(settings, item.kind, capabilities.value)
    if (durationIssue && !recovering) { fail(id, new Error(durationIssue)); return }
    const projectId = options.projectId?.()
    const references = (resolved.references || []).filter(reference => reference.selected).map(reference => ({ ...reference }))
    const version = begin(id)
    try {
      const referenceImages = []
      for (const reference of references) {
        const asset = await uploadGenerationImage(await referenceBlob(reference.url), projectId)
        referenceImages.push({ reference_id: reference.id, asset_id: asset.id, mention: reference.mention,
          role: item.kind === 'video' && reference.firstFrame ? 'first_frame' as const : 'reference' as const })
      }
      const primary = referenceImages.find(reference => reference.role === 'first_frame') || referenceImages[0]
      const legacy = !resolved.references && resolved.imageUrl ? await uploadGenerationImage(await referenceBlob(resolved.imageUrl), projectId) : null
      if (!active(id, version)) return
      const request = { node_id: id, kind: item.kind, prompt: resolved.prompt.trim(), reference_asset_id: primary?.asset_id || legacy?.id || null,
        settings, ...(referenceImages.length && (referenceImages.length > 1 || /@图片\d+/.test(resolved.prompt)) ? { reference_images: referenceImages } : {}), ...(projectId ? { project_id: projectId } : {}) }
      if (!recovering) {
        const check = await preflightGeneration(request)
        if (!active(id, version)) return
        if (!check.valid) throw new Error(check.issues.join('；') || check.reason || '生成参数未通过校验')
      }
      let pending = node(id)!.pendingSubmission
      if (!pending || pending.inputSignature !== inputSignature || pending.retryOf) {
        pending = { inputSignature, idempotencyKey: crypto.randomUUID() }
        node(id)!.pendingSubmission = pending
        changed() // Persist before POST; a lost response/reload must reuse this key.
      }
      const job = await createGeneration(request, pending.idempotencyKey)
      await receive(id, job, inputSignature, version)
    } catch (error) { if (active(id, version)) fail(id, error) }
    finally { finish(id, version) }
  }
  async function cancel(id: string) {
    const jobId = node(id)?.generationTask?.jobId
    if (!jobId || busy.value[id]) return
    if (!jobs.value[id] || !sameProject(jobs.value[id])) { notify('此生成任务不属于当前项目'); return }
    const version = begin(id)
    try {
      const job = await cancelGeneration(jobId)
      if (active(id, version) && current(id, jobId)) { accept(id, job); close(id) }
    } catch (error) { if (active(id, version)) fail(id, error) }
    finally { finish(id, version) }
  }
  async function retry(id: string) {
    const item = node(id), job = jobs.value[id]
    if (!item?.generationTask || !job || busy.value[id]) return
    if (isReferenceAsset(item)) { notify('上传图片仅作为参考资产，请连接到新的生成节点'); return }
    if (!sameProject(job)) { notify('此生成任务不属于当前项目'); return }
    if (job.error?.retryable === false) { notify('此错误需要修改输入后保存新任务'); return }
    if (isStale(id)) { notify('当前输入已变化，请校验并保存新任务'); return }
    const durationIssue = generationDurationIssue(job.request.settings, item.kind, capabilities.value)
    const recovering = item.pendingSubmission?.retryOf === job.id && item.pendingSubmission?.inputSignature === item.generationTask.inputSignature
    if (durationIssue && !recovering) { fail(id, new Error(durationIssue)); return }
    const version = begin(id)
    let pending = item.pendingSubmission
    if (!pending || pending.retryOf !== job.id || pending.inputSignature !== item.generationTask.inputSignature) {
      pending = { idempotencyKey: crypto.randomUUID(), inputSignature: item.generationTask.inputSignature, retryOf: job.id }
      item.pendingSubmission = pending
      changed()
    }
    try { await receive(id, await retryGeneration(job.id, pending.idempotencyKey), item.generationTask.inputSignature, version) }
    catch (error) { if (active(id, version)) fail(id, error) }
    finally { finish(id, version) }
  }
  function detach(id: string) {
    const job = jobs.value[id]
    operationVersions.set(id, (operationVersions.get(id) || 0) + 1)
    close(id)
    if (job && sameProject(job) && ['queued', 'submitting', 'running', 'finalizing'].includes(job.status)) void cancelGeneration(job.id).catch(() => undefined)
    busy.value = { ...busy.value, [id]: false }
    stopIfIdle()
  }
  async function restore() {
    const items = generators.value.flatMap(item => item.generationTask ? [{ id: item.id, jobId: item.generationTask.jobId }] : [])
    // REST identifies which saved tasks are still active. Subsequent replay
    // starts at the saved cursor, covering changes that happen during the GET.
    await Promise.all(items.map(item => refresh(item.id, item.jobId)))
  }
  function outputFor(id: string): GenerationAsset | null {
    const item = node(id), job = jobs.value[id]
    if (!item || isReferenceAsset(item)) return null
    // Draft edits and a new attempt never erase an already delivered version.
    const selected = activeGeneratorAsset(item)
    if (selected) return selected
    if (job?.status !== 'succeeded') return null
    if (!sameProject(job)) return null
    const output = job.assets.find(asset => asset.mime_type?.startsWith(item.kind + '/') && generationAssetUrl(asset.url))
    return output ? { ...output, url: generationAssetUrl(output.url) } : null
  }
  function visibility() {
    if (document.hidden) { subscriptions.clear(); closeStream() }
    else void restore()
  }
  watch(() => generators.value.map(item => item.id), ids => {
    for (const id of subscriptions.keys()) if (!ids.includes(id)) close(id)
  })
  watch(() => options.projectId?.() || null, () => {
    scopeVersion++
    subscriptions.clear(); pendingEvents.clear(); closeStream()
    projectCursor = null; recovering = null
    for (const [id, version] of operationVersions) operationVersions.set(id, version + 1)
    jobs.value = {}; busy.value = {}; errors.value = {}
  })
  onMounted(() => {
    document.addEventListener('visibilitychange', visibility)
    void getGenerationCapabilities().then(value => { if (!disposed) capabilities.value = value }).catch(() => undefined)
  })
  onBeforeUnmount(() => { disposed = true; subscriptions.clear(); pendingEvents.clear(); closeStream(); document.removeEventListener('visibilitychange', visibility) })
  return { jobs, capabilities, busy, errors, isStale, canRecoverSubmission, submit, cancel, retry, detach, restore, outputFor,
    streamCount: () => stream ? 1 : 0 }
}
