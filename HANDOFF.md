# 交接说明（给接手的 DSH 会话）

> 这份文档面向**接手的 AI 会话**，不是项目介绍。项目介绍看 [`README.md`](README.md)。
> 生成时间：2026-09-21。

## 0. 先读这三样

| 文档 | 作用 |
| --- | --- |
| [`README.md`](README.md) | 启动步骤、测试、完成度表、环境限制 |
| [`docs/mvp/05_开发任务与验收.md`](docs/mvp/05_开发任务与验收.md) | M01—M16 任务分解、完成定义、验收矩阵 |
| [`docs/mvp/01_文件架构与职责.md`](docs/mvp/01_文件架构与职责.md) | 每个文件该实现什么、函数签名、事务边界 |

`docs/mvp/00—04` 是范围、数据模型、HTTP 契约、后台流程；`docs/mvp/contracts/` 是机器可读契约。

## 1. 东西都在哪

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| 项目 | `D:\LitReview Generator` | 本会话的工作区 |
| PostgreSQL 17.9 | `D:\review-mvp-pg` | 免安装版，只监听 127.0.0.1:5432 |
| Redis 5.0.14 | `<项目>\.data\redis` | 只监听 127.0.0.1:6379，**关闭持久化** |
| 旧位置 | `C:\Users\别浪慢慢来\Desktop\大创\coder` | **已弃用**，留作退路，确认无误后删除 |

虚拟环境在 `<项目>\.venv`，Python 3.12.13，基础解释器 `D:\anaconda3\python.exe`。

⚠ **项目路径必须保持纯 ASCII**，原因见第 4 节第 1 条。当前名字里的空格**没问题**（已验证
ruff/pytest/celery 控制台脚本、pytest rootdir、Celery 任务发现都正常）。

## 2. 现在是什么状态

**已完成**：M03「搭建项目骨架与统一响应」的验收产物——`common/accounts/projects`、
登录与 CSRF、统一错误 envelope、数据库迁移、依赖锁文件、配置示例、两账号项目隔离验证。

**已实现**：

- `config`（settings/urls/celery/wsgi）、`common`（http/auth/storage/idempotency/locking/models）
- `accounts`（csrf/login/logout/me）、`projects`（CRUD + 乐观锁 + corpus_revision 规则）
- 全部数据库模型（按 `02` 定义，含部分唯一索引、fencing、检查约束）
- 43 条 HTTP 路由（按 `03` 挂载，无尾斜杠）
- M04 `jobs` 幂等受理 / 取消 / 重试 / 进度 / 心跳 / 租约 fencing / 失联恢复；
  `llm` mock 与真实适配器、结构校验、预算、全局并发、峰谷快照结算、缓存与私有产物
- `llm/prompt_loader`（分段、参数渲染、版本哈希）
- `prompts/` 下 7 个提示词模板

**未实现**（调用时返回 `503 SERVICE_UNAVAILABLE` 并注明所属阶段，不会伪装成成功）：
`literature` 上传与 arXiv 导入、`parsing`、`analysis`、`retrieval`、`reviews`。

**测试**：启用 `RUN_M04_REDIS_TEST=1` 实测 `137 passed, 35 skipped`；默认不启用时预期 `136 passed, 36 skipped`。
35 条是 M05+ 的待实现行为；额外 1 条是需显式启用的 Redis 闭环。skip **不是通过**。
`llm.0002_model_call_gate_and_slot` 已应用到本机 PostgreSQL。详见 [M04 实现与验收](docs/mvp/M04_实现与验收.md)。

**未验证**：`compose.yaml` 与 `deploy/backend.Dockerfile` 只过了 56 项静态检查
（`.data/tools/validate_deploy.py`），**没有真实 build / up 过**（本机无 Docker）。
有 Docker 后跑 `python deploy/verify_stack.py`。

