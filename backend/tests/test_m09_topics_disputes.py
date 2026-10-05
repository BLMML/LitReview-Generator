"""M09 主题与争议候选测试（docs/mvp/01 第 4.4 节、docs/mvp/04 第 3 节）。

覆盖：

1. **完整流水线**：snapshot → parse → extract → index → topics → disputes → finalize，
   全部成功后 `Run.ready` 与 `Job.succeeded` 在同一事务提交；
2. index 阶段发布 Run 专属索引产物与 manifest，且检索优先复用它；
3. 主题：多归属合法、成员至少 1 篇、允许未归类、证据须来自成员之内；
4. 争议：左右必须不同、都在本 Run 且**在同一主题内**、两侧证据分别属于对应论文、
   条件四字段必填、分类枚举、reason 非空；
5. 阶段失败：topics / disputes 失败时 Run 显式失败，逐篇产物保留；
6. 覆盖统计只读冻结快照；
7. 人工复核：分类不可被覆盖、rejected 必填说明、递增 analysis_revision；
8. 越权与未就绪保护。

模型调用一律用替身，不产生付费调用。真实 PostgreSQL。
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest import analysis_extract_payload, make_text_pdf_file

from apps.analysis.models import (
    RUN_PAPER_READY,
    AnalysisRun,
    Dispute,
    PaperAnalysis,
    Topic,
)
from apps.analysis.schemas import SchemaValidationError, validate_disputes, validate_topics
from apps.common.http import ApiError
from apps.parsing.models import EvidenceChunk
from apps.retrieval import index as index_module
from apps.retrieval.models import RetrievalResult

pytestmark = pytest.mark.django_db

PAGES = [
    "Abstract\nSelf-attention improves sequence modelling on small corpora.",
    "Introduction\nRecurrent baselines are compared with attention models.",
    "3. Method\nWe replace the recurrent layer with a multi-head attention block.",
    "4. Results\nAttention improves accuracy by two points on the benchmark.",
    "5. Limitations\nThe evaluation covers a single language and dataset.",
]


@pytest.fixture(autouse=True)
def _clear_index_cache():
    index_module.clear_index_cache()
    yield
    index_module.clear_index_cache()


class _Result:
    def __init__(self, payload, call_id="mock"):
        self.payload = payload
        self.call_id = call_id


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


@pytest.fixture
def corpus(client_a, project_a, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    return [
        _upload(client_a, project_a, name="alpha", year=2020),
        _upload(client_a, project_a, name="beta", year=2021),
    ]


def _patch_llm(monkeypatch, *, topic_plan=None, disputes_builder=None):
    """替身：单篇抽取固定合法，主题与争议按参数构造。

    `topic_plan` 是 `callable(paper_ids) -> (topics, unassigned)`；默认把全部论文放进
    一个主题（这样争议阶段才有两篇可比较的成员）。
    `disputes_builder` 是 `callable(paper_ids, evidence_by_paper, topic_name) -> payload`。
    """
    from apps.analysis import services as analysis_services
    from apps.llm.schemas import (
        TASK_ASSIGN_TOPICS,
        TASK_COMPARE_DISPUTES,
        TASK_PAPER_EXTRACT,
    )

    # 记录每次调用实际拿到的白名单，供"作用域收窄"类断言使用
    seen: dict[str, list] = {"topics": [], "disputes": []}

    def fake_call(context, request):
        if request.task_type == TASK_PAPER_EXTRACT:
            return _Result(analysis_extract_payload(list(request.allowed_evidence_ids)))
        if request.task_type == TASK_ASSIGN_TOPICS:
            paper_ids = sorted(str(value) for value in request.allowed_paper_ids)
            seen["topics"].append(paper_ids)
            if topic_plan is None:
                return _Result(
                    {
                        "topics": [
                            {
                                "name": "注意力机制",
                                "description": "围绕注意力模块的对比研究。",
                                "paper_ids": paper_ids,
                                "evidence_ids": [],
                            }
                        ],
                        "unassigned_paper_ids": [],
                    }
                )
            topics, unassigned = topic_plan(paper_ids)
            return _Result({"topics": topics, "unassigned_paper_ids": unassigned})
        if request.task_type == TASK_COMPARE_DISPUTES:
            paper_ids = sorted(str(value) for value in request.allowed_paper_ids)
            seen["disputes"].append(paper_ids)
            if disputes_builder is None:
                return _Result({"disputes": []})
            return _Result(disputes_builder(paper_ids, request.allowed_evidence_ids))
        raise AssertionError(f"未预期的任务类型：{request.task_type}")

    monkeypatch.setattr(analysis_services, "call_structured", fake_call)
    return seen


def _run_pipeline(client, project, *, expect_ready=True):
    from apps.analysis import services as analysis_services
    from apps.jobs.services import claim_execution

    accepted = client.post(
        f"/api/v1/projects/{project.id}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    ).json()["data"]
    context = claim_execution(accepted["job_id"])
    analysis_services.execute_run(context, context.run_id)
    run = AnalysisRun.objects.get(pk=context.run_id)
    if expect_ready:
        assert run.status == "ready", run.error
    return run, accepted["job_id"]


def _evidence_of(run, paper_id) -> list[str]:
    run_paper = run.run_papers.get(paper_id=paper_id)
    return [
        str(value)
        for value in EvidenceChunk.objects.filter(
            parsed_document_id=run_paper.parsed_document_id
        )
        .order_by("page_number", "ordinal")
        .values_list("id", flat=True)
    ]


# ── 1. 完整流水线 ─────────────────────────────────────────────
def test_full_pipeline_reaches_ready(client_a, project_a, corpus, monkeypatch):
    """七段全部成功后 Run.ready 与 Job.succeeded 一并提交。"""
    _patch_llm(monkeypatch)
    run, job_id = _run_pipeline(client_a, project_a)

    assert run.status == "ready"
    assert run.error is None, "成功时不得留错误"
    assert run.run_papers.filter(status=RUN_PAPER_READY).count() == 2
    assert PaperAnalysis.objects.count() == 2
    assert Topic.objects.filter(run=run).count() == 1

    from apps.jobs.models import Job

    job = Job.objects.get(pk=job_id)
    assert job.status == "succeeded", job.error
    assert job.result["run_id"] == str(run.id)
    assert job.result["ready_count"] == 2
    assert job.result["topic_count"] == 1


def test_ready_run_needs_no_manual_promotion(client_a, project_a, corpus, monkeypatch):
    """检索前置 `Run.ready` 现在由流水线自己产生——这正是 M08 遗留的缺口。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)

    response = client_a.post(
        f"/api/v1/runs/{run.id}/retrievals",
        {"query": "attention method"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 202, response.json()
    assert RetrievalResult.objects.count() == 1


# ── 2. index 阶段 ─────────────────────────────────────────────
def test_index_artifact_is_published_and_reused(client_a, project_a, corpus, monkeypatch):
    """index 阶段发布 JSON 产物；检索优先复用它，而不是每次重建。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)

    assert run.index_asset_id is not None
    manifest = run.index_manifest
    assert manifest["run_id"] == str(run.id)
    assert manifest["chunk_count"] > 0
    assert manifest["source_digest"], "manifest 必须记录源摘要"
    assert manifest["index_config"]["artifact_format"].startswith("index-json")

    from apps.retrieval.services import run_snapshot_for

    index_module.clear_index_cache()
    loaded = index_module.load_published(run, run_snapshot_for(run))
    assert loaded is not None, "已发布的索引产物应当能被加载"
    assert loaded.index_asset_id == run.index_asset_id
    assert loaded.manifest["source_digest"] == manifest["source_digest"]


def test_index_artifact_mismatch_falls_back_to_rebuild(client_a, project_a, corpus, monkeypatch):
    """源摘要不一致时不得复用产物：必须重建，绝不能拿对不上的索引排序。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)

    from apps.retrieval.services import run_snapshot_for

    # 伪造一个更早的 revision：产物属于旧 revision，必须判为不一致
    run.analysis_revision += 1
    run.save(update_fields=["analysis_revision", "updated_at"])

    # 快照必须在改完之后再取：`build_index` 读的是快照里的 revision，
    # 用旧快照会得到旧值，断言就变成"测快照构造顺序"而不是"测一致性校验"。
    snapshot = run_snapshot_for(run)

    index_module.clear_index_cache()
    assert index_module.load_published(run, snapshot) is None, "revision 不符时不得复用"

    rebuilt = index_module.build_index(snapshot)
    assert rebuilt.engine is not None
    assert rebuilt.manifest["analysis_revision"] == run.analysis_revision


def test_index_artifact_with_corrupt_bytes_is_not_reused(client_a, project_a, corpus, monkeypatch):
    """产物字节被篡改时完整性校验必须失败，并退回内存重建。"""
    from apps.common.storage import resolve_storage_key

    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)

    path = resolve_storage_key(run.index_asset.storage_key)
    path.write_bytes(b'{"format":"index-json-v1","manifest":{},"vocabulary":{}}')

    from apps.retrieval.services import run_snapshot_for

    index_module.clear_index_cache()
    assert index_module.load_published(run, run_snapshot_for(run)) is None


