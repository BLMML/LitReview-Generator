"""M05 复核问题的回归测试（两轮独立复核）。

这些用例来自 2026-09-29 的两轮独立复核：第一轮 10 项边界全部失败，第二轮又发现
5 项历史数据兼容与导入边界问题。原有测试通过不能证明这些边界正确，
回归时不许放宽断言。

第一轮（[复核问题清单](docs/mvp/M05_复核问题清单_2026-09-29.md)）：
- P1 失效租约的 worker 不得写入文献（发布前在同一事务内复核执行资格）；
- P1 项目有活跃任务时上传 / 编辑必须 409 PROJECT_BUSY（幂等重放除外）；
- P2 授权下载的 MIME 不由上传者声明的 Content-Type 决定；
- P2 下载按字节上限**流式**中断，不先读完整包；
- P2 幂等重放返回第一次受理的原响应，不重新序列化当前数据；
- P2 连续重试（中间一次排队期间被取消）仍保留逐项账本与已冻结版本；
- P2 受理即暴露逐项 pending 账本（契约要求 `Job.result.items` 受理时存在）；
- P2 越权查询项目文献列表是 404，不是「200 + 空列表」；
- P2 重复上传立即清理本次暂存文件。

第二轮（[第二轮复核](docs/mvp/M05_第二轮复核_2026-09-29.md)）：
- 历史资产的错误 MIME 不得决定下载类型（旧 `text/html` 也必须按 PDF 返回）；
- 租约**过期**（token 未变）时不得再改写导入账本，终态任务同样不可覆盖；
- 旧形态数据（中间尝试 `result=None`）的账本恢复不得丢掉成功项与冻结版本；
- 满容量时同一份 PDF 应按内容复用为 duplicate，只有真正新增才拒绝；
- 总耗时上限覆盖重定向与响应头等待，而不只是正文读取。
"""

from __future__ import annotations

import asyncio
import copy
import io
import threading
import time
import uuid
from typing import Any

import httpx
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from tests.conftest import make_pdf_bytes

from apps.common.auth import ActorContext
from apps.common.http import ApiError
from apps.common.storage import stage_file, staging_root
from apps.jobs.models import Job
from apps.jobs.services import claim_execution, retry_job
from apps.literature import arxiv, services
from apps.literature.models import Paper

pytestmark = pytest.mark.django_db


