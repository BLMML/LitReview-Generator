import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ReviewEditor from '../../src/components/ReviewEditor.vue'
import { draftError, MISSING_SOURCE_CONTENT } from '../../src/reviewRules'
import type { Section } from '../../src/types/contracts'

const missing = (): Section[] =>
  ['background', 'significance'].map((key, i) => ({
    section_key: key as Section['section_key'],
    title: i ? '研究意义' : '研究背景',
    blocks: [
      { block_id: `b${i}`, kind: 'transition', text: MISSING_SOURCE_CONTENT, evidence_ids: [] },
    ],
  }))
const mountEditor = (sections: Section[]) =>
  mount(ReviewEditor, {
    props: { sections, disabled: false, activeBlock: '' },
  })

describe('source-only missing module editing', () => {
  it('does not permit attaching evidence to a missing notice', () => {
    const wrapper = mountEditor(missing())
    expect(wrapper.find('textarea').attributes('disabled')).toBeDefined()
    expect(wrapper.find('button.cite').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('缺失说明不添加引用')
  })
  it('switches between real source text and a canonical uncited missing notice', async () => {
    const wrapper = mountEditor(missing())
    await wrapper.find('select').setValue('claim')
    const update = wrapper.emitted('update')![0][0] as Section[]
    expect(update[0].blocks[0]).toMatchObject({ kind: 'claim', text: '', evidence_ids: [] })
    update[0].blocks[0].text = 'Complete original sentence.'
    update[0].blocks[0].evidence_ids = ['source-1']
    await wrapper.setProps({ sections: update })
    await wrapper.find('select').setValue('transition')
    const restored = wrapper.emitted('update')![1][0] as Section[]
    expect(restored[0].blocks[0]).toMatchObject({
      kind: 'transition',
      text: MISSING_SOURCE_CONTENT,
      evidence_ids: [],
    })
    expect(draftError(restored)).toBe('')
  })
  it('replaces a lone missing notice when adding real content rather than mixing both', async () => {
    const wrapper = mountEditor(missing())
    const add = wrapper.findAll('button').find((button) => button.text() === '+ 添加正文块')!
    await add.trigger('click')
    const update = wrapper.emitted('update')![0][0] as Section[]
    expect(update[0].blocks).toHaveLength(1)
    expect(update[0].blocks[0]).toMatchObject({ kind: 'claim', text: '', evidence_ids: [] })
    expect(update[1]).toEqual(missing()[1])
  })
  it('catches an already-invalid missing notice with a citation before a network write', () => {
    const draft = missing()
    draft[0].blocks[0].evidence_ids = ['source-1']
    expect(draftError(draft)).toContain('不能添加引用')
    draft[0].blocks[0].kind = 'claim'
    expect(draftError(draft)).toContain('需要完整原句')
  })
})