# ── 3. 主题校验 ───────────────────────────────────────────────
def _scope(run, *, only=None):
    from apps.analysis.services import _run_scope

    return _run_scope(run, only_papers=only)


def test_topics_allow_multi_membership(client_a, project_a, corpus, monkeypatch):
    """同一篇文献可以同时属于多个主题。"""
    paper_ids_holder: dict[str, list[str]] = {}

    def plan(paper_ids):
        paper_ids_holder["ids"] = paper_ids
        return (
            [
                {"name": "主题甲", "description": "", "paper_ids": paper_ids, "evidence_ids": []},
                {
                    "name": "主题乙",
                    "description": "",
                    "paper_ids": [paper_ids[0]],
                    "evidence_ids": [],
                },
            ],
            [],
        )

    _patch_llm(monkeypatch, topic_plan=plan)
    run, _job_id = _run_pipeline(client_a, project_a)

    topics = list(Topic.objects.filter(run=run).order_by("created_at"))
    assert len(topics) == 2
    first = paper_ids_holder["ids"][0]
    assert first in topics[0].paper_ids
    assert first in topics[1].paper_ids, "一文多主题必须被保留"


def test_topics_allow_unassigned_papers(client_a, project_a, corpus, monkeypatch):
    """允许未归类论文，并记入快照供覆盖检查使用。"""
    holder: dict[str, str] = {}

    def plan(paper_ids):
        holder["unassigned"] = paper_ids[1]
        return (
            [
                {
                    "name": "仅覆盖第一篇",
                    "description": "",
                    "paper_ids": [paper_ids[0]],
                    "evidence_ids": [],
                }
            ],
            [paper_ids[1]],
        )

    _patch_llm(monkeypatch, topic_plan=plan)
    run, job_id = _run_pipeline(client_a, project_a)

    assert run.config_snapshot["unassigned_paper_ids"] == [holder["unassigned"]]
    from apps.jobs.models import Job

    assert Job.objects.get(pk=job_id).result["unassigned_paper_count"] == 1


