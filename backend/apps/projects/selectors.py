"""projects.selectors：项目读取入口。

职责（docs/mvp/01 第 4.1 节）：
- `list_owned_projects(actor, pagination)`；
- `get_owned_project(actor, project_id)`；
- 越权和不存在均表现为 404；
- 提供 active_job_id 与 Run 是否过时所需信息。

跨 app 的读取用 apps.get_model 延迟取模型，避免模块级导入环。
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db.models import OuterRef, QuerySet, Subquery

from apps.common.auth import ActorContext
from apps.common.http import api_error
from apps.common.locking import ACTIVE_JOB_STATUSES
from apps.projects.models import Project


def list_owned_projects(actor: ActorContext) -> QuerySet[Project]:
    """只返回当前用户拥有的项目；排序稳定（created_at DESC, id DESC）。"""
    return Project.objects.filter(owner=actor.user).order_by("-created_at", "-id")


def annotate_runtime_pointers(queryset: QuerySet[Project]) -> QuerySet[Project]:
    """补上 latest_run_id 与 active_job_id。

    两者都是计算值：最近创建的 Run（不暗指已成功）与唯一活跃 Job。
    不维护重复指针，也就不存在指针与实体不一致的问题。
    """
    analysis_run = apps.get_model("analysis", "AnalysisRun")
    job = apps.get_model("jobs", "Job")

    latest_run = analysis_run.objects.filter(project=OuterRef("pk")).order_by("-created_at", "-id")
    active_job = (
        job.objects.filter(project=OuterRef("pk"), status__in=ACTIVE_JOB_STATUSES)
        .order_by("-created_at", "-id")
    )
    return queryset.annotate(
        latest_run_id=Subquery(latest_run.values("id")[:1]),
        active_job_id=Subquery(active_job.values("id")[:1]),
    )


def get_owned_project(actor: ActorContext, project_id: Any) -> Project:
    """按所有者定位项目；不存在或无权访问一律 404。"""
    project = (
        annotate_runtime_pointers(Project.objects.all())
        .filter(pk=project_id, owner=actor.user)
        .first()
    )
    if project is None:
        raise api_error("NOT_FOUND")
    return project


def is_run_stale(run: Any) -> bool:
    """Run 是否过时：由冻结的 corpus_revision 与项目当前值比较得到。"""
    return int(run.corpus_revision) != int(run.project.corpus_revision)
