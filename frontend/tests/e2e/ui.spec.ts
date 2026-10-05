import { test, expect, type Page, type Route } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { mkdirSync } from 'node:fs'
import type {
  Project,
  Run,
  Review,
  ReviewRevision,
  Section,
  Validation,
  Job,
  Paper,
} from '../../src/types/contracts'
const uid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
test('login raw aesthetic at desktop and mobile without a simulated signed-in session', async ({
  page,
}) => {
  await page.route('**/api/v1/auth/csrf', (r) =>
    r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        data: { csrf_token: 'login-test' },
        meta: { request_id: 'req_login' },
      }),
    }),
  )
  await page.route('**/api/v1/auth/me', (r) =>
    r.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({
        error: { code: 'AUTH_REQUIRED', message: '请先登录', details: {}, retryable: false },
        meta: { request_id: 'req_login' },
      }),
    }),
  )
  await page.goto('/login')
  mkdirSync('../.data/m12-qa', { recursive: true })
  await expect(page.getByRole('heading', { name: '进入工作台' })).toBeVisible()
  await page.screenshot({ path: '../.data/m12-qa/login-desktop.png', fullPage: true })
  await page.setViewportSize({ width: 375, height: 900 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: '../.data/m12-qa/login-mobile.png', fullPage: true })
})
const now = '2026-10-02T00:00:00Z',
  projectId = uid(1),
  runId = uid(2),
  reviewId = uid(3)