def test_topics_reject_empty_members_and_outsiders(client_a, project_a, corpus, monkeypatch):
    """成员至少 1 篇；不属于本 Run 的论文一律拒绝。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    scope = _scope(run)
    known = sorted(scope.allowed_paper_ids)

    with pytest.raises(SchemaValidationError):
        validate_topics(
            {
                "topics": [{"name": "空主题", "description": "", "paper_ids": [], "evidence_ids": []}],
                "unassigned_paper_ids": known,
            },
            run_scope=scope,
        )

    with pytest.raises(SchemaValidationError):
        validate_topics(
            {
                "topics": [
                    {
                        "name": "外来论文",
                        "description": "",
                        "paper_ids": [str(uuid.uuid4())],
                        "evidence_ids": [],
                    }
                ],
                "unassigned_paper_ids": known,
            },
            run_scope=scope,
        )


def test_topics_reject_evidence_from_non_members(client_a, project_a, corpus, monkeypatch):
    """主题证据必须来自**该主题成员**：跨成员引用等于让主题为别人的文献作证。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    scope = _scope(run)
    known = sorted(scope.allowed_paper_ids)
    outsider_evidence = _evidence_of(run, known[1])[0]

    with pytest.raises(SchemaValidationError):
        validate_topics(
            {
                "topics": [
                    {
                        "name": "只含第一篇",
                        "description": "",
                        "paper_ids": [known[0]],
                        "evidence_ids": [outsider_evidence],
                    }
                ],
                "unassigned_paper_ids": [known[1]],
            },
            run_scope=scope,
        )


