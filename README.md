# 学术文献综述智能生成系统

将 PDF / arXiv 文献整理为可回看原文的分析快照，按固定研究框架生成综述，支持人工修订、指定版本核验和 Word 草稿导出。

技术栈：Django 5.2、DRF、PostgreSQL、Celery / Redis、DeepSeek；前端为 Vue 3、TypeScript、Pinia、Vite 和 PDF.js，采用 **Anti-Polish / Raw Aesthetic** 风格。

当前版本：`v0.12.0-m12-raw-workbench`。文档整理日期：2026-10-05。Word 新导出规范见 [固定研究框架与原文出处](docs/mvp/Word固定研究框架与原文出处_2026-10-03.md)。**M03—M12 主流程已实现，工程验证通过；负责人签收与全面学术质量验收仍待完成，下一阶段为 M13。**

导航：[用户使用说明书](docs/用户使用说明书.md) · [快速启动](#快速启动) · [首次配置](#首次配置) · [使用流程](#使用流程) · [关键配置](#关键配置) · [测试与验证](#测试与验证) · [常见问题](#常见问题) · [容器部署](#容器部署) · [文档与目录](#文档与目录) · [下一步](#下一步)

## 当前进展

| 能力       | 已实现内容                                                                 |
| ---------- | -------------------------------------------------------------------------- |
| 账号与项目 | Session / CSRF、管理员创建账号、项目隔离、乐观锁、整项目删除与确认         |
| 文献账本   | PDF 上传与去重、arXiv 批量导入、元数据编辑、纳入开关、删除确认与同文件恢复 |
| 解析与证据 | 文本层提取、复杂页图像辅助、页级证据、授权 PDF 原文阅读                    |
| 分析与检索 | 2—30 篇冻结 Run、逐篇要素与摘要、分批主题汇总、覆盖全部主题成员的争议比较及人工复核、TF-IDF / 关键词检索    |
| 综述与导出 | 五个主模块与两个子模块、原文表述及 PDF 页段行出处、不可变正文版本、版本绑定核验、警示确认、Word 就绪后自动下载及历史下载           |
| 后台任务   | 幂等受理、进度、取消、重试、心跳、失联恢复、预算与峰谷快照结算             |
| 前端       | 登录到 Word 下载的完整操作链路、历史记录与任务恢复、桌面 / 手机布局        |

2026-10-03 已完成九篇资料项目的真实 DeepSeek 手稿生成冒烟，六个内容模块均有可定位原文；这不等同于全部论文真实分析、扫描件或全面学术质量验收。记录见 [空内容修复与真实生成](docs/mvp/手稿空内容修复与真实生成_2026-10-03.md)。容器构建、TLS 和备份恢复仍需 M13 实测。详细状态见 [开发任务与验收](docs/mvp/05_开发任务与验收.md) 和 [M12 实现与验收](docs/mvp/M12_实现与验收.md)。

## 快速启动

以下适用于**已有环境的本机** `D:\LitReview Generator`，使用 PowerShell。首次安装请先完成[首次配置](#首次配置)。直接调用虚拟环境中的 Python，无需激活脚本。

### 1. 启动 PostgreSQL 和 Redis

```powershell
cd "D:\LitReview Generator"
.\.venv\Scripts\python.exe .data\tools\pg_ctl_local.py start
.\.venv\Scripts\python.exe .data\tools\redis_local.py start
```

查看状态时将 `start` 改为 `status`。本机 PostgreSQL 位于 `D:\review-mvp-pg`，Redis 位于 `.data/redis`；上述工具与二进制属于被忽略的本机文件，不随 Git 克隆。新机器可自行安装服务或使用[容器方式](#容器部署)。

### 2. 应用迁移并启动 Django

终端 A：

```powershell
cd "D:\LitReview Generator\backend"
Remove-Item Env:LLM_MAX_INPUT_TOKENS -ErrorAction SilentlyContinue
Remove-Item Env:LLM_MODE -ErrorAction SilentlyContinue
..\.venv\Scripts\python.exe manage.py migrate
..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload
```

### 3. 启动 Celery worker

另开终端 B：

```powershell
cd "D:\LitReview Generator\backend"
Remove-Item Env:LLM_MAX_INPUT_TOKENS -ErrorAction SilentlyContinue
Remove-Item Env:LLM_MODE -ErrorAction SilentlyContinue
..\.venv\Scripts\python.exe -m celery -A config worker --pool=solo --concurrency=1 --loglevel=INFO
```

启动命令清除当前终端的旧模型模式覆盖，Web、worker 和 beat 统一读取根目录 `.env`。本机现已按用户授权启用 `LLM_MODE=real`，新模型任务会按配置计费。

Windows 使用 `--pool=solo`。出现 `Connected to redis` 和 `ready` 表示 worker 就绪；`mingle: all alone` 是单 worker 的正常日志。

### 4. 启动 Celery beat

另开终端 C，同一环境只运行一个 beat：

```powershell
cd "D:\LitReview Generator\backend"
Remove-Item Env:LLM_MAX_INPUT_TOKENS -ErrorAction SilentlyContinue
Remove-Item Env:LLM_MODE -ErrorAction SilentlyContinue
..\.venv\Scripts\python.exe -m celery -A config beat --loglevel=INFO --schedule "../.data/celerybeat-schedule"
```

`beat: Starting...` 和 `Sending due task` 表示调度正常。worker 会消费恢复扫描与清理任务；任务真实状态保存在 PostgreSQL。

### 5. 启动前端

另开终端 D：

```powershell
cd "D:\LitReview Generator\frontend"
npm.cmd run dev
```

首次安装或依赖锁更新后，先执行 `npm.cmd ci`。保留 A—D 四个终端运行，正常停止使用 Ctrl+C；worker 首次 Ctrl+C 会等待当前任务结束。

| 入口 / 服务        | 本机地址                                 |
| ------------------ | ---------------------------------------- |
| 用户工作台         | <http://127.0.0.1:5173>                  |
| Django 管理后台    | <http://127.0.0.1:8000/admin/>           |
| CSRF 接口探测      | <http://127.0.0.1:8000/api/v1/auth/csrf> |
| PostgreSQL / Redis | `127.0.0.1:5432` / `127.0.0.1:6379`      |

前端通过同域代理访问 `/api`，不读取模型密钥。Django、worker 与 beat 应使用同一份 `.env`；进程环境变量优先于 `.env`，上面的命令会清除终端残留的输入上限覆盖。

## 首次配置

环境要求：Python **3.12**、PostgreSQL **17**、Redis；前端需要 Node **22.13+（22 系列）或 24+**。本机已有验证版本为 Node 24.11.1 / npm 11.6.2；后端精确依赖见 [requirements.lock](requirements.lock)。

```powershell
cd "D:\LitReview Generator"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
cd frontend
npm.cmd ci
```

编辑根目录 `.env`，按 [.env.example](.env.example) 填写 `DATABASE_URL`、`REDIS_URL` 和稳定的 `DJANGO_SECRET_KEY`；开发起步使用 `APP_ENV=development`、`LLM_MODE=mock`。已有项目保留原虚拟环境和配置，无需重新创建。

`DATABASE_URL` 示例为 `postgresql://review:YOUR_PASSWORD@127.0.0.1:5432/review_mvp`，需事先建立数据库与角色。非生产环境未填写时，当前实现会回退 `.data/db.sqlite3`；SQLite 仅用于轻量检查，**完整集成、行锁和并发结论必须使用真实 PostgreSQL**。生产环境缺少 PostgreSQL 配置会直接拒绝启动。

基础服务启动后，创建管理员：

```powershell
cd "D:\LitReview Generator\backend"
..\.venv\Scripts\python.exe manage.py migrate
..\.venv\Scripts\python.exe manage.py createsuperuser
```

MVP 不提供公开注册。管理员登录管理后台，在“用户”中新增普通账号、设置密码并保持启用；普通用户从 5173 工作台登录，无需赋予管理员权限。

## 使用流程

1. 登录工作台，新建项目并填写研究问题。
2. 上传 PDF 或导入 arXiv ID，补全标题、作者、年份，纳入 2—30 篇论文。
3. 开始分析，在历史记录查看进度与逐篇失败原因。系统逐篇解析，再按请求大小分批归纳主题并汇总全部文献；同主题比较覆盖全部成员及跨批次组合，默认单次最多 8 篇，不再只比较前 8 篇。
4. 查看单篇要素、主题、争议和年份统计；从证据入口回到 PDF 原文页。
5. 检索证据，按固定研究框架生成综述，再人工编辑正文与引用并保存新版本。
6. 核验选中的已保存版本；阅读待审 / 过时 / 历史版本警示后，按允许条件确认并导出 Word。

任务执行或取消期间禁止资料修改。修改研究问题、论文元数据、纳入集合或删除论文会使旧 Run 过时；历史结果仍可读，新检索与生成需要重新分析。

“删除论文”经弹窗确认后移出当前账本并释放名额，保留历史分析、证据及原始 PDF。重新上传同一文件或新建同来源 arXiv 导入可恢复原记录。具体行为见 [论文删除实现与验证](docs/mvp/论文删除_实现与验证_2026-10-02.md)。

项目档案的“删除项目”移除整个项目；项目和下属原文、分析、综述、核验、任务与 Word 的旧链接随后均返回 404。项目任务进行中时禁止删除，确认时校验项目与语料版本。底层文件、历史和费用账本保留，当前不提供项目恢复；需要的文件应先下载。具体步骤见 [用户说明书第 4.3 节](docs/用户使用说明书.md#43-删除整个项目)。

## 关键配置

| 配置 / 限制                                | 当前口径                                                                 |
| ------------------------------------------ | ------------------------------------------------------------------------ |
| `LLM_MODE`                                 | 代码默认 `mock`；本机 `.env` 已授权启用 `real`，arXiv 导入两种模式均出网                        |
| `LLM_MAX_INPUT_TOKENS`                     | **384000**；完整提示词 UTF-8 字节数加封装余量的保守上界                  |
| `LLM_MAX_OUTPUT_TOKENS`                    | **4096**；与论文输入容量分别控制                                         |
| `LLM_MAX_CONCURRENCY` / `LLM_MAX_ATTEMPTS` | 部署级并发 2；最多 3 次尝试，含最多一次格式修复                          |
| `PROJECT_DEFAULT_BUDGET_USD`               | 示例为 0；零或未配置不表示无限预算                                       |
| PDF / 项目容量                             | 每份 ≤ 25 MiB / 50 页；当前账本最多 30 篇，分析至少纳入 2 篇             |
| 删除与名额                                 | 未删除但取消纳入的仍占名额；删除释放名额，恢复重新检查上限               |
| arXiv 批量导入                             | 每批 1—10 个 ID，不接受任意 URL                                          |
| `PRIVATE_STORAGE_ROOT`                     | Web / worker 共用私有目录；开发默认 `.private/storage`，生产必须显式设置 |

已验证完整三万字中文单篇及双篇输入，保留全部片段与证据 ID。多篇全文合并仍可能超限，程序不会静默截断；详见 [长文输入上限调整](docs/mvp/长文输入上限调整_2026-10-02.md)。

`real` 模式需显式配置 `DEEPSEEK_API_KEY`、六个峰谷单价、价目版本、当年 UTC 日历和非零预算；配置不全时拒绝启动，调用失败不退回 mock。预算预留按峰价，结算按每次请求冻结的 `pricing_snapshot` 与实际 usage。配置清单见 [01 第 8.1 节](docs/mvp/01_文件架构与职责.md) 和 [M04 记录](docs/mvp/M04_实现与验收.md)。

`.env`、密钥、账号表及私有原文不应公开。私有 PDF / Word 经资源归属校验下载，不能映射到前端 `public` 或公开 `MEDIA_URL`。数据库备份需要同时保留对应私有文件快照。

## 测试与验证

### 后端

先启动 PostgreSQL；Redis 闭环额外需要 Redis。以下测试默认使用模型替身：

```powershell
cd "D:\LitReview Generator\backend"
..\.venv\Scripts\python.exe manage.py check
..\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
..\.venv\Scripts\python.exe -m ruff check .
..\.venv\Scripts\python.exe -m pytest -q
```

完整运行可选 Redis 闭环：

```powershell
$env:RUN_M04_REDIS_TEST = "1"
..\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:RUN_M04_REDIS_TEST -ErrorAction SilentlyContinue
```

不设置该变量时，`test_m04_worker.py` 的独立队列测试会跳过。按需专项验证：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_paper_delete.py -q
..\.venv\Scripts\python.exe -m pytest tests/test_long_paper_input.py tests/test_llm_gateway.py tests/test_m11_reviews.py -q
..\.venv\Scripts\python.exe -m pytest tests/test_postgres_constraints.py -q
```

### 前端与真实浏览器

```powershell
cd "D:\LitReview Generator\frontend"
npm.cmd run build
npm.cmd test
npm.cmd exec playwright -- install chromium
npm.cmd run test:e2e
npm.cmd run format:check
```

默认 Playwright 测试使用 HTTP 夹具，涵盖删除确认、版本与警示、冲突保稿、取消恢复、证据阅读和响应式布局。测试夹具不进入产品页面。

真实浏览器走查先准备 [M12 记录](docs/mvp/M12_实现与验收.md) 中的两份测试 PDF，再执行：

```powershell
cd "D:\LitReview Generator\backend"
..\.venv\Scripts\python.exe -m pytest tests/walkthrough_m12_browser.py -q --reuse-db
```

该入口自动启动隔离 Django、5174 测试前端和临时私有存储，使用测试数据库；不要与其他数据库测试并行。

### 已记录的验证结果

**2026-10-03 项目删除复验**：后端全量 **639 passed / 0 skipped**（含 PostgreSQL 行锁和 Redis/Celery 闭环）；前端单测 **22 passed**、UI 浏览器测试 **16 passed**。两篇用户 PDF 的隔离浏览器完整流程 **1 passed**，覆盖生成、编辑、核验、Word 下载、整项目删除与旧链接 404。具体实现和验证边界见 [项目删除实现与验证](docs/mvp/项目删除_实现与验证_2026-10-03.md)。

**2026-10-03 用户 PDF 全流程复验**：后端全量 **624 passed / 0 skipped**；最后的 Word 分页修复后，浏览器完整流程与 M11 / 导出专项 **73 passed / 0 skipped**；前端单测 **22 passed**、UI 浏览器测试 **12 passed**。默认 mock 与真实 Redis / Celery 闭环均通过，具体语料、修复、测试边界及开发库结果见 [全流程测试与修复](docs/mvp/全流程测试与修复_2026-10-03.md)。各组有重叠，不能相加。

以下保留 **2026-10-02 的历史验证记录**，用于追踪专项变更。

| 范围                                         | 结果 / 记录                                                                                |
| -------------------------------------------- | ------------------------------------------------------------------------------------------ |
| M11 时点后端全量，含 PostgreSQL / Redis 闭环 | 584 passed / 0 skipped；[M11 记录](docs/mvp/M11_实现与验收.md)                             |
| 三万字输入、网关与 M11 核验回归              | 134 passed / 0 skipped；[长文记录](docs/mvp/长文输入上限调整_2026-10-02.md)                |
| 删除专项 / 相关后端回归                      | 14 passed / 170 passed，两组有重叠；[删除记录](docs/mvp/论文删除_实现与验证_2026-10-02.md) |
| 前端 Vitest / Playwright UI                  | 22 passed / 12 passed；[删除记录](docs/mvp/论文删除_实现与验证_2026-10-02.md)              |
| 隔离库真实浏览器完整流程，含删除和恢复       | 1 passed；[删除记录](docs/mvp/论文删除_实现与验证_2026-10-02.md)                           |

工程闭环通过不代表论文抽取、事实支持或学术合理性通过，真实质量验收属于 M02 / M14。

## 常见问题

| 现象                                      | 处理                                                                                                                                        |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 修改 token 上限后仍显示旧错误             | 配置只影响新进程；重启 Django / worker / beat，再点击“重新分析文献”。刷新不会重跑旧失败快照                                                 |
| 默认 mock 分析出现 `MODEL_OUTPUT_INVALID` | 当前默认 mock 已绑定实际论文和证据 ID；重启后端与 worker 后新建分析。旧失败 Run 保留原记录；mock 输出明确标为模拟，真实模型需配置价目和预算 |
| worker 出现 `Warm shutdown`               | 按下了 Ctrl+C，表示正常关闭；需要处理任务时重新启动                                                                                         |
| beat 提示命令参数解析失败                 | 检查是否粘贴了多余字符（如 `^B`），完整重输上方命令                                                                                         |
| 项目提示已有任务，无法删除 / 修改         | 查看历史记录，等任务终态或按支持的取消流程处理                                                                                              |
| 登录失败或 Cookie 不回传                  | 本机 HTTP 使用 `DJANGO_SECURE_COOKIES=false`；接入 HTTPS 后启用 Secure Cookie                                                               |
| 数据库连接失败                            | 先检查 PostgreSQL 状态和 `.env` 的连接串；本机安装与数据目录使用 ASCII 路径，例如 `D:\review-mvp-pg`                                        |
| 端口 8000 已被占用                        | 已有 Django 或容器入口在运行；先确认对应服务。强制重启命令见 [强制重启后端.txt](强制重启后端.txt)，会中断当前 Web 请求                      |
| 开发环境未知路由返回 HTML 404             | `DJANGO_DEBUG=true` 时使用 Django 调试页；统一 JSON 404 的 HTTP 冒烟需用 `DJANGO_DEBUG=false` 启动                                          |

## 容器部署

需要可用的 Docker Engine / Compose。已交付后端 / 前端 Dockerfile 和 nginx 同域代理配置；**真实构建、启动、TLS 与恢复验证仍待 M13**。

在根目录 `.env` 配置生产密钥、URL 安全的 `POSTGRES_PASSWORD` 和模型模式等必需项，然后运行：

```powershell
cd "D:\LitReview Generator"
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose ps
docker compose logs -f worker
```

Compose 的前端入口是 <http://127.0.0.1:8000>，与本地 Vite 的 5173 区分；Django 只在容器内部开放，PostgreSQL / Redis 不发布宿主机端口。容器入口与本地 Django 都使用 8000，切换运行方式前需停止另一套 Web 服务。

Compose 强制 `APP_ENV=production`、`DJANGO_DEBUG=false`。当前本地 HTTP 设置 `DJANGO_SECURE_COOKIES=false`，接入 HTTPS / TLS 后同步启用安全 Cookie。

验证脚本在有 Docker 的隔离试用环境中运行：

```powershell
.\.venv\Scripts\python.exe deploy\verify_stack.py
```

该脚本会构建 / 启动容器、应用迁移、运行 PostgreSQL 测试和 HTTP 走查，并创建演示账号与测试项目。当前配置的静态检查不能替代上述运行验证。

## 文档与目录

| 文档                                                                                                     | 用途                                                  |
| -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| [用户使用说明书](docs/用户使用说明书.md)                                                                 | 浏览器操作步骤、版本与警示、任务处理和常见问题        |
| [前端 README](frontend/README.md)                                                                        | 前端开发、证据回看、构建和测试                        |
| [MVP 文档导航](docs/mvp/README.md)                                                                       | 设计规范、任务与模块验收记录入口                      |
| [00 开发基线与范围](docs/mvp/00_开发基线与范围.md)                                                       | 术语、容量、状态与范围                                |
| [01 文件架构与职责](docs/mvp/01_文件架构与职责.md)                                                       | 逐文件职责、事务与完整变量清单                        |
| [02 数据模型与一致性](docs/mvp/02_数据模型与一致性.md)                                                   | 外键、快照、约束、预算与文件生命周期                  |
| [03 HTTP 契约](docs/mvp/03_HTTP接口契约.md)                                                              | 认证、统一响应、错误码、幂等和分页                    |
| [04 后台流程](docs/mvp/04_后台流程与DeepSeek规范.md)                                                     | 模型调用、证据校验、阶段提交与恢复                    |
| [05 开发任务与验收](docs/mvp/05_开发任务与验收.md)                                                       | M01—M16 任务、依赖、测试矩阵与人工验收                |
| [OpenAPI](docs/mvp/contracts/openapi.json) / [模型 Schema](docs/mvp/contracts/model_outputs.schema.json) | 45 个 HTTP 操作、43 个公共 Schema；模型结构化输出约束 |
| [HANDOFF](HANDOFF.md)                                                                                    | 本机环境、历史修复、接手约束与遗留项                  |

```text
.
├── backend/                 Django 配置、业务 apps、提示词与测试
├── frontend/                Vue 页面、API 封装、类型、组件与浏览器测试
├── docs/mvp/                设计契约、任务、实现与验收记录
├── design-system/           Anti-Polish / Raw Aesthetic 设计规则
├── deploy/                  Dockerfile、nginx 与容器验证脚本
├── scripts/                 辅助脚本
├── compose.yaml             容器编排
├── pyproject.toml           Python 基线、依赖范围与检查配置
├── requirements.lock        后端精确依赖
└── .env.example             配置模板
```

`.venv/`、`.env`、`.data/`、`.private/` 为本机环境、日志或私有数据，不入 Git。具体文件职责见 01，历史模块测试和复核记录通过文档导航查询。

## 下一步

1. **M12 人工验收**：核对登录、论文删除、原文阅读、版本保存、核验与 Word 下载。
2. **M13 集成与恢复**：验证不同用户隔离、网络中断、worker 失联、批量部分失败、长文分批、真实容器 / TLS，以及数据库与私有文件配套恢复。
3. **M14 真实模型与学术验收**：准备冻结语料和小额预算，执行真实 API 冒烟、人工核对与盲评。

MVP 不包含公开注册 / 找回密码、项目共享 / 实时协作、物理文件删除、预训练语义向量检索、自动投稿或发布。导出的 Word 始终是需要人工审阅的草稿。
