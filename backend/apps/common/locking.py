"""common.locking：Project 行锁与统一忙状态判断。

职责（docs/mvp/01 第 3.3 节）：
- `lock_project(project_id)` 必须在事务内调用；
- `require_project_idle(project)`；
- 执行尝试提交前检查租约 / fencing 标识。

本模块不包含任何模块业务；通过 apps.get_model 延迟取模型，避免 common 反向导入领域服务。
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from apps.common.http import api_error

# docs/mvp/00：每项目同时最多一个活跃后台任务
ACTIVE_JOB_STATUSES = ("queued", "running", "cancelling")


def _project_model() -> Any:
    return apps.get_model("projects", "Project")


def _job_model() -> Any:
    return apps.get_model("jobs", "Job")


def lock_project(project_id: Any, *, owner: Any | None = None, include_deleted: bool = False) -> Any:
    """在事务内锁定 Project 行并返回它。

    - 必须在 `transaction.atomic()` 内调用，否则并发受理无法收敛；
    - 不存在或不属于 owner 一律 NOT_FOUND，不泄露资源归属。
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("lock_project 必须在事务内调用")

    queryset = _project_model().objects.select_for_update()
    if owner is not None:
        queryset = queryset.filter(owner=owner)
        if not include_deleted:
            queryset = queryset.filter(deleted_at__isnull=True)
    # 无 owner 的内部结算仍可锁住已删除项目，避免丢失未结算账本。
    # include_deleted 仅供归属校验后的重复删除使用。
    project = queryset.filter(pk=project_id).first()
    if project is None:
        raise api_error("NOT_FOUND")
    return project


def active_job_of(project: Any) -> Any | None:
    """项目当前活跃任务；数据库是唯一真实状态来源。"""
    return (
        _job_model()
        .objects.filter(project=project, status__in=ACTIVE_JOB_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )


def require_project_idle(project: Any) -> None:
    """活跃任务存在时拒绝新的互斥写入（409 PROJECT_BUSY）。

    调用方应在 Project 行锁内执行本检查，并在错误 details 中给出 active_job_id。
    """
    job = active_job_of(project)
    if job is not None:
        raise api_error("PROJECT_BUSY", details={"active_job_id": str(job.id)})


def check_execution_lease(job: Any, lease_token: int, *, allow_cancelling: bool = False) -> None:
    """worker 提交阶段结果前的 fencing 检查。

    仅比较时间不够：必须匹配有效的 lease_token、当前状态与未取消条件。
    旧 worker 即使恢复，也不能越过本检查写入业务状态。
    """
    allowed_statuses = {"running", "cancelling"} if allow_cancelling else {"running"}
    if job.status not in allowed_statuses:
        raise api_error("INVALID_JOB_STATE", "任务已不在可写入状态", retryable=False)
    if job.cancel_requested_at is not None and not allow_cancelling:
        raise api_error("INVALID_JOB_STATE", "任务已被请求取消")
    if int(job.lease_token) != int(lease_token):
        raise api_error("INVALID_JOB_STATE", "执行租约已被新的尝试取代")
    if job.lease_expires_at is None or job.lease_expires_at <= timezone.now():
        raise api_error("INVALID_JOB_STATE", "执行租约已过期")