def test_topics_reject_paper_both_assigned_and_unassigned(client_a, project_a, corpus, monkeypatch):
    """同时"已归类"又"未归类"是自相矛盾的。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    scope = _scope(run)
    known = sorted(scope.allowed_paper_ids)

    with pytest.raises(SchemaValidationError):
        validate_topics(
            {
                "topics": [
                    {
                        "name": "主题",
                        "description": "",
                        "paper_ids": [known[0]],
                        "evidence_ids": [],
                    }
                ],
                "unassigned_paper_ids": [known[0]],
            },
            run_scope=scope,
        )


def test_topics_reject_unknown_fields_and_wrong_types(client_a, project_a, corpus, monkeypatch):
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    scope = _scope(run)
    known = sorted(scope.allowed_paper_ids)
    base = {
        "topics": [
            {"name": "主题", "description": "", "paper_ids": [known[0]], "evidence_ids": []}
        ],
        "unassigned_paper_ids": [known[1]],
    }

    with pytest.raises(SchemaValidationError):
        validate_topics({**base, "extra": 1}, run_scope=scope)
    with pytest.raises(SchemaValidationError):
        validate_topics({"topics": base["topics"]}, run_scope=scope)
    with pytest.raises(SchemaValidationError):
        validate_topics(
            {
                "topics": [
                    {"name": "", "description": "", "paper_ids": [known[0]], "evidence_ids": []}
                ],
                "unassigned_paper_ids": [known[1]],
            },
            run_scope=scope,
        )
    with pytest.raises(SchemaValidationError):
        validate_topics(
            {
                "topics": [
                    {
                        "name": "主题",
                        "description": None,
                        "paper_ids": [known[0]],
                        "evidence_ids": [],
                    }
                ],
                "unassigned_paper_ids": [known[1]],
            },
            run_scope=scope,
        )


# ── 4. 争议校验与写入 ─────────────────────────────────────────
def _dispute_payload(left, right, left_evidence, right_evidence, **overrides):
    payload = {
        "left_paper_id": left,
        "right_paper_id": right,
        "left_claim": {"value": "左侧结论", "evidence_ids": [left_evidence]},
        "right_claim": {"value": "右侧结论", "evidence_ids": [right_evidence]},
        "conditions": {
            "research_question": "注意力是否有效",
            "left_conditions": "小样本设置",
            "right_conditions": "全量数据设置",
            "comparability_reason": "数据集规模不同，结论不能直接比较",
        },
        "classification": "different_conditions",
        "reason": "条件不同，属于可比性存疑而非直接冲突",
    }
    payload.update(overrides)
    return payload


def test_disputes_are_persisted_with_pending_review(client_a, project_a, corpus, monkeypatch):
    """争议候选落库，人工状态必须从 pending 开始。"""
    holder: dict[str, list[str]] = {}

    def builder(paper_ids, _evidence_ids):
        holder["papers"] = paper_ids
        left, right = paper_ids[0], paper_ids[1]
        return {
            "disputes": [
                _dispute_payload(
                    left, right, _evidence_of(_run, left)[0], _evidence_of(_run, right)[0]
                )
            ]
        }

    _patch_llm(monkeypatch, disputes_builder=builder)
    # `builder` 需要 run 才能取证据，因此先跑一遍拿到 run 再重跑不可行；
    # 改为在闭包里延迟获取：先建 run，再执行
    from apps.analysis import services as analysis_services
    from apps.jobs.services import claim_execution

    accepted = client_a.post(
        f"/api/v1/projects/{project_a.id}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    ).json()["data"]
    _run = AnalysisRun.objects.get(pk=accepted["resource_id"])
    context = claim_execution(accepted["job_id"])
    analysis_services.execute_run(context, context.run_id)
    _run.refresh_from_db()

    assert _run.status == "ready", _run.error
    disputes = list(Dispute.objects.filter(run=_run))
    assert len(disputes) == 1
    dispute = disputes[0]
    assert dispute.review_status == "pending", "自动候选不得预置人工状态"
    assert dispute.classification == "different_conditions"
    assert dispute.version == 1
    assert dispute.conditions["comparability_reason"]
    assert set(dispute.left_claim) == {"value", "evidence_ids"}


def test_disputes_reject_same_sides_and_cross_paper_evidence(
    client_a, project_a, corpus, monkeypatch
):
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    scope = _scope(run)
    known = sorted(scope.allowed_paper_ids)
    left_evidence = _evidence_of(run, known[0])[0]
    right_evidence = _evidence_of(run, known[1])[0]

    # 左右相同
    with pytest.raises(SchemaValidationError):
        validate_disputes(
            {"disputes": [_dispute_payload(known[0], known[0], left_evidence, left_evidence)]},
            run_scope=scope,
        )

    # 左侧证据实际来自右侧论文
    with pytest.raises(SchemaValidationError):
        validate_disputes(
            {"disputes": [_dispute_payload(known[0], known[1], right_evidence, right_evidence)]},
            run_scope=scope,
        )

    # 右侧证据实际来自左侧论文
    with pytest.raises(SchemaValidationError):
        validate_disputes(
            {"disputes": [_dispute_payload(known[0], known[1], left_evidence, left_evidence)]},
            run_scope=scope,
        )


def test_disputes_reject_bad_classification_and_conditions(client_a, project_a, corpus, monkeypatch):
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    scope = _scope(run)
    known = sorted(scope.allowed_paper_ids)
    good = _dispute_payload(
        known[0], known[1], _evidence_of(run, known[0])[0], _evidence_of(run, known[1])[0]
    )

    with pytest.raises(SchemaValidationError):
        validate_disputes(
            {"disputes": [{**good, "classification": "definitely_wrong"}]}, run_scope=scope
        )

    broken_conditions = {**good["conditions"], "left_conditions": "   "}
    with pytest.raises(SchemaValidationError):
        validate_disputes(
            {"disputes": [{**good, "conditions": broken_conditions}]}, run_scope=scope
        )

    with pytest.raises(SchemaValidationError):
        validate_disputes({"disputes": [{**good, "reason": ""}]}, run_scope=scope)

    with pytest.raises(SchemaValidationError):
        validate_disputes({"disputes": [{**good, "extra": 1}]}, run_scope=scope)


def test_dispute_scope_is_narrowed_to_topic_members(client_a, project_a, corpus, monkeypatch):
    """争议白名单必须收窄到**同一主题**成员：主题外论文不在候选里，也不可被引用。"""
    seen: dict[str, list] = {}

    def plan(paper_ids):
        # 两篇各自成主题 → 争议阶段没有任何主题有 ≥2 篇成员
        return (
            [
                {"name": "甲", "description": "", "paper_ids": [paper_ids[0]], "evidence_ids": []},
                {"name": "乙", "description": "", "paper_ids": [paper_ids[1]], "evidence_ids": []},
            ],
            [],
        )

    _patch_llm(monkeypatch, topic_plan=plan)
    seen = _patch_llm(monkeypatch, topic_plan=plan)
    run, _job_id = _run_pipeline(client_a, project_a)

    assert Topic.objects.filter(run=run).count() == 2
    assert Dispute.objects.filter(run=run).count() == 0, "单篇主题不应产生争议"
    assert seen["disputes"] == [], "单篇主题不应发起争议比较调用"


def test_dispute_scope_excludes_papers_from_other_topics(client_a, project_a, monkeypatch):
    """同一 Run 内，跨主题引用也要被拒绝。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    for name in ("alpha", "beta", "gamma"):
        _upload(client_a, project_a, name=name, year=2020)

    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    known = sorted(_scope(run).allowed_paper_ids)
    assert len(known) == 3

    narrowed = _scope(run, only=known[:2])
    assert known[2] not in narrowed.allowed_paper_ids, "作用域必须收窄到主题成员"

    with pytest.raises(SchemaValidationError):
        validate_disputes(
            {
                "disputes": [
                    _dispute_payload(
                        known[0],
                        known[2],
                        _evidence_of(run, known[0])[0],
                        _evidence_of(run, known[2])[0],
                    )
                ]
            },
            run_scope=narrowed,
        )


