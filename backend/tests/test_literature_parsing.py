"""文献接入与解析测试（docs/mvp/01 第 9 节、docs/mvp/05 T03 / T04 / T09）。

M05「文献接入与文件授权」覆盖：
- PDF 大小 / 页数 / 加密 / 损坏的拒绝与返回码；
- 同项目同 SHA256 去重（不新增、不改 metadata/included、不递增 corpus_revision）；
- 同键同体重放与同键异体冲突；
- 单项目 30 篇上限；
- arXiv 受理账本、部分失败保留成功项、重试只补 pending/failed 且沿用已解析版本。

M06「解析与证据账本」覆盖（末尾 3 条，已从 skip 打开）：
- 页级证据 page_number 从 1 起且与原文一致；
- 无法识别的复杂页显式失败，不补造内容；
- 视觉转录默认 `image_unverified`，应用绝不自动设为 `human_verified`。

更细的切片、locator、跨 Run 引用等断言在 `tests/test_parsing_evidence.py`。
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest import make_pdf_bytes, make_pdf_file, make_text_pdf_file

from apps.common.http import ApiError

pytestmark = pytest.mark.django_db


def _key() -> str:
    return str(uuid.uuid4())


class _Snapshot:
    """`parse_paper` 需要的论文快照；字段与 RunPaper 对齐。"""

    def __init__(self, paper):
        self.paper_id = paper.id
        self.project_id = paper.project_id
        self.file_asset = paper.file_asset
        self.file_sha256 = paper.file_sha256
        self.title = paper.title


def _snapshot(paper):
    return _Snapshot(paper)


def _upload(client, project, *, key=None, file=None, title=None, **extra):
    data = {"file": file if file is not None else make_pdf_file(2)}
    if title is not None:
        data["title"] = title
    data.update(extra)
    return client.post(
        f"/api/v1/projects/{project.id}/papers/upload",
        data,
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=key or _key(),
    )


# ── T03：上传边界与恶意文件 ────────────────────────────────────
def test_pdf_size_limit_is_413(client_a, project_a, settings):
    """超过体积上限返回 413 FILE_TOO_LARGE。

    把上限压到 2 KiB 而不是造 25 MiB 的夹具：要验证的是「超限即拒绝」这条规则，
    不是某个具体字节数，用小上限能让真实 PDF 触发同一条代码路径。
    """
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PDF_MAX_BYTES": 2048}
    oversized = make_pdf_file(120, name="oversized.pdf")
    assert len(oversized.read()) > 2048
    oversized.seek(0)

    response = _upload(client_a, project_a, file=oversized)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_pdf_page_limit_is_422(client_a, project_a, settings):
    """超过页数上限返回 422 PAGE_LIMIT_EXCEEDED；正好等于上限仍然通过。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PDF_MAX_PAGES": 3}

    rejected = _upload(client_a, project_a, file=make_pdf_file(4, name="four.pdf"))
    assert rejected.status_code == 422
    body = rejected.json()["error"]
    assert body["code"] == "PAGE_LIMIT_EXCEEDED"
    assert body["details"]["page_count"] == 4

    accepted = _upload(client_a, project_a, file=make_pdf_file(3, name="three.pdf"))
    assert accepted.status_code == 201
    assert accepted.json()["data"]["page_count"] == 3


