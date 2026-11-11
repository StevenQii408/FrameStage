import { flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadLocalImage, saveLocalImage, validateLocalImage } from '../src/generatorStorage'

describe('local image import validation', () => {
  it('accepts images but rejects video, empty and oversized files', () => {
    expect(validateLocalImage({ type: 'image/png', size: 1024 })).toBeNull()
    expect(validateLocalImage({ type: 'video/mp4', size: 1024 })).toContain('图片')
    expect(validateLocalImage({ type: 'image/jpeg', size: 0 })).toContain('为空')
    expect(validateLocalImage({ type: 'image/webp', size: 26 * 1024 * 1024 })).toContain('25 MB')
  })
})

function controlledDatabase() {
  const request = { result: undefined as unknown, error: null as Error | null, onsuccess: () => {}, onerror: () => {} }
  const store = { put: vi.fn(() => request), get: vi.fn(() => request) }
  const tx = { error: null as Error | null, oncomplete: () => {}, onerror: () => {}, onabort: () => {},
    objectStore: () => store, abort: vi.fn(() => tx.onabort()) }
  const db = { transaction: vi.fn(() => tx), close: vi.fn() }
  vi.stubGlobal('indexedDB', { open: () => {
    const open = { result: db, onsuccess: () => {} }
    queueMicrotask(() => open.onsuccess())
    return open
  } })
  return { request, store, tx, db }
}
afterEach(() => vi.unstubAllGlobals())

describe('IndexedDB commit acknowledgement', () => {
  it('does not announce image save before the transaction commits', async () => {
    const { request, tx, db } = controlledDatabase()
    let saved = false
    const result = saveLocalImage('image-test', new Blob(['pixels'])).then(() => { saved = true })
    await flushPromises()
    request.onsuccess(); await flushPromises()
    expect(saved).toBe(false)
    expect(db.close).not.toHaveBeenCalled()
    tx.oncomplete(); await result
    expect(saved).toBe(true)
    expect(db.close).toHaveBeenCalled()
  })

  it('reports a commit failure even when the write request already succeeded', async () => {
    const { request, tx } = controlledDatabase()
    const result = saveLocalImage('image-test', new Blob(['pixels'])).catch(error => error)
    await flushPromises(); request.onsuccess()
    tx.error = new Error('disk commit failed'); tx.onerror()
    expect(await result).toMatchObject({ message: 'disk commit failed' })
  })

  it('rejects transaction aborts without leaving an apparently saved reference', async () => {
    const { request, tx, db } = controlledDatabase()
    const result = saveLocalImage('image-test', new Blob(['pixels'])).catch(error => error)
    await flushPromises(); request.onsuccess(); tx.onabort()
    expect(await result).toMatchObject({ message: '图片存储已中止' })
    expect(db.close).toHaveBeenCalled()
  })

  it('waits for read commit and returns the staged blob', async () => {
    const { request, tx } = controlledDatabase()
    const image = new Blob(['pixels'])
    const result = loadLocalImage('image-test')
    await flushPromises(); request.result = image; request.onsuccess(); tx.oncomplete()
    expect(await result).toBe(image)
  })
})
