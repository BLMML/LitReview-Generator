"""projects.api：项目列表 / 创建 / 详情 / PATCH 的 DTO 适配。

字段以 docs/mvp/03 第 2.1 节与 contracts/openapi.json 的 Project /
ProjectCreate / ProjectPatch 为准。HTTP 字段保持 snake_case。
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response

from apps.common.auth import require_actor
from apps.common.http import (
    EnvelopePagination,
    StrictSerializer,
    reject_unknown_query_params,
    success,
)
from apps.projects.selectors import (
    annotate_runtime_pointers,
    get_owned_project,
    list_owned_projects,
)
from apps.projects.services import create_project, update_project


class ProjectCreateSerializer(StrictSerializer):
    """ProjectCreate 契约：title 1—120、research_question 5—2,000。"""

    title = serializers.CharField(
        min_length=1, max_length=settings.MVP_LIMITS["PROJECT_TITLE_MAX"]
    )
    research_question = serializers.CharField(
        min_length=settings.MVP_LIMITS["RESEARCH_QUESTION_MIN"],
        max_length=settings.MVP_LIMITS["RESEARCH_QUESTION_MAX"],
    )
    output_language = serializers.ChoiceField(
        choices=list(settings.OUTPUT_LANGUAGES_ALLOWED),
        default=settings.OUTPUT_LANGUAGE_DEFAULT,
    )


class ProjectPatchSerializer(StrictSerializer):
    """ProjectPatch 契约：expected_version 必填，title / research_question 至少一项。"""

    expected_version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(
        min_length=1,
        max_length=settings.MVP_LIMITS["PROJECT_TITLE_MAX"],
        required=False,
    )
    research_question = serializers.CharField(
        min_length=settings.MVP_LIMITS["RESEARCH_QUESTION_MIN"],
        max_length=settings.MVP_LIMITS["RESEARCH_QUESTION_MAX"],
        required=False,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "title" not in attrs and "research_question" not in attrs:
            raise serializers.ValidationError(
                {"title": ["title 与 research_question 至少提供一项"]}
            )
        return attrs


class ProjectSerializer(serializers.Serializer):
    """ProjectDTO：10 个必含字段。"""

    id = serializers.UUIDField()
    title = serializers.CharField()
    research_question = serializers.CharField()
    output_language = serializers.CharField()
    version = serializers.IntegerField()
    corpus_revision = serializers.IntegerField()
    latest_run_id = serializers.SerializerMethodField()
    active_job_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def get_latest_run_id(self, obj: Any) -> str | None:
        value = getattr(obj, "latest_run_id", None)
        return str(value) if value else None

    def get_active_job_id(self, obj: Any) -> str | None:
        value = getattr(obj, "active_job_id", None)
        return str(value) if value else None


def _dto(project_id: Any) -> dict[str, Any]:
    """重新读取带运行时指针的项目，保证 create / patch 的响应与详情同源。"""
    from apps.projects.models import Project

    annotated = annotate_runtime_pointers(Project.objects.filter(pk=project_id)).first()
    return ProjectSerializer(annotated).data


@api_view(["GET", "POST"])
def projects_collection(request: Any) -> Response:
    """GET /projects（分页）与 POST /projects（201）。"""
    actor = require_actor(request)

    if request.method == "GET":
        reject_unknown_query_params(request, {"page", "page_size"})
        queryset = annotate_runtime_pointers(list_owned_projects(actor))
        paginator = EnvelopePagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(ProjectSerializer(page, many=True).data)

    serializer = ProjectCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    project = create_project(actor, serializer.validated_data)
    return success(_dto(project.pk), request=request, status_code=201)


@api_view(["GET", "PATCH"])
def project_detail(request: Any, project_id: Any) -> Response:
    """GET /projects/{id} 与 PATCH /projects/{id}。"""
    actor = require_actor(request)

    if request.method == "GET":
        project = get_owned_project(actor, project_id)
        return success(ProjectSerializer(project).data, request=request)

    serializer = ProjectPatchSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    validated = serializer.validated_data
    changes = {
        key: validated[key] for key in ("title", "research_question") if key in validated
    }
    update_project(actor, project_id, validated["expected_version"], changes)
    return success(_dto(project_id), request=request)


urlpatterns = [
    path("projects", projects_collection, name="project-list-create"),
    path("projects/<uuid:project_id>", project_detail, name="project-detail"),
]
