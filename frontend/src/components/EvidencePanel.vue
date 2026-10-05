<script setup lang="ts">
import type { Evidence, RunPaper, Paper } from '../types/contracts'
import PdfViewer from './PdfViewer.vue'
import { download } from '../api/resources'
import { useAction } from '../composables/action'
const action = useAction()
defineProps<{
  evidence: Evidence | null
  paper: RunPaper | Paper | null
  fileUrl?: string
  page?: number
  selectable?: boolean
  references?: string[]
}>()
const emit = defineEmits<{ close: []; select: [evidence: Evidence]; page: [number: number] }>()
const verified = {
  text_matched: '文字匹配',
  image_unverified: '图像证据待核对',
  human_verified: '人工核对标记',
}
</script>
<template>
  <aside class="evidence-panel sheet" aria-label="证据与原文">
    <div class="tape"></div>
    <div class="section-heading">
      <h2>原文在这里</h2>
      <button class="quiet" @click="emit('close')">收起</button>
    </div>
    <p class="eyebrow">SOURCE / FIELD NOTES</p>
    <h3>{{ paper?.title ?? '原文证据' }}</h3>
    <p v-if="paper" class="footnote">
      {{ paper.authors.join(' / ') || '作者未记录' }} · {{ paper.year ?? '年份未知' }}
    </p>
    <template v-if="evidence">
      <p class="footnote">
        {{
          references?.length
            ? '关联记录：' + references.join(' / ')
            : '原文片段；可核对后选择引用。'
        }}
      </p>
      <div class="chips">
        <span class="badge">第 {{ evidence.page_number }} 页</span
        ><span class="badge">{{ evidence.element_type }}</span
        ><span
          class="badge"
          :class="evidence.verification === 'image_unverified' ? 'warning' : ''"
          >{{ verified[evidence.verification] }}</span
        >
      </div>
      <blockquote class="evidence-text">{{ evidence.text }}</blockquote>
      <p class="footnote">
        {{ evidence.section || '章节未标注' }} ·
        {{ evidence.locator.granularity === 'page' ? '页级定位' : '区域定位' }}
      </p>
      <p v-if="evidence.verification === 'image_unverified'" class="notice warning">
        请对照 PDF 页面核对转录内容。
      </p>
      <button v-if="selectable" class="primary" @click="emit('select', evidence)">
        把这条证据加入当前块 +
      </button></template
    ><PdfViewer
      v-if="evidence?.file_url || fileUrl"
      :url="evidence?.file_url || fileUrl || ''"
      :page="page ?? evidence?.page_number ?? 1"
      @page="emit('page', $event)"
    />
    <p v-else class="empty">选择一条证据即可查看片段与原文页码。</p>
    <button
      v-if="evidence?.file_url || fileUrl"
      class="quiet"
      :disabled="action.pending.value"
      @click="action.perform(() => download(evidence?.file_url || fileUrl || '', '原文.pdf'))"
    >
      下载原始 PDF ↓
    </button>
    <p v-if="action.error.value" class="notice danger" role="alert">{{ action.error.value }}</p>
  </aside>
</template>
