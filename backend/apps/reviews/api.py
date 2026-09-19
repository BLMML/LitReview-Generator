"""reviews.api：生成入口、综述与历史版本、核验 / 导出创建和查询、授权 Word 下载。

职责（docs/mvp/01 第 4.6 节、docs/mvp/03 第 2.5 节）：
- 导出请求指向确切 revision 和报告；缺少匹配的报告时拒绝；
- 历史列表的 revision_id 过滤值必须属于当前 Review，否则 404。

**骨架状态**：读接口已实现；生成 / 核验 / 导出调用尚未实现的服务。
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import FileResponse
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view
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
from apps.reviews.models import Export, Review, ReviewRevision, ValidationReport
from apps.reviews.services import (
    create_export,
    create_review,
    create_validation,
    save_revision,
)

# Word 的正式 MIME；下载按 attachment 处理
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class ReviewCreateSerializer(StrictSerializer):
    title = serializers.CharField(
        min_length=1, max_length=settings.MVP_LIMITS["REVIEW_TITLE_MAX"], required=False
    )
    target_chars = serializers.IntegerField(
        min_value=settings.MVP_LIMITS["REVIEW_TARGET_CHARS_MIN"],
        max_value=settings.MVP_LIMITS["REVIEW_TARGET_CHARS_MAX"],
        default=settings.MVP_LIMITS["REVIEW_TARGET_CHARS_DEFAULT"],
    )
    style = serializers.ChoiceField(
        choices=list(settings.REVIEW_STYLES_ALLOWED), default=settings.REVIEW_STYLE_DEFAULT
    )


class RevisionCreateSerializer(StrictSerializer):
    """全量五节正文；结构校验由 reviews.schemas 统一执行。"""

    base_revision_number = serializers.IntegerField(min_value=1)
    sections = serializers.ListField(child=serializers.DictField(), allow_empty=False)


class ValidationCreateSerializer(StrictSerializer):
    revision_id = serializers.UUIDField()


class ExportCreateSerializer(StrictSerializer):
    revision_id = serializers.UUIDField()
    validation_id = serializers.UUIDField()
    acknowledge_warnings = serializers.BooleanField(default=False)


class ReviewSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    project_id = serializers.UUIDField(source="project.id")
    run_id = serializers.UUIDField(source="run.id")
    title = serializers.CharField()
    analysis_revision = serializers.IntegerField()
    target_chars = serializers.IntegerField()
    style = serializers.CharField()
    latest_revision = serializers.SerializerMethodField()
    job_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()

    def get_latest_revision(self, obj: Review) -> Any:
        """首次生成未完成为 null，不展示空白“成功综述”。"""
        revision = obj.revisions.order_by("-revision_number").first()
        return ReviewRevisionSerializer(revision).data if revision else None

    def get_job_id(self, obj: Review) -> str | None:
        from apps.jobs.models import Job

        job = (
            Job.objects.filter(resource_type="review", resource_id=obj.id)
            .order_by("-attempt")
            .first()
        )
        return str(job.id) if job else None


class ReviewRevisionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    review_id = serializers.UUIDField(source="review.id")
    run_id = serializers.UUIDField(source="run.id")
    revision_number = serializers.IntegerField()
    analysis_revision = serializers.IntegerField()
    corpus_revision = serializers.IntegerField()
    sections = serializers.JSONField()
    source = serializers.CharField()
    created_by = serializers.SerializerMethodField()
    base_revision_number = serializers.SerializerMethodField()
    content_hash = serializers.CharField()
    created_at = serializers.DateTimeField()

    def get_created_by(self, obj: ReviewRevision) -> str:
        return str(obj.created_by_id)

    def get_base_revision_number(self, obj: ReviewRevision) -> int | None:
        return obj.base_revision.revision_number if obj.base_revision_id else None


class ValidationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    review_id = serializers.SerializerMethodField()
    revision_id = serializers.UUIDField(source="revision.id")
    result = serializers.CharField(allow_null=True)
    structure_passed = serializers.BooleanField(allow_null=True)
    blocks = serializers.JSONField(allow_null=True)
    content_hash = serializers.CharField()
    job_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()

    def get_review_id(self, obj: ValidationReport) -> str:
        return str(obj.revision.review_id)

    def get_job_id(self, obj: ValidationReport) -> str | None:
        from apps.jobs.models import Job

        job = (
            Job.objects.filter(resource_type="validation_report", resource_id=obj.id)
            .order_by("-attempt")
            .first()
        )
        return str(job.id) if job else None


class ExportSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    review_id = serializers.SerializerMethodField()
    revision_id = serializers.UUIDField(source="revision.id")
    validation_id = serializers.UUIDField(source="validation_report.id")
    status = serializers.CharField()
    acknowledge_warnings = serializers.BooleanField()
    warnings = serializers.JSONField()
    template_revision = serializers.CharField()
    file_url = serializers.SerializerMethodField()
    error = serializers.JSONField()
    job_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()

    def get_review_id(self, obj: Export) -> str:
        return str(obj.revision.review_id)

    def get_file_url(self, obj: Export) -> str | None:
        return f"/api/v1/exports/{obj.id}/file" if obj.status == "ready" else None

    def get_job_id(self, obj: Export) -> str | None:
        from apps.jobs.models import Job

        job = (
            Job.objects.filter(resource_type="export", resource_id=obj.id)
            .order_by("-attempt")
            .first()
        )
        return str(job.id) if job else None


def _owned_review(actor: Any, review_id: Any) -> Review:
    review = (
        Review.objects.select_related("project", "run")
        .filter(pk=review_id, project__owner=actor.user)
        .first()
    )
    if review is None:
        raise api_error("NOT_FOUND")
    return review


def _paginate(request: Any, queryset: Any, serializer: Any) -> Response:
    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(serializer(page, many=True).data)


@api_view(["POST"])
def run_reviews_create(request: Any, run_id: Any) -> Response:
    """POST /runs/{run_id}/reviews（202，幂等标记 K）。"""
    actor = require_actor(request)
    key = parse_idempotency_key(idempotency_header_key(request))
    serializer = ReviewCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    accepted_job = create_review(actor, run_id, serializer.validated_data, key)
    return success(accepted_job.as_dto(), request=request, status_code=202)


@api_view(["GET"])
def project_reviews(request: Any, project_id: Any) -> Response:
    """GET /projects/{project_id}/reviews。"""
    actor = require_actor(request)
    reject_unknown_query_params(request, {"page", "page_size"})
    queryset = Review.objects.select_related("project", "run").filter(
        project_id=project_id, project__owner=actor.user
    )
    return _paginate(request, queryset.order_by("-created_at", "-id"), ReviewSerializer)


@api_view(["GET"])
def review_detail(request: Any, review_id: Any) -> Response:
    """GET /reviews/{review_id}，包含 latest_revision（首次生成为 null）。"""
    actor = require_actor(request)
    return success(ReviewSerializer(_owned_review(actor, review_id)).data, request=request)


@api_view(["POST", "GET"])
def review_revisions(request: Any, review_id: Any) -> Response:
    """POST 保存新版本（201）与 GET 历史版本列表。"""
    actor = require_actor(request)
    review = _owned_review(actor, review_id)

    if request.method == "GET":
        reject_unknown_query_params(request, {"page", "page_size"})
        queryset = ReviewRevision.objects.filter(review=review)
        return _paginate(
            request, queryset.order_by("-revision_number"), ReviewRevisionSerializer
        )

    serializer = RevisionCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    revision = save_revision(
        actor,
        review_id,
        serializer.validated_data["base_revision_number"],
        serializer.validated_data["sections"],
    )
    return success(ReviewRevisionSerializer(revision).data, request=request, status_code=201)


@api_view(["GET"])
def review_revision_detail(request: Any, review_id: Any, revision_id: Any) -> Response:
    """GET /reviews/{review_id}/revisions/{revision_id}。"""
    actor = require_actor(request)
    review = _owned_review(actor, review_id)
    revision = ReviewRevision.objects.filter(pk=revision_id, review=review).first()
    if revision is None:
        raise api_error("NOT_FOUND")
    return success(ReviewRevisionSerializer(revision).data, request=request)


@api_view(["POST", "GET"])
def review_validations(request: Any, review_id: Any) -> Response:
    """POST 受理核验（202）与 GET 历史核验列表（可按 revision_id 过滤）。"""
    actor = require_actor(request)
    review = _owned_review(actor, review_id)

    if request.method == "GET":
        reject_unknown_query_params(request, {"page", "page_size", "revision_id"})
        queryset = ValidationReport.objects.filter(revision__review=review)
        revision_id = request.query_params.get("revision_id")
        if revision_id:
            # 过滤值必须属于当前 Review，否则 404
            if not ReviewRevision.objects.filter(pk=revision_id, review=review).exists():
                raise api_error("NOT_FOUND")
            queryset = queryset.filter(revision_id=revision_id)
        return _paginate(request, queryset.order_by("-created_at", "-id"), ValidationSerializer)

    key = parse_idempotency_key(idempotency_header_key(request))
    serializer = ValidationCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    accepted_job = create_validation(
        actor, review_id, {"revision_id": str(serializer.validated_data["revision_id"])}, key
    )
    return success(accepted_job.as_dto(), request=request, status_code=202)


@api_view(["GET"])
def validation_detail(request: Any, validation_id: Any) -> Response:
    """GET /validations/{validation_id}：执行中 result / structure_passed / blocks 为 null。"""
    actor = require_actor(request)
    validation = (
        ValidationReport.objects.select_related("revision", "revision__review")
        .filter(pk=validation_id, revision__review__project__owner=actor.user)
        .first()
    )
    if validation is None:
        raise api_error("NOT_FOUND")
    return success(ValidationSerializer(validation).data, request=request)


@api_view(["POST", "GET"])
def review_exports(request: Any, review_id: Any) -> Response:
    """POST 受理导出（202）与 GET 历史导出列表。"""
    actor = require_actor(request)
    review = _owned_review(actor, review_id)

    if request.method == "GET":
        reject_unknown_query_params(request, {"page", "page_size", "revision_id"})
        queryset = Export.objects.filter(revision__review=review)
        revision_id = request.query_params.get("revision_id")
        if revision_id:
            if not ReviewRevision.objects.filter(pk=revision_id, review=review).exists():
                raise api_error("NOT_FOUND")
            queryset = queryset.filter(revision_id=revision_id)
        return _paginate(request, queryset.order_by("-created_at", "-id"), ExportSerializer)

    key = parse_idempotency_key(idempotency_header_key(request))
    serializer = ExportCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payload = {
        "revision_id": str(serializer.validated_data["revision_id"]),
        "validation_id": str(serializer.validated_data["validation_id"]),
        "acknowledge_warnings": serializer.validated_data["acknowledge_warnings"],
    }
    accepted_job = create_export(actor, review_id, payload, key)
    return success(accepted_job.as_dto(), request=request, status_code=202)


@api_view(["GET"])
def export_detail(request: Any, export_id: Any) -> Response:
    """GET /exports/{export_id}。"""
    actor = require_actor(request)
    export = (
        Export.objects.select_related("revision", "revision__review", "validation_report")
        .filter(pk=export_id, revision__review__project__owner=actor.user)
        .first()
    )
    if export is None:
        raise api_error("NOT_FOUND")
    return success(ExportSerializer(export).data, request=request)


@api_view(["GET"])
def export_file(request: Any, export_id: Any) -> Response | FileResponse:
    """GET /exports/{export_id}/file：未 ready 为 409 RESULT_NOT_READY。"""
    actor = require_actor(request)
    export = (
        Export.objects.select_related("revision", "revision__review", "file_asset")
        .filter(pk=export_id, revision__review__project__owner=actor.user)
        .first()
    )
    if export is None:
        raise api_error("NOT_FOUND")
    if export.status != "ready" or export.file_asset is None:
        raise api_error("RESULT_NOT_READY")

    asset = export.file_asset
    handle = open_asset(asset)
    response = FileResponse(handle, content_type=asset.media_type or DOCX_MEDIA_TYPE)
    response["Content-Length"] = str(asset.size_bytes)
    # attachment：Word 草稿按下载处理；文件名 UTF-8 编码，不泄漏存储路径
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{asset.original_filename}"
    return response


urlpatterns = [
    path("runs/<uuid:run_id>/reviews", run_reviews_create, name="run-review-create"),
    path("projects/<uuid:project_id>/reviews", project_reviews, name="project-reviews"),
    path("reviews/<uuid:review_id>", review_detail, name="review-detail"),
    path("reviews/<uuid:review_id>/revisions", review_revisions, name="review-revisions"),
    path(
        "reviews/<uuid:review_id>/revisions/<uuid:revision_id>",
        review_revision_detail,
        name="review-revision-detail",
    ),
    path("reviews/<uuid:review_id>/validations", review_validations, name="review-validations"),
    path("validations/<uuid:validation_id>", validation_detail, name="validation-detail"),
    path("reviews/<uuid:review_id>/exports", review_exports, name="review-exports"),
    path("exports/<uuid:export_id>", export_detail, name="export-detail"),
    path("exports/<uuid:export_id>/file", export_file, name="export-file"),
]
