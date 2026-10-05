"""Run 与检索测试（docs/mvp/01 第 9 节、docs/mvp/05 T09 / T10 / T11）。

**已全部打开，无 skip**：
- M07「分析快照与单篇理解」：Run 冻结 research_question / corpus_revision，不随后续编辑变化；
  少于 2 篇纳入文献返回 422 INSUFFICIENT_PAPERS；任一 RunPaper 失败则 Run.failed，
  成功阶段保留；语料变化后旧 Run 可读、is_stale 为 true；年份统计只读 RunPaper 冻结快照；
  主题允许多归属、争议左右必须不同、争议复核递增 analysis_revision、rejected 必须带说明；
  证据跨 Run 引用被拒绝。
- M08「检索与结果持久化」：过时 Run 阻断**新检索**（409 STALE_RUN）；
  重排不得引入候选之外的证据（422 INVALID_EVIDENCE）。
- M09「主题与争议候选」：主题由流水线自己归纳，一文多主题合法，Run 由流水线走到 ready。

并发相关结论必须用真实 PostgreSQL 事务与独立连接验证，不能用 SQLite 顶替。
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest import make_text_pdf_file

from apps.analysis.models import (
    AnalysisRun,
    Dispute,
    PaperAnalysis,
    RunPaper,
    Topic,
)
from apps.analysis.schemas import SchemaValidationError, validate_paper_analysis
from apps.common.http import ApiError

pytestmark = pytest.mark.django_db

PAGES = [
    "Abstract\nEvidence for the frozen corpus test.",
    "Introduction\nBaseline comparison details.",
    "3. Method\nThe proposed approach in detail.",
]


def _upload(client, project, *, name: str, year=None):
    response = client.post(
        f"/api/v1/projects/{project.id}/papers/upload",
        {"file": make_text_pdf_file([f"{name} {p}" for p in PAGES], name=f"{name}.pdf")},
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


def _free_project():
    """把活跃 Job 置为终态，让项目恢复空闲。"""
    from apps.jobs.models import Job

    Job.objects.filter(status__in=("queued", "running", "cancelling")).update(status="failed")


@pytest.fixture
def corpus(client_a, project_a, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    first = _upload(client_a, project_a, name="alpha", year=2018)
    second = _upload(client_a, project_a, name="beta", year=2022)
    return first, second


def _extract_payload(ids: list[str]) -> dict:
    single = {"value": "结论成立", "evidence_ids": [ids[0]]}
    listed = [{"value": "方法要点", "evidence_ids": [ids[-1]]}]
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


def _patch_extractor(monkeypatch):
    from apps.analysis import services as analysis_services

    class _Result:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = f"mock-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        analysis_services,
        "call_structured",
        lambda context, request: _Result(_extract_payload(list(request.allowed_evidence_ids))),
    )


def _run_pipeline(job_id):
    from apps.analysis import services as analysis_services
    from apps.jobs.services import claim_execution

    context = claim_execution(job_id)
    analysis_services.execute_run(context, context.run_id)
    return AnalysisRun.objects.get(pk=context.run_id)


# ── Run 冻结与 stale ──────────────────────────────────────────
def test_run_freezes_research_question_and_corpus(client_a, project_a, corpus):
    from apps.projects.models import Project

    # fixture 里的 Project 是上传前的快照；上传会递增 corpus_revision，必须重读
    project = Project.objects.get(pk=project_a.pk)
    frozen_question = project.research_question

    assert _create_run(client_a, project_a).status_code == 202
    run = AnalysisRun.objects.get()
    assert run.research_question == frozen_question
    assert run.corpus_revision == project.corpus_revision

    _free_project()
    assert (
        client_a.patch(
            f"/api/v1/projects/{project_a.id}",
            {"expected_version": 1, "research_question": "一个全新的研究问题，用于制造 stale。"},
            format="json",
        ).status_code
        == 200
    )

    run.refresh_from_db()
    assert run.research_question == frozen_question, "Run 必须保留冻结问题"
    detail = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]
    assert detail["is_stale"] is True, "语料变化后旧 Run 应标记 stale"
    assert detail["research_question"] == frozen_question, "历史页显示冻结问题"


def test_run_requires_at_least_two_papers(client_a, project_a, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    _upload(client_a, project_a, name="solo")
    response = _create_run(client_a, project_a)

    assert response.status_code == 422, response.json()
    assert response.json()["error"]["code"] == "INSUFFICIENT_PAPERS"
    assert not AnalysisRun.objects.exists()


# ── 任一论文失败使 Run 失败 ───────────────────────────────────
def test_any_run_paper_failure_fails_the_run(client_a, project_a, corpus, monkeypatch):
    from apps.analysis import services as analysis_services

    calls = {"n": 0}

    class _Result:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = "mock-fail"

    def flaky(context, request):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ApiError("PDF_PARSE_FAILED", "模拟第二篇解析失败")
        return _Result(_extract_payload(list(request.allowed_evidence_ids)))

    monkeypatch.setattr(analysis_services, "call_structured", flaky)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    assert run.status == "failed", "任一论文失败则 Run 失败"
    assert run.run_papers.filter(status="ready").count() == 1, "成功阶段必须保留"
    assert PaperAnalysis.objects.count() == 1
    assert run.error["details"]["papers"], "失败原因必须可见"


# ── 年份统计 ──────────────────────────────────────────────────
def test_year_counts_use_frozen_snapshot(client_a, project_a, corpus, monkeypatch):
    _patch_extractor(monkeypatch)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    stats = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]["stats"]
    assert stats["included_count"] == 2
    assert stats["year_counts"] == {"2018": 1, "2022": 1}
    assert stats["unknown_year_count"] == 0

    # 修改 Paper 年份不改变已冻结统计
    _free_project()
    paper_id = uuid.UUID(corpus[0]["id"])
    RunPaper.objects.filter(paper_id=paper_id).values_list("year", flat=True).get()
    assert (
        client_a.patch(
            f"/api/v1/papers/{paper_id}", {"expected_version": 2, "year": 2024}, format="json"
        ).status_code
        == 200
    )
    stats_after = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]["stats"]
    assert stats_after["year_counts"] == {"2018": 1, "2022": 1}, "统计必须只读快照"


# ── 主题与争议（数据面）──────────────────────────────────────
def _make_run(project, *, corpus_revision=1, status="ready"):
    return AnalysisRun.objects.create(
        project=project,
        status=status,
        corpus_revision=corpus_revision,
        research_question="用于主题与争议测试的研究问题",
        output_language="zh-CN",
    )


def _make_run_paper(run, paper, *, status="ready"):
    return RunPaper.objects.create(
        run=run,
        paper=paper,
        title=paper.title,
        authors=[],
        year=paper.year,
        source=paper.source,
        file_asset=paper.file_asset,
        file_sha256=paper.file_sha256,
        page_count=paper.page_count,
        status=status,
    )


def test_topics_allow_multi_membership(project_a, corpus):
    """一文多主题合法：成员至少 1 篇且都属于本 Run。"""
    from apps.literature.models import Paper

    papers = list(Paper.objects.filter(project=project_a).order_by("created_at"))
    run = _make_run(project_a)
    for paper in papers:
        _make_run_paper(run, paper)

    shared = str(papers[0].id)
    first = Topic.objects.create(
        run=run, name="注意力机制", description="围绕注意力模块。", paper_ids=[shared]
    )
    second = Topic.objects.create(
        run=run,
        name="小样本学习",
        description="少样本评估协议。",
        paper_ids=[shared, str(papers[1].id)],
    )

    assert first.paper_ids == [shared]
    assert shared in second.paper_ids, "同一篇文献可以同时属于多个主题"
    assert Topic.objects.filter(run=run).count() == 2


def test_dispute_sides_must_differ(project_a, corpus):
    """左右论文相同时被数据库约束拒绝（不靠服务层自觉）。"""
    from django.db import IntegrityError
    from django.db.utils import InternalError

    from apps.literature.models import Paper

    paper = Paper.objects.filter(project=project_a).order_by("created_at").first()
    run = _make_run(project_a)
    with pytest.raises((IntegrityError, InternalError)):
        Dispute.objects.create(
            run=run,
            left_paper=paper,
            right_paper=paper,
            classification="possible_conflict",
            reason="左右相同，应被数据库拒绝",
        )


def test_dispute_review_bumps_analysis_revision(client_a, ready_run):
    """人工复核递增 Dispute.version 与 Run.analysis_revision。"""
    run = ready_run
    papers = list(run.run_papers.order_by("created_at"))
    dispute = Dispute.objects.create(
        run=run,
        left_paper=papers[0].paper,
        right_paper=papers[1].paper,
        left_claim={"value": "结论 A", "evidence_ids": []},
        right_claim={"value": "结论 B", "evidence_ids": []},
        conditions={
            "research_question": "同一问题",
            "left_conditions": "条件甲",
            "right_conditions": "条件乙",
            "comparability_reason": "条件略有差异",
        },
        classification="possible_conflict",
        reason="初步判断存在分歧",
    )
    before = run.analysis_revision

    response = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "confirmed", "review_note": "两名评审确认"},
        format="json",
    )
    assert response.status_code == 200, response.json()
    body = response.json()["data"]
    assert body["review_status"] == "confirmed"
    assert body["version"] == 2
    assert body["classification"] == "possible_conflict", "自动分类不得被人工覆盖"

    run.refresh_from_db()
    assert run.analysis_revision == before + 1, "人工复核必须递增 analysis_revision"


def test_dispute_rejected_requires_note(client_a, ready_run):
    run = ready_run
    papers = list(run.run_papers.order_by("created_at"))
    dispute = Dispute.objects.create(
        run=run,
        left_paper=papers[0].paper,
        right_paper=papers[1].paper,
        classification="possible_conflict",
        reason="初步判断存在分歧",
    )
    response = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "rejected"},
        format="json",
    )
    assert response.status_code == 400, response.json()
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ── 证据跨 Run 引用 ───────────────────────────────────────────
def test_evidence_reference_across_runs_is_rejected(client_a, project_a, corpus, monkeypatch):
    """跨 Run 引用证据一律拒绝：另一个 Run 读不到不属于它的证据。"""
    _patch_extractor(monkeypatch)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])
    run_paper = run.run_papers.first()
    evidence_id = run_paper.analysis.payload["summary"]["evidence_ids"][0]

    from apps.parsing.evidence import get_evidence_batch

    assert get_evidence_batch(run.id, [evidence_id]), "本 Run 应能读到自己的证据"

    # 另一个 Run 未纳入任何解析产物
    other = _make_run(project_a, status="processing")
    assert get_evidence_batch(other.id, [evidence_id]) == [], "跨 Run 引用必须被拒绝"

    # 即使该 Run 纳入了同一篇论文，但未回填解析产物，也不可见
    from apps.literature.models import Paper

    paper = Paper.objects.get(pk=run_paper.paper_id)
    _make_run_paper(other, paper, status="pending")
    assert get_evidence_batch(other.id, [evidence_id]) == []


def test_analysis_rejects_evidence_from_another_paper(client_a, project_a, corpus, monkeypatch):
    """同 Run 内跨篇引用同样被拒绝。"""
    _patch_extractor(monkeypatch)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    run_papers = list(run.run_papers.order_by("created_at"))
    from apps.parsing.models import EvidenceChunk

    other_ids = [
        str(value)
        for value in EvidenceChunk.objects.filter(
            parsed_document_id=run_papers[1].parsed_document_id
        ).values_list("id", flat=True)
    ]
    assert other_ids

    with pytest.raises(SchemaValidationError):
        validate_paper_analysis(_extract_payload(other_ids), run_scope=run_papers[0])


# ── 检索（M08）────────────────────────────────────────────────
def test_stale_run_blocks_new_retrieval(client_a, project_a, corpus, monkeypatch):
    """语料变化后旧 Run 仍可读，但**新检索**返回 409 STALE_RUN。"""
    _patch_extractor(monkeypatch)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])

    # M07 之后 Run 是 failed；检索的前置是 ready，这里按 M08 的真实前置推进
    run.status = "ready"
    run.error = None
    run.save(update_fields=["status", "error", "updated_at"])

    _free_project()
    detail = client_a.get(f"/api/v1/runs/{run.id}")
    assert detail.status_code == 200, "旧 Run 仍可读"

    assert (
        client_a.patch(
            f"/api/v1/projects/{project_a.id}",
            {"expected_version": 1, "research_question": "换问题以制造 stale 语料快照。"},
            format="json",
        ).status_code
        == 200
    )
    assert client_a.get(f"/api/v1/runs/{run.id}").json()["data"]["is_stale"] is True

    response = client_a.post(
        f"/api/v1/runs/{run.id}/retrievals",
        {"query": "attention method"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "STALE_RUN"


def test_rerank_cannot_introduce_new_evidence(ready_run, monkeypatch):
    """重排结果包含候选外 ID 时返回 422 INVALID_EVIDENCE。

    直接打 M08 的校验入口：候选集合由召回产生，模型只被允许重排它们。
    """
    from apps.common.http import ApiError
    from apps.retrieval.services import validate_ranked_evidence

    candidate_ids = {str(uuid.uuid4()) for _ in range(3)}
    known = sorted(candidate_ids)
    stranger = str(uuid.uuid4())

    assert validate_ranked_evidence(
        [{"evidence_id": known[0], "score": 0.4, "reason": "ok"}], candidate_ids
    )

    with pytest.raises(ApiError) as error:
        validate_ranked_evidence(
            [{"evidence_id": stranger, "score": 0.9, "reason": "候选外"}], candidate_ids
        )
    assert error.value.code == "INVALID_EVIDENCE"
    assert error.value.status_code == 422


# ── 主题归纳服务（M09）────────────────────────────────────────
def test_topics_are_generated_by_service(client_a, project_a, corpus, monkeypatch):
    """主题由流水线自己归纳：成员至少 1 篇、允许一文多主题、允许未归类。

    更细的校验（跨成员证据、未知字段、条件对齐、作用域收窄等）见
    `tests/test_m09_topics_disputes.py`。
    """
    from apps.analysis import services as analysis_services
    from apps.llm.schemas import (
        TASK_ASSIGN_TOPICS,
        TASK_COMPARE_DISPUTES,
        TASK_PAPER_EXTRACT,
    )

    _patch_extractor(monkeypatch)
    real_call = analysis_services.call_structured

    class _Fake:
        def __init__(self, payload):
            self.payload = payload
            self.call_id = "mock:topics"

    seen: dict[str, list[str]] = {}

    def fake_call(context, request):
        if request.task_type == TASK_ASSIGN_TOPICS:
            paper_ids = sorted(str(value) for value in request.allowed_paper_ids)
            seen["ids"] = paper_ids
            return _Fake(
                {
                    "topics": [
                        {
                            "name": "注意力机制",
                            "description": "围绕注意力模块。",
                            "paper_ids": paper_ids,
                            "evidence_ids": [],
                        },
                        {
                            "name": "小样本评估",
                            "description": "少样本协议下的评估。",
                            "paper_ids": [paper_ids[0]],
                            "evidence_ids": [],
                        },
                    ],
                    # 允许未归类：第二篇只出现在第一个主题里，这里不重复声明
                    "unassigned_paper_ids": [],
                }
            )
        if request.task_type == TASK_COMPARE_DISPUTES:
            return _Fake({"disputes": []})
        if request.task_type == TASK_PAPER_EXTRACT:
            return real_call(context, request)
        raise AssertionError(f"未预期的任务类型：{request.task_type}")

    monkeypatch.setattr(analysis_services, "call_structured", fake_call)

    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])
    assert run.status == "ready", run.error

    topics = list(Topic.objects.filter(run=run).order_by("created_at", "id"))
    assert len(topics) == 2, "服务必须落库全部主题"

    # 用替身实际收到的白名单顺序，而不是 `run_papers` 的创建顺序：
    # 两者都是稳定排序，但排序键不同，混用会让断言依赖巧合。
    shared_paper = seen["ids"][0]
    membership = [topic for topic in topics if shared_paper in topic.paper_ids]
    assert len(membership) == 2, "同一篇文献可以同时属于多个主题"
    for topic in topics:
        assert topic.paper_ids, "每个主题的成员至少 1 篇"
        assert topic.name

    # Run 级产物经 API 可读，且附带覆盖统计
    listed = client_a.get(f"/api/v1/runs/{run.id}/topics")
    assert listed.status_code == 200, listed.json()
    assert len(listed.json()["data"]) == 2
    stats = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]["stats"]
    assert stats["included_count"] == 2
    assert stats["ready_count"] == 2
