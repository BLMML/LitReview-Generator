<script setup lang="ts">
import { ref } from 'vue'
import type { Retrieval } from '../types/contracts'
defineProps<{ retrievals: Retrieval[]; disabled: boolean; stale: boolean; ready: boolean }>()
const emit = defineEmits<{ search: [query: string]; evidence: [id: string] }>()
const query = ref('')
</script>
<template>
  <section class="panel">
    <div class="section-heading">
      <h2>找一条证据</h2>
      <span class="mono">EVIDENCE SEARCH</span>
    </div>
    <form @submit.prevent="emit('search', query.trim())">
      <label for="search-query">在本次分析快照中检索</label
      ><textarea
        id="search-query"
        v-model="query"
        required
        minlength="2"
        maxlength="2000"
        rows="2"
        placeholder="例如：哪些研究讨论了检索结果对答案可靠性的影响？"
        :disabled="disabled || stale || !ready"
      ></textarea
      ><button :disabled="disabled || stale || !ready">检索证据 →</button>
    </form>
    <p v-if="stale" class="notice warning">
      历史 Run 可查看已保存结果。需要新检索时，请先创建新的分析。
    </p>
    <p class="footnote">查询扩展 → 关键词召回 → 模型重排；排序分数表示相关性。</p>
    <h3 class="subheading">检索记录 / {{ retrievals.length }}</h3>
    <p v-if="!retrievals.length" class="empty">还没有检索记录。也可直接按原文页码选择证据。</p>
    <details v-for="(r, i) in retrievals" :key="r.id" :open="i === 0" class="retrieval">
      <summary>
        {{ r.query }}
        <span class="badge">{{
          r.results === null ? '等待结果' : `${r.results.length} 条结果`
        }}</span>
      </summary>
      <p v-if="r.results === null" class="muted">尚无完成结果，执行状态请查看历史记录。</p>
      <p v-else-if="!r.results.length" class="muted">检索已完成，未找到符合条件的证据。</p>
      <ol v-else class="hit-list">
        <li v-for="hit in r.results" :key="hit.evidence_id">
          <span class="mono">{{ String(hit.rank).padStart(2, '0') }}</span>
          <div>
            <p>{{ hit.reason }}</p>
            <span class="footnote">相关性 {{ hit.score.toFixed(2) }}</span>
          </div>
          <button class="quiet" @click="emit('evidence', hit.evidence_id)">回看原文 ↗</button>
        </li>
      </ol>
    </details>
  </section>
</template>
