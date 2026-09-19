"""parsing.services：文本优先、复杂页 DeepSeek 辅助的解析编排。

职责（docs/mvp/01 第 4.3 节）：
- `parse_paper(context, paper_snapshot, parser_config) → ParsedDocument`
- 按冻结配置复用可验证结果；解析失败明确反馈；
- 模型辅助结果仍需结构校验，无法识别不能补造内容。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "parsing.services 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"


def parse_paper(
    context: Any,
    paper_snapshot: Any,
    parser_config: Mapping[str, Any],
) -> Any:
    """执行完整解析并发布 ParsedDocument + Chunks。

    只有全部页完成才发布；失败页面保留诊断，不能未经告知忽略缺页。
    复用条件：项目 + source_sha256 + parser_revision + config_hash 全部一致。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
