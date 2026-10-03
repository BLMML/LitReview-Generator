# 学术文献综述智能生成系统 — MVP 后端

Django 5.2 + DRF + Celery + PostgreSQL + DeepSeek 的模块化单体后端。

**M03 基础层、M04 任务 / 模型调用底座与 M05 文献接入已实现，尚未形成完整业务产品。**
PDF 上传、元数据编辑、授权下载与 arXiv 导入已经落地；M05 保持「待验收」，仍需人工抽检。
页级解析、分析、检索、生成、核验、导出尚未实现，对应入口明确返回 `503 SERVICE_UNAVAILABLE` 并注明所属阶段。

设计与契约文档在 [`docs/mvp/`](docs/mvp/README.md)：

| 文档 | 内容 |
| --- | --- |
| [00 开发基线与范围](docs/mvp/00_开发基线与范围.md) | 首版做什么、暂缓什么；术语、版本与状态 |
| [01 文件架构与职责](docs/mvp/01_文件架构与职责.md) | 文件树、逐文件职责、事务边界、环境变量清单 |
| [02 数据模型与一致性](docs/mvp/02_数据模型与一致性.md) | 表字段、外键、索引、唯一约束、快照与预算 |
| [03 HTTP 接口契约](docs/mvp/03_HTTP接口契约.md) | 请求响应、认证、错误码、幂等、分页 |
| [04 后台流程与 DeepSeek 规范](docs/mvp/04_后台流程与DeepSeek规范.md) | 阶段流水线、模型调用、引用校验、失败恢复 |
| [05 开发任务与验收](docs/mvp/05_开发任务与验收.md) | 分工、排期、测试矩阵与人工验收 |
| [contracts/](docs/mvp/contracts/) | `openapi.json`（43 个操作）、`model_outputs.schema.json` |

---

## 一、首次启动

前置：Python **3.12**（基线冻结，见 docs/mvp/00 第 2 节）。

```bash
# 1) 虚拟环境
python3.12 -m venv .venv
source .venv/Scripts/activate        # Windows
# source .venv/bin/activate          # Linux / macOS

# 2) 依赖（锁文件保证可重复安装）
pip install -r requirements.lock

# 3) 配置
cp .env.example .env                 # 按需填写；.env 不入库

# 4) 建库迁移
cd backend
python manage.py migrate

# 5) 建管理员账号（MVP 不提供注册接口）
python manage.py createsuperuser

# 6) 起服务
python manage.py runserver           # Web
celery -A config worker -l INFO      # worker（另开终端，需要 Redis）
celery -A config beat   -l INFO      # 定时恢复扫描（另开终端）
```

同域前端通过开发代理把 `/api` 转发到 Django，不要用“允许任意来源 + 携带凭据”解决跨域。

### 数据库

`DATABASE_URL` 指向 PostgreSQL。**MVP 基线与集成测试要求 PostgreSQL**，
行锁、并发受理与部分唯一索引必须用真实 PostgreSQL 验证。

本机没有 PostgreSQL 时，`development` 会回退到 `.data/db.sqlite3` 并在启动时打印警告；
`test` 与 `production` 不回退。SQLite 足以跑契约与权限测试，但不能替代并发结论。

因此 `.env` 里的 `DATABASE_URL` **默认留空**。装了 PostgreSQL 之后再填上，
否则本地测试会因为连不上库而报错（连接超时由 `DJANGO_DB_CONNECT_TIMEOUT` 控制，默认 5 秒）。

### Redis 与 Celery

`REDIS_URL` 只做队列与临时协调。**任务真实状态永远在数据库**：
投递失败保留 `queued` 由 `reconcile_jobs` 补发，不以队列 ACK 推导成功。

### 容器方式（内部试用）

前置：**Docker Desktop**（Windows 上用 WSL2 后端）。需要管理员权限，通常要重启一次。
装好后确认 `docker version` 打印的是**服务端**版本，而不只是客户端版本。

`.env` 已经生成（含随机的 `DJANGO_SECRET_KEY` 与 `POSTGRES_PASSWORD`，均不入库）。

