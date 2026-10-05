import { request, binary } from './client'
import type {
  User,
  Project,
  Paper,
  PaperPatch,
  PaperDeletion,
  ProjectPatch,
  ProjectCreate,
  ProjectDeletion,
  Run,
  PaperAnalysis,
  Topic,
  Dispute,
  Evidence,
  Retrieval,
  Review,
  ReviewRevision,
  Validation,
  Export,
  Job,
  JobAccepted,
  Section,
  ReviewCreate,
  Envelope,
} from '../types/contracts'
const get = <T>(url: string) => request<T>(url)
const post = <T>(url: string, body: unknown, key?: string) =>
  request<T>(url, { method: 'POST', body, key })
const patch = <T>(url: string, body: unknown) => request<T>(url, { method: 'PATCH', body })
const page = (n: number) => `?page=${n}&page_size=50`
export async function allPages<T>(load: (page: number) => Promise<Envelope<T[]>>) {
  const items: T[] = []
  for (let n = 1; ; n++) {
    const r = await load(n)
    items.push(...r.data)
    if (items.length >= (r.meta.total ?? items.length) || !r.data.length) return items
  }
}
export const api = {
  me: () => get<User>('/auth/me'),
  login: (username: string, password: string) => post<User>('/auth/login', { username, password }),
  logout: () => post<{ logged_out: boolean }>('/auth/logout', {}),
  projects: (n = 1) => get<Project[]>(`/projects${page(n)}`),
  createProject: (body: ProjectCreate) => post<Project>('/projects', body),
  project: (id: string) => get<Project>(`/projects/${id}`),
  patchProject: (id: string, body: ProjectPatch) => patch<Project>(`/projects/${id}`, body),
  deleteProject: (id: string, expected_version: number, expected_corpus_revision: number) =>
    request<ProjectDeletion>(`/projects/${id}`, {
      method: 'DELETE',
      body: { expected_version, expected_corpus_revision },
    }),
  papers: (id: string, n = 1) => get<Paper[]>(`/projects/${id}/papers${page(n)}`),
  paper: (id: string) => get<Paper>(`/papers/${id}`),
  patchPaper: (id: string, body: PaperPatch) => patch<Paper>(`/papers/${id}`, body),
  deletePaper: (id: string, expected_version: number) =>
    request<PaperDeletion>(`/papers/${id}`, { method: 'DELETE', body: { expected_version } }),
  upload: (id: string, file: File, title: string, key: string) => {
    const body = new FormData()
    body.append('file', file)
    if (title.trim()) body.append('title', title.trim())
    return post<Paper>(`/projects/${id}/papers/upload`, body, key)
  },
  importArxiv: (id: string, arxiv_ids: string[], key: string) =>
    post<JobAccepted>(`/projects/${id}/imports/arxiv`, { arxiv_ids }, key),
  runs: (id: string, n = 1) => get<Run[]>(`/projects/${id}/runs${page(n)}`),
  createRun: (id: string, key: string) => post<JobAccepted>(`/projects/${id}/runs`, {}, key),
  run: (id: string) => get<Run>(`/runs/${id}`),
  analysis: (run: string, paper: string) =>
    get<PaperAnalysis>(`/runs/${run}/papers/${paper}/analysis`),
  topics: (id: string, n = 1) => get<Topic[]>(`/runs/${id}/topics${page(n)}`),
  disputes: (id: string, n = 1) => get<Dispute[]>(`/runs/${id}/disputes${page(n)}`),
  patchDispute: (
    id: string,
    expected_version: number,
    review_status: Dispute['review_status'],
    review_note: string,
  ) => patch<Dispute>(`/disputes/${id}`, { expected_version, review_status, review_note }),
  evidence: (run: string, id: string) => get<Evidence>(`/runs/${run}/evidence/${id}`),
  paperEvidence: (run: string, paper: string, n = 1, pageNumber?: number) =>
    get<Evidence[]>(
      `/runs/${run}/papers/${paper}/evidence${page(n)}${pageNumber ? `&page_number=${pageNumber}` : ''}`,
    ),
  retrievals: (run: string, n = 1) => get<Retrieval[]>(`/runs/${run}/retrievals${page(n)}`),
  retrieval: (id: string) => get<Retrieval>(`/retrievals/${id}`),
  retrieve: (run: string, query: string, key: string) =>
    post<JobAccepted>(`/runs/${run}/retrievals`, { query, candidate_limit: 20, top_k: 5 }, key),
  reviews: (project: string, n = 1) => get<Review[]>(`/projects/${project}/reviews${page(n)}`),
  createReview: (run: string, body: ReviewCreate, key: string) =>
    post<JobAccepted>(`/runs/${run}/reviews`, body, key),
  review: (id: string) => get<Review>(`/reviews/${id}`),
  revisions: (id: string, n = 1) => get<ReviewRevision[]>(`/reviews/${id}/revisions${page(n)}`),
  revision: (review: string, id: string) =>
    get<ReviewRevision>(`/reviews/${review}/revisions/${id}`),
  saveRevision: (id: string, base_revision_number: number, sections: Section[]) =>
    post<ReviewRevision>(`/reviews/${id}/revisions`, { base_revision_number, sections }),
  validations: (id: string, n = 1) => get<Validation[]>(`/reviews/${id}/validations${page(n)}`),
  validation: (id: string) => get<Validation>(`/validations/${id}`),
  validate: (id: string, revision_id: string, key: string) =>
    post<JobAccepted>(`/reviews/${id}/validations`, { revision_id }, key),
  exports: (id: string, n = 1) => get<Export[]>(`/reviews/${id}/exports${page(n)}`),
  export: (id: string) => get<Export>(`/exports/${id}`),
  createExport: (
    id: string,
    revision_id: string,
    validation_id: string,
    acknowledge_warnings: boolean,
    key: string,
  ) =>
    post<JobAccepted>(
      `/reviews/${id}/exports`,
      { revision_id, validation_id, acknowledge_warnings },
      key,
    ),
  job: (id: string) => get<Job>(`/jobs/${id}`),
  jobs: (project: string, n = 1) => get<Job[]>(`/projects/${project}/jobs${page(n)}`),
  cancelJob: (id: string) => post<Job>(`/jobs/${id}/cancel`, {}),
  retryJob: (id: string, key: string) => post<JobAccepted>(`/jobs/${id}/retry`, {}, key),
  file: (url: string, signal?: AbortSignal) => binary(url, signal),
}
export async function download(url: string, filename: string, signal?: AbortSignal) {
  const blob = await api.file(url, signal)
  if (signal?.aborted) return
  const object = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = object
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(object), 1000)
}

// Every consumer must see the same API object after a development update.
// Reload instead of leaving already mounted or lazy-loaded pages on an old object.
if (import.meta.hot) {
  import.meta.hot.accept(() => window.location.reload())
}
