"""对真实运行的 Django 服务做端到端冒烟检查。

这是**手工运行的脚本**，不是 pytest 用例（文件名不以 test_ 开头，pytest 不会收集），
因为它要求一个已经启动的服务进程。

用法：

    # 终端 1（注意 DEBUG=false，见下）
    cd backend
    DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload

    # 终端 2
    python backend/tests/smoke_http.py

为什么必须 DJANGO_DEBUG=false：
Django 在 DEBUG=True 时用调试页覆盖 handler404，
因此“路由未命中 / 带尾斜杠 / 非法 UUID 也返回标准 JSON 错误”这条契约
只在 DEBUG=False 下成立。开发时看到 HTML 404 属于预期行为。

脚本只做只读探测，不写入任何业务数据。
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"

# 带 cookie jar：CSRF 校验要求 cookie 与 X-CSRFToken 同时匹配
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(path: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with OPENER.open(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw.strip() else None, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"_non_json": raw.strip()[:80]}
        return exc.code, parsed, dict(exc.headers)


def wait_for_server(attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            urllib.request.urlopen(f"{BASE}/auth/csrf", timeout=3)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    if not wait_for_server():
        print("服务未在预期时间内启动；请先在 backend/ 下运行 manage.py runserver")
        return 1

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  ' + detail}")
        if not condition:
            failures.append(label)

    # 1) CSRF 匿名可取，且设置 cookie
    status, body, headers = call("/auth/csrf")
    check("GET /auth/csrf → 200", status == 200, f"got {status}")
    token = (body or {}).get("data", {}).get("csrf_token")
    check("csrf_token 非空", bool(token))
    check("envelope 带 meta.request_id", bool((body or {}).get("meta", {}).get("request_id")))
    check("Set-Cookie 含 csrftoken", "csrftoken" in headers.get("Set-Cookie", ""))
    check("响应头回显 X-Request-Id", bool(headers.get("X-Request-Id")))
    cookie_token = next((c.value for c in JAR if c.name == "csrftoken"), None)
    # Django 会随机掩码令牌：cookie 里是未掩码密钥，data.csrf_token 是掩码值，
    # 两者本就不同。契约只要求前端拿 data.csrf_token 放进 X-CSRFToken，
    # cookie 随请求自动携带 —— 可用性由下面第 4 步验证。
    check("csrftoken cookie 已下发", bool(cookie_token))

    # 2) 未登录 401
    status, body, _ = call("/projects")
    check("GET /projects 未登录 → 401", status == 401, f"got {status}")
    check("错误码 AUTH_REQUIRED", (body or {}).get("error", {}).get("code") == "AUTH_REQUIRED")
    check("未登录响应不含 data", "data" not in (body or {}))

    # 3) 匿名 POST 缺 CSRF → 403
    status, body, _ = call("/auth/login", "POST", {"username": "x", "password": "y"})
    check("POST /auth/login 无 CSRF → 403", status == 403, f"got {status}")
    check("错误码 CSRF_FAILED", (body or {}).get("error", {}).get("code") == "CSRF_FAILED")

    # 4) 带 CSRF 的错误凭证 → 401（cookie + header 都要带）
    status, body, _ = call(
        "/auth/login",
        "POST",
        {"username": "nobody", "password": "wrong"},
        {"X-CSRFToken": token or ""},
    )
    check("POST /auth/login 错误凭证 → 401", status == 401, f"got {status}")
    check(
        "错误码 INVALID_CREDENTIALS",
        (body or {}).get("error", {}).get("code") == "INVALID_CREDENTIALS",
        str(body)[:120],
    )

    # 5) 带 CSRF 的未知字段 → 400（同时证明 CSRF 已通过）
    status, body, _ = call(
        "/auth/login",
        "POST",
        {"username": "x", "password": "y", "is_superuser": True},
        {"X-CSRFToken": token or ""},
    )
    check("POST /auth/login 未知字段 → 400", status == 400, f"got {status}")
    check(
        "未知字段被点名",
        "is_superuser" in json.dumps((body or {}).get("error", {}).get("details", {})),
        str(body)[:140],
    )

    # 6) 尾斜杠不重定向，且仍是 JSON
    status, body, _ = call("/auth/me/")
    check("GET /auth/me/ 尾斜杠 → 404", status == 404, f"got {status}")
    check(
        "尾斜杠也是标准 JSON 错误",
        (body or {}).get("error", {}).get("code") == "NOT_FOUND",
        str(body)[:80],
    )

    # 7) 未知路由 → JSON
    status, body, _ = call("/nope")
    check("未知路由 → 404 JSON", status == 404 and "error" in (body or {}), str(body)[:80])

    # 8) 非法 UUID → 404 JSON（路由不匹配，走兜底）
    status, body, _ = call("/projects/not-a-uuid")
    check("非法 UUID → 404 JSON", status == 404 and "error" in (body or {}), str(body)[:80])

    print()
    if failures:
        print(f"冒烟失败 {len(failures)} 项：{failures}")
        return 1
    print(f"端到端冒烟全部通过（{19} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