**一键验证整套环境**（推荐先跑这个）：

```bash
python deploy/verify_stack.py
```

它会依次验证：Compose 渲染结果、镜像构建、db/redis 健康、Django 确实连的是 PostgreSQL、
迁移可从空库应用、模型与迁移文件一致、能 ping 通 Redis、worker 就绪、beat 启动、
投递的任务真的被 worker 执行、**容器内用真实 PostgreSQL 跑完整测试套件**，
最后从宿主机跑 HTTP 冒烟与业务走查。加 `--down` 可在验证后自动停栈。

手动分步：

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose ps                      # 确认 db / redis 都是 healthy
docker compose logs -f worker          # 看 worker 是否连上 Redis
```

容器里 `APP_ENV=production`、`DJANGO_DEBUG=false` 由 `compose.yaml` 强制注入，
不跟随本地 `.env` 的开发设置。数据库与 Redis **不发布到宿主机**，
Web 目前只绑定 `127.0.0.1:8000`（前端与 nginx 尚未创建）。

⚠ **`DJANGO_SECURE_COOKIES`**：接入 HTTPS 之前必须保持 `false`。
Secure cookie 在纯 HTTP 下不会被浏览器或客户端回传，会导致登录直接失败。
`compose.yaml` 里默认已是 `false` 并附了说明；接上 nginx / TLS 后改成 `true`，
或删掉那一行让 settings 按 `APP_ENV=production` 自动取 `true`。

没有 Docker 时，可以先用静态校验器挡住"配置写错、一跑就崩"这类问题：

```bash
python .data/tools/validate_deploy.py       # 56 项：锚点结构、暴露面、变量引用、gunicorn
python .data/tools/check_prod_settings.py   # 用容器那套变量驱动 Django 配置
```

---

## 二、模拟与真实模型模式

由 `LLM_MODE` 控制，**默认 `mock`**：

- `mock`：模型调用使用本地替身，不访问模型供应商；不控制 arXiv 导入是否出网。测试夹具见 `backend/tests/fixtures/llm_cases.json`。
- `real`：必须显式设置，且同时提供 `DEEPSEEK_API_KEY` 与已核验价目，否则启动即失败。
  六个峰谷单价、价目版本、当年 UTC 日历和非零预算也必须齐备；真实调用失败**不会**静默退回模拟结果。
  预留始终按峰价，结算读取请求发起时冻结的 `pricing_snapshot`。详见 [M04 实现与验收](docs/mvp/M04_实现与验收.md)。

其他相关配置：`LLM_MAX_CONCURRENCY`（部署级全局上限，加 worker 绕不过）、
`LLM_MAX_ATTEMPTS`（含最多一次输出格式修复）、`PROJECT_DEFAULT_BUDGET_USD`
（零或未配置**不得**被解释为无限预算）。

模型 Key 只在后端进程环境里；前端构建变量、代码与 API 响应都不得出现密钥。

---

## 三、测试

```bash
cd backend
python -m pytest                     # 全部
python -m pytest tests/test_contract_auth.py -v
ruff check .                         # 静态检查（当前位于 backend）
python manage.py check               # Django 系统检查
```

当前状态（PostgreSQL，2026-09-29）：设置 `LLM_MODE=mock`、`RUN_M04_REDIS_TEST=1` 后，
**258 passed, 27 skipped**，包含真实 Redis 隔离队列 + Celery solo 测试 worker + mock 模型网关闭环。
三轮独立复现共 **17 项通过**；Ruff、Django、迁移一致性与依赖检查全部通过。
默认跳过 Redis 闭环时预期 **257 passed, 28 skipped**。
历史基线：第三轮修复方实测 255，第二轮 243，本轮补入守卫测试后为 258，
详见 [M05 实现与验收 §4.4](docs/mvp/M05_实现与验收.md)。
M04 的行锁 / 预算 / 恢复与 M05 的并发上传去重、容量上限测试都要求真实 PostgreSQL；
SQLite 不能替代这些并发结论。

```bash
# 只看 PostgreSQL 专有的并发与约束断言
python -m pytest tests/test_postgres_constraints.py -v
```

剩余 27 条后续业务验收 skip 不是通过，而是把 docs/mvp/01 第 9 节要求的验收行为先写成显式 skip，
避免把“没实现”伪装成“已通过”。实现对应阶段后逐条打开即可：

| 跳过来源 | 条数 | 所属阶段 |
| --- | --- | --- |
| `test_run_retrieval.py` | 11 | M07 / M08 分析、主题、争议、检索 |
| `test_review_export.py` | 13 | M10 / M11 生成、版本、核验、导出 |
| `test_literature_parsing.py` | 3 | M06 页级证据、复杂页显式失败、视觉转录默认未核验 |
| `test_m04_worker.py` | 1 | 需 `RUN_M04_REDIS_TEST=1` 的独立 Redis 队列验证 |

### 端到端冒烟（真实进程）

```bash
# 终端 1（注意 DEBUG=false，原因见下）
cd backend
DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload

