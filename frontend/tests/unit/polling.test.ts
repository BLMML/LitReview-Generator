import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useWorkspace } from '../../src/stores/workspace'
import { api } from '../../src/api/resources'
import { ApiError } from '../../src/api/client'
import type { Job } from '../../src/types/contracts'
vi.mock('../../src/api/resources', () => ({ api: { job: vi.fn() }, allPages: vi.fn() }))
const job = { id: 'j1', status: 'running' } as Job
beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
})
afterEach(() => {
  vi.useRealTimers()
  vi.resetAllMocks()
})
it('does not overlap in-flight job requests and stops after leaving the route', async () => {
  let resolve!: (value: { data: Job; meta: { request_id: string } }) => void
  vi.mocked(api.job).mockReturnValue(
    new Promise((r) => {
      resolve = r
    }) as ReturnType<typeof api.job>,
  )
  const store = useWorkspace()
  store.watchJob('j1')
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.job).toHaveBeenCalledTimes(1)
  resolve({ data: job, meta: { request_id: 'x' } })
  await vi.advanceTimersByTimeAsync(0)
  await vi.advanceTimersByTimeAsync(2000)
  expect(api.job).toHaveBeenCalledTimes(2)
  store.stop()
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.job).toHaveBeenCalledTimes(2)
})
it('hidden tabs slow polling to ten seconds', async () => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: true })
  vi.mocked(api.job).mockResolvedValue({ data: job, meta: { request_id: 'x' } })
  const store = useWorkspace()
  store.watchJob('j1')
  await vi.advanceTimersByTimeAsync(0)
  await vi.advanceTimersByTimeAsync(9999)
  expect(api.job).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(api.job).toHaveBeenCalledTimes(2)
  store.stop()
})
it('respects Retry-After and does not mislabel a network failure as a failed job', async () => {
  vi.mocked(api.job)
    .mockRejectedValueOnce(
      new ApiError(
        429,
        { code: 'RATE_LIMITED', message: 'wait', details: {}, retryable: true },
        '',
        20_000,
      ),
    )
    .mockResolvedValue({ data: job, meta: { request_id: 'x' } })
  const store = useWorkspace()
  store.watchJob('j1')
  await vi.advanceTimersByTimeAsync(0)
  expect(store.job).toBeNull()
  expect(store.pollError).toContain('服务器')
  await vi.advanceTimersByTimeAsync(19_999)
  expect(api.job).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(store.job?.status).toBe('running')
  store.stop()
})
it('cancelling continues polling until the server returns cancelled', async () => {
  vi.mocked(api.job)
    .mockResolvedValueOnce({ data: { ...job, status: 'cancelling' }, meta: { request_id: 'x' } })
    .mockResolvedValue({ data: { ...job, status: 'cancelled' }, meta: { request_id: 'x' } })
  const store = useWorkspace()
  store.watchJob('j1')
  await vi.advanceTimersByTimeAsync(0)
  expect(store.job?.status).toBe('cancelling')
  await vi.advanceTimersByTimeAsync(2000)
  expect(store.job?.status).toBe('cancelled')
  await vi.advanceTimersByTimeAsync(10_000)
  expect(api.job).toHaveBeenCalledTimes(2)
  store.stop()
})
