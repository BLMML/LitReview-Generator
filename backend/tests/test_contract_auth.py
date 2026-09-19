"""契约与认证测试（docs/mvp/01 第 9 节）。

覆盖：路由无尾斜杠、统一 envelope、CSRF 含登录、未登录 401、越权 404、
未知字段拒绝、分页。

骨架阶段只覆盖已经实现的接口（accounts / projects / jobs 读 / 路由表）；
其余端点在实现后按同一模式补齐。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


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


# ── 路由表完整性 ──────────────────────────────────────────────
def test_all_contract_routes_are_mounted():
    """43 个 HTTP 操作对应的路由都应存在，且不带尾斜杠。"""
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
