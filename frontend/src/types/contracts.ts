import type * as C from './generated'
export type {
  User,
  Project,
  Paper,
  PaperPatch,
  PaperDelete,
  PaperDeletion,
  ProjectPatch,
  ProjectCreate,
  ProjectDelete,
  ProjectDeletion,
  RunPaper,
  PaperAnalysis,
  EvidenceText,
  Topic,
  Dispute,
  Evidence,
  Retrieval,
  Block,
  Section,
  ReviewRevision,
  Review,
  Validation,
  ValidationBlock,
  JobAccepted,
  ImportItem,
  ImportBatchResult,
  ErrorDetail,
  ReviewCreate,
} from './generated'
// The established serializers use a year map and nullable usage totals. Keep
// these explicit instead of converting an unknown token count to zero.
export type Run = Omit<C.Run, 'stats' | 'job_id'> & {
  job_id: string | null
  stats: Omit<C.RunStats, 'year_counts'> & {
    year_counts: Record<string, number> | C.RunStats['year_counts']
  }
}
export type Job = Omit<C.Job, 'usage'> & {
  usage: Omit<C.Job['usage'], 'input_tokens' | 'output_tokens'> & {
    input_tokens: number | null
    output_tokens: number | null
  }
}
export type Export = C.Export & {
  acknowledge_warnings?: boolean
  error?: C.ErrorDetail | null
  template_revision?: string
}
export interface Meta {
  request_id: string
  page?: number
  page_size?: number
  total?: number
}
export interface Envelope<T> {
  data: T
  meta: Meta
}
export const terminal = (status: Job['status']) =>
  ['succeeded', 'failed', 'cancelled'].includes(status)
