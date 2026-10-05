<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { api } from '../api/resources'
import { errorMessage } from '../api/client'
GlobalWorkerOptions.workerSrc = workerUrl
const props = defineProps<{ url: string; page: number }>()
const canvas = ref<HTMLCanvasElement | null>(null),
  page = ref(1),
  total = ref(0),
  loading = ref(false),
  error = ref(''),
  wrapper = ref<HTMLElement | null>(null)
let pdf: PDFDocumentProxy | null = null,
  render: RenderTask | null = null,
  controller: AbortController | null = null,
  token = 0,
  renderToken = 0
const emit = defineEmits<{ page: [number: number] }>()
async function draw() {
  const ticket = ++renderToken
  render?.cancel()
  if (!pdf) return
  try {
    const p = await pdf.getPage(page.value)
    if (ticket !== renderToken) return
    await nextTick()
    const c = canvas.value
    if (!c) return
    const base = p.getViewport({ scale: 1 }),
      width = Math.min(wrapper.value?.clientWidth ?? 640, 900),
      ratio = window.devicePixelRatio || 1
    const view = p.getViewport({ scale: (width - 16) / base.width })
    c.width = Math.floor(view.width * ratio)
    c.height = Math.floor(view.height * ratio)
    c.style.width = `${view.width}px`
    c.style.height = `${view.height}px`
    const context = c.getContext('2d')
    if (!context) return
    render = p.render({
      canvas: c,
      canvasContext: context,
      viewport: view,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    })
    await render.promise
  } catch (e) {
    if (ticket === renderToken && !(e instanceof Error && e.name === 'RenderingCancelledException'))
      error.value = 'PDF 页面渲染失败，可打开原文件或重试。'
  }
}
async function load() {
  const ticket = ++token
  controller?.abort()
  render?.cancel()
  renderToken++
  const previous = pdf
  pdf = null
  total.value = 0
  error.value = ''
  loading.value = true
  if (previous) await previous.loadingTask.destroy()
  if (ticket !== token) return
  controller = new AbortController()
  try {
    const blob = await api.file(props.url, controller.signal)
    if (ticket !== token) return
    if (!blob.type.includes('pdf')) throw new Error('服务未返回 PDF 文件')
    const task = getDocument({ data: new Uint8Array(await blob.arrayBuffer()) })
    const document = await task.promise
    if (ticket !== token) {
      await document.loadingTask.destroy()
      return
    }
    pdf = document
    total.value = pdf.numPages
    page.value = Math.max(1, Math.min(props.page, total.value))
    await draw()
  } catch (e) {
    if (ticket === token) error.value = errorMessage(e)
  } finally {
    if (ticket === token) loading.value = false
  }
}
function go(value: number) {
  page.value = Math.max(1, Math.min(Number.isFinite(value) ? Math.trunc(value) : 1, total.value))
  emit('page', page.value)
  void draw()
}
watch(() => props.url, load, { immediate: true })
watch(
  () => props.page,
  (value) => {
    if (pdf && value !== page.value) go(value)
  },
)
let observer: ResizeObserver | null = null
watch(wrapper, (value) => {
  observer?.disconnect()
  if (value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => {
      if (!loading.value) void draw()
    })
    observer.observe(value)
  }
})
onBeforeUnmount(() => {
  token++
  renderToken++
  controller?.abort()
  render?.cancel()
  observer?.disconnect()
  void pdf?.loadingTask.destroy()
})
</script>
<template>
  <section class="pdf-viewer">
    <div class="pdf-toolbar">
      <button :disabled="loading || page <= 1" aria-label="上一页" @click="go(page - 1)">←</button
      ><label
        >第
        <input
          :value="page"
          type="number"
          min="1"
          :max="total || 1"
          :disabled="loading || !total"
          aria-label="原文页码"
          @change="go(Number(($event.target as HTMLInputElement).value))"
        />
        / {{ total || '…' }} 页</label
      ><button :disabled="loading || page >= total" aria-label="下一页" @click="go(page + 1)">
        →
      </button>
    </div>
    <p class="footnote">定位到原文页面；当前不提供句子高亮。</p>
    <p v-if="loading" role="status">正在加载完整 PDF…</p>
    <div v-if="error" class="notice danger" role="alert">
      <p>{{ error }}</p>
      <button @click="load">重新加载 PDF</button>
    </div>
    <div ref="wrapper" class="pdf-canvas">
      <canvas
        v-show="!loading && !error && total > 0"
        ref="canvas"
        :aria-label="`原文第 ${page} 页`"
      ></canvas>
    </div>
  </section>
</template>
