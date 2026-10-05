<script setup lang="ts">
import { clone } from '../clone'
import { MISSING_SOURCE_CONTENT } from '../reviewRules'
import type { Section } from '../types/contracts'
import { computed } from 'vue'
const props = defineProps<{ sections: Section[]; disabled: boolean; activeBlock: string }>()
const sourceLayout = computed(() => props.sections.some((s) => s.section_key === 'significance'))
const sectionNumbers: Record<string, string> = {
  background: '01',
  significance: '02',
  approach: '03',
  theoretical_basis: '04.1',
  research_status: '04.2',
  achievements: '05',
}
const emit = defineEmits<{
  update: [sections: Section[]]
  pick: [blockId: string]
  evidence: [id: string]
}>()
function update(section: number, block: number, key: 'text' | 'kind', value: string) {
  const copy = clone(props.sections)
  const current = copy[section].blocks[block]
  if (key === 'kind') {
    current.kind = value as 'claim' | 'transition'
    if (sourceLayout.value && value === 'transition') {
      current.text = MISSING_SOURCE_CONTENT
      current.evidence_ids = []
    } else if (sourceLayout.value && current.text === MISSING_SOURCE_CONTENT) {
      current.text = ''
      current.evidence_ids = []
    }
  } else current.text = value
  emit('update', copy)
}
function heading(section: number, title: string) {
  const copy = clone(props.sections)
  copy[section].title = title
  emit('update', copy)
}
function add(section: number) {
  const copy = clone(props.sections)
  const next = {
    block_id: crypto.randomUUID(),
    kind: (sourceLayout.value ? 'claim' : 'transition') as 'claim' | 'transition',
    text: '',
    evidence_ids: [],
  }
  if (
    sourceLayout.value &&
    copy[section].blocks.length === 1 &&
    copy[section].blocks[0].kind === 'transition'
  )
    copy[section].blocks = [next]
  else copy[section].blocks.push(next)
  emit('update', copy)
}
function remove(section: number, block: number) {
  const copy = clone(props.sections)
  copy[section].blocks.splice(block, 1)
  emit('update', copy)
}
function removeEvidence(section: number, block: number, id: string) {
  const copy = clone(props.sections)
  copy[section].blocks[block].evidence_ids = copy[section].blocks[block].evidence_ids.filter(
    (e) => e !== id,
  )
  emit('update', copy)
}
</script>
<template>
  <div class="review-editor">
    <section v-for="(s, si) in sections" :key="s.section_key" class="section-sheet sheet">
      <h2 v-if="sourceLayout && s.section_key === 'theoretical_basis'">04 文献综述</h2>
      <div class="section-heading">
        <span class="section-number">{{
          sourceLayout ? sectionNumbers[s.section_key] : String(si + 1).padStart(2, '0')
        }}</span
        ><label class="sr-only" :for="`heading-${si}`">第 {{ si + 1 }} 节标题</label
        ><input
          :id="`heading-${si}`"
          class="heading-input"
          :value="s.title"
          maxlength="120"
          :disabled="disabled || sourceLayout"
          @input="heading(si, ($event.target as HTMLInputElement).value)"
        />
      </div>
      <article
        v-for="(b, bi) in s.blocks"
        :key="b.block_id"
        class="editor-block"
        :class="{ selected: activeBlock === b.block_id }"
      >
        <div class="block-top">
          <label :for="`kind-${b.block_id}`">块 {{ bi + 1 }} 类型</label
          ><select
            :id="`kind-${b.block_id}`"
            :value="b.kind"
            :disabled="disabled"
            @change="update(si, bi, 'kind', ($event.target as HTMLSelectElement).value)"
          >
            <option value="claim">
              {{ sourceLayout ? '原文正文 / 需要证据' : '主张 / 需要证据' }}
            </option>
            <option value="transition" :disabled="sourceLayout && s.blocks.length > 1">
              {{ sourceLayout ? '缺失说明' : '过渡 / 组织行文' }}
            </option></select
          ><button
            class="quiet danger-text"
            :disabled="disabled || s.blocks.length <= 1"
            @click="remove(si, bi)"
          >
            删除块
          </button>
        </div>
        <label class="sr-only" :for="`text-${b.block_id}`"
          >{{ s.title }} 第 {{ bi + 1 }} 块正文</label
        ><textarea
          :id="`text-${b.block_id}`"
          :value="b.text"
          rows="5"
          maxlength="600"
          :disabled="disabled || (sourceLayout && b.kind === 'transition')"
          @input="update(si, bi, 'text', ($event.target as HTMLTextAreaElement).value)"
        ></textarea>
        <div class="block-bottom">
          <span class="footnote">{{ b.text.length }} / 600 字</span
          ><button
            class="cite"
            :disabled="
              disabled ||
              (sourceLayout && b.kind === 'transition') ||
              b.evidence_ids.length >= (sourceLayout ? 1 : 20)
            "
            @click="emit('pick', b.block_id)"
          >
            + 选择原文证据
          </button>
        </div>
        <div class="evidence-links">
          <div v-for="(id, n) in b.evidence_ids" :key="id" class="reference-chip">
            <button class="cite" @click="emit('evidence', id)">引用 {{ n + 1 }} ↗</button
            ><button
              class="remove-cite"
              :aria-label="`移除引用 ${n + 1}`"
              :disabled="disabled"
              @click="removeEvidence(si, bi, id)"
            >
              ×
            </button>
          </div>
        </div>
        <p v-if="sourceLayout && b.kind === 'transition'" class="footnote">
          缺失说明不添加引用。如原文包含本模块内容，请改为“原文正文”，填写完整原句并选择对应证据。
        </p>
        <p v-if="b.kind === 'claim' && !b.evidence_ids.length" class="danger-text small">
          这条主张尚未选择证据。
        </p>
      </article>
      <button
        class="quiet"
        :disabled="disabled || sections.reduce((n, x) => n + x.blocks.length, 0) >= 100"
        @click="add(si)"
      >
        + 添加正文块
      </button>
    </section>
  </div>
</template>
