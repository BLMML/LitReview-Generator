"""retrieval.api：启动检索与查询结果。

职责（docs/mvp/01 第 4.5 节、docs/mvp/03 第 2.4 节）：
- `create_retrieval`、`get_retrieval`；
- 拒绝对 stale 或未 ready 的 Run 开始新检索；
- 前端不接触索引文件。

**骨架状态**：查询已实现；创建检索调用尚未实现的服务。
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.analysis.models import AnalysisRun
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
from apps.retrieval.models import RetrievalResult
from apps.retrieval.services import create_retrieval


class RetrievalCreateSerializer(StrictSerializer):
    """query 2—2,000；candidate_limit 5—50；top_k 1—10 且不超过 candidate_limit。"""

    query = serializers.CharField(
        min_length=settings.MVP_LIMITS["RETRIEVAL_QUERY_MIN"],
        max_length=settings.MVP_LIMITS["RETRIEVAL_QUERY_MAX"],
    )
    candidate_limit = serializers.IntegerField(
        min_value=settings.MVP_LIMITS["RETRIEVAL_CANDIDATE_LIMIT_MIN"],
        max_value=settings.MVP_LIMITS["RETRIEVAL_CANDIDATE_LIMIT_MAX"],
        default=settings.MVP_LIMITS["RETRIEVAL_CANDIDATE_LIMIT_DEFAULT"],
    )
    top_k = serializers.IntegerField(
        min_value=settings.MVP_LIMITS["RETRIEVAL_TOP_K_MIN"],
        max_value=settings.MVP_LIMITS["RETRIEVAL_TOP_K_MAX"],
        default=settings.MVP_LIMITS["RETRIEVAL_TOP_K_DEFAULT"],
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["top_k"] > attrs["candidate_limit"]:
            raise serializers.ValidationError({"top_k": ["top_k 不得超过 candidate_limit"]})
        return attrs


class RetrievalSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    run_id = serializers.UUIDField(source="run.id")
    query = serializers.CharField()
    analysis_revision = serializers.IntegerField()
    candidate_limit = serializers.IntegerField()
    top_k = serializers.IntegerField()
    expanded_queries = serializers.JSONField()
    results = serializers.JSONField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    job_id = serializers.SerializerMethodField()

    def get_job_id(self, obj: RetrievalResult) -> str | None:
        from apps.jobs.models import Job

        job = (
            Job.objects.filter(resource_type="retrieval_result", resource_id=obj.id)
            .order_by("-attempt")
            .first()
        )
        return str(job.id) if job else None


@api_view(["POST"])
def run_retrievals_create(request: Any, run_id: Any) -> Response:
    """POST /runs/{run_id}/retrievals（202，幂等标记 K）。"""
    actor = require_actor(request)
    key = parse_idempotency_key(idempotency_header_key(request))
    serializer = RetrievalCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    run = AnalysisRun.objects.select_related("project").filter(pk=run_id).first()
    if run is None:
        raise api_error("NOT_FOUND")
    accepted_job = create_retrieval(actor, run_id, serializer.validated_data, key)
    return success(accepted_job.as_dto(), request=request, status_code=202)


@api_view(["GET"])
def run_retrievals_list(request: Any, run_id: Any) -> Response:
    """GET /runs/{run_id}/retrievals：用于恢复历史查询。"""
    actor = require_actor(request)
    reject_unknown_query_params(request, {"page", "page_size"})
    queryset = RetrievalResult.objects.select_related("run", "run__project").filter(
        run_id=run_id, run__project__owner=actor.user
    )
    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset.order_by("-created_at", "-id"), request)
    return paginator.get_paginated_response(RetrievalSerializer(page, many=True).data)


@api_view(["GET"])
def retrieval_detail(request: Any, retrieval_id: Any) -> Response:
    """GET /retrievals/{retrieval_id}：执行完成前 results 为 null。"""
    actor = require_actor(request)
    retrieval = (
        RetrievalResult.objects.select_related("run", "run__project")
        .filter(pk=retrieval_id, run__project__owner=actor.user)
        .first()
    )
    if retrieval is None:
        raise api_error("NOT_FOUND")
    return success(RetrievalSerializer(retrieval).data, request=request)


urlpatterns = [
    path("runs/<uuid:run_id>/retrievals", run_retrievals_create, name="run-retrieval-create"),
    path("retrievals/<uuid:retrieval_id>", retrieval_detail, name="retrieval-detail"),
]