# ── 5. 阶段失败 ───────────────────────────────────────────────
def test_topics_failure_fails_run_but_keeps_papers(client_a, project_a, corpus, monkeypatch):
    """主题阶段失败：Run 显式失败，逐篇产物与索引产物保留。"""
    from apps.analysis import services as analysis_services
    from apps.llm.schemas import TASK_ASSIGN_TOPICS

    _patch_llm(monkeypatch)
    real_call = analysis_services.call_structured

    def broken(context, request):
        if request.task_type == TASK_ASSIGN_TOPICS:
            raise ApiError("MODEL_OUTPUT_INVALID", "模拟主题归纳失败")
        return real_call(context, request)

    monkeypatch.setattr(analysis_services, "call_structured", broken)
    run, job_id = _run_pipeline(client_a, project_a, expect_ready=False)

    assert run.status == "failed"
    assert run.error["code"] == "MODEL_OUTPUT_INVALID"
    assert PaperAnalysis.objects.count() == 2, "逐篇产物必须保留"
    assert run.index_asset_id is not None, "已完成的 index 阶段产物也要保留"

    from apps.jobs.models import Job

    assert Job.objects.get(pk=job_id).status == "failed"
    assert RetrievalResult.objects.count() == 0


def test_disputes_failure_fails_run_but_keeps_topics(client_a, project_a, corpus, monkeypatch):
    """争议阶段失败：主题已提交的产物保留，Run 仍显式失败。"""
    from apps.analysis import services as analysis_services
    from apps.llm.schemas import TASK_COMPARE_DISPUTES

    _patch_llm(monkeypatch)
    real_call = analysis_services.call_structured

    def broken(context, request):
        if request.task_type == TASK_COMPARE_DISPUTES:
            raise ApiError("MODEL_OUTPUT_INVALID", "模拟争议比较失败")
        return real_call(context, request)

    monkeypatch.setattr(analysis_services, "call_structured", broken)
    run, _job_id = _run_pipeline(client_a, project_a, expect_ready=False)

    assert run.status == "failed"
    assert Topic.objects.filter(run=run).count() == 1, "主题阶段产物必须保留"
    assert Dispute.objects.filter(run=run).count() == 0


