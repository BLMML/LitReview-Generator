# HTTP 接口契约

2026-10-05 更新：新稿及新 Word 导出使用八个内容槽，固定研究背景→研究思路→研究意义→文献综述→文献述评；前四模块保留原文，述评基于前文自主分析，争议不足允许带说明导出。历史规范以此更新为准，详见 [固定框架与自主述评](Word固定框架与自主述评_2026-10-05.md)。

版本：1.0｜路径：`/api/v1`｜日期：2026-09-19。

配套机器可读契约：[contracts/openapi.json](contracts/openapi.json)。接口字段以本文件和 OpenAPI 同步维护，模型输出字段见 04，数据库字段见 02。下述请求均是设计示例，并非已运行服务。

## 1. 通用规则

### 1.1 身份、权限与请求

- 同域 Session Cookie 登录。`GET /auth/csrf` 取得 `data.csrf_token`，所有写请求携带 `X-CSRFToken`；登录也要校验，登录成功重新取得 token。
- `/auth/csrf`、`/auth/login` 匿名可访问；其余接口登录后访问。未登录访问受保护接口统一 401；CSRF 无效为 403，包括匿名 POST 登录。DRF SessionAuthentication 的默认行为需按此统一，不直接依赖默认状态码。
- 项目及其论文、Run、证据、综述、任务、导出必须属于当前用户。越权查询和写入统一 404，不能仅检查客户端提交的 `project_id`。
- UTF-8 JSON；文件上传使用 multipart。路由无末尾 `/`，写请求不靠重定向补齐地址。
- 未列出的输入字段、排序字段和枚举值返回 400；不接受客户端指定 owner、状态、模型密钥和磁盘路径。
- UUID 使用标准字符串。时间统一 UTC；页码从 1 起。年份不详为 null，不由模型猜测。

### 1.2 包装、分页与文件

成功示例：

```json
{
  "data": { "id": "11111111-1111-4111-8111-111111111111" },
  "meta": { "request_id": "req_example" }
}
```

列表使用 `data: []`，`meta` 为 `request_id/page/page_size/total`；page 默认 1，page_size 默认 20、最大 100。列表按 `created_at DESC, id DESC` 稳定排序；主题和争议例外按 `created_at ASC, id ASC`，依然分页。

错误示例：

```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "内容已被修改，请刷新后重试",
    "details": { "current_version": 3 },
    "retryable": false
  },
  "meta": { "request_id": "req_example" }
}
```

PDF / Word 下载成功直接返回二进制，不包 JSON。读取失败仍返回错误 JSON。PDF 返回 `application/pdf`、inline；Word 返回 DOCX MIME、attachment；文件名按 UTF-8 编码 Content-Disposition，不泄漏存储路径。PDF.js 必须允许整文件 200 加载；实现 Range 时遵守 206/416 语义。

### 1.3 幂等与异步

表中标记 **K** 的 POST 必须带 UUID `Idempotency-Key`；相同用户、方法、规范化路径和 key 的同体重放，返回原受理响应和相同资源 ID。不同体为 `409 IDEMPOTENCY_CONFLICT`。上传请求体比较包含文件 SHA256 和元数据。记录至少保留 7 天。

202 固定返回：

```json
{
  "data": {
    "job_id": "22222222-2222-4222-8222-222222222222",
    "resource_type": "analysis_run",
    "resource_id": "33333333-3333-4333-8333-333333333333"
  },
  "meta": { "request_id": "req_example" }
}
```

前端每 2 秒轮询 Job，页面后台时降为 10 秒，终态停止；网络中断后查询原 Job，不能盲目重新提交付费操作。项目 DTO 的 `active_job_id` 用于刷新恢复。结果资源预建，完成前结果字段为 null，实际执行状态只读其 Job。

## 2. 端点与输入输出

以下均省略 `/api/v1`。输出列是成功响应 `data` 类型；OpenAPI 定义 DTO 每个字段。

### 2.1 账号与项目

