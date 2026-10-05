"""M08 端到端走查：创建 Run → 推进到 ready → 发起检索 → 轮询 → 读结果。

这是**手工运行的脚本**，不是 pytest 用例（文件名不以 test_ 开头）。
它对真实运行的服务发真请求，验证 `docs/mvp/05` 第 6 节要求的 HTTP 阶段走查。

前置：M07 的流水线在 `index` / `topics` / `disputes` / `finalize` 未实现时会把 Run
显式判成 `failed`（这是刻意的），而检索要求 `Run.ready`。因此本脚本在第 3 步用
Django shell 把状态位推进到 `ready`——**解析与抽取产物本身仍是真实跑出来的**，
只有状态位与阶段标签是手工的：

    python manage.py shell -c "from apps.analysis.models import AnalysisRun as R; \
R.objects.filter(pk='<RUN_ID>').update(status='ready', error=None)"

准备两个演示用户与三个进程（服务 / worker / 可选 beat）。然后（回到仓库根）：

    python backend/tests/walkthrough_retrieval_http.py

可用环境变量覆盖：DEMO_USER / DEMO_PASSWORD / DEMO_OTHER_USER / DEMO_BASE。

关于结果条数：`LLM_MODE=mock` 时重排夹具用的是占位 UUID，不在候选白名单内，
因此**检索会诚实失败**——这本身就是契约边界的证据。本脚本接受两种终态，并分别断言：

- `succeeded`：结果形状必须合法（rank 从 1 连续、score ∈ [0,1]、无重复证据）；
- `failed`（`MODEL_OUTPUT_INVALID` 或 `INVALID_EVIDENCE`）：越界引用被拒，
  且 `results` 必须仍为 null。

拦截点要说清楚：真实 HTTP 路径下先由 M04 网关的 `validate_payload` 白名单拒绝
（`MODEL_OUTPUT_INVALID`）；服务层的候选集合校验（`INVALID_EVIDENCE`）由
`tests/test_m08_retrieval.py` 用替身绕过网关后直连覆盖。真实模型下的成功路径同样由该
测试文件覆盖。
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

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000/api/v1")

USER = os.environ.get("DEMO_USER", "alice")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-pass-123")
OTHER_USER = os.environ.get("DEMO_OTHER_USER", "bob")

FAILURES: list[str] = []

# `--run <run_id>`：复用上一次创建的 Run（已由人工推进到 ready），跳过第 1—2 步
ADVANCE = "--run" in sys.argv
REUSE_RUN_ID = sys.argv[sys.argv.index("--run") + 1] if ADVANCE else None

PAGES = [
    "Abstract\nSelf-attention improves sequence modelling on small corpora.",
    "Introduction\nRecurrent baselines are compared with attention models.",
    "3. Method\nWe replace the recurrent layer with a multi-head attention block.",
    "4. Results\nAttention improves accuracy by two points on the benchmark.",
]


def make_text_pdf(pages: list[str]) -> bytes:
    """生成含真实文本层的 PDF（空白页版无法驱动解析）。"""
    import io

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


def poll(session: Session, job_id: str, attempts: int = 90) -> dict:
    job: dict = {}
    for _ in range(attempts):
        _, body = session.call(f"/jobs/{job_id}")
        job = (body or {}).get("data") or {}
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(1)
    return job


def main() -> int:
    if not wait_for_server():
        print("服务未启动。请先在 backend/ 下运行：")
        print("    DJANGO_DEBUG=false python manage.py runserver 127.0.0.1:8000 --noreload")
        return 1

    print("=" * 68)
    print(f"1. 登录并准备语料与 Run（用户 {USER}）")
    print("=" * 68)
    a = Session()
    if not login(a, USER):
        print("登录失败。请先建用户（见 walkthrough_http.py 的说明）。")
        return 1
    check("登录成功", bool(a.csrf_token))

    if ADVANCE:
        run_id = REUSE_RUN_ID
        _, run_body = a.call(f"/runs/{run_id}")
        run_data = (run_body or {}).get("data") or {}
        if not run_data:
            print(f"找不到 Run {run_id}")
            return 1
        project_id = run_data["project_id"]
        print(f"      复用 Run {run_id}（状态 {run_data.get('status')}）")
        check("复用的 Run 已是 ready", run_data.get("status") == "ready", str(run_data.get("status")))

        # 第 6 步会故意编辑语料把 Run 弄成 stale，因此**同一个 Run 不能跑第二遍**。
        # 这里提前判定并给出可执行的下一步，避免后面十几项断言连坐失败、
        # 让人误以为是检索本身坏了。
        if run_data.get("is_stale") is True:
            print()
            print("该 Run 已 stale（很可能是上一次走查的第 6 步造成的），不能再次用于走查。")
            print("请重新跑不带 --run 的第一段，生成一个新的 Run：")
            print("  python backend/tests/walkthrough_retrieval_http.py")
            return 1
        check("复用的 Run 非 stale", run_data.get("is_stale") is False, str(run_data.get("is_stale")))
        job = {"status": "succeeded"}
        print()
        print("=" * 68)
        print("3. 受理检索（参数校验、幂等、忙状态）")
        print("=" * 68)
    else:
        status, body = a.call(
            "/projects",
            "POST",
            {
                "title": f"M08 走查 {uuid.uuid4().hex[:6]}",
                "research_question": "注意力机制在小样本条件下是否仍然有效？",
            },
        )
        check("创建项目 → 201", status == 201, f"got {status} {error_code(body)}")
        project_id = body["data"]["id"]

        for name in ("alpha", "beta"):
            status, body = a.upload(
                f"/projects/{project_id}/papers/upload",
                f"{name}.pdf",
                make_text_pdf([f"{name} {page}" for page in PAGES]),
                key=str(uuid.uuid4()),
            )
            check(f"上传 {name}.pdf → 201", status == 201, f"got {status} {error_code(body)}")

        status, body = a.call(
            f"/projects/{project_id}/runs",
            "POST",
            {},
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        check("创建 Run → 202", status == 202, f"got {status} {error_code(body)}")
        run_id = (body or {}).get("data", {}).get("resource_id")
        job = poll(a, (body or {}).get("data", {}).get("job_id", ""))
        print(f"      Run 的 Job 终态：{job.get('status')}")

        print()
        print("=" * 68)
        print("2. 前置：Run 必须 ready 且非 stale")
        print("=" * 68)
        status, body = a.call(
            f"/runs/{run_id}/retrievals",
            "POST",
            {"query": "attention method"},
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        print(f"      Run 状态：{job.get('status')}（M07 因后续阶段未实现而 failed）")
        check("未 ready 时拒绝检索 → 409", status == 409, f"got {status}")
        check("错误码 RUN_NOT_READY", error_code(body) == "RUN_NOT_READY", error_code(body))

        print()
        print("-" * 68)
        print("请另开终端执行下面这条命令，把 Run 推进到 ready，然后重新运行本脚本：")
        print(
            "  cd backend && ..\\.venv\\Scripts\\python.exe manage.py shell -c "
            f"\"from apps.analysis.models import AnalysisRun as R; "
            f"R.objects.filter(pk='{run_id}').update(status='ready', error=None)\""
        )
        print()
        print(f"  然后：python backend/tests/walkthrough_retrieval_http.py --run {run_id}")
        print("-" * 68)
        print("本次到此为止：Run 的解析与抽取产物已真实生成，只差状态位。")
        print(f"RUN_ID={run_id}")
        return 0
    status, body = a.call(
        f"/runs/{run_id}/retrievals",
        "POST",
        {"query": "x"},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("query 少于 2 字符 → 400", status == 400, f"got {status}")

    status, body = a.call(
        f"/runs/{run_id}/retrievals",
        "POST",
        {"query": "attention method", "candidate_limit": 5, "top_k": 6},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("top_k 超过 candidate_limit → 400", status == 400, f"got {status}")

    key = str(uuid.uuid4())
    status, body = a.call(
        f"/runs/{run_id}/retrievals",
        "POST",
        {"query": "attention method 是否有效"},
        extra_headers={"Idempotency-Key": key},
    )
    check("受理检索 → 202", status == 202, f"got {status} {error_code(body)}")
    accepted = (body or {}).get("data") or {}
    retrieval_id = accepted.get("resource_id")
    check("resource_type = retrieval_result", accepted.get("resource_type") == "retrieval_result")

    status, body = a.call(
        f"/runs/{run_id}/retrievals",
        "POST",
        {"query": "attention method 是否有效"},
        extra_headers={"Idempotency-Key": key},
    )
    replay = (body or {}).get("data") or {}
    check(
        "同键重放 → 同一检索记录",
        replay.get("resource_id") == retrieval_id,
        json.dumps(replay)[:120],
    )
    check("同键重放 → 同一 Job", replay.get("job_id") == accepted.get("job_id"))

    status, body = a.call(
        f"/runs/{run_id}/retrievals",
        "POST",
        {"query": "另一个问题"},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("项目忙时再次检索 → 409", status == 409, f"got {status}")
    check("错误码 PROJECT_BUSY", error_code(body) == "PROJECT_BUSY", error_code(body))

    status, body = a.call(f"/retrievals/{retrieval_id}")
    check("完成前可读 → 200", status == 200, f"got {status}")
    pending = (body or {}).get("data") or {}
    check("完成前 results 为 null", pending.get("results") is None, str(pending.get("results"))[:80])
    check("完成前 expanded_queries 为 null", pending.get("expanded_queries") is None)

    print()
    print("=" * 68)
    print("4. 轮询到终态并核对结果")
    print("=" * 68)
    job = poll(a, accepted.get("job_id", ""))
    print(f"      Job 状态：{job.get('status')}  阶段：{job.get('stage')}")
    check("Job 到达终态", job.get("status") in {"succeeded", "failed"}, str(job)[:140])

    _, body = a.call(f"/retrievals/{retrieval_id}")
    retrieval = (body or {}).get("data") or {}
    check("检索记录可读", retrieval.get("id") == retrieval_id)
    check("记录 query 与受理一致", retrieval.get("query") == "attention method 是否有效")
    check("记录 job_id", retrieval.get("job_id") == accepted.get("job_id"))

    if job.get("status") == "succeeded":
        results = retrieval.get("results")
        check("成功时 results 是数组（可为空）", isinstance(results, list), str(results)[:80])
        check(
            "已记录扩展查询",
            bool(retrieval.get("expanded_queries")),
            str(retrieval.get("expanded_queries"))[:100],
        )
        if results:
            check(
                "rank 从 1 连续",
                [item["rank"] for item in results] == list(range(1, len(results) + 1)),
                str([item.get("rank") for item in results]),
            )
            check(
                "score 在 0—1 之间",
                all(0 <= item["score"] <= 1 for item in results),
                str([item.get("score") for item in results]),
            )
            check(
                "每条结果都有 evidence_id 与 reason",
                all(item.get("evidence_id") and item.get("reason") for item in results),
            )
            check(
                "证据不重复",
                len({item["evidence_id"] for item in results}) == len(results),
            )
            check("结果条数不超过 top_k", len(results) <= (retrieval.get("top_k") or 5))

            evidence_id = results[0]["evidence_id"]
            status, body = a.call(f"/runs/{run_id}/evidence/{evidence_id}")
            check("结果证据可回看 → 200", status == 200, f"got {status} {error_code(body)}")
            evidence = (body or {}).get("data") or {}
            check("证据属于本 Run", evidence.get("run_id") == run_id)
            check("locator 页级且无 bbox", (evidence.get("locator") or {}).get("bbox") is None)
        else:
            print("      结果为空：召回或重排未返回可用证据（成功但无证据是合法结果）")
    else:
        code = (job.get("error") or {}).get("code")
        print(f"      失败原因：{code}  {(job.get('error') or {}).get('message')}")
        check(
            "失败原因可解释（越界引用或输出不合法）",
            code in {"INVALID_EVIDENCE", "MODEL_OUTPUT_INVALID"},
            str(code),
        )
        check("失败时不发布结果", retrieval.get("results") is None)
        print("      这是契约边界的证据：mock 夹具的占位 UUID 不在候选白名单内。")
        print("      注意拦截点——M04 网关的 validate_payload 在服务层之前就拒绝了，")
        print("      因此这里看到 MODEL_OUTPUT_INVALID；即使网关放行，")
        print("      retrieve_evidence 的候选集合校验仍会以 INVALID_EVIDENCE 挡下")
        print("      （由 tests/test_m08_retrieval.py 用替身直连覆盖）。")

    print()
    print("=" * 68)
    print("5. 历史查询与跨用户拒绝")
    print("=" * 68)
    status, body = a.call(f"/runs/{run_id}/retrievals")
    check("历史检索列表 → 200", status == 200, f"got {status} {error_code(body)}")
    items = (body or {}).get("data") or []
    check("历史里包含本次检索", any(item["id"] == retrieval_id for item in items), str(len(items)))

    b = Session()
    if not login(b, OTHER_USER):
        print(f"      跳过跨用户检查：用户 {OTHER_USER} 不存在。")
    else:
        check("他人读检索记录 → 404", b.call(f"/retrievals/{retrieval_id}")[0] == 404)
        check("他人读历史列表 → 404", b.call(f"/runs/{run_id}/retrievals")[0] == 404)
        check(
            "他人在他人 Run 上发起检索 → 404",
            b.call(
                f"/runs/{run_id}/retrievals",
                "POST",
                {"query": "attention method"},
                extra_headers={"Idempotency-Key": str(uuid.uuid4())},
            )[0]
            == 404,
        )

    print()
    print("=" * 68)
    print("6. 编辑语料后旧 Run 变 stale，新检索被拒")
    print("=" * 68)
    _, project_body = a.call(f"/projects/{project_id}")
    version = ((project_body or {}).get("data") or {}).get("version")
    status, body = a.call(
        f"/projects/{project_id}",
        "PATCH",
        {"expected_version": version, "research_question": "换一个研究问题以制造 stale 语料。"},
    )
    check("编辑研究问题 → 200", status == 200, f"got {status} {error_code(body)}")

    _, run_body = a.call(f"/runs/{run_id}")
    check("旧 Run 标记 stale", ((run_body or {}).get("data") or {}).get("is_stale") is True)
    check("旧 Run 仍可读单条检索", a.call(f"/retrievals/{retrieval_id}")[0] == 200)

    status, body = a.call(
        f"/runs/{run_id}/retrievals",
        "POST",
        {"query": "stale 之后的新检索"},
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check("stale 后新检索 → 409", status == 409, f"got {status}")
    check("错误码 STALE_RUN", error_code(body) == "STALE_RUN", error_code(body))

    print()
    print("=" * 68)
    if FAILURES:
        print(f"走查失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("M08 检索与结果持久化走查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
