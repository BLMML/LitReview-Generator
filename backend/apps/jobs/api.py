"""jobs.api：Job 查询、取消和重试。

职责（docs/mvp/01 第 4.7 节、docs/mvp/03 第 2.6 节）：
- `get_job`、`cancel_job`、`retry_job`；
- 输出阶段、预算 / 用量、逐项导入结果、错误原因与 retryable；
- 不会直接触发领域代码同步执行。

usage 聚合 ModelCall 账本，而不是在多处各自累加。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.apps import apps
from django.db.models import Sum
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.common.auth import require_actor
from apps.common.http import (
    EnvelopePagination,
    accepted,
    idempotency_header_key,
    reject_unknown_query_params,
    success,
)
from apps.common.idempotency import parse_idempotency_key
from apps.jobs.models import JOB_KIND_RESOURCE_TYPE, Job
from apps.jobs.services import cancel_job, retry_job
from apps.projects.selectors import get_owned_project

ZERO = Decimal("0")


def job_usage(job_id: Any) -> dict[str, Any]:
    """{input_tokens, output_tokens, estimated_cost_usd, unsettled_call_count}。

    未知用量不写 0 伪装成已知；未结算调用保留在 unsettled_call_count。
    """
    model_call = apps.get_model("llm", "ModelCall")
    calls = model_call.objects.filter(job_id=job_id)
    totals = calls.aggregate(
        input_tokens=Sum("input_tokens"),
        output_tokens=Sum("output_tokens"),
        estimated_cost=Sum("estimated_cost_usd"),
    )
    unsettled = calls.filter(outcome__in=("reserved", "in_flight", "unsettled")).count()
    estimated = totals["estimated_cost"]
    return {
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        # 金额统一输出字符串十进制
        "estimated_cost_usd": str(estimated if estimated is not None else ZERO),
        "unsettled_call_count": unsettled,
    }


class JobSerializer(serializers.Serializer):
    """JobDTO：字段与 contracts/openapi.json 的 Job 对应。"""

    id = serializers.UUIDField()
    project_id = serializers.UUIDField(source="project.id")
    kind = serializers.CharField()
    status = serializers.CharField()
    stage = serializers.CharField()
    progress = serializers.JSONField()
    resource_type = serializers.CharField()
    resource_id = serializers.UUIDField()
    retry_of_job_id = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    error = serializers.JSONField()
    result = serializers.JSONField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField()

    def get_retry_of_job_id(self, obj: Job) -> str | None:
        return str(obj.retry_of_job_id) if obj.retry_of_job_id else None

    def get_usage(self, obj: Job) -> dict[str, Any]:
        return job_usage(obj.id)


@api_view(["GET"])
def get_job(request: Any, job_id: Any) -> Response:
    """GET /jobs/{job_id}。"""
    actor = require_actor(request)
    job = (
        Job.objects.select_related("project")
        .filter(pk=job_id, project__owner=actor.user, project__deleted_at__isnull=True)
        .first()
    )
    if job is None:
        from apps.common.http import api_error

        raise api_error("NOT_FOUND")
    return success(JobSerializer(job).data, request=request)


@api_view(["POST"])
def job_cancel(request: Any, job_id: Any) -> Response:
    """POST /jobs/{job_id}/cancel：自然幂等，无 Idempotency-Key。"""
    actor = require_actor(request)
    job = cancel_job(actor, job_id)
    return success(JobSerializer(job).data, request=request)


@api_view(["POST"])
def job_retry(request: Any, job_id: Any) -> Response:
    """POST /jobs/{job_id}/retry：返回 202 与新 Job ID，resource_id 保持不变。"""
    actor = require_actor(request)
    key = parse_idempotency_key(idempotency_header_key(request))
    accepted_job = retry_job(actor, job_id, idempotency=key)
    return accepted(accepted_job.as_dto(), request=request)


@api_view(["GET"])
def project_jobs(request: Any, project_id: Any) -> Response:
    """GET /projects/{project_id}/jobs，含已完成 / 失败导入任务。"""
    actor = require_actor(request)
    reject_unknown_query_params(request, {"page", "page_size", "kind"})
    get_owned_project(actor, project_id)
    kind = request.query_params.get("kind")
    if kind is not None and kind not in JOB_KIND_RESOURCE_TYPE:
        from apps.common.http import api_error

        raise api_error("VALIDATION_ERROR", "不支持的 kind 过滤值", {"kind": ["取值不在枚举内"]})

    queryset = Job.objects.select_related("project").filter(
        project__owner=actor.user, project__deleted_at__isnull=True
    )
    queryset = queryset.filter(project_id=project_id)
    if kind:
        queryset = queryset.filter(kind=kind)

    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset.order_by("-created_at", "-id"), request)
    return paginator.get_paginated_response(JobSerializer(page, many=True).data)


urlpatterns = [
    path("jobs/<uuid:job_id>", get_job, name="job-detail"),
    path("jobs/<uuid:job_id>/cancel", job_cancel, name="job-cancel"),
    path("jobs/<uuid:job_id>/retry", job_retry, name="job-retry"),
    path("projects/<uuid:project_id>/jobs", project_jobs, name="project-jobs"),
]
