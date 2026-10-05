# 文献手稿前端 / M12

Vue 3 + TypeScript + Pinia + Vue Router + Vite + PDF.js。视图使用 ui-ux-pro-max 的 **Anti-Polish / Raw Aesthetic**，设计规则见 `../design-system/litreview-generator/MASTER.md`。

## 启动

已验证 Node 24.11.1 / npm 11.6.2。需要 Node 22.13+（22 系列）或 24+。

先按仓库 README 启动 PostgreSQL、Redis、Django（127.0.0.1:8000）、worker、beat。新开 PowerShell：

```powershell
cd "D:\LitReview Generator\frontend"
npm.cmd ci
npm.cmd run dev
```

打开 **http://127.0.0.1:5173**，使用管理员创建的账号。开发代理让 `/api` 与页面保持同源；无需修改 CORS。默认代理 8000，隔离联调可用服务端环境变量 `M12_API_PROXY_TARGET` 指向其他 Django 地址。它不会被打包进浏览器。

开发模式与模型模式相互独立：启动前端不启用付费 API。默认 mock 供应商的固定样例可能因真实证据 ID 或输入上限失败，页面会显示实际原因。真实模型与学术质量验收仍在 M14。

## 操作路径

1. 登录 → 新建项目，填研究问题。
2. 上传至少两篇 PDF 或导入 1—10 个 arXiv ID；在论文账本补标题、作者、年份与纳入状态。
3. 开始分析；历史记录保留阶段、进度、错误与逐项导入结果。
4. 查看冻结研究问题、单篇要素、主题、争议。每条证据可回到 PDF 原文页。
5. 在证据页检索，或直接按快照论文 / 页码选择证据。过时 Run 禁止新检索，仍可读旧记录。
6. 在手稿页生成固定研究框架，进入编辑。选取完整原句或原段，调整引用后保存新版本；固定标题不可修改。
7. 核验选中的已保存版本。待审 / 过时 / 历史版本需要显式确认警示后导出。
8. 点击导出后就近显示进度，Word 就绪后自动下载；同一正文和报告已有成品时直接下载现有文件。历史导出仍可手动下载；刷新恢复版本、报告、导出和后台任务。

任务期间禁止修改；保存冲突保留本地草稿。离开或退出登录时询问未保存改动；刷新 / 关闭浏览器由 beforeunload 提示。尚未完成的资源显示“等待结果”，不会显示为成功空结果。

前端不计算或显示模型输入 / 输出 token 数、费用估算及未结算调用数；后端继续记录用量、预留预算并结算费用。HTTP 类型保留相关字段，以兼容现有接口。

论文账本每篇提供“删除论文”，确认后移出当前账本并释放名额；任务期间禁用。已有分析和综述保留原文引用，重新上传同一文件可以恢复。删除后旧分析会提示语料过时，需要重新分析才能新检索或生成。

项目档案每张卡片提供“删除项目”，确认后移除整个项目，并关闭论文、分析、手稿和导出的旧链接；任务期间禁用，项目或语料变化须刷新后重新确认。需要的文件请先下载，当前界面不提供项目恢复；底层文件、历史产物与费用账本保留。具体操作见 [用户使用说明书](../docs/用户使用说明书.md#43-删除整个项目)。

## 检查

```powershell
npm.cmd run typecheck
npm.cmd run build
npm.cmd test
npm.cmd exec playwright -- install chromium
npm.cmd run test:e2e
npm.cmd run format:check
npm.cmd audit
```

`test:e2e` 默认运行明确的 HTTP 测试夹具，覆盖版本绑定、警示确认、冲突保稿、取消中、过时证据 / PDF 页码和 375 / 768 / 1024 / 1440 响应式。测试夹具只在测试进程拦截请求，产品没有内置演示数据。

真实 HTTP + PostgreSQL + 两篇真实论文浏览器全流程命令和材料来源见 `../docs/mvp/M12_实现与验收.md`。不要单独设置 `M12_LIVE_TEST=1` 去访问开发库；隔离测试入口会自动启动测试 Django 和测试前端。

浏览器入口当前直接使用产品默认 mock，支持通过 `M12_PDF_PATHS` 指定两份本地 PDF。mock 只验证工程流程，正文和核验明确标为模拟及待审；阶段进度由服务端回写，任务结束后会收起“任务已受理”提示。用户语料与真实队列复验见 [全流程测试与修复](../docs/mvp/全流程测试与修复_2026-10-03.md)。

## 文件边界

`api/client.ts` 是唯一 fetch 封装；`api/resources.ts` 显式列出路由；`stores/workspace.ts` 统一管理 Job 轮询；`composables/exportDelivery.ts` 只在用户明确请求导出时跟踪该 Export 资源并下载一次，页面销毁后停止。组件不直接请求供应商。私有 PDF / DOCX 经同源授权接口取回，错误 JSON 不会被当作下载文件。

`npm run contracts` 从冻结 OpenAPI 生成 `types/generated.ts`。`types/contracts.ts` 明确适配当前后端的年份映射、nullable 用量及 M11 导出扩展；不以 `any` 掩盖契约差异。新增 HTTP 字段时必须回填契约和类型。

生产构建采用 `deploy/frontend.Dockerfile` + nginx 同域代理；本机没有 Docker，容器构建与 TLS 联调留给 M13。禁止将 `.private`、原文、页图或导出目录映射为静态站点。
