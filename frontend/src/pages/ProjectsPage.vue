<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/resources'
import { useAction } from '../composables/action'
import type { Project } from '../types/contracts'
import ErrorNotice from '../components/ErrorNotice.vue'
const projects = ref<Project[]>([]),
  page = ref(1),
  total = ref(0),
  title = ref(''),
  question = ref(''),
  creating = ref(false),
  loaded = ref(false),
  action = useAction(),
  router = useRouter()
const deleteDialog = ref<HTMLDialogElement | null>(null),
  deleteTarget = ref<Project | null>(null)
async function readProjects() {
  const r = await api.projects(page.value)
  projects.value = r.data
  total.value = r.meta.total ?? r.data.length
  loaded.value = true
}
async function load() {
  await action.perform(readProjects)
}
async function askDelete(project: Project) {
  if (action.pending.value || project.active_job_id) return
  deleteTarget.value = { ...project }
  await nextTick()
  deleteDialog.value?.showModal()
}
async function confirmDelete() {
  if (!deleteTarget.value || action.pending.value) return
  const project = deleteTarget.value
  await action.perform(
    () => api.deleteProject(project.id, project.version, project.corpus_revision),
    async () => {
      projects.value = projects.value.filter((p) => p.id !== project.id)
      total.value = Math.max(0, total.value - 1)
      page.value = Math.min(page.value, Math.max(1, Math.ceil(total.value / 50)))
      action.success.value = '项目已删除，已从项目档案移除。'
      await readProjects()
    },
  )
  deleteDialog.value?.close()
}
async function create() {
  const body = {
    title: title.value.trim(),
    research_question: question.value.trim(),
    output_language: 'zh-CN' as const,
  }
  await action.perform(
    () => api.createProject(body),
    (r) => router.push(`/projects/${r.data.id}`).then(() => {}),
  )
}
onMounted(load)
function changePage(delta: number) {
  page.value += delta
  void load()
}
</script>
<template>
  <div class="page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">THE RESEARCH ARCHIVE / 01</p>
        <h1>项目档案<span class="pencil-note">一份问题，一本手稿。</span></h1>
        <p class="muted">从一个明确的研究问题开始，逐步建立你的文献综述。</p>
      </div>
      <button class="primary" @click="creating = !creating">
        {{ creating ? '收起创建表单' : '+ 新建研究项目' }}
      </button>
    </div>
    <ErrorNotice :message="action.error.value" :request-id="action.requestId.value"
      ><button class="quiet" @click="load">重新读取项目</button></ErrorNotice
    >
    <p v-if="action.success.value" class="notice good" role="status">{{ action.success.value }}</p>
    <section v-if="creating" class="sheet create-sheet">
      <p class="eyebrow">NEW NOTEBOOK</p>
      <h2>给研究一个起点</h2>
      <form @submit.prevent="create">
        <label for="title">项目名称</label
        ><input
          id="title"
          v-model="title"
          required
          maxlength="120"
          placeholder="例如：检索增强生成的可靠性"
        /><label for="question">研究问题</label
        ><textarea
          id="question"
          v-model="question"
          required
          minlength="5"
          maxlength="2000"
          rows="3"
          placeholder="你希望通过这些论文回答什么问题？"
        ></textarea>
        <p class="footnote">输出语言：中文。研究问题修改后，已有分析会保留为历史快照。</p>
        <button class="primary" :disabled="action.pending.value">
          {{ action.pending.value ? '创建中…' : '创建项目 →' }}
        </button>
      </form>
    </section>
    <div class="archive-meta">
      <span>{{ total }} 本项目手稿</span><span>私有档案 / 最新更新优先</span>
    </div>
    <p v-if="!loaded && !action.error.value" role="status" class="empty">正在打开档案…</p>
    <section v-if="loaded && !projects.length" class="empty-sheet">
      <span class="empty-number">00</span>
      <h2>第一本手稿，还等着你</h2>
      <p>新建项目，再上传至少两篇论文，就能开始整理主题与证据。</p>
      <button class="quiet" @click="creating = true">填写研究问题 →</button>
    </section>
    <div class="project-grid">
      <article v-for="(p, i) in projects" :key="p.id" class="project-sheet sheet">
        <RouterLink :to="`/projects/${p.id}`" class="project-link"
          ><div class="project-top">
            <span class="mono">{{ String((page - 1) * 50 + i + 1).padStart(2, '0') }}</span
            ><span class="badge" :class="p.active_job_id ? 'warning' : ''">{{
              p.active_job_id ? '任务进行中' : p.latest_run_id ? '已有分析' : '待整理文献'
            }}</span>
          </div>
          <h2>{{ p.title }}</h2>
          <p>{{ p.research_question }}</p>
        </RouterLink>
        <div class="project-bottom">
          <span
            >{{ new Date(p.updated_at).toLocaleDateString('zh-CN') }} · 语料 v{{
              p.corpus_revision
            }}</span
          >
          <div class="project-actions">
            <RouterLink :to="`/projects/${p.id}`">打开手稿 ↗</RouterLink>
            <button
              class="quiet danger-action"
              :disabled="action.pending.value || !!p.active_job_id"
              :title="p.active_job_id ? '项目任务进行中，请等待结束或先取消任务' : '删除整个项目'"
              @click="askDelete(p)"
            >
              删除项目
            </button>
          </div>
        </div>
      </article>
    </div>
    <dialog
      ref="deleteDialog"
      class="project-delete-dialog"
      aria-labelledby="project-delete-title"
      @close="deleteTarget = null"
      @cancel="action.pending.value && $event.preventDefault()"
    >
      <template v-if="deleteTarget">
        <span class="mono small">项目档案 / 删除确认</span>
        <h2 id="project-delete-title">删除这个项目？</h2>
        <p class="delete-project-name">{{ deleteTarget.title }}</p>
        <p>
          删除后，整个项目将从工作台移除。该项目的论文、分析、综述手稿和导出文件将无法再通过工作台或旧链接访问。
        </p>
        <p class="footnote">
          需要的文件请先下载。系统保留底层文件与费用记录，当前界面不提供项目恢复。
        </p>
        <div class="actions">
          <button
            type="button"
            autofocus
            :disabled="action.pending.value"
            @click="deleteDialog?.close()"
          >
            保留项目
          </button>
          <button
            type="button"
            class="danger-action"
            :disabled="action.pending.value"
            @click="confirmDelete"
          >
            {{ action.pending.value ? '删除中…' : '确认删除项目' }}
          </button>
        </div>
      </template>
    </dialog>
    <div v-if="total > 50" class="pagination">
      <button :disabled="page === 1 || action.pending.value" @click="changePage(-1)">上一页</button
      ><span>第 {{ page }} 页</span
      ><button :disabled="page * 50 >= total || action.pending.value" @click="changePage(1)">
        下一页
      </button>
    </div>
  </div>
</template>