def test_cancelled_run_is_not_ready(client_a, project_a, corpus, user_a, monkeypatch):
    """取消后不得 ready，且逐篇未完成的论文交回 pending。"""
    from apps.analysis import services as analysis_services
    from apps.common.auth import ActorContext
    from apps.jobs.services import cancel_job
    from apps.llm.schemas import TASK_ASSIGN_TOPICS

    _patch_llm(monkeypatch)
    real_call = analysis_services.call_structured
    state = {"cancelled": False}

    def cancel_then_continue(context, request):
        if request.task_type == TASK_ASSIGN_TOPICS and not state["cancelled"]:
            state["cancelled"] = True
            cancel_job(ActorContext(user=user_a, request_id="req_test"), context.job_id)
        return real_call(context, request)

    monkeypatch.setattr(analysis_services, "call_structured", cancel_then_continue)
    run, _job_id = _run_pipeline(client_a, project_a, expect_ready=False)

    assert run.status != "ready"
    assert run.status == "cancelled", run.error


# ── 6. 人工复核 ───────────────────────────────────────────────
def _make_dispute(run, *, classification="possible_conflict"):
    papers = sorted(str(value) for value in run.run_papers.values_list("paper_id", flat=True))
    return Dispute.objects.create(
        run=run,
        left_paper_id=papers[0],
        right_paper_id=papers[1],
        left_claim={"value": "左侧", "evidence_ids": []},
        right_claim={"value": "右侧", "evidence_ids": []},
        conditions={
            "research_question": "同一问题",
            "left_conditions": "甲条件",
            "right_conditions": "乙条件",
            "comparability_reason": "条件不同",
        },
        classification=classification,
        reason="初步判断",
    )


