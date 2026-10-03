"""真实业务闭环走查（M05）：登录 → 建项目 → 上传 PDF → 列表 → 编辑 → 下载 → 越权。

这是**手工运行的脚本**，不是 pytest 用例（文件名不以 test_ 开头，pytest 不会收集）。
它对一个真实运行的服务发真请求，用真实的 multipart 上传与二进制下载验证
M05「文献接入与文件授权」这条闭环，包括 pytest 里不容易覆盖的部分：
真实 WSGI 下的报文字节、multipart 边界、下载字节一致性与响应头编码。

准备两个用户（只需一次）：

    cd backend
    python manage.py shell -c "from django.contrib.auth import get_user_model as g; U=g(); \\
U.objects.create_user('alice', password='demo-pass-123'); \\
U.objects.create_user('bob', password='demo-pass-123')"

然后（注意 DEBUG=false）：

    # 终端 1
    cd backend
    DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload

    # 终端 2（回到仓库根）
    python backend/tests/walkthrough_papers_http.py

可用环境变量覆盖：DEMO_USER / DEMO_PASSWORD / DEMO_OTHER_USER / DEMO_BASE。

脚本会真实创建项目与文献；MVP 没有物理删除接口，所以这些记录会留在开发库里，
这是预期行为（与 walkthrough_http.py 一致）。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000/api/v1")

USER = os.environ.get("DEMO_USER", "alice")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-pass-123")
OTHER_USER = os.environ.get("DEMO_OTHER_USER", "bob")

FAILURES: list[str] = []


def make_pdf(pages: int = 2) -> bytes:
    """用 pypdf 生成结构合法的真 PDF；不手写字节模板，避免伪造"合法"。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Session:
    """带 cookie jar 的会话，模拟浏览器同源行为。"""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf_token: str | None = None

    def _send(self, request: urllib.request.Request) -> tuple[int, bytes, dict]:
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers or {})

    def call(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        send_csrf: bool = True,
        extra_headers: dict | None = None,
        query: str = "",
    ) -> tuple[int, dict | None]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{BASE}{path}{query}", data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if send_csrf and method != "GET" and self.csrf_token:
            request.add_header("X-CSRFToken", self.csrf_token)
        for name, value in (extra_headers or {}).items():
            request.add_header(name, value)
        status, raw, _headers = self._send(request)
        text = raw.decode("utf-8", errors="replace")
        try:
            return status, json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            return status, {"_non_json": text.strip()[:120]}

    def upload(
        self, path: str, filename: str, payload: bytes, *, key: str, fields: dict | None = None
    ) -> tuple[int, dict | None, dict]:
        """真实的 multipart/form-data 上传（不借助 requests，保持零额外依赖）。"""
        boundary = f"----walkthrough{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for name, value in (fields or {}).items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
        )
        parts.append(payload)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        data = b"".join(parts)

        request = urllib.request.Request(f"{BASE}{path}", data=data, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        request.add_header("Accept", "application/json")
        request.add_header("Idempotency-Key", key)
        if self.csrf_token:
            request.add_header("X-CSRFToken", self.csrf_token)
        status, raw, headers = self._send(request)
        text = raw.decode("utf-8", errors="replace")
        try:
            return status, json.loads(text) if text.strip() else None, headers
        except json.JSONDecodeError:
            return status, {"_non_json": text.strip()[:120]}, headers

    def download(self, path: str) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(f"{BASE}{path}", method="GET")
        request.add_header("Accept", "application/pdf, application/json")
        return self._send(request)

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


def login(session: Session, username: str) -> bool:
    session.refresh_csrf()
    status, body = session.call("/auth/login", "POST", {"username": username, "password": PASSWORD})
    if status != 200:
        return False
    session.refresh_csrf()
    return True


def main() -> int:
    if not wait_for_server():
        print("服务未启动。请先在 backend/ 下运行：")
        print("    DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload")
        return 1

    print("=" * 68)
    print(f"1. 登录并创建项目（用户 {USER}）")
    print("=" * 68)
    a = Session()
    if not login(a, USER):
        print("登录失败。请先建用户（只需一次）：")
        print(
            "    python manage.py shell -c \"from django.contrib.auth import get_user_model as g; "
            f"U=g(); U.objects.create_user('{USER}', password='{PASSWORD}')\""
        )
        return 1
    check("登录成功并重取 CSRF", bool(a.csrf_token))

    status, body = a.call(
        "/projects",
        "POST",
        {
            "title": f"M05 走查 {uuid.uuid4().hex[:6]}",
            "research_question": "文献接入与文件授权是否按契约工作？",
        },
    )
    check("创建项目 → 201", status == 201, f"got {status} {error_code(body)}")
    project = body["data"]
    project_id = project["id"]
    check("corpus_revision 初始为 1", project["corpus_revision"] == 1)

    print()
    print("=" * 68)
    print("2. 上传 PDF（multipart + 幂等键）")
    print("=" * 68)
    payload = make_pdf(3)
    digest = hashlib.sha256(payload).hexdigest()

    status, body, _ = a.upload(
        f"/projects/{project_id}/papers/upload", "注意力机制综述.pdf", payload, key=str(uuid.uuid4())
    )
    check("上传 → 201", status == 201, f"got {status} {error_code(body)}")
    paper = (body or {}).get("data") or {}
    paper_id = paper.get("id")
    check("PaperDTO 字段完整",
          {"id", "project_id", "title", "authors", "year", "source", "source_id", "source_url",
           "included", "version", "file_sha256", "file_size", "page_count", "original_filename",
           "file_url", "created_at", "updated_at"} == set(paper),
          str(sorted(paper))[:120])
    check("title 缺省用文件名", paper.get("title") == "注意力机制综述", str(paper.get("title")))
    check("authors 初始为 []", paper.get("authors") == [])
    check("year 初始为 null", paper.get("year") is None)
    check("source = upload", paper.get("source") == "upload")
    check("included 默认 true", paper.get("included") is True)
    check("version 初始为 1", paper.get("version") == 1)
    check("page_count = 3", paper.get("page_count") == 3, str(paper.get("page_count")))
    check("file_sha256 与本地一致", paper.get("file_sha256") == digest, str(paper.get("file_sha256")))
    check("file_url 指向授权路由", paper.get("file_url") == f"/api/v1/papers/{paper_id}/file")
    check("响应不含磁盘路径", "storage" not in json.dumps(paper).lower(), json.dumps(paper)[:120])

    status, body = a.call(f"/projects/{project_id}")
    check("新增文献后 corpus_revision 递增", body["data"]["corpus_revision"] == 2,
          str(body["data"].get("corpus_revision")))

    print()
    print("=" * 68)
    print("3. 重复上传去重与幂等冲突")
    print("=" * 68)
    status, body, headers = a.upload(
        f"/projects/{project_id}/papers/upload", "换了个名字.pdf", payload, key=str(uuid.uuid4())
    )
    check("同文件重复上传 → 201", status == 201, f"got {status} {error_code(body)}")
    check("返回既有文献 ID", ((body or {}).get("data") or {}).get("id") == paper_id)
    check("带 X-Resource-Reused: true",
          headers.get("X-Resource-Reused") == "true", str(headers.get("X-Resource-Reused")))
    check("既有标题未被覆盖",
          ((body or {}).get("data") or {}).get("title") == "注意力机制综述")
    status, body = a.call(f"/projects/{project_id}/papers")
    check("未新增第二篇文献", body["meta"]["total"] == 1, str(body["meta"]))

    key = str(uuid.uuid4())
    first_status, first_body, _ = a.upload(
        f"/projects/{project_id}/papers/upload", "幂等.pdf", make_pdf(2), key=key
    )
    replay_status, replay_body, _ = a.upload(
        f"/projects/{project_id}/papers/upload", "幂等.pdf", make_pdf(2), key=key
    )
    check("同键同体重放 → 同状态码", first_status == replay_status == 201,
          f"{first_status} / {replay_status}")
    check("同键同体重放 → 同资源",
          first_body["data"]["id"] == replay_body["data"]["id"])

    conflict_status, conflict_body, _ = a.upload(
        f"/projects/{project_id}/papers/upload", "幂等.pdf", make_pdf(5), key=key
    )
    check("同键不同文件 → 409", conflict_status == 409, f"got {conflict_status}")
    check("错误码 IDEMPOTENCY_CONFLICT", error_code(conflict_body) == "IDEMPOTENCY_CONFLICT",
          error_code(conflict_body))

    print()
    print("=" * 68)
    print("4. 上传边界（缺键 / 缺文件 / 非法内容 / 页数）")
    print("=" * 68)
    # 缺 file：先发一次 multipart 但只带 title
    boundary = f"----walkthrough{uuid.uuid4().hex}"
    body_bytes = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="title"\r\n\r\n只有标题\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    request = urllib.request.Request(
        f"{BASE}/projects/{project_id}/papers/upload", data=body_bytes, method="POST"
    )
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Idempotency-Key", str(uuid.uuid4()))
    request.add_header("X-CSRFToken", a.csrf_token or "")
    status, raw, _ = a._send(request)
    body = json.loads(raw.decode("utf-8"))
    check("缺 file → 400", status == 400, f"got {status} {error_code(body)}")
    check("点名 file 字段", "file" in (body.get("error") or {}).get("details", {}).get("fields", {}),
          json.dumps((body.get("error") or {}).get("details")))

    # 缺幂等键：契约要求标记 K 的写请求必须带 UUID 幂等键
    boundary = f"----walkthrough{uuid.uuid4().hex}"
    body_bytes = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="nokey.pdf"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode(),
        make_pdf(1),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{BASE}/projects/{project_id}/papers/upload", data=body_bytes, method="POST"
    )
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("X-CSRFToken", a.csrf_token or "")
    status, raw, _ = a._send(request)
    body = json.loads(raw.decode("utf-8"))
    check("缺幂等键 → 400", status == 400, f"got {status} {error_code(body)}")
    check("错误码 VALIDATION_ERROR", error_code(body) == "VALIDATION_ERROR", error_code(body))

    status, body, _ = a.upload(
        f"/projects/{project_id}/papers/upload", "fake.pdf", b"definitely not a pdf", key=str(uuid.uuid4())
    )
    check("非 PDF 内容 → 422", status == 422, f"got {status} {error_code(body)}")
    check("错误码 UNSUPPORTED_PDF", error_code(body) == "UNSUPPORTED_PDF", error_code(body))

    print()
    print("=" * 68)
    print("5. 列表、元数据编辑与纳入开关")
    print("=" * 68)
    status, body = a.call(f"/projects/{project_id}/papers", query="?page=1&page_size=20")
    check("文献列表分页 envelope",
          {"page", "page_size", "total"} <= set(body["meta"]), str(body["meta"]))
    check("列表按 created_at 倒序",
          [item["id"] for item in body["data"]][0] != paper_id or body["meta"]["total"] == 1)

    status, body = a.call(
        f"/papers/{paper_id}",
        "PATCH",
        {
            "expected_version": 1,
            "title": "注意力机制综述（已校对）",
            "authors": ["张三", "李四"],
            "year": 2021,
        },
    )
    check("编辑元数据 → 200", status == 200, f"got {status} {error_code(body)}")
    updated = body["data"]
    check("title 已更新", updated["title"] == "注意力机制综述（已校对）")
    check("authors 为字符串数组", updated["authors"] == ["张三", "李四"])
    check("year 已更新", updated["year"] == 2021)
    check("version 1→2", updated["version"] == 2, str(updated.get("version")))

    status, body = a.call(
        f"/papers/{paper_id}", "PATCH", {"expected_version": 1, "title": "用旧版本覆盖"}
    )
    check("过期 expected_version → 409", status == 409, f"got {status}")
    check("错误码 VERSION_CONFLICT", error_code(body) == "VERSION_CONFLICT", error_code(body))
    check("带回 current_version", (body.get("error") or {}).get("details", {}).get("current_version") == 2,
          str((body.get("error") or {}).get("details")))

    status, body = a.call(
        f"/papers/{paper_id}", "PATCH", {"expected_version": 2, "included": False}
    )
    check("取消纳入 → 200", status == 200 and body["data"]["included"] is False)
    check("纳入状态变化也涨 version", body["data"]["version"] == 3)

    status, body = a.call(f"/projects/{project_id}/papers", query="?included=false")
    check("按 included=false 过滤命中", [item["id"] for item in body["data"]] == [paper_id])
    status, body = a.call(f"/projects/{project_id}/papers", query="?included=true")
    check("按 included=true 过滤排除", all(item["id"] != paper_id for item in body["data"]))

    status, body = a.call(f"/projects/{project_id}/papers", query="?sort=title")
    check("未知查询参数 → 400", status == 400, f"got {status}")

    print()
    print("=" * 68)
    print("6. 授权下载（真实二进制与报文头）")
    print("=" * 68)
    status, raw, headers = a.download(f"/papers/{paper_id}/file")
    check("下载 → 200", status == 200, f"got {status}")
    check("Content-Type = application/pdf", headers.get("Content-Type") == "application/pdf",
          str(headers.get("Content-Type")))
    check("字节与原文件完全一致", raw == payload,
          f"{len(raw)} vs {len(payload)} bytes")
    check("字节哈希一致", hashlib.sha256(raw).hexdigest() == digest)
    disposition = headers.get("Content-Disposition", "")
    # 非 ASCII 文件名必须百分号编码，否则真实 WSGI 下会 UnicodeEncodeError
    check("Content-Disposition 含 RFC 5987 编码",
          "filename*=UTF-8''" in disposition, disposition)
    check("文件名已百分号编码",
          quote("注意力机制综述.pdf", safe="") in disposition, disposition)
    check("响应头不泄漏磁盘路径", "storage" not in disposition.lower(), disposition)

    print()
    print("=" * 68)
    print(f"7. 越权（换用户 {OTHER_USER}）")
    print("=" * 68)
    b = Session()
    if not login(b, OTHER_USER):
        print(f"      跳过：用户 {OTHER_USER} 不存在。")
        print(
            "      建用户：python manage.py shell -c \"from django.contrib.auth import get_user_model as g; "
            f"U=g(); U.objects.create_user('{OTHER_USER}', password='{PASSWORD}')\""
        )
    else:
        status, _raw, _headers = b.download(f"/papers/{paper_id}/file")
        check("他人下载原文 → 404", status == 404, f"got {status}")

        status, body = b.call(f"/papers/{paper_id}")
        check("他人读取详情 → 404", status == 404, f"got {status}")
        check("错误码 NOT_FOUND", error_code(body) == "NOT_FOUND", error_code(body))
        check("不泄露资源归属", "owner" not in json.dumps(body), json.dumps(body)[:100])

        status, body = b.call(
            f"/papers/{paper_id}", "PATCH", {"expected_version": 3, "title": "越权改名"}
        )
        check("他人编辑 → 404", status == 404, f"got {status}")

        status, body, _ = b.upload(
            f"/projects/{project_id}/papers/upload", "越权.pdf", make_pdf(1), key=str(uuid.uuid4())
        )
        check("向他人项目上传 → 404", status == 404, f"got {status}")

        status, body = b.call(f"/projects/{project_id}/papers")
        check("他人文献列表 → 404", status == 404, f"got {status}")
        check("错误码 NOT_FOUND", error_code(body) == "NOT_FOUND", error_code(body))

    print()
    print("=" * 68)
    print("8. arXiv 导入受理（1—10 个 ID，拒绝 URL）")
    print("=" * 68)
    status, body = a.call(
        f"/projects/{project_id}/imports/arxiv",
        "POST",
        {"arxiv_ids": ["https://arxiv.org/abs/2401.00001"]},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("URL 形式的 ID → 400", status == 400, f"got {status} {error_code(body)}")

    status, body = a.call(
        f"/projects/{project_id}/imports/arxiv",
        "POST",
        {"arxiv_ids": ["2401.00001", "2401.00002"]},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("受理 → 202", status == 202, f"got {status} {error_code(body)}")
    if status == 202:
        data = body["data"]
        check("resource_type = import_batch", data.get("resource_type") == "import_batch",
              str(data.get("resource_type")))
        check("resource_id = job_id（批次 ID）", data.get("resource_id") == data.get("job_id"))
        job_id = data["job_id"]

        # 轮询到终态：这里刻意真实等待，用来验证「受理 → 执行 → 终态」确实闭环，
        # 而不是只看 202。未启动 worker 时任务会停在 queued，脚本如实报告这种情况。
        job = None
        for _ in range(60):
            status, body = a.call(f"/jobs/{job_id}")
            if status != 200:
                break
            job = body["data"]
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(1)

        check("任务可查询 → 200", status == 200, f"got {status}")
        if job is None:
            check("任务可查询 → 200", False, "拿不到 Job")
        else:
            check("kind = import_arxiv", job.get("kind") == "import_arxiv", str(job.get("kind")))
            print(f"      Job 状态：{job['status']}  阶段：{job['stage']}")
            if job["status"] == "queued":
                print("      注意：任务仍在 queued。启动 worker 后重跑本步可看到终态：")
                print("        python -m celery -A config worker --pool=solo --loglevel=INFO")
                check("未执行时 result 为 null（不伪装成成功）", job.get("result") is None,
                      json.dumps(job.get("result"))[:120])
            else:
                items = (job.get("result") or {}).get("items") or []
                check("终态带回逐项账本", len(items) == 2, json.dumps(job.get("result"))[:150])
                check(
                    "账本结构完整",
                    all(
                        {"requested_id", "resolved_source_id", "status", "paper_id", "error"}
                        <= set(item)
                        for item in items
                    ),
                    json.dumps(items)[:150],
                )
                check(
                    "账本状态取值合法",
                    all(
                        item["status"] in {"pending", "imported", "duplicate", "failed"}
                        for item in items
                    ),
                    json.dumps([item.get("status") for item in items]),
                )
                check("requested_id 与请求一致",
                      [item["requested_id"] for item in items] == ["2401.00001", "2401.00002"],
                      json.dumps([item.get("requested_id") for item in items]))
                if job["status"] == "failed":
                    # 本机没有 arXiv 出网凭据/联系信息时，失败必须是可解释的失败
                    check("失败原因可解释", (job.get("error") or {}).get("code") in {
                        "ARXIV_FETCH_FAILED", "PAPER_LIMIT_EXCEEDED", "INTERNAL_ERROR",
                    }, json.dumps(job.get("error"))[:150])
                    check("失败逐项可见，不静默跳过",
                          any(item["status"] == "failed" for item in items),
                          json.dumps([item.get("status") for item in items]))
    else:
        print(f"      注意：导入受理返回 {status} {error_code(body)}（未断言 202 形状）")

    print()
    print("=" * 68)
    if FAILURES:
        print(f"走查失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("M05 文献接入与文件授权走查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