def _upload(client, project, *, key=None, content_type="application/pdf", title="Original title"):
    return client.post(
        f"/api/v1/projects/{project.pk}/papers/upload",
        {
            "file": SimpleUploadedFile("paper.pdf", make_pdf_bytes(), content_type=content_type),
            "title": title,
        },
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def _active_job(project, user):
    return Job.objects.create(
        project=project,
        requested_by=user,
        kind="import_arxiv",
        resource_type="import_batch",
        resource_id=uuid.uuid4(),
        input_hash="0" * 64,
        status="queued",
    )


def _fake_metadata():
    return arxiv.SourceMetadata(
        "2401.00001v1", "Example", ["Author"], 2024, "https://arxiv.org/abs/2401.00001v1"
    )


def _stage_fake_pdf(record):
    """替身下载器：返回与 conftest `make_pdf_bytes()` 相同的字节。"""
    return stage_file(
        io.BytesIO(make_pdf_bytes()),
        limit_bytes=25 * 1024 * 1024,
        original_filename=f"{record.source_id}.pdf",
        media_type="application/pdf",
        suffix=".pdf",
    )


def _make_paper(project, *, sha256: str):
    """直接建一篇最小 Paper（占用容量用，不涉及真实文件）。"""
    from apps.common.models import FileAsset

    asset = FileAsset.objects.create(
        project=project,
        storage_key=f"test/{uuid.uuid4().hex}",
        purpose="original_pdf",
        media_type="application/pdf",
        original_filename="occupier.pdf",
        sha256=sha256,
        size_bytes=1024,
    )
    return Paper.objects.create(
        project=project,
        file_asset=asset,
        title="占用容量的文献",
        file_sha256=sha256,
        page_count=1,
    )


# ── P1：忙碌项目与失效租约 ────────────────────────────────────
def test_upload_must_reject_busy_project(client_a, project_a, user_a):
    """项目有活跃任务时上传返回 409 PROJECT_BUSY，而不是静默受理。"""
    _active_job(project_a, user_a)
    response = _upload(client_a, project_a)
    assert response.status_code == 409, f"忙碌项目仍接受上传：HTTP {response.status_code}"
    assert response.json()["error"]["code"] == "PROJECT_BUSY"


def test_edit_must_reject_busy_project(client_a, project_a, user_a):
    first = _upload(client_a, project_a).json()["data"]
    _active_job(project_a, user_a)
    response = client_a.patch(
        f"/api/v1/papers/{first['id']}",
        {"expected_version": first["version"], "included": False},
        format="json",
    )
    assert response.status_code == 409, f"忙碌项目仍接受编辑：HTTP {response.status_code}"
    assert response.json()["error"]["code"] == "PROJECT_BUSY"


def test_idempotent_replay_still_works_while_project_is_busy(client_a, project_a, user_a):
    """幂等重放优先于忙碌判断：同键同体重放不受 PROJECT_BUSY 影响。

    这是 P1 修复必须保留的例外——契约明确「相同幂等键的重放」不算新写入。
    """
    key = str(uuid.uuid4())
    first = _upload(client_a, project_a, key=key)
    assert first.status_code == 201
    _active_job(project_a, user_a)

    replay = _upload(client_a, project_a, key=key)
    assert replay.status_code == 201, f"重放被忙碌判断误伤：{replay.status_code}"
    assert replay.json()["data"] == first.json()["data"]


def test_stale_worker_cannot_register_downloaded_paper(project_a, user_a, monkeypatch):
    """租约在下载期间被替换后，旧 worker 不得再写入 Paper。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = services.import_arxiv(
        ActorContext(user_a, "req_review"), project_a.pk, ["2401.00001"], uuid.uuid4()
    )
    context = claim_execution(accepted.job_id)
    metadata = arxiv.SourceMetadata(
        "2401.00001v1", "Example", ["Author"], 2024, "https://arxiv.org/abs/2401.00001v1"
    )
    monkeypatch.setattr(arxiv, "fetch_metadata", lambda requested: metadata)

    def download(record):
        # 模拟恢复扫描在旧 worker 等待网络期间接管：token 递增、状态判死
        Job.objects.filter(pk=context.job_id).update(lease_token=context.lease_token + 1, status="failed")
        return stage_file(
            io.BytesIO(make_pdf_bytes()),
            limit_bytes=25 * 1024 * 1024,
            original_filename="download.pdf",
            media_type="application/pdf",
            suffix=".pdf",
        )

    monkeypatch.setattr(arxiv, "download_pdf", download)
    with pytest.raises(ApiError):
        services.execute_import_arxiv(context, accepted.resource_id)

    count = Paper.objects.filter(project=project_a).count()
    assert count == 0, f"失效租约的 worker 仍写入了 {count} 篇文献"
    # 暂存文件也不得留下
    assert list(staging_root().iterdir()) == []


def test_cancelled_worker_cannot_register_downloaded_paper(project_a, user_a, monkeypatch):
    """取消已提交后，本次下载的产物不得再入库。"""
    from apps.jobs.services import cancel_job

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    actor = ActorContext(user_a, "req_review")
    accepted = services.import_arxiv(actor, project_a.pk, ["2401.00001"], uuid.uuid4())
    context = claim_execution(accepted.job_id)
    metadata = arxiv.SourceMetadata("2401.00001v1", "Example", [], 2024, "https://arxiv.org/abs/2401.00001v1")
    monkeypatch.setattr(arxiv, "fetch_metadata", lambda requested: metadata)

    def download(record):
        cancel_job(actor, context.job_id)
        return stage_file(
            io.BytesIO(make_pdf_bytes()),
            limit_bytes=25 * 1024 * 1024,
            original_filename="download.pdf",
            media_type="application/pdf",
            suffix=".pdf",
        )

    monkeypatch.setattr(arxiv, "download_pdf", download)
    services.execute_import_arxiv(context, accepted.resource_id)

    job = Job.objects.get(pk=context.job_id)
    assert job.status == "cancelled"
    assert not Paper.objects.filter(project=project_a).exists()
    assert list(staging_root().iterdir()) == []


# ── P2：MIME、流式上限、幂等快照 ──────────────────────────────
def test_pdf_download_mime_is_not_controlled_by_uploader(client_a, project_a):
    """上传者声明 text/html 也不能让下载响应变成 text/html。"""
    response = _upload(client_a, project_a, content_type="text/html")
    assert response.status_code == 201
    paper_id = response.json()["data"]["id"]

    # 先取存储类型：流式响应消费完之后连接可能已被回收，查询要放在前面
    from apps.common.models import FileAsset

    paper = Paper.objects.get(pk=paper_id)
    asset = FileAsset.objects.get(pk=paper.file_asset_id)
    assert asset.media_type == "application/pdf", "存储类型仍是上传者声明的值"

    download = client_a.get(f"/api/v1/papers/{paper_id}/file")
    try:
        assert download["Content-Type"] == "application/pdf", download["Content-Type"]
    finally:
        download.close()


def test_download_limit_stops_stream_early(settings, monkeypatch):
    """超过字节上限时必须中途停止读取，而不是先读完再判断。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PDF_MAX_BYTES": 4096}
    consumed: list[int] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(20):
                consumed.append(index)
                yield b"%PDF-" + b"x" * 4091

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf"}, stream=Body(), request=request
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        arxiv.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, trust_env=False, **kwargs),
    )
    metadata = arxiv.SourceMetadata("2401.00001v1", "Example", [], 2024, "https://arxiv.org/abs/2401.00001v1")
    with pytest.raises(ApiError) as error:
        arxiv.download_pdf(metadata)
    assert error.value.code == "FILE_TOO_LARGE"
    assert len(consumed) <= 2, f"读了 {len(consumed)} 块才检查上限"
    # 半成品必须清掉
    assert list(staging_root().iterdir()) == []


