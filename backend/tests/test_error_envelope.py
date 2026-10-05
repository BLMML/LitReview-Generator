"""统一错误 envelope 的兜底路径测试。

起因：`common/http.py` 的兜底分支把 `status_code` 传给了没有该参数的 `api_error()`，
于是**任何**不是 ApiError / Http404 / PermissionDenied / ValidationError / Throttled 的
DRF 异常（如 405 MethodNotAllowed、415 UnsupportedMediaType、401 NotAuthenticated 的
其它形态）都会在异常处理器内部再抛一次 TypeError，最终表现为 500 —— 而不是契约要求的
标准错误 JSON。这类缺陷只在真实请求打到该分支时才暴露，纯 schema 测试看不到。
"""

from __future__ import annotations

import pytest
from rest_framework import exceptions as drf_exceptions

from apps.common.http import ApiError, exception_handler

pytestmark = pytest.mark.django_db


def _handle(exc):
    response = exception_handler(exc, {"request": None})
    assert response is not None, f"{type(exc).__name__} 没有被收敛成标准错误响应"
    return response


@pytest.mark.parametrize(
    ("exception", "expected_code", "expected_status"),
    [
        (drf_exceptions.MethodNotAllowed("GET"), "VALIDATION_ERROR", 405),
        (drf_exceptions.UnsupportedMediaType("application/xml"), "VALIDATION_ERROR", 415),
        (drf_exceptions.NotFound("missing"), "NOT_FOUND", 404),
        (drf_exceptions.ParseError("bad json"), "VALIDATION_ERROR", 400),
        (drf_exceptions.NotAcceptable("application/xml"), "VALIDATION_ERROR", 406),
        (drf_exceptions.AuthenticationFailed("nope"), "INVALID_CREDENTIALS", 401),
    ],
)
def test_drf_exceptions_become_standard_envelope(exception, expected_code, expected_status):
    """DRF 库异常必须收敛成 {error:{code,message,details,retryable}} 且保留原状态码。

    修复前这里会在处理器内部抛 TypeError（`api_error() got an unexpected keyword
    argument 'status_code'`），客户端拿到 500，且没有任何可读原因。
    """
    response = _handle(exception)

    assert response.status_code == expected_status
    body = response.data
    assert set(body) == {"error", "meta"}, body
    assert set(body["error"]) == {"code", "message", "details", "retryable"}
    assert body["error"]["code"] == expected_code


def test_method_not_allowed_is_not_a_server_error():
    """405 是最容易被忽略的兜底分支：它必须保持 405，不能变成 500。"""
    response = _handle(drf_exceptions.MethodNotAllowed("GET"))
    assert response.status_code != 500
    assert response.status_code == 405


def test_api_error_helper_and_class_stay_consistent():
    """`api_error()` 与 `ApiError` 的能力差异要有测试兜住。

    `api_error()` 刻意不暴露 status_code（状态码由 ERROR_STATUS 决定）；
    需要自定义状态码时必须直接构造 `ApiError`。这里把两者的契约钉住，
    避免再次出现"误用辅助函数导致处理器崩溃"。
    """
    plain = ApiError("NOT_FOUND")
    assert plain.status_code == 404

    custom = ApiError("NOT_FOUND", "自定义状态", status_code=405)
    assert custom.status_code == 405
    assert custom.as_error_body()["code"] == "NOT_FOUND"

    with pytest.raises(TypeError):
        # 记录事实：辅助函数不接受 status_code，误用会立刻失败而不是静默忽略
        ApiError  # noqa: B018 - 仅为可读性
        from apps.common.http import api_error

        api_error("NOT_FOUND", status_code=405)  # type: ignore[call-arg]
