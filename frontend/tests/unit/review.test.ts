import { expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { reactive } from 'vue'
import ReviewEditor from '../../src/components/ReviewEditor.vue'
import { draftError, exportAllowed, exportWarnings } from '../../src/reviewRules'
import type {
  Job,
  Run,
  Review,
  ReviewRevision,
  Validation,
  Section,
} from '../../src/types/contracts'
const section: Section = {
  section_key: 'background',
  title: '背景',
  blocks: [{ block_id: crypto.randomUUID(), kind: 'claim', text: '论断', evidence_ids: ['e1'] }],
}
const revision = {
  id: 'v1',
  revision_number: 1,
  is_stale: false,
  sections: [section],
} as ReviewRevision
const report = {
  id: 'a1',
  revision_id: 'v1',
  result: 'passed',
  structure_passed: true,
  blocks: [],
} as unknown as Validation
const job = { status: 'succeeded' } as Job
it('requires the same saved revision and succeeded job to export', () => {
  expect(exportAllowed(revision, report, job, false)).toBe(true)
  expect(exportAllowed(revision, { ...report, revision_id: 'v2' }, job, false)).toBe(false)
  expect(exportAllowed(revision, report, { ...job, status: 'failed' }, false)).toBe(false)
  expect(exportAllowed(revision, report, job, true)).toBe(false)
})
it('pending/null reports and structural failures cannot export', () => {
  expect(exportAllowed(revision, { ...report, result: null, blocks: null }, job, false)).toBe(false)
  expect(exportAllowed(revision, { ...report, structure_passed: false }, job, false)).toBe(false)
})
it('needs_review can export only with external explicit warning acknowledgement', () => {
  const r = { ...report, result: 'needs_review' as const }
  expect(exportAllowed(revision, r, job, false)).toBe(true)
  expect(
    exportWarnings(
      revision,
      { latest_revision: revision } as Review,
      { is_stale: false } as Run,
      r,
    ),
  ).toEqual(['核验结果为 needs_review，正文存在待审问题'])
})
it('corpus staleness, analysis staleness, and historical revision are distinct warnings', () => {
  const warnings = exportWarnings(
    { ...revision, is_stale: true },
    { latest_revision: { id: 'v2' } } as Review,
    { is_stale: true } as Run,
    report,
  )
  expect(warnings).toHaveLength(3)
  expect(warnings.join(' ')).toContain('历史语料')
  expect(warnings.join(' ')).toContain('历史分析')
  expect(warnings.join(' ')).toContain('不是最新')
})
it('editing a reactive DTO preserves original IDs without mutating the saved revision', async () => {
  const sections = reactive([section])
  const wrapper = mount(ReviewEditor, { props: { sections, disabled: false, activeBlock: '' } })
  await wrapper.find('textarea').setValue('修改后的论断')
  const output = wrapper.emitted('update')![0][0] as Section[]
  expect(output[0].blocks[0].block_id).toBe(section.blocks[0].block_id)
  expect(output[0].blocks[0].text).toBe('修改后的论断')
  expect(section.blocks[0].text).toBe('论断')
})
it('new blocks receive UUIDs and keep the original block', async () => {
  const wrapper = mount(ReviewEditor, {
    props: { sections: [section], disabled: false, activeBlock: '' },
  })
  const buttons = wrapper.findAll('button')
  await buttons.find((b) => b.text().includes('添加正文块'))!.trigger('click')
  const output = wrapper.emitted('update')![0][0] as Section[]
  expect(output[0].blocks).toHaveLength(2)
  expect(output[0].blocks[1].block_id).toMatch(/^[0-9a-f-]{36}$/)
  expect(output[0].blocks[1].block_id).not.toBe(section.blocks[0].block_id)
})
it('draft errors identify unsupported claims and incomplete sections', () => {
  expect(draftError([{ ...section, blocks: [] }])).toContain('至少')
  expect(
    draftError([{ ...section, blocks: [{ ...section.blocks[0], evidence_ids: [] }] }]),
  ).toContain('主张')
  expect(draftError([section])).toBe('')
})
