<script setup lang="ts">
import { ref } from 'vue'
defineProps<{ disabled: boolean; pending: boolean }>()
const emit = defineEmits<{ upload: [file: File, title: string]; import: [ids: string[]] }>()
const file = ref<File | null>(null),
  title = ref(''),
  ids = ref(''),
  error = ref('')
function select(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null
  error.value = ''
}
function upload() {
  error.value = ''
  if (!file.value) {
    error.value = '请先选择 PDF 文件'
    return
  }
  if (!/\.pdf$/i.test(file.value.name)) {
    error.value = '请选择 PDF 文件'
    return
  }
  if (file.value.size > 25 * 1024 * 1024) {
    error.value = '文件超过 25 MiB 上限'
    return
  }
  emit('upload', file.value, title.value)
}
function importIds() {
  const list = ids.value.split(/[\s,，;；]+/).filter(Boolean)
  error.value = ''
  if (list.length < 1 || list.length > 10) {
    error.value = '每批请输入 1—10 个 arXiv ID'
    return
  }
  emit('import', list)
}
</script>
<template>
  <section class="panel upload-panel">
    <div class="section-heading">
      <h2>收集论文</h2>
      <span class="mono">INPUT / PDF + arXiv</span>
    </div>
    <p class="footnote">每份 PDF ≤ 25 MiB / 50 页；每个项目最多 30 篇。</p>
    <p v-if="error" class="notice danger" role="alert">{{ error }}</p>
    <div class="upload-grid">
      <form @submit.prevent="upload">
        <label for="pdf-file">本地 PDF</label
        ><input
          id="pdf-file"
          type="file"
          accept="application/pdf,.pdf"
          :disabled="disabled || pending"
          @change="select"
        /><label for="paper-title">论文标题（可选）</label
        ><input
          id="paper-title"
          v-model="title"
          maxlength="500"
          :disabled="disabled || pending"
          placeholder="留空则使用文件名"
        /><button :disabled="disabled || pending || !file">
          {{ pending ? '提交中…' : '上传论文 ↑' }}
        </button>
      </form>
      <form @submit.prevent="importIds">
        <label for="arxiv-ids">arXiv ID</label
        ><textarea
          id="arxiv-ids"
          v-model="ids"
          :disabled="disabled || pending"
          rows="3"
          placeholder="例如 2401.12345v2，一行一个"
        ></textarea>
        <p class="footnote">可一次导入 1—10 篇；结果会逐项保留在历史记录。</p>
        <button :disabled="disabled || pending || !ids.trim()">导入 arXiv →</button>
      </form>
    </div>
  </section>
</template>
