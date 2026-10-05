<script setup lang="ts">
import { computed } from 'vue'
import { terminal, type Job, type ImportItem } from '../types/contracts'
const props = defineProps<{ job: Job; disabled?: boolean; pollError?: string }>()
const emit = defineEmits<{ cancel: [id: string]; retry: [id: string]; reconnect: [id: string] }>()
const labels: Record<Job['status'], string> = {
  queued: '排队中',
  running: '执行中',
  cancelling: '正在取消',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
const names: Record<Job['kind'], string> = {
  import_arxiv: '导入论文',
  analyze: '分析文献',
  retrieve: '检索证据',
  generate_review: '生成综述',
  validate_review: '核验正文',
  export_docx: '导出 Word',
}
const items = computed(() => {
  const value = props.job.result?.items
  return Array.isArray(value) ? (value as ImportItem[]) : []
})
const percent = computed(() =>
  props.job.progress.total
    ? Math.min(100, (props.job.progress.completed / props.job.progress.total) * 100)
    : null,
)
</script>
<template>
  <section class="panel job-panel" :class="{ 'job-failed': job.status === 'failed' }">
    <div class="section-heading">
      <h2>{{ names[job.kind] }}</h2>
      <span
        class="badge"
        :class="
          job.status === 'failed' ? 'danger' : job.status === 'succeeded' ? 'good' : 'warning'
        "
        >{{ labels[job.status] }}</span
      >
    </div>
    <p class="mono small">
      阶段 {{ job.stage || '等待 worker' }} · {{ job.progress.completed }} /
      {{ job.progress.total ?? '待确定' }} {{ job.progress.unit }}
    </p>
    <div
      v-if="percent !== null"
      class="progress-track"
      role="progressbar"
      :aria-label="names[job.kind]"
      :aria-valuenow="job.progress.completed"
      :aria-valuemax="job.progress.total ?? undefined"
      :aria-valuemin="0"
    >
      <span :style="{ width: `${percent}%` }"></span>
    </div>
    <p v-else class="footnote">服务尚未给出总量，等待实际进度。</p>
    <div v-if="job.error" class="notice danger">
      <strong>{{ job.error.message }}</strong
      ><span class="mono small">{{ job.error.code }}</span>
      <p>
        {{
          job.error.retryable
            ? '原因处理后可重试，已完成阶段由服务恢复。'
            : '此错误不可直接重试，请核对材料与配置。'
        }}
      </p>
    </div>
    <p v-if="pollError" class="notice warning" role="status">
      {{ pollError }} <button class="quiet" @click="emit('reconnect', job.id)">重新连接</button>
    </p>
    <ul v-if="items.length" class="import-ledger">
      <li v-for="item in items" :key="item.requested_id">
        <strong>{{ item.requested_id }}</strong
        ><span>→ {{ item.resolved_source_id ?? '等待解析版本' }}</span
        ><span class="badge">{{
          { pending: '等待导入', imported: '已导入', duplicate: '已有论文', failed: '导入失败' }[
            item.status
          ]
        }}</span>
        <p v-if="item.error" class="danger-text">{{ item.error.message }}</p>
      </li>
    </ul>
    <div class="actions">
      <button
        v-if="!terminal(job.status)"
        :disabled="disabled || job.status === 'cancelling'"
        @click="emit('cancel', job.id)"
      >
        {{ job.status === 'cancelling' ? '等待取消完成…' : '取消任务' }}</button
      ><button
        v-if="(job.status === 'failed' && job.error?.retryable) || job.status === 'cancelled'"
        :disabled="disabled"
        @click="emit('retry', job.id)"
      >
        重试任务
      </button>
    </div>
    <details>
      <summary>任务编号与时间</summary>
      <p class="mono small">{{ job.id }}</p>
      <p class="footnote">创建 {{ new Date(job.created_at).toLocaleString('zh-CN') }}</p>
    </details>
  </section>
</template>
