"""reviews.exporter：python-docx 固定样式与证据附表。

职责（docs/mvp/01 第 4.6 节）：
- `render_docx(revision_snapshot, validation_report, bibliography, evidence) → StagedFile`
- 首次引用排序生成文献编号；冻结元数据生成参考文献；
- 证据附表、草稿标记与核验警示。

约束：**本文件不自行调用 DeepSeek**；Word 不重新检索、不重新生成正文，
始终导出用户指定的不可变 revision。所有导出都标明“AI 辅助生成草稿，需人工审阅”。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "reviews.exporter 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 D）"

# 固定模板版本；改变模板必须同步升级该值并让旧导出保持可追溯
TEMPLATE_REVISION = "docx-template-v1"

DRAFT_BANNER = "AI 辅助生成草稿，需人工审阅"


def render_docx(
    revision_snapshot: Any,
    validation_report: Any,
    bibliography: Sequence[Any],
    evidence: Sequence[Any],
) -> Any:
    """生成 DOCX 暂存文件；正文、核验与警示列表在此刻冻结。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def build_bibliography(run_snapshot: Any) -> list[dict[str, Any]]:
    """按首次引用顺序编号；元数据只来自冻结的 RunPaper，不抓取任何外部指标。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