def test_encrypted_pdf_is_422(client_a, project_a):
    """加密 PDF 一律拒绝，即使口令是空串也不进入流程。"""
    response = _upload(client_a, project_a, file=make_pdf_file(1, name="locked.pdf", encrypt="secret"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_PDF"


def test_broken_pdf_is_422(client_a, project_a):
    """损坏或扩展名伪装的文件必须可解释地拒绝，且不落库。"""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.literature.models import Paper

    fake = SimpleUploadedFile("fake.pdf", b"this is definitely not a pdf", content_type="application/pdf")
    response = _upload(client_a, project_a, file=fake)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_PDF"
    assert not Paper.objects.exists()
    # 被拒绝的上传不得留下暂存文件
    from apps.common.storage import staging_root

    assert list(staging_root().iterdir()) == []


def test_upload_rejects_path_traversal_filename_without_escaping_storage(client_a, project_a):
    """路径穿越文件名只保留基名，绝不参与磁盘路径拼接。"""
    from apps.literature.models import Paper

    response = _upload(client_a, project_a, file=make_pdf_file(1, name="../../../../etc/passwd.pdf"))
    assert response.status_code == 201
    paper = Paper.objects.get()
    assert paper.file_asset.original_filename == "passwd.pdf"
    # 存储键由服务端生成，不含用户提供名称，也不含路径穿越片段
    assert ".." not in paper.file_asset.storage_key
    assert "passwd" not in paper.file_asset.storage_key


# ── T03 / T05：同文件去重与幂等 ────────────────────────────────
def test_same_file_upload_dedupes_within_project(client_a, project_a):
    """同项目同 SHA256 返回既有 Paper，不新增、不递增语料版本。

    复用既有文献时成功码仍是 201（docs/mvp/03 第 2.2 节），
    并带 X-Resource-Reused 提示；既有 metadata/included 不被覆盖。
    """
    from apps.literature.models import Paper
    from apps.projects.models import Project

    first = _upload(client_a, project_a, file=make_pdf_file(2), title="原始标题")
    assert first.status_code == 201
    assert "X-Resource-Reused" not in first.headers

    paper = Paper.objects.get()
    revision_after_first = Project.objects.get(pk=project_a.pk).corpus_revision

    second = _upload(client_a, project_a, file=make_pdf_file(2), title="另一个标题")
    assert second.status_code == 201
    assert second.headers["X-Resource-Reused"] == "true"
    assert second.json()["data"]["id"] == str(paper.id)
    # 不覆盖既有元数据、不新增、不动语料版本
    assert second.json()["data"]["title"] == "原始标题"
    assert Paper.objects.count() == 1
    assert Project.objects.get(pk=project_a.pk).corpus_revision == revision_after_first


def test_duplicate_upload_does_not_reset_included_state(client_a, project_a):
    """重复上传不得把已取消纳入的文献重新纳入。"""
    from apps.literature.models import Paper

    assert _upload(client_a, project_a, file=make_pdf_file(2)).status_code == 201
    paper = Paper.objects.get()
    excluded = client_a.patch(
        f"/api/v1/papers/{paper.id}",
        {"expected_version": 1, "included": False},
        format="json",
    )
    assert excluded.status_code == 200
    assert excluded.json()["data"]["included"] is False

    again = _upload(client_a, project_a, file=make_pdf_file(2))
    assert again.status_code == 201
    assert again.json()["data"]["included"] is False
    assert again.json()["data"]["version"] == 2


def test_same_idempotency_key_replays_original_response(client_a, project_a):
    """同键同体重放返回原受理响应，不新增文献、不重复建文件。"""
    from apps.literature.models import Paper

    key = _key()
    first = _upload(client_a, project_a, key=key, file=make_pdf_file(2), title="同一个标题")
    second = _upload(client_a, project_a, key=key, file=make_pdf_file(2), title="同一个标题")

    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert Paper.objects.count() == 1
    # 重放不得留下第二份暂存文件
    from apps.common.storage import staging_root

    assert list(staging_root().iterdir()) == []


def test_same_key_with_different_file_is_idempotency_conflict(client_a, project_a):
    """同键不同文件内容返回 409 IDEMPOTENCY_CONFLICT（上传摘要含文件 SHA256）。"""
    from apps.literature.models import Paper

    key = _key()
    assert _upload(client_a, project_a, key=key, file=make_pdf_file(2)).status_code == 201
    conflict = _upload(client_a, project_a, key=key, file=make_pdf_file(3))

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突不产生第二篇文献
    assert Paper.objects.count() == 1


def test_same_key_with_different_title_is_idempotency_conflict(client_a, project_a):
    """元数据也参与摘要比较：同文件不同 title 仍是冲突。"""
    key = _key()
    assert _upload(client_a, project_a, key=key, file=make_pdf_file(2), title="标题一").status_code == 201
    conflict = _upload(client_a, project_a, key=key, file=make_pdf_file(2), title="标题二")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_idempotency_key_is_400(client_a, project_a):
    """标记 K 的上传缺幂等键先在入参校验阶段返回 400。"""
    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(2)},
        format="multipart",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ── 容量上限 ──────────────────────────────────────────────────
def test_paper_limit_is_422(client_a, project_a, settings):
    """第 31 篇返回 422 PAPER_LIMIT_EXCEEDED；取消纳入的文献也占额度。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 2}

    assert _upload(client_a, project_a, file=make_pdf_file(1)).status_code == 201
    second = _upload(client_a, project_a, file=make_pdf_file(2))
    assert second.status_code == 201
    # 把第二篇取消纳入：依然占用额度
    client_a.patch(
        f"/api/v1/papers/{second.json()['data']['id']}",
        {"expected_version": 1, "included": False},
        format="json",
    )

    over = _upload(client_a, project_a, file=make_pdf_file(3))
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "PAPER_LIMIT_EXCEEDED"
    # 重复上传既有文件不受容量限制影响
    again = _upload(client_a, project_a, file=make_pdf_file(1))
    assert again.status_code == 201
    assert again.headers["X-Resource-Reused"] == "true"


# ── T04：arXiv 部分成功与重试 ─────────────────────────────────
def _arxiv_metadata(source_id: str, *, title: str = "论文标题", year: int = 2021):
    from apps.literature.arxiv import SourceMetadata

    return SourceMetadata(
        source_id=source_id,
        title=title,
        authors=["张三", "李四"],
        year=year,
        source_url=f"https://arxiv.org/abs/{source_id}",
    )


def _fake_download(
    *,
    pages: int = 2,
    fail_for: set[str] | None = None,
    body: bytes | None = None,
    bodies: dict[str, bytes] | None = None,
):
    """替身下载器：不发网络请求，也不伪造真实失败语义。

    `bodies` 用于按具体版本给不同内容——真实 arXiv 里两篇不同论文的 PDF 必然不同。
    若替身每次都返回同样的字节，去重逻辑会把它们**正确地**判成「同一份文件」，
    于是容量、重试这些真正想验证的路径就测不到了。
    """

    def download(source_record):
        import io

        from apps.common.storage import stage_file

        if fail_for and source_record.source_id in fail_for:
            raise ApiError("ARXIV_FETCH_FAILED", "下载失败", retryable=True)
        if bodies and source_record.source_id in bodies:
            payload = bodies[source_record.source_id]
        elif body is not None:
            payload = body
        else:
            payload = make_pdf_bytes(pages)
        return stage_file(
            io.BytesIO(payload),
            limit_bytes=25 * 1024 * 1024,
            original_filename=f"{source_record.source_id}.pdf",
            media_type="application/pdf",
            suffix=".pdf",
        )

    return download


def _accepted_job(client, project, ids, *, key=None):
    response = client.post(
        f"/api/v1/projects/{project.id}/imports/arxiv",
        {"arxiv_ids": ids},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or _key(),
    )
    return response


def test_arxiv_accept_returns_202_with_batch_ledger(client_a, project_a, monkeypatch):
    """受理即 202，账本每项 pending，resource_id 为批次 ID（等于首次 Job ID）。"""
    from apps.jobs.models import Job

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    response = _accepted_job(client_a, project_a, ["2401.00001", "2401.00002"])

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["resource_type"] == "import_batch"
    assert data["resource_id"] == data["job_id"]

    job = Job.objects.get(pk=data["job_id"])
    assert job.kind == "import_arxiv" and job.status == "queued"
    assert [item["requested_id"] for item in job.frozen_input["items"]] == [
        "2401.00001",
        "2401.00002",
    ]
    assert all(item["status"] == "pending" for item in job.frozen_input["items"])
    assert all(item["resolved_source_id"] is None for item in job.frozen_input["items"])


@pytest.mark.parametrize(
    "payload",
    [
        {"arxiv_ids": []},
        {"arxiv_ids": ["https://arxiv.org/abs/2401.00001"]},
        {"arxiv_ids": ["not-an-id"]},
        {"arxiv_ids": ["2401.00001", "2401.00001"]},
        {"arxiv_ids": [f"2401.{index:05d}" for index in range(11)]},
    ],
)
def test_arxiv_accept_rejects_invalid_id_sets(client_a, project_a, payload, monkeypatch):
    """数量、重复、URL、格式都必须被拒；不接受任意 URL。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    response = _accepted_job(client_a, project_a, payload["arxiv_ids"])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_arxiv_partial_failure_keeps_successful_items(client_a, project_a, monkeypatch):
    """部分失败时 Job failed，但成功项保留在 Job.result.items。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.literature import services as literature
    from apps.literature.models import Paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = _accepted_job(client_a, project_a, ["2401.00001", "2401.00002"]).json()["data"]

    monkeypatch.setattr(
        literature.arxiv_client,
        "fetch_metadata",
        lambda arxiv_id: _arxiv_metadata(arxiv_id if "v" in arxiv_id else f"{arxiv_id}v2"),
    )
    monkeypatch.setattr(literature.arxiv_client, "download_pdf", _fake_download(fail_for={"2401.00002v2"}))

    job = Job.objects.get(pk=accepted["job_id"])
    context = claim_execution(job.pk)
    literature.execute_import_arxiv(context, job.resource_id)

    job.refresh_from_db()
    assert job.status == "failed"
    assert job.error["code"] == "ARXIV_FETCH_FAILED"
    assert job.error["retryable"] is True

    items = {item["requested_id"]: item for item in job.result["items"]}
    assert items["2401.00001"]["status"] == "imported"
    assert items["2401.00001"]["resolved_source_id"] == "2401.00001v2"
    assert items["2401.00002"]["status"] == "failed"
    assert items["2401.00002"]["error"]["code"] == "ARXIV_FETCH_FAILED"
    # 成功项不回滚
    assert Paper.objects.count() == 1
    assert Paper.objects.get().source == "arxiv"


def test_arxiv_retry_only_fills_pending_and_failed(client_a, project_a, monkeypatch):
    """重试只补 pending/failed，沿用已解析的具体版本，不重复导入成功项。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.literature import services as literature
    from apps.literature.models import Paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = _accepted_job(client_a, project_a, ["2401.00001", "2401.00002"]).json()["data"]
    job = Job.objects.get(pk=accepted["job_id"])

    resolved_calls: list[str] = []

    def fetch(arxiv_id):
        resolved_calls.append(arxiv_id)
        return _arxiv_metadata(arxiv_id if "v" in arxiv_id else f"{arxiv_id}v2")

    monkeypatch.setattr(literature.arxiv_client, "fetch_metadata", fetch)
    monkeypatch.setattr(
        literature.arxiv_client,
        "download_pdf",
        _fake_download(fail_for={"2401.00002v2"}, bodies={"2401.00001v2": make_pdf_bytes(2)}),
    )
    literature.execute_import_arxiv(claim_execution(job.pk), job.resource_id)
    job.refresh_from_db()
    assert job.status == "failed"
    assert Paper.objects.count() == 1

    # 重试：新 Job、同一 resource_id，沿用已解析版本
    retry = client_a.post(f"/api/v1/jobs/{job.id}/retry", format="json", HTTP_IDEMPOTENCY_KEY=_key())
    assert retry.status_code == 202
    retry_data = retry.json()["data"]
    assert retry_data["resource_id"] == accepted["resource_id"]
    assert retry_data["job_id"] != accepted["job_id"]

    retry_job = Job.objects.get(pk=retry_data["job_id"])
    assert retry_job.attempt == 2 and retry_job.retry_of_job_id == job.pk

    # 第二次执行：第一项不再重新解析、不再重新下载
    monkeypatch.setattr(
        literature.arxiv_client,
        "download_pdf",
        _fake_download(bodies={"2401.00002v2": make_pdf_bytes(3)}),
    )
    resolved_calls.clear()
    literature.execute_import_arxiv(claim_execution(retry_job.pk), retry_job.resource_id)

    retry_job.refresh_from_db()
    assert retry_job.status == "succeeded"
    assert resolved_calls == ["2401.00002v2"], "成功项不应被重新解析，只补失败项"
    assert Paper.objects.count() == 2
    by_id = {item["requested_id"]: item for item in retry_job.result["items"]}
    assert by_id["2401.00001"]["status"] == "imported"
    assert by_id["2401.00002"]["status"] == "imported"
    # 旧 Job 历史不被改写
    job.refresh_from_db()
    assert job.status == "failed"
    assert {item["status"] for item in job.result["items"]} == {"imported", "failed"}


def test_arxiv_duplicate_import_is_recorded_as_duplicate(client_a, project_a, monkeypatch):
    """同一具体版本重复导入记为 duplicate，不新建文献。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.literature import services as literature
    from apps.literature.models import Paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(
        literature.arxiv_client, "fetch_metadata", lambda arxiv_id: _arxiv_metadata("2401.00001v1")
    )
    monkeypatch.setattr(literature.arxiv_client, "download_pdf", _fake_download(pages=2))

    first = _accepted_job(client_a, project_a, ["2401.00001"]).json()["data"]
    job = Job.objects.get(pk=first["job_id"])
    literature.execute_import_arxiv(claim_execution(job.pk), job.resource_id)

    second = _accepted_job(client_a, project_a, ["2401.00001"]).json()["data"]
    job2 = Job.objects.get(pk=second["job_id"])
    literature.execute_import_arxiv(claim_execution(job2.pk), job2.resource_id)

    job2.refresh_from_db()
    assert job2.status == "succeeded"
    assert job2.result["items"][0]["status"] == "duplicate"
    assert Paper.objects.count() == 1


def test_arxiv_version_is_resolved_and_frozen_before_download(client_a, project_a, monkeypatch):
    """无版本 ID 先解析成具体版本，持久化后再下载；下载收到的是具体版本。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.literature import services as literature

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(
        literature.arxiv_client, "fetch_metadata", lambda arxiv_id: _arxiv_metadata("2401.00007v3")
    )

    seen: list[str] = []

    def download(source_record):
        seen.append(source_record.source_id)
        return _fake_download()(source_record)

    monkeypatch.setattr(literature.arxiv_client, "download_pdf", download)

    accepted = _accepted_job(client_a, project_a, ["2401.00007"]).json()["data"]
    job = Job.objects.get(pk=accepted["job_id"])
    literature.execute_import_arxiv(claim_execution(job.pk), job.resource_id)

    assert seen == ["2401.00007v3"], "下载必须使用已解析的具体版本"
    job.refresh_from_db()
    assert job.result["items"][0]["resolved_source_id"] == "2401.00007v3"


def test_arxiv_downloaded_pdf_uses_same_validation(client_a, project_a, monkeypatch):
    """导入的 PDF 走与上传同一套校验：坏内容记为失败项，不落库。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.literature import services as literature
    from apps.literature.models import Paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(
        literature.arxiv_client, "fetch_metadata", lambda arxiv_id: _arxiv_metadata(f"{arxiv_id}v1")
    )
    # 替身返回加密 PDF：下载成功但结构校验必须拒绝
    monkeypatch.setattr(
        literature.arxiv_client,
        "download_pdf",
        _fake_download(body=make_pdf_bytes(1, encrypt="pw")),
    )

    accepted = _accepted_job(client_a, project_a, ["2401.00009"]).json()["data"]
    job = Job.objects.get(pk=accepted["job_id"])
    literature.execute_import_arxiv(claim_execution(job.pk), job.resource_id)

    job.refresh_from_db()
    assert job.status == "failed"
    assert job.result["items"][0]["error"]["code"] == "UNSUPPORTED_PDF"
    assert not Paper.objects.exists()
    # 拒绝的下载内容不得留在暂存区
    from apps.common.storage import staging_root

    assert list(staging_root().iterdir()) == []


def test_arxiv_respects_paper_capacity(client_a, project_a, monkeypatch, settings):
    """容量已满时导入项明确失败，不静默丢弃。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.literature import services as literature

    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 1}

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(
        literature.arxiv_client, "fetch_metadata", lambda arxiv_id: _arxiv_metadata(f"{arxiv_id}v1")
    )
    monkeypatch.setattr(
        literature.arxiv_client,
        "download_pdf",
        _fake_download(bodies={"2401.00001v1": make_pdf_bytes(2), "2401.00002v1": make_pdf_bytes(3)}),
    )

    accepted = _accepted_job(client_a, project_a, ["2401.00001", "2401.00002"]).json()["data"]
    job = Job.objects.get(pk=accepted["job_id"])
    literature.execute_import_arxiv(claim_execution(job.pk), job.resource_id)

    job.refresh_from_db()
    assert job.status == "failed"
    items = {item["requested_id"]: item for item in job.result["items"]}
    assert items["2401.00001"]["status"] == "imported"
    assert items["2401.00002"]["status"] == "failed"
    assert items["2401.00002"]["error"]["code"] == "PAPER_LIMIT_EXCEEDED"


