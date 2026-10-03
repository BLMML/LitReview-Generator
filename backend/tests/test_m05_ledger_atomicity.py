"""真实 PostgreSQL 双连接验证导入账本与取消操作的提交顺序。"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from apps.common.auth import ActorContext
from apps.common.http import ApiError
from apps.jobs.models import Job
from apps.jobs.services import cancel_job, claim_execution, complete_job, mark_cancelled
from apps.literature import services

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _isolated_postgres(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("账本与取消的并发顺序需要真实 PostgreSQL 行锁")
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)


@pytest.fixture
def running_import(project_a, user_a):
    actor = ActorContext(user_a, "req_ledger_atomicity")
    accepted = services.import_arxiv(actor, project_a.pk, ["2401.00001"], uuid.uuid4())
    context = claim_execution(accepted.job_id)
    job = Job.objects.get(pk=context.job_id)
    items = copy.deepcopy(job.result["items"])
    items[0]["resolved_source_id"] = "2401.00001v1"
    return actor, context, job, items


def _backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _wait_for_row_lock(waiting_pid, holder_pid, finished):
    """观察数据库实际锁等待，不能仅靠线程启动或固定 sleep 推断并发成功。"""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [waiting_pid])
            if holder_pid in cursor.fetchone()[0]:
                return
        if finished.wait(0.01):
            pytest.fail("竞争操作在账本事务释放行锁前已经完成")
    pytest.fail("未观察到竞争连接等待当前 Job 行锁")


def test_cancel_waits_until_checked_ledger_write_commits(running_import, monkeypatch):
    actor, context, job, items = running_import
    checked = services._lease_writable
    holder_pid = _backend_pid()
    started = threading.Event()
    finished = threading.Event()
    backend_pids = []
    futures = []

    def cancel_on_another_connection():
        try:
            backend_pids.append(_backend_pid())
            started.set()
            cancel_job(actor, context.job_id)
        finally:
            connections.close_all()
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def check_then_race_cancellation(ctx, *, allow_cancelling):
            allowed = checked(ctx, allow_cancelling=allow_cancelling)
            assert allowed
            futures.append(pool.submit(cancel_on_another_connection))
            assert started.wait(5)
            _wait_for_row_lock(backend_pids[0], holder_pid, finished)
            return allowed

        monkeypatch.setattr(services, "_lease_writable", check_then_race_cancellation)
        assert services._save_items(context, job, items)
        futures[0].result(timeout=5)

    job.refresh_from_db()
    assert job.result == {"items": items}
    assert job.status == "cancelling"
    assert job.cancel_requested_at is not None
    # 已合法提交的账本可以保留，但后到的取消仍阻止整批宣布成功。
    with pytest.raises(ApiError) as error:
        complete_job(context, job.result)
    assert error.value.code == "INVALID_JOB_STATE"
    assert mark_cancelled(context, job).status == "cancelled"


def test_cancel_committed_first_rejects_waiting_ledger_writer(running_import):
    actor, context, job, items = running_import
    original_result = copy.deepcopy(job.result)
    holder_pid = _backend_pid()
    started = threading.Event()
    finished = threading.Event()
    backend_pids = []

    def write_on_another_connection():
        try:
            backend_pids.append(_backend_pid())
            started.set()
            return services._save_items(context, job, items)
        finally:
            connections.close_all()
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            cancel_job(actor, context.job_id)
            future = pool.submit(write_on_another_connection)
            assert started.wait(5)
            _wait_for_row_lock(backend_pids[0], holder_pid, finished)
        assert future.result(timeout=5) is False

    job.refresh_from_db()
    assert job.result == original_result
    assert job.status == "cancelling"


def test_context_cannot_write_another_projects_or_jobs_ledger(running_import, project_b):
    _, context, job, items = running_import
    original_result = copy.deepcopy(job.result)
    other_job = Job.objects.create(
        project=project_b,
        requested_by=project_b.owner,
        kind="import_arxiv",
        resource_type="import_batch",
        resource_id=uuid.uuid4(),
        input_hash="a" * 64,
        lease_token=context.lease_token,
        result={"items": []},
    )

    assert services._save_items(context, other_job, items) is False
    assert services._save_items(replace(context, project_id=project_b.pk), job, items) is False
    job.refresh_from_db()
    other_job.refresh_from_db()
    assert job.result == original_result
    assert other_job.result == {"items": []}


def test_lease_expiring_after_check_still_rejects_ledger_write(running_import, monkeypatch):
    _, context, job, items = running_import
    original_result = copy.deepcopy(job.result)
    checked = services._lease_writable
    after_expiry = job.lease_expires_at + timedelta(microseconds=1)

    def check_then_expire(ctx, *, allow_cancelling):
        allowed = checked(ctx, allow_cancelling=allow_cancelling)
        assert allowed
        monkeypatch.setattr(services.timezone, "now", lambda: after_expiry)
        return allowed

    monkeypatch.setattr(services, "_lease_writable", check_then_expire)
    assert services._save_items(context, job, items) is False
    job.refresh_from_db()
    assert job.result == original_result


@pytest.mark.parametrize("invalid_lease", ["expired", "replaced", "terminal"])
def test_cancellation_cleanup_cannot_bypass_fencing(running_import, invalid_lease):
    actor, context, job, items = running_import
    original_result = copy.deepcopy(job.result)
    cancel_job(actor, job.pk)
    changes = {
        "expired": {"lease_expires_at": timezone.now() - timedelta(seconds=1)},
        "replaced": {"lease_token": context.lease_token + 1},
        "terminal": {"status": "cancelled"},
    }
    Job.objects.filter(pk=job.pk).update(**changes[invalid_lease])

    assert services._save_items(context, job, items, allow_cancelling=True) is False
    job.refresh_from_db()
    assert job.result == original_result