const sections: Section[] = (
  [
    'background',
    'significance',
    'approach',
    'theoretical_basis',
    'research_status',
    'achievements',
  ] as const
).map((key, i) => ({
  section_key: key,
  title: ['研究背景', '研究意义', '研究思路', '理论基础', '国内外研究现状', '已取得研究成果'][i],
  blocks: [
    {
      block_id: uid(10 + i),
      kind: 'claim',
      text: '这是测试夹具的正文，需要回看证据。',
      evidence_ids: [uid(30)],
    },
  ],
}))
function makeJob(id: string, status: Job['status'], kind: Job['kind'] = 'validate_review'): Job {
  return {
    id,
    project_id: projectId,
    kind,
    status,
    stage: 'verify_blocks',
    progress: { completed: 5, total: 5, unit: 'blocks' },
    resource_type: 'validation_report',
    resource_id: uid(40),
    retry_of_job_id: null,
    usage: {
      input_tokens: 12345,
      output_tokens: 6789,
      estimated_cost_usd: '1.234567',
      unsettled_call_count: 1,
    },
    error: null,
    result: null,
    created_at: now,
    started_at: now,
    finished_at: status === 'succeeded' ? now : null,
  }
}
async function fixture(
  page: Page,
  options: {
    busy?: boolean
    stale?: boolean
    conflict?: boolean
    failDownload?: boolean
    papers?: boolean
    deleteConflict?: boolean
    projectDeleteConflict?: boolean
    projectTotal?: number
  } = {},
) {
  const project: Project = {
    id: projectId,
    title: 'Transformer 研究手稿 / 测试夹具',
    research_question: '当前项目问题',
    output_language: 'zh-CN',
    version: 1,
    corpus_revision: options.stale ? 2 : 1,
    latest_run_id: runId,
    active_job_id: options.busy ? uid(51) : null,
    created_at: now,
    updated_at: now,
  }
  const run: Run = {
    id: runId,
    project_id: projectId,
    job_id: uid(50),
    research_question: '冻结的问题：Transformer 如何学习语言表示？',
    output_language: 'zh-CN',
    status: 'ready',
    corpus_revision: 1,
    analysis_revision: 1,
    is_stale: !!options.stale,
    created_at: now,
    papers: [
      {
        paper_id: uid(4),
        title: '注意力机制 / 测试 PDF',
        authors: ['Test Author'],
        year: 2017,
        page_count: 2,
        status: 'ready',
        parsed_document_id: uid(5),
        error: null,
      },
    ],
    stats: {
      included_count: 1,
      ready_count: 1,
      failed_count: 0,
      year_counts: { '2017': 1 },
      unknown_year_count: 0,
    },
    error: null,
  }
  const revision: ReviewRevision = {
    id: uid(20),
    review_id: reviewId,
    run_id: runId,
    revision_number: 1,
    analysis_revision: 1,
    sections,
    source: 'generated',
    is_stale: false,
    created_by: '1',
    created_at: now,
  }
  const newer = { ...revision, id: uid(21), revision_number: 2, source: 'manual' as const }
  const review: Review = {
    id: reviewId,
    project_id: projectId,
    run_id: runId,
    title: '注意力机制研究手稿 / 测试夹具',
    target_chars: 3000,
    style: 'standard',
    generation_job_id: uid(52),
    latest_revision: newer,
    created_at: now,
  }
  const report: Validation = {
    id: uid(40),
    revision_id: newer.id,
    job_id: uid(50),
    result: 'needs_review',
    structure_passed: true,
    blocks: sections
      .flatMap((s) => s.blocks)
      .map((b) => ({
        block_id: b.block_id,
        structure_ok: true,
        support: 'uncertain' as const,
        issues: ['模拟核验未判断事实支持'],
        suggestion: null,
      })),
    created_at: now,
  }
  const jobs = [
    makeJob(uid(50), 'succeeded'),
    makeJob(uid(51), 'cancelling', 'analyze'),
    makeJob(uid(52), 'succeeded', 'generate_review'),
  ]
  const papers: Paper[] = options.papers
    ? [
        {
          id: uid(4),
          project_id: projectId,
          title: '注意力机制 / 测试 PDF',
          authors: ['Test Author'],
          year: 2017,
          source: 'upload',
          source_id: null,
          source_url: null,
          included: true,
          version: 1,
          file_sha256: '0'.repeat(64),
          file_size: 1234,
          page_count: 2,
          original_filename: 'evidence.pdf',
          file_url: `/api/v1/papers/${uid(4)}/file`,
          created_at: now,
          updated_at: now,
        },
      ]
    : []
  let projectDeleted = false
  const writes: { method: string; path: string; body: unknown; key?: string }[] = []
  const ok = (route: Route, data: unknown, list = false) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        data,
        meta: {
          request_id: 'req_ui_fixture',
          ...(list ? { page: 1, page_size: 50, total: Array.isArray(data) ? data.length : 0 } : {}),
        },
      }),
    })
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    if (route.request().method() !== 'GET') {
      writes.push({
        method: route.request().method(),
        path,
        body: route.request().postDataJSON(),
        key: route.request().headers()['idempotency-key'],
      })
      if (route.request().method() === 'DELETE' && path === `/projects/${projectId}`) {
        if (options.projectDeleteConflict)
          return route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({
              error: {
                code: 'VERSION_CONFLICT',
                message: '项目或文献已更新，请重新读取项目后再确认删除',
                details: {},
                retryable: false,
              },
              meta: { request_id: 'req_project_delete_conflict' },
            }),
          })
        projectDeleted = true
        return ok(route, { project_id: projectId, deleted: true })
      }
      if (route.request().method() === 'DELETE' && path === `/papers/${uid(4)}`) {
        if (options.deleteConflict)
          return route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({
              error: {
                code: 'VERSION_CONFLICT',
                message: '论文已被修改，请刷新后重试',
                details: {},
                retryable: false,
              },
              meta: { request_id: 'req_delete_conflict' },
            }),
          })
        papers.splice(0)
        project.corpus_revision += 1
        run.is_stale = true
        return ok(route, {
          paper_id: uid(4),
          deleted: true,
          corpus_revision: project.corpus_revision,
        })
      }
      if (path.endsWith('/revisions') && options.conflict)
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            error: {
              code: 'VERSION_CONFLICT',
              message: '内容已被修改，请刷新后重试',
              details: {},
              retryable: false,
            },
            meta: { request_id: 'req_conflict' },
          }),
        })
      if (path.endsWith('/exports'))
        return ok(route, { job_id: uid(60), resource_type: 'export', resource_id: uid(61) })
      return ok(route, {})
    }
    if (path === '/auth/csrf') return ok(route, { csrf_token: 'fixture' })
    if (path === '/auth/me')
      return ok(route, { id: '1', username: 'fixture', display_name: '联调用户' })
    if (path === '/projects') {
      if (options.projectTotal === 51) {
        const n = new URL(route.request().url()).searchParams.get('page')
        const others = Array.from({ length: 50 }, (_, i) => ({
          ...project,
          id: uid(1000 + i),
          title: `其他项目 ${i + 1}`,
        }))
        return route.fulfill({
          contentType: 'application/json',
          body: JSON.stringify({
            data: n === '2' ? (projectDeleted ? [] : [project]) : others,
            meta: {
              request_id: 'req_page',
              total: projectDeleted ? 50 : 51,
              page: Number(n),
              page_size: 50,
            },
          }),
        })
      }
      return ok(route, projectDeleted ? [] : [project], true)
    }
    if (path === `/projects/${projectId}`) return ok(route, project)
    if (path.endsWith('/papers')) return ok(route, papers, true)
    if (path.endsWith('/runs')) return ok(route, [run], true)
    if (path === `/runs/${runId}`) return ok(route, run)
    if (path.endsWith('/topics'))
      return ok(
        route,
        [
          {
            id: uid(70),
            run_id: runId,
            name: '表示学习',
            description: '一个测试主题',
            paper_ids: [uid(4)],
            evidence_ids: [uid(30)],
          },
        ],
        true,
      )
    if (path.endsWith('/disputes')) return ok(route, [], true)
    if (path.endsWith('/retrievals'))
      return ok(
        route,
        [
          {
            id: uid(80),
            run_id: runId,
            job_id: uid(50),
            query: 'attention representations',
            top_k: 5,
            candidate_limit: 20,
            created_at: now,
            results: [
              { evidence_id: uid(30), rank: 1, score: 0.9, reason: '与 attention 相关的原文片段' },
            ],
          },
        ],
        true,
      )
    if (path.endsWith(`/evidence/${uid(30)}`))
      return ok(route, {
        id: uid(30),
        run_id: runId,
        paper_id: uid(4),
        parsed_document_id: uid(5),
        page_number: 2,
        text: 'Evidence on page two.',
        section: 'Method',
        element_type: 'text',
        locator: { granularity: 'page', bbox: null },
        verification: 'text_matched',
        file_url: `/api/v1/papers/${uid(4)}/file`,
      })
    if (path.endsWith('/file')) {
      if (options.failDownload)
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({
            error: { code: 'AUTH_REQUIRED', message: '请先登录', details: {}, retryable: false },
            meta: { request_id: 'req_expired' },
          }),
        })
      return route.fulfill({
        contentType: 'application/pdf',
        body: readFileSync(new URL('../fixtures/evidence.pdf', import.meta.url)),
      })
    }
    if (path.endsWith('/reviews')) return ok(route, [review], true)
    if (path === `/reviews/${reviewId}`) return ok(route, review)
    if (path.endsWith('/revisions')) return ok(route, [newer, revision], true)
    if (path.endsWith('/validations')) return ok(route, [report], true)
    if (path.endsWith('/exports')) return ok(route, [], true)
    if (path.endsWith('/jobs')) return ok(route, jobs, true)
    if (path.startsWith('/jobs/'))
      return ok(
        route,
        jobs.find((j) => j.id === path.split('/')[2]) ?? makeJob(uid(60), 'running', 'export_docx'),
      )
    return route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({
        error: { code: 'NOT_FOUND', message: '不可用', details: {}, retryable: false },
        meta: { request_id: 'req_fixture' },
      }),
    })
  })
  return { writes, revision, newer, project }
}

