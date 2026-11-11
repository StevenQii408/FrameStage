const DB_NAME = 'frameforge-local-media-v1'
const STORE_NAME = 'images'
const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/avif'])
const MAX_BYTES = 25 * 1024 * 1024

export function validateLocalImage(file: Pick<File, 'type' | 'size'>): string | null {
  if (!ALLOWED_TYPES.has(file.type)) return '仅支持 PNG、JPEG、WebP、GIF 或 AVIF 图片'
  if (file.size > MAX_BYTES) return '图片不能超过 25 MB'
  if (file.size <= 0) return '图片文件为空'
  return null
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) { reject(new Error('此浏览器不支持本地图片存储')); return }
    const request = indexedDB.open(DB_NAME, 1)
    request.onupgradeneeded = () => { if (!request.result.objectStoreNames.contains(STORE_NAME)) request.result.createObjectStore(STORE_NAME) }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('图片存储不可用'))
  })
}

async function transaction<T>(mode: IDBTransactionMode, operation: (store: IDBObjectStore, resolve: (value: T) => void, reject: (reason: unknown) => void) => void): Promise<T> {
  const db = await openDatabase()
  return new Promise<T>((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, mode)
    let result: T
    let failed = false
    const fail = (reason: unknown) => {
      failed = true
      db.close()
      reject(reason || tx.error || new Error('图片存储失败'))
    }
    // Request success means the operation was staged, not that its transaction committed.
    tx.oncomplete = () => { db.close(); if (!failed) resolve(result) }
    tx.onerror = () => fail(tx.error)
    tx.onabort = () => fail(tx.error || new Error('图片存储已中止'))
    try {
      operation(tx.objectStore(STORE_NAME), value => { result = value }, reason => {
        fail(reason)
        try { tx.abort() } catch { /* It may already be aborted by IndexedDB. */ }
      })
    } catch (error) {
      fail(error)
      try { tx.abort() } catch { /* Preserve the original operation error. */ }
    }
  })
}

export function saveLocalImage(id: string, blob: Blob): Promise<void> {
  return transaction<void>('readwrite', (store, resolve, reject) => {
    const request = store.put(blob, id)
    request.onsuccess = () => resolve()
    request.onerror = () => reject(request.error)
  })
}

export function loadLocalImage(id: string): Promise<Blob | null> {
  return transaction<Blob | null>('readonly', (store, resolve, reject) => {
    const request = store.get(id)
    request.onsuccess = () => resolve(request.result instanceof Blob ? request.result : null)
    request.onerror = () => reject(request.error)
  })
}

export async function imageDimensions(blob: Blob): Promise<{ width: number; height: number }> {
  const bitmap = await createImageBitmap(blob)
  try { return { width: bitmap.width, height: bitmap.height } } finally { bitmap.close() }
}
