import type { ReviewRevision, Review, Run, Validation, Job, Section } from './types/contracts'
export const MISSING_SOURCE_CONTENT = '提供的素材中没有该内容。'
export function exportWarnings(
  revision: ReviewRevision | null,
  review: Review | null,
  run: Run | null,
  report: Validation | null,
) {
  const warnings: string[] = []
  if (report?.result === 'needs_review') warnings.push('核验结果为 needs_review，正文存在待审问题')
  if (run?.is_stale) warnings.push('Run 语料已过时，导出使用历史语料快照')
  if (revision?.is_stale) warnings.push('分析已过时，正文仍基于历史分析版本')
  if (revision && review?.latest_revision?.id !== revision.id)
    warnings.push('导出的正文不是最新版本')
  return warnings
}
export function exportAllowed(
  revision: ReviewRevision | null,
  report: Validation | null,
  job: Job | undefined,
  dirty: boolean,
) {
  return (
    !!revision &&
    !dirty &&
    report?.revision_id === revision.id &&
    report.result !== null &&
    report.structure_passed === true &&
    report.blocks !== null &&
    job?.status === 'succeeded'
  )
}
export function draftError(sections: Section[]) {
  const sourceLayout = sections.some((s) => s.section_key === 'significance')
  if (sections.reduce((n, s) => n + s.blocks.length, 0) > 100) return '正文最多 100 个块'
  for (const section of sections) {
    if (!section.title.trim()) return '章节标题不能为空'
    if (!section.blocks.length) return '每节至少保留一个块'
    for (const b of section.blocks) {
      if (sourceLayout && b.kind === 'transition') {
        if (
          section.blocks.length !== 1 ||
          b.text !== MISSING_SOURCE_CONTENT ||
          b.evidence_ids.length
        )
          return '缺失说明只能单独使用“提供的素材中没有该内容。”，不能添加引用；有原文时请改为原文正文'
      }
      if (sourceLayout && b.kind === 'claim') {
        if (b.text === MISSING_SOURCE_CONTENT)
          return '原文正文需要完整原句，缺失说明请使用缺失说明类型'
        if (b.evidence_ids.length !== 1) return '每个原文正文块须引用一条原文证据'
      }
      if (!b.text.trim() || b.text.length > 600) return '每个正文块需要 1—600 字'
      if (b.kind === 'claim' && !b.evidence_ids.length) return '每条主张至少引用一条原文证据'
    }
  }
  return ''
}
