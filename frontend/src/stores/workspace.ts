import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, allPages } from '../api/resources'
import { ApiError, errorMessage } from '../api/client'
import {
  terminal,
  type Project,
  type Paper,
  type Run,
  type Job,
  type Topic,
  type Dispute,
  type Retrieval,
  type Review,
} from '../types/contracts'
export const useWorkspace = defineStore('workspace', () => {
  const project = ref<Project | null>(null),
    papers = ref<Paper[]>([]),
    runs = ref<Run[]>([]),
    reviews = ref<Review[]>([]),
    jobs = ref<Job[]>([])
  const run = ref<Run | null>(null),
    topics = ref<Topic[]>([]),
    disputes = ref<Dispute[]>([]),
    retrievals = ref<Retrieval[]>([])
  const job = ref<Job | null>(null),
    loading = ref(false),
    error = ref(''),
    pollError = ref(''),
    runLoading = ref(false)
  const busy = computed(
    () => !!project.value?.active_job_id || !!(job.value && !terminal(job.value.status)),
  )
  let timer: ReturnType<typeof setTimeout> | undefined,
    epoch = 0,
    runEpoch = 0
  async function refresh() {
    if (!project.value) return
    const id = project.value.id
    const values = await Promise.all([
      api.project(id),
      allPages((n) => api.papers(id, n)),
      allPages((n) => api.runs(id, n)),
      allPages((n) => api.reviews(id, n)),
      allPages((n) => api.jobs(id, n)),
    ])
    if (project.value?.id !== id) return
    project.value = values[0].data
    papers.value = values[1]
    runs.value = values[2]
    reviews.value = values[3]
    jobs.value = values[4]
    if (!run.value && project.value.latest_run_id) await selectRun(project.value.latest_run_id)
    else if (run.value) await selectRun(run.value.id)
  }
  async function open(id: string) {
    stop()
    loading.value = true
    error.value = ''
    pollError.value = ''
    job.value = null
    const ticket = epoch
    project.value = null
    run.value = null
    papers.value = []
    runs.value = []
    jobs.value = []
    reviews.value = []
    topics.value = []
    disputes.value = []
    retrievals.value = []
    try {
      const p = (await api.project(id)).data
      if (ticket !== epoch) return
      project.value = p
      await refresh()
      if (ticket !== epoch) return
      if (project.value.active_job_id) watchJob(project.value.active_job_id)
    } catch (e) {
      if (ticket === epoch) error.value = errorMessage(e)
    } finally {
      if (ticket === epoch) loading.value = false
    }
  }
  async function selectRun(id: string) {
    const token = ++runEpoch
    runLoading.value = true
    try {
      const r = (await api.run(id)).data
      const [ts, ds, rs] = await Promise.all([
        r.status === 'ready' ? allPages((n) => api.topics(id, n)) : [],
        r.status === 'ready' ? allPages((n) => api.disputes(id, n)) : [],
        allPages((n) => api.retrievals(id, n)),
      ])
      if (token !== runEpoch) return
      run.value = r
      topics.value = ts
      disputes.value = ds
      retrievals.value = rs
    } finally {
      if (token === runEpoch) runLoading.value = false
    }
  }
  function stop() {
    epoch++
    clearTimeout(timer)
    timer = undefined
  }
  function reset() {
    stop()
    runEpoch++
    project.value = null
    run.value = null
    papers.value = []
    runs.value = []
    reviews.value = []
    jobs.value = []
    topics.value = []
    disputes.value = []
    retrievals.value = []
    job.value = null
    error.value = ''
    pollError.value = ''
  }
  function watchJob(id: string) {
    stop()
    const token = epoch
    pollError.value = ''
    const tick = async () => {
      if (token !== epoch) return
      let delay = document.hidden ? 10_000 : 2000
      try {
        const result = (await api.job(id)).data
        if (token !== epoch) return
        job.value = result
        pollError.value = ''
        if (terminal(result.status)) {
          await refresh()
          if (token !== epoch) return
          if (project.value?.active_job_id && project.value.active_job_id !== id)
            watchJob(project.value.active_job_id)
          return
        }
      } catch (e) {
        if (token !== epoch) return
        if (e instanceof ApiError && [401, 404].includes(e.status)) {
          pollError.value = errorMessage(e)
          return
        }
        pollError.value = `${errorMessage(e)} 任务状态仍以服务器为准，正在重连。`
        delay = Math.max(delay, e instanceof ApiError ? e.retryAfter : 0, 5000)
      }
      if (token === epoch) timer = setTimeout(tick, delay)
    }
    void tick()
  }
  function accepted(id: string) {
    if (project.value) project.value.active_job_id = id
    watchJob(id)
  }
  return {
    project,
    papers,
    runs,
    reviews,
    jobs,
    run,
    topics,
    disputes,
    retrievals,
    job,
    loading,
    runLoading,
    error,
    pollError,
    busy,
    open,
    refresh,
    selectRun,
    stop,
    reset,
    watchJob,
    accepted,
  }
})
