"""common.auth：Session 认证适配、CSRF 强制、未登录统一 401，并提供 ActorContext。

职责（docs/mvp/01 第 3.3 节）：
- `require_actor(request) → ActorContext`；未登录不返回业务资源信息。
- CSRF 无效统一 403 CSRF_FAILED（包括匿名 POST 登录），不直接依赖 DRF 默认状态码。

ActorContext 不是 HTTP 实体，只承载“谁在操作 + 本次请求标识”，不传递 Django request。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rest_framework import exceptions as drf_exceptions
from rest_framework.authentication import SessionAuthentication as DRFSessionAuthentication
from rest_framework.permissions import BasePermission

from apps.common.http import api_error, request_id_of


@dataclass(frozen=True)
class ActorContext:
    """一次用户发起操作的上下文：用户与 request_id。"""

    user: Any
    request_id: str

    @property
    def user_id(self) -> int:
        # Django 内置 User 的 ID 对外转字符串，绝不假设为 UUID
        return self.user.pk


def require_actor(request: Any) -> ActorContext:
    """从请求取出 ActorContext；未登录抛 401 AUTH_REQUIRED。"""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise api_error("AUTH_REQUIRED")
    return ActorContext(user=user, request_id=request_id_of(request))


class SessionAuthentication(DRFSessionAuthentication):
    """同域 Session Cookie 认证 + CSRF 强制。

    - 未登录返回 None，交由权限类产生统一 401（authenticate_header 返回 "Session"）；
    - CSRF 失败统一映射为 403 CSRF_FAILED，而不是 DRF 默认的 PermissionDenied。
    """

    def authenticate(self, request: Any) -> tuple[Any, None] | None:
        user = getattr(request._request, "user", None)
        if not user or not user.is_active:
            return None
        self.enforce_csrf(request)
        return (user, None)

    def enforce_csrf(self, request: Any) -> None:
        try:
            super().enforce_csrf(request)
        except drf_exceptions.PermissionDenied as exc:
            raise api_error("CSRF_FAILED") from exc

    def authenticate_header(self, request: Any) -> str:
        # 非空 → DRF 对未认证请求使用 401 而不是 403
        return "Session"


def enforce_csrf_or_403(request: Any) -> None:
    """对匿名写请求也强制 CSRF。

    DRF 的 SessionAuthentication 只在“已认证”时才校验 CSRF，
    而契约要求登录本身也必须带 X-CSRFToken；登录视图必须显式调用本函数。
    """
    SessionAuthentication().enforce_csrf(request)


class IsAuthenticated(BasePermission):
    """默认权限：登录即可访问；更细的归属校验在服务层完成。"""

    def has_permission(self, request: Any, view: Any) -> bool:
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated)
