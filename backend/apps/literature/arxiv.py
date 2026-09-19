"""literature.arxiv：arXiv ID 校验与真实元数据 / PDF 获取。

职责（docs/mvp/01 第 4.2 节）：
- 验证 1—10 个 arXiv ID；
- 向预定 arXiv 接口获取真实元数据和 PDF；
- 限制重定向、时间、下载大小和请求频率。

安全约束：**不接受用户提供的任意 URL**；数据源地址由服务端白名单构造。

**骨架状态：未实现。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apps.common.http import api_error

# 新旧合法形式与版本后缀；不接受 URL
ARXIV_ID_PATTERN = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$")

NOT_IMPLEMENTED_MESSAGE = "literature.arxiv 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"

# 服务端白名单：导出 / 元数据接口只允许这些主机
ARXIV_ALLOWED_HOSTS = ("arxiv.org", "export.arxiv.org")


@dataclass(frozen=True)
class SourceMetadata:
    """arXiv 返回的真实元数据；模型不得覆盖已保存元数据。"""

    source_id: str
    title: str
    authors: list[str]
    year: int | None
    source_url: str


def validate_arxiv_ids(raw_ids: Any) -> list[str]:
    """校验 1—10 个互不相同的合法 arXiv ID；不接受 URL。"""
    from django.conf import settings

    limits = settings.MVP_LIMITS
    if not isinstance(raw_ids, (list, tuple)):
        raise api_error("VALIDATION_ERROR", "arxiv_ids 必须是数组")
    if not limits["ARXIV_IDS_MIN"] <= len(raw_ids) <= limits["ARXIV_IDS_MAX"]:
        raise api_error(
            "VALIDATION_ERROR",
            f"arxiv_ids 数量必须在 {limits['ARXIV_IDS_MIN']}—{limits['ARXIV_IDS_MAX']} 之间",
        )
    normalized: list[str] = []
    for item in raw_ids:
        value = str(item).strip()
        if not ARXIV_ID_PATTERN.match(value):
            raise api_error("VALIDATION_ERROR", f"不是合法的 arXiv ID：{value!r}")
        if value in normalized:
            raise api_error("VALIDATION_ERROR", f"arxiv_ids 存在重复项：{value!r}")
        normalized.append(value)
    return normalized


def fetch_metadata(arxiv_id: str) -> SourceMetadata:
    """获取真实元数据。无版本 ID 需先解析并冻结具体版本。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def download_pdf(source_record: SourceMetadata) -> Any:
    """按受控重定向与大小上限下载 PDF，返回 StagedFile。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
