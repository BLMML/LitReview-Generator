"""accounts.api：csrf、login、logout、me。

职责（docs/mvp/01 第 4.1 节）：
- 实现 csrf、login、logout、me；管理员用 Django 管理命令建号；
- 登录自身也校验 CSRF，登录后前端重取令牌；
- 未登录统一 401，不返回业务资源信息。
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.middleware.csrf import get_token
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny

from apps.common.auth import enforce_csrf_or_403, require_actor
from apps.common.http import StrictSerializer, api_error, success


class LoginInputSerializer(StrictSerializer):
    """POST /auth/login 入参；字段与 LoginInput 契约一致。"""

    username = serializers.CharField(min_length=1, max_length=150)
    password = serializers.CharField(min_length=1, max_length=256, trim_whitespace=False)


def user_dto(user: Any) -> dict[str, Any]:
    """UserDTO：id 转字符串，绝不假设是 UUID。"""
    full_name = (user.get_full_name() or "").strip()
    return {
        "id": str(user.pk),
        "username": user.get_username(),
        "display_name": full_name or user.get_username(),
    }


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def get_csrf(request: Any) -> Any:
    """GET /auth/csrf → {csrf_token}，同时设置 cookie。"""
    token = get_token(request)
    response = success({"csrf_token": token}, request=request)
    # DRF 视图默认 csrf_exempt，这里显式写 cookie，保证前端首次即可带上 X-CSRFToken
    from django.conf import settings as django_settings

    response.set_cookie(
        "csrftoken",
        token,
        max_age=31449600,
        samesite=django_settings.CSRF_COOKIE_SAMESITE,
        secure=django_settings.CSRF_COOKIE_SECURE,
        httponly=django_settings.CSRF_COOKIE_HTTPONLY,
    )
    return response


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def login(request: Any) -> Any:
    """POST /auth/login → UserDTO，并设置 session cookie。"""
    # 匿名 POST 也必须校验 CSRF，否则登录接口会成为绕过点
    enforce_csrf_or_403(request)

    serializer = LoginInputSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    user = authenticate(
        request,
        username=serializer.validated_data["username"],
        password=serializer.validated_data["password"],
    )
    if user is None or not user.is_active:
        # 不区分“用户不存在”和“密码错误”，避免账号枚举
        raise api_error("INVALID_CREDENTIALS")

    django_login(request, user)
    # login() 会轮换 CSRF token；前端登录成功后须重新 GET /auth/csrf
    return success(user_dto(user), request=request)


@api_view(["POST"])
def logout(request: Any) -> Any:
    """POST /auth/logout → {logged_out:true}。"""
    require_actor(request)
    django_logout(request)
    return success({"logged_out": True}, request=request)


@api_view(["GET"])
def me(request: Any) -> Any:
    """GET /auth/me → UserDTO。"""
    actor = require_actor(request)
    return success(user_dto(actor.user), request=request)


urlpatterns = [
    path("auth/csrf", get_csrf, name="auth-csrf"),
    path("auth/login", login, name="auth-login"),
    path("auth/logout", logout, name="auth-logout"),
    path("auth/me", me, name="auth-me"),
]