| 方法与路径                      | 请求参数 / JSON                                                                        | 成功 | data                                          |
| ------------------------------- | -------------------------------------------------------------------------------------- | ---- | --------------------------------------------- |
| GET `/auth/csrf`                | 无                                                                                     | 200  | `{csrf_token}`，同时设置 cookie               |
| POST `/auth/login`              | `username`、`password` 非空                                                            | 200  | UserDTO，设置 session cookie                  |
| POST `/auth/logout`             | `{}`                                                                                   | 200  | `{logged_out:true}`                           |
| GET `/auth/me`                  | 无                                                                                     | 200  | UserDTO                                       |
| GET `/projects`                 | page、page_size                                                                        | 200  | ProjectDTO[]                                  |
| POST `/projects`                | title 1—120 字符、research_question 5—2,000 字符；output_language 默认且仅允许 `zh-CN` | 201  | ProjectDTO                                    |
| GET `/projects/{project_id}`    | 无                                                                                     | 200  | ProjectDTO                                    |
| PATCH `/projects/{project_id}`  | expected_version；可选 title、research_question（至少一项）                            | 200  | ProjectDTO                                    |
| DELETE `/projects/{project_id}` | JSON：expected_version、expected_corpus_revision，均为正整数                           | 200  | `{project_id,deleted:true}`；逻辑删除整个项目 |

项目 DELETE 要求 Session / CSRF 与所有者校验，锁 Project 后检查项目空闲、项目编辑版本和语料版本；忙碌为 409 PROJECT_BUSY，任一版本不匹配为 409 VERSION_CONFLICT。成功设置 Project.deleted_at，version 递增一次；不改写语料、历史结果或费用账本。所有者重复 DELETE 已删除项目仍返回 200，不再递增，不要求 Idempotency-Key。无归属或不存在为 404。删除后项目列表隐藏记录，项目以及下属论文 / PDF、Run / 证据 / 检索、综述 / 正文 / 核验、Job、Export / Word 的读写和旧幂等重放均为 404。当前没有项目恢复或物理清理接口；底层原文、不可变历史与记账记录保留，内部结算不受隐藏状态阻断。项目删除与论文删除的历史访问规则不同。

ProjectDTO 必含 `id/title/research_question/output_language/version/corpus_revision/latest_run_id/active_job_id/created_at/updated_at`。初始 version 与 corpus_revision 为 1；latest_run_id 指最近创建的 Run，不暗指已成功。title 修改只改 version；研究问题修改同时递增 corpus_revision。

### 2.2 文献与导入

| 方法与路径                                        | 请求                                                        | 成功 | data                                                      |
| ------------------------------------------------- | ----------------------------------------------------------- | ---- | --------------------------------------------------------- |
| GET `/projects/{project_id}/papers`               | page、page_size；可选 included=true/false                   | 200  | PaperDTO[]                                                |
| POST `/projects/{project_id}/papers/upload` **K** | multipart：file 必填；title 可选 1—500 字符                 | 201  | PaperDTO                                                  |
| POST `/projects/{project_id}/imports/arxiv` **K** | `arxiv_ids`：1—10 个不同的合法 arXiv ID，不接受 URL         | 202  | JobAccepted，kind=import_arxiv                            |
| GET `/papers/{paper_id}`                          | 无                                                          | 200  | PaperDTO                                                  |
| PATCH `/papers/{paper_id}`                        | expected_version；title、authors、year、included 中至少一项 | 200  | PaperDTO                                                  |
| DELETE `/papers/{paper_id}`                       | JSON：expected_version（正整数）                            | 200  | `{paper_id,deleted:true,corpus_revision}`；从当前账本移除 |
| GET `/papers/{paper_id}/file`                     | 无                                                          | 200  | PDF 二进制                                                |

PaperDTO 包含 `id/project_id/title/authors/year/source/source_id/source_url/included/version/file_sha256/file_size/page_count/original_filename/file_url/created_at/updated_at`。authors 为作者姓名字符串数组；source 为 `upload|arxiv`；year 为 1000—当前年份或 null。上传未提供 title 时暂用文件名；初始 authors=[]、year=null；模型提取的候选元数据不自动覆盖已保存元数据。

单文件上限 25 MiB / 50 页，校验 PDF 内容与可读性、密码保护和页数。相同项目 SHA256 重复上传返回既有 Paper（成功码仍为 201，响应头可增加 `X-Resource-Reused:true`）：未删除时不新增、不改其 metadata/included；已删除时新的幂等键恢复原记录并纳入，保留原元数据和文件，递增 Paper.version / Project.corpus_revision。同体旧幂等键重放优先返回原响应，不撤销后来发生的删除。新的 arXiv 导入同来源也按此恢复，且恢复受当前名额限制。

