import { ref } from 'vue'
import { api, allPages } from '../api/resources'
import { errorMessage } from '../api/client'
import type { Evidence, RunPaper, Paper, Run } from '../types/contracts'
export function useEvidence(getRun: () => Run | null) {
  const evidence = ref<Evidence | null>(null),
    sourcePaper = ref<RunPaper | Paper | null>(null),
    fileUrl = ref(''),
    page = ref(1),
    open = ref(false),
    loading = ref(false),
    error = ref('')
  const candidates = ref<Evidence[]>([]),
    paperId = ref(''),
    pageNumber = ref<number | ''>('')
  let token = 0
  async function show(id: string) {
    const run = getRun()
    if (!run) return
    const ticket = ++token
    loading.value = true
    error.value = ''
    try {
      const e = (await api.evidence(run.id, id)).data
      if (ticket !== token) return
      evidence.value = e
      sourcePaper.value = run.papers.find((p) => p.paper_id === e.paper_id) ?? null
      page.value = e.page_number
      fileUrl.value = ''
      open.value = true
    } catch (e) {
      if (ticket === token) error.value = errorMessage(e)
    } finally {
      if (ticket === token) loading.value = false
    }
  }
  function source(paper: Paper) {
    token++
    sourcePaper.value = paper
    evidence.value = null
    fileUrl.value = paper.file_url
    page.value = 1
    open.value = true
  }
  async function browse(id = paperId.value) {
    const run = getRun()
    if (!run || !id) return
    const ticket = ++token
    paperId.value = id
    loading.value = true
    error.value = ''
    candidates.value = []
    try {
      const list = await allPages((n) =>
        api.paperEvidence(
          run.id,
          id,
          n,
          pageNumber.value === '' ? undefined : Number(pageNumber.value),
        ),
      )
      if (ticket === token) candidates.value = list
    } catch (e) {
      if (ticket === token) error.value = errorMessage(e)
    } finally {
      if (ticket === token) loading.value = false
    }
  }
  function clear() {
    token++
    evidence.value = null
    sourcePaper.value = null
    candidates.value = []
    paperId.value = ''
    pageNumber.value = ''
    open.value = false
    error.value = ''
    loading.value = false
  }
  return {
    evidence,
    sourcePaper,
    fileUrl,
    page,
    open,
    loading,
    error,
    candidates,
    paperId,
    pageNumber,
    show,
    source,
    browse,
    clear,
  }
}
