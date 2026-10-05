<script setup lang="ts">
import { nextTick, ref } from 'vue'
import type { Paper, PaperPatch, RunPaper } from '../types/contracts'
const props = defineProps<{ papers: Paper[]; disabled: boolean; runPapers?: RunPaper[] }>()
const emit = defineEmits<{
  save: [id: string, body: PaperPatch]
  source: [paper: Paper]
  failure: []
  remove: [paper: Paper]
}>()
const editId = ref(''),
  title = ref(''),
  authors = ref(''),
  year = ref<number | ''>('')
const deleteDialog = ref<HTMLDialogElement | null>(null)
const deleteTarget = ref<Paper | null>(null)
async function askDelete(paper: Paper) {
  if (props.disabled) return
  deleteTarget.value = { ...paper }
  await nextTick()
  deleteDialog.value?.showModal()
}
function confirmDelete() {
  if (props.disabled || !deleteTarget.value) return
  const paper = deleteTarget.value
  deleteDialog.value?.close()
  deleteTarget.value = null
  emit('remove', paper)
}
function edit(p: Paper) {
  editId.value = p.id
  title.value = p.title
  authors.value = p.authors.join('; ')
  year.value = p.year ?? ''
}
function save(p: Paper) {
  emit('save', p.id, {
    expected_version: p.version,
    title: title.value,
    authors: authors.value
      .split(/[;；]/)
      .map((x) => x.trim())
      .filter(Boolean),
    year: year.value === '' ? null : Number(year.value),
  })
}
</script>
<template>
  <section class="panel">
    <div class="section-heading">
      <h2>论文账本</h2>
      <span class="mono"
        >{{ papers.filter((p) => p.included).length }} / {{ papers.length }} 篇纳入</span
      >
    </div>
    <p v-if="!papers.length" class="empty">还没有论文。上传 PDF 或导入 arXiv，开始建立语料。</p>
    <ol class="paper-list">
      <li v-for="(p, i) in papers" :key="p.id">
        <div class="paper-number mono">{{ String(i + 1).padStart(2, '0') }}</div>
        <div class="paper-content">
          <h3>{{ p.title }}</h3>
          <div v-if="runPapers?.find((r) => r.paper_id === p.id)?.error" class="notice danger">
            <p>
              选中快照的解析失败：{{ runPapers?.find((r) => r.paper_id === p.id)?.error?.message }}
            </p>
            <button class="quiet" @click="emit('failure')">查看分析账本 →</button>
          </div>
          <p class="footnote">
            {{ p.authors.join(' / ') || '作者待补充' }} · {{ p.year ?? '年份未知' }} ·
            {{ p.page_count }} 页
          </p>
          <div class="chips">
            <span class="badge">{{
              p.source === 'arxiv' ? `arXiv ${p.source_id}` : '本地 PDF'
            }}</span
            ><span class="badge" :class="p.included ? 'good' : ''">{{
              p.included ? '已纳入' : '未纳入'
            }}</span
            ><span class="mono small">元数据 v{{ p.version }}</span>
          </div>
          <div class="actions">
            <button class="quiet" @click="emit('source', p)">阅读原文 ↗</button
            ><button class="quiet" :disabled="disabled" @click="edit(p)">编辑元数据</button
            ><button
              class="quiet"
              :disabled="disabled"
              @click="emit('save', p.id, { expected_version: p.version, included: !p.included })"
            >
              {{ p.included ? '取消纳入' : '纳入分析' }}
            </button>
            <button class="quiet danger-action" :disabled="disabled" @click="askDelete(p)">
              删除论文
            </button>
          </div>
          <form v-if="editId === p.id" class="inline-form" @submit.prevent="save(p)">
            <label :for="`pt-${p.id}`">标题</label
            ><input
              :id="`pt-${p.id}`"
              v-model="title"
              required
              maxlength="500"
              :disabled="disabled"
            /><label :for="`pa-${p.id}`">作者（分号分隔）</label
            ><input :id="`pa-${p.id}`" v-model="authors" :disabled="disabled" /><label
              :for="`py-${p.id}`"
              >年份</label
            ><input
              :id="`py-${p.id}`"
              v-model="year"
              type="number"
              min="1000"
              :disabled="disabled"
            />
            <div class="actions">
              <button :disabled="disabled">保存元数据</button
              ><button type="button" class="quiet" @click="editId = ''">收起</button>
            </div>
            <p class="footnote">冲突时草稿会保留，请重新读取论文后核对版本。</p>
          </form>
        </div>
      </li>
    </ol>
    <dialog
      ref="deleteDialog"
      class="paper-delete-dialog"
      aria-labelledby="paper-delete-title"
      aria-describedby="paper-delete-description"
      @close="deleteTarget = null"
    >
      <template v-if="deleteTarget">
        <span class="mono small">文献账本 / 删除确认</span>
        <h2 id="paper-delete-title">删除这篇论文？</h2>
        <p class="delete-paper-name">{{ deleteTarget.title }}</p>
        <p id="paper-delete-description">
          删除后将移出当前账本，不再参与后续分析。已有分析和综述的原文引用会保留。
          重新上传同一文件可以恢复。
        </p>
        <div class="actions">
          <button type="button" autofocus @click="deleteDialog?.close()">保留论文</button>
          <button type="button" class="danger-action" :disabled="disabled" @click="confirmDelete">
            确认删除
          </button>
        </div>
      </template>
    </dialog>
  </section>
</template>