arXiv ID 支持新旧合法形式与版本后缀；无版本 ID 在下载前解析并冻结具体版本，去重用具体版本和 PDF 哈希。数据源地址由服务端白名单构造。受理时初始化 Job.result.items，每项含 requested_id、resolved_source_id（未解析为 null）、status（pending/imported/duplicate/failed）、paper_id（可空）和 error（可空）。解析后先持久化 resolved_source_id 再下载；重试复制上次账本，只补 pending/failed 项，沿用已解析具体版本。任一导入失败时 Job failed，成功项保留；取消和中断可以留下 pending 项。

MVP Paper PDF 不可原位覆盖；修改元数据或纳入状态递增 Paper.version 和 Project.corpus_revision。当前账本最多 30 篇未删除 Paper（含取消纳入的）；超限 422。无物理删除接口。

DELETE 要求会话、CSRF、资源归属及项目空闲；越权 / 不存在为 404，忙碌为 409 PROJECT_BUSY，版本不匹配为 409 VERSION_CONFLICT。成功设置 deleted_at、included=false，Paper.version 和 Project.corpus_revision 各递增一次。重复删除已删除记录仍为 200，不再递增；恢复后旧版本的 DELETE 返回冲突。删除后列表（包括 included=false 查询）隐藏记录，PATCH 为 404，新 Run 不再纳入；所属用户的详情、文件及历史证据仍可读。返回体仍使用统一 data/meta 信封，不要求 Idempotency-Key。

### 2.3 分析、主题、争议与证据

| 方法与路径                                      | 请求                                                          | 成功 | data                                          |
| ----------------------------------------------- | ------------------------------------------------------------- | ---- | --------------------------------------------- |
| GET `/projects/{project_id}/runs`               | page、page_size                                               | 200  | RunDTO[]                                      |
| POST `/projects/{project_id}/runs` **K**        | `{}`；冻结当前研究问题与全部 included=true 文献               | 202  | JobAccepted，resource_type=analysis_run       |
| GET `/runs/{run_id}`                            | 无                                                            | 200  | RunDTO                                        |
| GET `/runs/{run_id}/papers/{paper_id}/analysis` | 无                                                            | 200  | PaperAnalysisDTO                              |
| GET `/runs/{run_id}/topics`                     | page、page_size                                               | 200  | TopicDTO[]                                    |
| GET `/runs/{run_id}/disputes`                   | page、page_size；可选 review_status                           | 200  | DisputeDTO[]                                  |
| PATCH `/disputes/{dispute_id}`                  | expected_version、review_status；review_note 可选，拒绝时必填 | 200  | DisputeDTO                                    |
| GET `/runs/{run_id}/evidence/{evidence_id}`     | 无                                                            | 200  | EvidenceDTO                                   |
| GET `/runs/{run_id}/papers/{paper_id}/evidence` | page、page_size；可选 page_number（1—50）                     | 200  | EvidenceDTO[]，按 page_number/ordinal/id 升序 |

创建 Run 要求当前至少 2 篇纳入论文。RunDTO 必含 `id/project_id/research_question/output_language/status/job_id/corpus_revision/analysis_revision/is_stale/created_at/papers/stats/error`；research_question/output_language 是该 Run 的冻结值，历史页面不得用当前 Project 值代替。papers 是最多 30 项 RunPaper 快照账本，包含 paper_id、冻结 title/authors/year、status、parsed_document_id、page_count、error。stats 为 included_count/ready_count/failed_count/year_counts/unknown_year_count；趋势是本语料统计，不代表学科整体。

证据列表允许旧 Run，只读已完整发布的解析产物；论文尚未解析成功时返回 RESULT_NOT_READY。paper_id 必须在该 Run 内；页码超过该论文真实页数返回 VALIDATION_ERROR，合法但无可用片段的页面返回空数组。编辑器用此接口按页选取证据，不能要求用户猜测 UUID，也不能强迫为旧草稿重新调用付费检索。

