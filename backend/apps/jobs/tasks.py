"""jobs.tasks：Celery 薄适配与 kind → 领域服务的明确分发表。

职责（docs/mvp/01 第 4.7 节）：
- `execute_job(job_id)`：认领数据库执行资格后调用领域服务；
- `reconcile_jobs()`：扫描未投递 queued 与失去执行资格的任务并补投；
- `cleanup_expired_artifacts()`：只清理可确认未引用的临时产物与过期幂等记录。

jobs.services 不导入领域实现；**只有本文件**知道领域服务的名字。
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.http import set_log_context
from apps.jobs.models import (
    JOB_KIND_ANALYZE,
    JOB_KIND_EXPORT_DOCX,
    JOB_KIND_GENERATE_REVIEW,
    JOB_KIND_IMPORT_ARXIV,
    JOB_KIND_RETRIEVE,
    JOB_KIND_VALIDATE_REVIEW,
    JOB_STATUS_CANCELLING,
    Job,
)
from apps.jobs.services import (
    active_jobs_for_reconcile,
    claim_execution,
    dispatch,
    execution_heartbeat,
    fail_job,
    mark_cancelled,
)

logger = logging.getLogger("apps.jobs.tasks")

# kind → (模块, 函数)。函数签名统一为 (context, resource_id)。
DISPATCH_TABLE: dict[str, tuple[str, str]] = {
    JOB_KIND_ANALYZE: ("apps.analysis.services", "execute_run"),
    JOB_KIND_RETRIEVE: ("apps.retrieval.services", "execute_retrieval"),
    JOB_KIND_GENERATE_REVIEW: ("apps.reviews.services", "generate_review"),
    JOB_KIND_VALIDATE_REVIEW: ("apps.reviews.services", "validate_revision"),
    JOB_KIND_EXPORT_DOCX: ("apps.reviews.services", "export_revision"),
    JOB_KIND_IMPORT_ARXIV: ("apps.literature.services", "execute_import_arxiv"),
}


def resolve_handler(kind: str) -> Callable[..., Any]:
    """把 kind 解析为领域执行函数；表里没有就明确失败，不做猜测。"""
    entry = DISPATCH_TABLE.get(kind)
    if entry is None:
        raise LookupError(f"未注册的任务类型：{kind}")
    module_name, function_name = entry
    module = importlib.import_module(module_name)
    return getattr(module, function_name)


@shared_task(bind=True, ignore_result=True, name="apps.jobs.tasks.execute_job")
def execute_job(self: Any, job_id: str) -> str:
    """认领租约 → 调用固定分发器 → 统一终态处理。

    重新投递必须先领取数据库执行资格；旧 worker 即使恢复也不能越过 fencing 校验提交结果。
    """
    job_uuid = uuid.UUID(str(job_id))
    try:
        context = claim_execution(job_uuid)
    except Exception as exc:
        # 已有有效执行者、任务已取消或已终态：丢弃这条队列消息，不改业务状态
        logger.info("跳过任务 %s：%s", job_id, exc)
        return "skipped"

    set_log_context(job_id=str(context.job_id))
    job = Job.objects.filter(pk=job_uuid).first()
    if job is None:  # pragma: no cover - 竞态下任务被物理删除（MVP 无删除接口）
        return "missing"

    try:
        handler = resolve_handler(job.kind)
    except (LookupError, AttributeError, ImportError) as exc:
        fail_job(
            context,
            {
                "code": "INTERNAL_ERROR",
                "message": f"任务类型尚未实现：{job.kind}",
                "details": {},
                "retryable": False,
            },
        )
        logger.warning("无法分发任务 %s：%s", job_id, exc)
        return "unimplemented"

    try:
        with execution_heartbeat(context):
            handler(context, job.resource_id)
    except Exception as exc:  # noqa: BLE001 - 统一收敛为 Job.error
        logger.warning("任务执行失败 %s (%s)", job_id, type(exc).__name__)
        _record_failure(context, exc)
        return "failed"

    # 领域服务负责在自己的终态事务里提交结果；这里只处理“悄悄结束”的情况
    current = Job.objects.filter(pk=job_uuid).first()
    if current is not None and current.status == JOB_STATUS_CANCELLING:
        mark_cancelled(context, current)
    elif current is not None and current.status == "running":
        fail_job(
            context,
            {"code": "INTERNAL_ERROR", "message": "执行函数未提交终态", "details": {}, "retryable": True},
        )
        return "failed"
    return "done"


def _record_failure(context: Any, exc: Exception) -> None:
    """把异常收敛成 Job.error 的标准结构，绝不写入堆栈或供应商详情。"""
    from apps.common.http import ApiError

    if isinstance(exc, ApiError):
        payload = exc.as_error_body()
    else:
        payload = {
            "code": "INTERNAL_ERROR",
            "message": "任务执行失败，请查看服务日志",
            "details": {},
            "retryable": True,
        }
    try:
        fail_job(context, payload)
    except Exception:  # pragma: no cover - 租约已被取代时无法写终态
        logger.warning("无法写入任务终态 %s（租约可能已被取代）", context.job_id)


@shared_task(ignore_result=True, name="apps.jobs.tasks.reconcile_jobs")
def reconcile_jobs() -> dict[str, int]:
    """恢复扫描：补投 queued、回收过期租约。定时入口见 settings.CELERY_BEAT_SCHEDULE。"""
    stale_before = timezone.now() - timedelta(seconds=int(settings.JOB_HEARTBEAT_SECONDS) * 3)
    candidates = active_jobs_for_reconcile(stale_before=stale_before)
    redispatched = 0
    expired = 0
    for job in candidates:
        if job.status == "queued":
            dispatch(job)
            redispatched += 1
        else:
            # 先撤销旧执行资格，再终止本次尝试；不自动重做可能已付费的请求。
            from apps.llm.budget import abandon_calls

            with transaction.atomic():
                locked = Job.objects.select_for_update().get(pk=job.pk)
                if (
                    locked.status not in ("running", "cancelling")
                    or locked.lease_expires_at is None
                    or locked.lease_expires_at >= timezone.now()
                ):
                    continue
                abandon_calls(locked)
                locked.lease_token += 1
                locked.status = "cancelled" if locked.cancel_requested_at else "failed"
                locked.error = {
                    "code": "WORKER_LOST",
                    "message": "执行租约过期",
                    "details": {},
                    "retryable": True,
                }
                locked.finished_at = timezone.now()
                locked.lease_expires_at = None
                locked.save(
                    update_fields=[
                        "lease_token",
                        "status",
                        "error",
                        "finished_at",
                        "lease_expires_at",
                        "updated_at",
                    ]
                )
                expired += 1
    if redispatched or expired:
        logger.info("reconcile_jobs：补投 %s，回收过期租约 %s", redispatched, expired)
    return {"redispatched": redispatched, "expired": expired}


@shared_task(ignore_result=True, name="apps.jobs.tasks.cleanup_expired_artifacts")
def cleanup_expired_artifacts() -> dict[str, int]:
    """清理可确认未被引用的暂存产物与过期幂等记录；不触碰已登记的 FileAsset。"""
    from apps.common.idempotency import purge_expired
    from apps.common.storage import cleanup_staging

    staged = cleanup_staging()
    purged = purge_expired()
    return {"staged_removed": staged, "idempotency_purged": purged}
