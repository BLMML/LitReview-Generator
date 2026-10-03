"""literature.arxiv：arXiv ID 校验与真实元数据 / PDF 获取。

职责（docs/mvp/01 第 4.2 节、docs/mvp/04 第 6.2 节）：
- 验证 1—10 个 arXiv ID；
- 向预定 arXiv 接口获取真实元数据和 PDF；
- 限制重定向、时间、下载大小和请求频率。

安全约束：**不接受用户提供的任意 URL**；数据源地址由服务端白名单构造，
每一跳重定向都要重新校验主机，绝不把用户输入当成可请求地址。

可测性：下载和元数据请求经 `_http_async_client` 构造，测试用 `httpx.MockTransport`
替换 `httpx.AsyncClient` 即可注入受控响应（见 `tests/test_arxiv_client.py`），
因此自动测试不发真实网络请求。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from django.conf import settings

from apps.common.http import ApiError, api_error
from apps.common.storage import StagedFile, staging_root

# 新旧合法形式与版本后缀；不接受 URL
ARXIV_ID_PATTERN = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$")

# 服务端白名单：导出 / 元数据接口只允许这些主机
ARXIV_ALLOWED_HOSTS = ("arxiv.org", "export.arxiv.org")

ATOM_NAMESPACE = "{http://www.w3.org/2005/Atom}"
PDF_MEDIA_TYPE = "application/pdf"

# 无版本号时由接口解析出的具体版本；vN 后缀
_VERSION_SUFFIX = re.compile(r"v(\d+)$")

_rate_lock = threading.Lock()
_last_request_at = 0.0


@dataclass(frozen=True)
class SourceMetadata:
    """arXiv 返回的真实元数据；模型不得覆盖已保存元数据。"""

    source_id: str
    title: str
    authors: list[str]
    year: int | None
    source_url: str


def validate_arxiv_ids(raw_ids: Any) -> list[str]:
    """校验 1—10 个互不相同的合法 arXiv ID；不接受 URL。"""
    limits = settings.MVP_LIMITS
    if not isinstance(raw_ids, (list, tuple)):
        raise api_error("VALIDATION_ERROR", "arxiv_ids 必须是数组")
    if not limits["ARXIV_IDS_MIN"] <= len(raw_ids) <= limits["ARXIV_IDS_MAX"]:
        raise api_error(
            "VALIDATION_ERROR",
            f"arxiv_ids 数量必须在 {limits['ARXIV_IDS_MIN']}—{limits['ARXIV_IDS_MAX']} 之间",
        )
    normalized: list[str] = []
    for item in raw_ids:
        value = str(item).strip()
        if not ARXIV_ID_PATTERN.match(value):
            raise api_error("VALIDATION_ERROR", f"不是合法的 arXiv ID：{value!r}")
        if value in normalized:
            raise api_error("VALIDATION_ERROR", f"arxiv_ids 存在重复项：{value!r}")
        normalized.append(value)
    return normalized


def _require_allowed_url(url: str) -> str:
    """只允许白名单内的 HTTPS 地址；重定向目标同样要过这一关。"""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ARXIV_ALLOWED_HOSTS
        or parsed.username
        or parsed.password
    ):
        raise api_error("ARXIV_FETCH_FAILED", "数据源地址不在服务端白名单内", retryable=False)
    return url


def _respect_rate_limit() -> None:
    """请求频率下限：ARXIV_MIN_INTERVAL_SECONDS 内不重复出网。"""
    global _last_request_at
    interval = float(settings.MVP_LIMITS["ARXIV_MIN_INTERVAL_SECONDS"])
    with _rate_lock:
        waited = time.monotonic() - _last_request_at
        if waited < interval:
            time.sleep(interval - waited)
        _last_request_at = time.monotonic()


async def _respect_rate_limit_async() -> None:
    """等待请求间隔时可取消；锁只保护时间戳，不跨越异步等待。"""
    global _last_request_at
    interval = float(settings.MVP_LIMITS["ARXIV_MIN_INTERVAL_SECONDS"])
    while True:
        with _rate_lock:
            now = time.monotonic()
            delay = interval - (now - _last_request_at)
            if delay <= 0:
                _last_request_at = now
                return
        await asyncio.sleep(delay)


def _fetch_bytes(url: str) -> tuple[bytes, str, str]:
    """受控 GET：限制字节上限与总耗时，读取过程中超限立即停止。

    元数据响应很小，但仍要设上限，否则一个畸形响应同样能把内存吃满。
    返回 (内容, 最终 URL, Content-Type)。
    """
    chunks: list[bytes] = []

    def consume(chunk: bytes) -> None:
        chunks.append(chunk)

    cap = _metadata_limit_bytes() * 2
    final_url, media_type = _stream_to(url, consume, limit_bytes=cap, overflow_code="ARXIV_FETCH_FAILED")
    return b"".join(chunks), final_url, media_type


def _metadata_limit_bytes() -> int:
    """元数据响应上限：以 PDF 上限为尺度取一个明确的小值。"""
    return min(2 * 1024 * 1024, int(settings.MVP_LIMITS["PDF_MAX_BYTES"]))


def _raise_for_status(status: int) -> None:
    if status == 429:
        raise api_error("ARXIV_FETCH_FAILED", "arXiv 限流", retryable=True)
    if status == 404:
        raise api_error("ARXIV_FETCH_FAILED", "该 arXiv ID 不存在", retryable=False)
    if status >= 400:
        raise api_error("ARXIV_FETCH_FAILED", f"arXiv 返回 {status}", retryable=status >= 500)


def _http_get(url: str, *, timeout: float, max_redirects: int) -> httpx.Response:
    """受限 GET：手动跟随重定向，每一跳都重新校验主机。

    返回的 response 已完整读入内存；需要按字节上限中途停止的场景用
    `_fetch_bytes` 或 `_stream_to`。
    """
    with _http_client(timeout=timeout) as client:
        current = _require_allowed_url(url)
        for _ in range(max_redirects + 1):
            response = client.get(current)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise api_error("ARXIV_FETCH_FAILED", "重定向缺少目标地址", retryable=False)
                current = _require_allowed_url(str(httpx.URL(current).join(location)))
                continue
            return response
        raise api_error("ARXIV_FETCH_FAILED", "重定向次数超过上限", retryable=False)


def _http_client(*, timeout: float) -> httpx.Client:
    """统一构造客户端：不跟随自动重定向（由本模块逐跳校验主机）。"""
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": _user_agent(), "Accept": "*/*"},
        follow_redirects=False,
    )


def _stream_to(
    url: str,
    consume: Callable[[bytes], None],
    *,
    limit_bytes: int,
    overflow_code: str,
) -> tuple[str, str]:
    """同步 worker 入口；网络等待由异步内核统一取消，不遗留后台下载线程。"""
    budget = float(settings.MVP_LIMITS["ARXIV_DOWNLOAD_TIMEOUT_SECONDS"])
    try:
        return asyncio.run(
            _stream_to_async(
                url, consume, budget=budget, limit_bytes=limit_bytes, overflow_code=overflow_code
            )
        )
    except TimeoutError:
        raise api_error("ARXIV_FETCH_FAILED", "下载超过总耗时上限", retryable=True) from None
    except ApiError:
        raise
    except httpx.HTTPError as exc:
        raise api_error("ARXIV_FETCH_FAILED", "arXiv 请求失败", retryable=True) from exc


def _http_async_client(*, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": _user_agent(), "Accept": "*/*"},
        follow_redirects=False,
    )


async def _stream_to_async(
    url: str,
    consume: Callable[[bytes], None],
    *,
    budget: float,
    limit_bytes: int,
    overflow_code: str,
) -> tuple[str, str]:
    """同一截止时间覆盖限流、所有跳转、响应头及正文；到期中断正在等待的 I/O。"""
    max_redirects = int(settings.MVP_LIMITS["ARXIV_MAX_REDIRECTS"])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    total = 0
    async with asyncio.timeout_at(deadline), _http_async_client(timeout=budget) as client:
        current = _require_allowed_url(url)
        for _ in range(max_redirects + 1):
            await _respect_rate_limit_async()
            remaining = min(budget, deadline - loop.time())
            if remaining <= 0:
                raise TimeoutError
            async with client.stream("GET", current, timeout=remaining) as response:
                if loop.time() >= deadline:
                    raise TimeoutError
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise api_error("ARXIV_FETCH_FAILED", "重定向缺少目标地址", retryable=False)
                    current = _require_allowed_url(str(httpx.URL(current).join(location)))
                    continue
                _raise_for_status(response.status_code)
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit_bytes:
                        raise api_error(overflow_code, details={"limit_bytes": limit_bytes})
                    if loop.time() >= deadline:
                        raise TimeoutError
                    consume(chunk)
                    # 即使缓存/测试流立即产出，也让取消信号在块之间生效。
                    await asyncio.sleep(0)
                if loop.time() >= deadline:
                    raise TimeoutError
                media_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                return str(response.url), media_type
        raise api_error("ARXIV_FETCH_FAILED", "重定向次数超过上限", retryable=False)


def _stream_download(url: str, path: Any) -> None:
    """把响应**边下边写**到 path，累计超过上限立即停止并删除半成品。

    文件句柄延迟到第一个数据块才打开：非 200 响应（限流 / 404）在拿到正文前就会失败，
    此时若已持有打开的句柄，Windows 上连删除半成品都会因为「文件被占用」失败。
    """
    limit_bytes = int(settings.MVP_LIMITS["PDF_MAX_BYTES"])
    written = 0
    handle: Any = None

    def consume(chunk: bytes) -> None:
        nonlocal written, handle
        if handle is None:
            handle = path.open("wb")
        written += len(chunk)
        handle.write(chunk)

    try:
        _stream_to(url, consume, limit_bytes=limit_bytes, overflow_code="FILE_TOO_LARGE")
    except BaseException:
        # 先关闭再删除；半成品绝不留在暂存区，也不登记为资产
        if handle is not None:
            handle.close()
        path.unlink(missing_ok=True)
        raise
    else:
        if handle is not None:
            handle.close()

    if written == 0:
        path.unlink(missing_ok=True)
        raise api_error("ARXIV_FETCH_FAILED", "下载内容为空", retryable=True)


def _user_agent() -> str:
    """arXiv 要求客户端标识联系人；未配置时退回项目名而不是伪造邮箱。"""
    contact = (settings.ARXIV_CONTACT or "").strip()
    base = "LitReviewGenerator-MVP/1.0"
    return f"{base} (+mailto:{contact})" if contact else base


def _validate_arxiv_id(arxiv_id: str) -> str:
    value = str(arxiv_id).strip()
    if not ARXIV_ID_PATTERN.match(value):
        raise api_error("VALIDATION_ERROR", f"不是合法的 arXiv ID：{value!r}")
    return value


def _resolve_version(arxiv_id: str, entry_id: str) -> str:
    """把「无版本 ID」解析成接口返回的具体版本并冻结。

    接口返回的 entry id 形如 `http://arxiv.org/abs/2101.00001v3`，以它为准。
    必须拿到**带版本的** ID：无版本的 source_id 无法固定「我们下载的是哪一版」，
    日后同一 ID 可能指向新版本，去重与引用都会漂移，因此宁可失败也不猜版本。
    """
    resolved = entry_id.rsplit("/abs/", 1)[-1].strip() if "/abs/" in entry_id else ""
    if resolved and ARXIV_ID_PATTERN.match(resolved) and _VERSION_SUFFIX.search(resolved):
        return resolved
    # 接口没给版本时退回请求里的写法；请求里也没有版本则明确失败
    if _VERSION_SUFFIX.search(arxiv_id):
        return arxiv_id
    raise api_error("ARXIV_FETCH_FAILED", "未能解析出具体版本", retryable=True)


def _text(element: Any) -> str:
    if element is None or element.text is None:
        return ""
    return " ".join(element.text.split())


def _parse_atom(body: bytes) -> Any:
    """解析 arXiv Atom 响应。

    响应属于**外部不可信输入**，而标准库的 `xml.etree` 明确不防御实体扩展：
    自定义实体既可能被用来读本机文件（XXE），也可能指数膨胀耗尽内存
    （billion laughs）。真实 arXiv Atom 响应不含文档类型定义，
    因此这里在解析前直接拒绝任何带 `<!DOCTYPE` 的响应——
    这比依赖某个函数的参数更可靠，`XMLParser` 本身并不接受 `resolve_entities`。

    解析失败会抛 `ElementTree.ParseError` / `ValueError`，由调用方映射为业务错误。
    """
    if b"<!DOCTYPE" in body.upper():
        raise ValueError("响应包含文档类型定义，已按不可信输入拒绝")
    parser = ElementTree.XMLParser()
    parser.feed(body)
    return parser.close()


def fetch_metadata(arxiv_id: str) -> SourceMetadata:
    """获取真实元数据。无版本 ID 需先解析并冻结具体版本。"""
    requested = _validate_arxiv_id(arxiv_id)
    url = f"https://export.arxiv.org/api/query?id_list={requested}&max_results=1"
    body, final_url, _media = _fetch_bytes(url)
    # 最终地址也必须仍在白名单内，防止 30x 把人带到别处
    _require_allowed_url(final_url)

    try:
        root = _parse_atom(body)
    except (ElementTree.ParseError, ValueError) as exc:
        raise api_error("ARXIV_FETCH_FAILED", "arXiv 返回内容无法解析", retryable=True) from exc

    entry = root.find(f"{ATOM_NAMESPACE}entry")
    if entry is None:
        raise api_error("ARXIV_FETCH_FAILED", "该 arXiv ID 没有对应记录", retryable=False)

    entry_id = _text(entry.find(f"{ATOM_NAMESPACE}id"))
    resolved = _resolve_version(requested, entry_id)

    title = _text(entry.find(f"{ATOM_NAMESPACE}title"))
    if not title:
        raise api_error("ARXIV_FETCH_FAILED", "arXiv 未返回标题", retryable=True)

    authors = [_text(node.find(f"{ATOM_NAMESPACE}name")) for node in entry.findall(f"{ATOM_NAMESPACE}author")]
    authors = [name for name in authors if name]

    published = _text(entry.find(f"{ATOM_NAMESPACE}published")) or _text(
        entry.find(f"{ATOM_NAMESPACE}updated")
    )
    year: int | None = None
    if len(published) >= 4 and published[:4].isdigit():
        candidate = int(published[:4])
        # 数据库检查约束要求 year >= 1000；异常值宁可留空也不写脏数据
        if candidate >= settings.MVP_LIMITS["PAPER_YEAR_MIN"]:
            year = candidate

    return SourceMetadata(
        source_id=resolved,
        title=title[: settings.MVP_LIMITS["PAPER_TITLE_MAX"]],
        authors=authors,
        year=year,
        source_url=f"https://arxiv.org/abs/{resolved}",
    )


def download_pdf(source_record: SourceMetadata) -> StagedFile:
    """按受控重定向与大小上限**流式**下载 PDF，返回 StagedFile。

    先落到暂存文件（边下边算摘要），再校验魔数与结构；超过上限时立刻中断，
    不会先把整个响应读进内存。

    调用方负责在失败或重复时 `discard_staged`；本函数成功返回的暂存文件
    由调用方决定归档还是丢弃。
    """
    resolved = _validate_arxiv_id(source_record.source_id)
    url = _require_allowed_url(f"https://arxiv.org/pdf/{resolved}")

    # 先占一个暂存路径：下载过程中直接写入，避免在内存里攒完整包
    target = staging_root() / f"{uuid.uuid4().hex}.pdf"
    _stream_download(url, target)

    try:
        with target.open("rb") as handle:
            head = handle.read(1024)
        if not head.lstrip()[:5].startswith(b"%PDF-"):
            # 拿到的可能是 HTML 错误页或摘要页：明确失败，不把非 PDF 存成原文
            raise api_error("ARXIV_FETCH_FAILED", "下载结果不是 PDF", retryable=True)
        size_bytes = target.stat().st_size
        digest = _sha256_of(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    return StagedFile(
        path=target,
        sha256=digest,
        size_bytes=size_bytes,
        original_filename=f"{resolved}.pdf",
        # 已用魔数确认是 PDF，不沿用可能写错的供应商 Content-Type
        media_type=PDF_MEDIA_TYPE,
    )


def _sha256_of(path: Any) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
