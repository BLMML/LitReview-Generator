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

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from jsonschema import Draft202012Validator, FormatChecker

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
    try:
        choices = raw["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if choice["finish_reason"] != "stop":
            raise ValueError
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError
        payload = json.loads(content, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
        if not isinstance(payload, dict):
            raise ValueError
        provider_id = raw.get("id")
        if provider_id is not None and (not isinstance(provider_id, str) or len(provider_id) > 191):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise api_error("MODEL_OUTPUT_INVALID", "模型响应为空、截断或格式不合法") from exc
    return ModelTaskResult(payload, provider_id, normalize_usage(raw.get("usage")), "stop", "")


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def normalize_usage(raw: Any) -> ModelUsage:
    """只保留已知计数字段，不把供应商任意文本写入账本。"""
    if not isinstance(raw, Mapping):
        return ModelUsage()

    def count(value: Any) -> int | None:
        return value if type(value) is int and 0 <= value <= 10**12 else None

    incoming = count(raw.get("prompt_tokens"))
    outgoing = count(raw.get("completion_tokens"))
    hit = count(raw.get("prompt_cache_hit_tokens"))
    details = raw.get("prompt_tokens_details")
    if hit is None and isinstance(details, Mapping):
        hit = count(details.get("cached_tokens"))
    miss = count(raw.get("prompt_cache_miss_tokens"))
    if incoming is not None:
        if hit is not None and miss is None and hit <= incoming:
            miss = incoming - hit
        elif miss is not None and hit is None and miss <= incoming:
            hit = incoming - miss
        if hit is not None and miss is not None and hit + miss != incoming:
            hit = miss = None
    elif hit is not None and miss is not None:
        incoming = hit + miss
    safe = {
        "prompt_tokens": incoming,
        "completion_tokens": outgoing,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }
    return ModelUsage(incoming, outgoing, hit, miss, safe)


TASK_DEFINITIONS = dict(
    zip(
        TASK_TYPES,
        (
            "PageExtract",
            "PaperExtract",
            "Topics",
            "Disputes",
            "QueryExpand",
            "Rerank",
            "ReviewSection",
            "VerifyBlocks",
        ),
        strict=True,
    )
)


def output_schema(task_type: str) -> dict[str, Any]:
    if task_type not in TASK_DEFINITIONS:
        raise api_error("VALIDATION_ERROR", "未知模型任务类型")
    path = settings.REPO_ROOT / "docs/mvp/contracts/model_outputs.schema.json"
    if not path.is_file():
        path = settings.BASE_DIR / "contracts/model_outputs.schema.json"
    root = json.loads(path.read_text(encoding="utf-8"))
    return {"$defs": root["$defs"], "$ref": f"#/$defs/{TASK_DEFINITIONS[task_type]}"}


def validate_payload(request: ModelTaskRequest, payload: Mapping[str, Any]) -> None:
    """任务专属 Schema 与请求白名单；事实支持仍由领域模块核验。"""
    validator = Draft202012Validator(output_schema(request.task_type), format_checker=FormatChecker())
    if not validator.is_valid(payload):
        raise api_error("MODEL_OUTPUT_INVALID", "模型输出不符合任务 Schema")
    evidence = {str(i) for i in request.allowed_evidence_ids}
    papers = {str(i) for i in request.allowed_paper_ids}

    def visit(node: Any) -> None:
        if isinstance(node, float) and not math.isfinite(node):
            raise api_error("MODEL_OUTPUT_INVALID")
        if isinstance(node, list):
            for item in node:
                visit(item)
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            allowed = None
            if key in {"evidence_id", "evidence_ids"}:
                allowed = evidence
            elif key in {"paper_ids", "unassigned_paper_ids", "left_paper_id", "right_paper_id"}:
                allowed = papers
            if allowed is not None:
                ids = value if isinstance(value, list) else [value]
                if any(str(i) not in allowed for i in ids) or len(set(ids)) != len(ids):
                    raise api_error("MODEL_OUTPUT_INVALID", "引用不在请求白名单内或存在重复引用")
            visit(value)
        if "value" in node and node["value"] is not None and not node.get("evidence_ids"):
            raise api_error("MODEL_OUTPUT_INVALID", "事实要素缺少证据")
        if "left_paper_id" in node and node["left_paper_id"] == node["right_paper_id"]:
            raise api_error("MODEL_OUTPUT_INVALID", "争议两侧必须是不同论文")

    visit(payload)
    if request.task_type == TASK_RERANK:
        ids = [r["evidence_id"] for r in payload["results"]]
        if len(ids) != len(set(ids)):
            raise api_error("MODEL_OUTPUT_INVALID", "重排结果重复")
    if request.task_type == TASK_VERIFY_BLOCKS:
        expected = {str(b["block_id"]) for b in request.payload.get("blocks_with_evidence", [])}
        actual = [b["block_id"] for b in payload["blocks"]]
        if set(actual) != expected or len(actual) != len(set(actual)):
            raise api_error("MODEL_OUTPUT_INVALID", "核验结果未完整覆盖输入块")


def provider_error_code(reason: str) -> str:
    """把供应商错误原因映射为后台错误码；未知原因保守归为 INTERNAL_ERROR。"""
    return PROVIDER_ERROR_CODES.get(reason, "INTERNAL_ERROR")
