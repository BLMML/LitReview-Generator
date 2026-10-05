<script setup lang="ts">
import { clone } from '../clone'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { api, allPages, download } from '../api/resources'
import { useAction } from '../composables/action'
import { useExportDelivery } from '../composables/exportDelivery'
import { useEvidence } from '../composables/evidence'
import { useWorkspace } from '../stores/workspace'
import { draftError, exportAllowed, exportWarnings } from '../reviewRules'
import { ApiError } from '../api/client'
import { draftGuard } from '../composables/draftGuard'
import {
  terminal,
  type Review,
  type ReviewRevision,
  type Validation,
  type Export,
  type Job,
  type Section,
  type Evidence,
} from '../types/contracts'
import ReviewEditor from '../components/ReviewEditor.vue'
import ValidationPanel from '../components/ValidationPanel.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import JobPanel from '../components/JobPanel.vue'
import ErrorNotice from '../components/ErrorNotice.vue'
const route = useRoute(),
  router = useRouter(),
  store = useWorkspace(),
  action = useAction()
const id = String(route.params.id),
  review = ref<Review | null>(null),
  revisions = ref<ReviewRevision[]>([]),
  selected = ref<ReviewRevision | null>(null),
  sections = ref<Section[]>([]),
  original = ref(''),
  reports = ref<Validation[]>([]),
  exports = ref<Export[]>([]),
  jobMap = ref<Record<string, Job>>({}),
  reportId = ref(''),
  acknowledged = ref(false),
  activeBlock = ref(''),
  picker = ref(false),
  loaded = ref(false)
const exportDelivery = useExportDelivery()
const evidence = useEvidence(() => store.run),
  dirty = computed(() => JSON.stringify(sections.value) !== original.value),
  disabled = computed(() => store.busy || action.pending.value)
const relevantReports = computed(() =>
    reports.value.filter((v) => v.revision_id === selected.value?.id),
  ),
  report = computed(
    () =>
      relevantReports.value.find((v) => v.id === reportId.value) ??
      relevantReports.value[0] ??
      null,
  )
const validationJob = computed(() => (report.value ? jobMap.value[report.value.job_id] : undefined))
const sourceLayout = computed(
  () => !!selected.value?.sections.some((s) => s.section_key === 'significance'),
)
const warnings = computed(() =>
    exportWarnings(selected.value, review.value, store.run, report.value),
  ),
  canExport = computed(
    () =>
      sourceLayout.value &&
      exportAllowed(selected.value, report.value, validationJob.value, dirty.value),
  )
