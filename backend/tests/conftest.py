"""测试夹具。

职责（docs/mvp/01 第 9 节）：
- 隔离测试库、两位用户 / 两个项目、冻结小语料、模拟供应商、
  受控时钟与私有临时目录；
- **默认测试不产生付费调用**。

本机没有 PostgreSQL 时，DATABASE_URL 留空会回退到 SQLite，
足以跑契约与权限测试。但行锁、部分唯一索引与并发受理必须用真实 PostgreSQL 验证，
不能用 SQLite 或单线程 mock 得出通过结论（docs/mvp/01 第 9 节）。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from django.contrib.auth import get_user_model

# 必须在 settings 加载前固定为 mock：任何测试都不得触发付费调用
os.environ["LLM_MODE"] = "mock"
os.environ.setdefault("APP_ENV", "test")

User = get_user_model()

PASSWORD = "test-pass-9f3a"


@pytest.fixture(autouse=True)
def _no_paid_calls(settings: pytest.FixtureRequest) -> None:
    """双重保险：即使外部环境把 LLM_MODE 设成 real，测试里也强制 mock。"""
    settings.LLM_MODE = "mock"
    settings.PROJECT_DEFAULT_BUDGET_USD = 0


@pytest.fixture(autouse=True)
def _private_storage(settings: pytest.FixtureRequest) -> Any:
    """每个测试用独立的私有存储根目录，不污染真实目录。

    刻意放在仓库内的 .data/ 下，而不是系统临时目录：
    本机运行在文件沙箱中，系统临时目录对被测进程不可写。
    """
    import shutil
    import uuid
    from pathlib import Path

    base = Path(settings.REPO_ROOT) / ".data" / "test-storage" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    settings.PRIVATE_STORAGE_ROOT = base
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def user_a(db) -> User:
    return User.objects.create_user(username="alice", password=PASSWORD)


@pytest.fixture
def user_b(db) -> User:
    return User.objects.create_user(username="bob", password=PASSWORD)


@pytest.fixture
def project_a(db, user_a: User):
    from apps.common.auth import ActorContext
    from apps.projects.services import create_project

    actor = ActorContext(user=user_a, request_id="req_test")
    return create_project(
        actor,
        {"title": "项目 A", "research_question": "注意力机制在小样本场景下是否有效？"},
    )


@pytest.fixture
def project_b(db, user_b: User):
    from apps.common.auth import ActorContext
    from apps.projects.services import create_project

    actor = ActorContext(user=user_b, request_id="req_test")
    return create_project(
        actor,
        {"title": "项目 B", "research_question": "图神经网络在分子性质预测中的表现如何？"},
    )


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def client_a(api_client, user_a: User):
    api_client.force_login(user_a)
    return api_client


@pytest.fixture
def client_b(api_client, user_b: User):
    api_client.force_login(user_b)
    return api_client


@pytest.fixture
def csrf_client():
    """启用 CSRF 校验的客户端，用于验证匿名 POST 也必须带令牌。"""
    from rest_framework.test import APIClient

    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def api_v1() -> str:
    return "/api/v1"
