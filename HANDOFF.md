# 交接说明（给接手的 DSH 会话）

> 这份文档面向**接手的 AI 会话**，不是项目介绍。项目介绍看 [`README.md`](README.md)。
> 生成时间：2026-09-21｜**最近更新：2026-10-03（用户 PDF 全流程修复与项目删除）**。

2026-10-03 追加：产品默认 mock 已绑定真实请求 ID，浏览器走查不再替换供应商响应；命名错误夹具不变。分析 / 检索 / 生成 / 核验 / 导出均回写阶段和进度，成功进度与资源发布同事务；终态收起“已受理”提示。Word 版本记录保持完整，模板改为 `docx-template-v2`。用户指定 19 页与 8 页 PDF 的隔离浏览器及真实 Redis/Celery 闭环均跑通，开发库 `alexchen` 名下保留独立测试项目。完整证据和最终复测见 [全流程测试与修复](docs/mvp/全流程测试与修复_2026-10-03.md)。旧失败 Run 不改写，应新建分析；保持 mock，真实模型 / 学术验收仍未完成。

2026-10-03 Word 规范追加：新生成 `review-gen-v3-source-only` / Word `docx-template-v3-source-only`。正文采用固定研究框架、完整原文句段和 PDF 页段行出处，不输出模拟草稿或审计附录。旧稿需重新生成后才能使用新导出。详见 [实现与验证](docs/mvp/Word固定研究框架与原文出处_2026-10-03.md)。

## 0. 先读这三样

2026-10-03 项目删除追加：项目档案提供删除确认，整项目逻辑删除后所有下属资源与旧下载链接关闭；任务期间禁用，校验项目和语料两项版本。`projects.0002_project_soft_delete` 已应用到开发库，Django 已重新启动，现有用户项目保留。当前无恢复接口，底层文件、历史快照和费用账本保留；内部结算不要套用所有者接口的删除过滤。实现与回归证据见 [项目删除实现与验证](docs/mvp/项目删除_实现与验证_2026-10-03.md)。

| 文档                                                             | 作用                                   |
| ---------------------------------------------------------------- | -------------------------------------- |
| [`README.md`](README.md)                                         | 启动步骤、测试、完成度表、环境限制     |
| [`docs/mvp/05_开发任务与验收.md`](docs/mvp/05_开发任务与验收.md) | M01—M16 任务分解、完成定义、验收矩阵   |
| [`docs/mvp/01_文件架构与职责.md`](docs/mvp/01_文件架构与职责.md) | 每个文件该实现什么、函数签名、事务边界 |

`docs/mvp/00—04` 是范围、数据模型、HTTP 契约、后台流程；`docs/mvp/contracts/` 是机器可读契约。

## 1. 东西都在哪

| 组件            | 位置                                     | 说明                                  |
| --------------- | ---------------------------------------- | ------------------------------------- |
| 项目            | `D:\LitReview Generator`                 | 本会话的工作区                        |
| PostgreSQL 17.9 | `D:\review-mvp-pg`                       | 免安装版，只监听 127.0.0.1:5432       |
| Redis 5.0.14    | `<项目>\.data\redis`                     | 只监听 127.0.0.1:6379，**关闭持久化** |
| 旧位置          | `C:\Users\别浪慢慢来\Desktop\大创\coder` | **已弃用**，留作退路，确认无误后删除  |

虚拟环境在 `<项目>\.venv`，Python 3.12.13，基础解释器 `D:\anaconda3\python.exe`。

⚠ **项目路径必须保持纯 ASCII**，原因见第 4 节第 1 条。当前名字里的空格**没问题**（已验证
ruff/pytest/celery 控制台脚本、pytest rootdir、Celery 任务发现都正常）。

## 2. 现在是什么状态

**已实现**：M03「项目骨架与统一响应」、M04「任务与模型调用底座」、
M05「文献接入与文件授权」、M06「解析与证据账本」（2026-09-30 修复复核问题）。M04—M06 的人工签收仍为「待验收」。

**已实现**：