def test_arxiv_cancellation_stops_remaining_items(client_a, project_a, user_a, monkeypatch):
    """取消后停止后续项，收尾为 cancelled，未发布的项留在 pending 供重试。

    取消在第一项**下载期间**到达：此时发布检查会发现取消意图，本项产物被拒绝入库。
    因此两项都应保持 pending——第一项没有落地任何东西，重试时要重新处理它。
    """
    from apps.common.auth import ActorContext
    from apps.jobs.models import Job
    from apps.jobs.services import cancel_job, claim_execution
    from apps.literature import services as literature
    from apps.literature.models import Paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(
        literature.arxiv_client, "fetch_metadata", lambda arxiv_id: _arxiv_metadata(f"{arxiv_id}v1")
    )

    accepted = _accepted_job(client_a, project_a, ["2401.00001", "2401.00002"]).json()["data"]
    job_id = Job.objects.get(pk=accepted["job_id"]).pk
    # 取消意图用明确的 actor 表达，不依赖 project_a.owner 的延迟加载
    actor = ActorContext(user=user_a, request_id="req_test")

    downloads: list[str] = []

    def cancelling_download(source_record):
        # 第一项下载时提交取消意图，第二项必须不再开始
        downloads.append(source_record.source_id)
        cancel_job(actor, job_id)
        return _fake_download()(source_record)

    monkeypatch.setattr(literature.arxiv_client, "download_pdf", cancelling_download)

    literature.execute_import_arxiv(claim_execution(job_id), accepted["resource_id"])

    job = Job.objects.get(pk=job_id)
    assert job.status == "cancelled"
    statuses = [item["status"] for item in job.result["items"]]
    # 取消早于发布：本项产物不入库，两项都留 pending 供重试继续，没有项被跳过
    assert statuses == ["pending", "pending"], statuses
    assert len(downloads) == 1, f"取消后仍开始了后续下载：{downloads}"
    assert not Paper.objects.filter(project=project_a).exists(), "取消后仍写入了文献"