def test_upload_replay_preserves_original_data(client_a, project_a):
    """重放返回第一次受理的原响应，不被后续编辑污染。"""
    key = str(uuid.uuid4())
    first = _upload(client_a, project_a, key=key).json()["data"]

    edited = client_a.patch(
        f"/api/v1/papers/{first['id']}",
        {"expected_version": first["version"], "title": "Later title"},
        format="json",
    )
    assert edited.status_code == 200 and edited.json()["data"]["version"] == 2

    again = _upload(client_a, project_a, key=key).json()["data"]
    assert again == first, f"重放被改写：{again['title']} / v{again['version']}"


# ── P2：账本初始化与连续重试 ──────────────────────────────────
def test_accepted_import_exposes_pending_items(client_a, project_a, monkeypatch):
    """受理即暴露 pending 账本（03 §2.2 要求 Job.result.items 受理时就存在）。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = client_a.post(
        f"/api/v1/projects/{project_a.pk}/imports/arxiv",
        {"arxiv_ids": ["2401.00001"]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert accepted.status_code == 202
    job_id = accepted.json()["data"]["job_id"]

    detail = client_a.get(f"/api/v1/jobs/{job_id}").json()["data"]
    items = (detail.get("result") or {}).get("items")
    assert items, f"排队中的 Job 没有 result.items：{detail.get('result')!r}"
    assert items[0]["status"] == "pending"
    assert items[0]["requested_id"] == "2401.00001"
    assert items[0]["resolved_source_id"] is None


def test_retry_after_queued_cancel_preserves_import_ledger(project_a, user_a, monkeypatch):
    """J1 部分成功 → J2 排队时被取消（从未执行）→ J3 重试仍保留账本。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    actor = ActorContext(user_a, "req_review")
    first = services.import_arxiv(actor, project_a.pk, ["2401.00001", "2401.00002"], uuid.uuid4())
    job = Job.objects.get(pk=first.job_id)
    ledger = [
        {
            "requested_id": "2401.00001",
            "resolved_source_id": "2401.00001v2",
            "status": "imported",
            "paper_id": str(uuid.uuid4()),
            "error": None,
        },
        {
            "requested_id": "2401.00002",
            "resolved_source_id": "2401.00002v2",
            "status": "failed",
            "paper_id": None,
            "error": {"code": "ARXIV_FETCH_FAILED", "message": "timeout", "retryable": True},
        },
    ]
    Job.objects.filter(pk=job.pk).update(status="failed", result={"items": ledger})
    second = retry_job(actor, job.pk, uuid.uuid4())
    # 排队期间被取消：领域 worker 从未执行，它的 result 一直是 null
    Job.objects.filter(pk=second.job_id).update(status="cancelled")
    third = retry_job(actor, second.job_id, uuid.uuid4())

    restored = services._load_items(Job.objects.get(pk=third.job_id))
    assert restored[0]["status"] == "imported", f"成功项被重置：{restored[0]}"
    assert restored[1]["resolved_source_id"] == "2401.00002v2", "已冻结版本丢失"

    # 新尝试也必须立即暴露同一份账本，且进度不回到 0
    retry_job_row = Job.objects.get(pk=third.job_id)
    assert (retry_job_row.result or {}).get("items"), "重试任务没有立即可见的账本"
    assert retry_job_row.progress.get("completed") == 1, retry_job_row.progress
    assert retry_job_row.progress.get("total") == 2