test('project deletion confirms scope, cancels safely and persists after refresh', async ({
  page,
}) => {
  const { writes, project } = await fixture(page)
  await page.goto('/projects')
  const remove = page.getByRole('button', { name: '删除项目', exact: true })
  await remove.click()
  const dialog = page.getByRole('dialog', { name: '删除这个项目？' })
  await expect(dialog.getByText(project.title, { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '保留项目' })).toBeFocused()
  expect(page.url()).toMatch(/\/projects$/)
  await dialog.getByRole('button', { name: '保留项目' }).click()
  expect(writes).toEqual([])
  await expect(remove).toBeFocused()
  await remove.click()
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  expect(writes).toEqual([])
  await remove.click()
  mkdirSync('../.data/m12-qa', { recursive: true })
  await page.screenshot({ path: '../.data/m12-qa/project-delete-dialog-desktop.png' })
  await page.setViewportSize({ width: 375, height: 900 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: '../.data/m12-qa/project-delete-dialog-mobile.png' })
  await dialog.getByRole('button', { name: '确认删除项目' }).click()
  await expect(page.getByText('项目已删除，已从项目档案移除。')).toBeVisible()
  await expect(remove).toHaveCount(0)
  await expect(page.getByText('0 本项目手稿')).toBeVisible()
  expect(writes).toEqual([
    {
      method: 'DELETE',
      path: `/projects/${projectId}`,
      body: { expected_version: 1, expected_corpus_revision: 1 },
      key: undefined,
    },
  ])
  await page.reload()
  await expect(page.locator('.project-sheet')).toHaveCount(0)
})

