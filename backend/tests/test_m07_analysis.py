"""M07 分析快照与单篇理解测试（docs/mvp/01 第 4.4 节、docs/mvp/05 T09 / T10）。

覆盖九组行为（`M07_开发步骤_2026-09-30.md` 第 5 节）：

1. 少于两篇拒绝、两篇受理 202、同 key 重放、未知请求字段拒绝；
2. 项目忙状态有效；
3. 研究问题 / 年份 / 标题 / 文件哈希与完整配置冻结，后续编辑不改旧 Run；
4. 真 RunPaper 接入 M06 回填产物，单篇分析与证据通过 API 可读且来源一致；
5. PaperExtract 缺字段、未知字段、空 value、假证据、跨篇引用被拒绝；
6. 一篇成功一篇失败时 Run.failed，成功产物保留、失败原因可见；
7. 取消 / 租约失效不写入业务结果，重试保留成功阶段；
8. 年份统计只读 RunPaper 快照；
9. index / topics / disputes / finalize 未实现时不得宣布 Run.ready。

模型调用一律走替身（`call_structured`），不产生付费调用。
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest import make_text_pdf_file

from apps.analysis.models import (
    RUN_FAILED,
    RUN_PAPER_FAILED,
    RUN_PAPER_READY,
    AnalysisRun,
    PaperAnalysis,
    RunPaper,
)
from apps.analysis.schemas import SchemaValidationError, validate_paper_analysis
from apps.common.http import ApiError

pytestmark = pytest.mark.django_db

PAGE_BODIES = [
    "Abstract\nAttention mechanisms improve sequence models.",
    "Introduction\nWe compare attention with recurrent baselines.",
    "3. Method\nWe replace the recurrent layer with self-attention.",
    "4. Results\nAccuracy improves by two points on the benchmark.",
]


def _paper_bodies(seed: str) -> list[str]:
    return [f"{seed} {body}" for body in PAGE_BODIES]


def _upload_paper(client, project, *, name: str, year=None, title=None):
    """上传一篇含文本层的 PDF；可选补年份，便于验证年份统计只读快照。"""
    response = client.post(
        f"/api/v1/projects/{project.id}/papers/upload",
        {
            "file": make_text_pdf_file(_paper_bodies(name), name=f"{name}.pdf"),
            **({"title": title} if title else {}),
        },
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    if year is not None:
        patched = client.patch(
            f"/api/v1/papers/{data['id']}", {"expected_version": 1, "year": year}, format="json"
        )
        assert patched.status_code == 200, patched.json()
        data = patched.json()["data"]
    return data


def _create_run(client, project, *, key=None):
    return client.post(
        f"/api/v1/projects/{project.id}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


@pytest.fixture
def two_papers(client_a, project_a, monkeypatch):
    """两篇纳入文献；暂停队列投递，由测试自己驱动 worker。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    first = _upload_paper(client_a, project_a, name="alpha", year=2020)
    second = _upload_paper(client_a, project_a, name="beta", year=2021)
    return first, second


def _extract_payload(evidence_ids: list[str], *, value: str = "结论成立") -> dict:
    """构造一份结构与引用都合法的 PaperExtract 输出。"""
    single = {"value": value, "evidence_ids": [evidence_ids[0]]}
    listed = [{"value": "方法要点", "evidence_ids": [evidence_ids[-1]]}]
    return {
        "research_question": single,
        "research_object": {"value": None, "evidence_ids": []},
        "summary": single,
        "methods": listed,
        "datasets": [],
        "metrics": [],
        "conclusions": listed,
        "limitations": [],
    }


