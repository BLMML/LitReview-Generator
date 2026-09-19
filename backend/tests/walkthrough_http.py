"""真实业务闭环走查：登录 → 建项目 → 乐观锁编辑 → 越权与冲突。

这是**手工运行的脚本**，不是 pytest 用例（文件名不以 test_ 开头，pytest 不会收集）。
它对一个真实运行的服务发真请求，走的是骨架里已经实现的那条业务闭环，
用来肉眼确认 envelope、认证、CSRF、乐观锁、项目隔离确实按契约工作。

准备两个用户（只需一次）：

    cd backend
    python manage.py shell -c "from django.contrib.auth import get_user_model as g; U=g(); \
U.objects.create_user('alice', password='demo-pass-123'); \
U.objects.create_user('bob', password='demo-pass-123')"

然后（注意 DEBUG=false）：

    # 终端 1
    cd backend
    DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload

    # 终端 2
    python backend/tests/walkthrough_http.py

可用环境变量覆盖：DEMO_USER / DEMO_PASSWORD / DEMO_OTHER_USER。
脚本会真实创建一个项目，并在结束时把该项目标记为已取消纳入的初始状态——
MVP 没有物理删除接口，所以测试项目会留在开发库里，这是预期行为。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000/api/v1"

USER = os.environ.get("DEMO_USER", "alice")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-pass-123")
OTHER_USER = os.environ.get("DEMO_OTHER_USER", "bob")

FAILURES: list[str] = []


class Session:
    """一个带 cookie jar 的会话，模拟浏览器同源行为。"""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf_token: str | None = None

    def call(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        send_csrf: bool = True,
        extra_headers: dict | None = None,
    ) -> tuple[int, dict | None]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if send_csrf and method != "GET" and self.csrf_token:
            request.add_header("X-CSRFToken", self.csrf_token)
        for name, value in (extra_headers or {}).items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"_non_json": raw.strip()[:100]}

    def refresh_csrf(self) -> None:
        _, body = self.call("/auth/csrf")
        self.csrf_token = ((body or {}).get("data") or {}).get("csrf_token")


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  ← ' + detail}")
    if not condition:
        FAILURES.append(label)


def error_code(body: dict | None) -> str:
    return ((body or {}).get("error") or {}).get("code", "<无错误码>")


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
        print("服务未启动。请先在 backend/ 下运行：")
        print("    DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload")
        return 1

    print("=" * 68)
    print(f"1. 认证闭环（用户 {USER}）")
    print("=" * 68)
    a = Session()
    a.refresh_csrf()
    check("取到 CSRF 令牌", bool(a.csrf_token))

    status, body = a.call("/auth/me")
    check("登录前访问 /auth/me → 401", status == 401, f"got {status}")
    check("错误码 AUTH_REQUIRED", error_code(body) == "AUTH_REQUIRED", error_code(body))

    status, body = a.call("/auth/login", "POST", {"username": USER, "password": PASSWORD})
    if status != 200:
        print(f"\n登录失败（{status} {error_code(body)}）。")
        print("请先建用户（只需一次）：")
        print(
            "    python manage.py shell -c \"from django.contrib.auth import get_user_model as g; "
            f"U=g(); U.objects.create_user('{USER}', password='{PASSWORD}')\""
        )
        return 1
    user = body["data"]
    check("登录成功 → 200", True)
    check("UserDTO 字段完整", set(user) == {"id", "username", "display_name"}, str(user))
    check("id 是字符串", isinstance(user["id"], str))
    # 登录会轮换令牌，前端必须重取
    a.refresh_csrf()

    status, body = a.call("/auth/me")
    check("登录后 /auth/me → 200", status == 200 and body["data"]["username"] == USER)

    print()
    print("=" * 68)
    print("2. 项目创建与乐观锁")
    print("=" * 68)
    status, body = a.call("/projects")
    total_before = body["meta"]["total"]
    check("项目列表分页 envelope", {"page", "page_size", "total"} <= set(body["meta"]), str(body["meta"]))
    print(f"      当前项目数：{total_before}")

    status, body = a.call(
        "/projects",
        "POST",
        {"title": "走查项目", "research_question": "注意力机制在小样本场景下是否仍然有效？"},
    )
    check("创建项目 → 201", status == 201, f"got {status} {error_code(body)}")
    project = body["data"]
    project_id = project["id"]
    check("version 初始为 1", project["version"] == 1, str(project.get("version")))
    check("corpus_revision 初始为 1", project["corpus_revision"] == 1)
    check("latest_run_id 为 null", project["latest_run_id"] is None)
    check("active_job_id 为 null", project["active_job_id"] is None)
    check("output_language 默认 zh-CN", project["output_language"] == "zh-CN")

    status, body = a.call(f"/projects/{project_id}")
    check("读取项目详情 → 200", status == 200 and body["data"]["id"] == project_id)

    # PATCH title：只涨 version，不动 corpus_revision
    status, body = a.call(
        f"/projects/{project_id}", "PATCH", {"expected_version": 1, "title": "走查项目（改名）"}
    )
    check("改 title → 200", status == 200, f"got {status} {error_code(body)}")
    check("version 1→2", body["data"]["version"] == 2, str(body["data"].get("version")))
    check("corpus_revision 保持 1", body["data"]["corpus_revision"] == 1)

    # PATCH research_question：version 与 corpus_revision 同时涨
    status, body = a.call(
        f"/projects/{project_id}",
        "PATCH",
        {"expected_version": 2, "research_question": "换一个完全不同的研究问题，用于验证语料版本。"},
    )
    check("改研究问题 → 200", status == 200, f"got {status} {error_code(body)}")
    check("version 2→3", body["data"]["version"] == 3)
    check("corpus_revision 1→2", body["data"]["corpus_revision"] == 2)

    # 用过期版本再改：必须 409，不能静默覆盖
    status, body = a.call(
        f"/projects/{project_id}", "PATCH", {"expected_version": 1, "title": "并发覆盖"}
    )
    check("过期 expected_version → 409", status == 409, f"got {status}")
    check("错误码 VERSION_CONFLICT", error_code(body) == "VERSION_CONFLICT", error_code(body))
    check(
        "details 带回 current_version",
        (body.get("error", {}).get("details") or {}).get("current_version") == 3,
        str(body.get("error", {}).get("details")),
    )

    # 无变化保存：不递增版本
    status, body = a.call(
        f"/projects/{project_id}",
        "PATCH",
        {"expected_version": 3, "title": "走查项目（改名）"},
    )
    check("内容相同的保存不涨版本", status == 200 and body["data"]["version"] == 3,
          str(body.get("data", {}).get("version")))

    print()
    print("=" * 68)
    print("3. 输入校验")
    print("=" * 68)
    status, body = a.call(
        "/projects", "POST", {"title": "x", "research_question": "够长的问题描述文本。", "owner": 999}
    )
    check("未知字段 → 400", status == 400, f"got {status}")
    check("未知字段被点名", "owner" in json.dumps(body.get("error", {}).get("details", {})))

    status, body = a.call("/projects", "POST", {"title": "x", "research_question": "太短"})
    check("研究问题过短 → 400", status == 400, f"got {status}")

    status, body = a.call(
        "/projects",
        "POST",
        {"title": "x" * 121, "research_question": "够长的问题描述文本。"},
    )
    check("标题超长 → 400", status == 400, f"got {status}")

    status, body = a.call(
        "/projects",
        "POST",
        {"title": "x", "research_question": "够长。", "output_language": "en-US"},
    )
    check("非法 output_language → 400", status == 400, f"got {status}")

    print()
    print("=" * 68)
    print("4. 任务接口（尚无任务）")
    print("=" * 68)
    status, body = a.call(f"/projects/{project_id}/jobs")
    check("项目任务列表 → 200", status == 200, f"got {status}")
    check("新项目任务为空", body["data"] == [] and body["meta"]["total"] == 0)

    status, body = a.call("/jobs/11111111-1111-4111-8111-111111111111")
    check("不存在的 Job → 404", status == 404, f"got {status}")

    # POST /runs 标记为 K：缺幂等键必须先被拦住
    status, body = a.call(f"/projects/{project_id}/runs", "POST", {})
    check("缺 Idempotency-Key → 400", status == 400, f"got {status}")
    check("错误码 VALIDATION_ERROR", error_code(body) == "VALIDATION_ERROR", error_code(body))

    # 格式不合法的幂等键同样拒绝
    status, body = a.call(
        f"/projects/{project_id}/runs",
        "POST",
        {},
        extra_headers={"Idempotency-Key": "not-a-uuid"},
    )
    check("非法 Idempotency-Key → 400", status == 400, f"got {status}")

    # 带上合法幂等键才会进入服务层；该阶段尚未实现，应明确 503
    status, body = a.call(
        f"/projects/{project_id}/runs",
        "POST",
        {},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("创建 Run → 503（未实现，阶段 C）", status == 503, f"got {status} {error_code(body)}")
    check("未实现也走统一 envelope", error_code(body) == "SERVICE_UNAVAILABLE", error_code(body))
    check(
        "未实现不伪装成成功，也不返回 data",
        "data" not in body and body["error"]["retryable"] is False,
        str(body)[:120],
    )

    print()
    print("=" * 68)
    print(f"5. 项目隔离（换用户 {OTHER_USER}）")
    print("=" * 68)
    b = Session()
    b.refresh_csrf()
    status, body = b.call("/auth/login", "POST", {"username": OTHER_USER, "password": PASSWORD})
    if status != 200:
        print(f"      跳过：用户 {OTHER_USER} 不存在（{status} {error_code(body)}）")
        print(
            "      建用户：python manage.py shell -c \"from django.contrib.auth import get_user_model as g; "
            f"U=g(); U.objects.create_user('{OTHER_USER}', password='{PASSWORD}')\""
        )
    else:
        b.refresh_csrf()
        status, body = b.call(f"/projects/{project_id}")
        check("他人项目 → 404", status == 404, f"got {status}")
        check("错误码 NOT_FOUND", error_code(body) == "NOT_FOUND", error_code(body))
        check("不泄露资源归属", "owner" not in json.dumps(body), str(body)[:80])

        status, body = b.call(f"/projects/{project_id}", "PATCH",
                              {"expected_version": 3, "title": "越权改名"})
        check("越权修改 → 404", status == 404, f"got {status}")

        status, body = b.call("/projects")
        check("他人列表里没有该项目",
              all(item["id"] != project_id for item in body["data"]))

    print()
    print("=" * 68)
    print("6. 退出")
    print("=" * 68)
    status, body = a.call("/auth/logout", "POST", {})
    check("登出 → 200", status == 200 and body["data"] == {"logged_out": True}, f"got {status}")

    status, body = a.call("/auth/me")
    check("登出后 /auth/me → 401", status == 401, f"got {status}")

    print()
    print("=" * 68)
    if FAILURES:
        print(f"走查失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("业务闭环走查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