test('active project tasks disable deletion without disabling navigation', async ({ page }) => {
  const { writes, project } = await fixture(page, { busy: true })
  await page.goto('/projects')
  await expect(page.getByRole('button', { name: '删除项目', exact: true })).toBeDisabled()
  await page.getByRole('link').filter({ hasText: project.title }).click()
  await expect(page.getByRole('heading', { name: '论文账本' })).toBeVisible()
  expect(writes).toEqual([])
})

test('a project deletion conflict keeps the project visible', async ({ page }) => {
  await fixture(page, { projectDeleteConflict: true })
  await page.goto('/projects')
  await page.getByRole('button', { name: '删除项目', exact: true }).click()
  await page.getByRole('button', { name: '确认删除项目' }).click()
  await expect(page.getByText('项目或文献已更新，请重新读取项目后再确认删除')).toBeVisible()
  await expect(page.locator('.project-sheet')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '删除项目', exact: true })).toBeEnabled()
})

test('deleting the last project on page two returns to the previous page', async ({ page }) => {
  await fixture(page, { projectTotal: 51 })
  await page.goto('/projects')
  await page.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(page.getByText('第 2 页', { exact: true })).toBeVisible()
  await expect(page.locator('.project-sheet')).toHaveCount(1)
  await page.getByRole('button', { name: '删除项目', exact: true }).click()
  await page.getByRole('button', { name: '确认删除项目' }).click()
  await expect(page.locator('.project-sheet')).toHaveCount(50)
  await expect(page.getByText('50 本项目手稿')).toBeVisible()
  await expect(page.getByRole('button', { name: '下一页', exact: true })).toHaveCount(0)
})

test('paper deletion requires confirmation, refreshes the ledger and marks history stale', async ({
  page,
}) => {
  const { writes } = await fixture(page, { papers: true })
  await page.goto(`/projects/${projectId}`)
  const remove = page.getByRole('button', { name: '删除论文', exact: true })
  await expect(remove).toBeVisible()
  await remove.click()
  const dialog = page.getByRole('dialog', { name: '删除这篇论文？' })
  await expect(dialog.getByText('注意力机制 / 测试 PDF', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '保留论文' })).toBeFocused()
  await dialog.getByRole('button', { name: '保留论文' }).click()
  expect(writes).toEqual([])
  await expect(remove).toBeFocused()
  await remove.click()
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  expect(writes).toEqual([])
  await remove.click()
  mkdirSync('../.data/m12-qa', { recursive: true })
  await page.screenshot({ path: '../.data/m12-qa/paper-delete-dialog-desktop.png' })
  await page.setViewportSize({ width: 375, height: 900 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.screenshot({ path: '../.data/m12-qa/paper-delete-dialog-mobile.png' })
  await dialog.getByRole('button', { name: '确认删除' }).click()
  await expect(remove).toHaveCount(0)
  await expect(page.getByText('论文已从当前账本删除，历史分析与引用已保留。')).toBeVisible()
  await expect(page.getByText('还没有论文。上传 PDF 或导入 arXiv，开始建立语料。')).toBeVisible()
  await expect(page.getByLabel('分析历史')).toContainText('/ 已过时')
  expect(writes).toEqual([
    { method: 'DELETE', path: `/papers/${uid(4)}`, body: { expected_version: 1 }, key: undefined },
  ])
  await page.reload()
  await expect(remove).toHaveCount(0)
})

test('a deletion conflict keeps the paper and permits another attempt', async ({ page }) => {
  await fixture(page, { papers: true, deleteConflict: true })
  await page.goto(`/projects/${projectId}`)
  await page.getByRole('button', { name: '删除论文', exact: true }).click()
  await page.getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByText('论文已被修改，请刷新后重试')).toBeVisible()
  await expect(page.locator('.paper-list h3')).toHaveText('注意力机制 / 测试 PDF')
  await expect(page.getByRole('button', { name: '删除论文', exact: true })).toBeEnabled()
})

