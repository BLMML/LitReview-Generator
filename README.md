# 学术文献综述智能生成系统 — MVP 后端

Django 5.2 + DRF + Celery + PostgreSQL + DeepSeek 的模块化单体后端。

**这是骨架，不是完成品。** 项目结构、数据库模型、HTTP 路由表与公共基础层已经落地并跑通；
领域业务逻辑（上传解析、分析、检索、生成、核验、导出）尚未实现，
对应入口会明确返回 `503 SERVICE_UNAVAILABLE` 并注明所属阶段，不会伪装成成功。

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

### Redis 与 Celery

`REDIS_URL` 只做队列与临时协调。**任务真实状态永远在数据库**：
投递失败保留 `queued` 由 `reconcile_jobs` 补发，不以队列 ACK 推导成功。

---

## 二、模拟与真实模型模式

由 `LLM_MODE` 控制，**默认 `mock`**：

- `mock`：不产生任何网络请求。测试夹具见 `backend/tests/fixtures/llm_cases.json`。
- `real`：必须显式设置，且同时提供 `DEEPSEEK_API_KEY` 与已核验价目，否则启动即失败。
  正式（`production`）模式下真实调用失败**不会**静默退回模拟结果。

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
ruff check backend                   # 静态检查
python manage.py check               # Django 系统检查
```

当前状态：**24 passed, 47 skipped**。

跳过的 47 条不是通过，而是把 docs/mvp/01 第 9 节要求的验收行为先写成显式 skip，
避免把“没实现”伪装成“已通过”。实现对应阶段后逐条打开即可。

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

---

## 四、骨架完成度

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| `config`（settings / urls / celery / wsgi） | ✅ 可用 | 环境校验、DRF、日志脱敏、无尾斜杠、JSON 404/500 兜底 |
| `common`（http / auth / storage / idempotency / locking / models） | ✅ 可用 | envelope、分页、CSRF、统一 401/404、暂存与归档、幂等重放、Project 行锁与 fencing |
| `accounts` | ✅ 可用 | csrf / login / logout / me |
| `projects` | ✅ 可用 | 列表、创建、详情、PATCH（乐观锁 + corpus_revision 规则） |
| `jobs` | 🟡 部分 | 模型与受理 / 取消 / 重试 / 租约 fencing 已实现；领域分发目标尚未实现 |
| `literature` | 🟡 部分 | 只读接口与 PDF 下载已接好；上传与 arXiv 导入未实现 |
| `parsing` / `analysis` / `retrieval` / `reviews` / `llm` | 🟡 模型与签名就绪 | 数据库模型、路由、序列化器、函数签名与事务边界注释齐全；业务实现待补 |
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

此外，本机**没有 Docker、PostgreSQL 与 Redis**，因此：

- `compose.yaml` 与 `deploy/backend.Dockerfile` 已按 docs/mvp/01 第 8 节写好，
  但**未经实际构建与启动验证**；
- 迁移是在 SQLite 上跑通的，**尚未在 PostgreSQL 上验证**；
- Celery worker / beat 未实机启动过。

这三项必须在有 Docker 的机器上补做，属于 docs/mvp/01 第 9 节要求的运行验证，
不能由文档检查代替。

---

## 七、MVP 明确不包含

- 预训练语义向量、质量加权、跨模型家族实验（留在后续完整项目）；
- 被引次数、期刊等级、作者影响力等质量指标（不抓取、也不用模型猜）；
- 项目共享、团队空间、实时协作、多人编辑；
- 注册、找回密码、第三方登录；每个用户粘贴自己 Key 的页面；
- 物理删除接口（文献取消纳入不删除历史 Run 使用的证据与导出材料）；
- WebSocket 实时推送（前端轮询任务状态）；
- 直接投稿或自动发布（所有 Word 都是草稿）。
