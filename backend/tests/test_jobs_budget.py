"""M04：真实事务下的受理、幂等、任务恢复、预算和部署级并发。"""

import threading
import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from apps.common.auth import ActorContext
from apps.common.http import ApiError, api_error
from apps.jobs import services, tasks
from apps.jobs.models import Job
from apps.llm.budget import check_budget, reserve_call, settle_call
from apps.llm.models import ModelCall, UsageBudget
from apps.llm.schemas import ModelUsage

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    sender = Mock()
    monkeypatch.setattr(tasks.execute_job, "delay", sender)
    return sender


@pytest.fixture
def actor(user_a):
    return ActorContext(user_a, "req_m04")


def new_job(project, user, **changes):
    return Job.objects.create(
        project=project,
        requested_by=user,
        kind="import_arxiv",
        resource_type="import_batch",
        resource_id=uuid.uuid4(),
        input_hash="0" * 64,
        **changes,
    )


@pytest.fixture
def context(project_a, user_a):
    return services.claim_execution(new_job(project_a, user_a).pk)


@pytest.fixture
def prices(settings):
    settings.LLM_MODE = "real"
    settings.LLM_PRICING_VERSION = "test-only"
    settings.LLM_PRICING_CALENDAR_YEAR = timezone.now().year
    settings.LLM_PRICING_OFF_PEAK_DATES_UTC = [f"{timezone.now().year}-01-01"]
    settings.PROJECT_DEFAULT_BUDGET_USD = Decimal("10")
    UsageBudget.objects.update(limit_usd=Decimal("10"))
    settings.LLM_PRICING = {
        "peak": {
            "input_cache_hit_per_million_usd": Decimal("1"),
            "input_cache_miss_per_million_usd": Decimal("2"),
            "output_per_million_usd": Decimal("4"),
        },
        "off_peak": {
            "input_cache_hit_per_million_usd": Decimal("0.5"),
            "input_cache_miss_per_million_usd": Decimal("1"),
            "output_per_million_usd": Decimal("2"),
        },
    }


def test_second_active_job_per_project_is_rejected(actor, project_a):
    services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {}})
    with pytest.raises(ApiError) as error:
        services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {}})
    assert error.value.code == "PROJECT_BUSY"
    assert Job.objects.count() == 1


def test_idempotent_replay_precedes_busy_check(actor, project_a, no_broker):
    key = uuid.uuid4()
    first = services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {}}, key)
    again = services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {}}, key)
    assert first.job_id == again.job_id and again.replayed
    assert Job.objects.count() == no_broker.call_count == 1


def test_same_key_different_body_is_conflict(actor, project_a):
    key = uuid.uuid4()
    services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {"ids": ["a"]}}, key)
    with pytest.raises(ApiError) as error:
        services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {"ids": ["b"]}}, key)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_missing_idempotency_key_is_400(client_a, project_a, user_a):
    job = new_job(project_a, user_a, status="failed")
    response = client_a.post(f"/api/v1/jobs/{job.pk}/retry", {}, format="json")
    assert response.status_code == 400
    assert Job.objects.count() == 1


def test_queued_job_cancels_immediately(actor, project_a, user_a):
    job = new_job(project_a, user_a)
    result = services.cancel_job(actor, job.pk)
    assert result.status == "cancelled" and result.finished_at is not None
    assert tasks.execute_job(str(job.pk)) == "skipped"


def test_running_job_cancel_is_cooperative(actor, context, prices):
    reservation = reserve_call(context, ModelUsage(1000, 1000))
    result = services.cancel_job(actor, context.job_id)
    assert result.status == "cancelling" and result.finished_at is None
    assert UsageBudget.objects.get().reserved_usd == reservation.reserved_usd
    services.mark_cancelled(context, result)
    result.refresh_from_db()
    assert result.status == "cancelled"
    assert UsageBudget.objects.get().reserved_usd == reservation.reserved_usd


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_cancel_on_terminal_job_keeps_status(actor, project_a, user_a, status):
    job = new_job(project_a, user_a, status=status)
    assert services.cancel_job(actor, job.pk).status == status