Run 非 ready 时单篇已成功产物与证据可读；尚不存在的单篇分析返回 409 RESULT_NOT_READY，主题 / 争议列表在 Run 非 ready 时返回同码。旧 Run 可读。修改争议要求 Run ready 且非 stale；只允许修改 review_status/review_note，不能覆盖自动 classification。成功修改递增 Dispute.version 与 Run.analysis_revision。

EvidenceDTO：`id/run_id/paper_id/parsed_document_id/page_number/section/element_type/text/locator/verification/file_url`。element_type=`text|table|formula|figure_caption`；locator 含 granularity=`page|region` 及可空 bbox，bbox 若存在是归一化左上原点 `[x0,y0,x1,y1]`；verification=`text_matched|image_unverified|human_verified`。不可把页级定位渲染成已精确高亮。展示 image_unverified 时明确是视觉转录待核对。

### 2.4 检索与生成

| 方法与路径                             | 请求                                                                                        | 成功 | data                                                     |
| -------------------------------------- | ------------------------------------------------------------------------------------------- | ---- | -------------------------------------------------------- |
| POST `/runs/{run_id}/retrievals` **K** | query 2—2,000 字符；candidate_limit 默认20，5—50；top_k 默认5，1—10且不超过 candidate_limit | 202  | JobAccepted，resource_type=retrieval_result              |
| GET `/retrievals/{retrieval_id}`       | 无                                                                                          | 200  | RetrievalDTO                                             |
| GET `/runs/{run_id}/retrievals`        | page、page_size                                                                             | 200  | RetrievalDTO[]，用于恢复历史查询                         |
| POST `/runs/{run_id}/reviews` **K**    | title 可选 1—120；target_chars 默认3000，2000—4000；style 默认且仅允许 standard             | 202  | JobAccepted，resource_type=review                        |
| GET `/projects/{project_id}/reviews`   | page、page_size                                                                             | 200  | ReviewDTO[]                                              |
| GET `/reviews/{review_id}`             | 无                                                                                          | 200  | ReviewDTO，包含 latest_revision（首次生成未完成为 null） |

创建检索 / 综述要求 Run ready、非 stale。RetrievalDTO 结果 `results` 在执行完成前为 null，成功可为 []（无证据），每项 evidence_id/rank/score/reason；score 为 0—1 相关性排序分，不能显示为事实正确概率。

生成时新建 Review，冻结 run_id 与 analysis_revision；只有完整、结构合法正文才能提交 revision 1。生成失败保留 Review 与 Job，不展示空白“成功综述”。生成不自动提交最终验证：前端随后显式发起 validations。

### 2.5 编辑、核验、导出

| 方法与路径                                         | 请求                                                        | 成功 | data                                         |
| -------------------------------------------------- | ----------------------------------------------------------- | ---- | -------------------------------------------- |
| POST `/reviews/{review_id}/revisions`              | base_revision_number、sections 全量固定内容槽                     | 201  | ReviewRevisionDTO                            |
| GET `/reviews/{review_id}/revisions`               | page、page_size                                             | 200  | ReviewRevisionDTO[]                          |
| GET `/reviews/{review_id}/revisions/{revision_id}` | 无                                                          | 200  | ReviewRevisionDTO                            |
| POST `/reviews/{review_id}/validations` **K**      | revision_id                                                 | 202  | JobAccepted，resource_type=validation_report |
| GET `/reviews/{review_id}/validations`             | page、page_size；可选 revision_id                           | 200  | ValidationDTO[]                              |
| GET `/validations/{validation_id}`                 | 无                                                          | 200  | ValidationDTO                                |
| POST `/reviews/{review_id}/exports` **K**          | revision_id、validation_id；acknowledge_warnings 默认 false | 202  | JobAccepted，resource_type=export            |
| GET `/reviews/{review_id}/exports`                 | page、page_size；可选 revision_id                           | 200  | ExportDTO[]                                  |
| GET `/exports/{export_id}`                         | 无                                                          | 200  | ExportDTO                                    |
| GET `/exports/{export_id}/file`                    | 无                                                          | 200  | DOCX；未 ready 为 409 RESULT_NOT_READY       |

