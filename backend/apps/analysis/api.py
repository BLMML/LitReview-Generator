"""analysis.api：Run、单篇分析、主题 / 争议、证据详情、争议 PATCH。

职责（docs/mvp/01 第 4.4 节、docs/mvp/03 第 2.3 节）：
- 查询仅返回所属项目资源；
- 证据入口可委托 parsing 的批量读取，但 HTTP 路由仍按 docs/mvp/00；
- 输出账本和 stale 提示。

**骨架状态**：读接口已实现；创建 Run、争议复核与证据读取调用尚未实现的服务。
"""

from __future__ import annotations

from typing import Any

from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.analysis.models import AnalysisRun, Dispute, PaperAnalysis, RunPaper, Topic
from apps.analysis.services import create_run, review_dispute
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
from apps.jobs.models import JOB_KIND_ANALYZE, Job


class RunPaperSerializer(serializers.Serializer):
    paper_id = serializers.UUIDField(source="paper.id")
    title = serializers.CharField()
    authors = serializers.JSONField()
    year = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
    parsed_document_id = serializers.SerializerMethodField()
    page_count = serializers.IntegerField()
    error = serializers.JSONField()

    def get_parsed_document_id(self, obj: RunPaper) -> str | None:
        return str(obj.parsed_document_id) if obj.parsed_document_id else None


class RunSerializer(serializers.Serializer):
    """RunDTO：冻结的 research_question / output_language，而不是当前 Project 值。"""

    id = serializers.UUIDField()
    project_id = serializers.UUIDField(source="project.id")
    research_question = serializers.CharField()
    output_language = serializers.CharField()
    status = serializers.CharField()
    job_id = serializers.SerializerMethodField()
    corpus_revision = serializers.IntegerField()
    analysis_revision = serializers.IntegerField()
    is_stale = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    papers = RunPaperSerializer(source="run_papers", many=True)
    stats = serializers.SerializerMethodField()
    error = serializers.JSONField()

    def get_job_id(self, obj: AnalysisRun) -> str | None:
        job = (
            Job.objects.filter(
                resource_type="analysis_run", resource_id=obj.id, kind=JOB_KIND_ANALYZE
            )
            .order_by("-attempt")
            .first()
        )
        return str(job.id) if job else None

    def get_is_stale(self, obj: AnalysisRun) -> bool:
        return int(obj.corpus_revision) != int(obj.project.corpus_revision)

    def get_stats(self, obj: AnalysisRun) -> dict[str, Any]:
        """统计只基于纳入语料快照，不读取当前 Paper 年份。"""
        year_counts: dict[str, int] = {}
        unknown_year = 0
        included = ready = failed = 0
        for run_paper in obj.run_papers.all():
            included += 1
            if run_paper.status == "ready":
                ready += 1
            elif run_paper.status == "failed":
                failed += 1
            if run_paper.year is None:
                unknown_year += 1
            else:
                key = str(run_paper.year)
                year_counts[key] = year_counts.get(key, 0) + 1
        return {
            "included_count": included,
            "ready_count": ready,
            "failed_count": failed,
            "year_counts": year_counts,
            "unknown_year_count": unknown_year,
        }


class TopicSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    run_id = serializers.UUIDField(source="run.id")
    name = serializers.CharField()
    description = serializers.CharField()
    paper_ids = serializers.JSONField()
    evidence_ids = serializers.JSONField()
    created_at = serializers.DateTimeField()


class DisputeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    run_id = serializers.UUIDField(source="run.id")
    left_paper_id = serializers.UUIDField(source="left_paper.id")
    right_paper_id = serializers.UUIDField(source="right_paper.id")
    left_claim = serializers.JSONField()
    right_claim = serializers.JSONField()
    conditions = serializers.JSONField()
    classification = serializers.CharField()
    reason = serializers.CharField()
    review_status = serializers.CharField()
    review_note = serializers.CharField()
    version = serializers.IntegerField()
    reviewed_by = serializers.SerializerMethodField()
    reviewed_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def get_reviewed_by(self, obj: Dispute) -> str | None:
        return str(obj.reviewed_by_id) if obj.reviewed_by_id else None


class PaperAnalysisSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    run_id = serializers.SerializerMethodField()
    paper_id = serializers.SerializerMethodField()
    payload = serializers.JSONField()
    generation_snapshot = serializers.JSONField()
    created_at = serializers.DateTimeField()

    def get_run_id(self, obj: PaperAnalysis) -> str:
        return str(obj.run_paper.run_id)

    def get_paper_id(self, obj: PaperAnalysis) -> str:
        return str(obj.run_paper.paper_id)