def test_duplicate_delivery_does_not_reexecute(project_a, user_a, monkeypatch):
    job = new_job(project_a, user_a)
    handler = Mock(side_effect=lambda ctx, rid: services.complete_job(ctx, {"done": True}))
    monkeypatch.setattr(tasks, "resolve_handler", lambda kind: handler)
    assert tasks.execute_job(str(job.pk)) == "done"
    assert tasks.execute_job(str(job.pk)) == "skipped"
    assert handler.call_count == 1
    job.refresh_from_db()
    assert job.status == "succeeded" and job.result == {"done": True}


def test_stale_worker_cannot_publish(context):
    Job.objects.filter(pk=context.job_id).update(lease_token=context.lease_token + 1)
    for action in (
        lambda: services.complete_job(context),
        lambda: services.record_progress(context, completed=1),
        lambda: services.mark_cancelled(context, Job.objects.get(pk=context.job_id)),
    ):
        with pytest.raises(ApiError):
            action()
    assert Job.objects.get().status == "running"


def test_retry_only_for_latest_failed_attempt(actor, project_a, user_a):
    job = new_job(project_a, user_a, status="failed")
    second = services.retry_job(actor, job.pk, uuid.uuid4())
    second_job = Job.objects.get(pk=second.job_id)
    assert second_job.attempt == 2 and second_job.retry_of_job_id == job.pk
    assert second_job.resource_id == job.resource_id
    with pytest.raises(ApiError) as error:
        services.retry_job(actor, job.pk, uuid.uuid4())
    assert error.value.code == "INVALID_JOB_STATE"


def test_retry_key_replays_even_when_new_attempt_is_busy(actor, project_a, user_a, no_broker):
    job = new_job(project_a, user_a, status="failed")
    key = uuid.uuid4()
    first = services.retry_job(actor, job.pk, key)
    again = services.retry_job(actor, job.pk, key)
    assert again.job_id == first.job_id and again.replayed and no_broker.call_count == 1


def test_budget_reservation_blocks_over_spend(context, prices):
    UsageBudget.objects.filter(project_id=context.project_id).update(limit_usd=Decimal("0.001"))
    with pytest.raises(ApiError) as error:
        reserve_call(context, ModelUsage(1000, 1000))
    assert error.value.code == "BUDGET_EXCEEDED"
    assert not ModelCall.objects.exists()


def test_unsettled_calls_keep_reservation(context, prices):
    reservation = reserve_call(context, ModelUsage(1000, 1000))
    settle_call(reservation, ModelUsage())
    assert UsageBudget.objects.get().reserved_usd == Decimal("0.006")
    call = ModelCall.objects.get()
    assert call.outcome == "unsettled" and call.estimated_cost_usd is None
    assert call.slot_expires_at is None and call.settled_at is None


def test_zero_budget_rejects_real_but_allows_mock(context, prices, settings):
    UsageBudget.objects.filter(project_id=context.project_id).update(limit_usd=0)
    with pytest.raises(ApiError):
        check_budget(context)
    settings.LLM_MODE = "mock"
    assert reserve_call(context, ModelUsage(1000, 1000)).reserved_usd == 0


def test_settlement_is_idempotent_and_uses_frozen_tariff(context, prices, settings):
    reservation = reserve_call(context, ModelUsage(1000, 1000))
    call = ModelCall.objects.get()
    call.pricing_snapshot["tariff"] = "off_peak"
    call.save(update_fields=["pricing_snapshot"])
    settings.LLM_PRICING = {}  # 调价不影响历史调用
    usage = ModelUsage(100, 20, 40, 60)
    settle_call(reservation, usage)
    settle_call(reservation, usage)
    budget = UsageBudget.objects.get()
    assert budget.reserved_usd == 0
    assert budget.settled_usd == Decimal("0.00012")


def test_actual_overage_is_recorded_not_clipped(context, prices):
    UsageBudget.objects.filter(project_id=context.project_id).update(limit_usd=Decimal("0.001"))
    reservation = reserve_call(context, ModelUsage(1, 1))
    settle_call(reservation, ModelUsage(100000, 100000, 0, 100000))
    assert UsageBudget.objects.get().settled_usd > Decimal("0.001")
    with pytest.raises(ApiError):
        reserve_call(context, ModelUsage(1, 1))