test('active tasks disable paper deletion while keeping the source readable', async ({ page }) => {
  const { writes } = await fixture(page, { papers: true, busy: true })
  await page.goto(`/projects/${projectId}`)
  await expect(page.getByRole('button', { name: '删除论文', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '阅读原文 ↗' })).toBeEnabled()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  expect(writes).toEqual([])
})
test('version-bound report: warnings acknowledged, reset on history switch, frozen question shown', async ({
  page,
}) => {
  const { writes, revision, newer } = await fixture(page)
  await page.goto(`/reviews/${reviewId}`)
  await expect(
    page.getByText('冻结的问题：Transformer 如何学习语言表示？', { exact: true }),
  ).toBeVisible()
  const download = page.getByRole('button', { name: '导出正文 v2 ↓' })
  await expect(download).toBeDisabled()
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  await expect(download).toBeEnabled()
  await page.getByLabel('正文版本', { exact: true }).selectOption(revision.id)
  await expect(page.getByRole('button', { name: '导出正文 v1 ↓' })).toBeDisabled()
  await page.getByLabel('正文版本', { exact: true }).selectOption(newer.id)
  await expect(page.getByLabel('我已阅读这些警示，仍要导出此版本')).not.toBeChecked()
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  await download.click()
  expect(writes[0]).toMatchObject({
    body: { revision_id: newer.id, validation_id: uid(40), acknowledge_warnings: true },
  })
  expect(writes[0].key).toMatch(/^[0-9a-f-]{36}$/)
})
test('the fixed research framework locks titles and permits one source per block', async ({
  page,
}) => {
  await fixture(page)
  await page.goto(`/reviews/${reviewId}`)
  await expect(page.locator('.heading-input')).toHaveCount(6)
  const titles = [
    '研究背景',
    '研究意义',
    '研究思路',
    '理论基础',
    '国内外研究现状',
    '已取得研究成果',
  ]
  for (let i = 0; i < titles.length; i++) {
    await expect(page.locator('.heading-input').nth(i)).toHaveValue(titles[i])
    await expect(page.locator('.heading-input').nth(i)).toBeDisabled()
  }
  await expect(page.getByRole('heading', { name: '04 文献综述', exact: true })).toBeVisible()
  await expect(page.locator('.section-number')).toHaveText(['01', '02', '03', '04.1', '04.2', '05'])
  await expect(page.getByRole('button', { name: '+ 选择原文证据' }).first()).toBeDisabled()
})

test('a save conflict preserves local text and unsaved route exit can be cancelled', async ({
  page,
}) => {
  await fixture(page, { conflict: true })
  await page.goto(`/reviews/${reviewId}`)
  const text = page.getByLabel('研究背景 第 1 块正文')
  await text.fill('此草稿不能丢失。')
  await page.getByRole('button', { name: '保存为新版本 →' }).click()
  await expect(page.getByText('内容已被修改，请刷新后重试')).toBeVisible()
  await expect(text).toHaveValue('此草稿不能丢失。')
  page.once('dialog', (dialog) => dialog.dismiss())
  await page.getByRole('link', { name: '项目档案', exact: true }).last().click()
  await expect(text).toHaveValue('此草稿不能丢失。')
  expect(new URL(page.url()).pathname).toBe(`/reviews/${reviewId}`)
})
test('reload restores a cancelling task, keeps reads and disables mutations', async ({ page }) => {
  await fixture(page, { busy: true })
  await page.goto(`/projects/${projectId}`)
  await expect(page.getByText('正在取消', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '等待取消完成…' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '修改项目问题' })).toBeDisabled()
  await page.reload()
  await expect(page.getByText('正在取消', { exact: true })).toBeVisible()
  await expect(page.locator('.job-panel').first()).not.toContainText(
    /tokens|已记账估算|尚未结算|费用未确定|12345|6789|1\.234567/,
  )
})
test('stale history stays readable and PDF goes to evidence page 2', async ({ page }) => {
  await fixture(page, { stale: true })
  await page.goto(`/projects/${projectId}?tab=search`)
  await expect(page.getByRole('button', { name: '检索证据 →' })).toBeDisabled()
  await page.getByRole('button', { name: '回看原文 ↗' }).click()
  await expect(page.getByLabel('原文页码', { exact: true })).toHaveValue('2')
  await expect(page.locator('canvas')).toHaveAttribute('width', /[1-9]/)
  await page.getByRole('button', { name: '上一页', exact: true }).click()
  await expect(page.getByLabel('原文页码', { exact: true })).toHaveValue('1')
})
for (const width of [375, 768, 1024, 1440])
  test(`workspace responsive at ${width}px with visible keyboard focus`, async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.setViewportSize({ width, height: 1000 })
    await fixture(page)
    await page.goto(`/projects/${projectId}`)
    await expect(page.getByRole('heading', { name: '论文账本' })).toBeVisible()
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true)
    await page.keyboard.press('Tab')
    expect(
      await page.evaluate(() => getComputedStyle(document.activeElement!).outlineStyle),
    ).not.toBe('none')
    await page.screenshot({ path: `test-results/workspace-${width}.png`, fullPage: true })
    expect(errors).toEqual([])
  })

