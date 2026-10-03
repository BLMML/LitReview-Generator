"""真实异步等待验证 arXiv 整体截止时间；MockTransport 保证完全不出网。"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from apps.common.http import ApiError
from apps.common.storage import staging_root
from apps.literature import arxiv


@pytest.fixture
def deadline(settings, monkeypatch):
    settings.MVP_LIMITS = {
        **settings.MVP_LIMITS,
        "ARXIV_DOWNLOAD_TIMEOUT_SECONDS": 0.3,
        "ARXIV_MIN_INTERVAL_SECONDS": 0,
    }
    real_client = httpx.AsyncClient
    clients = []

    def route(handler):
        transport = httpx.MockTransport(handler)

        def create(**kwargs):
            client = real_client(transport=transport, trust_env=False, **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(arxiv.httpx, "AsyncClient", create)

    yield route
    assert clients and all(client.is_closed for client in clients), "到期后客户端未关闭"
    assert list(staging_root().iterdir()) == [], "超时留下了半成品"


def _metadata():
    return arxiv.SourceMetadata("2401.00001v1", "Example", [], 2024, "https://arxiv.org/abs/2401.00001v1")


def _expect_total_timeout(operation):
    started = time.monotonic()
    with pytest.raises(ApiError) as error:
        operation()
    elapsed = time.monotonic() - started
    assert error.value.code == "ARXIV_FETCH_FAILED"
    assert error.value.retryable is True
    # 给 Windows 调度保留余量；模拟的无限等待为 2 秒，旧实现会明显越过此界。
    assert 0.2 <= elapsed < 0.75, f"总预算 0.3 秒，实际用了 {elapsed:.3f} 秒"


@pytest.mark.parametrize("metadata_request", [False, True], ids=["pdf", "metadata"])
def test_total_deadline_cancels_slow_response_headers(deadline, metadata_request):
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(str(request.url))
            raise
        pytest.fail("响应头等待越过总预算仍未取消")

    deadline(handler)
    operation = (
        (lambda: arxiv.fetch_metadata("2401.00001"))
        if metadata_request
        else (lambda: arxiv.download_pdf(_metadata()))
    )
    _expect_total_timeout(operation)
    assert len(cancelled) == 1


def test_redirect_chain_shares_deadline_and_closes_previous_response(deadline):
    calls = []
    responses = []
    cancelled = []

    async def handler(request):
        calls.append(request)
        try:
            await asyncio.sleep(0.18)
        except asyncio.CancelledError:
            cancelled.append(request)
            raise
        response = httpx.Response(
            302,
            headers={"Location": f"https://arxiv.org/pdf/hop{len(calls)}"},
            request=request,
        )
        responses.append(response)
        return response

    deadline(handler)
    _expect_total_timeout(lambda: arxiv.download_pdf(_metadata()))
    assert len(calls) == 2, "总预算不能在重定向后重新开始"
    assert cancelled == [calls[-1]]
    assert len(responses) == 1 and responses[0].is_closed
    first_timeout = calls[0].extensions["timeout"]["read"]
    second_timeout = calls[1].extensions["timeout"]["read"]
    assert 0 < second_timeout < first_timeout <= 0.3


@pytest.mark.parametrize("drip", [False, True], ids=["stalled-body", "dripping-body"])
def test_total_deadline_cancels_body_and_cleans_partial_file(deadline, drip):
    chunks = []
    cancelled = []
    responses = []
    partial_file_seen = []

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"%PDF-1.4\n"
            # 下一次迭代之前，上一块已经写入暂存文件，验证覆盖真实半成品清理。
            partial_file_seen.append(bool(list(staging_root().iterdir())))
            try:
                for index in range(40):
                    # 涓滴间隔低于单 read 超时，但累计时间必定越过整体预算。
                    await asyncio.sleep(0.05 if drip else 2)
                    chunks.append(index)
                    yield b"x" * 64
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def aclose(self):
            self.closed = True

    body = Body()

    async def handler(request):
        response = httpx.Response(
            200, headers={"Content-Type": "application/pdf"}, stream=body, request=request
        )
        responses.append(response)
        return response

    deadline(handler)
    _expect_total_timeout(lambda: arxiv.download_pdf(_metadata()))
    assert partial_file_seen == [True], "测试未真正写出半成品"
    assert cancelled == [True], "正文等待没有收到取消信号"
    assert body.closed and responses[0].is_closed
    if drip:
        assert 1 <= len(chunks) < 10, "不能因持续收到小块而延长总预算"
    else:
        assert chunks == []
