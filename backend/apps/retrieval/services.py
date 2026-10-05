"""retrieval.services：外部受理检索任务与内部同步检索入口。

职责（docs/mvp/01 第 4.5 节）：
- `create_retrieval(actor, run_id, payload, key) → AcceptedJob`
- `execute_retrieval(context, retrieval_id) → RetrievalResult`
- `retrieve_evidence(context, run_snapshot, query, limits) → RankedEvidence[]`

关键约束：`retrieve_evidence` 是内部同步阶段函数，在当前 worker 中执行，
使用同一个父 Job 的取消、预算和调用记录。
**不能为每个章节再创建一个公开 retrieve Job**，否则会与"每项目一个活跃 Job"冲突。

从模型重排结果读取的 evidence ID 必须属于候选集合；score 不是事实正确率。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.analysis.models import RUN_READY, AnalysisRun, RunPaper
from apps.common.auth import ActorContext
from apps.common.http import api_error
from apps.common.locking import check_execution_lease, lock_project
from apps.jobs.models import JOB_KIND_RETRIEVE, Job
from apps.jobs.services import AcceptedJob, ExecutionContext, accept_job, claim_execution, record_progress
from apps.llm.client import call_structured
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import (
    TASK_EXPAND_QUERY,
    TASK_RERANK,
    ModelTaskRequest,
)
from apps.parsing.models import EvidenceChunk
from apps.retrieval.index import build_index, candidate_metadata, load_published, recall
from apps.retrieval.models import RetrievalResult

logger = logging.getLogger("apps.retrieval.services")

NOT_IMPLEMENTED_MESSAGE = "retrieval.services 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"

EXPAND_PROMPT = "expand_query.md"
RERANK_PROMPT = "rerank_evidence.md"
SCHEMA_VERSION = "1.0"
CONFIG_VERSION = "retrieval-v1"


@dataclass(frozen=True)
class RankedEvidence:
    """一次检索的有序结果；`rank` 从 1 连续起。"""

    evidence_id: str
    rank: int
    score: float
    reason: str


# ── 受理 ──────────────────────────────────────────────────────
def create_retrieval(actor: ActorContext, run_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理检索；要求 Run ready 且非 stale，否则 409 RUN_NOT_READY / STALE_RUN。"""
    from apps.projects.models import Project

    query = _clean_query(payload.get("query"))
    candidate_limit = _limit(
        payload.get("candidate_limit"),
        "RETRIEVAL_CANDIDATE_LIMIT_MIN",
        "RETRIEVAL_CANDIDATE_LIMIT_MAX",
        "RETRIEVAL_CANDIDATE_LIMIT_DEFAULT",
    )
    top_k = _limit(
        payload.get("top_k"),
        "RETRIEVAL_TOP_K_MIN",
        "RETRIEVAL_TOP_K_MAX",
        "RETRIEVAL_TOP_K_DEFAULT",
    )
    if top_k > candidate_limit:
        raise api_error(
            "VALIDATION_ERROR",
            "top_k 不得超过 candidate_limit",
            {"fields": {"top_k": ["不得超过 candidate_limit"]}},
        )

    run = (
        AnalysisRun.objects.select_related("project")
        .filter(pk=run_id, project__owner=actor.user, project__deleted_at__isnull=True)
        .first()
    )
    if run is None:
        raise api_error("NOT_FOUND")

    project = Project.objects.filter(pk=run.project_id, owner=actor.user, deleted_at__isnull=True).first()
    if project is None:  # pragma: no cover - 与上面的过滤条件等价
        raise api_error("NOT_FOUND")

    canonical_path = f"/api/v1/runs/{run_id}/retrievals"
    config_snapshot = _build_config_snapshot()

    with transaction.atomic():
        locked = lock_project(project.pk, owner=actor.user)
        # 必须先于忙状态判断：同键重放不能被 PROJECT_BUSY 挡住
        from apps.common.idempotency import (
            ReplayResult,
            claim_or_replay,
            request_fingerprint,
        )

        claim = claim_or_replay(
            actor,
            "POST",
            canonical_path,
            uuid.UUID(str(key)),
            request_fingerprint(
                {
                    "kind": JOB_KIND_RETRIEVE,
                    "query": query,
                    "candidate_limit": candidate_limit,
                    "top_k": top_k,
                }
            ),
        )
        if isinstance(claim, ReplayResult):
            return AcceptedJob(
                uuid.UUID(claim.body["job_id"]),
                claim.body["resource_type"],
                uuid.UUID(claim.body["resource_id"]),
                replayed=True,
            )

        _require_searchable(run, locked)

        retrieval = RetrievalResult.objects.create(
            run=run,
            query=query,
            analysis_revision=run.analysis_revision,
            candidate_limit=candidate_limit,
            top_k=top_k,
            config_snapshot=config_snapshot,
            # 完成前必须为 null：前端据此区分"还没跑"与"跑完但无结果"
            expanded_queries=None,
            candidates=None,
            results=None,
        )

        return accept_job(
            actor,
            locked,
            JOB_KIND_RETRIEVE,
            {
                "resource_id": retrieval.id,
                "frozen_input": {
                    "run_id": str(run.id),
                    "retrieval_id": str(retrieval.id),
                    "query": query,
                    "candidate_limit": candidate_limit,
                    "top_k": top_k,
                    "config_snapshot": config_snapshot,
                },
            },
            idempotency=claim,
        )