class DisputePatchSerializer(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    review_status = serializers.ChoiceField(choices=["pending", "confirmed", "rejected"])
    review_note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


def _owned_run(actor: Any, run_id: Any) -> AnalysisRun:
    run = (
        AnalysisRun.objects.select_related("project")
        .prefetch_related("run_papers")
        .filter(pk=run_id, project__owner=actor.user)
        .first()
    )
    if run is None:
        raise api_error("NOT_FOUND")
    return run


def _require_ready(run: AnalysisRun) -> None:
    if run.status != "ready":
        raise api_error("RESULT_NOT_READY")


@api_view(["GET", "POST"])
def project_runs(request: Any, project_id: Any) -> Response:
    """GET /projects/{project_id}/runs 与 POST（202，幂等标记 K）。"""
    actor = require_actor(request)
    if request.method == "GET":
        reject_unknown_query_params(request, {"page", "page_size"})
        queryset = (
            AnalysisRun.objects.select_related("project")
            .prefetch_related("run_papers")
            .filter(project_id=project_id, project__owner=actor.user)
        )
        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(queryset.order_by("-created_at", "-id"), request)
        return paginator.get_paginated_response(RunSerializer(page, many=True).data)

    key = parse_idempotency_key(idempotency_header_key(request))
    accepted_job = create_run(actor, project_id, request.data or {}, key)
    return success(accepted_job.as_dto(), request=request, status_code=202)


@api_view(["GET"])
def run_detail(request: Any, run_id: Any) -> Response:
    """GET /runs/{run_id}：旧 Run 可读，is_stale 由后端计算。"""
    actor = require_actor(request)
    return success(RunSerializer(_owned_run(actor, run_id)).data, request=request)


@api_view(["GET"])
def paper_analysis(request: Any, run_id: Any, paper_id: Any) -> Response:
    """GET /runs/{run_id}/papers/{paper_id}/analysis。"""
    actor = require_actor(request)
    _owned_run(actor, run_id)
    analysis = (
        PaperAnalysis.objects.select_related("run_paper")
        .filter(run_paper__run_id=run_id, run_paper__paper_id=paper_id)
        .first()
    )
    if analysis is None:
        raise api_error("RESULT_NOT_READY")
    return success(PaperAnalysisSerializer(analysis).data, request=request)


@api_view(["GET"])
def run_topics(request: Any, run_id: Any) -> Response:
    """GET /runs/{run_id}/topics：主题按 created_at ASC, id ASC 排序。"""
    actor = require_actor(request)
    run = _owned_run(actor, run_id)
    _require_ready(run)
    reject_unknown_query_params(request, {"page", "page_size"})
    queryset = Topic.objects.filter(run=run).order_by("created_at", "id")
    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(TopicSerializer(page, many=True).data)


@api_view(["GET"])
def run_disputes(request: Any, run_id: Any) -> Response:
    """GET /runs/{run_id}/disputes：争议按 created_at ASC, id ASC 排序。"""
    actor = require_actor(request)
    run = _owned_run(actor, run_id)
    _require_ready(run)
    reject_unknown_query_params(request, {"page", "page_size", "review_status"})
    queryset = Dispute.objects.filter(run=run)
    review_status = request.query_params.get("review_status")
    if review_status:
        queryset = queryset.filter(review_status=review_status)
    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset.order_by("created_at", "id"), request)
    return paginator.get_paginated_response(DisputeSerializer(page, many=True).data)


@api_view(["PATCH"])
def dispute_detail(request: Any, dispute_id: Any) -> Response:
    """PATCH /disputes/{dispute_id}：只允许人工字段，自动分类不可覆盖。"""
    actor = require_actor(request)
    serializer = DisputePatchSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    validated = serializer.validated_data
    changes = {
        key: validated[key] for key in ("review_status", "review_note") if key in validated
    }
    dispute = review_dispute(actor, dispute_id, validated["expected_version"], changes)
    return success(DisputeSerializer(dispute).data, request=request)


@api_view(["GET"])
def run_evidence_detail(request: Any, run_id: Any, evidence_id: Any) -> Response:
    """GET /runs/{run_id}/evidence/{evidence_id}。"""
    actor = require_actor(request)
    _owned_run(actor, run_id)
    from apps.parsing.evidence import get_evidence_batch

    items = get_evidence_batch(run_id, [evidence_id])
    if not items:
        raise api_error("NOT_FOUND")
    return success(items[0], request=request)


@api_view(["GET"])
def paper_evidence_list(request: Any, run_id: Any, paper_id: Any) -> Response:
    """GET /runs/{run_id}/papers/{paper_id}/evidence：按 page_number / ordinal / id 升序。

    页码超过该论文真实页数返回 VALIDATION_ERROR；合法但无片段返回空数组。
    """
    actor = require_actor(request)
    run = _owned_run(actor, run_id)
    reject_unknown_query_params(request, {"page", "page_size", "page_number"})
    run_paper = run.run_papers.filter(paper_id=paper_id).first()
    if run_paper is None:
        raise api_error("NOT_FOUND")
    if run_paper.parsed_document_id is None:
        raise api_error("RESULT_NOT_READY")

    page_number = request.query_params.get("page_number")
    if page_number is not None:
        try:
            page_number = int(page_number)
        except ValueError as exc:
            raise api_error("VALIDATION_ERROR", "page_number 必须是整数") from exc
        if not 1 <= page_number <= run_paper.page_count:
            raise api_error(
                "VALIDATION_ERROR",
                "page_number 超出该论文真实页数",
                {"page_number": [f"必须在 1—{run_paper.page_count} 之间"]},
            )

    from apps.parsing.evidence import list_evidence_for_run_paper

    queryset = list_evidence_for_run_paper(run_paper, page_number=page_number)
    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator.get_paginated_response(page)


urlpatterns = [
    path("projects/<uuid:project_id>/runs", project_runs, name="project-runs"),
    path("runs/<uuid:run_id>", run_detail, name="run-detail"),
    path(
        "runs/<uuid:run_id>/papers/<uuid:paper_id>/analysis",
        paper_analysis,
        name="run-paper-analysis",
    ),
    path("runs/<uuid:run_id>/topics", run_topics, name="run-topics"),
    path("runs/<uuid:run_id>/disputes", run_disputes, name="run-disputes"),
    path(
        "runs/<uuid:run_id>/papers/<uuid:paper_id>/evidence",
        paper_evidence_list,
        name="run-paper-evidence",
    ),
    path(
        "runs/<uuid:run_id>/evidence/<uuid:evidence_id>",
        run_evidence_detail,
        name="run-evidence-detail",
    ),
    path("disputes/<uuid:dispute_id>", dispute_detail, name="dispute-detail"),
]
