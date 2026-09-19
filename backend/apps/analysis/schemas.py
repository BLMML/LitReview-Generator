"""analysis.schemas：模型输出的内部结构校验。

职责（docs/mvp/01 第 4.4 节）：
- `validate_paper_analysis`、`validate_topics`、`validate_disputes`；
- 校验未知字段、证据范围与缺失值；输出验证后的结构。

EvidenceText 统一为 `{value: string|null, evidence_ids: UUID[]}`：
缺失时 value=null、evidence_ids=[]；有事实 value 时至少一个合法证据。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 单篇分析 payload 的固定字段
EVIDENCE_TEXT_SINGLE_FIELDS = ("research_question", "research_object", "summary")
EVIDENCE_TEXT_LIST_FIELDS = ("methods", "datasets", "metrics", "conclusions", "limitations")
PAPER_ANALYSIS_FIELDS = EVIDENCE_TEXT_SINGLE_FIELDS + EVIDENCE_TEXT_LIST_FIELDS

DISPUTE_CONDITION_FIELDS = (
    "research_question",
    "left_conditions",
    "right_conditions",
    "comparability_reason",
)

NOT_IMPLEMENTED_MESSAGE = "analysis.schemas 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"


class SchemaValidationError(ValueError):
    """结构不合法；由调用方映射为 Job.error 的 MODEL_OUTPUT_INVALID。"""


def validate_paper_analysis(payload: Mapping[str, Any], *, run_scope: Any) -> dict[str, Any]:
    """校验单篇要素与摘要：未知字段、缺失值形态、证据属于同一 RunPaper。"""
    raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)


def validate_topics(topics: Sequence[Mapping[str, Any]], *, run_scope: Any) -> list[dict[str, Any]]:
    """校验主题：paper_ids 属于本 Run 的 Paper.id，成员至少 1 篇，允许一文多主题。"""
    raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)


def validate_disputes(
    disputes: Sequence[Mapping[str, Any]], *, run_scope: Any
) -> list[dict[str, Any]]:
    """校验争议：左右论文不同且都在 Run 内，两侧证据分别属于对应论文。"""
    raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)


__all__ = [
    "DISPUTE_CONDITION_FIELDS",
    "EVIDENCE_TEXT_LIST_FIELDS",
    "EVIDENCE_TEXT_SINGLE_FIELDS",
    "PAPER_ANALYSIS_FIELDS",
    "SchemaValidationError",
    "validate_disputes",
    "validate_paper_analysis",
    "validate_topics",
]
