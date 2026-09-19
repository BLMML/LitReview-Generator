"""llm.schemas：ModelTaskRequest / ModelTaskResult 与供应商用量、错误结构。

职责（docs/mvp/01 第 4.8 节、docs/mvp/04 第 2 节）：
- 定义请求与结果结构，把协议结果转换为统一内部形式；
- 请求含 task_type、template_version、schema_version、model_config、payload、
  allowed_evidence_ids、allowed_paper_ids；
- 结果含 payload、provider_request_id、usage、finish_status、call_id；
- `normalize_response(raw) → ModelTaskResult`。

领域引用与归属由调用模块再验证：本模块不理解综述业务状态。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from apps.common.http import api_error

# 与 docs/mvp/01 第 5 节、contracts/model_outputs.schema.json 的任务名一致
TASK_PAGE_EXTRACT = "page_extract"
TASK_PAPER_EXTRACT = "paper_extract"
TASK_ASSIGN_TOPICS = "topics"
TASK_COMPARE_DISPUTES = "disputes"
TASK_EXPAND_QUERY = "query_expand"
TASK_RERANK = "rerank"
TASK_REVIEW_SECTION = "review_section"
TASK_VERIFY_BLOCKS = "verify_blocks"

TASK_TYPES = (
    TASK_PAGE_EXTRACT,
    TASK_PAPER_EXTRACT,
    TASK_ASSIGN_TOPICS,
    TASK_COMPARE_DISPUTES,
    TASK_EXPAND_QUERY,
    TASK_RERANK,
    TASK_REVIEW_SECTION,
    TASK_VERIFY_BLOCKS,
)

# task_type → 提示词文件名（backend/prompts/）
TASK_PROMPT_FILE = {
    TASK_PAGE_EXTRACT: "extract_paper.md",
    TASK_PAPER_EXTRACT: "extract_paper.md",
    TASK_ASSIGN_TOPICS: "assign_topics.md",
    TASK_COMPARE_DISPUTES: "compare_disputes.md",
    TASK_EXPAND_QUERY: "expand_query.md",
    TASK_RERANK: "rerank_evidence.md",
    TASK_REVIEW_SECTION: "generate_sections.md",
    TASK_VERIFY_BLOCKS: "validate_claims.md",
}

FINISH_STATUS_STOP = "stop"
FINISH_STATUS_TRUNCATED = "length"
FINISH_STATUS_FAILED = "failed"

# 供应商错误 → 后台 Job 错误码（docs/mvp/03 第 3 节）
PROVIDER_ERROR_CODES = {
    "timeout": "MODEL_TIMEOUT",
    "rate_limited": "MODEL_RATE_LIMITED",
    "auth_failed": "MODEL_AUTH_FAILED",
    "balance_insufficient": "MODEL_BALANCE_INSUFFICIENT",
    "output_invalid": "MODEL_OUTPUT_INVALID",
}

NOT_IMPLEMENTED_MESSAGE = "llm.schemas.normalize_response 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"


@dataclass(frozen=True)
class ModelUsage:
    """供应商用量。未知计数为 None，绝不写 0 伪装成已知。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelTaskRequest:
    """一次结构化模型调用的输入。原文是**不可信材料**，不得覆盖系统规则。"""

    task_type: str
    template_version: str
    schema_version: str
    model_config: Mapping[str, Any]
    payload: Mapping[str, Any]
    allowed_evidence_ids: Sequence[Any] = ()
    allowed_paper_ids: Sequence[Any] = ()
    logical_call_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ModelTaskResult:
    """一次结构化模型调用的结果。"""

    payload: Mapping[str, Any]
    provider_request_id: str | None
    usage: ModelUsage
    finish_status: str
    call_id: str
    # 未结算费用保留 null 与预留，不当作零成本
    estimated_cost_usd: Decimal | None = None


def normalize_response(raw: Mapping[str, Any]) -> ModelTaskResult:
    """把供应商原始响应转换为统一内部形式。

    结构正确只表示格式符合要求；事实支持仍需证据核验和人工评审。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def provider_error_code(reason: str) -> str:
    """把供应商错误原因映射为后台错误码；未知原因保守归为 INTERNAL_ERROR。"""
    return PROVIDER_ERROR_CODES.get(reason, "INTERNAL_ERROR")