def _require_searchable(run: AnalysisRun, project: Any) -> None:
    """新检索要求 Run ready 且非 stale。旧 Run 可读，但不能开新检索。"""
    if run.status != RUN_READY:
        raise api_error("RUN_NOT_READY", details={"status": run.status})
    if int(run.corpus_revision) != int(project.corpus_revision):
        raise api_error("STALE_RUN", details={"corpus_revision": run.corpus_revision})


def _clean_query(raw: Any) -> str:
    if not isinstance(raw, str):
        raise api_error("VALIDATION_ERROR", "query 必须是字符串")
    query = raw.strip()
    minimum = int(settings.MVP_LIMITS["RETRIEVAL_QUERY_MIN"])
    maximum = int(settings.MVP_LIMITS["RETRIEVAL_QUERY_MAX"])
    if not minimum <= len(query) <= maximum:
        raise api_error(
            "VALIDATION_ERROR",
            f"query 长度必须在 {minimum}—{maximum} 之间",
            {"fields": {"query": [f"长度必须在 {minimum}—{maximum} 之间"]}},
        )
    return query


def _limit(raw: Any, minimum_key: str, maximum_key: str, default_key: str) -> int:
    value = settings.MVP_LIMITS[default_key] if raw is None else raw
    if type(value) is not int:
        raise api_error("VALIDATION_ERROR", "检索参数必须是整数")
    minimum = int(settings.MVP_LIMITS[minimum_key])
    maximum = int(settings.MVP_LIMITS[maximum_key])
    if not minimum <= value <= maximum:
        raise api_error("VALIDATION_ERROR", f"检索参数必须在 {minimum}—{maximum} 之间")
    return value


def freeze_retrieval_config() -> dict[str, Any]:
    """供公开检索与综述受理共用的配置快照；调用方保存后显式透传。"""
    return _build_config_snapshot()


def _build_config_snapshot() -> dict[str, Any]:
    """冻结本次检索的索引 / 模板 / 模型配置。"""
    from apps.retrieval import index as index_module

    expand = load_prompt(
        EXPAND_PROMPT,
        {
            "research_question": "",
            "current_query": "",
            "domain_context": "",
            "max_queries": settings.RETRIEVAL_MAX_QUERIES,
        },
        task_type=TASK_EXPAND_QUERY,
    )
    rerank = load_prompt(
        RERANK_PROMPT,
        {"query": "", "candidates": "", "allowed_evidence_ids": "", "max_results": 1},
        task_type=TASK_RERANK,
    )
    return {
        "version": CONFIG_VERSION,
        "index": {
            "INDEX_CONFIG_VERSION": index_module.INDEX_CONFIG_VERSION,
            "max_features": int(settings.RETRIEVAL_INDEX_MAX_FEATURES),
            "min_df": int(settings.RETRIEVAL_INDEX_MIN_DF),
            "ngram_max": int(settings.RETRIEVAL_INDEX_NGRAM_MAX),
        },
        "expand_query": {
            "template_version": expand.version,
            "schema_version": SCHEMA_VERSION,
            "max_queries": int(settings.RETRIEVAL_MAX_QUERIES),
        },
        "rerank": {
            "template_version": rerank.version,
            "schema_version": SCHEMA_VERSION,
            "max_candidates": int(settings.RETRIEVAL_RERANK_MAX_CANDIDATES),
        },
        "model_config": {
            "model": settings.DEEPSEEK_MODEL,
            "thinking": settings.DEEPSEEK_THINKING_MODE,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            "temperature": 0,
        },
    }


