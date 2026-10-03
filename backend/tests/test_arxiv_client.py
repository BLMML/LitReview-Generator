"""arXiv 客户端测试（docs/mvp/01 第 4.2 节、docs/mvp/05 T04）。

覆盖：ID 校验、真实元数据解析、无版本 ID 解析并冻结具体版本、
服务端白名单与重定向逐跳校验、下载大小上限、非 PDF 内容拒绝、
以及外部响应解析的实体扩展防护（XXE / billion laughs）。

所有出网请求都用**真实 `httpx.MockTransport`** 注入，因此本文件不发真实网络请求，
也不依赖 arXiv 可用性；真实接口冒烟属于 M14 的人工验收步骤。
"""

from __future__ import annotations

import httpx
import pytest
from tests.conftest import make_pdf_bytes

from apps.common.http import ApiError
from apps.literature import arxiv

pytestmark = pytest.mark.django_db


def _atom_entry(arxiv_id: str, *, title: str = "示例论文", published: str = "2021-03-04T12:00:00Z") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}</id>
    <published>{published}</published>
    <title>{title}</title>
    <author><name>张三</name></author>
    <author><name>Li Si</name></author>
  </entry>
</feed>
""".encode()


class _FakeResponse:
    """最小 httpx.Response 替身：只提供客户端真正读取的字段。"""

    def __init__(
        self, *, status_code=200, content=b"", headers=None, url="https://export.arxiv.org/api/query"
    ):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.url = url
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def json(self):  # pragma: no cover - 客户端不使用
        raise NotImplementedError


@pytest.fixture
def routed(monkeypatch):
    """按 URL 子串路由的替身出网层，基于**真实 httpx.MockTransport**。

    刻意不用手写的假 Client / 假 Response：真实 `httpx` 才会暴露 `client.stream()`
    的上下文管理、`aiter_bytes()` 分块与响应头解析行为，而流式下载的正确性正是
    依赖这些细节。测试因此走的是与生产同一套客户端代码路径。
    """
    calls: list[str] = []
    routes: dict[str, _FakeResponse] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        for fragment, stub in routes.items():
            if fragment in url:
                headers = dict(stub.headers)
                if "content-type" not in {name.lower() for name in headers}:
                    headers["content-type"] = "application/octet-stream"
                return httpx.Response(
                    stub.status_code, headers=headers, content=stub.content, request=request
                )
        return httpx.Response(404, content=b"", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    real_async_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, trust_env=False, **kwargs)

    def fake_async_client(**kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=transport, trust_env=False, **kwargs)

    monkeypatch.setattr(arxiv.httpx, "Client", fake_client)
    monkeypatch.setattr(arxiv.httpx, "AsyncClient", fake_async_client)
    return type("Routed", (), {"calls": calls, "routes": routes})


# ── ID 校验 ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    [
        "2401.00001",
        "2401.00001v12",
        "hep-th/9901001",
        "hep-th/9901001v2",
        "math.GT/0309136",
        " 2401.00001 ",  # 前后空白被规整
    ],
)
def test_valid_ids_are_accepted(value):
    assert arxiv.validate_arxiv_ids([value]) == [value.strip()]


@pytest.mark.parametrize(
    "value",
    [
        "https://arxiv.org/abs/2401.00001",
        "arxiv.org/abs/2401.00001",
        "2401.000",  # 位数不足
        "2401.00001x",
        "ftp://arxiv.org/2401.00001",
        "'; DROP TABLE papers; --",
        "",
    ],
)
def test_invalid_or_url_like_ids_are_rejected(value):
    """不接受任意 URL，也不接受注入式输入。"""
    with pytest.raises(ApiError) as error:
        arxiv.validate_arxiv_ids([value])
    assert error.value.code == "VALIDATION_ERROR"


def test_id_count_and_uniqueness_are_enforced(settings):
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "ARXIV_IDS_MIN": 1, "ARXIV_IDS_MAX": 3}
    with pytest.raises(ApiError):
        arxiv.validate_arxiv_ids([])
    with pytest.raises(ApiError):
        arxiv.validate_arxiv_ids(["2401.00001"] * 2)
    with pytest.raises(ApiError):
        arxiv.validate_arxiv_ids(["2401.00001", "2401.00002", "2401.00003", "2401.00004"])
    assert arxiv.validate_arxiv_ids(["2401.00001", "2401.00002"]) == ["2401.00001", "2401.00002"]


# ── 元数据解析 ────────────────────────────────────────────────
def test_fetch_metadata_parses_real_fields(routed):
    routed.routes["api/query"] = _FakeResponse(content=_atom_entry("2401.00001v3"))

    metadata = arxiv.fetch_metadata("2401.00001")

    assert metadata.source_id == "2401.00001v3"  # 无版本 ID 被解析成具体版本
    assert metadata.title == "示例论文"
    assert metadata.authors == ["张三", "Li Si"]
    assert metadata.year == 2021
    assert metadata.source_url == "https://arxiv.org/abs/2401.00001v3"
    assert routed.calls == ["https://export.arxiv.org/api/query?id_list=2401.00001&max_results=1"]


def test_fetch_metadata_keeps_explicit_version(routed):
    """请求里已带版本时，沿用请求的版本而不是接口当前版本。"""
    routed.routes["api/query"] = _FakeResponse(content=_atom_entry("2401.00001v1"))
    assert arxiv.fetch_metadata("2401.00001v1").source_id == "2401.00001v1"


def test_fetch_metadata_normalizes_whitespace_in_title(routed):
    routed.routes["api/query"] = _FakeResponse(
        content=_atom_entry("2401.00001v1", title="Multi\n   line    title")
    )
    assert arxiv.fetch_metadata("2401.00001").title == "Multi line title"


def test_fetch_metadata_without_version_in_response_fails(routed):
    """接口没给版本、请求也没带版本时必须明确失败，不能猜一个版本号。"""
    routed.routes["api/query"] = _FakeResponse(content=_atom_entry("2401.00001"))
    with pytest.raises(ApiError) as error:
        arxiv.fetch_metadata("2401.00001")
    assert error.value.code == "ARXIV_FETCH_FAILED"


def test_fetch_metadata_without_title_fails(routed):
    body = _atom_entry("2401.00001v1", title="")
    routed.routes["api/query"] = _FakeResponse(content=body)
    with pytest.raises(ApiError) as error:
        arxiv.fetch_metadata("2401.00001")
    assert error.value.code == "ARXIV_FETCH_FAILED"


def test_fetch_metadata_ignores_implausible_year(routed):
    """年份低于数据库检查约束下限时留空，不写脏数据。"""
    routed.routes["api/query"] = _FakeResponse(
        content=_atom_entry("2401.00001v1", published="0999-01-01T00:00:00Z")
    )
    assert arxiv.fetch_metadata("2401.00001").year is None


def test_missing_entry_is_not_retryable(routed):
    routed.routes["api/query"] = _FakeResponse(
        content=b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>'
    )
    with pytest.raises(ApiError) as error:
        arxiv.fetch_metadata("2401.00001")
    assert error.value.code == "ARXIV_FETCH_FAILED"
    assert error.value.retryable is False


# ── 外部响应解析的硬化（S314 提到的攻击面）────────────────────
def test_external_entities_are_not_resolved(routed, tmp_path):
    """XXE：响应里的外部实体不得被解析成文件内容。

    这是 arxiv.py 显式关闭 resolve_entities 的原因，也是本用例存在的意义——
    仅靠「换用 defusedxml」这条规则名无法证明实际行为。
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///{secret.as_posix()}">]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <published>2021-01-01T00:00:00Z</published>
    <title>&xxe;</title>
    <author><name>A</name></author>
  </entry>