- `config`（settings/urls/celery/wsgi）、`common`（http/auth/storage/idempotency/locking/models）
- `accounts`（csrf/login/logout/me）、`projects`（CRUD + 乐观锁 + corpus_revision 规则）
- 全部数据库模型（按 `02` 定义，含部分唯一索引、fencing、检查约束）
- 45 个 HTTP 操作（按 `03` 挂载，无尾斜杠；包含论文与项目逻辑删除）
- M04 `jobs` 幂等受理 / 取消 / 重试 / 进度 / 心跳 / 租约 fencing / 失联恢复；
  `llm` mock 与真实适配器、结构校验、预算、全局并发、峰谷快照结算、缓存与私有产物
- M05 `literature` 上传校验与去重、元数据编辑与纳入开关、授权 PDF 下载、
  arXiv 受理与逐项账本、部分失败保留与重试补项；`arxiv` 客户端（限流 / 重定向逐跳校验 /
  大小上限 / XXE 防护）；`parsing.pdf` 的 PDF 结构检查（大小 / 页数 / 加密 / 损坏）
- M06 `parsing` 文本层提取、受控 PNG 渲染（不依赖 Pillow）、片段划分（不跨页、600—1200 字符）、
  证据批量读取与跨 Run 引用拒绝、无文本层页面的图像辅助与失败诊断
- M07 `analysis` Run / RunPaper 与配置冻结、逐篇解析（M06）与要素抽取（M04）、
  单篇产物与证据 API、争议人工复核；`execute_run` 的 snapshot → parse → extract 三段
- M08 `retrieval` TF-IDF + 关键词融合召回、查询扩展与重排、候选集合校验、
  `retrieve_evidence` 内部同步入口；索引为 Run 专属 **JSON 产物**（不含 pickle），
  `analysis` 的 index 阶段负责发布，检索按**源摘要**校验后复用
- M09 `analysis` 主题归纳（一文多主题、允许未归类）、同主题内争议候选与条件对齐、
  人工复核、覆盖与年份统计；`execute_run` 的七段流水线全部接通
- M10 `reviews` 五节结构化生成（逐节检索证据、`block_id` 由后端分配、全稿结构检查后
  一次性提交 revision 1）、不可变编辑版本与乐观锁、`is_stale` 计算
- M11 `reviews` 指定版本核验、完整报告、警示确认、首次引用编号、私有 Word 导出；
  失败重试重新检查警示，取消与租约回收同步 Export 状态
- `llm/prompt_loader`（分段、参数渲染、版本哈希）
- `prompts/` 下 7 个提示词模板

**后端主流程已接通**：M11 四个服务入口已经实现。分析流水线七段可自行达到 ready；
核验与导出可以指定已保存历史正文。M12 已实现 Vue 前端，登录、文献与任务账本、分析 / 检索 / 原文、正文编辑、指定版本核验和历史 Word 下载全部接通。

**M11 全量测试记录**：`LLM_MODE=mock`、`RUN_M04_REDIS_TEST=1`，实测 **584 passed / 0 skipped**（358.87 秒）；
默认跳过 Redis 闭环时，会额外跳过 1 条；本轮全量已显式启用并通过该闭环。
其中 31 条是外部独立复核问题的回归（`test_m07_m09_review_regressions.py`）：
两轮复核共 15 类问题，修复后独立检查分别 **21 / 21**、**32 / 32** 通过，详见
[第一轮修复](docs/mvp/M07_M09_复核修复与复验_2026-10-01.md) 与
[第二轮修复](docs/mvp/M07_M09_第二轮复验修复_2026-10-01.md)。
Ruff、Django、迁移一致性与依赖检查全部通过。
M11 原有 7 条 skip 已全部打开，并新增工程回归；默认仅可选 Redis 闭环会跳过。
`llm.0002_model_call_gate_and_slot` 已应用到本机 PostgreSQL。
M04 详见 [M04 实现与验收](docs/mvp/M04_实现与验收.md)，
M05 详见 [M05 实现与验收](docs/mvp/M05_实现与验收.md)，
M06 详见 [M06 实现与验收](docs/mvp/M06_实现与验收.md)，
M07 详见 [M07 实现与验收](docs/mvp/M07_实现与验收.md)，
M08 详见 [M08 实现与验收](docs/mvp/M08_实现与验收.md)，
M09 详见 [M09 实现与验收](docs/mvp/M09_实现与验收.md)，
M10 详见 [M10 实现与验收](docs/mvp/M10_实现与验收.md)，
M11 详见 [M11 实现与验收](docs/mvp/M11_实现与验收.md)；
M05 的独立复核与修复对照见
[M05 复核问题清单](docs/mvp/M05_复核问题清单_2026-09-29.md) 与
[M05 第二轮复核](docs/mvp/M05_第二轮复核_2026-09-29.md)，最终两项修复见
[M05 第三轮复核](docs/mvp/M05_第三轮复核_2026-09-29.md)。