# ── 执行 ──────────────────────────────────────────────────────
def execute_retrieval(context: ExecutionContext, retrieval_id: Any) -> RetrievalResult:
    """worker 入口：查询扩展 → 召回 → DeepSeek 重排 → 证据归属校验 → 发布。"""
    retrieval = (
        RetrievalResult.objects.select_related("run", "run__project")
        .filter(pk=retrieval_id, run__project_id=context.project_id)
        .first()
    )
    if retrieval is None:
        raise api_error("NOT_FOUND")

    snapshot = run_snapshot_for(retrieval.run)
    config = _frozen_config(retrieval)
    # 检索记录 ID 进入配置：逻辑调用 ID 由它派生，重试才能复用同一次逻辑调用
    config["retrieval_id"] = str(retrieval.pk)
    # ApiError 直接向上抛：由 jobs.tasks 统一收敛成 Job.error，
    # 这里不吞掉也不改码，避免出现两套并行的错误处理。
    ranked, expanded, candidates = retrieve_evidence(
        context,
        snapshot,
        retrieval.query,
        {"candidate_limit": retrieval.candidate_limit, "top_k": retrieval.top_k},
        config=config,
        track_progress=True,
    )

    record_progress(context, stage="publish", completed=3)
    _publish(context, retrieval, expanded_queries=expanded, candidates=candidates, results=ranked)
    return retrieval


