"""契约与认证测试（docs/mvp/01 第 9 节）。

覆盖：路由无尾斜杠、统一 envelope、CSRF 含登录、未登录 401、越权 404、
未知字段拒绝、分页。

骨架阶段只覆盖已经实现的接口（accounts / projects / jobs 读 / 路由表）；
其余端点在实现后按同一模式补齐。
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest import make_pdf_file

pytestmark = pytest.mark.django_db


def _upload(client, project, *, key=None, file=None, title=None, **extra):
    """带合法幂等键的上传；未显式传 key 时每次生成新键。"""
    data = {"file": file if file is not None else make_pdf_file(2)}
    if title is not None:
        data["title"] = title
    data.update(extra)
    return client.post(
        f"/api/v1/projects/{project.id}/papers/upload",
        data,
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


# ── 认证闭环 ──────────────────────────────────────────────────
def test_csrf_endpoint_is_anonymous_and_returns_token(csrf_client, api_v1):
    response = csrf_client.get(f"{api_v1}/auth/csrf")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["csrf_token"]
    assert body["meta"]["request_id"].startswith("req_")
    assert "csrftoken" in response.cookies


def test_login_requires_csrf_token(csrf_client, api_v1, user_a):
    """CSRF 无效为 403，包括匿名 POST 登录。"""
    response = csrf_client.post(
        f"{api_v1}/auth/login",
        {"username": "alice", "password": "test-pass-9f3a"},
        format="json",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_login_with_csrf_succeeds_and_me_returns_user(csrf_client, api_v1, user_a):
    token = csrf_client.get(f"{api_v1}/auth/csrf").json()["data"]["csrf_token"]
    response = csrf_client.post(
        f"{api_v1}/auth/login",
        {"username": "alice", "password": "test-pass-9f3a"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["username"] == "alice"
    assert data["display_name"] == "alice"
    # 用户 ID 对外是字符串，不假设是 UUID
    assert isinstance(data["id"], str)

    me = csrf_client.get(f"{api_v1}/auth/me")
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "alice"


def test_login_with_wrong_password_is_401(csrf_client, api_v1, user_a):
    token = csrf_client.get(f"{api_v1}/auth/csrf").json()["data"]["csrf_token"]
    response = csrf_client.post(
        f"{api_v1}/auth/login",
        {"username": "alice", "password": "wrong-password"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_login_rejects_unknown_fields(csrf_client, api_v1, user_a):
    token = csrf_client.get(f"{api_v1}/auth/csrf").json()["data"]["csrf_token"]
    response = csrf_client.post(
        f"{api_v1}/auth/login",
        {"username": "alice", "password": "test-pass-9f3a", "is_superuser": True},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "path",
    ["/auth/me", "/projects"],
)
def test_anonymous_protected_endpoints_are_401(api_client, api_v1, path):
    """未登录统一 401，且不返回业务资源信息。"""
    response = api_client.get(f"{api_v1}{path}")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "AUTH_REQUIRED"
    assert "data" not in body


# ── 路由与 envelope ───────────────────────────────────────────
def test_trailing_slash_is_not_redirected(api_client, api_v1):
    """路由无尾斜杠；写请求也不靠重定向补齐地址。"""
    response = api_client.get(f"{api_v1}/auth/me/")
    assert response.status_code == 404
    # 兜底处理器仍返回标准错误 JSON，而不是 HTML 错误页
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_unknown_route_returns_json_error(api_client, api_v1):
    response = api_client.get(f"{api_v1}/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_request_id_is_echoed(api_client, api_v1):
    response = api_client.get(f"{api_v1}/auth/csrf", HTTP_X_REQUEST_ID="trace-abc-123")
    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "trace-abc-123"
    assert response.json()["meta"]["request_id"] == "trace-abc-123"


# ── 项目 CRUD 与乐观锁 ────────────────────────────────────────
def test_create_project_defaults(client_a, api_v1):
    response = client_a.post(
        f"{api_v1}/projects",
        {"title": "新项目", "research_question": "这是一个足够长的研究问题描述。"},
        format="json",
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["output_language"] == "zh-CN"
    assert data["version"] == 1
    assert data["corpus_revision"] == 1
    assert data["latest_run_id"] is None
    assert data["active_job_id"] is None
    assert set(data) == {
        "id",
        "title",
        "research_question",
        "output_language",
        "version",
        "corpus_revision",
        "latest_run_id",
        "active_job_id",
        "created_at",
        "updated_at",
    }


def test_create_project_rejects_out_of_range_title(client_a, api_v1):
    response = client_a.post(
        f"{api_v1}/projects",
        {"title": "x" * 121, "research_question": "这是一个足够长的研究问题描述。"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_project_rejects_unknown_field(client_a, api_v1):
    response = client_a.post(
        f"{api_v1}/projects",
        {
            "title": "新项目",
            "research_question": "这是一个足够长的研究问题描述。",
            "owner": 999,
        },
        format="json",
    )
    assert response.status_code == 400
    detail = response.json()["error"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert "owner" in detail["details"]["fields"]


def test_project_list_is_paginated_and_owner_scoped(client_a, project_a, project_b, api_v1):
    response = client_a.get(f"{api_v1}/projects")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["page"] == 1
    assert body["meta"]["page_size"] == 20
    assert body["meta"]["total"] == 1
    assert [item["id"] for item in body["data"]] == [str(project_a.id)]


def test_project_list_rejects_unknown_query_params(client_a, api_v1):
    response = client_a.get(f"{api_v1}/projects?sort=-title")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_cross_tenant_project_is_404(client_b, project_a, api_v1):
    """越权查询统一 404，不泄露资源属于他人。"""
    response = client_b.get(f"{api_v1}/projects/{project_a.id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_patch_project_bumps_version_only_for_title(client_a, project_a, api_v1):
    response = client_a.patch(
        f"{api_v1}/projects/{project_a.id}",
        {"expected_version": 1, "title": "改名后的项目"},
        format="json",
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "改名后的项目"
    assert data["version"] == 2
    # 只改 title 不影响语料版本
    assert data["corpus_revision"] == 1


def test_patch_research_question_bumps_corpus_revision(client_a, project_a, api_v1):
    response = client_a.patch(
        f"{api_v1}/projects/{project_a.id}",
        {"expected_version": 1, "research_question": "换一个完全不同的研究问题。"},
        format="json",
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["version"] == 2
    assert data["corpus_revision"] == 2


def test_patch_with_stale_version_is_conflict(client_a, project_a, api_v1):
    response = client_a.patch(
        f"{api_v1}/projects/{project_a.id}",
        {"expected_version": 99, "title": "并发修改"},
        format="json",
    )
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "VERSION_CONFLICT"
    assert body["details"]["current_version"] == 1


def test_patch_project_requires_at_least_one_field(client_a, project_a, api_v1):
    response = client_a.patch(
        f"{api_v1}/projects/{project_a.id}",
        {"expected_version": 1},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ── 任务接口 ──────────────────────────────────────────────────
def test_project_jobs_empty_list(client_a, project_a, api_v1):
    response = client_a.get(f"{api_v1}/projects/{project_a.id}/jobs")
    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["meta"]["total"] == 0


def test_project_jobs_rejects_unknown_kind(client_a, project_a, api_v1):
    response = client_a.get(f"{api_v1}/projects/{project_a.id}/jobs?kind=unknown_kind")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_unknown_job_is_404(client_a, api_v1):
    response = client_a.get(f"{api_v1}/jobs/11111111-1111-4111-8111-111111111111")
    assert response.status_code == 404


# ── 文献上传 / 编辑 / 下载（M05）────────────────────────────────
def test_paper_upload_requires_auth(api_client, project_a):
    response = api_client.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(1)},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_paper_upload_to_other_users_project_is_404(client_b, project_a):
    """越权上传不泄露项目归属，且不产生任何文献。"""
    from apps.literature.models import Paper

    response = _upload(client_b, project_a)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert not Paper.objects.exists()


def test_paper_upload_missing_file_is_400(client_a, project_a):
    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"title": "只有标题"},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 400
    detail = response.json()["error"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert "file" in detail["details"]["fields"]


def test_paper_upload_rejects_unknown_field(client_a, project_a):
    """multipart 里的未知字段同样 400，不被静默忽略。"""
    response = _upload(client_a, project_a, source="arxiv", included="false")
    assert response.status_code == 400
    detail = response.json()["error"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert set(detail["details"]["fields"]) == {"source", "included"}


def test_paper_upload_without_idempotency_key_is_400(client_a, project_a):
    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(1)},
        format="multipart",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_paper_upload_title_defaults_to_filename(client_a, project_a):
    """未提供 title 时暂用文件名；authors/year 初始为空。"""
    response = _upload(client_a, project_a, file=make_pdf_file(2, name="Attention is all you need.pdf"))
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["title"] == "Attention is all you need"
    assert data["authors"] == [] and data["year"] is None
    assert data["source"] == "upload" and data["source_id"] is None
    assert data["included"] is True and data["version"] == 1
    assert data["page_count"] == 2
    assert data["original_filename"] == "Attention is all you need.pdf"
    assert data["file_url"] == f"/api/v1/papers/{data['id']}/file"


def test_paper_upload_title_too_long_is_400(client_a, project_a):
    response = _upload(client_a, project_a, title="x" * 501)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_paper_upload_rejects_uuid_key_that_is_not_a_uuid(client_a, project_a):
    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(1)},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="not-a-uuid",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_paper_list_is_owner_scoped_and_paginated(client_a, client_b, project_a, project_b):
    _upload(client_a, project_a, file=make_pdf_file(1, name="a1.pdf"))
    _upload(client_a, project_a, file=make_pdf_file(2, name="a2.pdf"))
    _upload(client_b, project_b, file=make_pdf_file(1, name="b1.pdf"))

    response = client_a.get(f"/api/v1/projects/{project_a.id}/papers")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 2
    assert [item["original_filename"] for item in body["data"]] == ["a2.pdf", "a1.pdf"]

    # 另一个账号读别人的项目：整个项目对他不存在，必须是 404 而不是空列表
    # （200 + 空列表会让「越权」和「本来就没文献」无法区分）
    cross = client_b.get(f"/api/v1/projects/{project_a.id}/papers")
    assert cross.status_code == 404
    assert cross.json()["error"]["code"] == "NOT_FOUND"


def test_paper_list_filters_by_included(client_a, project_a):
    first = _upload(client_a, project_a, file=make_pdf_file(1, name="keep.pdf")).json()["data"]
    second = _upload(client_a, project_a, file=make_pdf_file(2, name="drop.pdf")).json()["data"]
    client_a.patch(
        f"/api/v1/papers/{second['id']}", {"expected_version": 1, "included": False}, format="json"
    )

    included = client_a.get(f"/api/v1/projects/{project_a.id}/papers?included=true").json()
    assert [item["id"] for item in included["data"]] == [first["id"]]

    excluded = client_a.get(f"/api/v1/projects/{project_a.id}/papers?included=false").json()
    assert [item["id"] for item in excluded["data"]] == [second["id"]]

    bad = client_a.get(f"/api/v1/projects/{project_a.id}/papers?included=maybe")
    assert bad.status_code == 400


def test_paper_list_rejects_unknown_query_param(client_a, project_a):
    response = client_a.get(f"/api/v1/projects/{project_a.id}/papers?sort=title")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_paper_patch_updates_metadata_and_bumps_corpus_revision(client_a, project_a):
    from apps.projects.models import Project

    paper = _upload(client_a, project_a).json()["data"]
    # 上传本身已经让语料快照前进一格（新增文献属于语料变化）
    after_upload = Project.objects.get(pk=project_a.pk).corpus_revision

    response = client_a.patch(
        f"/api/v1/papers/{paper['id']}",
        {
            "expected_version": 1,
            "title": "改名后的标题",
            "authors": ["张三", "李四"],
            "year": 2021,
            "included": False,
        },
        format="json",
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "改名后的标题"
    assert data["authors"] == ["张三", "李四"]
    assert data["year"] == 2021 and data["included"] is False
    assert data["version"] == 2
    # 元数据与纳入状态都属于语料快照
    assert Project.objects.get(pk=project_a.pk).corpus_revision == after_upload + 1


def test_paper_patch_with_identical_content_does_not_bump_version(client_a, project_a):
    """内容相同的保存不涨版本，也不动语料版本。"""
    from apps.projects.models import Project

    paper = _upload(client_a, project_a, title="不变标题").json()["data"]
    after_upload = Project.objects.get(pk=project_a.pk).corpus_revision

    response = client_a.patch(
        f"/api/v1/papers/{paper['id']}",
        {"expected_version": 1, "title": "不变标题", "included": True},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["data"]["version"] == 1
    assert Project.objects.get(pk=project_a.pk).corpus_revision == after_upload


def test_paper_patch_with_stale_version_is_conflict(client_a, project_a):
    paper = _upload(client_a, project_a).json()["data"]
    client_a.patch(
        f"/api/v1/papers/{paper['id']}", {"expected_version": 1, "title": "第一次"}, format="json"
    )
    response = client_a.patch(
        f"/api/v1/papers/{paper['id']}", {"expected_version": 1, "title": "第二次"}, format="json"
    )
    assert response.status_code == 409
    detail = response.json()["error"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["details"]["current_version"] == 2


def test_paper_patch_requires_at_least_one_editable_field(client_a, project_a):
    paper = _upload(client_a, project_a).json()["data"]
    response = client_a.patch(
        f"/api/v1/papers/{paper['id']}", {"expected_version": 1}, format="json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_paper_patch_rejects_unknown_field_and_bad_year(client_a, project_a):
    paper = _upload(client_a, project_a).json()["data"]

    unknown = client_a.patch(
        f"/api/v1/papers/{paper['id']}",
        {"expected_version": 1, "title": "x", "citation_count": 3},
        format="json",
    )
    assert unknown.status_code == 400
    assert "citation_count" in unknown.json()["error"]["details"]["fields"]

    # 年份低于 1000 违反契约与数据库检查约束
    bad_year = client_a.patch(
        f"/api/v1/papers/{paper['id']}", {"expected_version": 1, "year": 999}, format="json"
    )
    assert bad_year.status_code == 400


def test_paper_patch_does_not_allow_replacing_the_file(client_a, project_a):
    """PDF 不可原位替换：改哈希的请求必须被拒绝，不产生第二种响应形式。"""
    paper = _upload(client_a, project_a).json()["data"]
    response = client_a.patch(
        f"/api/v1/papers/{paper['id']}",
        {"expected_version": 1, "file_sha256": "0" * 64},
        format="json",
    )
    assert response.status_code == 400
    assert "file_sha256" in response.json()["error"]["details"]["fields"]


def test_paper_detail_and_file_are_owner_scoped(client_b, client_a, project_a):
    """跨用户读详情与下载原文一律 404，不泄露归属，也不暴露磁盘路径。"""
    paper = _upload(client_a, project_a).json()["data"]

    assert client_b.get(f"/api/v1/papers/{paper['id']}").status_code == 404
    assert client_b.get(f"/api/v1/papers/{paper['id']}/file").status_code == 404

    unknown = uuid.uuid4()
    assert client_a.get(f"/api/v1/papers/{unknown}").status_code == 404
    assert client_a.get(f"/api/v1/papers/{unknown}/file").status_code == 404


def test_paper_file_download_returns_original_bytes(client_a, project_a):
    """下载成功直接返回二进制，且字节与上传内容一致。"""
    from tests.conftest import make_pdf_bytes

    payload = make_pdf_bytes(2)
    from django.core.files.uploadedfile import SimpleUploadedFile

    uploaded = SimpleUploadedFile("原文.pdf", payload, content_type="application/pdf")
    paper = _upload(client_a, project_a, file=uploaded).json()["data"]

    response = client_a.get(f"/api/v1/papers/{paper['id']}/file")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert b"".join(response.streaming_content) == payload
    # 非 ASCII 文件名必须百分号编码，否则 WSGI 下会抛 UnicodeEncodeError
    disposition = response["Content-Disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%E5%8E%9F%E6%96%87.pdf" in disposition
    # 不得出现任何磁盘路径
    assert "storage" not in disposition.lower()


def test_paper_file_download_failure_is_json_not_binary(client_a, project_a):
    """文件端点失败时仍返回标准 JSON 错误，而不是下载一段错误文本。"""
    response = client_a.get(f"/api/v1/papers/{uuid.uuid4()}/file")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_arxiv_import_requires_auth_and_valid_ids(api_client, client_a, project_a, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)

    anonymous = api_client.post(
        f"/api/v1/projects/{project_a.id}/imports/arxiv",
        {"arxiv_ids": ["2401.00001"]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert anonymous.status_code == 401

    unknown_field = client_a.post(
        f"/api/v1/projects/{project_a.id}/imports/arxiv",
        {"arxiv_ids": ["2401.00001"], "url": "https://example.com/x.pdf"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert unknown_field.status_code == 400

    no_key = client_a.post(
        f"/api/v1/projects/{project_a.id}/imports/arxiv",
        {"arxiv_ids": ["2401.00001"]},
        format="json",
    )
    assert no_key.status_code == 400


def test_arxiv_import_is_idempotent_on_replay(client_a, project_a, monkeypatch):
    """同键重放返回原受理响应，不新建 Job。"""
    from apps.jobs.models import Job

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    key = str(uuid.uuid4())
    first = client_a.post(
        f"/api/v1/projects/{project_a.id}/imports/arxiv",
        {"arxiv_ids": ["2401.00001", "2401.00002"]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    second = client_a.post(
        f"/api/v1/projects/{project_a.id}/imports/arxiv",
        {"arxiv_ids": ["2401.00001", "2401.00002"]},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key,
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["data"] == second.json()["data"]
    assert Job.objects.count() == 1


# ── 路由表完整性 ──────────────────────────────────────────────
def test_all_contract_routes_are_mounted():
    """45 个 HTTP 操作对应的路由都应存在，且不带尾斜杠。"""
    from django.urls import get_resolver

    # 路由表按 app 分成多个 include，全部挂在同一个 /api/v1/ 前缀下
    mounts = [
        pattern
        for pattern in get_resolver().url_patterns
        if str(pattern.pattern) == "api/v1/"
    ]
    assert mounts, "没有找到 /api/v1/ 路由挂载点"

    mounted: list[str] = []
    for mount in mounts:
        mounted.extend(str(sub.pattern) for sub in mount.url_patterns)

    expected = {
        "auth/csrf",
        "auth/login",
        "auth/logout",
        "auth/me",
        "projects",
        "projects/<uuid:project_id>",
        "projects/<uuid:project_id>/papers",
        "projects/<uuid:project_id>/papers/upload",
        "projects/<uuid:project_id>/imports/arxiv",
        "projects/<uuid:project_id>/runs",
        "projects/<uuid:project_id>/jobs",
        "projects/<uuid:project_id>/reviews",
        "papers/<uuid:paper_id>",
        "papers/<uuid:paper_id>/file",
        "runs/<uuid:run_id>",
        "runs/<uuid:run_id>/papers/<uuid:paper_id>/analysis",
        "runs/<uuid:run_id>/papers/<uuid:paper_id>/evidence",
        "runs/<uuid:run_id>/topics",
        "runs/<uuid:run_id>/disputes",
        "runs/<uuid:run_id>/evidence/<uuid:evidence_id>",
        "disputes/<uuid:dispute_id>",
        "runs/<uuid:run_id>/retrievals",
        "retrievals/<uuid:retrieval_id>",
        "runs/<uuid:run_id>/reviews",
        "reviews/<uuid:review_id>",
        "reviews/<uuid:review_id>/revisions",
        "reviews/<uuid:review_id>/revisions/<uuid:revision_id>",
        "reviews/<uuid:review_id>/validations",
        "validations/<uuid:validation_id>",
        "reviews/<uuid:review_id>/exports",
        "exports/<uuid:export_id>",
        "exports/<uuid:export_id>/file",
        "jobs/<uuid:job_id>",
        "jobs/<uuid:job_id>/cancel",
        "jobs/<uuid:job_id>/retry",
    }
    missing = expected - set(mounted)
    assert not missing, f"缺少路由：{sorted(missing)}"
    assert all(not item.endswith("/") for item in mounted), "路由不得带尾斜杠"


# `03` 为每条路径规定的方法集合。只断言"路径存在"不够：
# `runs/{id}/retrievals` 曾经只挂了 POST 的 create 视图，GET 落进 405 兜底分支，
# 而当时的路由测试因为只看路径，完全没有发现这个缺口。
EXPECTED_METHODS: dict[str, set[str]] = {
    "auth/csrf": {"GET"},
    "auth/login": {"POST"},
    "auth/logout": {"POST"},
    "auth/me": {"GET"},
    "projects": {"GET", "POST"},
    "projects/<uuid:project_id>": {"GET", "PATCH", "DELETE"},
    "projects/<uuid:project_id>/papers": {"GET"},
    "projects/<uuid:project_id>/papers/upload": {"POST"},
    "projects/<uuid:project_id>/imports/arxiv": {"POST"},
    "projects/<uuid:project_id>/runs": {"GET", "POST"},
    "projects/<uuid:project_id>/jobs": {"GET"},
    "papers/<uuid:paper_id>": {"GET", "PATCH"},
    "papers/<uuid:paper_id>/file": {"GET"},
    "runs/<uuid:run_id>": {"GET"},
    "runs/<uuid:run_id>/papers/<uuid:paper_id>/analysis": {"GET"},
    "runs/<uuid:run_id>/papers/<uuid:paper_id>/evidence": {"GET"},
    "runs/<uuid:run_id>/topics": {"GET"},
    "runs/<uuid:run_id>/disputes": {"GET"},
    "runs/<uuid:run_id>/evidence/<uuid:evidence_id>": {"GET"},
    "disputes/<uuid:dispute_id>": {"PATCH"},
    "runs/<uuid:run_id>/retrievals": {"GET", "POST"},
    "retrievals/<uuid:retrieval_id>": {"GET"},
    "jobs/<uuid:job_id>": {"GET"},
    "jobs/<uuid:job_id>/cancel": {"POST"},
    "jobs/<uuid:job_id>/retry": {"POST"},
}


def test_core_routes_expose_every_contract_method():
    """契约规定的每个方法都必须真的被某个视图处理。

    用 `resolve()` 取路径的候选视图，并读取 `cls.actions` / `initkwargs.method`
    判断支持的 HTTP 方法；两者都拿不到时至少要求候选视图存在。
    """
    from django.urls import resolve

    problems: list[str] = []
    for pattern, methods in EXPECTED_METHODS.items():
        concrete = pattern.replace("<uuid:project_id>", str(uuid.uuid4()))
        concrete = concrete.replace("<uuid:paper_id>", str(uuid.uuid4()))
        concrete = concrete.replace("<uuid:run_id>", str(uuid.uuid4()))
        concrete = concrete.replace("<uuid:evidence_id>", str(uuid.uuid4()))
        concrete = concrete.replace("<uuid:dispute_id>", str(uuid.uuid4()))
        concrete = concrete.replace("<uuid:retrieval_id>", str(uuid.uuid4()))
        concrete = concrete.replace("<uuid:job_id>", str(uuid.uuid4()))
        try:
            match = resolve(f"/api/v1/{concrete}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{pattern}: 无法解析（{type(exc).__name__}）")
            continue

        func = match.func
        supported: set[str] = set()
        actions = getattr(getattr(func, "cls", None), "actions", None)
        if isinstance(actions, dict):
            supported = {method.upper() for method in actions}
        else:
            declared = getattr(func, "initkwargs", {}).get("method")
            allowed = getattr(func, "allowed_methods", None)
            if declared:
                supported = {str(declared).upper()}
            elif allowed:
                supported = {str(item).upper() for item in allowed}

        # 无法静态判断时不误报：只要求候选视图存在
        if supported and not methods <= supported:
            problems.append(f"{pattern}: 缺少 {sorted(methods - supported)}（实际 {sorted(supported)}）")

    assert not problems, "契约方法未全部挂载：\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "path_template",
    [
        "runs/{run}/retrievals",
        "projects/{project}/runs",
        "projects/{project}/papers",
        "runs/{run}/topics",
        "runs/{run}/disputes",
    ],
)
def test_documented_get_routes_are_not_method_not_allowed(client_a, api_v1, path_template, project_a):
    """行为级验证：契约里的 GET 路径不得返回 405。

    **必须用已登录请求**：未登录时 401 与 405 都来自 DRF 的方法分发检查，
    两者无法区分。已登录后，方法存在 → 正常业务响应（200/404 等）；
    方法不存在 → 405 会盖过 404。

    这正是 `runs/{id}/retrievals` 曾经的形态：路径存在、只挂了 POST，
    GET 落到 405 兜底分支，而只检查路径是否存在的路由测试发现不了。
    """
    path = path_template.format(run=str(uuid.uuid4()), project=str(project_a.id))
    response = client_a.get(f"{api_v1}/{path}")

    assert response.status_code != 405, f"GET {path} 返回 405：该方法未挂载视图"
