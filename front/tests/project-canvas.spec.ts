import { Blob as ExportBlob } from 'node:buffer'
import { defineComponent, h } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/client'
import * as api from '../src/api/projects'
import { useProjectCanvas } from '../src/composables/useProjectCanvas'
import { projectCacheKey, readProjectCache, writeProjectCache } from '../src/projectPersistence'

vi.mock('../src/api/projects', () => ({ getProject: vi.fn(), getProjectCanvas: vi.fn(), saveProjectCanvas: vi.fn(), createProject: vi.fn(), importLegacyCanvas: vi.fn() }))

const wrappers: ReturnType<typeof mount>[] = []
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function harness(id = 'project-a') {
  let canvas!: ReturnType<typeof useProjectCanvas>
  const wrapper = mount(defineComponent({ setup() { canvas = useProjectCanvas(id); return () => h('div') } }))
  wrappers.push(wrapper)
  return { canvas, wrapper }
}
const state = (prompt: string) => ({ generators: [{ id: 'gen-same', manualPrompt: prompt }] })
const saved = (revision: number, value = state('server'), project = 'project-a') => ({ project_id: project, revision, state: value })

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  vi.mocked(api.getProject).mockImplementation(async id => ({ id, name: id, created_at: '', updated_at: '', job_ids: [] }))
  vi.mocked(api.getProjectCanvas).mockImplementation(async id => saved(4, state('server'), id))
  vi.mocked(api.saveProjectCanvas).mockImplementation(async (id, revision, value) => ({ project_id: id, revision: revision + 1, state: value }))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })

describe('project canvas persistence and revision isolation', () => {
  it('accepts a duplicate-only server repair without manufacturing a stale-cache conflict', async () => {
    const draft = { key: 'draft-one', inputType: 'video', position: { x: 80, y: 90 }, scriptText: '' }
    const original = { ...state('same prompt'), drafts: Array(512).fill(draft) }
    const repaired = { ...state('same prompt'), drafts: [draft] }
    writeProjectCache('project-a', { revision: 3, state: original, dirty: true, savedAt: '' })
    vi.mocked(api.getProjectCanvas).mockResolvedValue(saved(4, repaired))
    const { canvas } = harness()
    await canvas.load()
    expect(canvas.conflict.value).toBe(false)
    expect(canvas.initialState.value).toEqual(repaired)
    expect(readProjectCache('project-a')).toMatchObject({ revision: 4, dirty: false, state: repaired })
    expect(api.saveProjectCanvas).not.toHaveBeenCalled()
  })

  it('keeps real local edits protected when the server removed duplicate drafts', async () => {
    const draft = { key: 'draft-one', inputType: 'video', position: { x: 80, y: 90 }, scriptText: '' }
    writeProjectCache('project-a', { revision: 3, state: { ...state('unsaved edit'), drafts: Array(512).fill(draft) }, dirty: true, savedAt: '' })
    vi.mocked(api.getProjectCanvas).mockResolvedValue(saved(4, { ...state('server prompt'), drafts: [draft] }))
    const { canvas } = harness()
    await canvas.load()
    expect(canvas.conflict.value).toBe(true)
    expect(readProjectCache('project-a')?.revision).toBe(3)
    expect(api.saveProjectCanvas).not.toHaveBeenCalled()
  })
  it('keeps edits made during a pending save and serializes the next save against the returned revision', async () => {
    const { canvas } = harness()
    await canvas.load()
    const first = deferred<api.ProjectCanvas>()
    vi.mocked(api.saveProjectCanvas).mockImplementationOnce(() => first.promise)
    canvas.update(state('first'))
    const pending = canvas.flush()
    expect(api.saveProjectCanvas).toHaveBeenCalledWith('project-a', 4, state('first'))
    canvas.update(state('second'))
    const parallel = canvas.flush()
    expect(readProjectCache('project-a')).toMatchObject({ revision: 4, dirty: true, state: state('second') })
    first.resolve(saved(5, state('first')))
    expect(await pending).toBe(true)
    expect(await parallel).toBe(true)
    expect(api.saveProjectCanvas).toHaveBeenCalledTimes(2)
    expect(api.saveProjectCanvas).toHaveBeenLastCalledWith('project-a', 5, state('second'))
    expect(readProjectCache('project-a')).toMatchObject({ revision: 6, dirty: false, state: state('second') })
    expect(canvas.status.value).toBe('saved')
  })

  it('preserves a failed network save locally and retries the same expected revision', async () => {
    const { canvas } = harness()
    await canvas.load()
    canvas.update(state('offline edit'))
    vi.mocked(api.saveProjectCanvas).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect(await canvas.flush()).toBe(false)
    expect(canvas.status.value).toBe('local')
    expect(canvas.conflict.value).toBe(false)
    expect(readProjectCache('project-a')).toMatchObject({ revision: 4, dirty: true, state: state('offline edit') })
    expect(await canvas.flush()).toBe(true)
    expect(api.saveProjectCanvas).toHaveBeenLastCalledWith('project-a', 4, state('offline edit'))
  })

  it('exports the current window draft when both the cache and server save fail', async () => {
    const { canvas } = harness()
    await canvas.load()
    const quota = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('QuotaExceededError') })
    vi.stubGlobal('Blob', ExportBlob)
    const createURL = vi.fn((_file: ExportBlob) => 'blob:export')
    vi.stubGlobal('URL', { createObjectURL: createURL, revokeObjectURL: vi.fn() })
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    try {
      canvas.update(state('latest window edit'))
      vi.mocked(api.saveProjectCanvas).mockRejectedValueOnce(new TypeError('offline'))
      expect(await canvas.flush()).toBe(false)
      expect(canvas.status.value).toBe('memory')
      expect(readProjectCache('project-a')?.state).toEqual(state('server'))
      canvas.downloadLocal()
      expect(download).toHaveBeenCalledOnce()
      const [file] = createURL.mock.calls[0]
      expect(JSON.parse(await file.text()).state).toEqual(state('latest window edit'))
      expect(await canvas.flush()).toBe(true)
      expect(canvas.status.value).toBe('saved')
    } finally { download.mockRestore(); quota.mockRestore(); vi.unstubAllGlobals() }
  })

  it('pauses after HTTP 409 and preserves the dirty draft until explicit reload', async () => {
    const { canvas } = harness()
    await canvas.load()
    canvas.update(state('conflicting draft'))
    vi.mocked(api.saveProjectCanvas).mockRejectedValueOnce(new ApiError('另一窗口已更新', 409))
    expect(await canvas.flush()).toBe(false)
    canvas.update(state('latest conflicting draft'))
    expect(await canvas.flush()).toBe(false)
    expect(api.saveProjectCanvas).toHaveBeenCalledTimes(1)
    expect(canvas.status.value).toBe('conflict')
    expect(readProjectCache('project-a')).toMatchObject({ dirty: true, state: state('latest conflicting draft') })
    vi.mocked(api.getProjectCanvas).mockResolvedValue(saved(8, state('other window')))
    await canvas.reloadServer()
    expect(JSON.parse(localStorage.getItem('frameforge-project:project-a:recovery-v1')!)).toMatchObject({ dirty: true, state: state('latest conflicting draft') })
    expect(canvas.initialState.value).toEqual(state('other window'))
    expect(readProjectCache('project-a')).toMatchObject({ revision: 8, dirty: false, state: state('other window') })
  })

  it('restores an unsaved cache with matching revision and resumes autosave', async () => {
    writeProjectCache('project-a', { revision: 4, dirty: true, state: state('recover me'), savedAt: '' })
    const { canvas } = harness()
    await canvas.load()
    expect(canvas.initialState.value).toEqual(state('recover me'))
    expect(canvas.status.value).toBe('pending')
    await vi.advanceTimersByTimeAsync(650)
    expect(api.saveProjectCanvas).toHaveBeenCalledWith('project-a', 4, state('recover me'))
    expect(canvas.status.value).toBe('saved')
  })

  it('does not silently overwrite a cached draft when the server revision differs', async () => {
    writeProjectCache('project-a', { revision: 2, dirty: true, state: state('older draft'), savedAt: '' })
    const { canvas } = harness()
    await canvas.load()
    expect(canvas.ready.value).toBe(true)
    expect(canvas.conflict.value).toBe(true)
    expect(canvas.initialState.value).toEqual(state('older draft'))
    await vi.advanceTimersByTimeAsync(2000)
    expect(api.saveProjectCanvas).not.toHaveBeenCalled()
    expect(readProjectCache('project-a')?.revision).toBe(2)
  })

  it('preserves the conflicting base revision across canvas hydration updates and a second reload', async () => {
    writeProjectCache('project-a', { revision: 2, dirty: true, state: state('older draft'), savedAt: '' })
    const first = harness()
    await first.canvas.load()
    expect(first.canvas.conflict.value).toBe(true)
    // Workbench restoration can emit its normalized snapshot even while the conflict overlay is shown.
    first.canvas.update(state('normalized older draft'))
    expect(readProjectCache('project-a')?.revision).toBe(2)
    first.wrapper.unmount()
    const second = harness()
    await second.canvas.load()
    await vi.advanceTimersByTimeAsync(2000)
    expect(second.canvas.conflict.value).toBe(true)
    expect(api.saveProjectCanvas).not.toHaveBeenCalled()
  })

  it('isolates per-project caches even when generator IDs are identical', async () => {
    const a = harness('project-a').canvas
    const b = harness('project-b').canvas
    await Promise.all([a.load(), b.load()])
    a.update(state('a only')); b.update(state('b only'))
    await Promise.all([a.flush(), b.flush()])
    expect(readProjectCache('project-a')?.state).toEqual(state('a only'))
    expect(readProjectCache('project-b')?.state).toEqual(state('b only'))
    expect(projectCacheKey('project-a')).not.toBe(projectCacheKey('project-b'))
  })

  it('does not apply a delayed load after the workspace is disposed', async () => {
    const pending = deferred<api.ProjectCanvas>()
    vi.mocked(api.getProjectCanvas).mockImplementationOnce(() => pending.promise)
    const { canvas, wrapper } = harness()
    const loading = canvas.load()
    wrapper.unmount()
    pending.resolve(saved(4))
    await loading
    expect(canvas.ready.value).toBe(false)
    expect(readProjectCache('project-a')).toBeNull()
  })

  it('clears debounce work on disposal but retains a recoverable unsaved cache', async () => {
    const { canvas, wrapper } = harness()
    await canvas.load()
    canvas.update(state('retain on close'))
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(api.saveProjectCanvas).not.toHaveBeenCalled()
    expect(readProjectCache('project-a')).toMatchObject({ dirty: true, state: state('retain on close') })
  })

  it('retains a local draft and permits leaving after another window deletes the project', async () => {
    const { canvas } = harness()
    await canvas.load()
    canvas.update(state('local before remote deletion'))
    vi.mocked(api.saveProjectCanvas).mockRejectedValueOnce(new ApiError('项目已移入回收站', 410))
    expect(await canvas.flush()).toBe(false)
    expect(canvas.unavailable.value).toBe(true)
    expect(readProjectCache('project-a')?.dirty).toBe(true)
    expect(await canvas.flush()).toBe(true)
    expect(api.saveProjectCanvas).toHaveBeenCalledTimes(1)
  })
})