def test_late_usage_keeps_reservation_and_does_not_publish(context, prices):
    reservation = reserve_call(context, ModelUsage(1000, 1000))
    Job.objects.filter(pk=context.job_id).update(lease_token=context.lease_token + 1)
    with pytest.raises(ApiError):
        settle_call(reservation, ModelUsage(100, 10, 0, 100, {"prompt_tokens": 100}))
    call = ModelCall.objects.get()
    assert call.outcome == "unsettled" and call.usage_raw["prompt_tokens"] == 100
    assert UsageBudget.objects.get().reserved_usd == reservation.reserved_usd


@pytest.mark.parametrize("cancel", [False, True])
def test_recovery_revokes_lease_without_automatic_reexecution(context, prices, cancel):
    reservation = reserve_call(context, ModelUsage(1000, 1000))
    Job.objects.filter(pk=context.job_id).update(
        status="cancelling" if cancel else "running",
        lease_expires_at=timezone.now() - timedelta(seconds=1),
        cancel_requested_at=timezone.now() if cancel else None,
    )
    assert tasks.reconcile_jobs()["expired"] == 1
    job = Job.objects.get()
    assert job.status == ("cancelled" if cancel else "failed")
    assert job.error["code"] == "WORKER_LOST" and job.lease_token > context.lease_token
    assert ModelCall.objects.get().outcome == "unsettled"
    assert UsageBudget.objects.get().reserved_usd == reservation.reserved_usd


def test_queue_failure_leaves_recoverable_job(actor, project_a, no_broker):
    no_broker.side_effect = RuntimeError("broker unavailable")
    accepted = services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {}})
    job = Job.objects.get(pk=accepted.job_id)
    assert job.status == "queued" and job.dispatch_attempts == 1
    Job.objects.filter(pk=job.pk).update(
        created_at=timezone.now() - timedelta(minutes=2),
        last_dispatched_at=timezone.now() - timedelta(minutes=2),
    )
    no_broker.side_effect = None
    assert tasks.reconcile_jobs()["redispatched"] == 1
    assert tasks.reconcile_jobs()["redispatched"] == 0


def test_no_dispatch_on_transaction_rollback(actor, project_a, no_broker):
    with pytest.raises(RuntimeError), transaction.atomic():
        services.accept_job(actor, project_a, "import_arxiv", {"frozen_input": {}})
        raise RuntimeError("rollback")
    assert not Job.objects.exists() and not no_broker.called


def test_return_without_terminal_state_fails(project_a, user_a, monkeypatch):
    job = new_job(project_a, user_a)
    monkeypatch.setattr(tasks, "resolve_handler", lambda kind: lambda *args: None)
    assert tasks.execute_job(str(job.pk)) == "failed"
    job.refresh_from_db()
    assert job.status == "failed"


def test_handler_cancellation_becomes_cancelled(actor, project_a, user_a, monkeypatch):
    job = new_job(project_a, user_a)

    def handler(ctx, rid):
        services.cancel_job(actor, ctx.job_id)
        ctx.ensure_not_cancelled()

    monkeypatch.setattr(tasks, "resolve_handler", lambda kind: handler)
    tasks.execute_job(str(job.pk))
    job.refresh_from_db()
    assert job.status == "cancelled"


def test_terminal_job_cannot_be_overwritten(context):
    services.complete_job(context, {"ok": True})
    with pytest.raises(ApiError):
        services.fail_job(context, api_error("INTERNAL_ERROR").as_error_body())
    assert Job.objects.get().status == "succeeded"


def test_heartbeat_renews_during_wait(context, settings):
    settings.JOB_HEARTBEAT_SECONDS = 0.03
    before = Job.objects.get().heartbeat_at
    with services.execution_heartbeat(context):
        threading.Event().wait(0.12)
    assert Job.objects.get().heartbeat_at > before


