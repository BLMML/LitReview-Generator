"""common.http：成功 / 错误响应包装、分页、request_id、DRF 异常转换。

职责（docs/mvp/01 第 3.3 节）：
- `success(data, meta)` 与 `api_error(code, message, details, retryable)`；
- 分页结果符合 docs/mvp/03 第 1.2 节；
- 拒绝未知字段与未知过滤参数；
- request_id 注入（含日志过滤器）。

契约要点：
- 成功：``{"data": ..., "meta": {"request_id": ...}}``
- 列表：``meta`` 额外含 page / page_size / total
- 错误：``{"error": {"code","message","details","retryable"}, "meta": {...}}``
- 错误体不得包含堆栈、密钥或内部路径。
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Mapping
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.serializers import Serializer

logger = logging.getLogger("apps.common.http")

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_PREFIX = "req_"
REQUEST_ID_MAX_LENGTH = 64
IDEMPOTENCY_HEADER = "Idempotency-Key"


def idempotency_header_key(request: Any) -> str | None:
    """读取标记 K 的写请求必须携带的幂等键原始值。"""
    return request.headers.get(IDEMPOTENCY_HEADER)

# ── 上下文：request_id / job_id / stage ───────────────────────
_context = threading.local()


def _ctx() -> dict[str, str]:
    store = getattr(_context, "value", None)
    if store is None:
        store = {"request_id": "-", "job_id": "-", "stage": "-"}
        _context.value = store
    return store


def set_log_context(*, request_id: str | None = None, job_id: str | None = None,
                    stage: str | None = None) -> None:
    """由中间件与 worker 阶段代码写入；
    不得在其中放项目状态，只放日志标识。"""
    store = _ctx()
    if request_id is not None:
        store["request_id"] = request_id
    if job_id is not None:
        store["job_id"] = job_id
    if stage is not None:
        store["stage"] = stage


def clear_log_context() -> None:
    store = _ctx()
    store["request_id"] = "-"
    store["job_id"] = "-"
    store["stage"] = "-"


def current_request_id() -> str:
    return _ctx()["request_id"]


# ── 日志过滤器 ────────────────────────────────────────────────
class RequestContextFilter(logging.Filter):
    """给每条日志补上 request_id / job_id / stage。"""

    def filter(self, record: logging.LogRecord) -> bool:
        store = _ctx()
        for key in ("request_id", "job_id", "stage"):
            if not hasattr(record, key):
                setattr(record, key, store[key])
        return True


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-csrftoken)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|password)\b\s*[:=]\s*\S+"),
)


class SecretRedactionFilter(logging.Filter):
    """兜底脱敏：认证头、Cookie、Key 与密码不得进入普通日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 格式化失败时不阻断日志
            return True
        redacted = message
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(lambda m: f"{m.group(1) if m.groups() else '***'}=[REDACTED]",
                                   redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# ── request_id 中间件 ─────────────────────────────────────────
def _normalize_request_id(raw: str | None) -> str:
    if not raw:
        return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex[:16]}"
    # 只接受保守字符集，避免响应头注入
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "", raw)[:REQUEST_ID_MAX_LENGTH]
    return cleaned or f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex[:16]}"