def _patch_extractor(monkeypatch, builder):
    """用替身替换模型网关：单篇抽取走 `builder`，Run 级阶段返回最小合法输出。

    M09 之后 `execute_run` 会继续调用主题归纳与争议比较，因此替身必须同时响应
    这两类任务，否则测不到 M07 想验证的"单篇 + Run 收尾"路径。
    `builder` 仍然只拿到**本篇**证据 ID，单篇的断言不受影响。
    """
    from apps.analysis import services as analysis_services
    from apps.llm.schemas import TASK_ASSIGN_TOPICS, TASK_COMPARE_DISPUTES, TASK_PAPER_EXTRACT

    class _Result:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = f"mock-{uuid.uuid4().hex[:8]}"

    def fake_call(context, request):
        if request.task_type == TASK_PAPER_EXTRACT:
            return _Result(builder(list(request.allowed_evidence_ids)))
        if request.task_type == TASK_ASSIGN_TOPICS:
            # 把全部论文放进一个主题：既验证多成员主题，也不留未归类论文
            return _Result(
                {
                    "topics": [
                        {
                            "name": "注意力机制",
                            "description": "围绕注意力模块的对比研究。",
                            "paper_ids": sorted(str(value) for value in request.allowed_paper_ids),
                            "evidence_ids": [],
                        }
                    ],
                    "unassigned_paper_ids": [],
                }
            )
        if request.task_type == TASK_COMPARE_DISPUTES:
            # 默认不构造争议：没有候选是合法结果，不要求模型凑数
            return _Result({"disputes": []})
        raise AssertionError(f"未预期的任务类型：{request.task_type}")

    monkeypatch.setattr(analysis_services, "call_structured", fake_call)


def _run_pipeline(job_id):
    """按 worker 的真实路径执行一次 Run。"""
    from apps.analysis import services as analysis_services
    from apps.jobs.services import claim_execution

    context = claim_execution(job_id)
    run_id = context.run_id
    analysis_services.execute_run(context, run_id)
    return AnalysisRun.objects.get(pk=run_id)