def test_review_cannot_override_automatic_classification(client_a, project_a, corpus, monkeypatch):
    """人工只能改复核字段，不能覆盖自动 classification。"""
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    dispute = _make_dispute(run, classification="possible_conflict")

    response = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {
            "expected_version": 1,
            "review_status": "confirmed",
            "classification": "complementary",
        },
        format="json",
    )
    # StrictSerializer 必须把未知字段挡在 400
    assert response.status_code == 400, response.json()
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    dispute.refresh_from_db()
    assert dispute.classification == "possible_conflict"
    assert dispute.review_status == "pending"


def test_review_rejected_requires_note(client_a, project_a, corpus, monkeypatch):
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    dispute = _make_dispute(run)

    response = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "rejected"},
        format="json",
    )
    assert response.status_code == 400, response.json()
    dispute.refresh_from_db()
    assert dispute.review_status == "pending"

    ok = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "rejected", "review_note": "证据不足以支持冲突"},
        format="json",
    )
    assert ok.status_code == 200, ok.json()
    dispute.refresh_from_db()
    assert dispute.review_status == "rejected"
    assert dispute.version == 2


def test_review_bumps_analysis_revision_and_uses_optimistic_lock(
    client_a, project_a, corpus, monkeypatch
):
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    dispute = _make_dispute(run)
    before = run.analysis_revision

    first = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "confirmed"},
        format="json",
    )
    assert first.status_code == 200, first.json()
    run.refresh_from_db()
    assert run.analysis_revision == before + 1

    # 用过期版本再改必须 409，并带回当前版本
    stale = client_a.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "pending"},
        format="json",
    )
    assert stale.status_code == 409, stale.json()
    assert stale.json()["error"]["details"]["current_version"] == 2


def test_foreign_dispute_review_is_404(client_a, client_b, project_a, corpus, monkeypatch):
    _patch_llm(monkeypatch)
    run, _job_id = _run_pipeline(client_a, project_a)
    dispute = _make_dispute(run)

    response = client_b.patch(
        f"/api/v1/disputes/{dispute.id}",
        {"expected_version": 1, "review_status": "confirmed"},
        format="json",
    )
    assert response.status_code == 404


# ── 7. 冻结快照缺失不得留下悬空 Run ───────────────────────────
def test_missing_frozen_config_fails_run_instead_of_hanging(client_a, project_a, corpus, monkeypatch):
    """缺冻结配置的 Run 必须收尾成 failed，不能永远停在 processing。

    回归用例：`_frozen_config` 原先在任何 try 之外抛错，异常直接冒到 `jobs.tasks`，
    结果是 **Job.failed 而 Run 停在 processing** —— 前端既看不到完成也看不到原因，
    只能靠人工查库。修复后 Run 与 Job 必须同时落到 failed。
    """
    from apps.analysis import services as analysis_services
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution

    _patch_llm(monkeypatch)
    _run_pipeline(client_a, project_a)  # 先让项目产生过真实产物

    # 走真实受理拿到一个可用的 Job 上下文（含有效租约）
    accepted = client_a.post(
        f"/api/v1/projects/{project_a.id}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert accepted.status_code == 202, accepted.json()
    job_id = accepted.json()["data"]["job_id"]

    # 模拟"由旧版本代码创建的 Run"：快照里缺少后来才加入的两段配置
    legacy = AnalysisRun.objects.create(
        project=project_a,
        corpus_revision=project_a.corpus_revision,
        research_question=project_a.research_question,
        output_language=project_a.output_language,
        config_snapshot={"parsing": {}, "extraction": {}},
    )

    # 用同一个 Job 上下文去驱动这个 legacy Run
    context = claim_execution(job_id)
    analysis_services.execute_run(context, legacy.id)

    legacy.refresh_from_db()
    assert legacy.status == "failed", f"必须收尾为 failed，实际 {legacy.status}"
    assert legacy.status != "processing", "缺冻结配置不得留下悬空 Run"
    assert legacy.error["code"] == "INTERNAL_ERROR"
    assert "冻结配置" in legacy.error["message"]

    job = Job.objects.get(pk=job_id)
    assert job.status == "failed", job.status
    assert job.error["code"] == legacy.error["code"]
