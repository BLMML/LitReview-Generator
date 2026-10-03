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
def client_a(user_a: User):
    """用户 A 的独立客户端。

    刻意不复用 `api_client`：APIClient 实例内部保存当前登录身份，
    两个账号共用一个实例时后登录的会覆盖前一个，
    「跨用户 404」这类断言就会假通过或假失败。
    """
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_login(user_a)
    return client


@pytest.fixture
def client_b(user_b: User):
    """用户 B 的独立客户端；与 client_a 互不影响。"""
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_login(user_b)
    return client


@pytest.fixture
def csrf_client():
    """启用 CSRF 校验的客户端，用于验证匿名 POST 也必须带令牌。"""
    from rest_framework.test import APIClient

    return APIClient(enforce_csrf_checks=True)


@pytest.fixture
def api_v1() -> str:
    return "/api/v1"


# ── 真实 PDF 夹具（M05 上传校验用）────────────────────────────
def make_pdf_bytes(pages: int = 1, *, encrypt: str | None = None) -> bytes:
    """用 pypdf 生成结构合法的真 PDF；可指定页数与加密口令。

    刻意不用极小的手写模板：上传校验必须面对真实解析器行为，
    手写字节串容易在「合法」与「损坏」之间给出错误结论。
    """
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if encrypt:
        writer.encrypt(encrypt)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_pdf_file(pages: int = 1, *, name: str = "paper.pdf", encrypt: str | None = None) -> Any:
    """包成 DRF 上传用的 SimpleUploadedFile。"""
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(
        name, make_pdf_bytes(pages, encrypt=encrypt), content_type="application/pdf"
    )


@pytest.fixture
def pdf_bytes() -> Any:
    return make_pdf_bytes(3)


@pytest.fixture
def upload_pdf_file() -> Any:
    """可复用的上传文件工厂：`upload_pdf_file(pages=2, name="x.pdf")`。"""
    return make_pdf_file


@pytest.fixture(autouse=True)
def _no_arxiv_rate_limit(settings: pytest.FixtureRequest) -> None:
    """测试里不真的按 arXiv 频率下限等待；限流逻辑本身单测另行覆盖。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "ARXIV_MIN_INTERVAL_SECONDS": 0}
