"""PostgreSQL 约束与并发验证（docs/mvp/05 第 4 节 T05 / T06 / T11）。

**这些测试只在 PostgreSQL 上运行。** docs/mvp/01 第 9 节明确要求：
并发约束必须用真实 PostgreSQL 事务与独立连接验证，不能仅用 SQLite 或单线程 mock
得出结论。SQLite 上没有部分唯一索引、没有真正的事务隔离，跑出来的"通过"没有意义，
因此本文件在 SQLite 下整组跳过。

跑法（需要 DATABASE_URL 指向 PostgreSQL）：

    python -m pytest tests/test_postgres_constraints.py -v
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.db import connections
from django.db.utils import IntegrityError

from apps.common.http import ApiError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def require_postgresql():
    """SQLite 下不做这些断言：环境本身不提供要验证的语义。"""
    if connections["default"].vendor != "postgresql":
        pytest.skip(
            "并发与约束结论只在 PostgreSQL 上有效（见 docs/mvp/01 第 9 节）；"
            "当前是 SQLite，请设置 DATABASE_URL 后重跑"
        )


def make_job(project, user, *, status="queued", attempt=1, resource_id=None, resource_type=None):
    from apps.jobs.models import Job

    return Job.objects.create(
        project=project,
        requested_by=user,
        kind="analyze",
        status=status,
        resource_type=resource_type or "analysis_run",
        resource_id=resource_id or project.id,
        input_hash="0" * 64,
        attempt=attempt,
    )


# ── T06：每项目同时最多一个活跃任务 ────────────────────────────
def test_second_active_job_is_rejected_by_database(project_a, user_a):
    """部分唯一索引必须由数据库保证，而不是只靠服务层判断。"""
    make_job(project_a, user_a, status="queued")
    with pytest.raises(IntegrityError):
        make_job(project_a, user_a, status="queued", resource_id=user_a.pk)


@pytest.mark.parametrize("second_status", ["queued", "running", "cancelling"])
def test_all_active_statuses_occupy_the_slot(project_a, user_a, second_status):
    make_job(project_a, user_a, status="running")
    with pytest.raises(IntegrityError):
        make_job(project_a, user_a, status=second_status, resource_id=user_a.pk)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_job_frees_the_slot(project_a, user_a, terminal):
    """终态任务不占用项目：失败后必须能重试。"""
    make_job(project_a, user_a, status=terminal)
    job2 = make_job(project_a, user_a, status="queued", resource_id=user_a.pk)
    assert job2.pk is not None


def test_different_projects_do_not_block_each_other(project_a, project_b, user_a, user_b):
    make_job(project_a, user_a, status="queued")
    make_job(project_b, user_b, status="queued")
    from apps.jobs.models import Job

    assert Job.objects.filter(status="queued").count() == 2


# ── 重试：同一资源的 attempt 必须唯一 ──────────────────────────
def test_resource_attempt_pair_is_unique(project_a, user_a):
    make_job(project_a, user_a, status="failed", attempt=1, resource_id=user_a.pk)
    with pytest.raises(IntegrityError):
        make_job(project_a, user_a, status="queued", attempt=1, resource_id=user_a.pk)


def test_new_attempt_is_allowed(project_a, user_a):
    make_job(project_a, user_a, status="failed", attempt=1, resource_id=user_a.pk)
    job = make_job(project_a, user_a, status="queued", attempt=2, resource_id=user_a.pk)
    assert job.attempt == 2


# ── T11：乐观锁与检查约束 ─────────────────────────────────────
@pytest.mark.parametrize("field,value", [("version", 0), ("corpus_revision", 0)])
def test_project_version_must_be_positive(project_a, field, value):

    setattr(project_a, field, value)
    with pytest.raises(IntegrityError):
        project_a.save(update_fields=[field])


def make_paper(project, name="paper"):
    """建一篇最小可用的 Paper（含必需的 FileAsset）。"""
    import uuid

    from apps.common.models import FileAsset
    from apps.literature.models import Paper

    asset = FileAsset.objects.create(
        project=project,
        storage_key=f"test/{uuid.uuid4().hex}",
        purpose="original_pdf",
        media_type="application/pdf",
        original_filename=f"{name}.pdf",
        sha256=uuid.uuid4().hex * 2,
        size_bytes=1024,
    )
    return Paper.objects.create(
        project=project,
        file_asset=asset,
        title=name,
        file_sha256=uuid.uuid4().hex * 2,
        page_count=5,
    )


def test_paper_page_count_upper_bound_is_enforced(project_a):
    """页数上限写在数据库检查约束里，不只在服务层校验。"""
    import uuid

    from apps.common.models import FileAsset
    from apps.literature.models import Paper

    asset = FileAsset.objects.create(
        project=project_a,
        storage_key=f"test/{uuid.uuid4().hex}",
        purpose="original_pdf",
        media_type="application/pdf",
        original_filename="too-long.pdf",
        sha256=uuid.uuid4().hex * 2,
        size_bytes=1024,
    )
    with pytest.raises(IntegrityError):
        Paper.objects.create(
            project=project_a,
            file_asset=asset,
            title="超过 50 页",
            file_sha256=uuid.uuid4().hex * 2,
            page_count=51,  # MVP 上限是 50
        )


def test_same_file_hash_cannot_be_uploaded_twice(project_a):
    """同项目同 SHA256 只能有一篇：重复上传由唯一约束兜底。"""
    import uuid

    from apps.common.models import FileAsset
    from apps.literature.models import Paper

    shared = uuid.uuid4().hex * 2
    first = make_paper(project_a, "first")
    first.file_sha256 = shared
    first.save(update_fields=["file_sha256"])

    asset = FileAsset.objects.create(
        project=project_a,
        storage_key=f"test/{uuid.uuid4().hex}",
        purpose="original_pdf",
        media_type="application/pdf",
        original_filename="dup.pdf",
        sha256=uuid.uuid4().hex * 2,
        size_bytes=1024,
    )
    with pytest.raises(IntegrityError):
        Paper.objects.create(
            project=project_a,
            file_asset=asset,
            title="重复文件",
            file_sha256=shared,
            page_count=5,
        )


def test_dispute_sides_must_differ(project_a):
    """dispute_sides_differ：数据库层禁止左右论文是同一篇。"""
    from apps.analysis.models import AnalysisRun, Dispute

    paper = make_paper(project_a, "唯一一篇")
    run = AnalysisRun.objects.create(
        project=project_a,
        corpus_revision=1,
        research_question="用于测试的研究问题",
        output_language="zh-CN",
    )
    with pytest.raises(IntegrityError):
        Dispute.objects.create(
            run=run,
            left_paper=paper,
            right_paper=paper,
            classification="possible_conflict",
            reason="左右相同，应被数据库拒绝",
        )


# ── T05：两个并发受理，只能有一个成功 ──────────────────────────
def test_concurrent_accept_only_one_wins(project_a, user_a):
    """两个独立连接同时为同一项目建活跃任务，数据库只允许一个成功。

    这是 SQLite 给不出的结论：SQLite 会整体串行化写入，
    掩盖了"两个请求同时到达"时的真实竞争。
    """
    outcome: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=15)

    def attempt(tag: str) -> None:
        try:
            barrier.wait()  # 尽量让两条 INSERT 同时出发
            make_job(project_a, user_a, status="queued", resource_id=user_a.pk)
            result = "accepted"
        except IntegrityError:
            result = "blocked"
        except Exception as exc:  # noqa: BLE001
            result = f"error:{type(exc).__name__}"
        finally:
            # 只关本线程的连接；close_all() 的跨线程行为在不同 Django 版本间不一致
            connections["default"].close()
        with lock:
            outcome.append(result)

    threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcome) == 2, f"线程未正常结束：{outcome}"
    assert sorted(outcome) == ["accepted", "blocked"], f"并发结果不符合预期：{outcome}"

    from apps.jobs.models import Job

    assert Job.objects.filter(project=project_a, status="queued").count() == 1


def test_row_lock_serializes_project_writes(project_a, user_a):
    """select_for_update 在 PostgreSQL 上真的会阻塞第二个事务。"""
    from django.db import transaction

    from apps.common.locking import lock_project

    observed: list[str] = []

    def holder() -> None:
        try:
            with transaction.atomic():
                lock_project(project_a.pk, owner=user_a)
                observed.append("locked")
                # 持锁期间，另一个连接必须等
                threading.Event().wait(1.2)
        finally:
            connections["default"].close()

    thread = threading.Thread(target=holder)
    thread.start()
    threading.Event().wait(0.3)

    import time

    started = time.monotonic()
    with transaction.atomic():
        lock_project(project_a.pk, owner=user_a)
    waited = time.monotonic() - started
    thread.join(timeout=30)

    assert observed == ["locked"]
    assert waited >= 0.5, f"第二个事务没有被阻塞（等待 {waited:.2f}s），行锁可能没生效"


# ── T03 / T05：并发上传与容量（M05）────────────────────────────
def _upload_in_thread(user_id, project_id, payload, *, title=None):
    """在独立连接上跑一次真实上传，返回 (结果标记, paper_id)。"""
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.common.auth import ActorContext
    from apps.common.idempotency import parse_idempotency_key
    from apps.literature.services import upload_pdf

    try:
        user = get_user_model().objects.get(pk=user_id)
        actor = ActorContext(user=user, request_id="req_concurrent")
        uploaded = SimpleUploadedFile("same.pdf", payload, content_type="application/pdf")
        result = upload_pdf(
            actor, project_id, uploaded, {"title": title}, parse_idempotency_key(str(uuid.uuid4()))
        )
        return ("ok", result.paper_id)
    except ApiError as exc:
        return (exc.code, None)
    except Exception as exc:  # noqa: BLE001
        return (f"error:{type(exc).__name__}", None)
    finally:
        # 只关本线程的连接；close_all() 的跨线程行为在不同 Django 版本间不一致
        connections["default"].close()


def test_concurrent_same_file_upload_creates_one_paper(project_a, user_a):
    """两个连接同时上传同一份文件：唯一约束必须收敛成一篇文献。

    SQLite 会整体串行化写入，掩盖真实竞争，因此该结论只在 PostgreSQL 上有效。
    """
    from tests.conftest import make_pdf_bytes

    from apps.literature.models import Paper

    payload = make_pdf_bytes(2)
    barrier = threading.Barrier(2, timeout=15)

    def attempt():
        barrier.wait()
        return _upload_in_thread(user_a.pk, project_a.pk, payload)

    results: list[tuple[str, str | None]] = []
    lock = threading.Lock()

    def run():
        outcome = attempt()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2, f"线程未正常结束：{results}"
    # 去重语义：两次都「成功」，但只落一篇文献
    assert [tag for tag, _ in results] == ["ok", "ok"], f"并发上传未收敛：{results}"
    assert Paper.objects.filter(project=project_a).count() == 1
    assert len({paper_id for _, paper_id in results}) == 1, "两次上传必须返回同一篇文献"


def test_concurrent_uploads_respect_capacity_limit(project_a, user_a, settings):
    """容量上限在项目行锁内判定：并发上传不得突破 30 篇。

    上限压到 2，用 4 个并发连接抢；无论调度顺序如何，最终都只能有 2 篇。
    """
    from tests.conftest import make_pdf_bytes

    from apps.literature.models import Paper

    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 2}
    payloads = [make_pdf_bytes(pages) for pages in range(1, 5)]
    barrier = threading.Barrier(4, timeout=20)

    results: list[tuple[str, str | None]] = []
    lock = threading.Lock()

    def run(payload):
        barrier.wait()
        outcome = _upload_in_thread(user_a.pk, project_a.pk, payload)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 4, f"线程未正常结束：{results}"
    accepted = [tag for tag, _ in results if tag == "ok"]
    rejected = [tag for tag, _ in results if tag == "PAPER_LIMIT_EXCEEDED"]
    assert len(accepted) == 2, f"容量上限被突破：{results}"
    assert len(rejected) == 2, f"超限没有被拒绝：{results}"
    assert Paper.objects.filter(project=project_a).count() == 2


def test_concurrent_paper_patch_accepts_only_one_writer(project_a, user_a):
    """双端基于同一 version 保存，只能有一个成功（T11 乐观锁）。"""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from tests.conftest import make_pdf_bytes

    from apps.common.auth import ActorContext
    from apps.common.idempotency import parse_idempotency_key
    from apps.literature.models import Paper
    from apps.literature.services import upload_pdf

    actor = ActorContext(user=user_a, request_id="req_test")
    uploaded = upload_pdf(
        actor,
        project_a.pk,
        SimpleUploadedFile("lock.pdf", make_pdf_bytes(1), content_type="application/pdf"),
        {},
        parse_idempotency_key(str(uuid.uuid4())),
    )
    paper = Paper.objects.get(pk=uploaded.paper_id)

    barrier = threading.Barrier(2, timeout=15)
    results: list[str] = []
    lock = threading.Lock()

    def run(title: str) -> None:
        from django.contrib.auth import get_user_model

        from apps.common.auth import ActorContext as Ctx
        from apps.literature.services import update_paper as update

        try:
            barrier.wait()
            user = get_user_model().objects.get(pk=user_a.pk)
            update(Ctx(user=user, request_id="req_c"), paper.pk, 1, {"title": title})
            outcome = "ok"
        except ApiError as exc:
            outcome = exc.code
        except Exception as exc:  # noqa: BLE001
            outcome = f"error:{type(exc).__name__}"
        finally:
            connections["default"].close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(f"并发标题{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == ["VERSION_CONFLICT", "ok"], f"乐观锁未生效：{results}"
    paper.refresh_from_db()
    assert paper.version == 2
    assert paper.title in {"并发标题0", "并发标题1"}