# ── 1. 受理 ───────────────────────────────────────────────────
def test_run_requires_at_least_two_papers(client_a, project_a, monkeypatch):
    """少于 2 篇纳入文献返回 422 INSUFFICIENT_PAPERS。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    _upload_paper(client_a, project_a, name="only")
    response = _create_run(client_a, project_a)

    assert response.status_code == 422, response.json()
    assert response.json()["error"]["code"] == "INSUFFICIENT_PAPERS"
    assert response.json()["error"]["details"]["included_count"] == 1
    assert not AnalysisRun.objects.exists(), "被拒绝时不得创建 Run"


def test_run_accepts_two_papers_with_202(client_a, project_a, two_papers):
    response = _create_run(client_a, project_a)

    assert response.status_code == 202, response.json()
    data = response.json()["data"]
    assert data["resource_type"] == "analysis_run"
    run = AnalysisRun.objects.get(pk=data["resource_id"])
    assert run.status == "processing"
    assert run.run_papers.count() == 2


def test_same_idempotency_key_replays_without_new_run(client_a, project_a, two_papers):
    """同键重放返回原受理响应，即使项目此刻正忙。

    幂等查找必须先于 PROJECT_BUSY：否则前端重复提交会被误报为"项目忙"，
    而契约要求同键同体重放永远返回第一次的 202。
    """
    key = str(uuid.uuid4())
    first = _create_run(client_a, project_a, key=key)
    second = _create_run(client_a, project_a, key=key)

    assert first.status_code == 202, first.json()
    assert second.status_code == 202, second.json()
    assert first.json()["data"] == second.json()["data"]
    assert AnalysisRun.objects.count() == 1, "重放不得产生第二套 Run"


def test_create_run_rejects_unknown_fields(client_a, project_a, two_papers):
    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/runs",
        {"research_question": "不该被接受"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 400, response.json()
    assert "research_question" in response.json()["error"]["details"]["fields"]
    assert not AnalysisRun.objects.exists()


def test_create_run_missing_idempotency_key_is_400(client_a, project_a, two_papers):
    response = client_a.post(f"/api/v1/projects/{project_a.id}/runs", {}, format="json")
    assert response.status_code == 400


# ── 2. 忙状态 ─────────────────────────────────────────────────
def test_busy_project_rejects_second_run(client_a, project_a, two_papers, user_a):
    from apps.jobs.models import Job

    first = _create_run(client_a, project_a)
    assert first.status_code == 202
    assert Job.objects.filter(kind="analyze", status="queued").count() == 1

    second = _create_run(client_a, project_a)
    assert second.status_code == 409, second.json()
    assert second.json()["error"]["code"] == "PROJECT_BUSY"
    assert AnalysisRun.objects.count() == 1


def test_busy_project_rejects_metadata_edit(client_a, project_a, two_papers):
    paper_id = two_papers[0]["id"]
    assert _create_run(client_a, project_a).status_code == 202
    response = client_a.patch(
        f"/api/v1/papers/{paper_id}", {"expected_version": 1, "included": False}, format="json"
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "PROJECT_BUSY"


# ── 3. 冻结快照 ───────────────────────────────────────────────
def _free_project():
    """把活跃 Job 置为终态，让项目恢复空闲（编辑类操作才被允许）。"""
    from apps.jobs.models import Job

    Job.objects.filter(status__in=("queued", "running", "cancelling")).update(status="failed")


def test_run_freezes_research_question_and_corpus(client_a, project_a, two_papers):
    """研究问题 / 语料版本 / 配置在创建时冻结，后续编辑不改旧 Run。"""
    assert _create_run(client_a, project_a).status_code == 202
    run = AnalysisRun.objects.get()
    frozen_question = run.research_question
    frozen_revision = run.corpus_revision
    assert run.config_snapshot["parsing"]["PARSER_REVISION"]
    assert run.config_snapshot["extraction"]["PAPER_TEMPLATE_VERSION"]

    # 活跃任务期间禁止编辑（PROJECT_BUSY 是正确行为），先释放项目
    _free_project()
    patched = client_a.patch(
        f"/api/v1/projects/{project_a.id}",
        {"expected_version": 1, "research_question": "换一个完全不同的研究问题。"},
        format="json",
    )
    assert patched.status_code == 200, patched.json()
    run.refresh_from_db()
    assert run.research_question == frozen_question
    assert run.corpus_revision == frozen_revision

    detail = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]
    assert detail["research_question"] == frozen_question, "旧 Run 必须读冻结问题"
    assert detail["is_stale"] is True, "语料变化后旧 Run 应标记 stale"


def test_run_paper_snapshot_keeps_original_metadata(client_a, project_a, two_papers):
    """RunPaper 冻结标题与年份；后续编辑 Paper 不改变快照。"""
    assert _create_run(client_a, project_a).status_code == 202
    run = AnalysisRun.objects.get()
    snapshot = {item.paper_id: item for item in run.run_papers.all()}
    paper_id = uuid.UUID(two_papers[0]["id"])
    assert snapshot[paper_id].year == 2020

    _free_project()
    patched = client_a.patch(
        f"/api/v1/papers/{paper_id}",
        {"expected_version": 2, "title": "改名后的标题", "year": 2024},
        format="json",
    )
    assert patched.status_code == 200, patched.json()

    run.refresh_from_db()
    refreshed = {item.paper_id: item for item in run.run_papers.all()}
    assert refreshed[paper_id].title != "改名后的标题", "快照不得跟随编辑"
    assert refreshed[paper_id].year == 2020


# ── 4. 成功路径 ───────────────────────────────────────────────
def test_pipeline_parses_extracts_and_exposes_analysis(client_a, project_a, two_papers, monkeypatch):
    """两篇都成功：回填解析产物、保存单篇分析，且可通过 API 读回。"""
    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]

    run = _run_pipeline(accepted["job_id"])

    assert run.run_papers.filter(status=RUN_PAPER_READY).count() == 2
    assert PaperAnalysis.objects.count() == 2
    for run_paper in run.run_papers.all():
        assert run_paper.parsed_document_id is not None, "解析产物未回填"
        analysis = run_paper.analysis
        for name in ("research_question", "research_object", "summary"):
            assert isinstance(analysis.payload[name]["value"], (str, type(None)))
        assert analysis.model_call_ids, "必须记录关联的模型调用"

    # 单篇分析可读，且证据来源一致
    paper_id = two_papers[0]["id"]
    detail = client_a.get(f"/api/v1/runs/{run.id}/papers/{paper_id}/analysis")
    assert detail.status_code == 200, detail.json()
    dto = detail.json()["data"]
    # 契约要求八个分析字段**展平在顶层**，不得嵌在 payload 里
    assert "payload" not in dto, "单篇分析不得返回嵌套 payload"
    assert dto["summary"]["value"] == "结论成立"
    assert dto["run_id"] == str(run.id)
    assert dto["paper_id"] == paper_id

    evidence_id = dto["summary"]["evidence_ids"][0]
    evidence = client_a.get(f"/api/v1/runs/{run.id}/evidence/{evidence_id}")
    assert evidence.status_code == 200, evidence.json()
    body = evidence.json()["data"]
    assert body["paper_id"] == paper_id, "证据必须属于本篇"
    assert body["file_url"] == f"/api/v1/papers/{paper_id}/file"
    assert body["locator"]["bbox"] is None, "MVP 不得声称句子级坐标"


def test_analysis_readable_before_run_ready(client_a, project_a, two_papers, monkeypatch):
    """单篇已完成产物在 Run 尚未 ready 时也必须可读（契约 T09）。

    M09 之后流水线会一路跑到 ready，因此这里刻意让 Run 级阶段失败来制造
    "逐篇产物齐全、Run 非 ready"的中间态——这正是契约要求可读的那种状态。
    """
    from apps.analysis import services as analysis_services

    _patch_extractor(monkeypatch, _extract_payload)
    real_call = analysis_services.call_structured

    def broken_topics(context, request):
        from apps.llm.schemas import TASK_ASSIGN_TOPICS

        if request.task_type == TASK_ASSIGN_TOPICS:
            raise ApiError("MODEL_OUTPUT_INVALID", "模拟主题归纳失败")
        return real_call(context, request)

    monkeypatch.setattr(analysis_services, "call_structured", broken_topics)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status != "ready", "本用例需要一个非 ready 的 Run"
    response = client_a.get(f"/api/v1/runs/{run.id}/papers/{two_papers[0]['id']}/analysis")
    assert response.status_code == 200, "单篇产物在 Run 非 ready 时也应可读"

    # 而 Run 级产物（主题 / 争议）此时必须明确不可读
    for path in ("topics", "disputes"):
        blocked = client_a.get(f"/api/v1/runs/{run.id}/{path}")
        assert blocked.status_code == 409, blocked.json()
        assert blocked.json()["error"]["code"] == "RESULT_NOT_READY"


def test_evidence_list_filters_by_page(client_a, project_a, two_papers, monkeypatch):
    """按页取证据；页码超出真实页数返回 VALIDATION_ERROR。"""
    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])
    paper_id = two_papers[0]["id"]

    listed = client_a.get(
        f"/api/v1/runs/{run.id}/papers/{paper_id}/evidence", {"page_number": 1}
    )
    assert listed.status_code == 200, listed.json()
    assert listed.json()["data"], "第一页应有证据片段"
    assert all(item["page_number"] == 1 for item in listed.json()["data"])

    bad = client_a.get(
        f"/api/v1/runs/{run.id}/papers/{paper_id}/evidence", {"page_number": 999}
    )
    assert bad.status_code == 400, bad.json()
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


# ── 5. 输出结构校验 ───────────────────────────────────────────
def _evidence_ids_of(run_paper: RunPaper) -> list[str]:
    from apps.parsing.models import EvidenceChunk

    return [
        str(value)
        for value in EvidenceChunk.objects.filter(
            parsed_document_id=run_paper.parsed_document_id
        )
        .order_by("page_number", "ordinal")
        .values_list("id", flat=True)
    ]


@pytest.fixture
def parsed_run_paper(client_a, project_a, two_papers, monkeypatch):
    """跑完一篇，返回 (RunPaper, 本篇证据 ID 列表)。"""
    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])
    run_paper = run.run_papers.order_by("created_at").first()
    return run_paper, _evidence_ids_of(run_paper)


def test_validate_rejects_missing_and_unknown_fields(parsed_run_paper):
    run_paper, ids = parsed_run_paper
    good = _extract_payload(ids)

    incomplete = dict(good)
    incomplete.pop("limitations")
    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(incomplete, run_scope=run_paper)

    unknown = {**good, "citation_count": 42}
    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(unknown, run_scope=run_paper)


def test_validate_rejects_placeholder_value_without_evidence(parsed_run_paper):
    """缺失必须写 null；用空串/占位文本代替会被拒绝。"""
    run_paper, ids = parsed_run_paper
    payload = _extract_payload(ids)
    payload["summary"] = {"value": "", "evidence_ids": []}
    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(payload, run_scope=run_paper)


def test_validate_rejects_fact_without_evidence(parsed_run_paper):
    run_paper, ids = parsed_run_paper
    payload = _extract_payload(ids)
    payload["summary"] = {"value": "有结论但没给证据", "evidence_ids": []}
    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(payload, run_scope=run_paper)


def test_validate_rejects_unknown_evidence_id(parsed_run_paper):
    run_paper, ids = parsed_run_paper
    payload = _extract_payload(ids)
    payload["summary"] = {"value": "引用了不存在的证据", "evidence_ids": [str(uuid.uuid4())]}
    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(payload, run_scope=run_paper)


def test_validate_rejects_evidence_from_the_other_paper(parsed_run_paper, project_a):
    """同 Run 但属于**另一篇**论文的证据必须拒绝（跨篇引用）。"""
    run_paper, _ids = parsed_run_paper
    other = RunPaper.objects.filter(run_id=run_paper.run_id).exclude(pk=run_paper.pk).first()
    other_ids = _evidence_ids_of(other)
    assert other_ids, "另一篇也应有证据，否则本用例失去意义"

    payload = _extract_payload(other_ids)
    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(payload, run_scope=run_paper)


def test_validate_accepts_null_with_empty_evidence(parsed_run_paper):
    """缺失信息写成 null + [] 是合法结果，不要求模型凑内容。"""
    run_paper, ids = parsed_run_paper
    payload = _extract_payload(ids)
    payload["datasets"] = [{"value": None, "evidence_ids": []}]
    payload["metrics"] = []
    result = validate_paper_analysis(payload, run_scope=run_paper)
    assert result["datasets"] == [{"value": None, "evidence_ids": []}]
    assert result["metrics"] == []


# ── 6. 部分失败 ───────────────────────────────────────────────
def test_one_paper_failure_fails_run_but_keeps_success(client_a, project_a, two_papers, monkeypatch):
    """一篇成功一篇失败：Run.failed，成功产物保留，失败原因可见。"""
    from apps.analysis import services as analysis_services

    calls = {"count": 0}

    class _Result:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = f"mock-{uuid.uuid4().hex[:8]}"

    def flaky(context, request):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ApiError("MODEL_OUTPUT_INVALID", "模拟第二篇结构不合法")
        return _Result(_extract_payload(list(request.allowed_evidence_ids)))

    monkeypatch.setattr(analysis_services, "call_structured", flaky)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status == RUN_FAILED
    assert run.run_papers.filter(status=RUN_PAPER_READY).count() == 1
    failed = run.run_papers.filter(status=RUN_PAPER_FAILED).first()
    assert failed is not None and failed.error["code"] == "MODEL_OUTPUT_INVALID"
    assert PaperAnalysis.objects.count() == 1, "成功产物必须保留，且不虚报全成功"

    detail = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]
    assert detail["stats"]["ready_count"] == 1
    assert detail["stats"]["failed_count"] == 1


def test_invalid_model_output_is_rejected_not_stored(client_a, project_a, two_papers, monkeypatch):
    """结构不合法（假证据）不得写入 PaperAnalysis。"""

    def bogus(ids):
        return _extract_payload(ids) | {
            "summary": {"value": "假的", "evidence_ids": [str(uuid.uuid4())]}
        }

    _patch_extractor(monkeypatch, bogus)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status == RUN_FAILED
    assert not PaperAnalysis.objects.exists(), "结构不合法的输出不得入库"
    failed = run.run_papers.filter(status=RUN_PAPER_FAILED).first()
    assert failed.error["code"] == "MODEL_OUTPUT_INVALID"


# ── 7. 取消与租约 ─────────────────────────────────────────────
def test_cancelled_run_does_not_publish_results(client_a, project_a, two_papers, user_a, monkeypatch):
    """取消后停止后续调用，Run.cancelled，未完成的论文交回 pending。"""
    from apps.analysis import services as analysis_services
    from apps.common.auth import ActorContext
    from apps.jobs.services import cancel_job

    calls = {"count": 0}

    class _Result:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = "mock-cancel"

    def cancelling(context, request):
        calls["count"] += 1
        if calls["count"] == 1:
            cancel_job(ActorContext(user=user_a, request_id="req_test"), context.job_id)
        return _Result(_extract_payload(list(request.allowed_evidence_ids)))

    monkeypatch.setattr(analysis_services, "call_structured", cancelling)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status == "cancelled", run.error
    assert calls["count"] == 1, "取消后不得继续发起模型调用"
    pending = run.run_papers.filter(status="pending").count()
    assert pending >= 1, "未完成的论文必须交回 pending，供重试继续"


def test_expired_lease_cannot_publish_results(client_a, project_a, two_papers, monkeypatch):
    """租约失效后不得写入业务结果。"""
    from datetime import timedelta

    from django.utils import timezone

    from apps.analysis import services as analysis_services
    from apps.jobs.models import Job

    class _Result:
        payload = None
        call_id = "mock-lease"

    def stale_lease(context, request):
        Job.objects.filter(pk=context.job_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        return _Result()

    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]
    monkeypatch.setattr(analysis_services, "call_structured", stale_lease)

    with pytest.raises(ApiError):
        _run_pipeline(accepted["job_id"])

    assert not PaperAnalysis.objects.exists(), "租约失效时不得写入单篇产物"


# ── 8. 年份统计 ───────────────────────────────────────────────
def test_year_counts_use_frozen_snapshot(client_a, project_a, two_papers, monkeypatch):
    """year_counts / unknown_year_count 只读 RunPaper 冻结年份。"""
    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    stats = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]["stats"]
    assert stats["included_count"] == 2
    assert stats["year_counts"] == {"2020": 1, "2021": 1}
    assert stats["unknown_year_count"] == 0


def test_unknown_year_is_counted_separately(client_a, project_a, monkeypatch):
    """缺年份的文献进入 unknown_year_count，不被算进某一年。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    _upload_paper(client_a, project_a, name="gamma")  # 不设年份
    _upload_paper(client_a, project_a, name="delta", year=2019)
    _patch_extractor(monkeypatch, _extract_payload)

    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])
    stats = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]["stats"]

    assert stats["unknown_year_count"] == 1
    assert stats["year_counts"] == {"2019": 1}


