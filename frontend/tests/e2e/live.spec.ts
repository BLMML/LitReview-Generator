import { test, expect } from '@playwright/test'
import path from 'node:path'
import { mkdirSync } from 'node:fs'
test('real HTTP: two real papers → analysis → evidence → edit → bound validation → warning export', async ({
  page,
}) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  const output = process.env.M12_QA_DIR!
  const inputFiles: string[] = JSON.parse(process.env.M12_INPUT_FILES!)
  const reviewTitle = process.env.M12_REVIEW_TITLE!
  mkdirSync(output, { recursive: true })
  await page.goto('/login')
  await page.getByLabel('用户名', { exact: true }).fill('m12-browser')
  await page.getByLabel('密码', { exact: true }).fill('m12-test-only')
  await page.getByRole('button', { name: '登录，打开手稿 →' }).click()
  await expect(page.getByRole('heading', { name: '项目档案' })).toBeVisible()
  await page.getByRole('button', { name: '+ 新建研究项目' }).click()
  await page.getByLabel('项目名称', { exact: true }).fill(process.env.M12_PROJECT_TITLE!)
  await page.getByLabel('研究问题', { exact: true }).fill(process.env.M12_RESEARCH_QUESTION!)
  await page.getByRole('button', { name: '创建项目 →' }).click()
  await expect(page.getByRole('heading', { name: '论文账本' })).toBeVisible()
  const workspaceUrl = page.url()
  for (const filename of inputFiles) {
    await page.getByLabel('本地 PDF').setInputFiles(filename)
    await page.getByRole('button', { name: '上传论文 ↑' }).click()
    await expect(page.getByText('论文已上传，重复文件会复用已有记录。')).toBeVisible()
  }
  await page.getByRole('button', { name: '开始分析文献 →' }).click()
  await expect(page.getByRole('button', { name: '重新分析文献 →' })).toBeEnabled({
    timeout: 90_000,
  })
  await page.reload()
  await page.getByRole('button', { name: '02 / 分析' }).click()
  await expect(page.getByRole('heading', { name: '模拟联调主题', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '要素与摘要' }).first().click()
  await expect(page.getByRole('heading', { name: '单篇分析', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '证据 1 ↗' }).first().click()
  await expect(page.getByRole('heading', { name: '原文在这里' })).toBeVisible()
  await expect(page.locator('canvas')).toHaveAttribute('width', /[1-9]/)
  await page.getByRole('button', { name: '03 / 证据' }).click()
  await page.getByLabel('在本次分析快照中检索').fill('社会工作 志愿服务 社区')
  await page.getByRole('button', { name: '检索证据 →' }).click()
  await expect(page.getByRole('button', { name: '回看原文 ↗' }).first()).toBeVisible({
    timeout: 90_000,
  })
  await page.getByRole('button', { name: '回看原文 ↗' }).first().click()
  await expect(page.locator('canvas')).toHaveAttribute('width', /[1-9]/)
  await page.getByRole('button', { name: '04 / 手稿' }).click()
  await page.getByLabel('综述标题').fill(reviewTitle)
  await page.getByRole('button', { name: '生成综述 →' }).click()
  await expect(page.getByRole('link', { name: new RegExp(reviewTitle) })).toBeVisible({
    timeout: 90_000,
  })
  const reviewLink = page.getByRole('link', { name: new RegExp(reviewTitle) })
  await expect(reviewLink.getByText('正文 v1')).toBeVisible({ timeout: 90_000 })
  await reviewLink.click()
  await expect(page.getByText('此版本已保存', { exact: true })).toBeVisible()
  const firstBlock = page.getByLabel('研究背景 第 1 块正文', { exact: true })
  const firstSection = page.locator('.section-sheet').first()
  const originalText = await firstBlock.inputValue()
  const hasEvidence = (await firstSection.getByRole('button', { name: '引用 1 ↗' }).count()) > 0
  if (hasEvidence) {
    await firstSection.getByRole('button', { name: '引用 1 ↗' }).first().click()
    await expect(page.getByRole('button', { name: '关闭' })).toBeVisible()
  }
  await firstSection.getByRole('button', { name: '+ 添加正文块' }).click()
  await page.getByLabel('研究背景 第 2 块正文', { exact: true }).fill(originalText)
  if (hasEvidence) {
    await firstSection.getByRole('button', { name: '+ 选择原文证据' }).nth(1).click()
    await page.getByRole('button', { name: '把这条证据加入当前块 +' }).click()
  } else {
    await firstSection.getByLabel('块 2 类型', { exact: true }).selectOption('transition')
  }
  await firstSection.getByRole('button', { name: '删除块', exact: true }).first().click()
  await expect(page.getByText('有未保存改动', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '核验正文 v1 →' })).toBeDisabled()
  await page.getByRole('button', { name: '保存为新版本 →' }).click()
  await expect(page.getByText('已保存正文 v2。需重新核验新版本。')).toBeVisible()
  await page.getByRole('button', { name: '核验正文 v2 →' }).click()
  await expect(page.getByText('此版本仍需人工审阅', { exact: true })).toBeVisible({
    timeout: 90_000,
  })
  const exportButton = page.getByRole('button', { name: '导出正文 v2 ↓', exact: true })
  await expect(exportButton).toBeDisabled()
  await page.getByLabel('我已阅读这些警示，仍要导出此版本').check()
  await exportButton.click()
  await expect(page.getByRole('button', { name: '下载 Word ↓' })).toBeVisible({ timeout: 90_000 })
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 Word ↓' }).click()
  await (await downloaded).saveAs(path.join(output, 'browser-export-v2.docx'))
  await page.keyboard.press('Control+Home')
  await page.screenshot({ path: path.join(output, 'review-desktop.png'), fullPage: true })
  await page.reload()
  await expect(page.getByText('此版本仍需人工审阅', { exact: true })).toBeVisible()
  await page.getByLabel('正文版本', { exact: true }).selectOption({
    label: await page.getByLabel('正文版本', { exact: true }).locator('option').last().innerText(),
  })
  await expect(page.getByText(/正在查看历史正文 v1/)).toBeVisible()
  await expect(page.getByRole('button', { name: '导出正文 v1 ↓' })).toBeDisabled()
  await page.setViewportSize({ width: 375, height: 900 })
  await page.screenshot({ path: path.join(output, 'review-mobile.png'), fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(workspaceUrl)
  await expect(page.getByRole('button', { name: '删除论文', exact: true })).toHaveCount(2)
  const deletedTitle = await page.locator('.paper-list h3').first().innerText()
  await page.getByRole('button', { name: '删除论文', exact: true }).first().click()
  await page.getByRole('button', { name: '保留论文' }).click()
  await expect(page.getByRole('button', { name: '删除论文', exact: true })).toHaveCount(2)
  await page.getByRole('button', { name: '删除论文', exact: true }).first().click()
  await page.screenshot({ path: path.join(output, 'paper-delete-live.png') })
  await page.getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByText('论文已从当前账本删除，历史分析与引用已保留。')).toBeVisible()
  await expect(page.locator('.paper-list h3')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '重新分析文献 →' })).toBeDisabled()
  await page.reload()
  await expect(page.locator('.paper-list h3')).toHaveCount(1)
  await expect(page.getByLabel('分析历史')).toContainText('/ 已过时')
  await page.getByRole('button', { name: '02 / 分析' }).click()
  const historicalPaper = page.locator('.run-ledger li').filter({ hasText: deletedTitle })
  await historicalPaper.getByRole('button', { name: '要素与摘要' }).click()
  await page.getByRole('button', { name: '证据 1 ↗' }).first().click()
  await expect(page.locator('canvas')).toHaveAttribute('width', /[1-9]/)
  await page.getByRole('button', { name: '01 / 文献' }).click()
  await page
    .getByLabel('本地 PDF')
    .setInputFiles(inputFiles.find((file) => path.basename(file, '.pdf') === deletedTitle)!)
  await page.getByRole('button', { name: '上传论文 ↑' }).click()
  await expect(page.locator('.paper-list h3')).toHaveCount(2)
  await expect(page.getByRole('button', { name: '重新分析文献 →' })).toBeEnabled()
  await page.goto('/projects')
  await page.getByRole('button', { name: '删除项目', exact: true }).click()
  await page.getByRole('button', { name: '保留项目' }).click()
  await expect(page.locator('.project-sheet')).toHaveCount(1)
  await page.getByRole('button', { name: '删除项目', exact: true }).click()
  await page.screenshot({ path: path.join(output, 'project-delete-live.png') })
  await page.getByRole('button', { name: '确认删除项目' }).click()
  await expect(page.getByText('0 本项目手稿')).toBeVisible()
  await page.reload()
  await expect(page.locator('.project-sheet')).toHaveCount(0)
  const deletedProjectResponse = page.waitForResponse(
    (response) =>
      response.url().includes('/api/v1/projects/') &&
      response.request().method() === 'GET' &&
      response.status() === 404,
  )
  await page.goto(workspaceUrl)
  expect((await deletedProjectResponse).status()).toBe(404)
  await expect(page.getByText('资源不存在或不可用')).toBeVisible()
  await expect(page.getByText('正在打开工作手稿…')).toHaveCount(0)
  expect(errors).toEqual([])
})
