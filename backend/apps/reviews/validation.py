"""reviews.validation：结构检查、证据有效性检查、语义支持核验与问题汇总。

职责（docs/mvp/01 第 4.6 节）：
- `validate_structure(revision) → StructuralFindings`
- `validate_claim_support(context, revision, evidence) → ClaimFindings`
- `build_report(...) → ValidationReport`

判定规则：任一结构错误或 unsupported/uncertain 均为 needs_review；
结构错误禁止导出；要区分“不支持”“证据不足”与“执行失败”。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "reviews.validation 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 D）"


@dataclass(frozen=True)
class StructuralFindings:
    passed: bool
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ClaimFindings:
    blocks: list[dict[str, Any]] = field(default_factory=list)
    result: str = "needs_review"


def validate_structure(revision: Any) -> StructuralFindings:
    """纯结构检查：五节、块数、字符数、引用上限、block_id 唯一、禁止 HTML。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def validate_claim_support(
    context: Any,
    revision: Any,
    evidence: Sequence[Any],
) -> ClaimFindings:
    """用 DeepSeek 逐 block 核验语义支持；引文只用提供的 evidence ID。

    本函数不重新润色正文，只产出支持 / 不支持 / 不确定的核验发现。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def build_report(
    revision: Any,
    structural: StructuralFindings,
    claims: ClaimFindings,
) -> dict[str, Any]:
    """汇总为 ValidationReport 的三字段（result / structure_passed / blocks）。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
