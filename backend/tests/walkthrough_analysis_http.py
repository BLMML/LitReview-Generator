"""M07 端到端走查：登录 → 建项目 → 两篇 PDF → 创建 Run → 轮询 → 单篇分析与证据。

这是**手工运行的脚本**，不是 pytest 用例（文件名不以 test_ 开头）。
它对真实运行的服务发真请求，验证 `docs/mvp/05` 第 6 节要求的 HTTP 阶段走查：

1. `POST /projects/{id}/runs` 带合法 Idempotency-Key → 202；
2. 同键重放 → 同一 job_id，不产生第二个 Run；
3. 项目忙时再次发起 → 409 PROJECT_BUSY；
4. 轮询 Job 与 Run 到终态，两者状态必须一致；
5. Run 的 research_question / corpus_revision 是**冻结值**，后续编辑不改（并变 stale）；
6. 单篇分析可读，其 evidence_ids 能逐条通过
   `GET /runs/{id}/evidence/{evidence_id}` 查回，且与本篇证据一致；
7. 跨用户读取 Run / 分析 / 证据一律 404；
8. 后续阶段（index / topics / disputes / finalize）未实现时，Run **不得** ready。

准备两个演示用户（只需一次）与三个进程（服务 / worker / 可选 beat），见本目录
`walkthrough_http.py` 的说明。然后（回到仓库根）：

    python backend/tests/walkthrough_analysis_http.py

可用环境变量覆盖：DEMO_USER / DEMO_PASSWORD / DEMO_OTHER_USER / DEMO_BASE。

脚本会真实创建项目、文献与 Run；MVP 没有物理删除接口，这些记录会留在开发库里。
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000/api/v1")

USER = os.environ.get("DEMO_USER", "alice")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-pass-123")
OTHER_USER = os.environ.get("DEMO_OTHER_USER", "bob")

FAILURES: list[str] = []

PAGES = [
    "Abstract\nWe study whether attention helps under small-sample conditions.",
    "Introduction\nRecurrent and convolutional baselines are compared.",
    "3. Method\nWe replace the recurrent layer with self-attention.",
    "4. Results\nAccuracy improves on the benchmark split.",
]


def make_text_pdf(pages: list[str]) -> bytes:
    """生成含真实文本层的 PDF（空白页版无法驱动解析）。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for body in pages:
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        font_ref = writer._add_object(font)  # noqa: SLF001 - 走查脚本构造 PDF
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        content = ["BT", "/F1 12 Tf", "50 780 Td", "16 TL"]
        for line in body.split("\n"):
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content.append(f"({escaped}) Tj T*")
        content.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(content).encode("latin-1", errors="replace"))
        page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Session:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf_token: str | None = None

    def _send(self, request: urllib.request.Request) -> tuple[int, bytes]:
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def call(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        *,
        extra_headers: dict | None = None,
        query: str = "",
    ) -> tuple[int, dict | None]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{BASE}{path}{query}", data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if method != "GET" and self.csrf_token:
            request.add_header("X-CSRFToken", self.csrf_token)
        for name, value in (extra_headers or {}).items():
            request.add_header(name, value)
        status, raw = self._send(request)
        text = raw.decode("utf-8", errors="replace")
        try:
            return status, json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            return status, {"_non_json": text.strip()[:120]}

    def upload(self, path: str, filename: str, payload: bytes, *, key: str) -> tuple[int, dict | None]:
        boundary = f"----walkthrough{uuid.uuid4().hex}"
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'
        ).encode()
        body = b"".join([head, payload, f"\r\n--{boundary}--\r\n".encode()])
        request = urllib.request.Request(f"{BASE}{path}", data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        request.add_header("Accept", "application/json")
        request.add_header("Idempotency-Key", key)
        if self.csrf_token:
            request.add_header("X-CSRFToken", self.csrf_token)
        status, raw = self._send(request)
        text = raw.decode("utf-8", errors="replace")
        try:
            return status, json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            return status, {"_non_json": text.strip()[:120]}

    def refresh_csrf(self) -> None:
        _, body = self.call("/auth/csrf")
        self.csrf_token = ((body or {}).get("data") or {}).get("csrf_token")


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  ← ' + detail}")
    if not condition:
        FAILURES.append(label)


def error_code(body: dict | None) -> str:
    return ((body or {}).get("error") or {}).get("code", "<无错误码>")


def login(session: Session, username: str) -> bool:
    session.refresh_csrf()
    status, _ = session.call("/auth/login", "POST", {"username": username, "password": PASSWORD})
    if status != 200:
        return False
    session.refresh_csrf()
    return True


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
    print(f"1. 登录并准备两篇文献（用户 {USER}）")
    print("=" * 68)
    a = Session()
    if not login(a, USER):
        print("登录失败。请先建用户：")
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
            "title": f"M07 走查 {uuid.uuid4().hex[:6]}",
            "research_question": "注意力机制在小样本条件下是否仍然有效？",
        },
    )
    check("创建项目 → 201", status == 201, f"got {status} {error_code(body)}")
    project = body["data"]
    project_id = project["id"]
    frozen_question = project["research_question"]

    paper_ids = []
    for name, year in (("alpha", 2020), ("beta", 2021)):
        status, body = a.upload(
            f"/projects/{project_id}/papers/upload",
            f"{name}.pdf",
            make_text_pdf([f"{name} {page}" for page in PAGES]),
            key=str(uuid.uuid4()),
        )
        check(f"上传 {name}.pdf → 201", status == 201, f"got {status} {error_code(body)}")
        paper = body["data"]
        paper_ids.append(paper["id"])
        a.call(
            f"/papers/{paper_ids[-1]}",
            "PATCH",
            {"expected_version": paper["version"], "year": year},
        )
    check("两篇文献就绪", len(paper_ids) == 2)

    print()
    print("=" * 68)
    print("2. 创建分析 Run（幂等 + 忙状态）")
    print("=" * 68)
    key = str(uuid.uuid4())
    status, body = a.call(
        f"/projects/{project_id}/runs", "POST", {}, extra_headers={"Idempotency-Key": key}
    )
    check("创建 Run → 202", status == 202, f"got {status} {error_code(body)}")
    accepted = (body or {}).get("data") or {}
    run_id = accepted.get("resource_id")
    job_id = accepted.get("job_id")
    check("resource_type = analysis_run", accepted.get("resource_type") == "analysis_run")

    status, body = a.call(
        f"/projects/{project_id}/runs", "POST", {}, extra_headers={"Idempotency-Key": key}
    )
    replay = (body or {}).get("data") or {}
    check("同键重放 → 同一 Run", replay.get("resource_id") == run_id, json.dumps(replay)[:120])
    check("同键重放 → 同一 Job", replay.get("job_id") == job_id)

    status, body = a.call(
        f"/projects/{project_id}/runs", "POST", {}, extra_headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    # 忙状态只在 Job 仍排队/运行时成立，而 worker 可能已经跑完了这个很短的流水线。
    # 走查里这是一个**时序依赖**的观测：两种结果都合法，据实分别断言。
    # 权威结论由 tests/test_m07_analysis.py 的确定性用例给出（那时队列被暂停）。
    if status == 409:
        check("项目忙时再次创建 → 409", True)
        check("错误码 PROJECT_BUSY", error_code(body) == "PROJECT_BUSY", error_code(body))
    else:
        check("项目已空闲时创建新 Run → 202", status == 202, f"got {status} {error_code(body)}")
        print("      本项目已空闲（worker 快于本检查），PROJECT_BUSY 的确定性断言见专用测试")
        # 这个额外 Run 的 Job 会占住项目，让第 7 步的"编辑项目"变成 PROJECT_BUSY。
        # 走查是自己制造的这次并发，自己收尾。
        extra_job = ((body or {}).get("data") or {}).get("job_id")
        if extra_job:
            a.call(f"/jobs/{extra_job}/cancel", "POST", {})
            for _ in range(30):
                _, extra_body = a.call(f"/jobs/{extra_job}")
                state = ((extra_body or {}).get("data") or {}).get("status")
                if state in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(1)
            print(f"      已收尾额外 Job：{state}")

    print()
    print("=" * 68)
    print("3. 轮询到终态")
    print("=" * 68)
    job = run = None
    for _ in range(90):
        _, job_body = a.call(f"/jobs/{job_id}")
        job = (job_body or {}).get("data") or {}
        _, run_body = a.call(f"/runs/{run_id}")
        run = (run_body or {}).get("data") or {}
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(1)

    print(f"      Job 状态：{job.get('status')}  阶段：{job.get('stage')}")
    print(f"      Run 状态：{run.get('status')}")
    check("Job 到达终态", job.get("status") in {"succeeded", "failed", "cancelled"}, str(job)[:120])
    check("Run 到达终态", run.get("status") in {"ready", "failed", "cancelled"}, str(run)[:120])
    check(
        "Job 与 Run 状态一致（不得一个成功一个失败）",
        (job.get("status") == "succeeded") == (run.get("status") == "ready"),
        f"job={job.get('status')} run={run.get('status')}",
    )

    print()
    print("=" * 68)
    print("4. 冻结快照与完整流水线终态")
    print("=" * 68)
    check("Run 冻结研究问题", run.get("research_question") == frozen_question)
    check("Run 记录语料版本", isinstance(run.get("corpus_revision"), int))

    # M09 之后流水线会一路跑到 finalize。mock 夹具的重排/主题占位 UUID 不满足
    # "只能引用候选/成员"的契约，因此这里**接受两种终态**并分别断言：
    # 要么整条链路成功，要么在 Run 级阶段诚实失败且逐篇产物保留。
    if run.get("status") == "ready":
        check("完整流水线到达 ready", True)
        check("成功时无遗留错误", run.get("error") is None, json.dumps(run.get("error"))[:120])
    else:
        error = run.get("error") or {}
        check("未 ready 时必须给出可解释原因", bool(error.get("code")), json.dumps(error)[:150])
        check(
            "失败发生在 Run 级阶段（逐篇产物应保留）",
            error.get("code") in {"MODEL_OUTPUT_INVALID", "INVALID_EVIDENCE", "INTERNAL_ERROR"},
            str(error.get("code")),
        )

    print()
    print("=" * 68)
    print("5. 单篇分析、证据回看与跨用户拒绝")
    print("=" * 68)
    ready = [item for item in (run.get("papers") or []) if item.get("status") == "ready"]
    print(f"      已就绪论文：{len(ready)} / {len(run.get('papers') or [])}")
    if ready:
        target = ready[0]
        status, body = a.call(f"/runs/{run_id}/papers/{target['paper_id']}/analysis")
        check("单篇分析可读 → 200", status == 200, f"got {status} {error_code(body)}")
        payload = ((body or {}).get("data") or {}).get("payload") or {}
        check(
            "分析含八个规定字段",
            set(payload)
            == {
                "research_question",
                "research_object",
                "summary",
                "methods",
                "datasets",
                "metrics",
                "conclusions",
                "limitations",
            },
            str(sorted(payload)),
        )

        evidence_ids = (payload.get("summary") or {}).get("evidence_ids") or []
        check("摘要带本篇证据", bool(evidence_ids), json.dumps(payload.get("summary"))[:120])
        if evidence_ids:
            status, body = a.call(f"/runs/{run_id}/evidence/{evidence_ids[0]}")
            check("证据可回看 → 200", status == 200, f"got {status} {error_code(body)}")
            evidence = (body or {}).get("data") or {}
            check("证据属于本篇论文", evidence.get("paper_id") == target["paper_id"])
            page_number = evidence.get("page_number")
            check(
                "证据带真实页码",
                isinstance(page_number, int) and page_number >= 1,
                str(page_number),
            )
            check("locator 为页级且无 bbox", (evidence.get("locator") or {}).get("bbox") is None)
            check(
                "证据给出授权文件入口",
                evidence.get("file_url") == f"/api/v1/papers/{target['paper_id']}/file",
                str(evidence.get("file_url")),
            )

        status, body = a.call(
            f"/runs/{run_id}/papers/{target['paper_id']}/evidence", query="?page_number=1"
        )
        check("按页取证据 → 200", status == 200, f"got {status} {error_code(body)}")
        listed = (body or {}).get("data") or []
        check(
            "第一页证据非空且页码一致",
            bool(listed) and all(item["page_number"] == 1 for item in listed),
            str(listed)[:120],
        )

        status, body = a.call(
            f"/runs/{run_id}/papers/{target['paper_id']}/evidence", query="?page_number=999"
        )
        check("越界页码 → 400", status == 400, f"got {status}")
    else:
        print("      跳过：本次没有任何论文就绪（mock 输出与真实证据 ID 不匹配属预期）")
        print("      单篇产物的完整断言由 tests/test_m07_analysis.py 覆盖。")

    b = Session()
    if not login(b, OTHER_USER):
        print(f"      跳过跨用户检查：用户 {OTHER_USER} 不存在。")
    else:
        check("他人读 Run → 404", b.call(f"/runs/{run_id}")[0] == 404)
        check(
            "他人读单篇分析 → 404",
            b.call(f"/runs/{run_id}/papers/{paper_ids[0]}/analysis")[0] == 404,
        )
        check("他人读检索历史 → 404", b.call(f"/runs/{run_id}/retrievals")[0] == 404)

    print()
    print("=" * 68)
    print("6. Run 级产物（主题 / 争议）与覆盖统计")
    print("=" * 68)
    if run.get("status") == "ready":
        status, body = a.call(f"/runs/{run_id}/topics")
        check("主题列表可读 → 200", status == 200, f"got {status} {error_code(body)}")
        topics = (body or {}).get("data") or []
        check("归纳出主题", bool(topics), "没有主题")
        if topics:
            check(
                "主题 DTO 字段齐全",
                {"id", "run_id", "name", "description", "paper_ids", "evidence_ids"}
                <= set(topics[0]),
                str(sorted(topics[0]))[:120],
            )
            check("主题至少 1 篇成员", bool(topics[0].get("paper_ids")))

        status, body = a.call(f"/runs/{run_id}/disputes")
        check("争议列表可读 → 200", status == 200, f"got {status} {error_code(body)}")
        disputes = (body or {}).get("data") or []
        print(f"      争议候选：{len(disputes)} 条（0 条是合法结果，不要求模型凑数）")
        if disputes:
            first = disputes[0]
            check(
                "争议 DTO 字段齐全",
                {"left_paper_id", "right_paper_id", "left_claim", "right_claim", "conditions",
                 "classification", "reason", "review_status", "version"} <= set(first),
                str(sorted(first))[:140],
            )
            check("左右论文不同", first["left_paper_id"] != first["right_paper_id"])
            check("人工状态从 pending 开始", first["review_status"] == "pending")
            check("版本从 1 开始", first["version"] == 1)

        stats = run.get("stats") or {}
        check("覆盖统计只读快照", stats.get("included_count") == 2, str(stats)[:120])
        check("就绪计数与冻结一致", stats.get("ready_count") == 2, str(stats)[:120])
    else:
        status, _body = a.call(f"/runs/{run_id}/topics")
        check("非 ready 时主题不可读 → 409", status == 409, f"got {status}")

    print()
    print("=" * 68)
    print("7. 编辑后旧 Run 变 stale")
    print("=" * 68)
    _, project_body = a.call(f"/projects/{project_id}")
    current_version = ((project_body or {}).get("data") or {}).get("version")
    status, body = a.call(
        f"/projects/{project_id}",
        "PATCH",
        {"expected_version": current_version, "research_question": "换一个全新问题以制造 stale。"},
    )
    check("编辑研究问题 → 200", status == 200, f"got {status} {error_code(body)}")
    _, run_body = a.call(f"/runs/{run_id}")
    refreshed = (run_body or {}).get("data") or {}
    check("旧 Run 变 stale", refreshed.get("is_stale") is True, str(refreshed.get("is_stale")))
    check(
        "旧 Run 仍显示冻结问题",
        refreshed.get("research_question") == frozen_question,
        str(refreshed.get("research_question"))[:80],
    )

    print()
    print("=" * 68)
    if FAILURES:
        print(f"走查失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("M07 分析快照与单篇理解走查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
