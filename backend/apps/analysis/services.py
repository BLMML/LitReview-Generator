"""analysis.services：Run 生命周期。

职责（docs/mvp/01 第 4.4 节）：
- `create_run(actor, project_id, payload, key) → AcceptedJob`
- `execute_run(context, run_id) → AnalysisRun`
- `review_dispute(actor, dispute_id, expected_version, changes) → Dispute`
- `get_run_snapshot(actor_or_context, run_id)`

阶段顺序（docs/mvp/04 第 3 节）：
snapshot → parse → extract → index → topics → disputes → finalize。
任一 RunPaper 失败则 Run 失败，成功阶段保留；不得只把成功论文汇总后标为 ready。

M07—M09 已实现上述全部阶段；只有完整解析、抽取、索引和汇总成功后才发布 Run.ready。

事务边界（docs/mvp/01 第 7.2 节）：
- 受理：Project 行锁内完成幂等查找、忙状态校验、冻结快照、创建 Run / RunPaper / Job；
- worker 阶段：PDF 解析、模型调用在事务外；回填产物与阶段状态在持 Job 锁并复查租约的短事务内。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.analysis.batching import BATCH_REVISION, comparison_batches, input_bound, paper_batches
from apps.analysis.models import (
    REVIEW_STATUS_PENDING,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PAPER_FAILED,
    RUN_PAPER_PENDING,
    RUN_PAPER_PROCESSING,
    RUN_PAPER_READY,
    RUN_PROCESSING,
    RUN_READY,
    AnalysisRun,
    Dispute,
    PaperAnalysis,
    RunPaper,
    Topic,
)
from apps.analysis.schemas import (
    SchemaValidationError,
    validate_disputes,
    validate_paper_analysis,
    validate_topics,
)
from apps.common.auth import ActorContext
from apps.common.http import ApiError, api_error
from apps.common.idempotency import (
    ReplayResult,
    claim_or_replay,
    request_fingerprint,
)
from apps.common.locking import check_execution_lease, lock_project, require_project_idle
from apps.jobs.models import JOB_KIND_ANALYZE, Job
from apps.jobs.services import AcceptedJob, ExecutionContext, accept_job, claim_execution, record_progress
from apps.llm.client import call_structured
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import (
    TASK_ASSIGN_TOPICS,
    TASK_COMPARE_DISPUTES,
    TASK_PAPER_EXTRACT,
    ModelTaskRequest,
)
from apps.parsing.config import effective_config
from apps.parsing.services import parse_paper
from apps.retrieval.index import index_config

logger = logging.getLogger("apps.analysis.services")

PAPER_EXTRACT_PROMPT = "extract_paper.md"
ASSIGN_TOPICS_PROMPT = "assign_topics.md"
COMPARE_DISPUTES_PROMPT = "compare_disputes.md"
EXTRACT_SCHEMA_VERSION = "1.0"


# ── 受理 ──────────────────────────────────────────────────────
def create_run(actor: ActorContext, project_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理分析并冻结 RunPaper。

    payload 为已校验的空对象；要求当前至少 2 篇纳入论文，否则 422 INSUFFICIENT_PAPERS。

    幂等、忙状态校验与提交后投递全部交给 `jobs.services.accept_job`（同一事务内）；
    本函数在其外层事务内预建 Run 与 RunPaper，不重复记录受理响应、也不重复投递。
    """
    from apps.literature.models import Paper
    from apps.projects.models import Project

    if payload:
        raise api_error(
            "VALIDATION_ERROR",
            "创建分析不接受请求字段",
            {"fields": {name: ["未支持的字段"] for name in sorted(payload)}},
        )

    project = Project.objects.filter(pk=project_id, owner=actor.user, deleted_at__isnull=True).first()
    if project is None:
        raise api_error("NOT_FOUND")

    config_snapshot = _build_config_snapshot()

    # 幂等键必须落在**真实 HTTP 路径**上。`accept_job` 内部用的是它自己的
    # `/jobs/{kind}` 约定路径，与 `POST /projects/{id}/runs` 不是同一个作用域，
    # 若交给它构造 key，同名 key 的重放会查不到记录而被 PROJECT_BUSY 挡住。
    #
    # 指纹只包含**客户端请求身份**（本接口的正文必须是空对象）。
    # 把服务端生成的 config_snapshot 放进来会让"部署时改了索引配置"被误判成
    # "同键不同请求体"，于是浏览器对已受理请求的正常重放变成 409 IDEMPOTENCY_CONFLICT。
    # 服务端配置在**首次受理时**冻结进 Run，重放只回放原响应。
    canonical_path = f"/api/v1/projects/{project_id}/runs"
    client_fingerprint = request_fingerprint({"kind": JOB_KIND_ANALYZE})

    with transaction.atomic():
        locked = lock_project(project.pk, owner=actor.user)
        claim = claim_or_replay(
            actor,
            "POST",
            canonical_path,
            uuid.UUID(str(key)),
            client_fingerprint,
        )
        if isinstance(claim, ReplayResult):
            # 同一幂等键重放：返回原受理响应，不建第二套 Run / RunPaper / Job。
            # 正常路径下 accept_job 会先行重放，这里只作防御性兜底。
            return AcceptedJob(
                uuid.UUID(claim.body["job_id"]),
                claim.body["resource_type"],
                uuid.UUID(claim.body["resource_id"]),
                replayed=True,
            )

        papers = list(
            Paper.objects.select_related("file_asset")
            .filter(project=locked, included=True, deleted_at__isnull=True)
            .order_by("created_at", "id")
        )
        minimum = int(settings.MVP_LIMITS["PAPERS_PER_PROJECT_MIN"])
        if len(papers) < minimum:
            raise api_error(
                "INSUFFICIENT_PAPERS",
                details={"included_count": len(papers), "required": minimum},
            )

        maximum = int(settings.MVP_LIMITS["PAPERS_PER_PROJECT_MAX"])
        if len(papers) > maximum:
            raise api_error("PAPER_LIMIT_EXCEEDED", details={"included_count": len(papers), "limit": maximum})

        run = AnalysisRun.objects.create(
            project=locked,
            status=RUN_PROCESSING,
            # 冻结当前语料版本；后续 Project 编辑只会让它变 stale，不会改这里
            corpus_revision=locked.corpus_revision,
            research_question=locked.research_question,
            output_language=locked.output_language,
            config_snapshot=config_snapshot,
        )
        for paper in papers:
            RunPaper.objects.create(
                run=run,
                paper=paper,
                title=paper.title,
                authors=list(paper.authors or []),
                year=paper.year,
                source=paper.source,
                source_id=paper.source_id,
                source_url=paper.source_url,
                file_asset=paper.file_asset,
                file_sha256=paper.file_sha256,
                page_count=paper.page_count,
                status=RUN_PAPER_PENDING,
            )
        run.config_snapshot = {**config_snapshot, "paper_count": len(papers)}
        run.save(update_fields=["config_snapshot", "updated_at"])

        return accept_job(
            actor,
            locked,
            JOB_KIND_ANALYZE,
            {
                "resource_id": run.id,
                # run_id 让 ExecutionContext.run_id 有值，worker 据此校验作用域
                "frozen_input": {"run_id": str(run.id), "config_snapshot": run.config_snapshot},
            },
            idempotency=claim,
        )