def test_expired_worker_cannot_renew_or_settle(context):
    Job.objects.filter(pk=context.job_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(ApiError):
        services.heartbeat(context)
    with pytest.raises(ApiError):
        services.complete_job(context)


def test_global_slots_shared_between_projects(context, project_b, user_b, settings):
    settings.LLM_MAX_CONCURRENCY = 1
    other = services.claim_execution(new_job(project_b, user_b).pk)
    first = reserve_call(context, ModelUsage(100, 100))
    with pytest.raises(ApiError) as error:
        reserve_call(other, ModelUsage(100, 100))
    assert error.value.code == "MODEL_RATE_LIMITED"
    settle_call(first, ModelUsage(0, 0, 0, 0))
    assert reserve_call(other, ModelUsage(100, 100)).call_id


def test_concurrent_global_slot_claims_are_serialized(context, project_b, user_b, settings):
    if connection.vendor != "postgresql":
        pytest.skip("需要真实 PostgreSQL 并发")
    settings.LLM_MAX_CONCURRENCY = 1
    other = services.claim_execution(new_job(project_b, user_b).pk)
    barrier = threading.Barrier(2, timeout=10)
    results = []

    def run(ctx):
        try:
            barrier.wait()
            reserve_call(ctx, ModelUsage(10, 10))
            results.append("accepted")
        except ApiError as exc:
            results.append(exc.code)
        finally:
            connections.close_all()

    workers = [threading.Thread(target=run, args=(ctx,)) for ctx in (context, other)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
    assert sorted(results) == ["MODEL_RATE_LIMITED", "accepted"]


def test_foreign_context_cannot_reserve(context, project_b):
    with pytest.raises(ApiError):
        reserve_call(replace(context, project_id=project_b.pk), ModelUsage(1, 1))


def test_logical_attempt_cannot_be_sent_twice(context):
    logical = uuid.uuid4()
    reserve_call(context, ModelUsage(1, 1), logical_call_id=logical)
    with pytest.raises(ApiError):
        reserve_call(context, ModelUsage(1, 1), logical_call_id=logical)
    assert ModelCall.objects.count() == 1


def test_expired_slot_can_be_reclaimed_without_refunding(context, prices, project_b, user_b, settings):
    settings.LLM_MAX_CONCURRENCY = 1
    first = reserve_call(context, ModelUsage(1000, 1000))
    ModelCall.objects.filter(pk=first.model_call_pk).update(
        slot_expires_at=timezone.now() - timedelta(seconds=1),
        outcome="unsettled",
    )
    other = services.claim_execution(new_job(project_b, user_b).pk)
    # project_b 由 mock 配置创建，单独设定测试预算。
    UsageBudget.objects.filter(project_id=project_b.pk).update(limit_usd=10)
    assert reserve_call(other, ModelUsage(1000, 1000)).call_id
    assert UsageBudget.objects.get(project_id=context.project_id).reserved_usd == first.reserved_usd


def test_stale_run_retry_rejected(actor, project_a, user_a):
    from apps.analysis.models import AnalysisRun

    run = AnalysisRun.objects.create(
        project=project_a, corpus_revision=1, research_question="question", output_language="zh-CN"
    )
    job = new_job(project_a, user_a, status="failed")
    Job.objects.filter(pk=job.pk).update(kind="analyze", resource_type="analysis_run", resource_id=run.pk)
    project_a.corpus_revision = 2
    project_a.save(update_fields=["corpus_revision"])
    with pytest.raises(ApiError) as error:
        services.retry_job(actor, job.pk, uuid.uuid4())
    assert error.value.code == "STALE_RUN" and Job.objects.count() == 1


def test_reservation_cannot_settle_another_jobs_bill(context, project_b, user_b):
    first = reserve_call(context, ModelUsage(10, 10))
    other = services.claim_execution(new_job(project_b, user_b).pk)
    second = reserve_call(other, ModelUsage(10, 10))
    with pytest.raises(ApiError):
        settle_call(replace(first, model_call_pk=second.model_call_pk), ModelUsage(0, 0, 0, 0))
    assert ModelCall.objects.filter(settled_at__isnull=True).count() == 2