**未验证**：M12 新增 `deploy/frontend.Dockerfile` / `nginx.conf`，Compose 改为前端代理独占回环 8000，Web 内部 expose。
已过 YAML 与暴露面静态检查，**没有真实 build / up 过**（本机无 Docker）。旧 56 项检查包含旧端口假设，不能当作 M12 证据。
有 Docker 后跑 `python deploy/verify_stack.py`。

**待人工确认**：M01、M02 的其余决策；本次用户已采纳价格配置清单第 14、15 项。
**M04 状态**：代码及替身 / 本机集成测试完成，付费 API 冒烟未执行。
**M05 状态**：文献接入已实现，三轮复核共 19 项问题均已有回归覆盖。
第三轮的两项残留问题已补齐代码并经接手方独立核对确认：
账本检查与 UPDATE 在同一事务中持有 Job 锁；下载以异步绝对截止时间取消限流、重定向、响应头和正文等待。
核对同时发现第三轮「账本并发」复现用例的断言被结构性关闭（回退修复仍然通过），
已在 `tests/test_m05_review_regressions.py` 补入可判定的行锁探测守卫并双向验证。
历史记录包含真实 arXiv 导入；接手核对时以新的异步下载路径重跑过一次真实导入并成功，
全程无模型调用。
PDF 难例抽检与真实语料仍属 M02 人工项，M05 保持「待验收」。
**M06 状态**：解析与证据账本已实现，M05 遗留的 3 条 M06 skip 已打开并换成真断言。
本轮复核的 8 类问题已修复；16 项独立检查全部通过，新增 37 项正式回归。
要点：真实 RunPaper 可直接解析；持有效租约原子发布；完整冻结参数控制执行与缓存；
片段不跨页且保持页内顺序；图像逐页调用，保留类型与章节；图像片段一律 `image_unverified`
且 `original_text=null`；文本片段必须匹配同页原文。任一页失败即整篇失败并保留逐页诊断；
损坏缓存明确失败，暂存和事务回滚清理未提交文件。解析版本为 parse-v2，历史产物保留。
详见 [M06 复核与修复对照](docs/mvp/M06_复核问题清单_2026-09-30.md)。
**M07 状态**：分析受理与冻结、逐篇解析与要素抽取、单篇产物与证据 API、争议人工复核已实现；
M07 的 7 条 skip 已打开并换成真断言，新增 `test_m07_analysis.py`（27 条）。
要点：**M07 当时** `index`/`topics`/`disputes`/`finalize` 未实现，故 Run 成功后显式失败并点名
阶段（该机制已在 M09 落地后移除，现在流水线能自己走到 ready）；
幂等键落在真实 HTTP 路径（否则同键重放会被 PROJECT\*BUSY 挡住）；
冻结配置就是解析配置；逐篇失败不拖垮整批但也不虚报；租约过期显式补检。
同时修掉 3 个既有缺陷（都不是 M07 引入的）：
`common/http.py` 兜底分支把 `status_code` 传给不接受它的 `api_error()`，
导致**任何** DRF 库异常都变成 500；`GET /runs/{id}/retrievals` 从未挂载视图（落进 405）；
证据列表接口把模型实例交给 JSON 渲染（500）。三者都已补回归。
真实模型输出、M02 语料人工抽检未完成，M07 保持「待验收」。
**M08 状态**：检索链路已实现；M08 的 2 条 skip 已打开，新增 `test_m08_retrieval.py`（29 条）。
要点：索引**只在 worker 内存中构建**，不落盘也不 pickle——既然不允许接收用户上传的
序列化对象，索性不引入序列化面，因而没有"索引文件被替换 / 缓存与语料不符"这一整类风险；
向量器必须与矩阵一起保存（查询时重新 fit 会静默算错相似度）；
召回是 TF-IDF 余弦 + 关键词覆盖率融合；越界引用有两道拦截（网关白名单 +
服务层候选集合，后者才是权威）；完成前 `results` 为 null、成功但无证据为 `[]`，
两者必须可区分；结果与 `Job.succeeded` 在同一事务提交；重排的逻辑调用 ID 由检索记录 ID
派生，随机 ID 会让重试重复计费。
真实模型输出、召回相关性人工抽检、目标语料规模下的索引性能均未验证，M08 保持「待验收」。
**M09 状态**：主题与争议候选已实现；最后 1 条业务 skip 已打开，新增
`test_m09_topics_disputes.py`（24 条）。`test_run_retrieval.py` **再无 skip**。
要点：索引产物改为 **Run 专属 JSON**（M08 的纯内存方案让 `index_asset` 永远是 null，
是契约缺口），加载按 `run_id` / `analysis_revision` / **源摘要**三重校验，
不符即重建——解析产物重建而索引未更新，会给出指向已不存在内容的排序；
`index.py` 不再依赖 scikit-learn，TF-IDF 与余弦由本项目实现，因此每个数值都能写进
产物并读回校验；争议只在**同一主题内**比较且白名单随之收窄，单篇主题不发起调用；
零争议与单篇主题都是合法结果，不要求模型凑数；人工字段与自动分类严格分离
（StrictSerializer 把"顺手改 classification"挡在 400）。
修掉 1 个既有缺陷：`_frozen_config` 原先在任何 try 之外抛错，
**Job.failed 而 Run 永远停在 processing**，M09 加了 `topics`/`disputes` 两段配置后
所有旧 Run 都会命中；已包进 try 走 `_finish_stage_failed` 正常收尾，并补了回退验证过的回归。
主题归纳目前是**单批调用**（`04` 要求"分批归纳与合并"），语料接近 30 篇时可能触及模型
上下文上限——这是已知简化，不是已完成的分批逻辑。真实模型输出与学术合理性人工抽检
未完成，M09 保持「待验收」。
**M07–M09 外部复核（两轮，共 15 类问题）**：均已修复并纳入正式回归
（`test_m07_m09_review_regressions.py`，31 条）；独立检查分别 **21 / 21**、**32 / 32** 通过。
要点：
(1) `RunPaper.analysis` 是反向 OneToOne，**没有 `analysis_id`**，原先直接取该属性让
"有成功单篇产物时的重试"必然崩溃；
(2) 阶段发布必须**先锁 Job 行**再校验——只校验不加锁时，另一个事务能在窗口内提交取消，
随后本次写入照样落库；且"发布"与"收尾"对租约状态的要求相反（收尾必须能处理已取消的 Job），
因此拆成 `_locked_job(locked=True)` 与 `_lock_job_row`/`_owned_job_row` 两个入口；
(3) 幂等指纹不能含服务端生成的 `config_snapshot`，否则每次部署改配置，
浏览器对已受理请求的正常重放都会变成 409；
(4) **"先检查后使用"不是授权**：清理与收尾的判定必须放进写入所在的**同一事务**，
并持 Job 行锁（`_locked_current_attempt`），否则"检查时有效、写入前被接管"仍会改坏当前资源；
单篇入口也必须先校验再改 status，不能先改成 processing 再等解析服务报错；
(5) `with transaction.atomic()` 退出**不等于已提交**——提交阶段仍可能抛错，
索引清理责任要等块**正常退出**后才解除，且删文件前先确认没有已提交登记；
(6) 冻结配置必须贯通调用链：`_frozen_config` 漏掉 index 段时 `_stage_index` 会静默
回退到当前默认值；检索加载 / 重建与查询向量化同样要用该 Run 的配置；
(7) 争议阶段改为**事务外算完全部候选再一次性替换**，"失败"才不会把先前完整提交的产物删掉。
详见 [第一轮修复](docs/mvp/M07_M09_复核修复与复验_2026-10-01.md) 与
[第二轮修复](docs/mvp/M07_M09_第二轮复验修复_2026-10-01.md)。
**M10 状态**：五节结构化生成与不可变编辑版本已实现；M10 的 6 条 skip 已打开，
`test_review_export.py` 的模块级 skip 已拆开，当时保留的 M11 7 条也已在本阶段打开，
新增 `test_m10_reviews.py`（25 条）。要点：
(1) `block_id` 由**后端**分配，模型自带的 ID 一律忽略——全稿唯一性不能取决于模型自觉；
(2) 单节校验 + 全稿复核缺一不可：`block_id` 唯一与 100 块总量都是跨节约束；
(3) 任一节不合法就不提交任何正文，`latest_revision` 保持 null，
前端据此区分"还没生成好"与"生成了一份正文"；
(4) 人工改字**继承** base 的 `analysis_revision`，不会让过时的分析重新变新；
(5) 每节的检索查询必须**短**：关键词覆盖率是"命中词数 / 查询词数"，
把整段节要求拼进查询会让召回直接落空——第一版就踩了这个坑。
另记录两个**夹具**陷阱（不是产品缺陷，但会让测试看似在验证、实际什么都没验证）：
`make_text_pdf_file` 的内容流是 latin-1，**中文会整体变成 `?`**，词表里只剩文件名，
于是中文研究问题永远命中不了任何片段；以及 M04 网关拒绝同一请求内的重复引用，
替身必须给每个 block 分不同的证据 ID。
2026-10-02 复核的 8 类问题已修复：正文保存 `PROJECT_BUSY`、锁内冻结分析版本、
模型账本下一次尝试恢复、目标篇幅指令、内部检索配置冻结与稳定身份、单节 block 上限、
无证据章节覆盖说明。新增 `test_m10_review_regressions.py`（21 条）与网关回归（5 条），
并统一发布的 Project → Review → Job 锁序、在锁内计算新版本号；取消鉴权 JOIN 只锁 Job。
`review-gen-v2` 冻结总目标及五节篇幅分配；目标不是精确字数保证。旧 v1 未完成综述应
使用新幂等键重新受理，旧幂等重放仍返回原 Job；历史正文可读。
详见 [M10 修复与复验](docs/mvp/M10_复核修复与复验_2026-10-02.md)。
**M11 状态**：版本绑定、结构与语义核验、警示确认、Word 导出和授权下载已实现；
默认每批最多 5 块、按系统提示词和完整原文上界继续分批，报告失败保持三字段 null。
纯承接语以外的 not_applicable 保守转 uncertain；默认 mock 也返回 uncertain。
Export 受理、执行及 retry 共用警示检查，旧确认不能覆盖未来新增警示。
导出发布与失败/取消/恢复统一 Job → Export 锁序，发布前先锁 Project；文件回滚清理自身产物。
新增 snapshots.py，不增加迁移；Word 样例已检查 6 页。真实模型与学术判断仍未签收。
**M12 状态**：`frontend/` 从零实现，样式依据用户指定的 ui-ux-pro-max / Anti-Polish / Raw Aesthetic；设计规则在 `design-system/litreview-generator/MASTER.md`。
版本 `v0.12.0-m12-raw-workbench`。前端严格构建、22 单元测试、9 浏览器 UI 测试通过；隔离 PostgreSQL + 真实 HTTP 浏览器闭环通过，相关后端回归与闭环合计 74 passed / 0 skipped。依赖审计 0 vulnerabilities。
走查入口是 `backend/tests/walkthrough_m12_browser.py`，需显式执行，正式服务层 / 网关 / 账本 / 导出器处理；仅在测试替换 mock 输出和队列投递。
M12 首次联调使用 500000 测试上限，随后用户于 2026-10-02 授权提高长文容量：部署默认和本机 `.env` 的保守输入上限已改为 256000，支持约两万字单篇及双篇比较；浏览器走查已去掉 500000 特例。输出和金额预算不变，多篇合并仍可能超限。默认 mock 固定证据 ID 也可能使真实 Run 失败，不能伪称可用于学术验收。
长文 / 网关 / M11 追加回归 128 passed，部署上限下浏览器复验 1 passed；完整两万字中文单篇请求上界 74337，双篇 140383。配置已写入 `.env`，2026-10-02 16:27 按用户要求重启 Django / worker 并启动 beat，明确继承 256000 上限和 mock 模式。HTTP、worker ping、debug_task 与周期任务均通过；后台进程清单和日志在 `.data/service-logs/20261002-162723/`。详见 [长文输入上限调整](docs/mvp/长文输入上限调整_2026-10-02.md)。

