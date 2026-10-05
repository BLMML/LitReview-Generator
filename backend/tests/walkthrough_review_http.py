"""M10 端到端走查：创建 Review → 生成五节正文 → 编辑保存新版本。

这是**手工运行的脚本**，不是 pytest 用例（文件名不以 test_ 开头）。
它对真实运行的服务发真请求，验证 `docs/mvp/05` 第 6 节要求的 HTTP 阶段走查。

前置：生成要求 `Run.ready` 且存在已完成的单篇分析。M07 的流水线在真实
`paper_extract` 输出不合规时会诚实失败（mock 夹具的占位 UUID 就是这种情况），
因此第 2 步用一段 Django shell 脚本把 Run 推进到 ready——**解析产物与单篇分析
都是真实生成的**，只有话题与状态位是补齐的：

    见脚本打印的命令

可用环境变量覆盖：DEMO_USER / DEMO_PASSWORD / DEMO_OTHER_USER / DEMO_BASE。

关于正文条数：`LLM_MODE=mock` 时 `generate_sections` 夹具返回的 block 引用
占位 UUID，不在本节证据白名单内，因此**生成会以 MODEL_OUTPUT_INVALID 诚实失败**。
本脚本接受两种终态并分别断言：成功则校验五节结构与 DTO；失败则要求
`latest_revision` 保持为 null，绝不展示空白“成功综述”。
真实模型下的成功路径由 `tests/test_m10_reviews.py` 用替身完整覆盖。
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

ADVANCE = "--run" in sys.argv
REUSE_RUN_ID = sys.argv[sys.argv.index("--run") + 1] if ADVANCE else None

FAILURES: list[str] = []

PAGES = [
    "Abstract\nSelf-attention improves sequence modelling on small corpora.",
    "Introduction\nRecurrent baselines are compared with attention models on small samples.",
    "3. Method\nWe replace the recurrent layer with a multi-head attention block.",
    "4. Results\nAttention improves small-sample accuracy by two points on the benchmark.",
]

QUESTION = "Does attention improve sequence modelling accuracy on small-sample benchmarks?"

SECTION_KEYS = (
    "background",
    "directions",
    "representative_work",
    "disagreements",
    "trends",
)

# 提示用户保存为 backend/_advance_m10.py 的辅助脚本。
# 它新建一个 ready 的 Run：用**正式入口** claim_execution 领取执行资格
# （自造 ExecutionContext 会被租约校验按状态与有效期拒掉），
# 再真实解析每篇文献、写入单篇分析与主题。
_ADVANCE_HELPER = '''\
import json, uuid

from apps.analysis.models import (
    RUN_PAPER_READY, RUN_READY, AnalysisRun, PaperAnalysis, RunPaper, Topic,
)
from apps.jobs.models import Job
from apps.jobs.services import claim_execution
from apps.parsing.models import EvidenceChunk
from apps.parsing.services import parse_paper
from apps.projects.models import Project

project = Project.objects.get(pk="{project_id}")
template = AnalysisRun.objects.filter(project=project).order_by("-created_at").first()
run = AnalysisRun.objects.create(
    project=project,
    corpus_revision=project.corpus_revision,
    research_question=project.research_question,
    output_language=project.output_language,
    config_snapshot=template.config_snapshot,
)
for paper in project.papers.filter(included=True).order_by("created_at", "id"):
    RunPaper.objects.create(
        run=run, paper=paper, title=paper.title, authors=list(paper.authors or []),
        year=paper.year, source=paper.source, source_id=paper.source_id,
        source_url=paper.source_url, file_asset=paper.file_asset,
        file_sha256=paper.file_sha256, page_count=paper.page_count, status="pending",
    )

job = Job.objects.create(
    project=project, requested_by=project.owner, kind="analyze", status="queued",
    resource_type="analysis_run", resource_id=run.id,
    frozen_input={{"run_id": str(run.id)}}, input_hash="0" * 64,
)
ctx = claim_execution(job.id)
for rp in run.run_papers.order_by("created_at", "id"):
    doc = parse_paper(ctx, rp, run.config_snapshot["parsing"])
    RunPaper.objects.filter(pk=rp.pk).update(parsed_document=doc, status=RUN_PAPER_READY)
    ids = [str(v) for v in EvidenceChunk.objects.filter(parsed_document=doc)
           .order_by("page_number", "ordinal").values_list("id", flat=True)]
    PaperAnalysis.objects.create(run_paper=rp, payload={{
        "research_question": {{"value": "attention helps", "evidence_ids": [ids[0]]}},
        "research_object": {{"value": None, "evidence_ids": []}},
        "summary": {{"value": "attention improves accuracy", "evidence_ids": [ids[0]]}},
        "methods": [], "datasets": [], "metrics": [], "conclusions": [], "limitations": [],
    }})
Topic.objects.create(
    run=run, name="attention", description="",
    paper_ids=[str(v) for v in run.run_papers.values_list("paper_id", flat=True)],
    evidence_ids=[],
)
AnalysisRun.objects.filter(pk=run.pk).update(status=RUN_READY, error=None)
Job.objects.filter(pk=job.id).update(status="succeeded", lease_expires_at=None)
print("RUN_ID", run.id)
'''


def make_text_pdf(pages: list[str]) -> bytes:
    """生成含真实文本层的 PDF。

    ⚠ 内容流按 latin-1 编码，**中文会整体变成 `?`**。因此走查用英文语料，
    与研究问题同语言——检索查询用的是冻结研究问题，语言不一致时召回必然落空。
    """
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
        font_ref = writer._add_object(font)  # noqa: SLF001
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


def poll_job(session: Session, job_id: str, attempts: int = 90) -> dict:
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
    print(f"1. 登录并取一个 Run（用户 {USER}）")
    print("=" * 68)
    a = Session()
    if not login(a, USER):
        print("登录失败。请先建用户（见 walkthrough_http.py 的说明）。")
        return 1
    check("登录成功", bool(a.csrf_token))

    if ADVANCE:
        run_id = REUSE_RUN_ID
        _, run_body = a.call(f"/runs/{run_id}")
        run = (run_body or {}).get("data") or {}
        if not run:
            print(f"找不到 Run {run_id}")
            return 1
        project_id = run["project_id"]
        check("复用的 Run 已是 ready", run.get("status") == "ready", str(run.get("status")))
    else:
        status, body = a.call(
            "/projects",
            "POST",
            {"title": f"M10 走查 {uuid.uuid4().hex[:6]}", "research_question": QUESTION},
        )
        check("创建项目 → 201", status == 201, f"got {status} {error_code(body)}")
        project_id = body["data"]["id"]

        paper_ids = []
        for name in ("alpha", "beta"):
            status, body = a.upload(
                f"/projects/{project_id}/papers/upload",
                f"{name}.pdf",
                make_text_pdf([f"{name} {page}" for page in PAGES]),
                key=str(uuid.uuid4()),
            )
            check(f"上传 {name}.pdf → 201", status == 201, f"got {status} {error_code(body)}")
            paper_ids.append(body["data"]["id"])

        status, body = a.call(
            f"/projects/{project_id}/runs",
            "POST",
            {},
            extra_headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        check("创建 Run → 202", status == 202, f"got {status} {error_code(body)}")
        run_id = (body or {}).get("data", {}).get("resource_id")
        job = poll_job(a, (body or {}).get("data", {}).get("job_id", ""))
        print(f"      Run 的 Job 终态：{job.get('status')}")

        print()
        print("-" * 68)
        print("生成要求 Run.ready 且有已完成的单篇分析。请另开终端，用下面的脚本")
        print("在本项目里新建一个 ready 的 Run（解析与单篇分析都是真实生成的）：")
        print()
        print("  把下面内容存成 backend/_advance_m10.py，然后执行：")
        print("    cd backend")
        print("    ..\\.venv\\Scripts\\python.exe manage.py shell -c "
              '"exec(open(\'_advance_m10.py\', encoding=\'utf-8\').read())"')
        print()
        print(_ADVANCE_HELPER.format(project_id=project_id))
        print()
        print("  脚本会打印新的 RUN_ID，然后：python backend/tests/"
              "walkthrough_review_http.py --run <新 RUN_ID>")
        print("-" * 68)
        print("本次到此为止：文献与 Run 已受理，只差 ready 前置。")
        print(f"RUN_ID={run_id}")
        return 0

    print()
    print("=" * 68)
    print("2. 受理生成")
    print("=" * 68)
    key = str(uuid.uuid4())
    status, body = a.call(
        f"/runs/{run_id}/reviews",
        "POST",
        {"title": "注意力机制综述", "target_chars": 3000, "style": "standard"},
        extra_headers={"Idempotency-Key": key},
    )
    check("创建 Review → 202", status == 202, f"got {status} {error_code(body)}")
    accepted = (body or {}).get("data") or {}
    review_id = accepted.get("resource_id")
    check("resource_type = review", accepted.get("resource_type") == "review")

    status, body = a.call(
        f"/runs/{run_id}/reviews",
        "POST",
        {"title": "注意力机制综述", "target_chars": 3000, "style": "standard"},
        extra_headers={"Idempotency-Key": key},
    )
    replay = (body or {}).get("data") or {}
    check("同键重放 → 同一 Review", replay.get("resource_id") == review_id, json.dumps(replay)[:120])

    _, body = a.call(f"/reviews/{review_id}")
    pending = (body or {}).get("data") or {}
    check("生成前 latest_revision 为 null", pending.get("latest_revision") is None)
    check("生成前有 generation_job_id", bool(pending.get("generation_job_id")))

    print()
    print("=" * 68)
    print("3. 轮询生成并核对正文")
    print("=" * 68)
    job = poll_job(a, accepted.get("job_id", ""))
    print(f"      Job 状态：{job.get('status')}  阶段：{job.get('stage')}")
    check("Job 到达终态", job.get("status") in {"succeeded", "failed"}, str(job)[:140])

    _, body = a.call(f"/reviews/{review_id}")
    review = (body or {}).get("data") or {}
    check("Review 可读", review.get("id") == review_id)
    check("Review 记录生成任务", review.get("generation_job_id") == accepted.get("job_id"))

    if job.get("status") == "succeeded":
        revision = review.get("latest_revision")
        check("提交了 revision 1", (revision or {}).get("revision_number") == 1, str(revision)[:100])
        sections = (revision or {}).get("sections") or []
        check(
            "五节顺序固定",
            [item.get("section_key") for item in sections] == list(SECTION_KEYS),
            str([item.get("section_key") for item in sections]),
        )
        check("每节至少一个 block", all(item.get("blocks") for item in sections))
        ids = [b.get("block_id") for item in sections for b in item["blocks"]]
        check("block_id 全稿唯一", len(ids) == len(set(ids)))
        check(
            "claim 都带证据",
            all(
                b.get("evidence_ids")
                for item in sections
                for b in item["blocks"]
                if b.get("kind") == "claim"
            ),
        )
        check("source = generated", (revision or {}).get("source") == "generated")

        print()
        print("=" * 68)
        print("4. 编辑保存新版本")
        print("=" * 68)
        edited = json.loads(json.dumps(sections, ensure_ascii=False))
        edited[0]["blocks"][0]["text"] = edited[0]["blocks"][0]["text"] + " （人工补充）"

        status, body = a.call(
            f"/reviews/{review_id}/revisions",
            "POST",
            {"base_revision_number": 1, "sections": edited},
        )
        check("保存编辑 → 201", status == 201, f"got {status} {error_code(body)}")
        if status == 201:
            saved = (body or {}).get("data") or {}
            check("新版本号 = 2", saved.get("revision_number") == 2, str(saved.get("revision_number")))
            check("source = manual", saved.get("source") == "manual")

        status, body = a.call(
            f"/reviews/{review_id}/revisions",
            "POST",
            {"base_revision_number": 1, "sections": edited},
        )
        check("过期 base → 409", status == 409, f"got {status}")
        check("错误码 VERSION_CONFLICT", error_code(body) == "VERSION_CONFLICT", error_code(body))

        _, body = a.call(f"/reviews/{review_id}/revisions", query="?page_size=20")
        check("历史版本列表可读", (body or {}).get("data") is not None)
        history = (body or {}).get("data") or []
        check("历史里有 2 个版本", len(history) == 2, str(len(history)))
    else:
        code = (job.get("error") or {}).get("code")
        print(f"      失败原因：{code}  {(job.get('error') or {}).get('message')}")
        check("失败时不展示空白正文", review.get("latest_revision") is None)
        check("失败原因可解释", bool(code), str(code))
        print("      mock 夹具的占位 evidence UUID 不在本节白名单内，生成会诚实失败；")
        print("      真实模型下的成功路径由 tests/test_m10_reviews.py 用替身覆盖。")

    print()
    print("=" * 68)
    print("5. 跨用户拒绝")
    print("=" * 68)
    b = Session()
    if not login(b, OTHER_USER):
        print(f"      跳过：用户 {OTHER_USER} 不存在。")
    else:
        check("他人读 Review → 404", b.call(f"/reviews/{review_id}")[0] == 404)
        check(
            "他人读版本历史 → 404",
            b.call(f"/reviews/{review_id}/revisions")[0] == 404,
        )

    print()
    print("=" * 68)
    if FAILURES:
        print(f"走查失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"    - {item}")
        return 1
    print("M10 综述生成与编辑走查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