# ── P2：越权列表与暂存清理 ────────────────────────────────────
def test_foreign_project_paper_list_is_404(client_b, project_a):
    """他人项目必须是 404，不能是「200 + 空列表」。"""
    response = client_b.get(f"/api/v1/projects/{project_a.pk}/papers")
    assert response.status_code == 404, f"越权查询返回 HTTP {response.status_code}"
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_missing_project_paper_list_is_404(client_a):
    response = client_a.get(f"/api/v1/projects/{uuid.uuid4()}/papers")
    assert response.status_code == 404


def test_duplicate_upload_cleans_its_staged_file(client_a, project_a):
    """重复上传（换新 key）必须立即清理本次暂存文件。"""
    assert _upload(client_a, project_a).status_code == 201
    assert _upload(client_a, project_a).status_code == 201
    leftovers = list(staging_root().iterdir())
    assert not leftovers, f"重复上传留下 {len(leftovers)} 个暂存文件"
    assert Paper.objects.count() == 1


# ── 第二轮复核：历史数据兼容与导入边界 ────────────────────────
def test_existing_pdf_asset_always_downloads_as_pdf(client_a, project_a):
    """历史资产的错误 MIME 不得决定下载类型。

    早期版本的 `FileAsset.media_type` 来自上传者声明的 `Content-Type`，
    库里可能存着 `text/html`。契约保证这个端点只返回 PDF，所以响应类型必须固定，
    而不能沿用历史数据。只测新上传通过不足以证明旧资产安全。
    """
    from apps.common.models import FileAsset

    first = _upload(client_a, project_a)
    assert first.status_code == 201
    paper = Paper.objects.get(pk=first.json()["data"]["id"])
    # 模拟修复前遗留的持久状态
    FileAsset.objects.filter(pk=paper.file_asset_id).update(media_type="text/html")

    response = client_a.get(f"/api/v1/papers/{paper.pk}/file")
    try:
        assert response["Content-Type"] == "application/pdf", response["Content-Type"]
    finally:
        response.close()


def test_expired_lease_does_not_overwrite_import_ledger(project_a, user_a, monkeypatch):
    """租约过期后不得再改写导入账本（token 未变的窗口期）。

    文献写入已被发布检查拦住，但账本写入若只比较 token，仍会在已判死的任务上
    留下新的进度。恢复扫描尚未递增 token 时这个窗口是真实存在的。
    """
    import copy
    from datetime import timedelta

    from django.utils import timezone

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = services.import_arxiv(
        ActorContext(user_a, "req_review"), project_a.pk, ["2401.00001"], uuid.uuid4()
    )
    context = claim_execution(accepted.job_id)
    initial = copy.deepcopy(Job.objects.get(pk=context.job_id).result)

    def resolve(requested):
        # 解析期间租约到期，但 token 还没被恢复扫描替换
        Job.objects.filter(pk=context.job_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        return arxiv.SourceMetadata("2401.00001v1", "Example", [], 2024, "https://arxiv.org/abs/2401.00001v1")

    monkeypatch.setattr(arxiv, "fetch_metadata", resolve)
    monkeypatch.setattr(arxiv, "download_pdf", _stage_fake_pdf)
    with pytest.raises(ApiError):
        services.execute_import_arxiv(context, accepted.resource_id)

    actual = Job.objects.get(pk=context.job_id).result
    assert actual == initial, f"过期租约改写了账本：{actual}"


def test_terminal_job_ledger_cannot_be_overwritten(project_a, user_a, monkeypatch):
    """终态任务的账本不得被再次写入。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = services.import_arxiv(
        ActorContext(user_a, "req_review"), project_a.pk, ["2401.00001"], uuid.uuid4()
    )
    context = claim_execution(accepted.job_id)
    Job.objects.filter(pk=context.job_id).update(status="cancelled")
    assert services._lease_writable(context, allow_cancelling=False) is False
    assert services._lease_writable(context, allow_cancelling=True) is False


def test_full_project_arxiv_same_pdf_is_duplicate(client_a, project_a, user_a, settings, monkeypatch):
    """满容量时，导入同一份 PDF 应按内容复用，而不是被容量超限拒绝。

    去重是「同项目同内容」，与来源无关：同一份 PDF 可能早已以「上传」来源存在。
    """
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 1}
    assert _upload(client_a, project_a).status_code == 201

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(arxiv, "fetch_metadata", lambda requested: _fake_metadata())
    monkeypatch.setattr(arxiv, "download_pdf", _stage_fake_pdf)

    accepted = services.import_arxiv(
        ActorContext(user_a, "req_review"), project_a.pk, ["2401.00001"], uuid.uuid4()
    )
    context = claim_execution(accepted.job_id)
    services.execute_import_arxiv(context, accepted.resource_id)

    job = Job.objects.get(pk=context.job_id)
    assert job.result["items"][0]["status"] == "duplicate", job.result
    assert Paper.objects.filter(project=project_a).count() == 1


def test_full_project_arxiv_new_pdf_still_rejected(project_a, user_a, settings, monkeypatch):
    """满容量且内容确实是新的：仍然拒绝，不因上一条修复而放宽。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 1}
    _make_paper(project_a, sha256="a" * 64)

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(arxiv, "fetch_metadata", lambda requested: _fake_metadata())
    monkeypatch.setattr(arxiv, "download_pdf", _stage_fake_pdf)

    accepted = services.import_arxiv(
        ActorContext(user_a, "req_review"), project_a.pk, ["2401.00001"], uuid.uuid4()
    )
    context = claim_execution(accepted.job_id)
    services.execute_import_arxiv(context, accepted.resource_id)

    job = Job.objects.get(pk=context.job_id)
    items = job.result["items"]
    assert items[0]["status"] == "failed", items
    assert items[0]["error"]["code"] == "PAPER_LIMIT_EXCEEDED"
    assert Paper.objects.filter(project=project_a).count() == 1