随后用户追加三万字要求，当前默认 / 示例 / `.env` 为 **384000**；134 项长文 / 网关 / M11 回归通过。三万字完整单篇最大测试上界 109890、双篇 211483，未截断证据。17:27 重启 Django / worker / beat 并验证，日志在 `.data/service-logs/20261002-172701/`。截图两份桌面原件与项目哈希一致，实际请求上界 90749 / 56139；页面显示 16:08 的旧失败 Run（早于首次上限重启），应创建新的分析，配置更新不会自动清除历史错误。详细数据见上述调整记录。

2026-10-02 追加论文账本“删除论文”与确认弹窗：逻辑删除释放名额，保留历史证据；同文件新上传 / 新 arXiv 导入可恢复。迁移 `literature.0002_paper_soft_delete` 已应用，Django / worker / beat 已重启并验证。专项 14 passed、相关回归 170 passed、前端单测 22 passed、UI 12 passed、隔离真实浏览器 1 passed；回归与专项有重叠，不累加。详见 [论文删除实现与验证](docs/mvp/论文删除_实现与验证_2026-10-02.md)。
启动、测试、截图、Word 与全部限制见 [M12 实现与验收](docs/mvp/M12_实现与验收.md)。没有数据库迁移，未修改用户 `.env`。
**下一步是 M13**（集成、故障与恢复检查）。