test('thirty included papers load across pages and submit one complete analysis', async ({
  page,
}) => {
  await fixture(page)
  const papers: Paper[] = Array.from({ length: 30 }, (_, i) => ({
    id: uid(2000 + i),
    project_id: projectId,
    title: `容量论文 ${i + 1}`,
    authors: [],
    year: 2020,
    source: 'upload',
    source_id: null,
    source_url: null,
    included: true,
    version: 1,
    file_sha256: String(i).padStart(64, '0'),
    file_size: 1234,
    page_count: 1,
    original_filename: `capacity-${i + 1}.pdf`,
    file_url: `/api/v1/papers/${uid(2000 + i)}/file`,
    created_at: now,
    updated_at: now,
  }))
  const readPages: number[] = []
  await page.route(`**/api/v1/projects/${projectId}/papers?*`, (route) => {
    const number = Number(new URL(route.request().url()).searchParams.get('page') || 1)
    readPages.push(number)
    return route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        data: papers.slice((number - 1) * 20, number * 20),
        meta: { request_id: 'req_capacity', page: number, page_size: 20, total: 30 },
      }),
    })
  })
  const writes: unknown[] = []
  await page.route(`**/api/v1/projects/${projectId}/runs`, (route) => {
    if (route.request().method() === 'GET') return route.fallback()
    writes.push(route.request().postDataJSON())
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        data: { job_id: uid(50), resource_id: runId, resource_type: 'analysis_run' },
        meta: { request_id: 'req_capacity' },
      }),
    })
  })
  await page.goto(`/projects/${projectId}`)
  await expect(page.getByText(/已纳入 30 \/ 30/)).toBeVisible()
  await expect(page.getByRole('heading', { name: '容量论文 30', exact: true })).toBeVisible()
  expect(readPages).toContain(2)
  const analyze = page.getByRole('button', { name: '重新分析文献 →', exact: true })
  await expect(analyze).toBeEnabled()
  await analyze.click()
  await expect.poll(() => writes.length).toBe(1)
  expect(writes).toEqual([{}]) // Backend freezes every included paper; no client-side slice.
})

async function exportDownloadFixture(
  page: Page,
  options: {
    existing?: boolean
    failDownloadOnce?: boolean
    rejectSubmission?: boolean
    rejectSubmissionOnce?: boolean
  } = {},
) {
  const { writes, newer } = await fixture(page)
  let created = false,
    reads = 0,
    files = 0
  const state = () => (options.existing || reads >= 2 ? 'ready' : 'pending')
  const exported = () => ({
    id: uid(61),
    review_id: reviewId,
    revision_id: newer.id,
    validation_id: uid(40),
    job_id: uid(60),
    status: state(),
    warnings: ['核验结果为 needs_review，正文存在待审问题'],
    file_url: state() === 'ready' ? `/api/v1/exports/${uid(61)}/file` : null,
    created_at: now,
  })
  const json = (route: Route, data: unknown, list = false) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        data,
        meta: {
          request_id: 'req_download',
          ...(list ? { page: 1, page_size: 50, total: (data as unknown[]).length } : {}),
        },
      }),
    })
  await page.route(`**/api/v1/reviews/${reviewId}/exports**`, (route) => {
    if (route.request().method() === 'POST') {
      writes.push({
        method: 'POST',
        path: `/reviews/${reviewId}/exports`,
        body: route.request().postDataJSON(),
        key: route.request().headers()['idempotency-key'],
      })
      const uncertain =
        options.rejectSubmissionOnce &&
        writes.filter((w) => w.path.endsWith('/exports')).length === 1
      if (uncertain) {
        created = true
        reads = 2
      }
      if (options.rejectSubmission || uncertain)
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({
            error: {
              code: 'INTERNAL_ERROR',
              message: '导出服务暂时不可用',
              details: {},
              retryable: true,
            },
            meta: { request_id: 'req_submit' },
          }),
        })
      created = true
      return json(route, { job_id: uid(60), resource_type: 'export', resource_id: uid(61) })
    }
    return json(route, created || options.existing ? [exported()] : [], true)
  })
  await page.route(`**/api/v1/exports/${uid(61)}**`, (route) => {
    if (new URL(route.request().url()).pathname.endsWith('/file')) {
      files++
      if (options.failDownloadOnce && files === 1)
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({
            error: {
              code: 'INTERNAL_ERROR',
              message: '下载连接中断',
              details: {},
              retryable: true,
            },
            meta: { request_id: 'req_file' },
          }),
        })
      return route.fulfill({
        contentType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        body: Buffer.from('PK-word-download-fixture'),
      })
    }
    reads++
    return json(route, exported())
  })
  await page.route(`**/api/v1/jobs/${uid(60)}`, (route) =>
    json(route, {
      ...makeJob(uid(60), state() === 'ready' ? 'succeeded' : 'running', 'export_docx'),
      resource_type: 'export',
      resource_id: uid(61),
    }),
  )
  return { writes, newer, files: () => files }
}

