import { beforeEach, describe, expect, it, vi } from 'vitest'
import { webcrypto } from 'node:crypto'
import { createProject, importLegacyCanvas } from '../src/api/projects'
import { migrateLegacyCanvas, readProjectCache } from '../src/projectPersistence'

vi.mock('../src/api/projects', () => ({ createProject: vi.fn(), importLegacyCanvas: vi.fn() }))
describe('legacy project migration', () => {
  beforeEach(() => {
    localStorage.clear(); vi.clearAllMocks(); vi.stubGlobal('crypto', webcrypto)
    vi.mocked(createProject).mockResolvedValue({ id: 'project-history', name: '旧工作台', created_at: '', updated_at: '' })
    vi.mocked(importLegacyCanvas).mockImplementation(async (id, state) => ({ project_id: id, state, revision: 1 }))
  })
  it('preserves drafts, viewport, hidden jobs, and original data without pruning shared images', async () => {
    const source = { jobKeys: {}, positions: {}, sceneUi: {}, selectedId: '', activeTaskKey: '',
      drafts: [{ key: 'draft-1', inputType: 'script', position: { x: 20, y: 30 }, scriptText: '雨停了。' }],
      viewport: { x: 90, y: 40, zoom: .7 }, activeView: 'timeline', hiddenJobIds: ['job-hidden'] }
    localStorage.setItem('frameforge-canvas-v1', JSON.stringify(source))
    await expect(migrateLegacyCanvas('')).resolves.toBe('project-history')
    expect(vi.mocked(importLegacyCanvas).mock.calls[0][1]).toMatchObject(source)
    expect(JSON.parse(localStorage.getItem('frameforge-canvas-v1')!)).toEqual(source)
    expect(readProjectCache('project-history')?.state).toMatchObject(source)
    await expect(migrateLegacyCanvas('')).resolves.toBeNull()
    expect(createProject).toHaveBeenCalledTimes(1)
  })
  it('uses the same backend creation idempotency key across concurrent tabs', async () => {
    localStorage.setItem('frameforge-canvas-v1', JSON.stringify({ jobKeys: { 'job-1': 'input-1' }, positions: {}, sceneUi: {}, selectedId: '', activeTaskKey: '' }))
    let finish!: (project: Awaited<ReturnType<typeof createProject>>) => void
    const pending = new Promise<Awaited<ReturnType<typeof createProject>>>(resolve => { finish = resolve })
    vi.mocked(createProject).mockReturnValue(pending)
    const one = migrateLegacyCanvas(''), two = migrateLegacyCanvas('')
    await vi.waitFor(() => expect(createProject).toHaveBeenCalledTimes(2))
    const keys = vi.mocked(createProject).mock.calls.map(call => call[1])
    expect(keys[0]).toMatch(/^legacy-[0-9a-f]{64}$/)
    expect(keys[1]).toBe(keys[0])
    finish({ id: 'project-history', name: '旧工作台', created_at: '', updated_at: '' })
    await Promise.all([one, two])
    expect(importLegacyCanvas).toHaveBeenCalledTimes(2)
  })
  it('retains the pending project and original canvas after a failed import', async () => {
    const source = JSON.stringify({ jobKeys: { 'job-1': 'input-1' }, positions: {}, sceneUi: {}, selectedId: '', activeTaskKey: '' })
    localStorage.setItem('frameforge-canvas-v1', source)
    vi.mocked(importLegacyCanvas).mockRejectedValueOnce(new Error('offline'))
    await expect(migrateLegacyCanvas('')).rejects.toThrow('offline')
    expect(localStorage.getItem('frameforge-project-migration-v1')).toBeNull()
    expect(localStorage.getItem('frameforge-canvas-v1')).toBe(source)
    await migrateLegacyCanvas('')
    expect(createProject).toHaveBeenCalledTimes(1)
  })
})