## 3. 常用命令

```powershell
cd 'D:\LitReview Generator'

# 服务
.venv\Scripts\python.exe .data\tools\pg_ctl_local.py start|stop|status
.venv\Scripts\python.exe .data\tools\redis_local.py   start|stop|status

# 测试与检查
cd backend
..\.venv\Scripts\python.exe manage.py check
..\.venv\Scripts\python.exe -m pytest -q                      # 实际结果见 M06 验收记录
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
M03—M12 代码已实现，接下来是 **M13：集成、故障与恢复检查**。
真实难例抽检及各阶段人工签收按验收清单继续执行。

1. 按 `05` 第 4 节矩阵验证两账号隔离、忙碌冲突、网络中断与幂等、批量导入部分失败、取消及 worker 失联恢复。
2. 已按用户追加要求提高保守输入上限到 384000，覆盖三万字单篇及双篇；继续验证多篇长文合并并按需要实现分批，记录默认 mock 样例 ID 的限制；不靠测试特例放宽付费配置。
3. 有 Docker 后执行真实构建、Compose 启动、前端同域登录、私有下载、worker / beat、TLS 与 cookie 检查。
4. 执行隔离备份恢复，校验文件哈希、历史正文 / 核验绑定、权限与任务状态。
5. 保留已有回归，结合 M12 浏览器走查复验修复；页面人工验收仍需负责人确认，真实模型与学术盲评属于 M14。

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
..\.venv\Scripts\python.exe -m pytest -q                     # 默认仅跳过可选 Redis 闭环，见 M11 记录
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