# ── 9. Run 级阶段失败时不得宣布成功 ───────────────────────────
def test_run_level_stage_failure_does_not_declare_ready(
    client_a, project_a, two_papers, monkeypatch
):
    """Run 级阶段（topics / disputes）失败时，逐篇产物保留但 Run 必须显式失败。

    M09 之前这里断言的是"后续阶段未实现 → pending_stages"；M09 之后这些阶段
    已经实现，因此改为断言真正的失败路径：模型对主题归纳返回不合法结构。
    """
    from apps.analysis import services as analysis_services
    from apps.analysis.models import PaperAnalysis

    _patch_extractor(monkeypatch, _extract_payload)

    real_call = analysis_services.call_structured

    def broken_topics(context, request):
        from apps.llm.schemas import TASK_ASSIGN_TOPICS

        if request.task_type == TASK_ASSIGN_TOPICS:
            raise ApiError("MODEL_OUTPUT_INVALID", "模拟主题归纳输出不合法")
        return real_call(context, request)

    monkeypatch.setattr(analysis_services, "call_structured", broken_topics)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status != "ready", "Run 级阶段失败时绝不能标 ready"
    assert run.status == RUN_FAILED
    assert run.error["code"] == "MODEL_OUTPUT_INVALID"
    assert PaperAnalysis.objects.count() == 2, "逐篇成功产物必须保留"

    from apps.jobs.models import Job

    job = Job.objects.get(pk=accepted["job_id"])
    assert job.status == "failed", job.status
    assert job.error["code"] == run.error["code"]


