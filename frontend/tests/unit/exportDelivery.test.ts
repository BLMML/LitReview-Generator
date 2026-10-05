import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
import { api, download } from '../../src/api/resources'
import { useExportDelivery } from '../../src/composables/exportDelivery'
import type { Export } from '../../src/types/contracts'

vi.mock('../../src/api/resources', () => ({ api: { export: vi.fn() }, download: vi.fn() }))
const getExport = vi.mocked(api.export),
  saveFile = vi.mocked(download)
const item = (status: Export['status']) => ({
  data: {
    id: 'export-1',
    status,
    file_url: status === 'ready' ? '/api/v1/exports/export-1/file' : null,
  } as Export,
  meta: { request_id: 'req_export' },
})
const scopes: ReturnType<typeof effectScope>[] = []
function setup() {
  const scope = effectScope()
  scopes.push(scope)
  return scope.run(useExportDelivery)!
}
async function settle() {
  for (let i = 0; i < 8; i++) await Promise.resolve()
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  saveFile.mockResolvedValue(undefined)
})
afterEach(() => {
  scopes.splice(0).forEach((s) => s.stop())
  vi.useRealTimers()
})

it('waits for file readiness and downloads the exact frozen version once', async () => {
  getExport.mockResolvedValueOnce(item('pending')).mockResolvedValueOnce(item('ready'))
  const delivery = setup()
  delivery.begin()
  delivery.start('export-1', '综述-v2.docx')
  await settle()
  expect(delivery.busy.value).toBe(true)
  expect(delivery.message.value).toContain('完成后会自动下载')
  expect(saveFile).not.toHaveBeenCalled()
  // Repeated clicks while waiting do not create a second poll/download loop.
  delivery.start('export-1', 'wrong-version.docx')
  await vi.advanceTimersByTimeAsync(2000)
  expect(saveFile).toHaveBeenCalledExactlyOnceWith(
    '/api/v1/exports/export-1/file',
    '综述-v2.docx',
    expect.any(AbortSignal),
  )
  expect(delivery.busy.value).toBe(false)
  await vi.advanceTimersByTimeAsync(10000)
  expect(saveFile).toHaveBeenCalledTimes(1)
})

it('opening history does not download any files until a user requests one', async () => {
  const delivery = setup()
  await vi.advanceTimersByTimeAsync(10000)
  expect(getExport).not.toHaveBeenCalled()
  expect(saveFile).not.toHaveBeenCalled()
  getExport.mockResolvedValue(item('ready'))
  delivery.start('export-1', '综述-v1.docx')
  await settle()
  expect(saveFile).toHaveBeenCalledTimes(1)
  expect(delivery.message.value).toContain('下载已开始')
})

it('shows download errors and permits retrying the existing generated file', async () => {
  getExport.mockResolvedValue(item('ready'))
  saveFile.mockRejectedValueOnce(new Error('连接中断'))
  const delivery = setup()
  delivery.start('export-1', '综述-v1.docx')
  await settle()
  expect(delivery.busy.value).toBe(false)
  expect(delivery.error.value).toContain('连接中断')
  delivery.start('export-1', '综述-v1.docx')
  await settle()
  expect(delivery.error.value).toBe('')
  expect(saveFile).toHaveBeenCalledTimes(2)
})

it.each(['failed', 'cancelled'] as const)(
  'reports a %s export and never downloads it',
  async (status) => {
    getExport.mockResolvedValue(item(status))
    const delivery = setup()
    delivery.start('export-1', '综述-v1.docx')
    await settle()
    expect(delivery.error.value).not.toBe('')
    expect(delivery.busy.value).toBe(false)
    expect(saveFile).not.toHaveBeenCalled()
  },
)

it('cancels pending delivery when the user leaves the page', async () => {
  let resolve!: (value: ReturnType<typeof item>) => void
  getExport.mockReturnValue(
    new Promise((done) => {
      resolve = done
    }),
  )
  const delivery = setup()
  delivery.start('export-1', '综述-v1.docx')
  scopes[0].stop()
  resolve(item('ready'))
  await settle()
  expect(saveFile).not.toHaveBeenCalled()
})

it('a history refresh failure does not cancel an already accepted export', async () => {
  getExport.mockResolvedValueOnce(item('pending')).mockResolvedValueOnce(item('ready'))
  const delivery = setup()
  delivery.begin()
  delivery.start('export-1', '综述-v1.docx')
  delivery.submissionFailed('历史暂时无法读取')
  await settle()
  await vi.advanceTimersByTimeAsync(2000)
  expect(saveFile).toHaveBeenCalledTimes(1)
  expect(delivery.error.value).toBe('')
})

it('a rejected submission shows the reason beside the export control', () => {
  const delivery = setup()
  delivery.begin()
  delivery.submissionFailed('导出请求 API 失败')
  expect(delivery.busy.value).toBe(false)
  expect(delivery.error.value).toContain('API 失败')
})
