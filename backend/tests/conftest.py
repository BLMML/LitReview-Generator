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


# ── 含文本层的 PDF 夹具（M06 解析与证据用）────────────────────
def make_text_pdf_bytes(pages: list[str]) -> bytes:
    """生成**含文本层**的真 PDF，每项是一页的正文（`\\n` 分行）。

    M06 的解析与证据测试必须面对真实文本层：`make_pdf_bytes` 造的是空白页版 PDF，
    提取出来是空串，无法验证分页、切片与原文匹配。这里用 pypdf 手工写内容流，
    不引入 reportlab 之类只为测试存在的依赖。
    """
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for body in pages:
        page = writer.add_blank_page(width=595, height=842)  # A4 点
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        font_ref = writer._add_object(font)  # noqa: SLF001 - 仅为构造测试 PDF
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        lines = body.split("\n") if body else [""]
        content = ["BT", "/F1 12 Tf", "50 780 Td", "16 TL"]
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content.append(f"({escaped}) Tj T*")
        content.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(content).encode("latin-1", errors="replace"))
        page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_text_pdf_file(pages: list[str], *, name: str = "text.pdf") -> Any:
    """包成上传用的 SimpleUploadedFile。"""
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, make_text_pdf_bytes(pages), content_type="application/pdf")


@pytest.fixture(autouse=True)
def _no_arxiv_rate_limit(settings: pytest.FixtureRequest) -> None:
    """测试里不真的按 arXiv 频率下限等待；限流逻辑本身单测另行覆盖。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "ARXIV_MIN_INTERVAL_SECONDS": 0}


# ── 已就绪的 Run（M08 检索与 M10 生成的前置）──────────────────
ANALYSIS_PAGES = [
    "Abstract\nSelf-attention improves sequence modelling on small corpora.",
    "Introduction\nRecurrent baselines are compared with attention models.",
    "3. Method\nWe replace the recurrent layer with a multi-head attention block.",
    "4. Results\nAttention improves accuracy by two points on the benchmark.",
    "5. Limitations\nThe evaluation covers a single language and dataset.",
]


def analysis_extract_payload(evidence_ids: list[str]) -> dict:
    """结构合法的 PaperExtract 输出，供 M07/M08/M10 夹具共用。"""
    single = {"value": "结论成立", "evidence_ids": [evidence_ids[0]]}
    listed = [{"value": "方法要点", "evidence_ids": [evidence_ids[-1]]}]
    return {
        "research_question": single,
        "research_object": {"value": None, "evidence_ids": []},
        "summary": single,
        "methods": listed,
        "datasets": [],
        "metrics": [],
        "conclusions": listed,
        "limitations": [],
    }


@pytest.fixture
def ready_run(client_a, project_a, monkeypatch):
    """一个 `ready` 的 Run，含两篇已解析并已抽取的论文。

    M07 的 `execute_run` 在后续阶段（index / topics / disputes / finalize）未实现时
    会把 Run 显式判成 failed；而检索与生成的前置是"Run 已 ready"。
    该状态要等 M09 才由流水线自己产生，所以这里在建好真实产物后推进状态位——
    **产物本身仍是真实跑出来的**，只有状态位是手工的。
    """
    import uuid as _uuid

    from apps.analysis import services as analysis_services
    from apps.analysis.models import RUN_READY, AnalysisRun

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    for name in ("alpha", "beta"):
        response = client_a.post(
            f"/api/v1/projects/{project_a.id}/papers/upload",
            {
                "file": make_text_pdf_file(
                    [f"{name} {page}" for page in ANALYSIS_PAGES], name=f"{name}.pdf"
                )
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(_uuid.uuid4()),
        )
        assert response.status_code == 201, response.json()

    class _Result:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = "mock:paper_extract"

    monkeypatch.setattr(
        analysis_services,
        "call_structured",
        lambda context, request: _Result(
            analysis_extract_payload(list(request.allowed_evidence_ids))
        ),
    )

    accepted = client_a.post(
        f"/api/v1/projects/{project_a.id}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(_uuid.uuid4()),
    ).json()["data"]

    from apps.jobs.services import claim_execution

    context = claim_execution(accepted["job_id"])
    analysis_services.execute_run(context, context.run_id)

    run = AnalysisRun.objects.get(pk=context.run_id)
    run.status = RUN_READY
    run.error = None
    run.save(update_fields=["status", "error", "updated_at"])
    return run