class RequestIdMiddleware:
    """读取或生成 request_id，写入 request 与响应头。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.headers.get(REQUEST_ID_HEADER)
        request.request_id = _normalize_request_id(raw)
        set_log_context(request_id=request.request_id)
        try:
            response = self.get_response(request)
        finally:
            clear_log_context()
        response[REQUEST_ID_HEADER] = request.request_id
        return response


def request_id_of(request: Any) -> str:
    value = getattr(request, "request_id", None)
    return value or current_request_id()


# ── 错误码与 HTTP 状态映射（docs/mvp/03 第 3 节）───────────────
ERROR_STATUS: dict[str, int] = {
    # 400
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
    # 401
    "AUTH_REQUIRED": status.HTTP_401_UNAUTHORIZED,
    "INVALID_CREDENTIALS": status.HTTP_401_UNAUTHORIZED,
    # 403
    "CSRF_FAILED": status.HTTP_403_FORBIDDEN,
    # 404
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    # 409
    "PROJECT_BUSY": status.HTTP_409_CONFLICT,
    "IDEMPOTENCY_CONFLICT": status.HTTP_409_CONFLICT,
    "VERSION_CONFLICT": status.HTTP_409_CONFLICT,
    "RUN_NOT_READY": status.HTTP_409_CONFLICT,
    "RESULT_NOT_READY": status.HTTP_409_CONFLICT,
    "STALE_RUN": status.HTTP_409_CONFLICT,
    "WARNINGS_NOT_ACKNOWLEDGED": status.HTTP_409_CONFLICT,
    "INVALID_JOB_STATE": status.HTTP_409_CONFLICT,
    # 413
    "FILE_TOO_LARGE": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    # 422
    "UNSUPPORTED_PDF": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "PAGE_LIMIT_EXCEEDED": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "PAPER_LIMIT_EXCEEDED": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "INSUFFICIENT_PAPERS": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "INVALID_EVIDENCE": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "VALIDATION_REQUIRED": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "STRUCTURAL_VALIDATION_FAILED": status.HTTP_422_UNPROCESSABLE_ENTITY,
    # 429
    "RATE_LIMITED": status.HTTP_429_TOO_MANY_REQUESTS,
    # 503
    "SERVICE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
}

# 后台错误码：放在 Job.error，不作为同步 HTTP 错误返回
JOB_ERROR_CODES = (
    "PDF_PARSE_FAILED",
    "ARXIV_FETCH_FAILED",
    "MODEL_TIMEOUT",
    "MODEL_RATE_LIMITED",
    "MODEL_OUTPUT_INVALID",
    "MODEL_AUTH_FAILED",
    "MODEL_BALANCE_INSUFFICIENT",
    "BUDGET_EXCEEDED",
    "WORKER_LOST",
    "INTERNAL_ERROR",
)

DEFAULT_MESSAGES: dict[str, str] = {
    "VALIDATION_ERROR": "请求参数不合法",
    "AUTH_REQUIRED": "请先登录",
    "INVALID_CREDENTIALS": "用户名或密码不正确",
    "CSRF_FAILED": "CSRF 校验失败，请刷新令牌后重试",
    "NOT_FOUND": "资源不存在或无权访问",
    "PROJECT_BUSY": "项目已有进行中的任务，请等待其结束",
    "IDEMPOTENCY_CONFLICT": "同一幂等键已用于不同的请求内容",
    "VERSION_CONFLICT": "内容已被修改，请刷新后重试",
    "RUN_NOT_READY": "分析尚未就绪",
    "RESULT_NOT_READY": "结果尚未就绪",
    "STALE_RUN": "分析快照已过时，请重新创建分析",
    "WARNINGS_NOT_ACKNOWLEDGED": "存在需要显式确认的警示",
    "INVALID_JOB_STATE": "当前任务状态不允许该操作",
    "FILE_TOO_LARGE": "文件超过 25 MiB 上限",
    "UNSUPPORTED_PDF": "PDF 无法解析或已加密",
    "PAGE_LIMIT_EXCEEDED": "PDF 页数超过 50 页上限",
    "PAPER_LIMIT_EXCEEDED": "项目文献数超过 30 篇上限",
    "INSUFFICIENT_PAPERS": "至少需要 2 篇纳入文献",
    "INVALID_EVIDENCE": "证据引用不合法",
    "VALIDATION_REQUIRED": "需要先完成核验",
    "STRUCTURAL_VALIDATION_FAILED": "正文结构校验未通过",
    "RATE_LIMITED": "请求过于频繁",
    "SERVICE_UNAVAILABLE": "服务暂时不可用",
}


class ApiError(drf_exceptions.APIException):
    """统一业务错误。序列化时只输出 code/message/details/retryable。"""

    default_detail = "请求处理失败"

    def __init__(
        self,
        code: str,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, self.default_detail)
        self.details = dict(details or {})
        self.retryable = retryable
        super().__init__(self.message)
        self.status_code = status_code or ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST)

    def as_error_body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


def api_error(
    code: str,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
) -> ApiError:
    """构造统一错误；调用方直接 ``raise api_error(...)``。"""
    return ApiError(code, message=message, details=details, retryable=retryable)


# ── 响应包装 ──────────────────────────────────────────────────
def success(
    data: Any,
    *,
    request: Any | None = None,
    meta: Mapping[str, Any] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """成功响应：``{"data": ..., "meta": {"request_id": ...}}``。"""
    payload_meta: dict[str, Any] = {"request_id": request_id_of(request)}
    if meta:
        payload_meta.update(meta)
    return Response({"data": data, "meta": payload_meta}, status=status_code)


def accepted(job: Any, *, request: Any | None = None) -> Response:
    """202 固定形状：data 为 JobAccepted。"""
    return success(job, request=request, status_code=status.HTTP_202_ACCEPTED)


def error_response(error: ApiError, *, request: Any | None = None) -> Response:
    return Response(
        {"error": error.as_error_body(), "meta": {"request_id": request_id_of(request)}},
        status=error.status_code,
    )


# ── 分页 ──────────────────────────────────────────────────────
class EnvelopePagination(PageNumberPagination):
    """page / page_size 分页，输出统一 envelope。"""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"

    def get_paginated_response(self, data: Any) -> Response:
        request = self.request
        return success(
            data,
            request=request,
            meta={
                "page": self.page.number,
                "page_size": self.get_page_size(request),
                "total": self.page.paginator.count,
            },
        )


# ── 严格输入校验 ──────────────────────────────────────────────
class StrictSerializer(Serializer):
    """拒绝未列出的输入字段（docs/mvp/03 第 1.1 节）。

    DRF 默认忽略未知字段；契约要求返回 400，因此所有写请求序列化器都应继承本类。
    """

    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, Mapping):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise api_error(
                    "VALIDATION_ERROR",
                    "请求包含未支持的字段",
                    {"fields": {name: ["未支持的字段"] for name in unknown}},
                )
        return super().to_internal_value(data)


def reject_unknown_query_params(request: Any, allowed: set[str]) -> None:
    """未知过滤参数返回 400，而不是被静默忽略。"""
    unknown = sorted(set(request.query_params.keys()) - allowed)
    if unknown:
        raise api_error(
            "VALIDATION_ERROR",
            "请求包含未支持的查询参数",
            {"params": {name: ["未支持的参数"] for name in unknown}},
        )


def reject_unknown_multipart_fields(request: Any, allowed: set[str]) -> None:
    """multipart 上传体同样拒绝未列出的字段（docs/mvp/03 第 1.1 节）。

    序列化器拦不住 multipart：`request.data` 里既有表单字段也有文件，
    必须在这里显式比对，否则多余字段会被静默忽略。
    """
    unknown = sorted(set(request.data.keys()) - allowed)
    if unknown:
        raise api_error(
            "VALIDATION_ERROR",
            "请求包含未支持的字段",
            {"fields": {name: ["未支持的字段"] for name in unknown}},
        )


def paginate(queryset: Any, request: Any) -> Response:
    """按 envelope 分页；排序由调用方（selectors）保证稳定。"""
    paginator = EnvelopePagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = getattr(queryset, "serializer_class", None)
    if serializer is None:
        raise api_error("SERVICE_UNAVAILABLE", "分页对象缺少序列化器")
    return paginator.get_paginated_response(serializer(page, many=True).data)


# ── DRF 异常转换 ──────────────────────────────────────────────
_DRF_CODE_MAP: tuple[tuple[type[Exception], str], ...] = (
    (drf_exceptions.NotAuthenticated, "AUTH_REQUIRED"),
    (drf_exceptions.AuthenticationFailed, "INVALID_CREDENTIALS"),
    (drf_exceptions.PermissionDenied, "NOT_FOUND"),
)


def exception_handler(exc: Exception, context: Mapping[str, Any]) -> Response | None:
    """把所有异常收敛到统一错误 envelope。

    - 越权与不存在一律表现为 404，不泄露资源属于他人；
    - 未登录统一 401，不返回业务资源信息；
    - 未知异常记录堆栈后返回 INTERNAL_ERROR，响应体不含内部细节。
    """
    request = context.get("request") if context else None

    if isinstance(exc, ApiError):
        return error_response(exc, request=request)

    if isinstance(exc, Http404):
        return error_response(api_error("NOT_FOUND"), request=request)
    if isinstance(exc, DjangoPermissionDenied):
        return error_response(api_error("NOT_FOUND"), request=request)

    for exc_type, code in _DRF_CODE_MAP:
        if isinstance(exc, exc_type):
            return error_response(api_error(code), request=request)

    if isinstance(exc, drf_exceptions.ValidationError):
        return error_response(
            api_error("VALIDATION_ERROR", "请求参数不合法", {"fields": exc.detail}),
            request=request,
        )

    if isinstance(exc, drf_exceptions.Throttled):
        error = api_error("RATE_LIMITED", retryable=True)
        response = error_response(error, request=request)
        if exc.wait is not None:
            response["Retry-After"] = str(int(exc.wait))
        return response

    if isinstance(exc, drf_exceptions.APIException):
        code = "NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR"
        # 这里必须直接构造 ApiError：`api_error()` 辅助函数没有 status_code 参数，
        # 传给它会在**异常处理器内部**抛 TypeError，把本该是标准 envelope 的响应变成 500。
        return error_response(
            ApiError(code, str(exc.detail), status_code=exc.status_code), request=request
        )

    logger.exception("未处理的服务器异常", extra={"request_id": request_id_of(request)})
    return error_response(api_error("SERVICE_UNAVAILABLE", retryable=True), request=request)


# ── URL 解析失败的兜底：仍然返回统一 envelope ──────────────────
def _error_json(request: Any, error: ApiError) -> Any:
    from django.http import JsonResponse

    return JsonResponse(
        {"error": error.as_error_body(), "meta": {"request_id": request_id_of(request)}},
        status=error.status_code,
    )


def json_404(request: Any, exception: Exception | None = None) -> Any:
    """路由未命中（包括带尾斜杠的地址）也返回标准错误 JSON。"""
    return _error_json(request, api_error("NOT_FOUND"))


def json_500(request: Any) -> Any:
    """兜底 500：不泄露堆栈、密钥或内部路径。"""
    logger.error("未捕获的服务器错误", extra={"request_id": request_id_of(request)})
    return _error_json(request, api_error("SERVICE_UNAVAILABLE", retryable=True))
