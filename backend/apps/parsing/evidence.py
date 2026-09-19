"""parsing.evidence：可追溯片段的划分与读取。

职责（docs/mvp/01 第 4.3 节）：
- `build_chunks(parsed_document, elements) → EvidenceChunk[]`
- `get_evidence_batch(run_scope, evidence_ids) → EvidenceDTO[]`，拒绝跨 Run 引用。

证据规则：
- text_matched 要求与该页原文匹配；
- 视觉转录初始为 image_unverified；
- human_verified 是保留枚举，MVP 不自动设置。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "parsing.evidence 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"


def build_chunks(parsed_document: Any, elements: Iterable[Any]) -> list[Any]:
    """按页和元素划分片段；ordinal 从 1 起，page_number 为真实 1 起始页码。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def get_evidence_batch(run_scope: Any, evidence_ids: Sequence[Any]) -> list[Any]:
    """按解析结果批量读取证据；跨 Run 引用一律拒绝。

    EvidenceDTO 的 run_id/paper_id 通过请求 Run 与 RunPaper→ParsedDocument 关系查得。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def list_evidence_for_run_paper(run_paper: Any, *, page_number: int | None = None) -> Any:
    """只读查询：某 RunPaper 的片段，按 page_number / ordinal / id 升序。

    允许旧 Run 读取已完整发布的解析产物；未解析成功时由调用方返回 RESULT_NOT_READY。
    返回 queryset，由 API 层统一分页与序列化。
    """
    from apps.parsing.models import EvidenceChunk

    queryset = EvidenceChunk.objects.filter(parsed_document_id=run_paper.parsed_document_id)
    if page_number is not None:
        queryset = queryset.filter(page_number=page_number)
    return queryset.order_by("page_number", "ordinal", "id")