# ── M06：解析与证据账本（已实现，见 tests/test_parsing_evidence.py）────
def test_evidence_page_numbers_start_at_one(client_a, project_a):
    """页级证据的 page_number 从 1 起，且片段文本确实来自该页原文。"""
    from apps.literature.models import Paper
    from apps.parsing.evidence import build_chunks
    from apps.parsing.models import EvidenceChunk
    from apps.parsing.pdf import extract_pages
    from apps.parsing.services import parse_paper

    body = "Attention Is All You Need\nAbstract: the dominant sequence model."
    response = _upload(client_a, project_a, file=make_text_pdf_file([body], name="one-page.pdf"))
    assert response.status_code == 201, response.json()
    paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])

    document = parse_paper(
        None,
        _snapshot(paper),
        {},
    )
    chunks = list(EvidenceChunk.objects.filter(parsed_document=document).order_by("ordinal"))
    assert chunks, "没有生成证据片段"
    assert [chunk.ordinal for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert {chunk.page_number for chunk in chunks} == {1}, "页码必须从 1 起且指向真实页"

    # 片段必须能在该页原文中找到，而不是模型改写后的自证文本
    page_text = " ".join(extract_pages(paper.file_asset)[0].text.split())
    for chunk in chunks:
        assert " ".join(chunk.text.split()) in page_text
    assert build_chunks  # 该入口与 parse_paper 共用同一实现


def test_unrecognized_page_fails_explicitly(client_a, project_a):
    """无法识别的复杂页显式失败：不发布产物、不产生片段、不补造内容。"""
    from tests.conftest import make_pdf_file

    from apps.literature.models import Paper
    from apps.parsing.models import EvidenceChunk, ParsedDocument
    from apps.parsing.services import parse_paper

    # 没有文本层的 PDF：解析器读不出内容，也不启用图像辅助
    response = _upload(client_a, project_a, file=make_pdf_file(1, name="no-text.pdf"))
    assert response.status_code == 201, response.json()
    paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])

    with pytest.raises(ApiError) as error:
        parse_paper(None, _snapshot(paper), {})

    assert error.value.code == "PDF_PARSE_FAILED"
    assert [item["page_number"] for item in error.value.details["pages"]] == [1]
    assert not ParsedDocument.objects.exists(), "失败时不得发布解析产物"
    assert not EvidenceChunk.objects.exists(), "失败时不得产生任何片段"


