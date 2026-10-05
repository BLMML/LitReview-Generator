import type { ReviewRevision, Review, Run, Validation, Job, Section } from './types/contracts'
export const MISSING_SOURCE_CONTENT = '提供的素材中没有该内容。'
export const commentaryKeys = ['achievements', 'limitations', 'innovation']
export const analysisRoles: Record<string, Record<string, string>> = {
  achievements: { achievement: '已取得成果分析' },
  limitations: { viewpoint_comparison: '争议与观点对比', root_cause: '争议成因分析' },
  innovation: {
    research_gap: '研究空白',
    future_direction: '未来突破方向',
    innovation: '本文拟开展的创新点',
  },
}
export function hasCommentary(sections: Section[]) {
  return sections.some((s) => s.section_key === 'limitations' || s.section_key === 'innovation')
}
export function isCommentarySection(sections: Section[], key: string) {
  return hasCommentary(sections) && commentaryKeys.includes(key)
}
export function commentaryNotices(sections: Section[], findings?: Validation['blocks']) {
  if (!hasCommentary(sections)) return []
  const supported = findings
    ? new Set(
        findings
          .filter((b) => b.support === 'supported' && b.structure_ok && !b.issues.length)
          .map((b) => b.block_id),
      )
    : null
  const blocks = sections
    .flatMap((s) => s.blocks)
    .filter((b) => !supported || supported.has(b.block_id))
  const paired = new Set(
    blocks
      .filter(
        (b) =>
          b.analysis_role === 'viewpoint_comparison' && b.kind === 'claim' && b.controversy_number,
      )
      .map((b) => b.controversy_number),
  )
  const causes = new Set(
    blocks
      .filter((b) => b.analysis_role === 'root_cause' && b.kind === 'claim')
      .map((b) => b.controversy_number),
  )
  const notices: string[] = []
  if ([...paired].filter((n) => causes.has(n)).length < 2)
    notices.push(
      '争议证据不足：尚未形成至少两个有依据的学术争议、观点对比及成因分析；导出的 Word 会明确标为述评草稿。',
    )
  const roles = new Set(blocks.filter((b) => b.kind === 'claim').map((b) => b.analysis_role))
  const required: Record<string, string> = {
    achievement: '已取得成果',
    research_gap: '研究空白',
    future_direction: '未来突破方向',
    innovation: '本文拟开展的创新点',
  }
  const missing = Object.entries(required)
    .filter(([role]) => !roles.has(role as NonNullable<Section['blocks'][number]['analysis_role']>))
    .map(([, label]) => label)
  if (missing.length) notices.push(`述评内容不足：${missing.join('、')}尚需补充材料与核对。`)
  return notices
}
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
  const basis = new Set(
    sections
      .filter((s) => !commentaryKeys.includes(s.section_key))
      .flatMap((s) => s.blocks.flatMap((b) => b.evidence_ids)),
  )
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
      if (isCommentarySection(sections, section.section_key) && b.kind === 'claim') {
        if (!b.analysis_role || !(b.analysis_role in analysisRoles[section.section_key]))
          return '请选择符合本述评模块的分析内容'
        if (b.analysis_role === 'viewpoint_comparison' || b.analysis_role === 'root_cause') {
          if (
            !Number.isInteger(b.controversy_number) ||
            !b.controversy_number ||
            b.controversy_number < 1 ||
            b.controversy_number > 20
          )
            return '请填写 1—20 的争议编号，同一争议的观点对比与成因使用相同编号'
        }
        if (b.evidence_ids.some((id) => !basis.has(id)))
          return '述评须选择前文原文模块已经引用的证据'
      } else if (sourceLayout && b.kind === 'claim') {
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