def _build_config_snapshot() -> dict[str, Any]:
    """冻结全部阶段的配置。

    解析快照直接使用 `parsing.config.effective_config()` 的结果——与解析阶段
    **同一份**配置，因此缓存身份与执行参数不会漂移。
    每条提示词都先用**真实参数形状**渲染再取版本：`prompt_loader` 要求模板里每个
    占位符都有值，空参会直接抛错，而版本号必须与 `call_structured` 内部校验的来源一致。
    """
    extract_prompt = load_prompt(
        PAPER_EXTRACT_PROMPT,
        {
            "paper_title": "",
            "paper_metadata": "",
            "research_question": "",
            "evidence_chunks": "",
            "allowed_evidence_ids": "",
        },
        task_type=TASK_PAPER_EXTRACT,
    )
    topics_prompt = load_prompt(
        ASSIGN_TOPICS_PROMPT,
        {
            "research_question": "",
            "paper_analyses": "",
            "allowed_paper_ids": "",
            "allowed_evidence_ids": "",
            "max_topics": int(settings.TOPICS_MAX),
            "output_language": "",
        },
        task_type=TASK_ASSIGN_TOPICS,
    )
    disputes_prompt = load_prompt(
        COMPARE_DISPUTES_PROMPT,
        {
            "research_question": "",
            "topic_name": "",
            "candidate_papers": "",
            "allowed_paper_ids": "",
            "allowed_evidence_ids": "",
            "max_disputes": int(settings.DISPUTES_MAX),
            "output_language": "",
        },
        task_type=TASK_COMPARE_DISPUTES,
    )
    model_config = {
        "model": settings.DEEPSEEK_MODEL,
        "thinking": settings.DEEPSEEK_THINKING_MODE,
        "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
        "temperature": 0,
    }
    return {
        "parsing": effective_config(None),
        "extraction": {
            "PAPER_TEMPLATE_VERSION": extract_prompt.version,
            "SCHEMA_VERSION": EXTRACT_SCHEMA_VERSION,
            "MODEL_CONFIG": dict(model_config),
        },
        "index": {
            **index_config(),
            "artifact_version": settings.INDEX_ARTIFACT_VERSION,
        },
        "topics": {
            "template_version": topics_prompt.version,
            "schema_version": EXTRACT_SCHEMA_VERSION,
            "max_topics": int(settings.TOPICS_MAX),
            "batch_revision": BATCH_REVISION,
            "papers_per_batch": int(settings.ANALYSIS_PAPERS_PER_BATCH),
            "input_token_bound": int(settings.LLM_MAX_INPUT_TOKENS),
            "model_config": dict(model_config),
        },
        "disputes": {
            "template_version": disputes_prompt.version,
            "schema_version": EXTRACT_SCHEMA_VERSION,
            "max_disputes": int(settings.DISPUTES_MAX),
            # Historical key now bounds one request; it never truncates a topic.
            "max_papers_per_topic": int(settings.DISPUTES_MAX_PAPERS_PER_TOPIC),
            "batch_revision": BATCH_REVISION,
            "candidate_policy": "analysis-cited-evidence-v1",
            "input_token_bound": int(settings.LLM_MAX_INPUT_TOKENS),
            "model_config": dict(model_config),
        },
    }


def get_run_snapshot(actor_or_context: Any, run_id: Any) -> dict[str, Any]:
    """读取冻结快照：Run 不读取后来修改的 Paper 元数据替换快照。

    `actor_or_context` 可以是 `ActorContext`（HTTP 路径，按 owner 过滤）
    或 `ExecutionContext`（worker 路径，按项目过滤）。
    """
    queryset = AnalysisRun.objects.select_related("project")
    if isinstance(actor_or_context, ExecutionContext):
        queryset = queryset.filter(project_id=actor_or_context.project_id)
    else:
        queryset = queryset.filter(project__owner=actor_or_context.user, project__deleted_at__isnull=True)

    run = queryset.filter(pk=run_id).first()
    if run is None:
        raise api_error("NOT_FOUND")

    papers = [
        {
            "run_paper_id": str(run_paper.id),
            "paper_id": str(run_paper.paper_id),
            "title": run_paper.title,
            "authors": list(run_paper.authors or []),
            "year": run_paper.year,
            "source": run_paper.source,
            "source_id": run_paper.source_id,
            "source_url": run_paper.source_url,
            "file_asset_id": str(run_paper.file_asset_id),
            "file_sha256": run_paper.file_sha256,
            "page_count": run_paper.page_count,
            "status": run_paper.status,
            "parsed_document_id": (
                str(run_paper.parsed_document_id) if run_paper.parsed_document_id else None
            ),
        }
        for run_paper in run.run_papers.order_by("created_at", "id")
    ]
    return {
        "run_id": str(run.id),
        "project_id": str(run.project_id),
        "status": run.status,
        "corpus_revision": run.corpus_revision,
        "analysis_revision": run.analysis_revision,
        "research_question": run.research_question,
        "output_language": run.output_language,
        "config_snapshot": dict(run.config_snapshot or {}),
        "papers": papers,
    }


# ── 执行 ──────────────────────────────────────────────────────
def execute_run(context: ExecutionContext, run_id: Any) -> AnalysisRun:
    """worker 入口：按固定阶段推进 snapshot → parse → extract → index → topics → disputes → finalize。

    任一 RunPaper 失败则 Run 失败，**已完整提交的成功产物保留**；
    后续阶段（index / topics / disputes）失败同样使 Run 失败，绝不跳过它们后宣布 ready。
    只有全部阶段成功，才在持有效租约的单一事务里提交 `Run.ready` 与 `Job.succeeded`。
    """
    run, job = _load_scoped_run(context, run_id)
    try:
        config_snapshot = _frozen_config(run)
    except ApiError as exc:
        # 冻结快照缺失/不完整时**必须**把 Run 收尾成失败。
        # 若让异常直接冒到 jobs.tasks，Job 会 failed 而 Run 永远停在 processing——
        # 前端会看到一个既没完成、也没原因的 Run（旧 schema 的 Run 正是这种情形）。
        return _finish_stage_failed(context, run, exc.as_error_body())

    # 上一次尝试若在解析中途死掉，RunPaper 可能停在 processing；按契约交回 pending。
    # 同样的 TOCTOU 约束：判定与写入必须在同一事务、同一 Job 行锁下完成，
    # 否则这次重置本身就可能覆盖已被新尝试接管的资源。
    with transaction.atomic():
        if _locked_current_attempt(context, run) is None:
            raise api_error("INVALID_JOB_STATE", "本次尝试已失效")
        RunPaper.objects.filter(run=run, status=RUN_PAPER_PROCESSING).update(
            status=RUN_PAPER_PENDING, updated_at=_now()
        )

    papers = list(run.run_papers.select_related("paper", "file_asset").order_by("created_at", "id"))
    failed: list[tuple[RunPaper, dict[str, Any]]] = []
    completed = sum(_completed_analysis_for(paper) is not None for paper in papers)
    record_progress(context, stage="snapshot", completed=completed, total=len(papers), unit="paper")

    for run_paper in papers:
        if _completed_analysis_for(run_paper) is not None:
            # 重试只补未完成项：已完整提交的产物不重新调用模型。
            # 注意 RunPaper 与 PaperAnalysis 是**反向 OneToOne**，没有 `analysis_id`
            # 这个字段——直接取它会抛 AttributeError，把最需要复用结果的重试路径打死。
            continue
        context.ensure_not_cancelled()
        try:
            _execute_run_paper(context, run, run_paper, config_snapshot)
        except ApiError as exc:
            if exc.code == "INVALID_JOB_STATE":
                # 取消已提交、租约过期或被取代：一律当作本次尝试失效处理。
                # 此时不能写任何业务产物，也没有资格把 Run 判成 failed——
                # 直接向上抛，由 jobs.tasks 统一收尾（cancelling → cancelled，
                # 租约过期 → 交给恢复扫描判 WORKER_LOST）。
                _release_processing_paper(context, run_paper)
                if _cancel_requested(context):
                    _finish_cancelled(context, run)
                    return run
                raise
            failed.append((run_paper, exc.as_error_body()))
            _mark_paper_failed(context, run_paper, exc.as_error_body())
        except SchemaValidationError as exc:
            payload = {
                "code": "MODEL_OUTPUT_INVALID",
                "message": str(exc),
                "details": {},
                "retryable": False,
            }
            failed.append((run_paper, payload))
            _mark_paper_failed(context, run_paper, payload)

        except Exception:  # noqa: BLE001 - 逐篇失败不拖垮整批记账
            logger.exception("单篇分析失败 run=%s paper=%s", run.id, run_paper.paper_id)
            payload = {
                "code": "INTERNAL_ERROR",
                "message": "单篇分析失败，请查看服务日志",
                "details": {},
                "retryable": True,
            }
            failed.append((run_paper, payload))
            _mark_paper_failed(context, run_paper, payload)

        completed += 1
        record_progress(context, completed=completed, stage="extract")

    if failed:
        return _finalize_run(context, run, failed)

    # 逐篇都成功，进入 Run 级阶段；任一阶段失败都要显式失败，不能宣布 ready
    try:
        record_progress(context, stage="index")
        _stage_index(context, run, config_snapshot)
        record_progress(context, stage="topics")
        topics = _stage_topics(context, run, config_snapshot)
        record_progress(context, stage="disputes")
        _stage_disputes(context, run, topics, config_snapshot)
        record_progress(context, stage="finalize")
    except ApiError as exc:
        if exc.code == "INVALID_JOB_STATE":
            if _cancel_requested(context):
                _finish_cancelled(context, run)
                return run
            raise
        return _finish_stage_failed(context, run, exc.as_error_body())
    except SchemaValidationError as exc:
        return _finish_stage_failed(
            context,
            run,
            {
                "code": "MODEL_OUTPUT_INVALID",
                "message": str(exc),
                "details": {},
                "retryable": False,
            },
        )
    except Exception:  # noqa: BLE001 - 收敛为可读错误，不泄露堆栈
        logger.exception("Run 级阶段失败 run=%s", run.id)
        return _finish_stage_failed(
            context,
            run,
            {
                "code": "INTERNAL_ERROR",
                "message": "分析流程的后续阶段失败，请查看服务日志",
                "details": {},
                "retryable": True,
            },
        )

    return _finalize_ready(context, run, config_snapshot)


