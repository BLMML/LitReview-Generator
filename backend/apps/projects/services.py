"""projects.services：项目用例入口。

职责（docs/mvp/01 第 4.1 节）：
- 创建项目；
- 编辑时锁 Project、检查空闲与 expected_version；
- 研究问题变化递增 corpus_revision。

事务边界遵循 docs/mvp/01 第 7.2 节：短事务内完成校验与版本递增，不跨外部请求持锁。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import transaction

from apps.common.auth import ActorContext
from apps.common.http import api_error
from apps.common.locking import lock_project, require_project_idle
from apps.projects.models import Project


def _ensure_usage_budget(project: Project) -> None:
    """项目级预算行随项目创建；限额来自部署配置，不在项目 HTTP 输入里开放。

    用 apps.get_model 延迟取模型：projects 不硬依赖 llm 模块。
    """
    usage_budget = apps.get_model("llm", "UsageBudget")
    usage_budget.objects.get_or_create(
        project=project,
        defaults={"limit_usd": settings.PROJECT_DEFAULT_BUDGET_USD},
    )


@transaction.atomic
def create_project(actor: ActorContext, payload: Mapping[str, Any]) -> Project:
    """POST /projects 的用例入口；owner 永远是请求者，不接受客户端指定。"""
    project = Project.objects.create(
        owner=actor.user,
        title=payload["title"],
        research_question=payload["research_question"],
        output_language=payload.get("output_language") or settings.OUTPUT_LANGUAGE_DEFAULT,
    )
    _ensure_usage_budget(project)
    return project


def update_project(
    actor: ActorContext,
    project_id: Any,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Project:
    """PATCH /projects/{id}。

    - 先锁 Project 行，再检查空闲与 expected_version；
    - title 变化只增加 version；
    - 研究问题实际变化时同时增加 corpus_revision。
    """
    if not changes:
        raise api_error(
            "VALIDATION_ERROR",
            "至少需要提供 title 或 research_question 之一",
            {"fields": {"title": ["未提供可修改字段"], "research_question": ["未提供可修改字段"]}},
        )

    with transaction.atomic():
        project = lock_project(project_id, owner=actor.user)
        require_project_idle(project)

        if int(project.version) != int(expected_version):
            raise api_error("VERSION_CONFLICT", details={"current_version": project.version})

        dirty_fields: list[str] = []

        new_title = changes.get("title")
        if new_title is not None and new_title != project.title:
            project.title = new_title
            dirty_fields.append("title")

        new_question = changes.get("research_question")
        if new_question is not None and new_question != project.research_question:
            project.research_question = new_question
            # 研究问题属于语料快照的一部分：改字让既有 Run 过时
            project.corpus_revision += 1
            dirty_fields.append("research_question")

        if not dirty_fields:
            # 提交内容与现值相同：不递增版本，避免把无变化保存伪装成编辑
            return project

        project.version += 1
        project.save(update_fields=[*dirty_fields, "version", "corpus_revision", "updated_at"])
        return project
