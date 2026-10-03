"""parsing.pdf：pypdf 文本读取、加密 / 页数检查、pypdfium2 受控页面渲染。

职责（docs/mvp/01 第 4.3 节）：
- `inspect_pdf(asset) → PdfInspection`
- `inspect_pdf_file(path) → PdfInspection`（上传阶段尚无 FileAsset 时使用）
- `extract_pages(asset) → PageTexts`
- `render_page(asset, page_number) → ImageAsset`

约定：所有业务页码为 1 起始；pypdfium2 渲染放在受限 worker 进程中，
不在 Web 请求线程中批量渲染。

**阶段状态**：PDF 结构检查（大小 / 页数 / 加密 / 损坏）已随 M05「文献接入」实现，
供 `literature.services.upload_pdf` 在上传时校验；文本提取与页面渲染属于 M06 解析阶段，
仍为未实现。

这也是 `01` 第 4.2 节「上传时先暂存并检查格式、体积、页数、加密和哈希」要求的落点：
PDF 解析只在 parsing 模块里做，literature 不另建一套 PDF 读取实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings

from apps.common.http import ApiError, api_error
from apps.common.storage import resolve_storage_key

NOT_IMPLEMENTED_MESSAGE = "parsing.pdf 的文本提取与页面渲染尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"


@dataclass(frozen=True)
class PdfInspection:
    """PDF 基础检查结果；不通过时由调用方映射为 413 / 422。

    通过检查只表示「文件结构可读、未加密、页数在 MVP 上限内」，
    不代表正文能被正确提取，更不代表语义识别正确（docs/mvp/01 第 4.3 节）。
    """

    page_count: int
    is_encrypted: bool
    # 需要图像辅助的页（空白页、异常提取结果）：1 起始页码
    pages_needing_ocr: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str
    is_empty: bool


def _limits() -> dict[str, Any]:
    return settings.MVP_LIMITS


def inspect_pdf_file(path: Any) -> PdfInspection:
    """对已落盘的文件做体积 / 页数 / 加密 / 损坏检查。

    失败映射（docs/mvp/03 第 3 节）：
    - 超过 25 MiB → 413 FILE_TOO_LARGE（正常由暂存阶段拦截，这里是兜底）；
    - 超过 50 页 → 422 PAGE_LIMIT_EXCEEDED；
    - 加密、损坏或不是 PDF → 422 UNSUPPORTED_PDF。

    只读取文件结构，不执行文档内容，也不信任扩展名。
    """
    import pypdf
    from pypdf.errors import PdfReadError

    limits = _limits()
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise api_error("UNSUPPORTED_PDF", "上传文件不可读") from exc

    if size <= 0:
        raise api_error("VALIDATION_ERROR", "上传内容为空")
    if size > limits["PDF_MAX_BYTES"]:
        raise api_error("FILE_TOO_LARGE", details={"limit_bytes": limits["PDF_MAX_BYTES"]})

    try:
        with candidate.open("rb") as handle:
            # strict=False：真实世界里大量合法 PDF 带有轻微结构偏差，
            # 沿用 pypdf 默认的宽松模式，只在确实读不出页对象时才判定损坏。
            reader = pypdf.PdfReader(handle, strict=False)
            if reader.is_encrypted:
                # 不接受加密 PDF：即使空密码可解密也不进入 MVP 流程
                raise api_error("UNSUPPORTED_PDF", "不接受加密 PDF")
            page_count = len(reader.pages)
    except ApiError:
        # 已带业务码的错误原样向上抛，不在这里改写成其它原因
        raise
    except (PdfReadError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # 损坏、被截断、伪装成 .pdf 的其他格式：统一 422，不回显解析器内部细节
        raise api_error("UNSUPPORTED_PDF", "PDF 已损坏或无法解析") from exc

    if page_count < 1:
        raise api_error("UNSUPPORTED_PDF", "PDF 没有任何页面")
    if page_count > limits["PDF_MAX_PAGES"]:
        raise api_error(
            "PAGE_LIMIT_EXCEEDED", details={"page_count": page_count, "limit_pages": limits["PDF_MAX_PAGES"]}
        )

    # 空白页与提取质量属于 M06 的解析职责，这里不做图像辅助判断
    return PdfInspection(page_count=page_count, is_encrypted=False, pages_needing_ocr=[])


def inspect_pdf(asset: Any) -> PdfInspection:
    """已登记 FileAsset 的结构检查；与上传阶段共用同一套判定。"""
    if asset is None:
        raise api_error("UNSUPPORTED_PDF")
    return inspect_pdf_file(resolve_storage_key(asset.storage_key))


def extract_pages(asset: Any) -> list[PageText]:
    """按页提取文本；检查空白与异常结果，定位需要图像辅助的页。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def render_page(asset: Any, page_number: int) -> Any:
    """渲染单页为图像资产；page_number 为 1 起始。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
