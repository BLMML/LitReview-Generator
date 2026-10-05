import { beforeEach, expect, it, vi } from 'vitest'
import {
  ApiError,
  Submission,
  binary,
  clearCsrf,
  refreshCsrf,
  request,
  retryDelay,
  unauthorizedHandler,
} from '../../src/api/client'
const ok = (data: unknown) =>
  new Response(JSON.stringify({ data, meta: { request_id: 'req_test' } }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
const failure = (status: number, code: string) =>
  new Response(
    JSON.stringify({
      error: { code, message: '失败', details: {}, retryable: false },
      meta: { request_id: 'req_failure' },
    }),
    { status, headers: { 'Content-Type': 'application/json' } },
  )
beforeEach(() => {
  clearCsrf()
  vi.unstubAllGlobals()
  unauthorizedHandler(() => {})
})
it('boots CSRF before the anonymous login POST, carries cookies and headers', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(ok({ csrf_token: 'first' }))
    .mockResolvedValueOnce(ok({ id: '1' }))
  vi.stubGlobal('fetch', fetcher)
  await request('/auth/login', { method: 'POST', body: { username: 'alice', password: 'test' } })
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/auth/csrf')
  expect(fetcher.mock.calls[1][1]).toMatchObject({
    credentials: 'same-origin',
    headers: { 'X-CSRFToken': 'first', 'Content-Type': 'application/json' },
  })
})
it('multipart leaves boundary creation to the browser and carries idempotency', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(ok({ csrf_token: 'token' }))
    .mockResolvedValueOnce(ok({}))
  vi.stubGlobal('fetch', fetcher)
  const body = new FormData()
  body.append('file', new File(['x'], 'paper.pdf'))
  await request('/projects/1/papers/upload', { method: 'POST', body, key: 'intent' })
  expect(fetcher.mock.calls[1][1].body).toBe(body)
  expect(fetcher.mock.calls[1][1].headers['Content-Type']).toBeUndefined()
  expect(fetcher.mock.calls[1][1].headers['Idempotency-Key']).toBe('intent')
})
it('refresh after login replaces the old CSRF token', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(ok({ csrf_token: 'old' }))
    .mockResolvedValueOnce(ok({ csrf_token: 'new' }))
    .mockResolvedValueOnce(ok({}))
  vi.stubGlobal('fetch', fetcher)
  await refreshCsrf()
  await refreshCsrf()
  await request('/auth/logout', { method: 'POST', body: {} })
  expect(fetcher.mock.calls[2][1].headers['X-CSRFToken']).toBe('new')
})
it('401 clears the session and keeps request_id for troubleshooting', async () => {
  const expired = vi.fn()
  unauthorizedHandler(expired)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(failure(401, 'AUTH_REQUIRED')))
  await expect(request('/projects')).rejects.toMatchObject({
    status: 401,
    requestId: 'req_failure',
  })
  expect(expired).toHaveBeenCalledOnce()
})
it('bad credentials do not trigger global session expiration', async () => {
  const expired = vi.fn()
  unauthorizedHandler(expired)
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(ok({ csrf_token: 'x' }))
      .mockResolvedValueOnce(failure(401, 'INVALID_CREDENTIALS')),
  )
  await expect(request('/auth/login', { method: 'POST', body: {} })).rejects.toBeInstanceOf(
    ApiError,
  )
  expect(expired).not.toHaveBeenCalled()
})
it('binary errors are parsed as JSON instead of downloaded as a Word document', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(failure(409, 'RESULT_NOT_READY')))
  await expect(binary('/api/v1/exports/1/file')).rejects.toMatchObject({
    error: { code: 'RESULT_NOT_READY' },
  })
})
it('404 does not expose a resource owner', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(failure(404, 'NOT_FOUND')))
  await expect(request('/reviews/1')).rejects.toMatchObject({ message: '资源不存在或不可用' })
})
it('does not send to external hosts', async () => {
  const f = vi.fn()
  vi.stubGlobal('fetch', f)
  await expect(request('https://vendor.example')).rejects.toThrow('同源')
  expect(f).not.toHaveBeenCalled()
})
it('network retry reuses the original key and frozen submit closure', async () => {
  const submit = new Submission(),
    keys: string[] = []
  let tries = 0
  const first = (key: string) => {
    keys.push(key)
    if (!tries++)
      return Promise.reject(
        new ApiError(0, {
          code: 'NETWORK_ERROR',
          message: 'uncertain',
          details: {},
          retryable: true,
        }),
      )
    return Promise.resolve('accepted')
  }
  await expect(submit.execute('payload-A', first)).rejects.toThrow('uncertain')
  await expect(submit.execute('payload-B', () => Promise.resolve('wrong'))).rejects.toThrow(
    '上次提交',
  )
  expect(await submit.execute('payload-A', () => Promise.resolve('wrong'))).toBe('accepted')
  expect(keys[1]).toBe(keys[0])
  expect(submit.pending).toBeNull()
})
it('a definite rejection allows a new intent', async () => {
  const submit = new Submission()
  await expect(
    submit.execute('A', () =>
      Promise.reject(
        new ApiError(400, {
          code: 'VALIDATION_ERROR',
          message: 'bad',
          details: {},
          retryable: false,
        }),
      ),
    ),
  ).rejects.toThrow()
  expect(await submit.execute('B', () => Promise.resolve('ok'))).toBe('ok')
})
it('Retry-After accepts seconds or a HTTP date', () => {
  expect(retryDelay('7')).toBe(7000)
  expect(retryDelay('Thu, 01 Oct 2026 00:00:10 GMT', Date.parse('2026-10-01T00:00:00Z'))).toBe(
    10_000,
  )
  expect(retryDelay('invalid')).toBe(0)
})