def _load_scoped_run(context: ExecutionContext, run_id: Any) -> tuple[AnalysisRun, Job]:
    run = (
        AnalysisRun.objects.select_related("project").filter(pk=run_id, project_id=context.project_id).first()
    )
    if run is None:
        raise api_error("NOT_FOUND")
    # 入口只做"这次尝试还有资格跑吗"的快速校验，不加行锁：
    # 真正的行锁放在每个阶段发布的短事务里，避免把长事务套在模型调用外面。
    job = _locked_job(context, run)
    return run, job


def _completed_analysis_for(run_paper: RunPaper) -> Any:
    """取本篇已完整提交的单篇分析；不存在时返回 None。

    `RunPaper.analysis` 是反向 OneToOne，**没有** `analysis_id` 属性——
    必须走关系查询或 `hasattr` 判断，不能直接取字段。
    只认 `status=ready` 且产物确实存在的组合：半途中断留下的 ready 标记不可复用。
    """
    if run_paper.status != RUN_PAPER_READY:
        return None
    return PaperAnalysis.objects.filter(run_paper_id=run_paper.pk).first()


def _locked_job(context: ExecutionContext, run: AnalysisRun, *, locked: bool = False) -> Job:
    """取当前 Job 并校验本次尝试仍有权写入。

    `check_execution_lease` 只比对 token / 状态 / 取消意图，**不检查租约是否过期**。
    恢复扫描尚未接管时 token 可能没变，这个窗口足以让旧 worker 写入结果，
    因此这里显式补上有效期判断。

    `locked=True` 时对 Job 行加 `SELECT ... FOR UPDATE`，把"校验"与"写入"绑在同一个
    行锁上。**阶段发布必须用 locked=True**：只查不加锁时，另一个事务可以在
    "校验通过"与"写入资源"之间提交取消或换租约，随后本次写入照样落库——
    实测能复现"取消已提交、PaperAnalysis 仍被创建"。
    """
    queryset = Job.objects.filter(pk=context.job_id, project_id=run.project_id)
    if locked:
        queryset = queryset.select_for_update()
    job = queryset.first()
    if job is None:
        raise api_error("INVALID_JOB_STATE", "执行上下文已失效")
    check_execution_lease(job, context.lease_token)
    if job.lease_expires_at is None or job.lease_expires_at <= _now():
        raise api_error("INVALID_JOB_STATE", "执行租约已过期")
    return job


def _lock_job_row(context: ExecutionContext, run: AnalysisRun) -> Job | None:
    """对本 Job 行加 `SELECT ... FOR UPDATE`，锁由**当前事务**持有到提交。

    与 `_locked_job(..., locked=True)` 分开是有意的：那个函数把"加锁"和"校验"
    揉在一起，但调用方有时需要先加锁、再自行决定校验口径（例如终态提交允许 cancelling）。
    这里只负责加锁，不解释状态。
    """
    return Job.objects.select_for_update().filter(pk=context.job_id, project_id=run.project_id).first()


def _owned_job_row(context: ExecutionContext, run: AnalysisRun) -> Job:
    """锁定 Job 行并确认"这是本次尝试"，但**不要求处于可写入状态**。

    终态提交要用它：任务被取消时 Job 本来就不可写入，用 `_locked_job` 会把
    正常的取消收尾也拒掉。这里只回答"我是不是当前尝试"。
    """
    job = Job.objects.select_for_update().filter(pk=context.job_id, project_id=run.project_id).first()
    if not _current_attempt_owns(job, context):
        raise api_error("INVALID_JOB_STATE", "本次尝试已失效")
    return job


def _current_attempt_owns(job: Job, context: ExecutionContext) -> bool:
    """本次尝试是否仍持有写入资格（不加锁的快速判断）。

    用于**清理与收尾**路径：这些路径只在"本次尝试仍然是当前尝试"时才允许改资源。
    已被新尝试取代或已终态的旧 worker 不得回写，否则会把新尝试的状态改坏。
    """
    if job is None or str(job.pk) != str(context.job_id):
        return False
    if int(job.lease_token) != int(context.lease_token):
        return False
    return job.status in ("running", "cancelling")


def _frozen_config(run: AnalysisRun) -> Mapping[str, Any]:
    snapshot = run.config_snapshot or {}
    parsing_config = snapshot.get("parsing")
    extraction = snapshot.get("extraction")
    index_stage = snapshot.get("index")
    topics = snapshot.get("topics")
    disputes = snapshot.get("disputes")
    if not all(
        isinstance(value, Mapping) for value in (parsing_config, extraction, index_stage, topics, disputes)
    ):
        # 缺任何一段都不能继续：Run 级阶段会用新默认值替换旧配置，
        # 那就等于用今天的模板去完成昨天的 Run。
        # **index 段必须在这里一并返回**：漏掉它时 `_stage_index` 拿到的是
        # "没有 index 的配置"，于是回退到当前默认值——流水线跑通、冻结形同虚设。
        raise api_error("INTERNAL_ERROR", "Run 缺少冻结配置快照，请新建分析")
    return {
        "parsing": parsing_config,
        "extraction": extraction,
        "index": index_stage,
        "topics": topics,
        "disputes": disputes,
    }


def _require_frozen_version(current: str, frozen: Any, stage: str) -> None:
    """冻结模板版本与当前模板不一致时显式失败。

    用新模板生成结果却标成旧配置，会让历史 Run 无法解释。
    """
    if frozen != current:
        raise api_error(
            "INTERNAL_ERROR",
            f"冻结的{stage}提示词版本与当前模板不一致，请新建分析",
            {"frozen": frozen, "current": current},
        )


def _execute_run_paper(
    context: ExecutionContext,
    run: AnalysisRun,
    run_paper: RunPaper,
    config_snapshot: Mapping[str, Any],
) -> None:
    """单篇：解析（M06）→ 抽取（M04）→ 校验 → 原子保存。"""
    # **先**确认本次尝试仍有写入资格，再改论文状态。
    # 原实现直接 `run_paper.save()` 把 status 改成 processing：一个持有旧 token 的
    # worker 会先把**已经 ready** 的论文改成 processing，随后解析服务才抛
    # INVALID_JOB_STATE——状态已经被改坏了。
    with transaction.atomic():
        _lock_job_row(context, run)
        _locked_job(context, run)
        RunPaper.objects.filter(pk=run_paper.pk).update(
            status=RUN_PAPER_PROCESSING, error=None, updated_at=_now()
        )
    run_paper.status = RUN_PAPER_PROCESSING
    run_paper.error = None

    record_progress(context, stage="parse")
    document = parse_paper(context, run_paper, dict(config_snapshot["parsing"]))
    _attach_parsed_document(context, run, run_paper, document)

    record_progress(context, stage="extract")
    payload, model_call_ids = _extract_paper(context, run, run_paper, config_snapshot["extraction"])
    _save_analysis(context, run, run_paper, payload, model_call_ids, config_snapshot)