def test_index_stage_publishes_artifact(client_a, project_a, two_papers, monkeypatch):
    """index 阶段必须发布 Run 专属索引产物与 manifest。"""
    from apps.analysis import services as analysis_services

    _patch_extractor(monkeypatch, _extract_payload)

    real_call = analysis_services.call_structured

    def stop_after_index(context, request):
        from apps.llm.schemas import TASK_ASSIGN_TOPICS

        if request.task_type == TASK_ASSIGN_TOPICS:
            raise ApiError("MODEL_OUTPUT_INVALID", "停在 index 之后，便于检查产物")
        return real_call(context, request)

    monkeypatch.setattr(analysis_services, "call_structured", stop_after_index)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.index_asset_id is not None, "index 阶段没有登记索引产物"
    assert run.index_manifest, "index 阶段没有写入 manifest"
    manifest = run.index_manifest
    assert manifest["run_id"] == str(run.id)
    assert manifest["chunk_count"] > 0
    assert manifest["index_config"]["version"]
    assert manifest["parsed_document_ids"], "manifest 必须记录解析产物 ID"


def test_topics_and_disputes_are_readable_once_ready(client_a, project_a, two_papers, monkeypatch):
    """Run ready 之后主题 / 争议列表可读，且 DTO 结构符合契约。

    "非 ready 时返回 RESULT_NOT_READY" 已由
    `test_analysis_readable_before_run_ready` 覆盖（那里制造了非 ready 的中间态）。
    """
    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status == "ready", run.error

    topics = client_a.get(f"/api/v1/runs/{run.id}/topics")
    assert topics.status_code == 200, topics.json()
    items = topics.json()["data"]
    assert items, "应当归纳出至少一个主题"
    for item in items:
        assert {"id", "run_id", "name", "description", "paper_ids", "evidence_ids"} <= set(item)
        assert item["run_id"] == str(run.id)
        assert item["paper_ids"], "主题成员至少 1 篇"

    disputes = client_a.get(f"/api/v1/runs/{run.id}/disputes")
    assert disputes.status_code == 200, disputes.json()
    # 没有争议候选是合法结果：替身默认不构造，因此这里只断言接口可读且是列表
    assert isinstance(disputes.json()["data"], list)


# ── 越权 ──────────────────────────────────────────────────────
def test_foreign_run_is_404(client_a, client_b, project_a, two_papers):
    accepted = _create_run(client_a, project_a).json()["data"]
    run_id = accepted["resource_id"]

    assert client_b.get(f"/api/v1/runs/{run_id}").status_code == 404
    assert (
        client_b.get(f"/api/v1/runs/{run_id}/papers/{two_papers[0]['id']}/analysis").status_code
        == 404
    )
