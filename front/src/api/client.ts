const API_BASE = (import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api').replace(/\/$/, '')
let activeProjectId = ''
let sessionToken: string | null = null
let sessionRequest: Promise<string> | null = null
export function setApiProjectId(id: string) { activeProjectId = id }
export function clearApiSession() { sessionToken = null; sessionRequest = null }

export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code?: string) { super(message); this.name = 'ApiError' }
}

async function responseError(response: Response): Promise<ApiError> {
    let detail = `${response.status} ${response.statusText}`
    let code: string | undefined
    try {
      const body = await response.json()
      const value = body.detail
      detail = typeof value === 'string' ? value : Array.isArray(value) ? value.map((item: { msg?: string }) => item.msg || String(item)).join('；') : value ? JSON.stringify(value) : detail
      code = typeof body.error?.code === 'string' ? body.error.code : undefined
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    return new ApiError(detail, response.status, code)
}

async function localSession(): Promise<string> {
  if (sessionToken !== null) return sessionToken
  if (!sessionRequest) {
    sessionRequest = (async () => {
      const response = await fetch(`${API_BASE}/session`, { cache: 'no-store', headers: { Accept: 'application/json' } })
      if (!response.ok) throw await responseError(response)
      const data = await response.json()
      if (data.enabled !== false && (typeof data.token !== 'string' || !data.token)) throw new Error('本机会话初始化失败，请刷新页面。')
      sessionToken = data.enabled === false ? '' : data.token
      return sessionToken!
    })().finally(() => { sessionRequest = null })
  }
  return sessionRequest
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const write = !['GET', 'HEAD', 'OPTIONS'].includes((init?.method || 'GET').toUpperCase())
  const headers = new Headers(init?.headers)
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  // Capture project identity before awaiting bootstrap; switching projects must
  // never redirect an already-started write into the newly active project.
  if (activeProjectId) headers.set('X-Project-ID', activeProjectId)
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      if (init?.signal?.aborted) throw init.signal.reason || new DOMException('cancelled', 'AbortError')
      if (write) headers.set('X-FrameForge-Session', await localSession())
      if (init?.signal?.aborted) throw init.signal.reason || new DOMException('cancelled', 'AbortError')
      const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
      if (response.ok) return response.json() as Promise<T>
      const error = await responseError(response)
      // These codes guarantee rejection BEFORE dispatch. Never replay ambiguous
      // network failures, validation errors, conflicts or arbitrary HTTP 403s.
      if (write && !attempt && error.status === 403 &&
          ['session_token_missing', 'session_token_invalid'].includes(error.code || '') &&
          !(typeof ReadableStream !== 'undefined' && init?.body instanceof ReadableStream)) {
        clearApiSession()
        continue
      }
      throw error
    }
    throw new Error('本机会话已失效，请刷新页面。')
  } catch (error) {
    if (error instanceof TypeError) throw new Error('无法连接后端。请确认后端已启动，并从本项目的 5174 页面进入；使用其他地址时，请检查 API 地址和后端跨域白名单。')
    throw error
  }
}

export function apiUrl(path: string): string {
  const url = `${API_BASE}${path}`
  return activeProjectId && /^\/(jobs|generations|generation-assets)\//.test(path)
    ? `${url}${url.includes('?') ? '&' : '?'}project_id=${encodeURIComponent(activeProjectId)}` : url
}