def test_legacy_queued_cancel_does_not_reset_inherited_ledger(project_a, user_a, monkeypatch):
    """旧数据形态：中间尝试 result=None，账本恢复仍不得丢掉成功项与冻结版本。

    新版 retry_job 会自动继承账本，所以现有用例覆盖不到「result 真的是 null」的
    历史状态。这里显式把中间尝试改回旧形态，验证 `_load_items` 不会被它的
    初始 frozen_input 截断更早的真实进度。
    """
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    actor = ActorContext(user_a, "req_review")
    first = services.import_arxiv(actor, project_a.pk, ["2401.00001", "2401.00002"], uuid.uuid4())
    ledger = [
        {
            "requested_id": "2401.00001",
            "resolved_source_id": "2401.00001v2",
            "status": "imported",
            "paper_id": str(uuid.uuid4()),
            "error": None,
        },
        {
            "requested_id": "2401.00002",
            "resolved_source_id": "2401.00002v2",
            "status": "failed",
            "paper_id": None,
            "error": {"code": "ARXIV_FETCH_FAILED"},
        },
    ]
    Job.objects.filter(pk=first.job_id).update(status="failed", result={"items": ledger})

    second = retry_job(actor, first.job_id, uuid.uuid4())
    # 旧版本留下的正是这种「排队后取消、从未执行」的形态
    Job.objects.filter(pk=second.job_id).update(status="cancelled", result=None)
    third = retry_job(actor, second.job_id, uuid.uuid4())
    job = Job.objects.get(pk=third.job_id)
    assert job.result["items"] == ledger, "新受理未继承祖先账本"

    loaded = services._load_items(job)
    assert loaded[0]["status"] == "imported", f"历史祖先的进度被重置：{loaded}"
    assert loaded[0]["resolved_source_id"] == "2401.00001v2"
    assert loaded[1]["resolved_source_id"] == "2401.00002v2"


