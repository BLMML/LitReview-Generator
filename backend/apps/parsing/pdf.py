"""parsing.pdf：pypdf 文本读取、加密 / 页数检查、pypdfium2 受控页面渲染。

职责（docs/mvp/01 第 4.3 节）：
- `inspect_pdf(asset) → PdfInspection`
- `inspect_pdf_file(path) → PdfInspection`（上传阶段尚无 FileAsset 时使用）
- `extract_pages(asset) → PageTexts`
- `render_page(asset, page_number) → ImageAsset`

约定：所有业务页码为 1 起始；pypdfium2 渲染放在受限 worker 进程中，
不在 Web 请求线程中批量渲染。

**阶段状态**：PDF 结构检查（大小 / 页数 / 加密 / 损坏）随 M05「文献接入」实现；
M06 补齐文本提取与受控页面渲染，供 `parsing.services` 组织解析。

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
    """按页提取文本；标记空白 / 异常页供上层决定是否走图像辅助。

    只做**文本层**读取，不做任何语义判断，也不补造内容：
    提取为空的页会被标记 `is_empty=True`，由 `parsing.services` 决定用 DeepSeek 图像能力
    转录，或如实失败。提取成功不等于语义识别正确（`01` 第 4.3 节）。
    """
    import pypdf
    from pypdf.errors import PdfReadError

    path = _asset_path(asset)
    pages: list[PageText] = []
    try:
        with path.open("rb") as handle:
            reader = pypdf.PdfReader(handle, strict=False)
            if reader.is_encrypted:
                raise api_error("UNSUPPORTED_PDF", "不接受加密 PDF")
            for index, page in enumerate(reader.pages, start=1):
                try:
                    raw = page.extract_text() or ""
                except Exception:  # noqa: BLE001 - 单页提取失败只影响该页
                    # 单页损坏不能让整篇静默跳过：留空并标记为需图像辅助
                    raw = ""
                text = _normalize_text(raw)
                pages.append(PageText(page_number=index, text=text, is_empty=not text.strip()))
    except ApiError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise api_error("PDF_PARSE_FAILED", "PDF 已损坏或无法解析") from exc

    if not pages:
        raise api_error("PDF_PARSE_FAILED", "PDF 没有任何可解析的页面")
    return pages


def _normalize_text(raw: str) -> str:
    r"""归一化页文本：统一换行、去掉零宽字符与行尾空白，保留段落结构。

    不改写文字本身（不纠正错别字、不做全半角转换），因为证据必须能与原文核对。
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "").replace("\u200b", "").replace("\ufeff", "")
    lines = [line.rstrip() for line in text.split("\n")]
    # 折叠三个以上连续空行为一个空行，保留段落边界
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line:
            blank_run = 0
            collapsed.append(line)
            continue
        blank_run += 1
        if blank_run <= 1:
            collapsed.append("")
    return "\n".join(collapsed).strip()


def _asset_path(asset: Any) -> Path:
    from apps.common.storage import resolve_storage_key

    if isinstance(asset, (str, Path)):
        return Path(asset)
    if asset is None:
        raise api_error("NOT_FOUND", "文件不存在或不可读")
    return resolve_storage_key(asset.storage_key)


def render_page(
    asset: Any, page_number: int, *, scale: float | None = None, max_bytes: int | None = None
) -> bytes:
    """把单页渲染为 PNG 字节；`page_number` 为 1 起始。

    受控渲染：限制渲染倍率与输出体积，超过图像上限直接报错，
    不返回被缩放到看不清关键文字的图（`04` 第 6.2 节）。

    `pypdfium2` 是唯一渲染实现；本项目未安装 Pillow，因此直接编码 PNG，
    不经过 PIL，避免为渲染引入新的图像库依赖。
    """
    import pypdfium2

    if not isinstance(page_number, int) or page_number < 1:
        raise api_error("VALIDATION_ERROR", "page_number 必须是 1 起始的整数")

    path = _asset_path(asset)
    scale = float(settings.PAGE_RENDER_SCALE if scale is None else scale)
    max_bytes = int(settings.PAGE_IMAGE_MAX_BYTES if max_bytes is None else max_bytes)

    document = None
    try:
        document = pypdfium2.PdfDocument(str(path))
        if page_number > len(document):
            raise api_error(
                "VALIDATION_ERROR",
                "page_number 超出该 PDF 真实页数",
                {"page_number": [f"必须在 1—{len(document)} 之间"]},
            )
        bitmap = document[page_number - 1].render(scale=scale)
        payload = _encode_png(bitmap)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 渲染失败必须显式失败，不补造内容
        raise api_error("PDF_PARSE_FAILED", f"第 {page_number} 页渲染失败") from exc
    finally:
        if document is not None:
            document.close()

    if len(payload) > max_bytes:
        raise api_error(
            "PDF_PARSE_FAILED",
            "渲染图像超过单页体积上限",
            {"page_number": page_number, "limit_bytes": max_bytes},
        )
    return payload


def _encode_png(bitmap: Any) -> bytes:
    """把 pypdfium2 位图编码为 PNG；不依赖 Pillow。

    位图格式为 3 通道 BGR（`n_channels=3`），按 stride 逐行取像素后转成 RGB。
    """
    import struct
    import zlib

    width = int(bitmap.width)
    height = int(bitmap.height)
    stride = int(bitmap.stride)
    channels = int(getattr(bitmap, "n_channels", 3))
    if channels not in (3, 4):
        raise api_error("PDF_PARSE_FAILED", "渲染位图通道数不支持")

    raw = bytes(bitmap.buffer)
    rows = bytearray()
    for y in range(height):
        start = y * stride
        line = raw[start : start + width * channels]
        rows.append(0)  # PNG 过滤器类型 0（None）
        for x in range(width):
            offset = x * channels
            b = line[offset]
            g = line[offset + 1]
            r = line[offset + 2]
            rows += bytes((r, g, b))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
