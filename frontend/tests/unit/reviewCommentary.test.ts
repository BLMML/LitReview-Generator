import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ReviewEditor from '../../src/components/ReviewEditor.vue'
import { commentaryNotices, draftError, MISSING_SOURCE_CONTENT } from '../../src/reviewRules'
import type { Block, Section, Validation } from '../../src/types/contracts'

function manuscript(): Section[] {
  const keys = [
    'background',
    'approach',
    'significance',
    'theoretical_basis',
    'research_status',
    'achievements',
    'limitations',
    'innovation',
  ] as const
  let next = 0
  const block = (analysis_role?: Block['analysis_role'], controversy_number?: number): Block => ({
    block_id: `block-${++next}`,
    kind: 'claim',
    text: analysis_role ? '基于前文的分析建议。' : '完整原文。',
    evidence_ids: analysis_role ? ['source-1', 'source-2'] : [next === 2 ? 'source-2' : 'source-1'],
    ...(analysis_role ? { analysis_role } : {}),
    ...(controversy_number ? { controversy_number } : {}),
  })
  return keys.map((key) => ({
    section_key: key,
    title: key,
    blocks:
      key === 'achievements'
        ? [block('achievement')]
        : key === 'limitations'
          ? [
              block('viewpoint_comparison', 1),
              block('root_cause', 1),
              block('viewpoint_comparison', 2),
              block('root_cause', 2),
            ]
          : key === 'innovation'
            ? [block('research_gap'), block('future_direction'), block('innovation')]
            : [block()],
  }))
}

describe('source modules and autonomous commentary', () => {
  it('permits multiple front-source references in commentary while keeping source blocks literal', () => {
    const sections = manuscript()
    expect(draftError(sections)).toBe('')
    sections[0].blocks[0].evidence_ids.push('source-2')
    expect(draftError(sections)).toContain('一条原文证据')
  })
  it('rejects a commentary citation that was not used in the source modules', () => {
    const sections = manuscript()
    sections[7].blocks[0].evidence_ids.push('source-3')
    expect(draftError(sections)).toContain('前文原文模块')
  })
  it('requires a cause for each of two controversy numbers and allows an honestly marked draft', () => {
    const sections = manuscript()
    expect(commentaryNotices(sections)).toEqual([])
    sections[6].blocks.pop()
    expect(draftError(sections)).toBe('')
    expect(commentaryNotices(sections)[0]).toContain('争议证据不足')
  })
  it('does not count unsupported semantic findings as a complete controversy', () => {
    const sections = manuscript()
    const findings = sections
      .flatMap((s) => s.blocks)
      .map((b) => ({
        block_id: b.block_id,
        structure_ok: true,
        support: 'supported',
        issues: [],
        suggestion: null,
      })) as NonNullable<Validation['blocks']>
    const target = findings.find((b) => b.block_id === sections[6].blocks[3].block_id)!
    target.support = 'uncertain'
    expect(commentaryNotices(sections, findings)[0]).toContain('争议证据不足')
  })
  it('offers commentary roles, pairs controversy numbers and keeps multiple references selectable', async () => {
    const sections = manuscript()
    const wrapper = mount(ReviewEditor, { props: { sections, disabled: false, activeBlock: '' } })
    expect(wrapper.find('button.cite').attributes('disabled')).toBeDefined()
    const commentary = wrapper.findAll('.section-sheet')[6]
    expect(commentary.find('button.cite').attributes('disabled')).toBeUndefined()
    await commentary.find('select[id^="role-"]').setValue('root_cause')
    const update = wrapper.emitted('update')![0][0] as Section[]
    expect(update[6].blocks[0]).toMatchObject({
      analysis_role: 'root_cause',
      controversy_number: 1,
    })
    await commentary.find('input[type="number"]').setValue('2')
    expect((wrapper.emitted('update')![1][0] as Section[])[6].blocks[0].controversy_number).toBe(2)
  })
  it('removes commentary metadata when a module becomes an uncited missing notice', async () => {
    const sections = manuscript()
    const wrapper = mount(ReviewEditor, { props: { sections, disabled: false, activeBlock: '' } })
    await wrapper.findAll('.section-sheet')[5].find('select[id^="kind-"]').setValue('transition')
    const update = wrapper.emitted('update')![0][0] as Section[]
    expect(update[5].blocks[0]).toMatchObject({
      kind: 'transition',
      text: MISSING_SOURCE_CONTENT,
      evidence_ids: [],
    })
    expect(update[5].blocks[0].analysis_role).toBeUndefined()
    expect(draftError(update)).toBe('')
  })
})