def test_total_download_deadline_includes_redirects(settings, monkeypatch):
    """总耗时上限覆盖重定向与响应头等待，而不只是正文读取。

    每一跳都低于单次请求超时，累计却超过总上限——只对正文计时挡不住这种链路。
    """
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "ARXIV_DOWNLOAD_TIMEOUT_SECONDS": 0.3}
    calls: list[str] = []
    cancelled: list[str] = []

    async def handler(request):
        calls.append(str(request.url))
        try:
            # 每跳 0.18 秒低于初始 0.3 秒上限，第二跳应被剩余总预算取消。
            await asyncio.sleep(0.18)
        except asyncio.CancelledError:
            cancelled.append(str(request.url))
            raise
        if len(calls) <= 2:
            return httpx.Response(
                302, headers={"Location": f"https://arxiv.org/pdf/hop{len(calls)}"}, request=request
            )
        return httpx.Response(200, content=make_pdf_bytes(), request=request)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        arxiv.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, trust_env=False, **kwargs),
    )

    started = time.monotonic()
    with pytest.raises(ApiError) as error:
        arxiv.download_pdf(_fake_metadata())
    elapsed = time.monotonic() - started
    assert error.value.code == "ARXIV_FETCH_FAILED"
    assert len(calls) <= 2, f"超出总时限后仍继续请求：{calls}"
    assert cancelled, "到期时没有取消仍在等待响应头的请求"
    assert elapsed < 0.75, f"总预算 0.3 秒，实际用了 {elapsed:.3f} 秒"
    assert list(staging_root().iterdir()) == [], "超时的半成品没有清理"


def test_slow_body_read_is_cancelled_at_total_deadline(settings, monkeypatch):
    """正文读取中被阻塞时也能按期取消，而不是等 read timeout 走完。

    「每跳用剩余 timeout」不足以防住这种情形：HTTPX 的 read timeout 是**每次读操作**
    的间隔上限，服务端持续缓慢出数据就能一直续命。只有可取消的整体截止时间能中断它。
    """
    budget = 0.5
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "ARXIV_DOWNLOAD_TIMEOUT_SECONDS": budget}
    cancelled: list[str] = []

    async def handler(request):
        async def body():
            yield b"%PDF-1.4\n"  # 先给一块，随后长时间不再产出
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled.append("body")
                raise
            yield b"tail"

        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            stream=_SlowBodyStream(body()),
            request=request,
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        arxiv.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, trust_env=False, **kwargs),
    )

    started = time.monotonic()
    with pytest.raises(ApiError) as error:
        arxiv.download_pdf(_fake_metadata())
    elapsed = time.monotonic() - started

    assert error.value.code == "ARXIV_FETCH_FAILED"
    assert cancelled, "正文阻塞的读取没有被取消"
    assert elapsed < budget * 3, f"总预算 {budget} 秒，正文阻塞下用了 {elapsed:.3f} 秒"
    assert list(staging_root().iterdir()) == [], "超时的半成品没有清理"


class _SlowBodyStream(httpx.AsyncByteStream):
    """把异步生成器包成 httpx 可消费的响应体，便于模拟「缓慢出数据」。"""

    def __init__(self, source) -> None:
        self._source = source

    async def __aiter__(self):
        async for chunk in self._source:
            yield chunk