# 终端 2
python backend/tests/smoke_http.py
```

**注意 `DJANGO_DEBUG=false`**：Django 在 `DEBUG=True` 时用调试页覆盖 `handler404`，
所以“未命中路由也返回标准 JSON 错误”这条契约只在 `DEBUG=False` 下成立。
开发时看到 HTML 404 属于预期，不是缺陷。

已实测通过（19 项）：CSRF 下发与 cookie、request_id 回显、未登录 401 且不含 data、
匿名 POST 缺 CSRF 为 403、错误凭证 401、未知字段 400 并点名、尾斜杠 404、
未知路由与非法 UUID 均为标准 JSON 错误。

### 业务闭环走查（带登录态）

比冒烟更进一步：真实走一遍“登录 → 建项目 → 乐观锁编辑 → 越权 → 登出”。

```bash
cd backend

# 1) 建两个演示用户（只需一次）
python manage.py shell -c "from django.contrib.auth import get_user_model as g; U=g(); U.objects.create_user('alice', password='demo-pass-123'); U.objects.create_user('bob', password='demo-pass-123')"

# 2) 起服务（另开终端）
DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload

# 3) 走查（回到仓库根）
python backend/tests/walkthrough_http.py
```

已实测通过（46 项），覆盖：`version` 与 `corpus_revision` 的两种递增规则、
过期 `expected_version` 返回 409 且带回 `current_version`、内容相同的保存不涨版本、
未知字段与非法枚举被点名、缺/非法 `Idempotency-Key` 为 400、
跨用户读写一律 404 且不泄露归属、登出后 401。

**未实现的端点在入参校验之后才返回 503** —— 也就是说 `POST /runs` 缺幂等键时
先得到 400，这是对的：契约要求的校验不因为没有业务实现就跳过。

### 文献接入走查（M05，真实 multipart 上传与二进制下载）

`walkthrough_papers_http.py` 走通 M05 的完整闭环。它用真实 multipart 上传与真实 HTTP
二进制下载，因此覆盖了 pytest 里不容易复现的部分：真实 WSGI 下的报文头编码、
multipart 边界、下载字节一致性与 RFC 5987 文件名。

准备用户与起服务的步骤同上，然后：

```bash
# 需要 worker 才能看到 arXiv 导入从受理走到终态（可选）
cd backend
python -m celery -A config worker --pool=solo --loglevel=INFO

