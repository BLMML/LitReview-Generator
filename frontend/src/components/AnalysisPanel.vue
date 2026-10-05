<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Run, Topic, Dispute, PaperAnalysis, EvidenceText } from '../types/contracts'
const props = defineProps<{
  run: Run
  topics: Topic[]
  disputes: Dispute[]
  analysis: PaperAnalysis | null
  disabled: boolean
}>()
const emit = defineEmits<{
  analysis: [paperId: string]
  evidence: [id: string]
  review: [dispute: Dispute, status: Dispute['review_status'], note: string]
  browse: [paperId: string]
}>()
const notes = ref<Record<string, string>>({})
const years = computed(() =>
  Array.isArray(props.run.stats.year_counts)
    ? props.run.stats.year_counts
    : Object.entries(props.run.stats.year_counts).map(([year, count]) => ({
        year: Number(year),
        count,
      })),
)
const fields = computed(() => {
  const a = props.analysis
  if (!a) return []
  return [
    ['研究问题', [a.research_question]],
    ['研究对象', [a.research_object]],
    ['方法', a.methods],
    ['数据集', a.datasets],
    ['指标', a.metrics],
    ['结论', a.conclusions],
    ['局限', a.limitations],
    ['摘要', [a.summary]],
  ] as [string, EvidenceText[]][]
})
const paperTitle = (id: string) =>
  props.run.papers.find((p) => p.paper_id === id)?.title ?? '快照论文'