def _attach_parsed_document(
    context: ExecutionContext, run: AnalysisRun, run_paper: RunPaper, document: Any
) -> None:
    """在持 Job 锁并复查租约的短事务内回填解析产物。"""
    with transaction.atomic():
        _locked_job(context, run, locked=True)
        RunPaper.objects.filter(pk=run_paper.pk).update(
            parsed_document=document, error=None, updated_at=_now()
        )
    run_paper.parsed_document = document


def _extract_paper(
    context: ExecutionContext,
    run: AnalysisRun,
    run_paper: RunPaper,
    extraction: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """调用 paper_extract 并完成结构校验；返回 (payload, model_call_ids)。"""
    draft = _evidence_pack(run_paper)
    if not draft["chunks"]:
        raise api_error(
            "PDF_PARSE_FAILED",
            "本篇没有可用证据片段，无法生成要素",
            retryable=False,
        )

    parameters = {
        "paper_title": run_paper.title,
        "paper_metadata": _paper_metadata(run_paper),
        "research_question": run.research_question,
        "evidence_chunks": json.dumps(draft["chunks"], ensure_ascii=False),
        "allowed_evidence_ids": json.dumps(draft["ids"], ensure_ascii=False),
    }
    prompt = load_prompt(PAPER_EXTRACT_PROMPT, parameters, task_type=TASK_PAPER_EXTRACT)
    frozen_template = extraction.get("PAPER_TEMPLATE_VERSION")
    if frozen_template != prompt.version:
        # 用新代码或新提示词生成结果却标成旧配置，会让历史 Run 无法解释
        raise api_error(
            "INTERNAL_ERROR",
            "冻结的提示词版本与当前模板不一致，请新建分析",
            {"frozen": frozen_template, "current": prompt.version},
        )

    request = ModelTaskRequest(
        task_type=TASK_PAPER_EXTRACT,
        template_version=prompt.version,
        schema_version=str(extraction.get("SCHEMA_VERSION", EXTRACT_SCHEMA_VERSION)),
        model_config=dict(extraction.get("MODEL_CONFIG") or {}),
        payload=parameters,
        allowed_evidence_ids=draft["ids"],
        allowed_paper_ids=[str(run_paper.paper_id)],
        # 同一篇的逻辑调用 ID 稳定：重试沿用既有的网络尝试与账本
        logical_call_id=uuid.uuid5(run.id, f"paper_extract:{run_paper.paper_id}"),
    )
    result = call_structured(context, request)
    context.ensure_not_cancelled()
    payload = validate_paper_analysis(dict(result.payload), run_scope=run_paper)
    return payload, [str(result.call_id)]


def _evidence_pack(run_paper: RunPaper) -> dict[str, Any]:
    """本篇证据包：只含本篇解析产物的片段，按页码与 ordinal 升序。"""
    from apps.parsing.models import EvidenceChunk

    if run_paper.parsed_document_id is None:
        return {"chunks": [], "ids": []}
    rows = list(
        EvidenceChunk.objects.filter(parsed_document_id=run_paper.parsed_document_id)
        .order_by("page_number", "ordinal", "id")
        .values("id", "page_number", "section", "text")
    )
    chunks = [
        {
            "evidence_id": str(row["id"]),
            "page_number": row["page_number"],
            "section": row["section"],
            "text": row["text"],
        }
        for row in rows
    ]
    return {"chunks": chunks, "ids": [item["evidence_id"] for item in chunks]}


def _paper_metadata(run_paper: RunPaper) -> str:
    authors = "、".join(run_paper.authors or []) or "未知"
    year = run_paper.year if run_paper.year is not None else "未知"
    return f"标题：{run_paper.title}；作者：{authors}；年份：{year}；来源：{run_paper.source}"


def _save_analysis(
    context: ExecutionContext,
    run: AnalysisRun,
    run_paper: RunPaper,
    payload: Mapping[str, Any],
    model_call_ids: list[str],
    config_snapshot: Mapping[str, Any],
) -> None:
    """原子保存单篇产物并置 ready。"""
    snapshot = {
        "config": dict(config_snapshot["extraction"]),
        "parsing_config_hash": _fingerprint(config_snapshot["parsing"]),
        "evidence_hash": _fingerprint(payload),
        "research_question": run.research_question,
    }
    with transaction.atomic():
        # 先把 Job 行**锁住**，再走常规资格校验。
        # 只校验不加锁时，另一个事务能在"校验通过"与"写入资源"之间提交取消，
        # 随后本次写入照样落库——实测复现过"取消已提交、PaperAnalysis 仍被创建"。
        # 锁由本事务持有到提交，因此校验与写入是不可分割的。
        _lock_job_row(context, run)
        _locked_job(context, run)
        PaperAnalysis.objects.update_or_create(
            run_paper=run_paper,
            defaults={
                "payload": dict(payload),
                "generation_snapshot": snapshot,
                "model_call_ids": model_call_ids,
            },
        )
        RunPaper.objects.filter(pk=run_paper.pk).update(status=RUN_PAPER_READY, error=None, updated_at=_now())


def _attempt_is_current(context: ExecutionContext, run: AnalysisRun) -> bool:
    """本次尝试是否仍是当前尝试（**所有清理与收尾路径的前置条件**）。

    已被恢复扫描接管、已被新尝试取代、或 Job 已终态时，旧 worker 继续跑完循环并不罕见。
    那种情况下它无权回写任何资源状态：把 paper 改回 pending / failed、把已 ready 的 Run
    改成 failed，都是在破坏当前尝试或已完成的结果。
    """
    job = Job.objects.filter(pk=context.job_id, project_id=run.project_id).first()
    return _current_attempt_owns(job, context)


def _locked_current_attempt(context: ExecutionContext, run: AnalysisRun) -> Job | None:
    """在**当前事务**内锁住 Job 行并判定"这仍是本次尝试"；不是则返回 None。

    ⚠ 必须由调用方在 `transaction.atomic()` 内使用，并且**在同一事务里完成写入**。

    这是清理与收尾路径唯一正确的授权方式。原先用 `_attempt_is_current` 先查一次
    布尔值再去写：那个查询在写入事务之外，两个动作之间另一个连接可以提交接管
    （恢复扫描换 token，或新尝试开始），于是旧 worker 照样把当前资源改坏——
    "先检查后使用"（TOCTOU）在并发下不构成授权。
    """
    job = Job.objects.select_for_update().filter(pk=context.job_id, project_id=run.project_id).first()
    if job is None or not _attempt_is_current(context, run):
        return None
    return job


def _release_processing_paper(context: ExecutionContext, run_paper: RunPaper) -> None:
    """取消 / 租约失效时把未完成的论文交回 pending（契约要求）。

    判定与写入在**同一事务、同一 Job 行锁**下完成；接管已发生则整体跳过。
    """
    try:
        with transaction.atomic():
            if _locked_current_attempt(context, run_paper.run) is None:
                logger.info("跳过交回 pending：本次尝试已失效 run_paper=%s", run_paper.pk)
                return
            RunPaper.objects.filter(pk=run_paper.pk, status=RUN_PAPER_PROCESSING).update(
                status=RUN_PAPER_PENDING, updated_at=_now()
            )
    except Exception:  # pragma: no cover - 收尾失败不掩盖原始取消
        logger.warning("交回 pending 失败 run_paper=%s", run_paper.pk)


def _cancel_requested(context: ExecutionContext) -> bool:
    """取消意图以数据库为准；内存标记不作为唯一依据。"""
    return Job.objects.filter(pk=context.job_id, cancel_requested_at__isnull=False).exists()


def _mark_paper_failed(context: ExecutionContext, run_paper: RunPaper, error: Mapping[str, Any]) -> None:
    """记录逐篇失败；判定与写入在同一事务、同一 Job 行锁下完成。"""
    try:
        with transaction.atomic():
            if _locked_current_attempt(context, run_paper.run) is None:
                logger.info("跳过逐篇失败写入：本次尝试已失效 run_paper=%s", run_paper.pk)
                return
            RunPaper.objects.filter(pk=run_paper.pk).update(
                status=RUN_PAPER_FAILED, error=dict(error), updated_at=_now()
            )
    except Exception:  # pragma: no cover - 租约已失效时无法写
        logger.warning("写入论文失败状态失败 run_paper=%s", run_paper.pk)


def _finish_cancelled(context: ExecutionContext, run: AnalysisRun) -> AnalysisRun:
    """取消收尾：Run.cancelled，成功产物保留。

    仅当**确实存在取消意图**且本次尝试仍是当前尝试时才改；否则直接返回当前状态。
    旧尝试不得把已经 ready 的 Run 改成 cancelled。

    这里**不能**复用 `_locked_job`：它校验的是"可写入"租约，而已请求取消的 Job
    本来就不处于可写入状态，用它会把正常的取消收尾也一并拒掉。

    判定与写入在同一事务、同一 Job 行锁下完成：取消意图与"是否仍是当前尝试"
    都必须**在锁内**读取，否则两个动作之间仍能被接管。
    """
    with transaction.atomic():
        job = _locked_current_attempt(context, run)
        if job is None:
            logger.info("跳过取消收尾：本次尝试已失效 run=%s", run.pk)
            return run
        if job.cancel_requested_at is None:
            logger.info("跳过取消收尾：没有取消意图 run=%s", run.pk)
            return run
        # 只改仍在进行中的 Run：已经 ready / failed / cancelled 的终态不再改写
        AnalysisRun.objects.filter(pk=run.pk, status=RUN_PROCESSING).update(
            status=RUN_CANCELLED,
            error={
                "code": "INVALID_JOB_STATE",
                "message": "分析已被取消",
                "details": {},
                "retryable": True,
            },
            updated_at=_now(),
        )
    run.refresh_from_db()
    return run


def _finish_terminal(
    context: ExecutionContext,
    run: AnalysisRun,
    error: Mapping[str, Any],
    *,
    result: Mapping[str, Any] | None = None,
) -> AnalysisRun:
    """失败 / 取消的**统一终态提交**：资源状态与 Job 终态在同一事务、同一判定下写入。

    两条硬约束：
    1. 禁止"先 `run.save()` 再 `fail_job()`"——后者会拒绝失效租约，于是业务状态
       已经被覆写、Job 却保持原样，页面与任务记录互相矛盾。
    2. Job 若已进入 `cancelling`，Run 必须写 **cancelled** 而不是 failed；
       实测过"Job.cancelled / Run.failed"这种分裂终态。
    """
    from django.utils import timezone

    from apps.jobs.services import fail_job, mark_cancelled

    # 判定与写入必须在同一事务、同一 Job 行锁下完成：事务外先查一次的布尔值
    # 在并发下不构成授权（接管可以发生在查询与写入之间）。
    with transaction.atomic():
        job = _locked_current_attempt(context, run)
        if job is None:
            # 旧尝试无权改业务状态，也不再碰 Job（可能已被新尝试接管）
            logger.info("跳过终态提交：本次尝试已失效 run=%s", run.pk)
            return run
        cancelling = job.status == "cancelling" or job.cancel_requested_at is not None
        payload = {
            **dict(error),
            "details": {**dict(error.get("details") or {}), "run_id": str(run.id)},
        }
        if cancelling:
            payload = {
                "code": "INVALID_JOB_STATE",
                "message": "分析已被取消",
                "details": {"run_id": str(run.id)},
                "retryable": True,
            }
        status = RUN_CANCELLED if cancelling else RUN_FAILED

        def finalize(locked_job: Job) -> None:
            AnalysisRun.objects.filter(pk=run.pk).update(
                status=status, error=payload, updated_at=timezone.now()
            )

        if cancelling:
            mark_cancelled(context, job)
            finalize(job)
        else:
            fail_job(context, payload, result=result, finalize_resource=finalize)

    run.refresh_from_db()
    return run


def _finalize_run(
    context: ExecutionContext,
    run: AnalysisRun,
    failed: list[tuple[RunPaper, dict[str, Any]]],
) -> AnalysisRun:
    """逐篇失败时的终态：成功产物已在各自事务提交，这里只决定 Run 状态。"""
    if not failed:  # pragma: no cover - 调用方只在 failed 非空时进入
        raise api_error("INTERNAL_ERROR", "逐篇失败的终态被错误调用")

    _first_paper, first_error = failed[0]
    return _finish_terminal(
        context,
        run,
        {
            "code": first_error.get("code", "PDF_PARSE_FAILED"),
            "message": f"{len(failed)} 篇文献处理失败，成功产物已保留",
            "details": {
                "papers": [{"paper_id": str(paper.paper_id), "error": error} for paper, error in failed]
            },
            "retryable": True,
        },
        result={"run_id": str(run.id)},
    )


def _finish_stage_failed(
    context: ExecutionContext, run: AnalysisRun, error: Mapping[str, Any]
) -> AnalysisRun:
    """Run 级阶段（index / topics / disputes）失败：显式失败，绝不宣布 ready。

    已完整提交的逐篇产物保留，因此重试可以只补 Run 级阶段。
    """
    return _finish_terminal(context, run, error, result={"run_id": str(run.id)})


def _finalize_ready(
    context: ExecutionContext, run: AnalysisRun, config_snapshot: Mapping[str, Any]
) -> AnalysisRun:
    """finalize：按冻结年份算统计、检查覆盖，并在**同一事务**提交 ready 与 succeeded。"""
    from apps.jobs.services import complete_job

    _assert_full_coverage(run)
    summary = _coverage_summary(run)
    with transaction.atomic():
        _locked_job(context, run, locked=True)

        def finalize(job: Job) -> None:
            AnalysisRun.objects.filter(pk=run.pk).update(
                status=RUN_READY,
                error=None,
                index_manifest=dict(run.index_manifest or {}),
                updated_at=_now(),
            )

        complete_job(context, {"run_id": str(run.id), **summary}, finalize_resource=finalize)

    run.status = RUN_READY
    run.error = None
    return run


def _assert_full_coverage(run: AnalysisRun) -> None:
    """宣布 ready 前的最后一道覆盖检查。

    主题校验已强制"成员并集 ∪ 未归类 = 纳入集合"，这里是**独立复验**：
    防止后续改动绕过校验，或主题记录被其它路径改写后仍宣布成功。
    宁可显式失败，也不让一篇文献在结果里静默消失。
    """
    included = {str(value) for value in run.run_papers.values_list("paper_id", flat=True)}
    assigned: set[str] = set()
    for values in Topic.objects.filter(run=run).values_list("paper_ids", flat=True):
        assigned |= {str(value) for value in (values or [])}
    unassigned = {str(value) for value in (run.config_snapshot or {}).get("unassigned_paper_ids", [])}
    missing = sorted(included - (assigned | unassigned))
    if missing:
        raise api_error(
            "INTERNAL_ERROR",
            f"分析覆盖不完整：{len(missing)} 篇纳入文献既未归类也未列入未归类",
            {"missing_paper_ids": missing[:10]},
        )


def _coverage_summary(run: AnalysisRun) -> dict[str, Any]:
    """按**冻结的 RunPaper 年份**统计，并检查覆盖；不读取当前 Paper。"""
    year_counts: dict[str, int] = {}
    unknown_year = 0
    ready = 0
    for run_paper in run.run_papers.all():
        if run_paper.status == RUN_PAPER_READY:
            ready += 1
        if run_paper.year is None:
            unknown_year += 1
        else:
            key = str(run_paper.year)
            year_counts[key] = year_counts.get(key, 0) + 1

    # paper_ids 是 JSONField，取回来是 list —— 不能直接塞进 set()
    flat: set[str] = set()
    for values in Topic.objects.filter(run=run).values_list("paper_ids", flat=True):
        flat |= {str(value) for value in (values or [])}
    unassigned = {str(value) for value in (run.config_snapshot or {}).get("unassigned_paper_ids", [])}
    return {
        "included_count": run.run_papers.count(),
        "ready_count": ready,
        "topic_count": Topic.objects.filter(run=run).count(),
        "dispute_count": Dispute.objects.filter(run=run).count(),
        "assigned_paper_count": len(flat),
        # 只统计"既不在任何主题、又确实没被归类"的论文
        "unassigned_paper_count": len(unassigned - flat),
        "year_counts": year_counts,
        "unknown_year_count": unknown_year,
    }


# ── index 阶段 ────────────────────────────────────────────────
def _stage_index(context: ExecutionContext, run: AnalysisRun, config_snapshot: Mapping[str, Any]) -> Any:
    """建立 Run 专属索引并登记为产物；同事务写入 index_asset 与 index_manifest。"""
    import io

    from apps.common.models import FileAsset
    from apps.common.storage import commit_asset, discard_staged, resolve_storage_key, stage_file
    from apps.projects.models import Project
    from apps.retrieval.index import build_index, effective_index_config, encode_reference
    from apps.retrieval.services import run_snapshot_for

    snapshot = run_snapshot_for(run)
    # 用**冻结**的索引配置构建：读全局默认值会让部署改配置后历史 Run 产出不同索引，
    # 而快照仍然声称是当时的参数。
    reference = build_index(snapshot, config=effective_index_config(config_snapshot.get("index")))
    raw = encode_reference(reference)
    limit = int(settings.INDEX_ARTIFACT_MAX_BYTES)
    if len(raw) > limit:
        raise api_error(
            "VALIDATION_ERROR",
            "索引产物超过体积上限",
            {"size_bytes": len(raw), "limit_bytes": limit},
        )

    staged = None
    committed_path: Any = None
    asset: Any = None
    try:
        staged = stage_file(
            io.BytesIO(raw),
            limit_bytes=limit,
            original_filename=f"index-{run.id}.json",
            media_type="application/json",
            suffix=".index.json",
        )
        with transaction.atomic():
            current = _locked_job(context, run, locked=True)
            project = Project.objects.get(pk=run.project_id)
            asset = commit_asset(project, staged, FileAsset.PURPOSE_INDEX, source_job=current)
            # commit_asset 已把文件移到最终位置。**不要在这里就放弃清理责任**：
            # `with` 块退出只代表"准备提交"，真正 COMMIT 发生在块外，
            # 那时仍可能抛 DatabaseError。提前清空 committed_path 会让
            # 提交阶段失败变成无人清理的孤儿文件。
            committed_path = resolve_storage_key(asset.storage_key)
            staged = None
            manifest = dict(reference.manifest)
            AnalysisRun.objects.filter(pk=run.pk).update(
                index_asset=asset, index_manifest=manifest, updated_at=_now()
            )
        # 走到这里事务已提交：文件被已登记的 FileAsset 引用，不再需要清理
        committed_path = None
    except BaseException:
        # 未提交的暂存文件必须删掉：它没有被任何记录引用，留着只会占空间
        discard_staged(staged)
        # 已 move 到最终位置的文件：登记可能随回滚消失（UPDATE 失败）**或**
        # 提交本身失败。两种情况都必须确认"数据库里确实没有这条记录"再清理，
        # 绝不能因为"提交结果不明"就删掉已被引用的文件。
        _remove_orphan(committed_path, asset)
        raise

    run.index_asset = asset
    run.index_manifest = dict(reference.manifest)
    logger.info("索引已发布 run=%s chunks=%s", run.id, reference.manifest.get("chunk_count"))
    return reference


def _remove_orphan(path: Any, asset: Any = None) -> None:
    """删除确认**没有**已提交登记引用的索引文件。

    先查库再删：宁可留下一个可被后续清理的孤儿，也不能删掉已被引用的产物。
    查询本身失败时同样选择保留文件。
    """
    from apps.common.models import FileAsset

    if path is None:
        return
    try:
        if asset is not None and FileAsset.objects.filter(pk=asset.pk).exists():
            return
    except Exception:  # pragma: no cover - 数据库不可用时保守保留
        logger.warning("无法确认索引文件是否被引用，保留 path=%s", path)
        return
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover - 清理失败不影响主流程
        logger.warning("清理孤儿索引文件失败 path=%s", path)


# ── topics 阶段 ───────────────────────────────────────────────
def _stage_topics(context, run, config_snapshot):
    """Analyze every batch, then merge validated topic candidates across batches."""
    config = config_snapshot["topics"]
    papers = _analysis_payload(run)
    if not papers:
        return []
    maximum = int(config.get("papers_per_batch", settings.ANALYSIS_PAPERS_PER_BATCH))
    bound = int(config.get("input_token_bound", settings.LLM_MAX_INPUT_TOKENS))

    def fits(items):
        scope = _run_scope(run, only_papers=[p["paper_id"] for p in items], config_snapshot=config_snapshot)
        return (
            input_bound(TASK_ASSIGN_TOPICS, ASSIGN_TOPICS_PROMPT, _topic_parameters(run, items, scope))
            <= bound
        )

    batches = paper_batches(papers, maximum, fits)
    partials = []
    for number, batch in enumerate(batches):
        context.ensure_not_cancelled()
        scope = _run_scope(run, only_papers=[p["paper_id"] for p in batch], config_snapshot=config_snapshot)
        identity = "assign_topics" if len(batches) == 1 else f"assign_topics:batch:{number}"
        partials.append(_assign_topic_batch(context, run, batch, scope, config_snapshot, identity))
    if len(partials) == 1:
        validated = partials[0]
    else:
        scope = _run_scope(run, config_snapshot=config_snapshot)
        merge_material = {"batch_results": partials}
        parameters = _topic_parameters(run, merge_material, scope)
        if input_bound(TASK_ASSIGN_TOPICS, ASSIGN_TOPICS_PROMPT, parameters) > bound:
            raise api_error("VALIDATION_ERROR", "全部主题归纳结果超过汇总输入上限，不能省略文献后汇总")
        validated = _assign_topic_batch(
            context, run, merge_material, scope, config_snapshot, "assign_topics:merge"
        )
    return _save_topics(context, run, validated)


def _topic_parameters(run, material, scope):
    return {
        "research_question": run.research_question,
        "paper_analyses": _json(material),
        "allowed_paper_ids": _json(sorted(scope.allowed_paper_ids)),
        "allowed_evidence_ids": _json(sorted(_all_evidence(scope))),
        "max_topics": scope.max_topics,
        "output_language": run.output_language,
    }


def _assign_topic_batch(context, run, material, scope, config_snapshot, identity):
    config = config_snapshot["topics"]
    parameters = _topic_parameters(run, material, scope)
    prompt = load_prompt(ASSIGN_TOPICS_PROMPT, parameters, task_type=TASK_ASSIGN_TOPICS)
    _require_frozen_version(prompt.version, config["template_version"], "主题归纳")
    request = ModelTaskRequest(
        task_type=TASK_ASSIGN_TOPICS,
        template_version=prompt.version,
        schema_version=str(config["schema_version"]),
        model_config=dict(config.get("model_config") or config_snapshot["extraction"]["MODEL_CONFIG"]),
        payload=parameters,
        allowed_paper_ids=sorted(scope.allowed_paper_ids),
        allowed_evidence_ids=sorted(_all_evidence(scope)),
        logical_call_id=uuid.uuid5(run.id, identity),
    )
    result = call_structured(context, request)
    context.ensure_not_cancelled()
    return validate_topics(dict(result.payload), run_scope=scope)


def _save_topics(context: ExecutionContext, run: AnalysisRun, validated: Mapping[str, Any]) -> list[Topic]:
    """原子写入主题列表；重试时先替换本 Run 的自动主题，避免重复累积。"""
    with transaction.atomic():
        _locked_job(context, run, locked=True)
        Topic.objects.filter(run=run).delete()
        created = [
            Topic.objects.create(
                run=run,
                name=item["name"],
                description=item["description"],
                paper_ids=list(item["paper_ids"]),
                evidence_ids=list(item["evidence_ids"]),
            )
            for item in validated["topics"]
        ]
        # 未归类论文记进快照：finalize 的覆盖检查要用，且必须可追溯
        snapshot = dict(run.config_snapshot or {})
        snapshot["unassigned_paper_ids"] = list(validated["unassigned_paper_ids"])
        AnalysisRun.objects.filter(pk=run.pk).update(config_snapshot=snapshot, updated_at=_now())
    run.config_snapshot = snapshot
    return created


# ── disputes 阶段 ─────────────────────────────────────────────
def _stage_disputes(
    context: ExecutionContext,
    run: AnalysisRun,
    topics: Sequence[Topic],
    config_snapshot: Mapping[str, Any],
) -> list[Dispute]:
    """同主题内成对比较；没有候选是合法结果，不要求模型凑数。

    **先在事务外把全部主题的候选都算完并校验，再用一个受租约保护的事务整体替换。**
    原实现在进入阶段时先删旧候选、随后逐主题追加：第一个主题提交后第二个主题失败，
    旧的完整候选集合已经被删掉，于是"重试失败"反而把先前完整提交的成功产物弄丢。
    """
    settings_for_disputes = config_snapshot["disputes"]
    # 每次比较的容量使用冻结配置；覆盖全部成员与跨批次论文组合。
    maximum_papers = int(
        settings_for_disputes.get("max_papers_per_topic") or settings.DISPUTES_MAX_PAPERS_PER_TOPIC
    )

    collected: list[dict[str, Any]] = []
    seen = set()
    bound = int(settings_for_disputes.get("input_token_bound", settings.LLM_MAX_INPUT_TOKENS))
    for position, topic in enumerate(topics):
        context.ensure_not_cancelled()
        member_ids = list(dict.fromkeys(str(value) for value in (topic.paper_ids or [])))
        if len(member_ids) < 2:
            continue
        all_scope = _run_scope(run, only_papers=member_ids, config_snapshot=config_snapshot)
        candidates = _candidate_papers(
            run,
            member_ids,
            all_scope,
            cited_only=settings_for_disputes.get("candidate_policy") == "analysis-cited-evidence-v1",
        )
        by_id = {item["paper_id"]: item for item in candidates}

        def fits(ids, by_id=by_id, topic=topic):
            selected = [by_id[pk] for pk in ids]
            return (
                input_bound(
                    TASK_COMPARE_DISPUTES,
                    COMPARE_DISPUTES_PROMPT,
                    _comparison_parameters(run, topic, selected, settings_for_disputes),
                )
                <= bound
            )

        batches = comparison_batches(member_ids, maximum_papers, fits)
        for number, batch in enumerate(batches):
            context.ensure_not_cancelled()
            identity = None if len(batches) == 1 else f"batch:{number}:{_fingerprint(batch)}"
            found = _compare_topic(
                context,
                run,
                topic,
                batch,
                settings_for_disputes,
                position,
                config_snapshot,
                **({"batch_identity": identity} if identity else {}),
            )
            for item in found:
                fingerprint = _fingerprint(item)
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    if len(collected) < int(settings_for_disputes["max_disputes"]):
                        collected.append(item)

    # 全部主题都成功后才替换；此刻才第一次动数据库
    return _save_disputes(context, run, collected)


def _compare_topic(
    context: ExecutionContext,
    run: AnalysisRun,
    topic: Topic,
    member_ids: Sequence[str],
    settings_for_disputes: Mapping[str, Any],
    position: int = 0,
    config_snapshot: Mapping[str, Any] | None = None,
    *,
    batch_identity: str | None = None,
) -> list[dict[str, Any]]:
    """调用模型并校验，**返回候选而不落库**。

    落库由 `_stage_disputes` 在所有主题都成功之后一次性完成：
    逐主题落库时，第一个主题提交后第二个主题失败，会把旧的完整候选集合留成半成品。
    """
    from apps.llm.schemas import TASK_COMPARE_DISPUTES

    scope = _run_scope(run, only_papers=member_ids, config_snapshot=config_snapshot)
    candidates = _candidate_papers(
        run,
        member_ids,
        scope,
        cited_only=settings_for_disputes.get("candidate_policy") == "analysis-cited-evidence-v1",
    )
    parameters = _comparison_parameters(run, topic, candidates, settings_for_disputes)
    evidence_by_paper = {
        item["paper_id"]: {e["evidence_id"] for e in item["evidence"]} for item in candidates
    }
    scope = _RunScope(set(member_ids), evidence_by_paper, scope.max_topics, scope.max_disputes)
    prompt = load_prompt(COMPARE_DISPUTES_PROMPT, parameters, task_type=TASK_COMPARE_DISPUTES)
    _require_frozen_version(prompt.version, settings_for_disputes["template_version"], "争议比较")

    request = ModelTaskRequest(
        task_type=TASK_COMPARE_DISPUTES,
        template_version=prompt.version,
        schema_version=str(settings_for_disputes["schema_version"]),
        model_config=dict(settings_for_disputes["model_config"]),
        payload=parameters,
        allowed_paper_ids=sorted(scope.allowed_paper_ids),
        allowed_evidence_ids=sorted(_all_evidence(scope)),
        # 逻辑调用 ID 必须**跨重试稳定**：用 Topic.id 不行——重试会重建主题、
        # ID 全变，于是每次重试都变成新的付费调用。改用"第几个主题"这个稳定位置。
        logical_call_id=uuid.uuid5(
            run.id,
            f"compare_disputes:{position}:{topic.name}" + (f":{batch_identity}" if batch_identity else ""),
        ),
    )
    result = call_structured(context, request)
    context.ensure_not_cancelled()
    validated = validate_disputes(dict(result.payload), run_scope=scope)
    return list(validated["disputes"])


def _comparison_parameters(run, topic, candidates, config):
    return {
        "research_question": run.research_question,
        "topic_name": topic.name,
        "candidate_papers": _json(candidates),
        "allowed_paper_ids": _json(sorted(item["paper_id"] for item in candidates)),
        "allowed_evidence_ids": _json(
            sorted({e["evidence_id"] for item in candidates for e in item["evidence"]})
        ),
        "max_disputes": int(config["max_disputes"]),
        "output_language": run.output_language,
    }


def _save_disputes(
    context: ExecutionContext, run: AnalysisRun, disputes: Sequence[Mapping[str, Any]]
) -> list[Dispute]:
    """在**一个**受租约保护的事务里整体替换本 Run 的自动争议候选。

    调用方必须已经收集并校验完所有主题的候选；本函数只做数据库替换。
    空集合同样是一次**完整**结果（"确实没有争议候选"），不是"还没跑完"。
    人工复核字段不跨重试保留——重试意味着自动产物重新生成，
    旧的 `confirmed` / `rejected` 结论不能套用到新候选上。
    """
    with transaction.atomic():
        _locked_job(context, run, locked=True)
        Dispute.objects.filter(run=run).delete()
        created = [
            Dispute.objects.create(
                run=run,
                left_paper_id=item["left_paper_id"],
                right_paper_id=item["right_paper_id"],
                left_claim=dict(item["left_claim"]),
                right_claim=dict(item["right_claim"]),
                conditions=dict(item["conditions"]),
                classification=item["classification"],
                reason=item["reason"],
                review_status=REVIEW_STATUS_PENDING,
            )
            for item in disputes
        ]
    return created


# ── 争议人工复核（M09 数据面就绪；此处只提供入口）─────────────
def review_dispute(
    actor: ActorContext,
    dispute_id: Any,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Any:
    """人工确认 / 拒绝争议。

    只允许修改 review_status / review_note，不能覆盖自动 classification。
    成功修改递增 Dispute.version 与 Run.analysis_revision；
    rejected 必须带非空说明。

    **前置约束（`03` 第 2.4 节、`00` 第 97 行）**：Run 必须 ready、
    语料未过时、且项目当前空闲（没有活跃 Job）。缺任一项都返回 409 且不改任何记录——
    否则尚未完成或已过时的分析可以被复核，后台检索 / 生成执行期间也能变更分析版本，
    破坏"每项目一个活跃写入"的串行约定。
    """
    from apps.analysis.models import REVIEW_STATUS_REJECTED, Dispute

    allowed = ("review_status", "review_note")
    if not any(name in changes for name in allowed):
        raise api_error(
            "VALIDATION_ERROR",
            "至少提供 review_status / review_note 之一",
            {"fields": {name: ["未提供可修改字段"] for name in allowed}},
        )

    with transaction.atomic():
        # 先锁 Project，与创建 Run / 检索受理保持**同一把锁顺序**，避免死锁
        dispute = (
            Dispute.objects.select_related("run", "run__project")
            .filter(pk=dispute_id, run__project__owner=actor.user, run__project__deleted_at__isnull=True)
            .first()
        )
        if dispute is None:
            raise api_error("NOT_FOUND")

        project = lock_project(dispute.run.project_id, owner=actor.user)
        require_project_idle(project)

        run = AnalysisRun.objects.select_for_update().get(pk=dispute.run_id)
        if run.status != RUN_READY:
            raise api_error("RESULT_NOT_READY", details={"status": run.status})
        if int(run.corpus_revision) != int(project.corpus_revision):
            raise api_error("STALE_RUN", details={"corpus_revision": run.corpus_revision})

        # 争议行锁必须在这里取：上面已按 Project → Run 的顺序加锁，
        # 争议本身放在最后，锁顺序与其它写路径一致。
        # 记下主键：`select_for_update()` 会丢弃 `select_related` 的缓存，
        # 于是下面的 `dispute.run` 会变成一次新查询，回来的对象是**新实例**。
        dispute_pk = dispute.pk
        dispute = Dispute.objects.select_for_update().get(pk=dispute_pk)
        if int(dispute.version) != int(expected_version):
            raise api_error("VERSION_CONFLICT", details={"current_version": dispute.version})

        dirty: list[str] = []
        if "review_status" in changes:
            status = str(changes["review_status"])
            if status != dispute.review_status:
                dispute.review_status = status
                dirty.append("review_status")
        if "review_note" in changes:
            note = str(changes["review_note"] or "").strip()
            if note != dispute.review_note:
                dispute.review_note = note
                dirty.append("review_note")

        if dispute.review_status == REVIEW_STATUS_REJECTED and not dispute.review_note:
            raise api_error(
                "VALIDATION_ERROR",
                "拒绝争议必须填写说明",
                {"fields": {"review_note": ["拒绝时必填"]}},
            )
        if not dirty:
            return dispute

        dispute.version += 1
        dispute.reviewed_by = actor.user
        dispute.reviewed_at = _now()
        dispute.save(
            update_fields=[
                *dirty,
                "version",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
        # 人工复核改变分析结论：递增 Run.analysis_revision，历史综述据此显示过时
        AnalysisRun.objects.filter(pk=dispute.run_id).update(
            analysis_revision=run.analysis_revision + 1, updated_at=_now()
        )
        return dispute


def _now() -> Any:
    from django.utils import timezone

    return timezone.now()


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ── 校验作用域与模型载荷 ──────────────────────────────────────
@dataclass(frozen=True)
class _RunScope:
    """`validate_topics` / `validate_disputes` 需要的白名单作用域。"""

    allowed_paper_ids: set[str]
    evidence_ids_by_paper: dict[str, set[str]]
    max_topics: int = 20
    max_disputes: int = 50


def _run_scope(
    run: AnalysisRun,
    *,
    only_papers: Sequence[str] | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
) -> _RunScope:
    """构造校验作用域：论文白名单 + 每篇允许引用的证据集合 + 该阶段的冻结上限。

    `only_papers` 用于争议阶段——同主题才进入比较范围，因此白名单必须收窄到该主题成员，
    否则模型可以引用主题外论文，而主题外论文并不在提示词的候选里。

    数量上限取自**冻结配置**：读当前 `settings` 会让同一 Run 在不同部署下
    把合法输出判坏（全局调小）或接受本应拒绝的输出（全局调大）。
    """
    from apps.parsing.models import EvidenceChunk

    pairs = list(
        run.run_papers.filter(parsed_document_id__isnull=False)
        .values_list("paper_id", "parsed_document_id")
        .order_by("created_at", "id")
    )
    wanted = {str(value) for value in only_papers} if only_papers is not None else None
    evidence_by_paper: dict[str, set[str]] = {}
    document_to_paper: dict[str, str] = {}
    for paper_id, document_id in pairs:
        paper_key = str(paper_id)
        if wanted is not None and paper_key not in wanted:
            continue
        document_to_paper[str(document_id)] = paper_key
        evidence_by_paper.setdefault(paper_key, set())

    if document_to_paper:
        for chunk_id, document_id in EvidenceChunk.objects.filter(
            parsed_document_id__in=list(document_to_paper)
        ).values_list("id", "parsed_document_id"):
            paper_key = document_to_paper.get(str(document_id))
            if paper_key is not None:
                evidence_by_paper.setdefault(paper_key, set()).add(str(chunk_id))

    frozen = config_snapshot or {}
    topics_stage = frozen.get("topics") if isinstance(frozen.get("topics"), Mapping) else {}
    disputes_stage = frozen.get("disputes") if isinstance(frozen.get("disputes"), Mapping) else {}
    return _RunScope(
        allowed_paper_ids=set(evidence_by_paper),
        evidence_ids_by_paper=evidence_by_paper,
        max_topics=int(topics_stage.get("max_topics") or settings.TOPICS_MAX),
        max_disputes=int(disputes_stage.get("max_disputes") or settings.DISPUTES_MAX),
    )


def _all_evidence(scope: _RunScope) -> set[str]:
    result: set[str] = set()
    for values in scope.evidence_ids_by_paper.values():
        result |= values
    return result


def _analysis_payload(run: AnalysisRun) -> list[dict[str, Any]]:
    """提交给主题归纳的单篇要素；已就绪的论文才参与。"""
    rows = (
        PaperAnalysis.objects.select_related("run_paper")
        .filter(run_paper__run=run, run_paper__status=RUN_PAPER_READY)
        .order_by("run_paper__created_at", "run_paper_id")
    )
    result = []
    for analysis in rows:
        run_paper = analysis.run_paper
        result.append(
            {
                "paper_id": str(run_paper.paper_id),
                "title": run_paper.title,
                "year": run_paper.year,
                **dict(analysis.payload or {}),
            }
        )
    return result


def _candidate_papers(
    run: AnalysisRun, member_ids: Sequence[str], scope: _RunScope, *, cited_only=False
) -> list[dict[str, Any]]:
    """争议比较的候选：主题成员的要素 + 带 evidence_id 的原文片段。

    只放入该主题成员的证据——跨主题的证据不应出现在比较材料里。
    """
    from apps.parsing.models import EvidenceChunk

    analyses = {str(item["paper_id"]): item for item in _analysis_payload(run)}
    document_by_paper = {
        str(paper_id): str(document_id)
        for paper_id, document_id in run.run_papers.filter(parsed_document_id__isnull=False).values_list(
            "paper_id", "parsed_document_id"
        )
    }
    documents = [document_by_paper[paper_id] for paper_id in member_ids if paper_id in document_by_paper]
    chunks: dict[str, list[dict[str, Any]]] = {}
    if documents:
        for chunk in (
            EvidenceChunk.objects.filter(parsed_document_id__in=documents)
            .order_by("page_number", "ordinal", "id")
            .values("id", "page_number", "section", "text", "parsed_document_id")
        ):
            for paper_id, document_id in document_by_paper.items():
                if document_id == str(chunk["parsed_document_id"]):
                    chunks.setdefault(paper_id, []).append(
                        {
                            "evidence_id": str(chunk["id"]),
                            "page_number": chunk["page_number"],
                            "section": chunk["section"],
                            "text": chunk["text"],
                        }
                    )
                    break

    result = []
    for paper_id in member_ids:
        if paper_id not in scope.allowed_paper_ids:
            continue
        cited = _cited_evidence(analyses.get(paper_id, {})) if cited_only else set()
        result.append(
            {
                "paper_id": paper_id,
                "analysis": analyses.get(paper_id, {}),
                "evidence": [
                    chunk
                    for chunk in chunks.get(paper_id, [])
                    if not cited_only or chunk["evidence_id"] in cited
                ],
            }
        )
    return result


def _cited_evidence(value):
    if isinstance(value, Mapping):
        result = {str(pk) for pk in value.get("evidence_ids", [])}
        for item in value.values():
            result.update(_cited_evidence(item))
        return result
    if isinstance(value, list):
        return {pk for item in value for pk in _cited_evidence(item)}
    return set()


def _fingerprint(payload: Any) -> str:
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def claim_run_execution(job_id: Any) -> ExecutionContext:
    """worker 领取执行资格；薄封装，便于测试与调用方统一入口。"""
    return claim_execution(job_id)