@pytest.mark.django_db(transaction=True)
def test_ledger_write_holds_job_row_lock_across_check_and_update(project_a, user_a, monkeypatch):
    """账本写入必须把 Job 行锁持有到 UPDATE 提交，而不是校验完就释放。

    第三轮复核指出的并发漏洞：修复前 `_lease_writable` 在自己那个事务里取得行锁，
    **校验完就提交释放**，随后的 UPDATE 只比对 job id 与 `lease_token`（取消不改 token），
    于是取消可以挤进「校验通过」与「UPDATE」之间先落地。

    本用例在 `_lease_writable` 返回的**那一瞬间**用独立连接探测该行是否仍被锁住：
    探测语句只有真的阻塞（PostgreSQL 报 `wait_event_type = Lock`）才说明行锁
    被持有到了写入阶段。若探测立刻成功，说明锁已在校验结束时释放——
    校验与 UPDATE 不在同一事务，漏洞窗口重新出现。

    探测用独立的 psycopg 连接（不是 Django 的 `default`），
    避免为了一个测量动作改动全局 DATABASES 配置。
    """
    from datetime import timedelta

    from django.conf import settings as django_settings
    from django.db import connections
    from django.utils import timezone

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    actor = ActorContext(user_a, "req_round3")
    accepted = services.import_arxiv(actor, project_a.pk, ["2401.00001"], uuid.uuid4())
    context = claim_execution(accepted.job_id)
    row = Job.objects.get(pk=context.job_id)
    items = copy.deepcopy(row.result["items"])
    items[0]["resolved_source_id"] = "2401.00001v1"

    probe_now = threading.Event()
    lock_seen = threading.Event()

    def probe() -> None:
        """独立连接尝试锁同一行；阻塞即证明写入方仍持有行锁。"""
        import psycopg

        try:
            if not probe_now.wait(15):
                return
            config = django_settings.DATABASES["default"]
            with psycopg.connect(
                dbname=str(config["NAME"]),
                user=str(config["USER"]),
                password=str(config["PASSWORD"]),
                host=str(config["HOST"]),
                port=str(config["PORT"]),
                connect_timeout=5,
            ) as conn:
                conn.autocommit = True
                with conn.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '1500ms'")
                    try:
                        cursor.execute(
                            "SELECT id FROM jobs_job WHERE id = %s FOR UPDATE", [row.pk]
                        )
                    except Exception:  # noqa: BLE001 - 等锁超时 = 确实被占用
                        lock_seen.set()
                        return
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = pg_backend_pid()"
                    )
                    if (cursor.fetchone() or [None])[0] == "Lock":
                        lock_seen.set()
        except Exception:  # noqa: BLE001 - 探针失败不应误判为通过
            return

    original_check = services._lease_writable

    def wrapped(ctx, *, allow_cancelling):
        allowed = original_check(ctx, allow_cancelling=allow_cancelling)
        # 校验刚刚返回：此刻探测行锁，正好捕获「校验后释放锁」的旧写法
        probe_now.set()
        lock_seen.wait(4)
        return allowed

    prober = threading.Thread(target=probe, daemon=True)
    prober.start()
    monkeypatch.setattr(services, "_lease_writable", wrapped)

    written: list[bool] = []

    def write_ledger():
        try:
            written.append(services._save_items(context, row, items))
        finally:
            connections.close_all()

    worker = threading.Thread(target=write_ledger, daemon=True)
    worker.start()
    worker.join(timeout=25)
    prober.join(timeout=5)

    assert written == [True], f"账本写入未成功：{written}"
    assert lock_seen.is_set(), (
        "资格校验返回时 Job 行锁已被释放：校验与 UPDATE 不在同一事务，"
        "取消请求可以插进两者之间"
    )

    refreshed = Job.objects.get(pk=row.pk)
    assert refreshed.result["items"] == items
    assert refreshed.lease_token == context.lease_token, "本用例不应改变租约 token"
    assert refreshed.lease_expires_at > timezone.now() - timedelta(seconds=1), "租约不应已过期"


@pytest.mark.django_db(transaction=True)
def test_retry_acceptance_waits_for_inflight_ledger_write(project_a, user_a, monkeypatch):
    """受理重试必须等上一次尝试的账本写完，否则会读到过期的 result。

    这条顺序是「重试继承账本」的前提：`retry_job` 在项目空闲检查之后读
    `previous.result`，必须在 Project 行锁**之内**重读，否则正在收尾的
    上一次尝试可能晚一步提交，新尝试就会继承到空账本。
    """
    from django.db import connections
    from django.db import transaction as db_transaction

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    actor = ActorContext(user_a, "req_round3")
    accepted = services.import_arxiv(actor, project_a.pk, ["2401.00001"], uuid.uuid4())
    job = Job.objects.get(pk=accepted.job_id)
    Job.objects.filter(pk=job.pk).update(status="failed")

    ledger = [
        {
            "requested_id": "2401.00001",
            "resolved_source_id": "2401.00001v2",
            "status": "imported",
            "paper_id": str(uuid.uuid4()),
            "error": None,
        }
    ]
    retried: list[Any] = []

    def retry():
        try:
            retried.append(retry_job(actor, job.pk, uuid.uuid4()))
        except Exception as exc:  # noqa: BLE001 - 失败也要让断言看到
            retried.append(exc)
        finally:
            connections.close_all()

    with db_transaction.atomic():
        # 模拟上一次尝试仍在写账本：持有 Job 行锁并尚未提交新 result
        Job.objects.select_for_update().get(pk=job.pk)
        thread = threading.Thread(target=retry, daemon=True)
        thread.start()
        time.sleep(0.3)
        assert retried == [], "重试受理没有等待正在写入的账本"
        Job.objects.filter(pk=job.pk).update(result={"items": ledger})

    thread.join(timeout=10)

    assert len(retried) == 1 and not isinstance(retried[0], Exception), retried
    new_job = Job.objects.get(pk=retried[0].job_id)
    items = (new_job.result or {}).get("items")
    assert items == ledger, f"新尝试没有继承已提交的账本：{items}"
