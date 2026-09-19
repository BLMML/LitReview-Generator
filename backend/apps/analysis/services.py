"""analysis.services：Run 生命周期。

职责（docs/mvp/01 第 4.4 节）：
- `create_run(actor, project_id, payload, key) → AcceptedJob`
- `execute_run(context, run_id) → AnalysisRun`
- `review_dispute(actor, dispute_id, expected_version, changes) → Dispute`
- `get_run_snapshot(actor_or_context, run_id)`

阶段顺序（docs/mvp/04 第 3 节）：
snapshot → parse → extract → index → topics → disputes → finalize。
任一 RunPaper 失败则 Run 失败，成功阶段保留；不得只把成功论文汇总后标为 ready。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "analysis.services 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"


def create_run(actor: Any, project_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理分析并冻结 RunPaper。

    payload 为已校验的空对象；要求当前至少 2 篇纳入论文，否则 422 INSUFFICIENT_PAPERS。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def execute_run(context: Any, run_id: Any) -> Any:
    """worker 入口：按固定阶段推进，Run.ready 与 Job.succeeded 在同一事务提交。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def review_dispute(
    actor: Any,
    dispute_id: Any,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Any:
    """人工确认 / 拒绝争议。

    只允许修改 review_status / review_note，不能覆盖自动 classification。
    成功修改递增 Dispute.version 与 Run.analysis_revision；
    rejected 必须带非空说明。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def get_run_snapshot(actor_or_context: Any, run_id: Any) -> Any:
    """读取冻结快照：Run 不读取后来修改的 Paper 元数据替换快照。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
