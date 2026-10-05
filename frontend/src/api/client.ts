import type { Envelope, ErrorDetail } from '../types/contracts'

export class ApiError extends Error {
  constructor(
    public status: number,
    public error: ErrorDetail,
    public requestId = '',
    public retryAfter = 0,
  ) {
    super(error.message)
  }
}
let csrf = ''
let csrfFlight: Promise<void> | null = null
let onUnauthorized: () => void = () => {}
export function unauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}
export function clearCsrf() {
  csrf = ''
}
export async function refreshCsrf() {
  if (!csrfFlight)
    csrfFlight = request<{ csrf_token: string }>('/auth/csrf')
      .then((r) => {
        csrf = r.data.csrf_token
      })
      .finally(() => {
        csrfFlight = null
      })
  await csrfFlight
}
interface Options {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  key?: string
  binary?: boolean
  signal?: AbortSignal
}
export function retryDelay(header: string | null, now = Date.now()) {
  if (!header) return 0
  const seconds = Number(header)
  return Number.isFinite(seconds)
    ? Math.max(0, seconds * 1000)
    : Math.max(0, Date.parse(header) - now) || 0
}
export async function request<T>(path: string, options: Options = {}): Promise<Envelope<T>> {
  const result = await send(path, options)
  return result as Envelope<T>
}
export async function binary(path: string, signal?: AbortSignal): Promise<Blob> {
  return (await send(path, { binary: true, signal })) as Blob
}
async function send(path: string, options: Options): Promise<unknown> {
  // No arbitrary host, private storage path, or vendor URL is accepted here.
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('://'))
    throw new Error('仅允许同源 API 地址')
  const url = path.startsWith('/api/v1/') ? path : `/api/v1${path}`
  const method = options.method ?? 'GET'
  if (method !== 'GET' && !csrf) await refreshCsrf()
  const headers: Record<string, string> = {
    'X-Request-Id': `req_${crypto.randomUUID().replaceAll('-', '')}`,
    Accept: options.binary ? '*/*' : 'application/json',
  }
  if (method !== 'GET') headers['X-CSRFToken'] = csrf
  if (options.key) headers['Idempotency-Key'] = options.key
  const multipart = options.body instanceof FormData
  if (options.body !== undefined && !multipart) headers['Content-Type'] = 'application/json'
  const controller = new AbortController()
  const abort = () => controller.abort()
  options.signal?.addEventListener('abort', abort, { once: true })
  if (options.signal?.aborted) controller.abort()
  const timeout = setTimeout(abort, multipart || options.binary ? 120_000 : 30_000)
  try {
    const response = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers,
      signal: controller.signal,
      body:
        options.body === undefined
          ? undefined
          : multipart
            ? (options.body as FormData)
            : JSON.stringify(options.body),
    })
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as {
        error?: ErrorDetail
        meta?: { request_id: string }
      } | null
      if (response.status === 401 && !url.endsWith('/auth/login')) onUnauthorized()
      if (body?.error?.code === 'CSRF_FAILED') clearCsrf()
      const detail = body?.error ?? {
        code: 'HTTP_ERROR',
        message: '服务暂时无法响应，请重试',
        details: {},
        retryable: response.status >= 500,
      }
      if (response.status === 404) detail.message = '资源不存在或不可用'
      throw new ApiError(
        response.status,
        detail,
        body?.meta?.request_id ?? response.headers.get('X-Request-Id') ?? '',
        retryDelay(response.headers.get('Retry-After')),
      )
    }
    return options.binary ? await response.blob() : await response.json()
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError(0, {
      code: 'NETWORK_ERROR',
      message: '网络中断或请求超时，提交结果尚不确定。可重试同一次提交。',
      details: {},
      retryable: true,
    })
  } finally {
    clearTimeout(timeout)
    options.signal?.removeEventListener('abort', abort)
  }
}
// Frozen payload + key survive an uncertain response. A changed form requires
// an explicit new intent; callers must not silently resubmit a changed body.
export class Submission {
  pending: { signature: string; key: string; run: () => Promise<unknown> } | null = null
  async execute<T>(signature: string, run: (key: string) => Promise<T>): Promise<T> {
    if (this.pending && this.pending.signature !== signature)
      throw new ApiError(0, {
        code: 'UNCERTAIN_SUBMISSION',
        message: '上次提交结果尚不确定。请先按原内容重试确认，再发起新的修改。',
        details: {},
        retryable: true,
      })
    if (!this.pending) {
      const key = crypto.randomUUID()
      this.pending = { signature, key, run: () => run(key) }
    }
    try {
      const result = (await this.pending.run()) as T
      this.pending = null
      return result
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status > 0 &&
        error.status < 500 &&
        error.status !== 429
      )
        this.pending = null
      throw error
    }
  }
}
export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请重试'
}