</feed>
""".encode()
    routed.routes["api/query"] = _FakeResponse(content=payload)

    try:
        metadata = arxiv.fetch_metadata("2401.00001")
    except ApiError as exc:
        assert exc.code == "ARXIV_FETCH_FAILED"
        return
    # 解析成功也不能把文件内容当标题带出来
    assert "TOP-SECRET-CONTENT" not in metadata.title


def test_entity_expansion_bomb_does_not_hang(routed):
    """billion laughs：实体不展开，解析要么失败要么得到空标题，绝不膨胀。"""
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE feed [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>&d;</title>
    <author><name>A</name></author>
  </entry>
</feed>
"""
    routed.routes["api/query"] = _FakeResponse(content=payload)
    with pytest.raises(ApiError):
        arxiv.fetch_metadata("2401.00001")


def test_malformed_xml_is_retryable_failure(routed):
    routed.routes["api/query"] = _FakeResponse(content=b"<feed><entry>unclosed")
    with pytest.raises(ApiError) as error:
        arxiv.fetch_metadata("2401.00001")
    assert error.value.code == "ARXIV_FETCH_FAILED"
    assert error.value.retryable is True


# ── 数据源白名单与重定向 ──────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://export.arxiv.org/api/query",  # 非 HTTPS
        "https://evil.example.com/api/query",
        "https://arxiv.org.evil.example.com/api/query",
        "https://user:pass@export.arxiv.org/api/query",
    ],
)
def test_non_whitelisted_urls_are_refused(url):
    """地址由服务端构造；白名单外一律拒绝，凭证式地址也不例外。"""
    with pytest.raises(ApiError) as error:
        arxiv._require_allowed_url(url)
    assert error.value.code == "ARXIV_FETCH_FAILED"


def test_redirect_to_foreign_host_is_refused(monkeypatch):
    """重定向每一跳都重新校验主机，不能把人带到白名单之外。"""
    hops = [
        _FakeResponse(status_code=302, headers={"Location": "https://evil.example.com/x.pdf"}),
    ]

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return hops.pop(0)

    monkeypatch.setattr(arxiv.httpx, "Client", lambda **kwargs: _Client())
    with pytest.raises(ApiError) as error:
        arxiv._http_get("https://arxiv.org/pdf/2401.00001v1", timeout=5, max_redirects=3)
    assert error.value.code == "ARXIV_FETCH_FAILED"


