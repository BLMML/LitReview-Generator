"""reviews.schemas：五类固定章节与 claim / transition 结构校验。

职责（docs/mvp/01 第 4.6 节、docs/mvp/03 第 2.5 节）：
- `validate_sections(run_scope, sections) → ValidatedSections`；
- 结构校验是保存和生成提交的共同门槛。

硬性限制：
- 固定五节顺序与 section_key；
- 每节至少一个 block；全稿 ≤100 block；每块 1—600 字符、≤20 引用；
- block_id 全文唯一；kind ∈ claim/transition；
- claim 至少一个当前 Run 证据；禁止 HTML、重复块 ID、非法引用；
- 正文不接受模型给出的文献编号。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from apps.reviews.models import SECTION_KEYS

NOT_IMPLEMENTED_MESSAGE = "reviews.schemas 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 D）"


class SectionsValidationError(ValueError):
    """正文结构不合法；映射为 422 STRUCTURAL_VALIDATION_FAILED。"""


def validate_sections(run_scope: Any, sections: Sequence[Any]) -> list[dict[str, Any]]:
    """校验并规范化五节正文。"""
    raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)


def normalize_sections(sections: Sequence[Any]) -> str:
    """生成 content_hash 用的规范化表示；顺序与空白必须稳定。"""
    raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)


__all__ = [
    "SECTION_KEYS",
    "SectionsValidationError",
    "normalize_sections",
    "validate_sections",
]