# 回到仓库根
python backend/tests/walkthrough_papers_http.py
```

历史走查已实测通过（52 项；本轮修复未重跑真实 arXiv 导入），覆盖：上传 201 与 PaperDTO 全字段、`title` 缺省取文件名、
`file_sha256` 与本地一致、新增文献递增 `corpus_revision`、同文件重复上传仍 201 且带
`X-Resource-Reused: true` 且不覆盖既有元数据、同键同体重放返回同资源、
同键异体 409、缺 `file` / 缺幂等键 400 并点名、非 PDF 422、元数据编辑与
`year`/`authors` 校验、过期 `expected_version` 409 且带回 `current_version`、
内容相同不涨版本、`included` 过滤、未知查询参数 400、下载字节与哈希一致、
`Content-Disposition` 的 RFC 5987 编码、跨用户详情 / 编辑 / 上传 / 下载 / 列表一律 404、
arXiv 拒绝 URL 形式 ID、导入受理 202 并轮询到终态且逐项账本结构合法。

**该脚本会真实访问 arXiv**（服务端白名单内的 `export.arxiv.org` 与 `arxiv.org`），
会下载真实 PDF 并写入私有存储。不想出网时只跑到第 7 节即可，或跳过第 8 节。

> 2026-09-29 前两轮独立复核共提出 14 项边界问题（旧 worker 失去租约仍可写入、忙碌项目仍接受上传、
> 重放被后续编辑污染、下载读完整包才判上限、历史资产按 `text/html` 下载、租约过期仍可改写账本、
> 满容量误拒重复导入、总时限不含重定向等），当时补入 20 条回归用例。
> 第三轮进一步发现账本校验与 UPDATE 之间释放锁、网络阻塞不能被总时限及时中断；
> 本轮把账本校验与 UPDATE 放进同一事务，并用 `AsyncClient` 与 `asyncio.timeout_at` 取消到期的网络等待。
> 本轮使用模型 mock 和 HTTP 替身；历史证据与本轮复验分开记录，见
> [第三轮复核及修复结果](docs/mvp/M05_第三轮复核_2026-09-29.md)。

---

## 四、骨架完成度

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| `config`（settings / urls / celery / wsgi） | ✅ 可用 | 环境校验、DRF、日志脱敏、无尾斜杠、JSON 404/500 兜底 |
| `common`（http / auth / storage / idempotency / locking / models） | ✅ 可用 | envelope、分页、CSRF、统一 401/404、暂存与归档、幂等重放、Project 行锁与 fencing |
| `accounts` | ✅ 可用 | csrf / login / logout / me |
| `projects` | ✅ 可用 | 列表、创建、详情、PATCH（乐观锁 + corpus_revision 规则） |
| `jobs` | ✅ M04 已实现 | 幂等受理、投递补偿、进度、取消、重试、心跳、租约 fencing 与失联恢复；M05 导入处理器已接通，M06+ 待实现 |
| `llm` | ✅ M04 已实现 | mock / 真实适配器、结构校验、重试、私有产物、缓存、全局并发、预算与峰谷结算；付费 API 未实测 |
| `literature` | 🟡 M05 已实现，待验收 | 上传校验与去重、元数据编辑与纳入开关、授权 PDF 下载、arXiv 受理与逐项账本、部分失败保留与重试补项；仍需人工抽检 |
| `parsing` | 🟡 部分 | PDF 结构检查（大小 / 页数 / 加密 / 损坏）已随 M05 实现；页级文本提取、证据片段与页面渲染属 M06 |
| `analysis` / `retrieval` / `reviews` | 🟡 模型与签名就绪 | 数据库模型、路由、序列化器、函数签名与事务边界注释齐全；业务实现待补 |
| `prompts`（7 个模板） | ✅ 已交付 | `prompt_loader` 可用：分段加载、参数渲染、版本哈希 |
| `tests` | 🟡 部分 | 契约 / 认证 / 权限测试可跑；其余按阶段 skip |
| `frontend` | ⛔ 未开始 | 本次范围只做后端 |
| `deploy` | 🟡 部分 | `backend.Dockerfile` 已交付；前端镜像与 nginx 待前端落地 |

### 已实现的关键约束

- 所有路由无尾斜杠，`APPEND_SLASH = False`；URL 未命中也是标准错误 JSON。
- 统一 envelope：成功 `{data, meta}`，错误 `{error:{code,message,details,retryable}, meta}`。
- 未登录统一 401；越权与不存在统一 404，不泄露资源归属。
- 未列出的输入字段与查询参数一律 400，不被静默忽略。
- 登录本身也校验 CSRF（匿名 POST 同样是攻击面）。
- 每项目最多一个活跃任务，由数据库部分唯一索引兜底，不靠 Redis 锁。
- 任务租约用单调递增的 `lease_token` 做 fencing，旧 worker 迟到也无法提交结果。
- 金额统一 `Decimal` + `numeric(18,8)`，未知用量写 `null` 而不是 `0`。
- 日志过滤器兜底脱敏认证头、Cookie 与 Key。
- 同项目同 SHA256 只保留一篇文献，由唯一约束兜底；重复上传返回既有资源且不递增语料版本。
- 上传体积上限在暂存流中即时生效，超大文件不会先落满磁盘再被拒绝。
- PDF 结构检查只认文件内容，不认扩展名；加密、损坏、超页数分别映射 422 / 413。
- 论文原文一律经授权路由读取，下载响应头**不含任何磁盘路径**，非 ASCII 文件名按 RFC 5987 编码。
- arXiv 地址由服务端白名单构造，**每一跳重定向都重新校验主机**，不接受用户提供的 URL。
- arXiv 响应按不可信输入解析：拒绝任何带 `<!DOCTYPE` 的响应，挡掉 XXE 与实体膨胀。

---

## 五、目录结构

```text
.
├── backend/
│   ├── manage.py
│   ├── config/                 settings / urls / celery / wsgi
│   ├── apps/
│   │   ├── common/             协议、权限基础、受控文件、幂等、锁
│   │   ├── accounts/           登录（无模型）
│   │   ├── projects/           Project
│   │   ├── literature/         Paper
│   │   ├── parsing/            ParsedDocument / EvidenceChunk
│   │   ├── analysis/           Run / RunPaper / PaperAnalysis / Topic / Dispute
│   │   ├── retrieval/          RetrievalResult
│   │   ├── reviews/            Review / Revision / Validation / Export
│   │   ├── jobs/               Job
│   │   └── llm/                ModelCall / UsageBudget / 唯一供应商适配器
│   ├── prompts/                7 个受版本控制的提示词模板
│   └── tests/                  conftest + 契约测试 + 分阶段测试清单
├── docs/mvp/                   设计与契约文档（本包原始交付物）
├── deploy/backend.Dockerfile
├── compose.yaml
├── pyproject.toml
├── requirements.lock           首次构建解析出的精确版本
└── .env.example
```

私有存储（原文、页图、解析产物、索引、导出文件）写入仓库外的
`PRIVATE_STORAGE_ROOT`，Web 与 worker 共享同一卷，绝不通过公开 `MEDIA_URL` 暴露。

---

## 六、本机环境说明（迁移到其他机器时可忽略）

搭建时发现两处与代码无关的本机限制，均已绕过，不影响仓库内容：

1. **`tempfile.mkdtemp()` 在该文件沙箱下创建的目录不可写**，导致 `ensurepip` /
   `get-pip` / `pip` 全部失败。已在本机 venv 内放置
   `.venv/Lib/site-packages/sitecustomize.py` 做等价替换。该文件在 `.venv` 内，
   已被 `.gitignore` 忽略，**不是仓库代码**；在正常机器上重新建 venv 不需要它。
2. **系统临时目录不可写**，因此 `pip` 的 `TEMP`/`TMP` 指向了 `.data/tmp`。
   同理，pytest 夹具把私有存储根目录建在 `.data/test-storage/` 而不是 `tmp_path`。

此外，本机**没有 Docker**，WSL 虽存在但未安装任何发行版。因此改用免安装版本地跑：

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| PostgreSQL 17.9 | 自动探测的 ASCII 路径，见下 | 免安装二进制 + 数据目录，只监听 127.0.0.1:5432 |
| Redis 5.0.14 | `.data/redis` | Windows 移植版，只监听 127.0.0.1:6379，**关闭持久化** |

PostgreSQL 的根目录**不写死**，按以下优先级自动探测（先找已有安装，找不到才新建）：

1. `--root <路径>` 命令行参数（给了就只看这一个）
2. `PG_ASCII_ROOT` 环境变量
3. `D:\review-mvp-pg`
4. `%PUBLIC%\review-mvp-pg`

同一批候选里，**集群与安装都完整**的位置优先于只有二进制的半成品，
这样搬家搬到一半时也不会选错。想知道当前选中了哪里：

```powershell
.venv\Scripts\python.exe .data\tools\pg_ctl_local.py status
```

管理命令（**必须在你自己的终端跑，不要在受限沙箱里跑**）：

```powershell
.venv\Scripts\python.exe .data\tools\pg_ctl_local.py init|start|stop|status
.venv\Scripts\python.exe .data\tools\redis_local.py   start|stop|status

