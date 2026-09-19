"""parsing.pdf：pypdf 文本读取、加密 / 页数检查、pypdfium2 受控页面渲染。

职责（docs/mvp/01 第 4.3 节）：
- `inspect_pdf(asset) → PdfInspection`
- `extract_pages(asset) → PageTexts`
- `render_page(asset, page_number) → ImageAsset`

约定：所有业务页码为 1 起始；pypdfium2 渲染放在受限 worker 进程中，
不在 Web 请求线程中批量渲染。

**骨架状态：未实现。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "parsing.pdf 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"


@dataclass(frozen=True)
class PdfInspection:
    """PDF 基础检查结果；不通过时由调用方映射为 413 / 422。"""

    page_count: int
    is_encrypted: bool
    # 需要图像辅助的页（空白页、异常提取结果）：1 起始页码
    pages_needing_ocr: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str
    is_empty: bool


def inspect_pdf(asset: Any) -> PdfInspection:
    """体积 / 页数 / 加密 / 损坏检查。

    失败映射：超过 25 MiB → 413 FILE_TOO_LARGE；超过 50 页 → 422 PAGE_LIMIT_EXCEEDED；
    加密或损坏 → 422 UNSUPPORTED_PDF。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def extract_pages(asset: Any) -> list[PageText]:
    """按页提取文本；检查空白与异常结果，定位需要图像辅助的页。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def render_page(asset: Any, page_number: int) -> Any:
    """渲染单页为图像资产；page_number 为 1 起始。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
