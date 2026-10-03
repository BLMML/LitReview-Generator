"""literature.api：文献列表、上传、arXiv 导入、详情、PATCH 与授权 PDF 下载。

职责（docs/mvp/01 第 4.2 节、docs/mvp/03 第 2.2 节）：
- 上传受理遵循 docs/mvp/03 的幂等语义；
- `download_paper_file` 验证 Paper → Project → owner 后输出二进制；
- 相同项目 SHA256 重复上传返回既有 Paper（201 + X-Resource-Reused:true）。

**骨架状态**：只读接口已接好查询，写接口与 arXiv 导入调用尚未实现的服务。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from apps.common.auth import require_actor
from apps.common.http import (
    EnvelopePagination,
    StrictSerializer,
    api_error,
    idempotency_header_key,
    reject_unknown_multipart_fields,
    reject_unknown_query_params,
    success,
)
from apps.common.idempotency import parse_idempotency_key
from apps.common.storage import open_asset
from apps.literature.models import Paper
from apps.literature.services import (
    PDF_MEDIA_TYPE,
    import_arxiv,
    update_paper,
    upload_pdf,
)
from apps.projects.models import Project


class PaperSerializer(serializers.Serializer):
    """PaperDTO（docs/mvp/03 第 2.2 节）。"""

    id = serializers.UUIDField()
    project_id = serializers.UUIDField(source="project.id")
    title = serializers.CharField()
    authors = serializers.JSONField()
    year = serializers.IntegerField(allow_null=True)
    source = serializers.CharField()
    source_id = serializers.CharField(allow_null=True)
    source_url = serializers.CharField(allow_null=True)
    included = serializers.BooleanField()
    version = serializers.IntegerField()
    file_sha256 = serializers.CharField()
    file_size = serializers.IntegerField(source="file_asset.size_bytes")
    page_count = serializers.IntegerField()
    original_filename = serializers.CharField(source="file_asset.original_filename")
    file_url = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def get_file_url(self, obj: Paper) -> str:
        # 由授权路由构造；绝不返回磁盘路径或存储键
        return f"/api/v1/papers/{obj.id}/file"


class PaperPatchSerializer(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(
        min_length=1, max_length=settings.MVP_LIMITS["PAPER_TITLE_MAX"], required=False
    )
    authors = serializers.ListField(child=serializers.CharField(max_length=200), required=False)
    year = serializers.IntegerField(
        min_value=settings.MVP_LIMITS["PAPER_YEAR_MIN"], required=False, allow_null=True
    )
    included = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        editable = {"title", "authors", "year", "included"}
        if not (editable & set(attrs)):
            raise serializers.ValidationError({"title": ["至少提供一个可修改字段"]})
        return attrs


class ArxivImportSerializer(StrictSerializer):
    arxiv_ids = serializers.ListField(child=serializers.CharField(), allow_empty=False)


@api_view(["GET"])
def project_papers(request: Any, project_id: Any) -> Response:
    """GET /projects/{project_id}/papers。

    先校验 Project 的存在与归属：他人的项目必须是 404，而不是「200 + 空列表」——
    后者会让「项目存在但对我不可见」与「项目本来就没有文献」无法区分，
    也会让前端把越权当成正常空结果（docs/mvp/00 第 6 节：越权资源统一 404）。
    """
    actor = require_actor(request)
    reject_unknown_query_params(request, {"page", "page_size", "included"})
    if not Project.objects.filter(pk=project_id, owner=actor.user).exists():
        raise api_error("NOT_FOUND")

    queryset = Paper.objects.select_related("project", "file_asset").filter(project_id=project_id)
    included = request.query_params.get("included")
    if included is not None:
        if included.lower() not in {"true", "false"}:
            raise api_error("VALIDATION_ERROR", "included 只能是 true/false")
        queryset = queryset.filter(included=included.lower() == "true")

    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset.order_by("-created_at", "-id"), request)
    return paginator.get_paginated_response(PaperSerializer(page, many=True).data)


@api_view(["POST"])
@parser_classes([MultiPartParser])
def paper_upload(request: Any, project_id: Any) -> Response:
    """POST /projects/{project_id}/papers/upload（幂等标记 K）。

    上传体只允许 `file`（必填）与 `title`（可选 1—500 字符）；其余字段一律 400，
    不被静默忽略。同项目同 SHA256 重传返回既有 Paper，成功码仍是 201，
    并带 `X-Resource-Reused: true`。
    """
    actor = require_actor(request)
    reject_unknown_multipart_fields(request, allowed={"file", "title"})
    key = parse_idempotency_key(idempotency_header_key(request))

    uploaded = request.FILES.get("file")
    if uploaded is None:
        raise api_error("VALIDATION_ERROR", "缺少 file 字段", {"fields": {"file": ["必填"]}})

    result = upload_pdf(actor, project_id, uploaded, {"title": request.data.get("title")}, key)
    # 复用幂等记录里的原受理响应；不重新序列化当前 Paper，
    # 否则用户之后的编辑会污染重放结果
    response = success(result.data, request=request, status_code=201)
    if result.reused:
        # 复用既有文献：不改其元数据与纳入状态，只提示前端这是同一份文件
        response["X-Resource-Reused"] = "true"
    return response


@api_view(["POST"])
def arxiv_import(request: Any, project_id: Any) -> Response:
    """POST /projects/{project_id}/imports/arxiv（幂等标记 K，202）。"""
    actor = require_actor(request)
    key = parse_idempotency_key(idempotency_header_key(request))
    serializer = ArxivImportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    accepted = import_arxiv(actor, project_id, serializer.validated_data["arxiv_ids"], key)
    return success(accepted.as_dto(), request=request, status_code=202)


@api_view(["GET", "PATCH"])
def paper_detail(request: Any, paper_id: Any) -> Response:
    """GET /papers/{paper_id} 与 PATCH /papers/{paper_id}。"""
    actor = require_actor(request)
    paper = (
        Paper.objects.select_related("project", "file_asset")
        .filter(pk=paper_id, project__owner=actor.user)
        .first()
    )
    if paper is None:
        raise api_error("NOT_FOUND")

    if request.method == "GET":
        return success(PaperSerializer(paper).data, request=request)

    serializer = PaperPatchSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    validated = serializer.validated_data
    changes = {
        key: validated[key]
        for key in ("title", "authors", "year", "included")
        if key in validated
    }
    updated = update_paper(actor, paper_id, validated["expected_version"], changes)
    return success(PaperSerializer(updated).data, request=request)


def _content_disposition(filename: str) -> str:
    """按 RFC 5987 编码文件名；非 ASCII 名不得直接写进报文头。

    `filename*=UTF-8''...` 的取值必须是百分号编码：Django 在 WSGI 下用 latin-1
    编码响应头，中文文件名不做处理会直接抛 UnicodeEncodeError。
    同时给出 ASCII 回退名，供不认识 `filename*` 的客户端使用。
    """
    encoded = quote(filename, safe="")
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "paper.pdf"
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


@api_view(["GET"])
def download_paper_file(request: Any, paper_id: Any) -> Response | FileResponse:
    """GET /papers/{paper_id}/file：成功直接返回二进制，失败仍是标准 JSON 错误。

    Paper → Project → owner 的归属校验用一次带 owner 过滤的查询完成：
    跨用户与不存在都表现为 404，且只读取 FileAsset，不暴露磁盘路径。

    **响应类型固定为 application/pdf**，不沿用 `asset.media_type`：该字段在早期版本里
    来自上传者声明的 `Content-Type`，历史数据中可能是 `text/html`。契约保证这个端点
    只返回 PDF，因此不能因为库里存着旧值就把响应类型交给历史数据决定。
    """
    actor = require_actor(request)
    paper = (
        Paper.objects.select_related("project", "file_asset")
        .filter(pk=paper_id, project__owner=actor.user)
        .first()
    )
    if paper is None:
        raise api_error("NOT_FOUND")

    asset = paper.file_asset
    if asset is None:  # pragma: no cover - OneToOne 必填，仅防御
        raise api_error("NOT_FOUND", "文件不存在或不可读")

    handle = open_asset(asset)
    response = FileResponse(handle, content_type=PDF_MEDIA_TYPE)
    response["Content-Length"] = str(asset.size_bytes)
    # inline 便于 PDF.js 整文件加载；文件名编码后输出，不泄漏存储路径
    response["Content-Disposition"] = _content_disposition(asset.original_filename)
    return response


urlpatterns = [
    path("projects/<uuid:project_id>/papers", project_papers, name="project-papers"),
    path("projects/<uuid:project_id>/papers/upload", paper_upload, name="paper-upload"),
    path("projects/<uuid:project_id>/imports/arxiv", arxiv_import, name="arxiv-import"),
    path("papers/<uuid:paper_id>", paper_detail, name="paper-detail"),
    path("papers/<uuid:paper_id>/file", download_paper_file, name="paper-file"),
]