保存正文仅允许已有 revision 的 Review；base_revision_number 必须等于最新版本号，否则 409 VERSION_CONFLICT。版本继承原 analysis_revision，不会因人工改字自动升级为新的分析结果。旧 Run 的草稿允许编辑和核验，但保持 stale 提示。

正文限制：新稿固定八个 section_key（background/approach/significance/theoretical_basis/research_status/achievements/limitations/innovation），对应五个主模块、文献综述两个子模块及述评三个子模块。标题和顺序固定，每槽至少一个 block，全稿最多 100 block，每块 1—600 字符。前五槽 claim 每块恰好引用一条本 Run 可定位 PDF 证据，使用完整原句或原段；述评 claim 可引用 1—20 条前文已用证据，必须含对应的 analysis_role；viewpoint_comparison/root_cause 另须有 1—20 的整数 controversy_number 并按争议配对。transition 只允许独立且无引用的“提供的素材中没有该内容。”。历史五节及六槽继续兼容读取、编辑和核验，新导出需八槽；争议不足允许在 Word 明确说明后导出草稿。

ValidationDTO 只针对指定 revision。执行中 `result/structure_passed/blocks` 为 null；成功后 result=`passed|needs_review`，每 block 显示 structure_ok、support、issues、suggestion。support=`supported|unsupported|uncertain|not_applicable`。passed 只是自动核验通过，仍需人工审阅。

导出必须满足：revision 属于该 Review；validation 指向同一 revision 且任务成功；structure_passed=true。若 needs_review、Run/analysis 过时，或导出并非最新正文，必须 acknowledge_warnings=true，否则 409 WARNINGS_NOT_ACKNOWLEDGED。历史正文核验可以执行，明确版本即可。

Word 正文按固定研究框架输出原文及 PDF 页段行出处，不显示模拟草稿、警示、参考文献或证据附录；核验和警示保留在工作台与后台，审计信息进入文档属性。导出时冻结正文、核验与警示列表；后续编辑不改变已生成文件。新请求可以生成新文件，幂等重放不得重复导出。

### 2.6 任务控制

| 方法与路径                        | 请求                       | 成功 | data                                   |
| --------------------------------- | -------------------------- | ---- | -------------------------------------- |
| GET `/jobs/{job_id}`              | 无                         | 200  | JobDTO                                 |
| GET `/projects/{project_id}/jobs` | page、page_size；可选 kind | 200  | JobDTO[]，含已完成 / 失败导入任务      |
| POST `/jobs/{job_id}/cancel`      | `{}`                       | 200  | JobDTO；本操作自然幂等，无 K           |
| POST `/jobs/{job_id}/retry` **K** | `{}`                       | 202  | JobAccepted，新 Job ID、原 resource_id |

JobDTO 包括 id/project_id/kind/status/stage/progress/resource_type/resource_id/retry_of_job_id/usage/error/result/created_at/started_at/finished_at。progress={completed,total,unit}，total 未知可为 null；前端不能拿预计进度判断成功。usage={input_tokens,output_tokens,estimated_cost_usd,unsettled_call_count}，cost 字符串十进制 USD，仅估算。

取消 queued 可直接 cancelled；running 变 cancelling，当前网络请求结束或超时后再结束；已终态取消返回原状态，不倒退。正常完成与取消竞争时，以持有有效租约的数据库提交结果为准；用户端展示最终状态。

retry 仅允许 failed/cancelled、无活跃 Job、失败原因允许重新尝试且前提已满足，并且该 Job 必须是对应业务资源的最新尝试。资源已有成功结果或已存在较新的尝试，返回 INVALID_JOB_STATE；不能在 J1 失败、J2 成功后重新执行 J1。创建新 Job 指向旧 Job，业务资源保持相同，重试使用原冻结输入；成功阶段可复用。analyze/retrieve/generate_review 的原快照已过时时返回 STALE_RUN，要求创建新 Run；生成时 analysis_revision 变化也视为过时。校验 / 导出固定历史版本可重试。旧 Job 历史不改写。

导出 retry 重新计算过时 / 历史版本等警示。新出现的警示若不在原 Export 冻结的已确认警示集合中，返回 WARNINGS_NOT_ACKNOWLEDGED；用户应新建 Export 并明确确认，不能用旧的 acknowledge_warnings=true 自动覆盖未来新增警示。历史列表的 revision_id 过滤值必须属于当前 Review，否则 404。