async function refresh() {
  const [r, versions, vs, es] = await Promise.all([
    api.review(id),
    allPages((n) => api.revisions(id, n)),
    allPages((n) => api.validations(id, n)),
    allPages((n) => api.exports(id, n)),
  ])
  if (!action.active()) return
  review.value = r.data
  revisions.value = versions
  reports.value = vs
  exports.value = es
  const ids = [
    ...new Set([r.data.generation_job_id, ...vs.map((v) => v.job_id), ...es.map((e) => e.job_id)]),
  ]
  const jobs = await Promise.all(ids.map((id) => api.job(id)))
  jobMap.value = Object.fromEntries(jobs.map((j) => [j.data.id, j.data]))
  if (!selected.value && versions.length)
    setRevision(
      versions.find((v) => v.id === route.query.revision) ?? r.data.latest_revision ?? versions[0],
    )
  else if (selected.value) {
    const updated = versions.find((v) => v.id === selected.value!.id)
    if (updated) selected.value = updated
  }
  loaded.value = true
}
function setRevision(revision: ReviewRevision) {
  selected.value = revision
  sections.value = clone(revision.sections)
  original.value = JSON.stringify(sections.value)
  reportId.value = ''
  acknowledged.value = false
  picker.value = false
  activeBlock.value = ''
  evidence.clear()
  void router.replace({ query: { revision: revision.id } })
}
function selectRevision(id: string) {
  const revision = revisions.value.find((v) => v.id === id)
  if (revision && (!dirty.value || window.confirm('此版本有未保存改动。放弃改动并打开其他版本？')))
    setRevision(revision)
}
async function load() {
  await action.perform(async () => {
    await refresh()
    if (action.active() && review.value) {
      await store.open(review.value.project_id)
      if (store.run?.id !== review.value.run_id) await store.selectRun(review.value.run_id)
    }
  })
}
async function save() {
  const problem = draftError(sections.value)
  if (problem) {
    action.error.value = problem
    return
  }
  if (!selected.value) return
  const body = clone(sections.value),
    base = selected.value.revision_number
  await action.perform(
    () => api.saveRevision(id, base, body),
    async (r) => {
      setRevision(r.data)
      await refresh()
      action.success.value = `已保存正文 v${r.data.revision_number}。需重新核验新版本。`
    },
  )
}
async function task(
  signature: string,
  fn: (key: string) => ReturnType<typeof api.validate>,
  exportFilename?: string,
) {
  return await action.perform(
    () => action.submission.execute(signature, fn),
    async (r) => {
      store.accepted(r.data.job_id)
      if (r.data.resource_type === 'validation_report') reportId.value = r.data.resource_id
      if (r.data.resource_type === 'export')
        exportDelivery.start(r.data.resource_id, exportFilename ?? '综述.docx')
      await refresh()
      action.success.value = '任务已受理。'
    },
  )
}
async function validate() {
  if (!selected.value || dirty.value) return
  const revision = selected.value.id
  await task(`validate:${revision}`, (key) => api.validate(id, revision, key))
}
async function exportWord() {
  if (
    disabled.value ||
    exportDelivery.busy.value ||
    !selected.value ||
    !report.value ||
    !canExport.value ||
    (warnings.value.length && !acknowledged.value)
  )
    return
  const revision = selected.value.id,
    validation = report.value.id,
    ack = acknowledged.value
  const filename = `综述-v${selected.value.revision_number}.docx`
  const existing = exports.value.find(
    (e) =>
      e.revision_id === revision &&
      e.validation_id === validation &&
      ['ready', 'pending'].includes(e.status) &&
      JSON.stringify(e.warnings) === JSON.stringify(warnings.value),
  )
  if (existing && !action.submission.pending) {
    exportDelivery.start(existing.id, filename)
    if (existing.status === 'pending') store.watchJob(existing.job_id)
    return
  }
  exportDelivery.begin()
  const accepted = await task(
    JSON.stringify(['export', revision, validation, ack]),
    (key) => api.createExport(id, revision, validation, ack, key),
    filename,
  )
  if (!accepted && action.active() && action.error.value)
    exportDelivery.submissionFailed(action.error.value)
}
function addEvidence(value: Evidence) {
  if (disabled.value || !activeBlock.value) return
  const copy = clone(sections.value),
    block = copy.flatMap((s) => s.blocks).find((b) => b.block_id === activeBlock.value)
  if (
    !block ||
    block.evidence_ids.length >= (sourceLayout.value ? 1 : 20) ||
    block.evidence_ids.includes(value.id)
  )
    return
  block.evidence_ids.push(value.id)
  sections.value = copy
  action.success.value = '证据已加入草稿，保存后才会形成新版本。'
}
function pick(id: string) {
  activeBlock.value = id
  picker.value = true
  if (!evidence.paperId.value)
    evidence.paperId.value = store.run?.papers.find((p) => p.parsed_document_id)?.paper_id ?? ''
  void evidence.browse()
}
function focusBlock(id: string) {
  activeBlock.value = id
  const block = sections.value.flatMap((s) => s.blocks).find((b) => b.block_id === id)
  document.getElementById(`text-${id}`)?.focus()
  if (block?.evidence_ids[0]) void evidence.show(block.evidence_ids[0])
}
async function cancel(id: string) {
  await action.perform(
    () => api.cancelJob(id),
    () => store.watchJob(id),
  )
}
async function retry(jobId: string) {
  const exported = exports.value.find((e) => e.job_id === jobId)
  const version = revisions.value.find((v) => v.id === exported?.revision_id)?.revision_number
  await task(
    `retry:${jobId}`,
    (key) => api.retryJob(jobId, key),
    exported ? `综述-v${version ?? ''}.docx` : undefined,
  )
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
const releaseDraftGuard = draftGuard(() => dirty.value)
onBeforeRouteLeave(() => !dirty.value || window.confirm('手稿有未保存改动。确定放弃并离开？'))
watch(
  () => [
    selected.value?.id,
    selected.value?.is_stale,
    store.run?.is_stale,
    review.value?.latest_revision?.id,
    report.value?.id,
    report.value?.result,
  ],
  () => {
    acknowledged.value = false
  },
)
watch(
  () => store.job,
  (j) => {
    if (j && terminal(j.status))
      void refresh().catch((e) => {
        action.error.value = e instanceof Error ? e.message : '历史读取失败，请重新读取'
      })
  },
)
onMounted(() => {
  window.addEventListener('beforeunload', beforeUnload)
  void load()
})
onBeforeUnmount(() => {
  releaseDraftGuard()
  store.stop()
  evidence.clear()
  window.removeEventListener('beforeunload', beforeUnload)
})
</script>
<template>
  <div class="page review-page">
    <div class="breadcrumbs">
      <RouterLink to="/projects">项目档案</RouterLink><span>/</span
      ><RouterLink v-if="review" :to="`/projects/${review.project_id}?tab=reviews`"
        >研究工作台</RouterLink
      ><span>/</span><span>综述手稿</span>
    </div>
    <ErrorNotice :message="action.error.value" :request-id="action.requestId.value"
      ><button class="quiet" :disabled="action.pending.value" @click="action.perform(refresh)">
        重新读取服务器版本（保留草稿）
      </button></ErrorNotice
    ><ErrorNotice :message="store.error" />
    <p v-if="action.success.value" class="notice good" role="status">{{ action.success.value }}</p>
    <p v-if="!loaded && !action.error.value" role="status">正在读取手稿与历史…</p>
    <template v-if="review"
      ><div class="page-heading">
        <div>
          <p class="eyebrow">THE WORKING MANUSCRIPT / 04</p>
          <h1>{{ review.title }}</h1>
          <p class="muted">
            固定研究框架 · 目标 {{ review.target_chars }} 字 · 正文采用原文，出处定位到页、段、行。
          </p>
        </div>
        <span class="pencil-note">边写，边核对。</span>
      </div>
      <div v-if="store.run" class="frozen-question">
        <span class="eyebrow">本手稿使用的研究问题</span>
        <p>{{ store.run.research_question }}</p>
      </div>
      <p v-if="store.busy" class="notice warning">
        项目任务进行中，可阅读与查看历史；编辑、新任务暂不可用。
      </p>
      <JobPanel
        v-if="store.job"
        :job="store.job"
        :disabled="action.pending.value"
        :poll-error="store.pollError"
        @cancel="cancel"
        @retry="retry"
        @reconnect="store.watchJob"
      /><JobPanel
        v-else-if="!selected && jobMap[review.generation_job_id]"
        :job="jobMap[review.generation_job_id]"
        :disabled="disabled"
        @cancel="cancel"
        @retry="retry"
      />
      <section v-if="!selected" class="empty-sheet">
        <h2>这份手稿还没有正文</h2>
        <p>生成任务完成后，首个版本会出现在这里。失败原因和可重试状态请查看任务面板。</p>
        <button class="quiet" :disabled="action.pending.value" @click="load">重新读取结果</button>
      </section>
      <template v-if="selected"
        ><div class="editor-toolbar">
          <div>
            <label for="revision-select">正文版本</label
            ><select
              id="revision-select"
              :value="selected.id"
              @change="selectRevision(($event.target as HTMLSelectElement).value)"
            >
              <option v-for="v in revisions" :key="v.id" :value="v.id">
                v{{ v.revision_number }} · {{ v.source === 'manual' ? '人工保存' : '模型生成' }} ·
                {{ new Date(v.created_at).toLocaleString('zh-CN') }}
              </option>
            </select>
          </div>
          <span class="badge" :class="dirty ? 'warning' : 'good'" role="status">{{
            dirty ? '有未保存改动' : '此版本已保存'
          }}</span
          ><button
            class="primary"
            :disabled="disabled || !dirty || selected.id !== review.latest_revision?.id"
            @click="save"
          >
            {{ action.pending.value ? '处理中…' : '保存为新版本 →' }}
          </button>
        </div>
        <p v-if="selected.id !== review.latest_revision?.id" class="notice warning">
          正在查看历史正文 v{{
            selected.revision_number
          }}。可核验或带警示导出；继续写作请打开最新版本。<button
            class="quiet"
            @click="selectRevision(review.latest_revision!.id)"
          >
            打开最新版本
          </button>
        </p>
        <p v-if="selected.is_stale" class="notice warning">
          此正文基于历史分析版本，保存修改不会自动消除过时状态。
        </p>
        <p v-if="!sourceLayout" class="notice warning">
          此手稿使用旧章节结构。请返回项目重新生成综述，再按固定研究框架导出 Word。
        </p>
        <p v-else class="footnote">
          正文须保留所引证据的完整原句或原段，每块对应一条证据。缺失模块只填写“提供的素材中没有该内容。”，不添加其他说明。
        </p>
        <div class="review-layout">
          <div class="manuscript">
            <ReviewEditor
              :sections="sections"
              :disabled="disabled || selected.id !== review.latest_revision?.id"
              :active-block="activeBlock"
              @update="sections = $event"
              @pick="pick"
              @evidence="evidence.show"
            />
          </div>
          <aside class="review-margin">
            <ValidationPanel
              :revision="selected"
              :reports="relevantReports"
              :report="report"
              :job="validationJob"
              :warnings="warnings"
              :acknowledged="acknowledged"
              :can-export="canExport"
              :export-busy="exportDelivery.busy.value"
              :export-message="exportDelivery.message.value"
              :export-error="exportDelivery.error.value"
              :disabled="disabled"
              :dirty="dirty"
              @select="reportId = $event"
              @validate="validate"
              @export="exportWord"
              @acknowledge="acknowledged = $event"
              @block="focusBlock"
            />
            <section v-if="picker && store.run" class="panel evidence-picker">
              <div class="section-heading">
                <h2>为当前块选证据</h2>
                <button class="quiet" @click="picker = false">收起</button>
              </div>
              <p class="footnote">来自这份手稿的 Run；历史语料也可按页回看。</p>
              <form @submit.prevent="evidence.browse()">
                <label for="picker-paper">快照论文</label
                ><select id="picker-paper" v-model="evidence.paperId.value">
                  <option
                    v-for="p in store.run.papers.filter((p) => p.parsed_document_id)"
                    :key="p.paper_id"
                    :value="p.paper_id"
                  >
                    {{ p.title }}
                  </option></select
                ><label for="picker-page">原文页码（可留空）</label
                ><input
                  id="picker-page"
                  v-model="evidence.pageNumber.value"
                  type="number"
                  min="1"
                  :max="
                    store.run.papers.find((p) => p.paper_id === evidence.paperId.value)?.page_count
                  "
                /><button :disabled="!evidence.paperId.value || evidence.loading.value">
                  读取证据
                </button>
              </form>
              <ul class="candidate-list">
                <li v-for="e in evidence.candidates.value" :key="e.id">
                  <p>{{ e.text }}</p>
                  <div class="actions">
                    <button class="cite" @click="evidence.show(e.id)">
                      第 {{ e.page_number }} 页 ↗</button
                    ><button :disabled="disabled" @click="addEvidence(e)">加入此块 +</button>
                  </div>
                </li>
              </ul>
              <p
                v-if="!evidence.loading.value && !evidence.candidates.value.length"
                class="footnote"
              >
                此页暂无片段，可调整页码再读取。
              </p>
            </section>
            <ErrorNotice :message="evidence.error.value" />
            <p v-if="evidence.loading.value" role="status">正在读取证据…</p>
            <EvidencePanel
              v-if="evidence.open.value"
              :evidence="evidence.evidence.value"
              :paper="evidence.sourcePaper.value"
              :page="evidence.page.value"
              :references="
                sections.flatMap((s) =>
                  s.blocks.flatMap((b, i) =>
                    b.evidence_ids.includes(evidence.evidence.value?.id ?? '')
                      ? [`${s.title} · 块 ${i + 1}`]
                      : [],
                  ),
                )
              "
              :selectable="picker && !disabled"
              @close="evidence.open.value = false"
              @page="evidence.page.value = $event"
              @select="addEvidence"
            />
          </aside></div
      ></template>
      <section class="panel export-history">
        <div class="section-heading">
          <h2>历史导出</h2>
          <span class="mono">EXPORT ARCHIVE</span>
        </div>
        <p v-if="!exports.length" class="empty">导出文件会保留准确的正文与核验版本。</p>
        <article v-for="e in exports" :key="e.id" class="export-row">
          <div>
            <h3>
              正文 v{{ revisions.find((v) => v.id === e.revision_id)?.revision_number ?? '?' }} /
              Word
            </h3>
            <p class="footnote">
              {{ new Date(e.created_at).toLocaleString('zh-CN') }} ·
              {{
                { pending: '等待导出', ready: '可下载', failed: '失败', cancelled: '已取消' }[
                  e.status
                ]
              }}
            </p>
            <ul class="small">
              <li v-for="w in e.warnings" :key="w">{{ w }}</li>
            </ul>
            <p v-if="jobMap[e.job_id]?.error" class="danger-text">
              {{ jobMap[e.job_id].error?.message }}
            </p>
            <details>
              <summary>核验绑定与任务</summary>
              <p class="mono small">
                正文 {{ e.revision_id }}<br />核验 {{ e.validation_id }}<br />任务 {{ e.job_id }}
              </p>
            </details>
          </div>
          <div class="actions">
            <button
              v-if="e.status === 'ready' && e.file_url"
              @click="
                action.perform(() =>
                  download(
                    e.file_url!,
                    `综述-v${revisions.find((v) => v.id === e.revision_id)?.revision_number ?? ''}.docx`,
                  ),
                )
              "
            >
              下载 Word ↓</button
            ><button
              v-if="
                (e.status === 'failed' && jobMap[e.job_id]?.error?.retryable) ||
                e.status === 'cancelled'
              "
              :disabled="disabled"
              @click="retry(e.job_id)"
            >
              重试导出
            </button>
          </div>
        </article>
        <p class="footnote">重试若出现新的警示，请选回该正文版本，阅读警示后创建一次新导出。</p>
      </section></template
    >
  </div>
</template>
