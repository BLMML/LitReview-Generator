"""literature.api：文献列表、上传、arXiv 导入、详情、PATCH 与授权 PDF 下载。

职责（docs/mvp/01 第 4.2 节、docs/mvp/03 第 2.2 节）：
- 上传受理遵循 docs/mvp/03 的幂等语义；
- `download_paper_file` 验证 Paper → Project → owner 后输出二进制；
- 相同项目 SHA256 重复上传返回既有 Paper（201 + X-Resource-Reused:true）。

**骨架状态**：只读接口已接好查询，写接口与 arXiv 导入调用尚未实现的服务。
"""

from __future__ import annotations

from typing import Any

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
    reject_unknown_query_params,
    success,
)
from apps.common.idempotency import parse_idempotency_key
from apps.common.storage import open_asset
from apps.literature.models import Paper
from apps.literature.services import import_arxiv, update_paper, upload_pdf


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
    """GET /projects/{project_id}/papers。"""
    actor = require_actor(request)
    reject_unknown_query_params(request, {"page", "page_size", "included"})
    queryset = Paper.objects.select_related("project", "file_asset").filter(
        project_id=project_id, project__owner=actor.user
    )
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
    """POST /projects/{project_id}/papers/upload（幂等标记 K）。"""
    actor = require_actor(request)
    key = parse_idempotency_key(idempotency_header_key(request))
    uploaded = request.FILES.get("file")
    if uploaded is None:
        raise api_error("VALIDATION_ERROR", "缺少 file 字段", {"fields": {"file": ["必填"]}})
    metadata = {"title": request.data.get("title")}
    paper = upload_pdf(actor, project_id, uploaded, metadata, key)
    return success(PaperSerializer(paper).data, request=request, status_code=201)


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


@api_view(["GET"])
def download_paper_file(request: Any, paper_id: Any) -> Response | FileResponse:
    """GET /papers/{paper_id}/file：成功直接返回二进制，失败仍是标准 JSON 错误。"""
    actor = require_actor(request)
    paper = (
        Paper.objects.select_related("project", "file_asset")
        .filter(pk=paper_id, project__owner=actor.user)
        .first()
    )
    if paper is None:
        raise api_error("NOT_FOUND")

    asset = paper.file_asset
    handle = open_asset(asset)
    response = FileResponse(handle, content_type=asset.media_type or "application/pdf")
    response["Content-Length"] = str(asset.size_bytes)
    # inline 便于 PDF.js 整文件加载；文件名按 UTF-8 编码，不泄漏存储路径
    response["Content-Disposition"] = f"inline; filename*=UTF-8''{asset.original_filename}"
    return response


urlpatterns = [
    path("projects/<uuid:project_id>/papers", project_papers, name="project-papers"),
    path("projects/<uuid:project_id>/papers/upload", paper_upload, name="paper-upload"),
    path("projects/<uuid:project_id>/imports/arxiv", arxiv_import, name="arxiv-import"),
    path("papers/<uuid:paper_id>", paper_detail, name="paper-detail"),
    path("papers/<uuid:paper_id>/file", download_paper_file, name="paper-file"),
]