def test_image_transcription_is_unverified_by_default(client_a, project_a, monkeypatch):
    """视觉转录的 verification=image_unverified，应用绝不自动设为 human_verified。"""
    import uuid as uuid_module

    from tests.conftest import make_pdf_file

    from apps.literature.models import Paper
    from apps.parsing import services as parsing_services
    from apps.parsing.models import (
        VERIFICATION_HUMAN_VERIFIED,
        VERIFICATION_IMAGE_UNVERIFIED,
        EvidenceChunk,
    )

    response = _upload(client_a, project_a, file=make_pdf_file(1, name="scan.pdf"))
    assert response.status_code == 201, response.json()
    paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])

    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution

    job = Job.objects.create(
        project_id=paper.project_id,
        requested_by=paper.project.owner,
        kind="analyze",
        resource_type="analysis_run",
        resource_id=uuid_module.uuid4(),
        input_hash="0" * 64,
    )
    context = claim_execution(job.pk)

    class _FakeResult:
        payload = {"elements": [{"element_type": "text", "text": "Scanned line", "section": None}]}

    monkeypatch.setattr(parsing_services, "call_structured", lambda ctx, req: _FakeResult())

    document = parsing_services.parse_paper(context, _snapshot(paper), {})
    chunks = EvidenceChunk.objects.filter(parsed_document=document)
    assert chunks.exists(), "图像辅助路径没有产出片段"
    for chunk in chunks:
        assert chunk.verification == VERIFICATION_IMAGE_UNVERIFIED
        assert chunk.verification != VERIFICATION_HUMAN_VERIFIED
        assert chunk.original_text is None, "图像转录不得伪称与原文匹配"