**待人工确认**：M01、M02 的其余决策；本次用户已采纳价格配置清单第 14、15 项。
**M04 状态**：代码及替身 / 本机集成测试完成，付费 API 冒烟未执行。M05 及其后待实现。

## 3. 常用命令

```powershell
cd 'D:\LitReview Generator'

# 服务
.venv\Scripts\python.exe .data\tools\pg_ctl_local.py start|stop|status
.venv\Scripts\python.exe .data\tools\redis_local.py   start|stop|status

# 测试与检查
cd backend
..\.venv\Scripts\python.exe manage.py check
..\.venv\Scripts\python.exe -m pytest -q                      # 默认预期 136 passed, 36 skipped
..\.venv\Scripts\python.exe -m pytest tests\test_postgres_constraints.py -v
..\.venv\Scripts\ruff.exe check . ..\deploy

# 端到端（另开终端，注意要 DEBUG=false）
$env:DJANGO_DEBUG='false'
..\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload
# 回到仓库根：
.venv\Scripts\python.exe backend\tests\smoke_http.py
.venv\Scripts\python.exe backend\tests\walkthrough_http.py
```

Celery 在 Windows 上要加 `--pool=solo`：

```powershell
cd backend
..\.venv\Scripts\python.exe -m celery -A config worker --pool=solo --loglevel=INFO
..\.venv\Scripts\python.exe -m celery -A config beat --loglevel=INFO
```

## 4. 这个项目踩过的坑（能省你几小时）

**① PostgreSQL 的安装路径不能含非 ASCII 字符**

