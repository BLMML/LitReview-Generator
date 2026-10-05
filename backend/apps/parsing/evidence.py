"""parsing.evidence：可追溯片段的划分与读取。

职责（docs/mvp/01 第 4.3 节）：
- `build_chunks(parsed_document, elements) → EvidenceChunk[]`
- `get_evidence_batch(run_scope, evidence_ids) → EvidenceDTO[]`，拒绝跨 Run 引用。

证据规则：
- text_matched 要求与该页原文匹配；
- 视觉转录初始为 image_unverified；
- human_verified 是保留枚举，MVP 不自动设置。

切片策略（`04` 第 6.2 节）：保留真实页码与相邻上下文，每片 600—1,200 字符。
**片段不跨页**——跨页合并会迫使 locator 伪造单页坐标，而 MVP 只承诺页级定位。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.common.http import api_error
from apps.common.models import FileAsset
from apps.common.storage import resolve_storage_key
from apps.parsing.models import (
    ELEMENT_FIGURE_CAPTION,
    ELEMENT_FORMULA,
    ELEMENT_TABLE,
    ELEMENT_TEXT,
    ELEMENT_TYPES,
    VERIFICATION_IMAGE_UNVERIFIED,
    VERIFICATION_TEXT_MATCHED,
    EvidenceChunk,
    ParsedDocument,
)

# 句末标点（中英文）与段落分隔
_CHUNK_BOUNDARY = re.compile(r"\n\s*\n|(?<=[。！？；!?;.])\s+")
# 可识别的行内章节标题：短、且以常见章节词开头
_SECTION_HEADING = re.compile(
    r"^\s*(?:\d+[.、]?\s*)?(abstract|introduction|related work|background|method(?:s|ology)?|"
    r"approach|experiment(?:s)?|results?|discussion|conclusion(?:s)?|references|acknowledg\w*|"
    r"摘要|引言|绪论|相关工作|研究背景|背景|方法|实验|结果|讨论|结论|参考文献|致谢)\s*[:：]?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChunkDraft:
    """待入库的片段草稿；`ordinal` 由 `build_chunks` 统一分配。"""

    page_number: int
    element_type: str
    text: str
    section: str | None = None
    # 该页是否由图像转录而来；视觉转录不得伪称与原文匹配
    from_image: bool = False
    source_image: Any = None
    locator: Mapping[str, Any] = field(default_factory=lambda: {"granularity": "page", "bbox": None})


def build_chunks(parsed_document: Any, elements: Iterable[Any]) -> list[EvidenceChunk]:
    """按页和元素划分片段；ordinal 从 1 起，page_number 为真实 1 起始页码。

    `elements` 接受 `ChunkDraft` 或形状相同的映射（便于从持久化的结构化产物重建）。
    同一解析产物内 ordinal 连续且唯一；重复构建同一内容返回既有片段，
    不覆盖已有证据或改变历史引用的 ID。
    """
    drafts = [_as_draft(element) for element in elements]
    drafts = [draft for draft in drafts if draft.text and draft.text.strip()]
    if not drafts:
        return []

    if any(type(draft.page_number) is not int for draft in drafts):
        raise api_error("VALIDATION_ERROR", "page_number 必须是整数")
    # 相同页保持原有元素顺序，避免按文字排序打乱阅读顺序。
    drafts.sort(key=lambda item: item.page_number)

    raw = _load_raw_pages(parsed_document)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("pages"), list):
        raise api_error("PDF_PARSE_FAILED", "原始页文本缺失或损坏")
    if (
        len(raw["pages"]) != parsed_document.page_count
        or not isinstance(raw.get("normalization_revision"), str)
        or any(
            not isinstance(page, Mapping)
            or type(page.get("page_number")) is not int
            or type(page.get("from_image")) is not bool
            or not isinstance(page.get("text"), str)
            or page.get("error") is not None
            for page in raw["pages"]
        )
    ):
        raise api_error("PDF_PARSE_FAILED", "原始页文本结构不合法")
    page_entries = {page["page_number"]: page for page in raw["pages"]}
    if set(page_entries) != set(range(1, parsed_document.page_count + 1)):
        raise api_error("PDF_PARSE_FAILED", "原始页文本的页号不完整")
    rows: list[EvidenceChunk] = []
    for ordinal, draft in enumerate(drafts, start=1):
        if draft.element_type not in ELEMENT_TYPES:
            raise api_error(
                "VALIDATION_ERROR",
                f"未知的 element_type：{draft.element_type!r}",
            )
        if type(draft.page_number) is not int or not 1 <= draft.page_number <= parsed_document.page_count:
            raise api_error("VALIDATION_ERROR", "page_number 必须位于该文献真实页数内")
        if dict(draft.locator) != {"granularity": "page", "bbox": None}:
            raise api_error("VALIDATION_ERROR", "当前解析仅支持真实页级定位")

        page = page_entries[draft.page_number]
        page_text = page.get("text")
        if not isinstance(page_text, str) or bool(page.get("from_image")) != draft.from_image:
            raise api_error("PDF_PARSE_FAILED", "片段来源与原始页记录不一致")
        if draft.from_image:
            # 图像转录：原文里没有对应文字，不得声称已匹配
            original_text = None
            verification = VERIFICATION_IMAGE_UNVERIFIED
            image = draft.source_image
            if (
                not isinstance(image, FileAsset)
                or image.project_id != parsed_document.project_id
                or image.purpose != FileAsset.PURPOSE_PAGE_IMAGE
            ):
                raise api_error("VALIDATION_ERROR", "图像片段必须绑定本项目的真实页图")
        else:
            if not page_text.strip() or " ".join(draft.text.split()) not in " ".join(page_text.split()):
                raise api_error("PDF_PARSE_FAILED", "片段无法匹配同页原文")
            if draft.source_image is not None:
                raise api_error("VALIDATION_ERROR", "文本层片段不应绑定图像来源")
            original_text = page_text
            verification = VERIFICATION_TEXT_MATCHED

        rows.append(
            EvidenceChunk(
                parsed_document=parsed_document,
                ordinal=ordinal,
                page_number=draft.page_number,
                element_type=draft.element_type,
                section=draft.section,
                text=draft.text,
                original_text=original_text,
                locator=dict(draft.locator),
                verification=verification,
                source_image=draft.source_image,
                normalization_revision=raw["normalization_revision"],
                content_hash=chunk_content_hash(draft.text),
            )
        )

    with transaction.atomic():
        ParsedDocument.objects.select_for_update().get(pk=parsed_document.pk)
        existing = list(EvidenceChunk.objects.filter(parsed_document=parsed_document).order_by("ordinal"))
        if existing:
            fields = (
                "ordinal",
                "page_number",
                "element_type",
                "section",
                "text",
                "original_text",
                "locator",
                "verification",
                "source_image_id",
                "normalization_revision",
                "content_hash",
            )
            if [tuple(getattr(row, name) for name in fields) for row in existing] != [
                tuple(getattr(row, name) for name in fields) for row in rows
            ]:
                raise api_error("INVALID_JOB_STATE", "已发布的证据不能覆盖或重建为不同内容")
            return existing
        return EvidenceChunk.objects.bulk_create(rows)


def _as_draft(element: Any) -> ChunkDraft:
    if isinstance(element, ChunkDraft):
        return element
    if isinstance(element, Mapping):
        if (
            type(element.get("page_number")) is not int
            or not isinstance(element.get("element_type"), str)
            or not isinstance(element.get("text"), str)
            or type(element.get("from_image", False)) is not bool
        ):
            raise api_error("VALIDATION_ERROR", "元素字段类型不合法")
        return ChunkDraft(
            page_number=element["page_number"],
            element_type=element["element_type"],
            text=element["text"],
            section=element.get("section"),
            from_image=bool(element.get("from_image", False)),
            source_image=element.get("source_image"),
            locator=element.get("locator") or {"granularity": "page", "bbox": None},
        )
    raise api_error("VALIDATION_ERROR", "元素结构不合法")


def _load_raw_pages(parsed_document: Any) -> Mapping[str, Any] | None:
    asset = getattr(parsed_document, "raw_pages_asset", None)
    if (
        asset is None
        or asset.project_id != parsed_document.project_id
        or asset.purpose != FileAsset.PURPOSE_RAW_PAGES
        or not 0 < asset.size_bytes <= 64 * 1024 * 1024
    ):
        return None
    try:
        path = resolve_storage_key(asset.storage_key)
        with path.open("rb") as stream:
            data = stream.read(64 * 1024 * 1024 + 1)
        if len(data) != asset.size_bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
            return None
        return json.loads(data)
    except (OSError, ValueError, TypeError):
        return None


def chunk_content_hash(text: str) -> str:
    """片段内容哈希：归一化空白后计算，便于比较与缓存。"""
    material = " ".join((text or "").split())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ── 原文切片（供 services 组装 ChunkDraft）─────────────────────
def split_page_text(
    text: str,
    *,
    min_chars: int | None = None,
    max_chars: int | None = None,
) -> list[str]:
    """把一页文本切成 600—1,200 字符的片段，保留段落与相邻上下文。

    切分只依据文本本身，**不跨页、不改写文字**；无法在标点处切开时按长度硬切，
    以便超长表格行之类的内容仍能被定位，而不是整页丢弃。
    """
    floor = int(min_chars if min_chars is not None else settings.EVIDENCE_CHUNK_MIN_CHARS)
    ceiling = int(max_chars if max_chars is not None else settings.EVIDENCE_CHUNK_MAX_CHARS)
    if floor <= 0 or ceiling < floor:
        raise api_error("VALIDATION_ERROR", "切片长度参数不合法")

    cleaned = (text or "").strip()
    if not cleaned:
        return []

    chunks: list[str] = []
    offset = 0
    while len(cleaned) - offset > ceiling:
        end = offset + ceiling
        remaining = len(cleaned) - end
        if 0 < remaining < floor and len(cleaned) - offset >= 2 * floor:
            end = len(cleaned) - floor
        candidates = [
            match.end()
            for match in _CHUNK_BOUNDARY.finditer(cleaned, offset, end)
            if match.end() - offset >= floor
        ]
        if candidates:
            end = candidates[-1]
        piece = cleaned[offset:end].strip()
        if piece:
            chunks.append(piece)
        offset = end
    tail = cleaned[offset:].strip()
    if tail:
        chunks.append(tail)
    return chunks


def detect_section(text: str) -> str | None:
    """识别行内章节标题；无法确定时返回 None，**不猜测**章节名。"""
    for line in (text or "").split("\n"):
        match = _SECTION_HEADING.match(line)
        if match:
            return line.strip().rstrip(":：").strip() or None
    return None


def element_type_for(text: str) -> str:
    """粗粒度元素类型判定：只识别明显形态，其余按正文处理。

    这是**形态启发式**，不是语义识别；MVP 只承诺页级定位，
    分类不确定时归为 `text`，不臆造 table / formula。
    """
    stripped = (text or "").strip()
    if not stripped:
        return ELEMENT_TEXT
    lowered = stripped.lower()
    if re.match(r"^(fig(?:ure)?|表|图)\s*\d+", lowered) or lowered.startswith(("figure ", "table ")):
        return ELEMENT_FIGURE_CAPTION if lowered.startswith(("fig", "图")) else ELEMENT_TABLE
    if "|" in stripped and stripped.count("|") >= 3:
        return ELEMENT_TABLE
    if len(stripped) <= 400 and re.search(r"[=∑∫√±≈≤≥]|\b[a-zA-Z]\s*=\s*[^=]", stripped):
        return ELEMENT_FORMULA
    return ELEMENT_TEXT


# ── 读取（跨 Run 引用拒绝）────────────────────────────────────
def get_evidence_batch(run_scope: Any, evidence_ids: Sequence[Any]) -> list[dict[str, Any]]:
    """按解析结果批量读取证据；跨 Run 引用一律拒绝。

    EvidenceDTO 的 run_id/paper_id 通过请求 Run 与 RunPaper→ParsedDocument 关系查得。

    本函数返回**已确认属于该 Run** 的证据；不属于该 Run 的 ID 直接省略，
    由调用方表现为 404——不区分"不存在"与"属于别的 Run"，避免泄露归属。
    """
    from apps.analysis.models import RunPaper

    run_id = getattr(run_scope, "pk", run_scope)
    wanted = [str(item) for item in evidence_ids if item]
    if not wanted:
        return []

    # 该 Run 实际纳入的解析产物 → paper 映射
    visible: dict[str, Any] = {}
    for run_paper in RunPaper.objects.filter(run_id=run_id, parsed_document_id__isnull=False).only(
        "paper_id", "parsed_document_id"
    ):
        visible[str(run_paper.parsed_document_id)] = run_paper.paper_id
    if not visible:
        return []

    rows = (
        EvidenceChunk.objects.filter(id__in=wanted, parsed_document_id__in=list(visible.keys()))
        .select_related("parsed_document")
        .order_by("page_number", "ordinal", "id")
    )
    return [_evidence_dto(row, run_id=run_id, paper_id=visible[str(row.parsed_document_id)]) for row in rows]


def _evidence_dto(row: EvidenceChunk, *, run_id: Any, paper_id: Any) -> dict[str, Any]:
    """EvidenceDTO（`03` 第 2.3 节）。

    `file_url` 指向授权下载路由；页面跳转由前端用 `page_number` 完成。
    MVP 只承诺页级定位，`locator.bbox` 恒为 null，不渲染成句子级高亮。
    """
    return {
        "id": str(row.id),
        "run_id": str(run_id),
        "paper_id": str(paper_id),
        "parsed_document_id": str(row.parsed_document_id),
        "page_number": row.page_number,
        "section": row.section,
        "element_type": row.element_type,
        "text": row.text,
        "locator": dict(row.locator or {"granularity": "page", "bbox": None}),
        "verification": row.verification,
        "file_url": f"/api/v1/papers/{paper_id}/file",
    }


def list_evidence_for_run_paper(run_paper: Any, *, page_number: int | None = None) -> Any:
    """只读查询：某 RunPaper 的片段 queryset，按 page_number / ordinal / id 升序。

    允许旧 Run 读取已完整发布的解析产物；未解析成功时由调用方返回 RESULT_NOT_READY。

    ⚠ 返回的是**模型 queryset**，不是 DTO。HTTP 层必须用 `list_evidence_dtos`
    或自行序列化：直接把模型实例交给 JSON 渲染会抛
    `TypeError: Object of type EvidenceChunk is not JSON serializable`。
    """
    queryset = EvidenceChunk.objects.filter(parsed_document_id=run_paper.parsed_document_id)
    if page_number is not None:
        queryset = queryset.filter(page_number=page_number)
    return queryset.order_by("page_number", "ordinal", "id")


def list_evidence_dtos(run_paper: Any, *, page_number: int | None = None) -> list[dict[str, Any]]:
    """某 RunPaper 的 EvidenceDTO 列表（`03` 第 2.3 节）。

    API 层直接分页本列表，不需要`serializer_class`：DTO 就是可 JSON 序列化的字典。
    """
    rows = list_evidence_for_run_paper(run_paper, page_number=page_number).select_related(
        "parsed_document"
    )
    return [
        _evidence_dto(row, run_id=run_paper.run_id, paper_id=run_paper.paper_id) for row in rows
    ]