test('export automatically downloads once after the file becomes ready', async ({ page }) => {
  const { writes } = await exportDownloadFixture(page)
  await page.goto(`/reviews/${reviewId}`)
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  const file = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
  await expect(page.locator('.export-check').getByRole('status')).toContainText('完成后会自动下载')
  await expect(page.getByRole('button', { name: '正在导出 Word…' })).toBeDisabled()
  expect((await file).suggestedFilename()).toBe('综述-v2.docx')
  await expect(page.locator('.export-check').getByRole('status')).toContainText('下载已开始')
  expect(writes.filter((w) => w.path.endsWith('/exports'))).toHaveLength(1)
})

test('existing exports download on explicit clicks without creating duplicate Word jobs', async ({
  page,
}) => {
  const { writes, files } = await exportDownloadFixture(page, { existing: true })
  await page.goto(`/reviews/${reviewId}`)
  await expect(page.locator('.export-check')).toBeVisible()
  expect(files()).toBe(0)
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  for (let i = 0; i < 2; i++) {
    const file = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
    expect((await file).suggestedFilename()).toBe('综述-v2.docx')
    await expect(page.getByRole('button', { name: '导出正文 v2 ↓' })).toBeEnabled()
  }
  expect(files()).toBe(2)
  expect(writes).toHaveLength(0)
})

test('download failure is visible beside export and retry uses the same generated file', async ({
  page,
}) => {
  const { writes, files } = await exportDownloadFixture(page, {
    existing: true,
    failDownloadOnce: true,
  })
  await page.goto(`/reviews/${reviewId}`)
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
  await expect(page.locator('.export-check').getByRole('alert')).toContainText('下载连接中断')
  const file = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
  expect((await file).suggestedFilename()).toBe('综述-v2.docx')
  await expect(page.locator('.export-check').getByRole('alert')).toHaveCount(0)
  expect(files()).toBe(2)
  expect(writes).toHaveLength(0)
})

test('a rejected export request displays the error beside the clicked control', async ({
  page,
}) => {
  await exportDownloadFixture(page, { rejectSubmission: true })
  await page.goto(`/reviews/${reviewId}`)
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
  await expect(page.locator('.export-check').getByRole('alert')).toContainText('导出服务暂时不可用')
  await expect(page.getByRole('button', { name: '导出正文 v2 ↓' })).toBeEnabled()
})

test('an uncertain export response is resolved with its original key even when history shows a ready file', async ({
  page,
}) => {
  const { writes } = await exportDownloadFixture(page, { rejectSubmissionOnce: true })
  await page.goto(`/reviews/${reviewId}`)
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
  await expect(page.locator('.export-check').getByRole('alert')).toContainText('导出服务暂时不可用')
  await page.getByRole('button', { name: '重新读取服务器版本（保留草稿）' }).click()
  await expect(page.locator('.export-row')).toHaveCount(1)
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  const file = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出正文 v2 ↓' }).click()
  expect((await file).suggestedFilename()).toBe('综述-v2.docx')
  const submits = writes.filter((w) => w.path.endsWith('/exports'))
  expect(submits).toHaveLength(2)
  expect(submits[1].key).toBe(submits[0].key)
})