# 显式指定位置（例如把 PostgreSQL 放到 D 盘）
.venv\Scripts\python.exe .data\tools\pg_ctl_local.py init --root D:\review-mvp-pg
```

`init` 会自动把二进制迁到 ASCII 路径、初始化集群、建库建角色并把 `DATABASE_URL`
写进 `.env`。写完 `.env` 后本地测试自动从 SQLite 切到 PostgreSQL，代码无需改动。

已验证（2026-09-21）：迁移在 PostgreSQL 上从空库全部应用；
**137 passed / 35 skipped**（启用 M04 Redis 集成验证），包含 PostgreSQL 并发与约束断言；
Redis 连通；worker 消费投递的任务；beat 每 30 秒触发 `reconcile_jobs` 并被 worker 执行。

仍未验证：`compose.yaml` 与 `deploy/backend.Dockerfile` 只过了 56 项静态检查，
**没有真实 build / up 过**。等有 Docker 的机器上用 `python deploy/verify_stack.py` 补做。

### ⚠️ PostgreSQL 装在含中文的路径下会失败（BUG #16926）

这是本机踩过的坑，值得记住：**Windows 上 PostgreSQL 的安装路径不能含非 ASCII 字符**。

initdb 后期自举时会生成一条 `COPY` 命令，把「安装目录下 `share/sql_features.txt`
的完整路径」写进 SQL，而服务器要求这条 SQL 是合法 UTF8。路径里只要有中文
（例如 Windows 用户名是中文时，`C:\Users\<中文名>\...` 就带上了非 ASCII 字节），
字节序列就不是 UTF8：

```
performing post-bootstrap initialization ...
FATAL:  invalid byte sequence for encoding "UTF8": 0xb1
```

核心开发者已明确表示不会修（[BUG #16926](https://www.postgresql.org/message-id/16926-e4ef345545d185ab%40postgresql.org)）。
注意这条报错**极易误导**：它看起来像编码或 locale 问题，但 `--locale=C`、`-E UTF8`
都改不动它，真正的变量是路径。数据目录也一并放在 ASCII 路径下更保险。

`.data/tools/pg_ctl_local.py` 已经内置了这个处理：检测到安装路径非 ASCII 时
自动复制 `bin`/`lib`/`share` 到 `<root>\pgsql` 再初始化。

### 已回填 docs/mvp/01 第 8.1 节的部署变量

以下四项已于 2026-09-21 随 M04 回填；六个峰谷单价及日历配置也已同步：

| 变量 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | 仅 compose 使用；会被拼进 `postgresql://` 连接串，必须 URL 安全 |
| `DJANGO_SECURE_COOKIES` | 留空跟随 `APP_ENV`；未接 TLS 时显式设 `false`，否则纯 HTTP 下登录失败 |
| `DJANGO_DB_CONNECT_TIMEOUT` | 数据库连接超时秒数，默认 5；避免连不上时挂到系统 TCP 超时 |
| `DJANGO_DB_CONN_MAX_AGE` | 数据库连接复用秒数，默认 60 |

---

## 七、MVP 明确不包含

- 预训练语义向量、质量加权、跨模型家族实验（留在后续完整项目）；
- 被引次数、期刊等级、作者影响力等质量指标（不抓取、也不用模型猜）；
- 项目共享、团队空间、实时协作、多人编辑；
- 注册、找回密码、第三方登录；每个用户粘贴自己 Key 的页面；
- 物理删除接口（文献取消纳入不删除历史 Run 使用的证据与导出材料）；
- WebSocket 实时推送（前端轮询任务状态）；
- 直接投稿或自动发布（所有 Word 都是草稿）。
