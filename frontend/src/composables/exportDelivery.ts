import { computed, onScopeDispose, ref } from 'vue'
import { api, download } from '../api/resources'
import { errorMessage } from '../api/client'

// Follow only an explicit export/download intent. Opening history must never
// download every old file; leaving the page cancels pending delivery.
export function useExportDelivery() {
  const phase = ref<'idle' | 'submitting' | 'waiting' | 'downloading' | 'complete' | 'error'>(
    'idle',
  )
  const message = ref(''),
    error = ref('')
  const busy = computed(() => ['submitting', 'waiting', 'downloading'].includes(phase.value))
  let epoch = 0,
    timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let active = true

  function clear() {
    clearTimeout(timer)
    timer = undefined
    controller?.abort()
    epoch++
  }
  function begin() {
    clear()
    phase.value = 'submitting'
    error.value = ''
    message.value = '正在提交导出请求…'
  }
  function fail(reason: string) {
    clear()
    phase.value = 'error'
    message.value = ''
    error.value = reason
  }
  function start(id: string, filename: string) {
    if (!active || ['waiting', 'downloading'].includes(phase.value)) return
    clear()
    controller = new AbortController()
    const token = epoch,
      signal = controller.signal
    phase.value = 'waiting'
    error.value = ''
    message.value = 'Word 正在生成，完成后会自动下载。'
    const tick = async () => {
      try {
        const item = (await api.export(id)).data
        if (!active || token !== epoch) return
        if (item.status === 'pending') {
          timer = setTimeout(() => void tick(), 2000)
          return
        }
        if (item.status !== 'ready' || !item.file_url) {
          fail(
            item.error?.message ??
              (item.status === 'cancelled'
                ? '导出已取消，可重新导出。'
                : 'Word 生成失败，请重试导出。'),
          )
          return
        }
        phase.value = 'downloading'
        message.value = 'Word 已生成，正在下载…'
        await download(item.file_url, filename, signal)
        if (!active || token !== epoch) return
        phase.value = 'complete'
        message.value = 'Word 下载已开始，请查看浏览器的下载记录。需要再次下载时可再点导出按钮。'
      } catch (e) {
        if (active && token === epoch)
          fail(`导出或下载未完成：${errorMessage(e)}。请重试导出；已生成的文件会直接下载。`)
      }
    }
    void tick()
  }
  function submissionFailed(reason: string) {
    if (phase.value === 'submitting') fail(reason)
  }
  onScopeDispose(() => {
    active = false
    clear()
  })
  return { busy, message, error, begin, start, submissionFailed }
}