def _publish(
    context: ExecutionContext,
    retrieval: RetrievalResult,
    *,
    expanded_queries: Sequence[str],
    candidates: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> None:
    """一次性发布扩展查询 / 候选 / 结果，并在**同一事务**提交 Job 终态。

    `jobs.tasks.execute_job` 只负责分发：领域服务不提交终态时它会把 Job 判成
    "执行函数未提交终态" 并标 failed。因此这里与解析 / 导入阶段一致，
    用 `complete_job` 的 `finalize_resource` 回调把产物与 Job.succeeded 放在一起，
    避免出现"结果已发布、Job 却是 failed"的自相矛盾状态。
    """
    from apps.jobs.services import complete_job

    frozen_expanded = list(expanded_queries)
    frozen_candidates = [dict(item) for item in candidates]
    frozen_results = [dict(item) for item in results]

    with transaction.atomic():
        _locked_job(context, retrieval.run_id)

        def finalize(job: Job) -> None:
            RetrievalResult.objects.filter(pk=retrieval.pk).update(
                expanded_queries=frozen_expanded,
                candidates=frozen_candidates,
                results=frozen_results,
                updated_at=_now(),
            )

        complete_job(
            context,
            {
                "retrieval_id": str(retrieval.pk),
                "run_id": str(retrieval.run_id),
                "result_count": len(frozen_results),
            },
            finalize_resource=finalize,
        )


def retrieve_evidence(
    context: ExecutionContext,
    run_snapshot: Any,
    query: str,
    limits: Any,
    *,
    config: Mapping[str, Any] | None = None,
    track_progress: bool = False,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """内部同步入口，供综述生成阶段复用；不创建公开 Job。

    返回 `(公开结果, 扩展查询, 内部候选)`。
    """
    config = dict(config or _build_config_snapshot())
    config.setdefault("run_id", str(run_snapshot.run_id))
    context.ensure_not_cancelled()

    if track_progress:
        record_progress(context, stage="expand_query", completed=0, total=4, unit="step")
    expanded = _expand_query(context, run_snapshot, query, config)
    if track_progress:
        record_progress(context, stage="recall", completed=1)
    # 优先用 index 阶段发布的 Run 专属产物；缺失或不一致时内存重建
    index_ref = _resolve_index(run_snapshot)
    candidate_limit = _limit_value(limits, "candidate_limit")
    top_k = _limit_value(limits, "top_k")

    recalled = recall(index_ref, expanded, {"candidate_limit": candidate_limit})
    if not recalled:
        # 成功但无证据是合法结果：results=[]，不是失败
        return [], expanded, []

    paper_by_document = _paper_by_document(run_snapshot.run_id)
    meta = {
        item["evidence_id"]: item
        for item in candidate_metadata(index_ref, [item.evidence_id for item in recalled])
    }
    candidates: list[dict[str, Any]] = []
    for item in recalled:
        info = meta.get(str(item.evidence_id))
        if info is None:  # pragma: no cover - 召回结果必来自索引
            continue
        candidates.append(
            {
                "evidence_id": str(item.evidence_id),
                "paper_id": paper_by_document.get(info["parsed_document_id"]),
                "recall_score": item.recall_score,
                "matched_queries": list(item.matched_queries),
                "page_number": info["page_number"],
                "section": info["section"],
                "element_type": info["element_type"],
                "text": info["text"],
            }
        )

    if track_progress:
        record_progress(context, stage="rerank", completed=2)
    ranked = _rerank(context, query, candidates, top_k, config)
    return ranked, expanded, candidates


def _expand_query(
    context: ExecutionContext,
    run_snapshot: Any,
    query: str,
    config: Mapping[str, Any],
) -> list[str]:
    """查询扩展；模型不可用时**保守退回原查询**，不让整次检索失败。"""
    settings_for_expand = config["expand_query"]
    maximum = int(settings_for_expand["max_queries"])
    parameters = {
        "research_question": str(getattr(run_snapshot, "research_question", "") or ""),
        "current_query": query,
        "domain_context": _domain_context(run_snapshot),
        "max_queries": maximum,
    }
    prompt = load_prompt(EXPAND_PROMPT, parameters, task_type=TASK_EXPAND_QUERY)
    _require_frozen_version(prompt.version, settings_for_expand["template_version"], "查询扩展")

    request = ModelTaskRequest(
        task_type=TASK_EXPAND_QUERY,
        template_version=prompt.version,
        schema_version=str(settings_for_expand["schema_version"]),
        model_config=dict(config["model_config"]),
        payload=parameters,
        logical_call_id=uuid.uuid5(
            _retrieval_seed({"run_id": str(run_snapshot.run_id), **config}), f"expand:{query}"
        ),
    )
    result = call_structured(context, request)
    context.ensure_not_cancelled()
    queries = _validated_queries(result.payload, original=query, maximum=maximum)
    return queries


def _validated_queries(payload: Mapping[str, Any], *, original: str, maximum: int) -> list[str]:
    """校验扩展查询：保留原查询、去重、限量；结构不合法则退回原查询。"""
    raw = payload.get("queries") if isinstance(payload, Mapping) else None
    if not isinstance(raw, list) or not raw:
        return [original]
    queries: list[str] = [original]
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in queries:
            continue
        queries.append(text)
        if len(queries) >= min(maximum, 5):
            break
    return queries


def _rerank(
    context: ExecutionContext,
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    top_k: int,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """DeepSeek 重排；结果必须落在候选集合内，越界即 INVALID_EVIDENCE。"""
    settings_for_rerank = config["rerank"]
    limit = min(int(settings_for_rerank["max_candidates"]), len(candidates))
    pool = list(candidates)[:limit]
    if not pool:
        return []

    parameters = {
        "query": query,
        "candidates": _json(pool),
        "allowed_evidence_ids": _json([str(item["evidence_id"]) for item in pool]),
        "max_results": max(1, min(top_k, 50)),
    }
    prompt = load_prompt(RERANK_PROMPT, parameters, task_type=TASK_RERANK)
    _require_frozen_version(prompt.version, settings_for_rerank["template_version"], "重排")

    request = ModelTaskRequest(
        task_type=TASK_RERANK,
        template_version=prompt.version,
        schema_version=str(settings_for_rerank["schema_version"]),
        model_config=dict(config["model_config"]),
        payload=parameters,
        allowed_evidence_ids=[str(item["evidence_id"]) for item in pool],
        # 同一检索记录的逻辑调用 ID 必须稳定：随机 ID 会让重试变成一次全新的
        # 付费调用，既重复计费，也让调用账本失去幂等性。
        logical_call_id=uuid.uuid5(_retrieval_seed(config), f"rerank:{query}"),
    )
    result = call_structured(context, request)
    context.ensure_not_cancelled()

    raw = result.payload.get("results") if isinstance(result.payload, Mapping) else None
    if not isinstance(raw, list):
        raise api_error("MODEL_OUTPUT_INVALID", "重排结果结构不合法")

    candidate_ids = {str(item["evidence_id"]) for item in pool}
    by_id = {str(item["evidence_id"]): item for item in pool}
    validated = validate_ranked_evidence(raw, candidate_ids)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in validated:
        evidence_id = str(item["evidence_id"])
        if evidence_id in seen:
            # 同一证据重复出现没有意义；保留首次（也就是更高相关的那次）
            continue
        score = item.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1:
            raise api_error("MODEL_OUTPUT_INVALID", "重排分数必须是 0—1 的数值")
        seen.add(evidence_id)
        results.append(
            {
                "evidence_id": evidence_id,
                "rank": len(results) + 1,
                "score": round(float(score), 6),
                "reason": str(item.get("reason") or ""),
                "paper_id": by_id[evidence_id].get("paper_id"),
                "page_number": by_id[evidence_id].get("page_number"),
            }
        )
        if len(results) >= min(top_k, 50):
            break
    return results


def validate_ranked_evidence(
    ranked: Sequence[Mapping[str, Any]],
    candidate_ids: set[Any],
) -> list[dict[str, Any]]:
    """拒绝候选集合之外的 evidence ID；重排不得新增证据。"""
    unknown = [
        item.get("evidence_id")
        for item in ranked
        if not isinstance(item, Mapping) or str(item.get("evidence_id")) not in candidate_ids
    ]
    if unknown:
        raise api_error(
            "INVALID_EVIDENCE",
            "重排结果包含候选之外的证据",
            {"evidence_ids": [str(value) for value in unknown[:10]]},
        )
    return [dict(item) for item in ranked]


# ── 共享工具 ──────────────────────────────────────────────────
def run_snapshot_for(run: AnalysisRun) -> RunSnapshot:
    """构造索引、检索与生成阶段共用的 Run 快照。

    公开导出：M09 的 index 阶段与 M10 的生成阶段都要用它，
    三处必须基于**同一份**快照定义，否则索引口径会漂移。
    """
    papers = list(run.run_papers.select_related("paper").order_by("created_at", "id"))
    return RunSnapshot(
        run_id=run.id,
        research_question=run.research_question,
        output_language=run.output_language,
        corpus_revision=run.corpus_revision,
        analysis_revision=run.analysis_revision,
        parsed_document_ids=[
            paper.parsed_document_id for paper in papers if paper.parsed_document_id is not None
        ],
        run_papers=papers,
        run=run,
    )


def _resolve_index(run_snapshot: Any) -> Any:
    """优先用已发布产物，缺失或不一致时内存重建。

    **必须透传该 Run 冻结的索引配置**：不传时 `load_published` / `build_index`
    会回退到当前部署默认值——部署改配置后，历史 Run 的索引会被判为不一致并
    按新参数重建，历史检索输出随部署漂移，而快照仍然声称是当时的参数。
    """
    config = _frozen_index_config(run_snapshot)
    run = getattr(run_snapshot, "run", None)
    if run is None:
        return build_index(run_snapshot, config=config)
    return load_published(run, run_snapshot, config=config) or build_index(run_snapshot, config=config)


def _frozen_index_config(run_snapshot: Any) -> dict[str, Any] | None:
    """从 Run 快照取冻结索引配置；没有快照时返回 None（由底层用当前默认值）。"""
    from apps.retrieval.index import effective_index_config

    run = getattr(run_snapshot, "run", None)
    snapshot = getattr(run, "config_snapshot", None) or {}
    stage = snapshot.get("index") if isinstance(snapshot, Mapping) else None
    if not isinstance(stage, Mapping):
        return None
    return effective_index_config(stage)


@dataclass(frozen=True)
class RunSnapshot:
    run_id: Any
    research_question: str
    output_language: str
    corpus_revision: int
    analysis_revision: int
    parsed_document_ids: list[Any]
    run_papers: list[Any]
    # 已发布的索引产物由 (run, 源摘要) 共同确定，因此快照必须带上 Run 本身
    run: Any = None


def _paper_by_document(run_id: Any) -> dict[str, str]:
    """parsed_document_id → paper_id 映射，用于给候选回填归属。"""
    result: dict[str, str] = {}
    for run_paper in RunPaper.objects.filter(run_id=run_id, parsed_document_id__isnull=False).only(
        "paper_id", "parsed_document_id"
    ):
        result[str(run_paper.parsed_document_id)] = str(run_paper.paper_id)
    return result


def _domain_context(run_snapshot: Any) -> str:
    """领域上下文：只用冻结的研究问题与会话可见的标题，不臆造术语体系。"""
    titles = [getattr(paper, "title", "") or "" for paper in getattr(run_snapshot, "run_papers", []) or []]
    titles = [title for title in titles if title]
    return "；".join(titles[:10])


def _frozen_config(retrieval: RetrievalResult) -> dict[str, Any]:
    snapshot = retrieval.config_snapshot or {}
    if not isinstance(snapshot, Mapping) or not snapshot.get("index"):
        raise api_error("INTERNAL_ERROR", "检索记录缺少冻结配置快照")
    return dict(snapshot)


def _require_frozen_version(current: str, frozen: Any, stage: str) -> None:
    """冻结模板版本与当前模板不一致时显式失败。

    用新模板生成结果却标成旧配置，会让历史检索无法解释。
    """
    if frozen != current:
        raise api_error(
            "INTERNAL_ERROR",
            f"冻结的{stage}提示词版本与当前模板不一致，请新建检索",
            {"frozen": frozen, "current": current},
        )


def _locked_job(context: ExecutionContext, run_id: Any) -> Job:
    """校验本次尝试仍有权写入；显式补检租约有效期。"""
    job = Job.objects.filter(pk=context.job_id, project_id=context.project_id).first()
    if job is None:
        raise api_error("INVALID_JOB_STATE", "执行上下文已失效")
    check_execution_lease(job, context.lease_token)
    if job.lease_expires_at is None or job.lease_expires_at <= _now():
        raise api_error("INVALID_JOB_STATE", "执行租约已过期")
    return job


def _retrieval_seed(config: Mapping[str, Any]) -> uuid.UUID:
    """逻辑调用 ID 的派生种子：优先用检索记录 ID，保证重试复用同一次逻辑调用。"""
    seed = config.get("retrieval_id")
    if seed is None:
        # 内部同步入口（综述生成阶段复用）没有独立的检索记录，退回 Run + 进程内随机量
        seed = config.get("run_id") or uuid.uuid4()
    try:
        return uuid.UUID(str(seed))
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, str(seed))


def _limit_value(limits: Any, name: str) -> int:
    if isinstance(limits, Mapping):
        return int(limits[name])
    return int(getattr(limits, name))


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _now() -> Any:
    from django.utils import timezone

    return timezone.now()


def claim_retrieval_execution(job_id: Any) -> ExecutionContext:
    """worker 领取执行资格；薄封装便于统一入口。"""
    return claim_execution(job_id)


# 供 M10 生成阶段复用的证据查询：只暴露属于本 Run 的片段
def evidence_rows_for_documents(document_ids: Sequence[Any]) -> list[EvidenceChunk]:
    return list(
        EvidenceChunk.objects.filter(parsed_document_id__in=list(document_ids)).order_by(
            "page_number", "ordinal", "id"
        )
    )
