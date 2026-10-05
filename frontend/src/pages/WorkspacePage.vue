<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/resources'
import { useWorkspace } from '../stores/workspace'
import { useAction } from '../composables/action'
import { useEvidence } from '../composables/evidence'
import type { Dispute, Paper, PaperAnalysis, PaperPatch } from '../types/contracts'
import UploadPanel from '../components/UploadPanel.vue'
import PaperList from '../components/PaperList.vue'
import AnalysisPanel from '../components/AnalysisPanel.vue'
import SearchPanel from '../components/SearchPanel.vue'
import EvidencePanel from '../components/EvidencePanel.vue'
import JobPanel from '../components/JobPanel.vue'
import ErrorNotice from '../components/ErrorNotice.vue'
const route = useRoute(),
  router = useRouter(),
  store = useWorkspace(),
  action = useAction(),
  evidence = useEvidence(() => store.run)
const tab = ref('papers'),
  analysis = ref<PaperAnalysis | null>(null),
  editProject = ref(false),
  title = ref(''),
  question = ref(''),
  expected = ref(1),
  reviewTitle = ref(''),
  target = ref(3000)
const disabled = computed(() => store.busy || action.pending.value)
const successNotice = computed(() => {
  if (
    action.success.value === '任务已受理，可在历史记录查看进度。' &&
    store.job &&
    ['succeeded', 'failed', 'cancelled'].includes(store.job.status)
  )
    return ''
  return action.success.value
})
const tabs = [
  { id: 'papers', label: '01 / 文献' },
  { id: 'analysis', label: '02 / 分析' },
  { id: 'search', label: '03 / 证据' },
  { id: 'reviews', label: '04 / 手稿' },
  { id: 'jobs', label: '历史记录' },
]
async function load() {
  await store.open(String(route.params.id))
  const r = String(route.query.run ?? '')
  if (r && r !== store.run?.id) await action.perform(() => store.selectRun(r))
  if (route.query.tab && tabs.some((t) => t.id === route.query.tab))
    tab.value = String(route.query.tab)
}
async function selectRun(id: string) {
  await action.perform(
    () => store.selectRun(id),
    () => router.replace({ query: { ...route.query, run: id } }).then(() => {}),
  )
}
function changeTab(id: string) {
  tab.value = id
  void router.replace({ query: { ...route.query, tab: id } })
}
async function task(signature: string, fn: (key: string) => ReturnType<typeof api.createRun>) {
  await action.perform(
    () => action.submission.execute(signature, fn),
    async (r) => {
      store.accepted(r.data.job_id)
      await store.refresh()
      if (r.data.resource_type === 'analysis_run') {
        await store.selectRun(r.data.resource_id)
        await router.replace({ query: { ...route.query, run: r.data.resource_id } })
      }
      action.success.value = '任务已受理，可在历史记录查看进度。'
    },
  )
}
async function upload(file: File, name: string) {
  const id = store.project!.id
  await action.perform(
    () =>
      action.submission.execute(
        `upload:${id}:${file.name}:${file.size}:${file.lastModified}:${name}`,
        (key) => api.upload(id, file, name, key),
      ),
    async () => {
      action.success.value = '论文已上传，重复文件会复用已有记录。'
      await store.refresh()
    },
  )
}
async function importArxiv(ids: string[]) {
  const id = store.project!.id
  await task(JSON.stringify(['import', id, ids]), (key) => api.importArxiv(id, ids, key))
}
async function savePaper(id: string, body: PaperPatch) {
  await action.perform(
    () => api.patchPaper(id, body),
    async () => {
      await store.refresh()
      action.success.value = '论文元数据已保存。'
    },
  )
}
async function deletePaper(paper: Paper) {
  await action.perform(
    () => api.deletePaper(paper.id, paper.version),
    async (result) => {
      // Only remove after server acceptance; a failed refresh must not restore
      // the deleted row or make the old Run appear current.
      store.papers = store.papers.filter((p) => p.id !== paper.id)
      if (store.project) store.project.corpus_revision = result.data.corpus_revision
      if (store.run) store.run.is_stale = store.run.corpus_revision !== result.data.corpus_revision
      if (analysis.value?.paper_id === paper.id) analysis.value = null
      if (evidence.fileUrl.value === paper.file_url) evidence.clear()
      await store.refresh()
      action.success.value = '论文已从当前账本删除，历史分析与引用已保留。'
    },
  )
}
async function loadAnalysis(id: string) {
  const run = store.run
  if (run)
    await action.perform(
      () => api.analysis(run.id, id),
      (r) => {
        if (store.run?.id === run.id) analysis.value = r.data
      },
    )
}
async function browse(id: string) {
  evidence.pageNumber.value = ''
  changeTab('search')
  await evidence.browse(id)
}
async function dispute(d: Dispute, status: Dispute['review_status'], note: string) {
  await action.perform(
    () => api.patchDispute(d.id, d.version, status, note),
    async () => {
      await store.refresh()
      action.success.value = '争议复核已保存，基于旧分析的手稿将提示过时。'
    },
  )
}
function beginEdit() {
  if (!store.project) return
  title.value = store.project.title
  question.value = store.project.research_question
  expected.value = store.project.version
  editProject.value = true
}
async function saveProject() {
  const id = store.project!.id
  await action.perform(
    () =>
      api.patchProject(id, {
        expected_version: expected.value,
        title: title.value,
        research_question: question.value,
      }),
    async () => {
      editProject.value = false
      await store.refresh()
      action.success.value = '项目已更新，已有 Run 保留原研究问题。'
    },
  )
}
async function generate() {
  const id = store.run!.id,
    body = {
      title: reviewTitle.value.trim() || store.project!.title,
      target_chars: Number(target.value),
      style: 'standard' as const,
    }
  await task(JSON.stringify(['generate', id, body]), (key) => api.createReview(id, body, key))
}
async function cancel(id: string) {
  await action.perform(
    () => api.cancelJob(id),
    () => store.watchJob(id),
  )
}
async function retry(id: string) {
  await task(`retry:${id}`, (key) => api.retryJob(id, key))
}
watch(
  () => store.run?.id,
  () => {
    analysis.value = null
    evidence.clear()
  },
)
onMounted(load)
onBeforeUnmount(() => {
  store.stop()
  evidence.clear()
})
</script>
<template>
  <div class="page workspace-page">
    <div class="breadcrumbs">
      <RouterLink to="/projects">项目档案</RouterLink><span>/</span><span>研究工作台</span>
    </div>
    <ErrorNotice :message="store.error"><button @click="load">重新打开</button></ErrorNotice>
    <p v-if="store.loading" role="status">正在打开工作手稿…</p>
    <template v-if="store.project"
      ><div class="page-heading">
        <div>
          <p class="eyebrow">RESEARCH NOTEBOOK / 02</p>
          <h1>{{ store.project.title }}</h1>
          <p class="workspace-question">{{ store.project.research_question }}</p>
        </div>
        <div class="heading-note">
          <span class="stamp">语料 v{{ store.project.corpus_revision }}</span
          ><button class="quiet" :disabled="disabled" @click="beginEdit">修改项目问题</button>
        </div>
      </div>
      <form v-if="editProject" class="sheet inline-form" @submit.prevent="saveProject">
        <label for="project-title">项目名称</label
        ><input
          id="project-title"
          v-model="title"
          required
          maxlength="120"
          :disabled="disabled"
        /><label for="project-question">研究问题</label
        ><textarea
          id="project-question"
          v-model="question"
          required
          minlength="5"
          maxlength="2000"
          rows="3"
          :disabled="disabled"
        ></textarea>
        <div class="actions">
          <button :disabled="disabled">保存项目</button
          ><button type="button" class="quiet" @click="editProject = false">收起</button>
        </div>
      </form>
      <ErrorNotice :message="action.error.value" :request-id="action.requestId.value" />
      <p v-if="successNotice" class="notice good" role="status">
        {{ successNotice }}
      </p>
      <p v-if="store.busy" class="notice warning">
        项目有进行中的任务，资料修改和新任务暂不可用。仍可阅读原文和取消任务。
      </p>
      <div class="workbench">
        <aside class="workspace-rail">
          <p class="eyebrow">CONTENTS</p>
          <nav aria-label="工作台内容">
            <button
              v-for="t in tabs"
              :key="t.id"
              :class="{ active: tab === t.id }"
              :aria-current="tab === t.id ? 'page' : undefined"
              @click="changeTab(t.id)"
            >
              {{ t.label }}
            </button>
          </nav>
          <div class="rail-note">
            <span class="pencil-note">先读，再写。</span>
            <p>文献 → 分析 → 证据 →<br />手稿 → 核验 → 导出</p>
          </div>
          <label for="run-history">分析历史</label
          ><select
            id="run-history"
            :value="store.run?.id ?? ''"
            :disabled="store.runLoading || action.pending.value"
            @change="selectRun(($event.target as HTMLSelectElement).value)"
          >
            <option value="" disabled>尚无分析</option>
            <option v-for="(r, i) in store.runs" :key="r.id" :value="r.id">
              {{ store.runs.length - i }} · 语料 v{{ r.corpus_revision }}
              {{ r.is_stale ? '/ 已过时' : '' }}
            </option></select
          ><button
            :disabled="
              disabled ||
              store.papers.filter((p) => p.included).length < 2 ||
              store.papers.filter((p) => p.included).length > 30
            "
            @click="
              task(`analyze:${store.project!.id}`, (key) => api.createRun(store.project!.id, key))
            "
          >
            {{ store.runs.length ? '重新分析文献 →' : '开始分析文献 →' }}
          </button>
          <p class="footnote">
            已纳入 {{ store.papers.filter((p) => p.included).length }} / 30
            篇；至少两篇。分篇分析后统一归纳。
          </p>
        </aside>
        <div class="workspace-content">
          <JobPanel
            v-if="store.job && tab !== 'jobs'"
            :job="store.job"
            :disabled="action.pending.value"
            :poll-error="store.pollError"
            @cancel="cancel"
            @retry="retry"
            @reconnect="store.watchJob"
          />
          <p v-if="store.pollError && !store.job" class="notice warning">
            {{ store.pollError
            }}<button
              v-if="store.project.active_job_id"
              @click="store.watchJob(store.project.active_job_id)"
            >
              重新连接
            </button>
          </p>
          <template v-if="tab === 'papers'"
            ><UploadPanel
              :disabled="store.busy"
              :pending="action.pending.value"
              @upload="upload"
              @import="importArxiv" /><PaperList
              :papers="store.papers"
              :run-papers="store.run?.papers"
              :disabled="disabled"
              @failure="changeTab('analysis')"
              @save="savePaper"
              @remove="deletePaper"
              @source="evidence.source" /></template
          ><template v-if="tab === 'analysis'"
            ><AnalysisPanel
              v-if="store.run"
              :run="store.run"
              :topics="store.topics"
              :disputes="store.disputes"
              :analysis="analysis"
              :disabled="disabled"
              @analysis="loadAnalysis"
              @evidence="evidence.show"
              @review="dispute"
              @browse="browse"
            />
            <section v-else class="empty-sheet">
              <h2>先建立分析快照</h2>
              <p>纳入至少两篇论文后，点击“开始分析文献”。主题、争议和单篇要素会整理在这里。</p>
            </section></template
          ><template v-if="tab === 'search'"
            ><SearchPanel
              v-if="store.run"
              :retrievals="store.retrievals"
              :disabled="disabled"
              :stale="store.run.is_stale"
              :ready="store.run.status === 'ready'"
              @search="
                (q) =>
                  task(JSON.stringify(['search', store.run!.id, q]), (key) =>
                    api.retrieve(store.run!.id, q, key),
                  )
              "
              @evidence="evidence.show"
            />
            <section class="panel" v-if="store.run">
              <h2>按原文页选择证据</h2>
              <form @submit.prevent="evidence.browse()">
                <label for="evidence-paper">快照论文</label
                ><select id="evidence-paper" v-model="evidence.paperId.value">
                  <option value="" disabled>选择论文</option>
                  <option
                    v-for="p in store.run.papers.filter((p) => p.parsed_document_id)"
                    :key="p.paper_id"
                    :value="p.paper_id"
                  >
                    {{ p.title }}
                  </option></select
                ><label for="evidence-page">原文页码（留空查看所有页）</label
                ><input
                  id="evidence-page"
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
                  <button class="cite" @click="evidence.show(e.id)">
                    第 {{ e.page_number }} 页 · {{ e.element_type }} ↗
                  </button>
                </li>
              </ul>
              <p
                v-if="
                  evidence.paperId.value &&
                  !evidence.loading.value &&
                  !evidence.candidates.value.length
                "
                class="footnote"
              >
                此范围暂无已加载的片段，可调整页码再读取。
              </p>
            </section>
            <section v-else class="empty-sheet">
              <h2>证据从分析快照中来</h2>
              <p>请先完成文献分析。</p>
            </section></template
          ><template v-if="tab === 'reviews'"
            ><section class="panel">
              <div class="section-heading">
                <h2>写一份综述</h2>
                <span class="mono">DRAFT / 05 SECTIONS</span>
              </div>
              <p>研究背景、主要方向、代表工作、学术争议、发展趋势。每条主张会保留它的证据。</p>
              <form @submit.prevent="generate">
                <label for="review-title">综述标题</label
                ><input
                  id="review-title"
                  v-model="reviewTitle"
                  :placeholder="store.project.title"
                  maxlength="120"
                  :disabled="disabled"
                /><label for="target-chars">目标字数</label
                ><input
                  id="target-chars"
                  v-model="target"
                  type="number"
                  min="2000"
                  max="4000"
                  :disabled="disabled"
                /><button
                  class="primary"
                  :disabled="
                    disabled || !store.run || store.run.status !== 'ready' || store.run.is_stale
                  "
                >
                  生成综述 →
                </button>
              </form>
              <p
                v-if="!store.run || store.run.status !== 'ready' || store.run.is_stale"
                class="notice warning"
              >
                需要当前语料的就绪分析快照。
              </p>
              <p class="footnote">
                目标字数是写作目标；证据不足时正文可能更短，仍需核验与人工阅读。
              </p>
            </section>
            <section class="panel">
              <h2>手稿档案</h2>
              <p v-if="!store.reviews.length" class="empty">
                还没有综述，生成后的版本会保存在这里。
              </p>
              <RouterLink
                v-for="r in store.reviews"
                :key="r.id"
                class="review-link"
                :to="`/reviews/${r.id}`"
                ><div>
                  <h3>{{ r.title }}</h3>
                  <span class="footnote">{{ new Date(r.created_at).toLocaleString('zh-CN') }}</span>
                </div>
                <span class="badge">{{
                  r.latest_revision ? `正文 v${r.latest_revision.revision_number}` : '尚无正文'
                }}</span
                ><span>打开 →</span></RouterLink
              >
            </section></template
          ><template v-if="tab === 'jobs'"
            ><div class="section-heading">
              <h2>历史记录</h2>
              <button
                class="quiet"
                :disabled="action.pending.value"
                @click="action.perform(() => store.refresh())"
              >
                重新读取
              </button>
            </div>
            <p v-if="!store.jobs.length" class="empty">尚无任务记录。</p>
            <JobPanel
              v-for="j in store.jobs"
              :key="j.id"
              :job="store.job?.id === j.id ? store.job : j"
              :disabled="action.pending.value || (store.busy && store.job?.id !== j.id)"
              :poll-error="store.job?.id === j.id ? store.pollError : ''"
              @cancel="cancel"
              @retry="retry"
              @reconnect="store.watchJob"
          /></template>
        </div>
        <div class="source-column">
          <ErrorNotice :message="evidence.error.value" />
          <p v-if="evidence.loading.value" role="status">正在读取证据…</p>
          <EvidencePanel
            v-if="evidence.open.value"
            :evidence="evidence.evidence.value"
            :paper="evidence.sourcePaper.value"
            :file-url="evidence.fileUrl.value"
            :page="evidence.page.value"
            :references="
              store.topics
                .filter((t) => t.evidence_ids.includes(evidence.evidence.value?.id ?? ''))
                .map((t) => t.name)
            "
            @close="evidence.open.value = false"
            @page="evidence.page.value = $event"
          />
          <div v-else class="source-placeholder">
            <span class="eyebrow">MARGIN NOTES</span
            ><svg viewBox="0 0 180 90" aria-hidden="true">
              <path
                d="M150 15q-62 50-120 46m23-18L28 62l27 8"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              />
            </svg>
            <h3>把依据留在旁边</h3>
            <p>点击主题、争议或检索记录中的证据，回到它在论文里的位置。</p>
            <span class="pencil-note">每条引用，都能回看。</span>
          </div>
        </div>
      </div></template
    >
  </div>
</template>