## 3. 公共错误码

| HTTP | code                                                                            | 前端处理                                       |
| ---- | ------------------------------------------------------------------------------- | ---------------------------------------------- |
| 400  | VALIDATION_ERROR                                                                | 定位字段错误；details.fields={字段:[提示]}     |
| 401  | AUTH_REQUIRED、INVALID_CREDENTIALS                                              | 登录 / 显示凭证错误，不自动重试密码            |
| 403  | CSRF_FAILED                                                                     | 刷新 token；写请求是否重发按幂等与用户操作判断 |
| 404  | NOT_FOUND                                                                       | 资源不存在或无权访问                           |
| 409  | PROJECT_BUSY                                                                    | 查询 active_job_id，展示当前任务               |
| 409  | IDEMPOTENCY_CONFLICT、VERSION_CONFLICT                                          | 不自动覆盖 / 不换 key 盲发                     |
| 409  | RUN_NOT_READY、RESULT_NOT_READY、STALE_RUN                                      | 等待或引导重新分析                             |
| 409  | WARNINGS_NOT_ACKNOWLEDGED、INVALID_JOB_STATE                                    | 按当前资源状态提示下一步                       |
| 413  | FILE_TOO_LARGE                                                                  | 提示 25 MiB 上限                               |
| 422  | UNSUPPORTED_PDF、PAGE_LIMIT_EXCEEDED、PAPER_LIMIT_EXCEEDED、INSUFFICIENT_PAPERS | 调整文件或纳入集合                             |
| 422  | INVALID_EVIDENCE、VALIDATION_REQUIRED、STRUCTURAL_VALIDATION_FAILED             | 修订证据或完成核验                             |
| 429  | RATE_LIMITED                                                                    | 尊重 Retry-After，限制请求频率                 |
| 503  | SERVICE_UNAVAILABLE                                                             | 保留输入与请求 key，查询或稍后再试             |

后台错误放在 Job.error，结构为 code/message/details/retryable。主要 code：PDF_PARSE_FAILED、ARXIV_FETCH_FAILED、MODEL_TIMEOUT、MODEL_RATE_LIMITED、MODEL_OUTPUT_INVALID、MODEL_AUTH_FAILED、MODEL_BALANCE_INSUFFICIENT、BUDGET_EXCEEDED、WORKER_LOST、INTERNAL_ERROR。客户端不得接收到供应商密钥、原始堆栈或敏感响应头。

请求已受理后失败通过 Job 显示，不把随后发生的供应商错误伪装成最初 HTTP 202 的失败响应。`retryable` 表示相同输入在修复前提后可能重试，不意味着无限自动重试。

## 4. 完整联调顺序

1. csrf → login → csrf → me。
2. POST projects → 上传 2—30 篇，或 imports/arxiv → 等导入 Job 终态。
3. 核对论文元数据与 included → POST runs → 轮询 analyze Job。
4. GET Run / 单篇分析 / topics / disputes → 必要时 PATCH dispute。
5. POST retrievals → GET Job → GET retrieval → GET evidence → 打开 paper/file 的真实页。
6. POST reviews → GET Job → GET review.latest_revision。
7. POST validations → GET Job → GET validation；人工修改后 POST revisions，再次 validations。
8. POST exports（按需明确警示确认）→ GET Job → GET export/file。

每一步保存业务 ID 和版本。所有真实付费阶段另有预算控制，不把此流程作为无需限制的自动循环。

## 5. 变更与前后端协作

- `contracts/openapi.json` 作为接口 Mock 和类型生成输入；机器契约覆盖形状，归属、版本和状态约束由本文件与测试保证。
- 新增可选响应字段兼容 v1；改名、删字段、改状态语义均先更新契约和联调样例，必要时升级 API 大版本。
- 服务端统一异常处理覆盖解析错误、数据库校验、权限和上传错误；代理层超限响应也需可由客户端识别。
- API 文档尚未经过服务端实现验证，首次集成必须逐条跑契约测试；不能用文档 JSON 自身通过校验代替接口测试。

参考：[DRF SessionAuthentication 与 CSRF](https://www.django-rest-framework.org/api-guide/authentication/)。
