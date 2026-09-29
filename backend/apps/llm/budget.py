"""持久预算与部署级名额。所有锁只覆盖短事务，不覆盖供应商请求。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.http import ApiError, api_error
from apps.common.locking import check_execution_lease, lock_project
from apps.jobs.models import Job
from apps.llm.models import ModelCall, ModelCallGate, UsageBudget
from apps.llm.pricing import cost, pricing_snapshot
from apps.llm.schemas import ModelTaskRequest, ModelUsage


@dataclass(frozen=True)
class Reservation:
    call_id: str
    reserved_usd: Decimal
    model_call_pk: Any
    job_id: Any
    lease_token: int


def _budget(project_id: Any) -> UsageBudget:
    budget, _ = UsageBudget.objects.get_or_create(
        project_id=project_id,
        defaults={"limit_usd": settings.PROJECT_DEFAULT_BUDGET_USD},
    )
    return UsageBudget.objects.select_for_update().get(pk=budget.pk)


def _job(context: Any, *, allow_cancelling: bool = False) -> Job:
    job = Job.objects.select_for_update().filter(pk=context.job_id).first()
    if job is None or job.project_id != context.project_id:
        raise api_error("INVALID_JOB_STATE", "任务上下文无效")
    check_execution_lease(job, context.lease_token, allow_cancelling=allow_cancelling)
    return job


def check_budget(context: Any) -> None:
    with transaction.atomic():
        lock_project(context.project_id)
        _job(context)
        budget = _budget(context.project_id)
        if settings.LLM_MODE != "mock" and (
            budget.limit_usd <= 0 or budget.settled_usd + budget.reserved_usd >= budget.limit_usd
        ):
            raise api_error("BUDGET_EXCEEDED")


def reserve_call(
    context: Any,
    maximum_usage: ModelUsage,
    *,
    request: ModelTaskRequest | None = None,
    network_attempt: int = 1,
    logical_call_id: uuid.UUID | None = None,
    request_hash: str = "",
    cache_key: str = "",
    model_snapshot: dict | None = None,
) -> Reservation:
    """同一门闩下认领全局名额；未知费用不因超时或租约失效而释放。"""
    snapshot = pricing_snapshot(mock=settings.LLM_MODE == "mock")
    amount = cost(maximum_usage, snapshot, reserve=True)
    if amount is None or not 1 <= network_attempt <= settings.LLM_MAX_ATTEMPTS:
        raise api_error("VALIDATION_ERROR", "模型调用上界或尝试次数不合法")
    now = timezone.now()
    with transaction.atomic():
        ModelCallGate.objects.get_or_create(pk=1)
        ModelCallGate.objects.select_for_update().get(pk=1)
        lock_project(context.project_id)
        _job(context)
        budget = _budget(context.project_id)
        if not snapshot["mock"] and (
            budget.limit_usd <= 0 or budget.settled_usd + budget.reserved_usd + amount > budget.limit_usd
        ):
            raise api_error("BUDGET_EXCEEDED")
        # 崩溃后名额可回收，但费用仍留在 reserved 中等待对账。
        ModelCall.objects.filter(outcome__in=("reserved", "in_flight"), slot_expires_at__lte=now).update(
            outcome="unsettled",
            slot_expires_at=None,
        )
        occupied = ModelCall.objects.filter(slot_expires_at__gt=now).count()
        if occupied >= settings.LLM_MAX_CONCURRENCY:
            raise api_error("MODEL_RATE_LIMITED", "部署级模型并发名额已满", retryable=True)
        logical_id = logical_call_id or (request.logical_call_id if request else None) or uuid.uuid4()
        if ModelCall.objects.filter(logical_call_id=logical_id, network_attempt=network_attempt).exists():
            raise api_error("INVALID_JOB_STATE", "该逻辑调用的网络尝试已受理")
        call = ModelCall.objects.create(
            project_id=context.project_id,
            job_id=context.job_id,
            lease_token=context.lease_token,
            task_type=request.task_type if request else "query_expand",
            logical_call_id=logical_id,
            network_attempt=network_attempt,
            request_hash=request_hash,
            cache_key=cache_key,
            model_snapshot=model_snapshot or {},
            pricing_snapshot=snapshot,
            reserved_usd=amount,
            slot_expires_at=now
            + timedelta(
                seconds=settings.LLM_REQUEST_TIMEOUT_SECONDS + settings.JOB_LEASE_SECONDS,
            ),
        )
        budget.reserved_usd += amount
        budget.save(update_fields=["reserved_usd", "updated_at"])
    return Reservation(str(call.pk), amount, call.pk, context.job_id, context.lease_token)


def mark_started(context: Any, reservation: Reservation) -> None:
    with transaction.atomic():
        _job(context)
        call = ModelCall.objects.select_for_update().get(pk=reservation.model_call_pk)
        if (
            call.outcome != "reserved"
            or call.lease_token != context.lease_token
            or call.job_id != context.job_id
            or reservation.job_id != context.job_id
        ):
            raise api_error("INVALID_JOB_STATE")
        now = timezone.now()
        snapshot = dict(call.pricing_snapshot)
        # 使用冻结的节假日日历和价表，只在实际发出请求时确定档位。
        hour = now.hour
        holiday = now.date().isoformat() in snapshot["off_peak_dates_utc"]
        peak = now.weekday() < 5 and (1 <= hour < 4 or 6 <= hour < 10) and not holiday
        snapshot.update(tariff="peak" if peak else "off_peak", priced_at=now.isoformat())
        call.pricing_snapshot = snapshot
        call.outcome = "in_flight"
        call.started_at = now
        call.save(update_fields=["outcome", "started_at", "pricing_snapshot", "updated_at"])


def settle_call(
    reservation: Reservation,
    usage: ModelUsage,
    *,
    outcome: str = "succeeded",
    error: dict | None = None,
    provider_request_id: str | None = None,
    raw_output_asset: Any = None,
    request_finished: bool = True,
) -> None:
    """幂等结算；迟到用量只作线索保存，不越过租约改写预算或成功结果。"""
    now = timezone.now()
    stale = False
    with transaction.atomic():
        project_id = Job.objects.values_list("project_id", flat=True).get(pk=reservation.job_id)
        lock_project(project_id)
        job = Job.objects.select_for_update().get(pk=reservation.job_id)
        budget = _budget(job.project_id)
        call = ModelCall.objects.select_for_update().get(pk=reservation.model_call_pk)
        if (
            call.job_id != job.pk
            or call.project_id != job.project_id
            or call.lease_token != reservation.lease_token
        ):
            raise api_error("INVALID_JOB_STATE", "预算凭据与调用不匹配")
        if call.settled_at is not None:
            return
        try:
            check_execution_lease(job, reservation.lease_token, allow_cancelling=True)
        except ApiError:
            stale = True
        call.usage_raw = dict(usage.raw)
        if stale:
            call.outcome = "unsettled"
            if request_finished:
                call.slot_expires_at = None
            call.save(update_fields=["usage_raw", "outcome", "slot_expires_at", "updated_at"])
        else:
            charge = cost(usage, call.pricing_snapshot)
            call.input_tokens = usage.input_tokens
            call.output_tokens = usage.output_tokens
            call.cache_hit_tokens = usage.cache_hit_tokens
            call.cache_miss_tokens = usage.cache_miss_tokens
            call.estimated_cost_usd = charge
            call.error = error
            call.provider_request_id = provider_request_id
            call.finished_at = now
            if request_finished:
                call.slot_expires_at = None
            call.outcome = outcome if charge is not None else "unsettled"
            if charge is not None:
                budget.reserved_usd -= call.reserved_usd
                budget.settled_usd += charge
                budget.save(update_fields=["reserved_usd", "settled_usd", "updated_at"])
                call.settled_at = now
            if job.cancel_requested_at is None and outcome == "succeeded":
                call.raw_output_asset = raw_output_asset
            call.save()
    if stale:
        raise api_error("INVALID_JOB_STATE", "迟到用量已记录，原执行租约已失效")


def abandon_calls(job: Job) -> None:
    """调用方须持有 Job 行锁；保留可能在途的名额直到原超时边界。"""
    ModelCall.objects.filter(job=job, settled_at__isnull=True).update(outcome="unsettled")