def test_redirect_limit_is_enforced(monkeypatch):
    """无限重定向必须被跳数上限截断。"""

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            return _FakeResponse(status_code=302, headers={"Location": url})

    monkeypatch.setattr(arxiv.httpx, "Client", lambda **kwargs: _Client())
    with pytest.raises(ApiError) as error:
        arxiv._http_get("https://arxiv.org/pdf/2401.00001v1", timeout=5, max_redirects=2)
    assert error.value.code == "ARXIV_FETCH_FAILED"


# ── 下载 ──────────────────────────────────────────────────────
def test_download_pdf_returns_staged_file(routed):
    payload = make_pdf_bytes(2)
    routed.routes["pdf/"] = _FakeResponse(
        content=payload, headers={"Content-Type": "application/pdf"}, url="https://arxiv.org/pdf/2401.00001v1"
    )
    metadata = arxiv.SourceMetadata(
        source_id="2401.00001v1",
        title="t",
        authors=[],
        year=None,
        source_url="https://arxiv.org/abs/2401.00001v1",
    )
    staged = arxiv.download_pdf(metadata)
    try:
        assert staged.size_bytes == len(payload)
        assert staged.original_filename == "2401.00001v1.pdf"
        assert staged.media_type == "application/pdf"
        assert routed.calls == ["https://arxiv.org/pdf/2401.00001v1"]
    finally:
        staged.path.unlink(missing_ok=True)


def test_download_rejects_non_pdf_body(routed):
    """拿到 HTML 错误页时明确失败，不把非 PDF 当原文存下来。"""
    routed.routes["pdf/"] = _FakeResponse(
        content=b"<html><body>Not Found</body></html>",
        headers={"Content-Type": "text/html"},
        url="https://arxiv.org/pdf/2401.00001v1",
    )
    metadata = arxiv.SourceMetadata(
        source_id="2401.00001v1", title="t", authors=[], year=None, source_url="u"
    )
    with pytest.raises(ApiError) as error:
        arxiv.download_pdf(metadata)
    assert error.value.code == "ARXIV_FETCH_FAILED"
    # 失败不得留下暂存文件
    from apps.common.storage import staging_root

    assert list(staging_root().iterdir()) == []


def test_download_enforces_size_limit(routed, settings):
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PDF_MAX_BYTES": 4096}
    routed.routes["pdf/"] = _FakeResponse(
        content=b"%PDF-1.4\n" + b"0" * 9000,
        headers={"Content-Type": "application/pdf"},
        url="https://arxiv.org/pdf/2401.00001v1",
    )
    metadata = arxiv.SourceMetadata(
        source_id="2401.00001v1", title="t", authors=[], year=None, source_url="u"
    )
    with pytest.raises(ApiError) as error:
        arxiv.download_pdf(metadata)
    assert error.value.code == "FILE_TOO_LARGE"


def test_download_maps_rate_limit_and_server_errors(routed):
    metadata = arxiv.SourceMetadata(
        source_id="2401.00001v1", title="t", authors=[], year=None, source_url="u"
    )
    routed.routes["pdf/"] = _FakeResponse(status_code=429, url="https://arxiv.org/pdf/x")
    with pytest.raises(ApiError) as limited:
        arxiv.download_pdf(metadata)
    assert limited.value.code == "ARXIV_FETCH_FAILED" and limited.value.retryable is True

    routed.routes["pdf/"] = _FakeResponse(status_code=503, url="https://arxiv.org/pdf/x")
    with pytest.raises(ApiError) as server:
        arxiv.download_pdf(metadata)
    assert server.value.retryable is True

    routed.routes["pdf/"] = _FakeResponse(status_code=404, url="https://arxiv.org/pdf/x")
    with pytest.raises(ApiError) as missing:
        arxiv.download_pdf(metadata)
    assert missing.value.retryable is False


def test_network_failure_is_mapped_not_leaked(monkeypatch):
    """底层异常必须收敛成可解释的业务错误，不把 httpx 细节抛到上层。"""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(boom)
    monkeypatch.setattr(
        arxiv.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, trust_env=False, **kwargs),
    )
    with pytest.raises(ApiError) as error:
        arxiv.fetch_metadata("2401.00001")
    assert error.value.code == "ARXIV_FETCH_FAILED"
    assert error.value.retryable is True
    assert "httpx" not in error.value.message.lower()


# ── 请求频率 ──────────────────────────────────────────────────
def test_rate_limit_spacing_is_respected(monkeypatch, settings):
    """请求频率下限：两次出网之间必须等到间隔，而不是连打接口。"""
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "ARXIV_MIN_INTERVAL_SECONDS": 0.2}
    monkeypatch.setattr(arxiv, "_last_request_at", 0.0)

    slept: list[float] = []
    monkeypatch.setattr(arxiv.time, "sleep", lambda seconds: slept.append(seconds))

    arxiv._respect_rate_limit()
    arxiv._respect_rate_limit()
    assert slept, "第二次请求必须等待频率间隔"
    assert slept[0] > 0