Windows 上的已知 bug（[BUG #16926](https://www.postgresql.org/message-id/16926-e4ef345545d185ab%40postgresql.org)），
核心开发者明确表示不会修。报错是：

```
performing post-bootstrap initialization ...
FATAL:  invalid byte sequence for encoding "UTF8": 0xb1
```

**极易误导**——看起来像编码或 locale 问题，但 `--locale=C`、`-E UTF8` 都改不动它。
真正的机制是 initdb 会把「安装目录下 `share/sql_features.txt` 的路径」写进一条 COPY 命令，
路径里的 GBK 字节不是合法 UTF8。**唯一的变量是路径**。
所以项目在 D 盘、PG 在 `D:\review-mvp-pg`——**别把它们搬回含中文的路径**。

**② venv 不能直接搬**

`ruff.exe` / `pytest.exe` / `celery.exe` / `gunicorn.exe` 里嵌了创建时的绝对路径。
换了位置必须删掉重建：`.venv\Scripts\python.exe -m venv` 不行，要用基础解释器
`D:\anaconda3\python.exe -m venv .venv`，再 `pip install -r requirements.lock`。

**③ `__pycache__` 也不能搬**

pytest 的断言重写缓存（`.pyc`）里嵌着**源码绝对路径**，而 robocopy 会保留修改时间，
于是新位置的 pytest 判定缓存有效、直接复用旧字节码——**测试报告里显示的是旧路径**，
你会误判跑的是哪份代码。搬家后必须清掉所有 `__pycache__`、`.pytest_cache`、`.ruff_cache`。
（这是真实踩过的：迁到 D 盘后第一次跑测试，跳过信息里显示的是 C 盘路径。）

**④ `pg_ctl start` 不能用管道捕获输出**

它派生的 `postgres` 会继承输出句柄，`subprocess` 永远等不到 EOF，
表现为「启动中 ...」无限卡住——**而数据库其实已经起来了**。
`pg_ctl_local.py` 已用 `capture=False` 把输出重定向到日志文件规避。

**⑤ 数据库没起来时测试会失败，这是设计如此**

`.env` 里有 `DATABASE_URL` 指向 PostgreSQL；PG 没启动时测试会报连接错误。
连接超时是 5 秒（`DJANGO_DB_CONNECT_TIMEOUT`），所以失败很快，不会挂住。
跑测试前先 `pg_ctl_local.py start`。

**⑥ Secure cookie + 纯 HTTP = 登录必然失败**

`APP_ENV=production` 会让 Session/CSRF cookie 带 `Secure`，纯 HTTP 下客户端不会回传。
`compose.yaml` 里已显式设 `DJANGO_SECURE_COOKIES: false` 并注明：**接入 nginx/TLS 后要改回 true**。

**⑦ 未实现的端点在入参校验之后才返回 503**

这是对的，不是 bug。例如 `POST /projects/{id}/runs` 缺 `Idempotency-Key` 时先得到 **400**，
带上合法幂等键才会走到服务层并返回 503。契约要求的校验不会因为没有业务实现而跳过。

**⑧ 后续业务 skip 不是通过**

见第 2 节。不要把它们当成已完成的能力对外描述。

## 5. 下一步该做什么

按 `05` 的关键路径 `M01 → M03 → M04/M05 → M06 → M07 → M08/M09 → M10 → M11 → M13 → M14/M15 → M16`，
M03 已完成，M04 代码已实现，接下来是 **M05：实现文献接入与文件授权**。

1. 完成 PDF 上传与文件授权，覆盖重复文件、大小限制、跨项目访问。
2. 接通 arXiv 导入任务，复用 M04 `accept_job`；该函数负责记录幂等响应并在事务提交后投递，不要再次投递。
3. 领域 worker 接收 `ExecutionContext`，在短事务中写状态，事务外调用模型 / 网络；用同一 context 提交进度与终态。
4. 用真实 PostgreSQL 跑 M05 测试，再进行带登录态 HTTP 验证。

M04 的真实付费 API 冒烟仍需已核验的价格、日历、密钥与人工确认的预算；当前保持 mock。

**同时有两个需要人工决定、不阻塞写代码的任务**（`👤` 标记，AI 不能替人拍板）：

- **M01 冻结 MVP 与容量**：逐项确认 `docs/mvp/00` 的默认值、每人每周投入时长、
  `PROJECT_DEFAULT_BUDGET_USD` 填多少（**零等于不能调用真实模型**）、DeepSeek 账号由谁提供。
- **M02 建立启动语料与判定标准**：20—30 篇有权处理的论文、标注表、划分调试集与冻结验收集。
  不阻塞写代码，但阻塞 M14 的学术验收。

## 6. 环境自检（一分钟确认没坏）

```powershell
cd 'D:\LitReview Generator'
.venv\Scripts\python.exe .data\tools\pg_ctl_local.py status   # 期望「状态：运行中」
.venv\Scripts\python.exe .data\tools\redis_local.py status   # 期望「状态：运行中」
cd backend
..\.venv\Scripts\python.exe -m pytest -q                     # 默认预期 136 passed, 36 skipped
```

若 PostgreSQL 未运行：`.venv\Scripts\python.exe .data\tools\pg_ctl_local.py start`。
它的安装位置是自动探测的（`--root` > `PG_ASCII_ROOT` > `D:\review-mvp-pg` > `%PUBLIC%\review-mvp-pg`），
并把「二进制与集群都完整」的位置排在只有二进制的半成品之前。

## 7. 边界：不要做的事

- **不要把项目或 PostgreSQL 搬到含非 ASCII 字符的路径**（第 4 节第 1 条）。
- **不要为了让测试变绿而删掉 skip**——那是验收清单。
- **不要在没有真实 PostgreSQL 的情况下声称并发约束已验证**。
  `05` 第 4 节原话：不得仅用 SQLite 通过结果证明 PostgreSQL 并发约束正确。
- **不要修改 `docs/mvp/` 下的文档来迁就实现**。契约变更要同步 `00—04`、OpenAPI、前端类型与测试。
- **不要执行 `.env` 之外的真实付费模型调用**。默认 `LLM_MODE=mock`；
  `real` 需要显式启用且同时具备密钥与已核验价目。
