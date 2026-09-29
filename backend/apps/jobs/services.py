"""jobs.services：统一受理、占用项目、领取执行、续租、进度提交、取消、终态提交与新尝试。

职责（docs/mvp/01 第 4.7 节、docs/mvp/04 第 2 节）：
- `accept_job(actor, project, kind, payload, idempotency) → AcceptedJob`
- `claim_execution(job_id) → ExecutionContext`
- `record_progress(context, ...)` / `heartbeat(context)`
- `cancel_job(actor, job_id)` / `retry_job(actor, job_id, key) → AcceptedJob`
- `complete_job(context, result)` / `fail_job(context, error)`

边界：本模块不知道任何领域实现，也不导入 analysis / reviews。
领域入口只由 jobs.tasks 的分发表调用；各领域服务可以反向调用这里的进度与终态接口。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.common.auth import ActorContext
from apps.common.http import api_error
from apps.common.idempotency import Claim, claim_or_replay, record_claim, request_fingerprint
from apps.common.locking import check_execution_lease, lock_project, require_project_idle
from apps.jobs.models import (
    ACTIVE_JOB_STATUSES,
    JOB_KIND_IMPORT_ARXIV,
    JOB_KIND_RESOURCE_TYPE,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_CANCELLING,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    Job,
    default_progress,
)


@dataclass(frozen=True)
class AcceptedJob:
    """202 响应体：job_id + resource_type + resource_id。"""

    job_id: uuid.UUID
    resource_type: str
    resource_id: uuid.UUID
    replayed: bool = False

    def as_dto(self) -> dict[str, str]:
        return {
            "job_id": str(self.job_id),
            "resource_type": self.resource_type,
            "resource_id": str(self.resource_id),
        }


@dataclass(frozen=True)
class ExecutionContext:
    """worker 阶段的执行上下文。

    至少含 job_id、project_id、run_id（可空）、attempt、lease_token、deadline、cancel_checker。
    它不是 HTTP 实体，也不携带 Django request。
    """

    job_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None
    attempt: int
    lease_token: int
    deadline: datetime
    cancel_checker: Callable[[], bool]

    def ensure_not_cancelled(self) -> None:
        if self.cancel_checker():
            raise api_error("INVALID_JOB_STATE", "任务已被请求取消")
        job = Job.objects.filter(pk=self.job_id, project_id=self.project_id).first()
        if job is None:
            raise api_error("INVALID_JOB_STATE")
        check_execution_lease(job, self.lease_token)


def payload_fingerprint(frozen_input: Mapping[str, Any]) -> str:
    """冻结输入的规范化摘要；重试据此复用同一份输入。"""
    material = json.dumps(frozen_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ── 受理 ──────────────────────────────────────────────────────
def accept_job(
    actor: ActorContext,
    project: Any,
    kind: str,
    payload: Mapping[str, Any],
    idempotency: Any | None = None,
) -> AcceptedJob:
    """统一受理入口。

    `payload` 至少包含 `resource_id`（预建的业务资源）与 `frozen_input`。
    `import_arxiv` 例外：契约规定其业务资源 ID 固定为首次受理的 Job ID，
    因此该 kind 传 `resource_id=None`，由本函数生成。

    必须在 Project 行锁内完成：幂等查找 → 空闲校验 → 冻结输入 → 创建 Job。
    传入 key 或 Claim 时由本服务在同一事务写幂等记录；提交后自动投递队列。
    领域调用方应在外层同一事务内预建资源，不得重复记录幂等响应或重复 dispatch。
    """
    if kind not in JOB_KIND_RESOURCE_TYPE:
        raise api_error("VALIDATION_ERROR", "不支持的任务类型")

    resource_type = JOB_KIND_RESOURCE_TYPE[kind]
    frozen_input = dict(payload.get("frozen_input") or {})
    requested_resource_id = payload.get("resource_id")
    if kind != JOB_KIND_IMPORT_ARXIV and requested_resource_id is None:
        raise api_error("VALIDATION_ERROR", "缺少业务资源 ID")

    with transaction.atomic():
        locked = lock_project(project.pk, owner=actor.user)
        claim = _claim(
            actor,
            f"/api/v1/projects/{project.pk}/jobs/{kind}",
            idempotency,
            {"kind": kind, "frozen_input": frozen_input},
        )
        if claim is not None and claim.is_replay:
            return _replayed(claim)
        require_project_idle(locked)

        job_id = uuid.uuid4()
        resource_id = job_id if kind == JOB_KIND_IMPORT_ARXIV else requested_resource_id
        if kind == JOB_KIND_IMPORT_ARXIV:
            # 导入批次：每项初始 pending，resolved_source_id 解析后再持久化
            frozen_input.setdefault("items", [])

        job = Job.objects.create(
            id=job_id,
            project=locked,
            requested_by=actor.user,
            kind=kind,
            status=JOB_STATUS_QUEUED,
            resource_type=resource_type,
            resource_id=resource_id,
            frozen_input=frozen_input,
            input_hash=payload_fingerprint(frozen_input),
            progress=default_progress(),
        )

        result = AcceptedJob(job_id=job.id, resource_type=resource_type, resource_id=resource_id)
        if claim is not None:
            record_claim(claim, status_code=202, body=result.as_dto(), project=locked, job=job)
        transaction.on_commit(lambda: dispatch(job))
    return result


def _claim(actor: ActorContext, path: str, key: Any, payload: dict) -> Any:
    if key is None:
        return None
    if isinstance(key, Claim):
        return claim_or_replay(actor, key.method, key.canonical_path, key.key, key.request_hash)
    return claim_or_replay(actor, "POST", path, uuid.UUID(str(key)), request_fingerprint(payload))


def _replayed(claim: Any) -> AcceptedJob:
    data = claim.body
    return AcceptedJob(
        uuid.UUID(data["job_id"]), data["resource_type"], uuid.UUID(data["resource_id"]), replayed=True
    )


def dispatch(job: Job) -> None:
    """把已提交的 queued 任务投递给 Celery。

    投递失败不回滚：Job 保持 queued，由 reconcile_jobs 扫描补发。
    """
    from apps.jobs.tasks import execute_job

    if not Job.objects.filter(pk=job.pk, status=JOB_STATUS_QUEUED).exists():
        return
    from django.db.models import F

    try:
        execute_job.delay(str(job.id))
    except Exception:  # pragma: no cover - broker 不可用时走补投路径
        logging.getLogger(__name__).warning("任务队列投递失败，将由恢复扫描补投：%s", job.pk)
    Job.objects.filter(pk=job.pk).update(
        dispatch_attempts=F("dispatch_attempts") + 1,
        last_dispatched_at=timezone.now(),
    )


# ── 领取与续租 ────────────────────────────────────────────────
def claim_execution(job_id: uuid.UUID) -> ExecutionContext:
    """领取数据库执行资格；重复投递不会重复执行。

    仅比较时间不够：认领时递增 lease_token，旧 worker 无法越过 fencing 提交结果。
    """
    now = timezone.now()
    lease_seconds = int(settings.JOB_LEASE_SECONDS)

    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            raise api_error("NOT_FOUND")

        if job.cancel_requested_at is not None:
            raise api_error("INVALID_JOB_STATE", "任务已被请求取消")

        if job.status != JOB_STATUS_QUEUED:
            raise api_error("INVALID_JOB_STATE", "任务不在可领取状态")

        job.lease_token += 1
        job.status = JOB_STATUS_RUNNING
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.save(
            update_fields=[
                "lease_token",
                "status",
                "started_at",
                "heartbeat_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
        run_id = job.frozen_input.get("run_id")
        return ExecutionContext(
            job_id=job.id,
            project_id=job.project_id,
            run_id=uuid.UUID(str(run_id)) if run_id else None,
            attempt=job.attempt,
            lease_token=job.lease_token,
            deadline=job.lease_expires_at,
            cancel_checker=lambda: _cancel_requested(job.id),
        )


def _cancel_requested(job_id: uuid.UUID) -> bool:
    """从数据库重新读取取消意图：内存标记不能作为唯一依据。"""
    return Job.objects.filter(pk=job_id, cancel_requested_at__isnull=False).exists()


def heartbeat(context: ExecutionContext) -> None:
    """续租。等待网络期间也必须续租，否则租约过期会让新 worker 抢走执行权。"""
    now = timezone.now()
    updated = Job.objects.filter(
        pk=context.job_id,
        status__in=(JOB_STATUS_RUNNING, JOB_STATUS_CANCELLING),
        lease_token=context.lease_token,
        lease_expires_at__gt=now,
    ).update(
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=int(settings.JOB_LEASE_SECONDS)),
    )
    if not updated:
        raise api_error("INVALID_JOB_STATE", "执行租约已失效")
    from apps.llm.models import ModelCall

    ModelCall.objects.filter(
        job_id=context.job_id, lease_token=context.lease_token, outcome__in=("reserved", "in_flight")
    ).update(
        slot_expires_at=now
        + timedelta(
            seconds=settings.LLM_REQUEST_TIMEOUT_SECONDS + settings.JOB_LEASE_SECONDS,
        ),
    )


@contextmanager
def execution_heartbeat(context: ExecutionContext):
    """用独立数据库连接续租，网络等待不占事务；退出时关闭线程连接。"""
    heartbeat(context)
    stopped = threading.Event()
    errors = []

    def renew():
        close_old_connections()
        try:
            while not stopped.wait(settings.JOB_HEARTBEAT_SECONDS):
                try:
                    heartbeat(context)
                except Exception as exc:
                    errors.append(exc)
                    return
        finally:
            from django.db import connections

            connections.close_all()

    thread = threading.Thread(target=renew, name="job-heartbeat", daemon=True)
    thread.start()
    try:
        yield
        if errors:
            raise api_error("INVALID_JOB_STATE", "续租失败，禁止提交本次结果")
    finally:
        stopped.set()
        thread.join(timeout=settings.DATABASES["default"].get("OPTIONS", {}).get("connect_timeout", 5) + 1)


def record_progress(
    context: ExecutionContext,
    *,
    completed: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    stage: str | None = None,
    expected_lease_token: int | None = None,
) -> None:
    """提交阶段进度。写阶段产物与进度都必须条件匹配有效 token。"""
    token = expected_lease_token if expected_lease_token is not None else context.lease_token
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=context.job_id).first()
        if job is None or int(job.lease_token) != int(token):
            raise api_error("INVALID_JOB_STATE", "执行租约已被新的尝试取代")
        check_execution_lease(job, token)
        if job.cancel_requested_at is not None:
            raise api_error("INVALID_JOB_STATE", "任务已被请求取消")

        progress = dict(job.progress or {})
        if completed is not None:
            progress["completed"] = completed
        if total is not None:
            progress["total"] = total
        if unit is not None:
            progress["unit"] = unit
        job.progress = progress
        if stage is not None:
            job.stage = stage
        job.save(update_fields=["progress", "stage", "updated_at"])


# ── 取消 ──────────────────────────────────────────────────────
def cancel_job(actor: ActorContext, job_id: uuid.UUID) -> Job:
    """协作取消：queued 直接 cancelled；running → cancelling。

    已终态取消返回原状态，不倒退；取消先提交则后续不能发布结果。
    """
    now = timezone.now()
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id, project__owner=actor.user).first()
        if job is None:
            raise api_error("NOT_FOUND")

        if job.status in (JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED):
            return job  # 自然幂等：不倒退、不报错

        if job.status == JOB_STATUS_QUEUED:
            job.status = JOB_STATUS_CANCELLED
            job.cancel_requested_at = job.cancel_requested_at or now
            job.finished_at = now
        else:
            job.status = JOB_STATUS_CANCELLING
            job.cancel_requested_at = job.cancel_requested_at or now
        job.save(update_fields=["status", "cancel_requested_at", "finished_at", "updated_at"])
        return job


# ── 终态提交 ──────────────────────────────────────────────────
def _finish(
    context: ExecutionContext,
    *,
    status: str,
    result: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    finalize_resource: Callable[[Job], None] | None = None,
) -> Job:
    """终态提交：再次检查执行资格，业务资源状态与 Job 状态在同一事务提交。"""
    now = timezone.now()
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=context.job_id).first()
        if job is None:
            raise api_error("NOT_FOUND")
        if int(job.lease_token) != int(context.lease_token):
            raise api_error("INVALID_JOB_STATE", "执行租约已被新的尝试取代")
        check_execution_lease(job, context.lease_token, allow_cancelling=True)
        if job.cancel_requested_at is not None and status == JOB_STATUS_SUCCEEDED:
            # 取消先提交则不能发布成功结果
            raise api_error("INVALID_JOB_STATE", "任务已被请求取消，不能发布成功结果")
        if job.cancel_requested_at is not None and status == JOB_STATUS_FAILED:
            status = JOB_STATUS_CANCELLED

        if finalize_resource is not None:
            finalize_resource(job)

        job.status = status
        job.result = dict(result) if result is not None else job.result
        job.error = dict(error) if error is not None else None
        job.finished_at = now
        job.save(update_fields=["status", "result", "error", "finished_at", "updated_at"])
        return job


def complete_job(
    context: ExecutionContext,
    result: Mapping[str, Any] | None = None,
    *,
    finalize_resource: Callable[[Job], None] | None = None,
) -> Job:
    return _finish(
        context,
        status=JOB_STATUS_SUCCEEDED,
        result=result,
        finalize_resource=finalize_resource,
    )


def fail_job(
    context: ExecutionContext,
    error: Mapping[str, Any],
    *,
    result: Mapping[str, Any] | None = None,
    finalize_resource: Callable[[Job], None] | None = None,
) -> Job:
    """失败也保留已完成阶段的产物；错误结构为 code/message/details/retryable。"""
    return _finish(
        context,
        status=JOB_STATUS_FAILED,
        result=result,
        error=error,
        finalize_resource=finalize_resource,
    )


def mark_cancelled(context: ExecutionContext, job: Job) -> Job:
    """worker 观察到取消意图后收尾为 cancelled。"""
    now = timezone.now()
    with transaction.atomic():
        locked = Job.objects.select_for_update().filter(pk=job.pk).first()
        if locked is None:
            raise api_error("NOT_FOUND")
        if locked.lease_token != context.lease_token:
            raise api_error("INVALID_JOB_STATE", "取消收尾租约已失效")
        if locked.status in (JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED):
            return locked or job
        check_execution_lease(locked, context.lease_token, allow_cancelling=True)
        if locked.cancel_requested_at is None:
            raise api_error("INVALID_JOB_STATE", "没有取消请求")
        locked.status = JOB_STATUS_CANCELLED
        locked.finished_at = now
        locked.save(update_fields=["status", "finished_at", "updated_at"])
        return locked


# ── 新尝试 ────────────────────────────────────────────────────
def retry_job(actor: ActorContext, job_id: uuid.UUID, idempotency: Any | None = None) -> AcceptedJob:
    """仅最新一次 failed/cancelled 尝试可以创建新 Job。

    resource_id 与 frozen_input 沿用；attempt 在 Project 锁内递增，旧 Job 历史不改写。
    """
    with transaction.atomic():
        previous = Job.objects.filter(pk=job_id, project__owner=actor.user).first()
        if previous is None:
            raise api_error("NOT_FOUND")

        locked = lock_project(previous.project_id, owner=actor.user)
        previous = Job.objects.select_for_update().get(pk=previous.pk)
        claim = _claim(actor, f"/api/v1/jobs/{job_id}/retry", idempotency, {})
        if claim is not None and claim.is_replay:
            return _replayed(claim)

        if previous.status not in (JOB_STATUS_FAILED, JOB_STATUS_CANCELLED):
            raise api_error("INVALID_JOB_STATE", "只有失败或已取消的任务可以重试")

        newer = (
            Job.objects.filter(
                resource_type=previous.resource_type,
                resource_id=previous.resource_id,
                attempt__gt=previous.attempt,
            )
            .order_by("-attempt")
            .first()
        )
        if newer is not None:
            # 不能在 J1 失败、J2 成功后重新执行 J1
            raise api_error("INVALID_JOB_STATE", "该资源已有更新的尝试")
        require_project_idle(locked)
        _check_retry_resource(previous)

        latest_attempt = (
            Job.objects.filter(
                resource_type=previous.resource_type,
                resource_id=previous.resource_id,
            ).aggregate(high=Max("attempt"))["high"]
            or previous.attempt
        )

        new_job = Job.objects.create(
            project=locked,
            requested_by=actor.user,
            kind=previous.kind,
            status=JOB_STATUS_QUEUED,
            resource_type=previous.resource_type,
            resource_id=previous.resource_id,
            frozen_input=previous.frozen_input,
            input_hash=previous.input_hash,
            retry_of_job=previous,
            attempt=latest_attempt + 1,
            progress=default_progress(),
        )
        result = AcceptedJob(
            job_id=new_job.id,
            resource_type=new_job.resource_type,
            resource_id=new_job.resource_id,
        )
        if claim is not None:
            record_claim(claim, status_code=202, body=result.as_dto(), project=locked, job=new_job)
        transaction.on_commit(lambda: dispatch(new_job))
        return result


def _check_retry_resource(job: Job) -> None:
    from django.apps import apps

    run_id = job.resource_id if job.kind == "analyze" else job.frozen_input.get("run_id")
    if run_id and job.kind in ("analyze", "retrieve", "generate_review"):
        run = (
            apps.get_model("analysis", "AnalysisRun")
            .objects.filter(
                pk=run_id,
                project_id=job.project_id,
            )
            .first()
        )
        if run is None:
            raise api_error("NOT_FOUND")
        project = apps.get_model("projects", "Project").objects.get(pk=job.project_id)
        if run.corpus_revision != project.corpus_revision:
            raise api_error("STALE_RUN")


def active_jobs_for_reconcile(*, stale_before: datetime, limit: int = 100) -> list[Job]:
    """恢复扫描候选：长时间未投递的 queued，或租约已过期的 running。

    Redis 锁不能替代持久唯一约束，因此这里以数据库条件查询为准。
    """
    now = timezone.now()
    stale_queued = Q(status=JOB_STATUS_QUEUED, created_at__lt=stale_before) & (
        Q(last_dispatched_at__isnull=True) | Q(last_dispatched_at__lt=stale_before)
    )
    expired_lease = Q(
        status__in=(JOB_STATUS_RUNNING, JOB_STATUS_CANCELLING),
        lease_expires_at__lt=now,
    )
    return list(
        Job.objects.filter(status__in=ACTIVE_JOB_STATUSES)
        .filter(stale_queued | expired_lease)
        .order_by("created_at")[:limit]
    )
