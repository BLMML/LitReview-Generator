"""retrieval.services：外部受理检索任务与内部同步检索入口。

职责（docs/mvp/01 第 4.5 节）：
- `create_retrieval(actor, run_id, payload, key) → AcceptedJob`
- `execute_retrieval(context, retrieval_id) → RetrievalResult`
- `retrieve_evidence(context, run_snapshot, query, limits) → RankedEvidence[]`

关键约束：`retrieve_evidence` 是内部同步阶段函数，在当前 worker 中执行，
使用同一个父 Job 的取消、预算和调用记录。
**不能为每个章节再创建一个公开 retrieve Job**，否则会与“每项目一个活跃 Job”冲突。

从模型重排结果读取的 evidence ID 必须属于候选集合；score 不是事实正确率。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "retrieval.services 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"


def create_retrieval(actor: Any, run_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理检索；要求 Run ready 且非 stale，否则 409 RUN_NOT_READY / STALE_RUN。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def execute_retrieval(context: Any, retrieval_id: Any) -> Any:
    """worker 入口：查询扩展 → 召回 → DeepSeek 重排 → 证据归属校验 → 发布。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def retrieve_evidence(
    context: Any,
    run_snapshot: Any,
    query: str,
    limits: Any,
) -> list[Any]:
    """内部同步入口，供综述生成阶段复用；不创建公开 Job。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def validate_ranked_evidence(
    ranked: Sequence[Mapping[str, Any]],
    candidate_ids: set[Any],
) -> list[dict[str, Any]]:
    """拒绝候选集合之外的 evidence ID；重排不得新增证据。"""
    for item in ranked:
        if item.get("evidence_id") not in candidate_ids:
            raise api_error("INVALID_EVIDENCE", "重排结果包含候选之外的证据")
    return [dict(item) for item in ranked]
