"""页级解析：冻结配置、逐页图像辅助和持有效租约的原子发布。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from apps.common.http import ApiError, api_error
from apps.common.locking import check_execution_lease
from apps.common.models import FileAsset
from apps.common.storage import commit_asset, discard_staged, resolve_storage_key, stage_file
from apps.jobs.models import Job
from apps.jobs.services import ExecutionContext
from apps.llm.client import call_structured
from apps.llm.schemas import TASK_PAGE_EXTRACT, ModelTaskRequest
from apps.parsing.config import effective_config
from apps.parsing.evidence import (
    ChunkDraft,
    build_chunks,
    chunk_content_hash,
    detect_section,
    element_type_for,
    split_page_text,
)
from apps.parsing.models import ELEMENT_TYPES, EvidenceChunk, ParsedDocument
from apps.parsing.pdf import extract_pages, inspect_pdf, render_page

logger = logging.getLogger("apps.parsing.services")
JSON_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    text: str
    from_image: bool = False
    source_image: Any = None
    error: Mapping[str, Any] | None = None
    elements: tuple[Mapping[str, Any], ...] = ()


def config_fingerprint(parser_config: Mapping[str, Any] | None) -> str:
    return _content_hash(effective_config(parser_config))


def _project_id(snapshot: Any) -> Any:
    project_id = getattr(snapshot, "project_id", None)
    run = getattr(snapshot, "run", None)
    if project_id is None and run is not None:
        project_id = run.project_id
    if project_id is None or (run is not None and str(run.project_id) != str(project_id)):
        raise api_error("VALIDATION_ERROR", "解析快照缺少项目或项目归属不一致")
    return project_id


def _validate_snapshot(context: ExecutionContext | None, snapshot: Any) -> tuple[Any, Any, FileAsset, str]:
    from apps.literature.models import Paper

    project_id = _project_id(snapshot)
    paper_id = getattr(snapshot, "paper_id", getattr(snapshot, "pk", None))
    asset = getattr(snapshot, "file_asset", None)
    sha256 = getattr(snapshot, "file_sha256", None)
    paper = Paper.objects.filter(pk=paper_id, project_id=project_id).first()
    if (
        paper is None
        or not isinstance(asset, FileAsset)
        or asset.project_id != paper.project_id
        or asset.pk != paper.file_asset_id
        or sha256 != paper.file_sha256
        or sha256 != asset.sha256
        or asset.purpose != FileAsset.PURPOSE_ORIGINAL_PDF
    ):
        raise api_error("VALIDATION_ERROR", "论文、快照与原始文件的归属或哈希不一致")
    if context is not None:
        if not isinstance(context, ExecutionContext) or str(context.project_id) != str(project_id):
            raise api_error("INVALID_JOB_STATE", "执行上下文与解析项目不一致")
        run_id = getattr(snapshot, "run_id", None)
        if run_id is not None and str(context.run_id) != str(run_id):
            raise api_error("INVALID_JOB_STATE", "执行上下文与冻结 Run 不一致")
        context.ensure_not_cancelled()
    return project_id, paper.pk, asset, str(sha256)


def _check_context(context: ExecutionContext | None) -> None:
    if context is not None:
        context.ensure_not_cancelled()


def _locked_job(context: ExecutionContext | None, project_id: Any) -> Job | None:
    """调用者保持此事务到阶段提交；取消、恢复和心跳在同一行上串行。"""
    if context is None:
        return None
    current = Job.objects.select_for_update().filter(pk=context.job_id, project_id=project_id).first()
    if current is None or str(context.project_id) != str(project_id):
        raise api_error("INVALID_JOB_STATE", "执行上下文已失效")
    check_execution_lease(current, context.lease_token)
    return current


def _check_locked_job(job: Job | None, context: ExecutionContext | None) -> None:
    if job is not None:
        check_execution_lease(job, context.lease_token)


def parse_paper(
    context: ExecutionContext | None, paper_snapshot: Any, parser_config: Mapping[str, Any] | None = None
) -> ParsedDocument:
    """文本优先；所有页面成功后才发布；None 上下文只支持本地文本解析。"""
    project_id, paper_id, source_asset, source_sha256 = _validate_snapshot(context, paper_snapshot)
    config = effective_config(parser_config)
    revision = config["PARSER_REVISION"]
    config_hash = _content_hash(config)
    cache_key = {
        "project_id": project_id,
        "source_sha256": source_sha256,
        "parser_revision": revision,
        "config_hash": config_hash,
    }
    cached = ParsedDocument.objects.filter(**cache_key).first()
    if cached is not None:
        _validate_cached(cached, config)
        with transaction.atomic():
            current = _locked_job(context, project_id)
            _check_locked_job(current, context)
        return cached

    inspection = inspect_pdf(source_asset)
    page_texts = extract_pages(source_asset)
    if [page.page_number for page in page_texts] != list(range(1, inspection.page_count + 1)):
        raise api_error("PDF_PARSE_FAILED", "文本层页号与真实页数不一致")
    pages = _resolve_pages(context, paper_snapshot, source_asset, page_texts, config)
    failed = [page for page in pages if page.error is not None or not page.text.strip()]
    raw_payload = _raw_pages_payload(pages, config)
    drafts = _drafts_for(pages, config)
    structured_payload = _structured_payload(pages, drafts, config, config_hash)
    if failed:
        diagnostic_ids = _commit_diagnostics(context, project_id, raw_payload, structured_payload)
        raise api_error(
            "PDF_PARSE_FAILED",
            f"有 {len(failed)} 页无法解析，未发布解析产物",
            {
                "pages": [
                    {"page_number": page.page_number, "error": dict(page.error or {})} for page in failed
                ],
                "diagnostics": diagnostic_ids,
            },
            retryable=True,
        )
    return _publish(
        context=context,
        project_id=project_id,
        paper_id=paper_id,
        source_asset=source_asset,
        source_sha256=source_sha256,
        revision=revision,
        config_hash=config_hash,
        raw_payload=raw_payload,
        structured_payload=structured_payload,
        drafts=drafts,
        config=config,
    )


def _resolve_pages(
    context: ExecutionContext | None,
    snapshot: Any,
    asset: FileAsset,
    page_texts: Sequence[Any],
    config: Mapping[str, Any],
) -> list[ParsedPage]:
    pages = []
    for page in page_texts:
        _check_context(context)
        if page.text.strip():
            pages.append(ParsedPage(page.page_number, page.text))
        elif context is None:
            pages.append(
                ParsedPage(
                    page.page_number,
                    "",
                    error={"code": "PDF_PARSE_FAILED", "message": "该页没有文本层，且未启用图像辅助"},
                )
            )
        else:
            # PageExtract 没有页号字段；一次只给一页，页号永远由后端绑定。
            pages.append(_transcribe_page(context, snapshot, asset, page.page_number, config))
    return pages


def _transcribe_page(
    context: ExecutionContext, snapshot: Any, source: FileAsset, number: int, config: Mapping[str, Any]
) -> ParsedPage:
    image = None
    try:
        image = _render_and_commit(context, _project_id(snapshot), source, number, config)
        request = ModelTaskRequest(
            task_type=TASK_PAGE_EXTRACT,
            template_version=config["PAGE_TEMPLATE_VERSION"],
            schema_version=config["SCHEMA_VERSION"],
            model_config={**config["MODEL_CONFIG"], "image_asset_ids": [str(image.pk)]},
            payload={"paper_title": str(snapshot.title or ""), "page_number": number, "page_content": ""},
            logical_call_id=uuid.uuid4(),
        )
        result = call_structured(context, request)
        _check_context(context)
        elements = _elements_of(result.payload)
        if not elements:
            raise api_error("PDF_PARSE_FAILED", "图像辅助未识别出可用文字")
        return ParsedPage(
            number, "\n".join(item["text"] for item in elements), True, image, elements=tuple(elements)
        )
    except ApiError as exc:
        if exc.code == "INVALID_JOB_STATE":
            raise
        return ParsedPage(number, "", source_image=image, error=exc.as_error_body())


def _elements_of(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != {"elements"}:
        raise api_error("PDF_PARSE_FAILED", "图像转录结构不合法")
    elements = payload["elements"]
    if not isinstance(elements, list) or len(elements) > 300:
        raise api_error("PDF_PARSE_FAILED", "图像转录元素数量不合法")
    for item in elements:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"element_type", "text", "section"}
            or item["element_type"] not in ELEMENT_TYPES
            or not isinstance(item["text"], str)
            or not item["text"].strip()
            or (item["section"] is not None and not isinstance(item["section"], str))
        ):
            raise api_error("PDF_PARSE_FAILED", "图像转录元素不合法")
    return elements


def _render_and_commit(
    context: ExecutionContext, project_id: Any, source: FileAsset, number: int, config: Mapping[str, Any]
) -> FileAsset:
    from apps.projects.models import Project

    payload = render_page(
        source, number, scale=config["PAGE_RENDER_SCALE"], max_bytes=config["PAGE_IMAGE_MAX_BYTES"]
    )
    staged = None
    asset = None
    try:
        staged = stage_file(
            io.BytesIO(payload),
            limit_bytes=config["PAGE_IMAGE_MAX_BYTES"],
            original_filename=f"page-{number}.png",
            media_type="image/png",
            suffix=".png",
        )
        with transaction.atomic():
            current = _locked_job(context, project_id)
            asset = commit_asset(
                Project.objects.get(pk=project_id), staged, FileAsset.PURPOSE_PAGE_IMAGE, source_job=current
            )
            staged = None
            _check_locked_job(current, context)
        return asset
    except BaseException:
        _cleanup_rolled_back([asset] if asset else [])
        raise
    finally:
        discard_staged(staged)


def _raw_pages_payload(pages: Sequence[ParsedPage], config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "parser_revision": config["PARSER_REVISION"],
        "normalization_revision": config["NORMALIZATION_REVISION"],
        "pages": [
            {
                "page_number": page.page_number,
                "text": page.text,
                "from_image": page.from_image,
                "error": dict(page.error) if page.error else None,
            }
            for page in pages
        ],
    }


def _structured_payload(
    pages: Sequence[ParsedPage], drafts: Sequence[ChunkDraft], config: Mapping[str, Any], config_hash: str
) -> dict[str, Any]:
    return {
        "parser_revision": config["PARSER_REVISION"],
        "normalization_revision": config["NORMALIZATION_REVISION"],
        "config_hash": config_hash,
        "config": dict(config),
        "page_count": len(pages),
        "pages": [
            {
                "page_number": page.page_number,
                "from_image": page.from_image,
                "source_image_id": str(page.source_image.pk) if page.source_image else None,
                "chunk_count": sum(draft.page_number == page.page_number for draft in drafts),
                "error": dict(page.error) if page.error else None,
            }
            for page in pages
        ],
        "chunks": iter_draft_payloads(drafts),
    }


def _stage_json(payload: Mapping[str, Any], suffix: str) -> Any:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return stage_file(
        io.BytesIO(data),
        limit_bytes=JSON_LIMIT_BYTES,
        original_filename=f"parsed{suffix}",
        media_type="application/json",
        suffix=suffix,
    )


def _commit_diagnostics(
    context: ExecutionContext | None, project_id: Any, raw: Mapping[str, Any], structured: Mapping[str, Any]
) -> dict[str, str]:
    from apps.projects.models import Project

    staged = []
    assets = []
    try:
        staged.append(_stage_json(raw, ".raw.json"))
        staged.append(_stage_json(structured, ".structured.json"))
        with transaction.atomic():
            current = _locked_job(context, project_id)
            project = Project.objects.get(pk=project_id)
            for temporary, purpose in zip(
                staged, (FileAsset.PURPOSE_RAW_PAGES, FileAsset.PURPOSE_STRUCTURED_PARSE), strict=True
            ):
                assets.append(commit_asset(project, temporary, purpose, source_job=current))
            _check_locked_job(current, context)
        return {"raw_pages_asset_id": str(assets[0].pk), "structured_asset_id": str(assets[1].pk)}
    except ApiError as exc:
        _cleanup_rolled_back(assets)
        if exc.code == "INVALID_JOB_STATE":
            raise
        logger.warning("解析诊断产物写入失败：%s", exc.code)
        return {}
    except Exception as exc:
        _cleanup_rolled_back(assets)
        logger.warning("解析诊断产物写入失败：%s", type(exc).__name__)
        return {}
    finally:
        for temporary in staged:
            discard_staged(temporary)


def _publish(
    *,
    context: ExecutionContext | None,
    project_id: Any,
    paper_id: Any,
    source_asset: FileAsset,
    source_sha256: str,
    revision: str,
    config_hash: str,
    raw_payload: Mapping[str, Any],
    structured_payload: Mapping[str, Any],
    drafts: Sequence[ChunkDraft],
    config: Mapping[str, Any],
) -> ParsedDocument:
    from apps.projects.models import Project

    raw_staged = structured_staged = None
    assets = []
    try:
        raw_staged = _stage_json(raw_payload, ".raw.json")
        structured_staged = _stage_json(structured_payload, ".structured.json")
        with transaction.atomic():
            current = _locked_job(context, project_id)
            # 同一原文件的发布串行；只在此短事务内持锁，不包围 PDF/模型处理。
            FileAsset.objects.select_for_update().get(pk=source_asset.pk, project_id=project_id)
            existing = ParsedDocument.objects.filter(
                project_id=project_id,
                source_sha256=source_sha256,
                parser_revision=revision,
                config_hash=config_hash,
            ).first()
            if existing is not None:
                _validate_cached(existing, config)
                _check_locked_job(current, context)
                return existing
            project = Project.objects.get(pk=project_id)
            assets.append(commit_asset(project, raw_staged, FileAsset.PURPOSE_RAW_PAGES, source_job=current))
            raw_staged = None
            assets.append(
                commit_asset(
                    project, structured_staged, FileAsset.PURPOSE_STRUCTURED_PARSE, source_job=current
                )
            )
            structured_staged = None
            document = ParsedDocument.objects.create(
                project_id=project_id,
                paper_id=paper_id,
                source_file=source_asset,
                source_sha256=source_sha256,
                parser_revision=revision,
                config_hash=config_hash,
                page_count=len(raw_payload["pages"]),
                content_hash=_content_hash(raw_payload),
                raw_pages_asset=assets[0],
                structured_asset=assets[1],
            )
            build_chunks(document, drafts)
            _check_locked_job(current, context)
        return document
    except BaseException:
        _cleanup_rolled_back(assets)
        raise
    finally:
        discard_staged(raw_staged)
        discard_staged(structured_staged)


def _cleanup_rolled_back(assets: Sequence[FileAsset]) -> None:
    for asset in assets:
        if not FileAsset.objects.filter(storage_key=asset.storage_key).exists():
            resolve_storage_key(asset.storage_key).unlink(missing_ok=True)


def _read_verified(asset: FileAsset, project_id: Any, purpose: str, limit: int) -> bytes:
    if asset.project_id != project_id or asset.purpose != purpose or asset.size_bytes > limit:
        raise api_error("PDF_PARSE_FAILED", "解析缓存资产归属或大小不合法")
    try:
        with resolve_storage_key(asset.storage_key).open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise api_error("PDF_PARSE_FAILED", "解析缓存文件缺失，需恢复原始产物") from exc
    if len(data) != asset.size_bytes or hashlib.sha256(data).hexdigest() != asset.sha256:
        raise api_error("PDF_PARSE_FAILED", "解析缓存文件完整性校验失败")
    return data


def _validate_cached(document: ParsedDocument, config: Mapping[str, Any]) -> None:
    """缺失/损坏时显式失败，保留历史产物及证据 ID，不原位替换。"""
    try:
        raw = json.loads(
            _read_verified(
                document.raw_pages_asset, document.project_id, FileAsset.PURPOSE_RAW_PAGES, JSON_LIMIT_BYTES
            )
        )
        structured = json.loads(
            _read_verified(
                document.structured_asset,
                document.project_id,
                FileAsset.PURPOSE_STRUCTURED_PARSE,
                JSON_LIMIT_BYTES,
            )
        )
        numbers = list(range(1, document.page_count + 1))
        if (
            _content_hash(raw) != document.content_hash
            or structured["config"] != config
            or structured["config_hash"] != document.config_hash
            or _content_hash(config) != document.config_hash
            or raw["parser_revision"] != document.parser_revision
            or raw["normalization_revision"] != config["NORMALIZATION_REVISION"]
            or structured["page_count"] != document.page_count
            or [page["page_number"] for page in raw["pages"]] != numbers
            or [page["page_number"] for page in structured["pages"]] != numbers
        ):
            raise ValueError("manifest mismatch")
        chunks = list(EvidenceChunk.objects.filter(parsed_document=document).order_by("ordinal"))
        expected = structured["chunks"]
        if not chunks or len(chunks) != len(expected):
            raise ValueError("incomplete chunks")
        pages = {page["page_number"]: page for page in raw["pages"]}
        page_images = {page["page_number"]: page["source_image_id"] for page in structured["pages"]}
        for index, (chunk, item) in enumerate(zip(chunks, expected, strict=True), start=1):
            page = pages[chunk.page_number]
            image_id = str(chunk.source_image_id) if chunk.source_image_id else None
            if (
                chunk.ordinal != index
                or page["error"] is not None
                or item
                != {
                    "page_number": chunk.page_number,
                    "element_type": chunk.element_type,
                    "text": chunk.text,
                    "section": chunk.section,
                    "from_image": page["from_image"],
                    "source_image_id": image_id,
                    "locator": chunk.locator,
                }
                or chunk.content_hash != chunk_content_hash(chunk.text)
                or chunk.normalization_revision != config["NORMALIZATION_REVISION"]
            ):
                raise ValueError("chunk mismatch")
            if page["from_image"]:
                if (
                    chunk.verification != "image_unverified"
                    or chunk.original_text is not None
                    or image_id != page_images[chunk.page_number]
                ):
                    raise ValueError("image provenance mismatch")
                _read_verified(
                    chunk.source_image,
                    document.project_id,
                    FileAsset.PURPOSE_PAGE_IMAGE,
                    config["PAGE_IMAGE_MAX_BYTES"],
                )
            elif (
                chunk.verification != "text_matched"
                or chunk.original_text != page["text"]
                or image_id is not None
                or " ".join(chunk.text.split()) not in " ".join(page["text"].split())
            ):
                raise ValueError("text provenance mismatch")
        for page in structured["pages"]:
            if page["error"] is not None or page["chunk_count"] != sum(
                chunk.page_number == page["page_number"] for chunk in chunks
            ):
                raise ValueError("page coverage mismatch")
    except ApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise api_error("PDF_PARSE_FAILED", "解析缓存清单或片段集合不完整，需恢复原始产物") from exc


def _content_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _drafts_for(pages: Sequence[ParsedPage], config: Mapping[str, Any]) -> list[ChunkDraft]:
    drafts = []
    for page in pages:
        if not page.text.strip() or page.error is not None:
            continue
        elements = page.elements or (
            {
                "text": page.text,
                "element_type": element_type_for(page.text),
                "section": detect_section(page.text),
            },
        )
        for element in elements:
            for piece in split_page_text(
                element["text"],
                min_chars=config["EVIDENCE_CHUNK_MIN_CHARS"],
                max_chars=config["EVIDENCE_CHUNK_MAX_CHARS"],
            ):
                drafts.append(
                    ChunkDraft(
                        page.page_number,
                        element["element_type"],
                        piece,
                        element["section"],
                        page.from_image,
                        page.source_image,
                    )
                )
    return drafts


def iter_draft_payloads(drafts: Iterable[ChunkDraft]) -> list[dict[str, Any]]:
    return [
        {
            "page_number": draft.page_number,
            "element_type": draft.element_type,
            "text": draft.text,
            "section": draft.section,
            "from_image": draft.from_image,
            "source_image_id": str(draft.source_image.pk) if draft.source_image else None,
            "locator": dict(draft.locator),
        }
        for draft in drafts
    ]