const classifications = {
  possible_conflict: '可能冲突',
  different_conditions: '条件不同',
  complementary: '互补',
  insufficient_evidence: '证据不足',
}
</script>
<template>
  <div class="analysis-panel">
    <section class="panel">
      <div class="section-heading">
        <h2>分析快照</h2>
        <span class="badge" :class="run.is_stale ? 'warning' : ''">{{
          run.is_stale
            ? '语料已过时'
            : { ready: '已就绪', processing: '分析中', failed: '失败', cancelled: '已取消' }[
                run.status
              ]
        }}</span>
      </div>
      <p class="eyebrow">FROZEN RESEARCH QUESTION</p>
      <p class="snapshot-question">{{ run.research_question }}</p>
      <p class="footnote">
        语料 v{{ run.corpus_revision }} / 分析 v{{ run.analysis_revision }} / 输出中文
      </p>
      <p v-if="run.is_stale" class="notice warning">
        这是历史语料快照。原文和证据仍可回看；新检索与生成需要重新分析。
      </p>
      <div class="stats-row">
        <div>
          <strong>{{ run.stats.ready_count }}</strong
          ><span>已解析</span>
        </div>
        <div>
          <strong>{{ run.stats.failed_count }}</strong
          ><span>解析失败</span>
        </div>
        <div>
          <strong>{{ run.stats.included_count }}</strong
          ><span>快照论文</span>
        </div>
      </div>
      <div class="chips">
        <span v-for="y in years" :key="y.year" class="badge"
          >{{ y.year }} 年 · {{ y.count }} 篇</span
        ><span class="badge">年份未知 · {{ run.stats.unknown_year_count }} 篇</span>
      </div>
      <ol class="run-ledger">
        <li v-for="paper in run.papers" :key="paper.paper_id">
          <h3>{{ paper.title }}</h3>
          <span class="badge">{{
            { pending: '待解析', processing: '解析中', ready: '已解析', failed: '失败' }[
              paper.status
            ]
          }}</span>
          <p v-if="paper.error" class="danger-text">{{ paper.error.message }}</p>
          <div class="actions">
            <button
              class="quiet"
              :disabled="paper.status !== 'ready'"
              @click="emit('analysis', paper.paper_id)"
            >
              要素与摘要</button
            ><button
              class="quiet"
              :disabled="!paper.parsed_document_id"
              @click="emit('browse', paper.paper_id)"
            >
              按页查看证据 →
            </button>
          </div>
        </li>
      </ol>
      <p v-if="run.error" class="notice danger">{{ run.error.message }}</p>
    </section>
    <section v-if="analysis" class="panel">
      <div class="section-heading">
        <h2>单篇分析</h2>
        <span class="mono">PAPER NOTES</span>
      </div>
      <h3>{{ paperTitle(analysis.paper_id) }}</h3>
      <div v-for="[label, values] in fields" :key="label" class="analysis-field">
        <h4>{{ label }}</h4>
        <p v-if="!values.length" class="muted">未提取到该项</p>
        <div v-for="(v, i) in values" :key="i">
          <p>{{ v.value ?? '未提取到该项' }}</p>
          <div class="evidence-links">
            <button
              v-for="(id, n) in v.evidence_ids"
              :key="id"
              class="cite"
              @click="emit('evidence', id)"
            >
              证据 {{ n + 1 }} ↗
            </button>
          </div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="section-heading">
        <h2>研究主题</h2>
        <span class="mono">TOPIC MAP</span>
      </div>
      <p v-if="run.status !== 'ready'" class="empty">分析尚未完成，主题等待生成。</p>
      <p v-else-if="!topics.length" class="empty">本次分析没有形成主题。</p>
      <article v-for="(topic, i) in topics" :key="topic.id" class="topic">
        <p class="eyebrow">方向 {{ String(i + 1).padStart(2, '0') }}</p>
        <h3>{{ topic.name }}</h3>
        <p>{{ topic.description }}</p>
        <ul class="small">
          <li v-for="id in topic.paper_ids" :key="id">{{ paperTitle(id) }}</li>
        </ul>
        <div class="evidence-links">
          <button
            v-for="(id, n) in topic.evidence_ids"
            :key="id"
            class="cite"
            @click="emit('evidence', id)"
          >
            依据 {{ n + 1 }} ↗
          </button>
        </div>
      </article>
      <p class="footnote">一篇论文可属于多个主题；未归类论文仍保留在快照账本中。</p>
    </section>
    <section class="panel">
      <div class="section-heading">
        <h2>争议对照</h2>
        <span class="mono">READ BOTH SIDES</span>
      </div>
      <p v-if="run.status !== 'ready'" class="empty">分析尚未完成，争议等待生成。</p>
      <p v-else-if="!disputes.length" class="empty">本次分析没有形成争议记录。</p>
      <article v-for="d in disputes" :key="d.id" class="dispute">
        <div class="chips">
          <span class="badge warning">{{ classifications[d.classification] }}</span
          ><span class="badge">{{
            { pending: '待人工复核', confirmed: '已确认', rejected: '已拒绝' }[d.review_status]
          }}</span>
        </div>
        <p class="footnote">对照问题：{{ d.conditions.research_question }}</p>
        <div class="compare">
          <div v-for="side in ['left', 'right'] as const" :key="side">
            <h3>{{ paperTitle(side === 'left' ? d.left_paper_id : d.right_paper_id) }}</h3>
            <p>{{ (side === 'left' ? d.left_claim : d.right_claim).value ?? '暂无主张' }}</p>
            <p class="footnote">
              适用条件：{{
                side === 'left' ? d.conditions.left_conditions : d.conditions.right_conditions
              }}
            </p>
            <div class="evidence-links">
              <button
                v-for="(id, n) in (side === 'left' ? d.left_claim : d.right_claim).evidence_ids"
                :key="id"
                class="cite"
                @click="emit('evidence', id)"
              >
                原文 {{ n + 1 }} ↗
              </button>
            </div>
          </div>
        </div>
        <p><strong>可比性：</strong>{{ d.conditions.comparability_reason }}</p>
        <p>{{ d.reason }}</p>
        <label :for="`note-${d.id}`">人工复核备注</label
        ><textarea
          :id="`note-${d.id}`"
          v-model="notes[d.id]"
          :placeholder="d.review_note || '写下你的判断依据'"
          maxlength="2000"
          :disabled="disabled"
          rows="2"
        ></textarea>
        <div class="actions">
          <button
            :disabled="disabled"
            @click="emit('review', d, 'confirmed', notes[d.id] ?? d.review_note)"
          >
            确认</button
          ><button
            :disabled="disabled"
            @click="emit('review', d, 'rejected', notes[d.id] ?? d.review_note)"
          >
            拒绝</button
          ><button
            class="quiet"
            :disabled="disabled"
            @click="emit('review', d, 'pending', notes[d.id] ?? d.review_note)"
          >
            标记待复核
          </button>
        </div>
      </article>
    </section>
  </div>
</template>
