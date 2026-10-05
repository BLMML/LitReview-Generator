<script setup lang="ts">
import type { Validation, Job, ReviewRevision } from '../types/contracts'
defineProps<{
  revision: ReviewRevision
  reports: Validation[]
  report: Validation | null
  job?: Job
  warnings: string[]
  acknowledged: boolean
  canExport: boolean
  exportBusy?: boolean
  exportMessage?: string
  exportError?: string
  disabled: boolean
  dirty: boolean
}>()
const emit = defineEmits<{
  select: [id: string]
  validate: []
  export: []
  acknowledge: [value: boolean]
  block: [id: string]
}>()
const support = {
  supported: '原文支持',
  unsupported: '原文不支持',
  uncertain: '无法确定',
  not_applicable: '不适用',
}
</script>
<template>
  <section class="panel validation-panel">
    <div class="section-heading">
      <h2>核验与导出</h2>
      <span class="stamp">正文 v{{ revision.revision_number }}</span>
    </div>
    <p class="footnote">核验只作用于此处选中的已保存版本。人工修改保存后需要重新核验。</p>
    <p v-if="dirty" class="notice warning">有未保存改动。先保存为新版本，再核验或导出。</p>
    <button :disabled="disabled || dirty" @click="emit('validate')">
      核验正文 v{{ revision.revision_number }} →</button
    ><label for="validation-history">此版本的核验报告</label
    ><select
      id="validation-history"
      :value="report?.id ?? ''"
      @change="emit('select', ($event.target as HTMLSelectElement).value)"
    >
      <option value="" disabled>尚无报告</option>
      <option v-for="v in reports" :key="v.id" :value="v.id">
        {{ new Date(v.created_at).toLocaleString('zh-CN') }} / {{ v.result ?? '尚无完成结果' }}
      </option></select
    ><template v-if="report"
      ><p
        class="notice"
        :class="
          job?.status === 'failed' ? 'danger' : report.result === 'passed' ? 'good' : 'warning'
        "
      >
        <strong>{{
          job?.status === 'failed'
            ? '核验任务失败'
            : job?.status === 'cancelled'
              ? '核验已取消'
              : job?.status !== 'succeeded'
                ? '尚无成功完成的核验报告'
                : report.result === 'passed'
                  ? '此版本核验通过'
                  : '此版本仍需人工审阅'
        }}</strong
        ><span v-if="report.structure_passed !== null"
          >结构校验：{{ report.structure_passed ? '通过' : '未通过' }}</span
        >
      </p>
      <p v-if="job?.error" class="danger-text">{{ job.error.message }}</p>
      <ul v-if="report.blocks !== null" class="validation-blocks">
        <li v-for="b in report.blocks" :key="b.block_id">
          <button class="quiet" @click="emit('block', b.block_id)">定位正文与引用 →</button
          ><strong>{{ support[b.support] }}</strong
          ><span class="badge">{{ b.structure_ok ? '结构正确' : '结构问题' }}</span>
          <ul>
            <li v-for="issue in b.issues" :key="issue">{{ issue }}</li>
          </ul>
          <p v-if="b.suggestion" class="footnote">建议：{{ b.suggestion }}</p>
        </li>
      </ul>
      <p v-else class="muted">此报告尚无完成结果。</p></template
    >
    <p v-else class="empty">尚未核验这个版本。</p>
    <div class="export-check">
      <h3>准备导出 Word</h3>
      <p v-if="exportMessage" class="notice good export-progress" role="status">
        {{ exportMessage }}
      </p>
      <p v-if="exportError" class="notice danger" role="alert">{{ exportError }}</p>
      <p class="footnote">点击后生成 Word 并自动下载，无需滚动到页面底部。</p>
      <p v-if="!canExport" class="footnote">需匹配此正文版本、结构通过且任务成功的核验报告。</p>
      <div v-if="warnings.length" class="notice warning">
        <strong>导出将携带以下警示</strong>
        <ul>
          <li v-for="w in warnings" :key="w">{{ w }}</li>
        </ul>
        <label class="check-label"
          ><input
            type="checkbox"
            :checked="acknowledged"
            :disabled="disabled || !canExport"
            @change="emit('acknowledge', ($event.target as HTMLInputElement).checked)"
          />我已阅读这些警示，仍要导出此版本</label
        >
      </div>
      <button
        class="primary wide"
        :disabled="disabled || exportBusy || !canExport || (warnings.length > 0 && !acknowledged)"
        @click="emit('export')"
      >
        {{ exportBusy ? '正在导出 Word…' : `导出正文 v${revision.revision_number} ↓` }}
      </button>
    </div>
  </section>
</template>
