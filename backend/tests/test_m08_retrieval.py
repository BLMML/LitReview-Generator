"""M08 检索与结果持久化测试（docs/mvp/01 第 4.5 节、docs/mvp/05 T11）。

覆盖：

1. 受理前置：Run 必须 ready 且非 stale，否则 409 RUN_NOT_READY / STALE_RUN；
2. 参数边界：query 长度、candidate_limit / top_k 范围与相互关系、未知字段；
3. 幂等：同键重放返回同一 Job 与同一 RetrievalResult，不产生第二条；
4. 执行：查询扩展 → 召回 → 重排 → 发布，Job 与 RetrievalResult 一并落到成功态；
5. **重排不得新增证据**：候选外 ID → 422 INVALID_EVIDENCE，且不发布结果；
6. 结果形状：rank 从 1 连续、score ∈ [0,1]、条数 ≤ top_k、evidence 属于本 Run；
7. 可复现：同输入两次执行得到相同顺序与分数；
8. 完成前 `results` 为 null；成功但无证据时 `results == []`（不是 null，也不是失败）；
9. 越权：他人的 Run / 检索记录一律 404。

模型调用一律用替身，不产生付费调用。真实 PostgreSQL。
"""

from __future__ import annotations

import uuid

import pytest
from tests.conftest import analysis_extract_payload

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
    """索引缓存在进程内，用例之间必须隔离。"""
    index_module.clear_index_cache()
    yield
    index_module.clear_index_cache()


def _extract_payload(ids: list[str]) -> dict:
    return analysis_extract_payload(ids)


class _Result:
    def __init__(self, payload, call_id="mock"):
        self.payload = payload
        self.call_id = call_id


def _all_evidence_ids(run) -> list[str]:
    documents = [
        paper.parsed_document_id
        for paper in run.run_papers.all()
        if paper.parsed_document_id is not None
    ]
    return [
        str(value)
        for value in EvidenceChunk.objects.filter(parsed_document_id__in=documents)
        .order_by("page_number", "ordinal")
        .values_list("id", flat=True)
    ]


def _patch_llm(monkeypatch, *, ranked=None, queries=None, drop_last=False):
    """替身：query_expand 返回给定扩展；rerank 按给定顺序打分。"""
    from apps.llm.schemas import TASK_EXPAND_QUERY, TASK_RERANK
    from apps.retrieval import services as retrieval_services

    def fake_call(context, request):
        if request.task_type == TASK_EXPAND_QUERY:
            expanded = (
                queries
                if queries is not None
                else [request.payload["current_query"], "attention baseline"]
            )
            return _Result({"queries": expanded}, call_id="mock:expand")
        if request.task_type == TASK_RERANK:
            allowed = list(request.allowed_evidence_ids)
            chosen = allowed if ranked is None else [item for item in ranked if item in allowed]
            if drop_last and chosen:
                chosen = chosen[:-1]
            payload = {
                "results": [
                    {
                        "evidence_id": evidence_id,
                        "score": round(1.0 - position * 0.1, 4),
                        "reason": f"第 {position + 1} 位相关",
                    }
                    for position, evidence_id in enumerate(chosen)
                ]
            }
            return _Result(payload, call_id="mock:rerank")
        raise AssertionError(f"未预期的任务类型：{request.task_type}")

    monkeypatch.setattr(retrieval_services, "call_structured", fake_call)


def _create_retrieval(client, run_id, *, query="attention 方法是否有效", key=None, **extra):
    body = {"query": query, **extra}
    return client.post(
        f"/api/v1/runs/{run_id}/retrievals",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def _execute(job_id):
    from apps.jobs.services import claim_execution
    from apps.retrieval import services as retrieval_services

    context = claim_execution(job_id)
    retrieval_services.execute_retrieval(context, context_resource_id(context))
    return RetrievalResult.objects.get(pk=context_resource_id(context))


def context_resource_id(context):
    from apps.jobs.models import Job

    return Job.objects.get(pk=context.job_id).resource_id


# ── 1. 受理前置 ───────────────────────────────────────────────
def test_retrieval_requires_ready_run(client_a, ready_run):
    """Run 未 ready 时拒绝开新检索。

    这里把 `ready_run` 的状态位改回 failed，模拟 M07 流水线刚结束时的真实状态：
    产物齐全但后续阶段未实现，此时**不允许**开始检索。
    """
    ready_run.status = "failed"
    ready_run.error = {
        "code": "INTERNAL_ERROR",
        "message": "分析流水线的后续阶段尚未实现",
        "details": {},
        "retryable": False,
    }
    ready_run.save(update_fields=["status", "error", "updated_at"])

    response = _create_retrieval(client_a, ready_run.id)
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "RUN_NOT_READY"
    assert not RetrievalResult.objects.exists()


def test_stale_run_blocks_new_retrieval(client_a, project_a, ready_run):
    """语料变化后旧 Run 可读，但**新检索**返回 409 STALE_RUN。"""
    from apps.jobs.models import Job

    Job.objects.filter(status__in=("queued", "running", "cancelling")).update(status="failed")
    detail = client_a.get(f"/api/v1/runs/{ready_run.id}")
    assert detail.status_code == 200, "旧 Run 仍可读"

    patched = client_a.patch(
        f"/api/v1/projects/{project_a.id}",
        {"expected_version": 1, "research_question": "换一个研究问题以制造 stale 语料。"},
        format="json",
    )
    assert patched.status_code == 200, patched.json()

    response = _create_retrieval(client_a, ready_run.id)
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "STALE_RUN"
    assert not RetrievalResult.objects.exists()


def test_retrieval_requires_idempotency_key(client_a, ready_run):
    response = client_a.post(
        f"/api/v1/runs/{ready_run.id}/retrievals", {"query": "attention"}, format="json"
    )
    assert response.status_code == 400


# ── 2. 参数边界 ───────────────────────────────────────────────
@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"query": "x"}, "query 少于 2 字符"),
        ({"query": "好" * 2001}, "query 超过 2000 字符"),
        ({"query": "attention", "candidate_limit": 4}, "candidate_limit 小于 5"),
        ({"query": "attention", "candidate_limit": 51}, "candidate_limit 大于 50"),
        ({"query": "attention", "top_k": 0}, "top_k 小于 1"),
        ({"query": "attention", "top_k": 11}, "top_k 大于 10"),
        ({"query": "attention", "candidate_limit": 5, "top_k": 6}, "top_k 超过 candidate_limit"),
        ({"query": "attention", "unknown": 1}, "未知字段"),
    ],
)
def test_retrieval_parameter_validation(client_a, ready_run, body, why):
    response = client_a.post(
        f"/api/v1/runs/{ready_run.id}/retrievals",
        body,
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 400, f"{why}：{response.json()}"
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not RetrievalResult.objects.exists()


def test_retrieval_defaults_are_applied(client_a, ready_run):
    response = _create_retrieval(client_a, ready_run.id)
    assert response.status_code == 202, response.json()
    retrieval = RetrievalResult.objects.get()
    assert retrieval.candidate_limit == 20
    assert retrieval.top_k == 5
    assert retrieval.analysis_revision == ready_run.analysis_revision


# ── 3. 幂等 ───────────────────────────────────────────────────
def test_same_key_replays_without_second_retrieval(client_a, ready_run):
    key = str(uuid.uuid4())
    first = _create_retrieval(client_a, ready_run.id, key=key)
    second = _create_retrieval(client_a, ready_run.id, key=key)

    assert first.status_code == 202, first.json()
    assert second.status_code == 202, second.json()
    assert first.json()["data"] == second.json()["data"]
    assert RetrievalResult.objects.count() == 1, "重放不得产生第二条检索记录"


# ── 4. 执行 ───────────────────────────────────────────────────
def test_retrieval_publishes_ranked_results(client_a, ready_run, monkeypatch):
    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]

    # 完成前必须是 null：前端据此区分"还没跑"与"跑完但无结果"
    pending = RetrievalResult.objects.get(pk=accepted["resource_id"])
    assert pending.expanded_queries is None
    assert pending.results is None

    retrieval = _execute(accepted["job_id"])

    assert retrieval.expanded_queries, "必须记录实际使用的扩展查询"
    assert retrieval.results is not None and retrieval.results, "应召回并重排出结果"
    assert retrieval.candidates, "必须保留内部候选供追溯"

    from apps.jobs.models import Job

    job = Job.objects.get(pk=accepted["job_id"])
    assert job.status == "succeeded", job.error
    assert job.result["result_count"] == len(retrieval.results)


def test_result_shape_and_ordering(client_a, ready_run, monkeypatch):
    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id, candidate_limit=8, top_k=3).json()["data"]
    retrieval = _execute(accepted["job_id"])

    assert len(retrieval.results) <= 3, "结果条数不得超过 top_k"
    assert [item["rank"] for item in retrieval.results] == list(
        range(1, len(retrieval.results) + 1)
    ), "rank 必须从 1 连续"
    for item in retrieval.results:
        assert 0 <= item["score"] <= 1, "score 是 0—1 相关性排序分"
        assert item["reason"], "每条结果必须给出理由"

    allowed = set(_all_evidence_ids(ready_run))
    for item in retrieval.results:
        assert item["evidence_id"] in allowed, "结果证据必须属于本 Run"


def test_rerank_cannot_introduce_new_evidence(client_a, ready_run, monkeypatch):
    """重排返回候选之外的 ID → 422 INVALID_EVIDENCE，且不得发布任何结果。"""
    from apps.llm.schemas import TASK_EXPAND_QUERY
    from apps.retrieval import services as retrieval_services

    stranger = str(uuid.uuid4())

    def fake_call(context, request):
        if request.task_type == TASK_EXPAND_QUERY:
            return _Result({"queries": [request.payload["current_query"]]}, call_id="mock:expand")
        # 绕过网关的白名单校验，直接模拟"模型返回了候选外的 ID"
        return _Result(
            {"results": [{"evidence_id": stranger, "score": 0.9, "reason": "伪造"}]},
            call_id="mock:rerank",
        )

    monkeypatch.setattr(retrieval_services, "call_structured", fake_call)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]

    with pytest.raises(ApiError) as error:
        _execute(accepted["job_id"])
    assert error.value.code == "INVALID_EVIDENCE"

    retrieval = RetrievalResult.objects.get(pk=accepted["resource_id"])
    assert retrieval.results is None, "越界重排不得发布结果"


def test_rerank_validation_rejects_duplicates_and_out_of_range_scores():
    """直接校验 `validate_ranked_evidence` 与分数范围。"""
    from apps.retrieval.services import validate_ranked_evidence

    known = {str(uuid.uuid4()), str(uuid.uuid4())}
    ids = list(known)

    ok = validate_ranked_evidence(
        [{"evidence_id": ids[0], "score": 0.5, "reason": "r"}], known
    )
    assert len(ok) == 1

    with pytest.raises(ApiError) as error:
        validate_ranked_evidence(
            [{"evidence_id": str(uuid.uuid4()), "score": 0.5, "reason": "r"}], known
        )
    assert error.value.code == "INVALID_EVIDENCE"

    # 缺 evidence_id 的条目同样越界
    with pytest.raises(ApiError) as error:
        validate_ranked_evidence([{"score": 0.5, "reason": "r"}], known)
    assert error.value.code == "INVALID_EVIDENCE"


def test_invalid_score_is_rejected(client_a, ready_run, monkeypatch):
    """score 超出 0—1 视为模型输出不合法，不得入库。"""
    from apps.llm.schemas import TASK_EXPAND_QUERY
    from apps.retrieval import services as retrieval_services

    def fake_call(context, request):
        if request.task_type == TASK_EXPAND_QUERY:
            return _Result({"queries": [request.payload["current_query"]]}, call_id="mock:expand")
        allowed = list(request.allowed_evidence_ids)
        return _Result(
            {"results": [{"evidence_id": allowed[0], "score": 7, "reason": "越界分数"}]},
            call_id="mock:rerank",
        )

    monkeypatch.setattr(retrieval_services, "call_structured", fake_call)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]

    with pytest.raises(ApiError) as error:
        _execute(accepted["job_id"])
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert RetrievalResult.objects.get(pk=accepted["resource_id"]).results is None


def test_empty_results_is_success_not_failure(client_a, ready_run, monkeypatch):
    """成功但无证据时 results == []，且 Job 成功——不能当成失败。"""
    _patch_llm(monkeypatch, queries=["zzzz-nonexistent-term-qqqq"])
    accepted = _create_retrieval(client_a, ready_run.id, query="zzzz nonexistent qqqq").json()[
        "data"
    ]
    retrieval = _execute(accepted["job_id"])

    assert retrieval.results == [], "无证据必须是空列表"
    assert retrieval.candidates == []
    from apps.jobs.models import Job

    assert Job.objects.get(pk=accepted["job_id"]).status == "succeeded"


# ── 5. 可复现 ─────────────────────────────────────────────────
def test_same_input_reproduces_same_order(client_a, ready_run, monkeypatch):
    """同一输入两次执行必须给出相同的证据顺序与分数。"""
    _patch_llm(monkeypatch)
    first_accepted = _create_retrieval(client_a, ready_run.id, query="attention 方法").json()["data"]
    first = _execute(first_accepted["job_id"])

    index_module.clear_index_cache()  # 强制重建索引，排除缓存偶然性
    second_accepted = _create_retrieval(client_a, ready_run.id, query="attention 方法").json()["data"]
    second = _execute(second_accepted["job_id"])

    assert [item["evidence_id"] for item in first.results] == [
        item["evidence_id"] for item in second.results
    ], "同输入必须复现同一顺序"
    assert [item["score"] for item in first.results] == [
        item["score"] for item in second.results
    ]
    assert first.expanded_queries == second.expanded_queries


def test_candidate_ordering_is_stable(ready_run):
    """`recall` 自身在同一输入下多次调用顺序一致。"""
    snapshot = _snapshot_for(ready_run)
    reference = index_module.build_index(snapshot)
    first = index_module.recall(reference, ["attention method"], {"candidate_limit": 10})
    second = index_module.recall(reference, ["attention method"], {"candidate_limit": 10})

    assert [(item.evidence_id, item.recall_score) for item in first] == [
        (item.evidence_id, item.recall_score) for item in second
    ]
    scores = [item.recall_score for item in first]
    assert scores == sorted(scores, reverse=True), "召回必须按分数降序"


def test_index_cache_key_includes_revision_and_version(ready_run):
    """索引缓存键必须含 Run、analysis_revision 与索引配置版本。"""
    snapshot = _snapshot_for(ready_run)
    reference = index_module.build_index(snapshot)
    assert str(ready_run.id) in reference.cache_key
    assert str(ready_run.analysis_revision) in reference.cache_key
    assert index_module.INDEX_CONFIG_VERSION in reference.cache_key
    assert reference.manifest["chunk_count"] > 0


def _snapshot_for(run):
    from apps.retrieval.services import run_snapshot_for

    return run_snapshot_for(run)


# ── 6. 查询扩展 ───────────────────────────────────────────────
def test_expansion_keeps_original_query_first(client_a, ready_run, monkeypatch):
    """扩展列表第一项必须是原查询；去重且不超过上限。"""
    _patch_llm(
        monkeypatch,
        queries=["attention 方法", "attention 方法", "attention mechanism", "", "attention baseline"],
    )
    accepted = _create_retrieval(client_a, ready_run.id, query="attention 方法").json()["data"]
    retrieval = _execute(accepted["job_id"])

    assert retrieval.expanded_queries[0] == "attention 方法", "第一项必须保留原查询"
    assert len(retrieval.expanded_queries) == len(set(retrieval.expanded_queries)), "不得重复"
    assert "" not in retrieval.expanded_queries
    assert len(retrieval.expanded_queries) <= 5


def test_expansion_failure_falls_back_to_original(client_a, ready_run, monkeypatch):
    """扩展结构不合法时保守退回原查询，而不是让整次检索失败。"""
    from apps.llm.schemas import TASK_EXPAND_QUERY
    from apps.retrieval import services as retrieval_services

    def fake_call(context, request):
        if request.task_type == TASK_EXPAND_QUERY:
            return _Result({"queries": "not-a-list"}, call_id="mock:expand")
        return _Result({"results": []}, call_id="mock:rerank")

    monkeypatch.setattr(retrieval_services, "call_structured", fake_call)
    accepted = _create_retrieval(client_a, ready_run.id, query="attention").json()["data"]
    retrieval = _execute(accepted["job_id"])

    assert retrieval.expanded_queries == ["attention"], "退回原查询"


# ── 7. 接口与越权 ─────────────────────────────────────────────
def test_retrieval_detail_is_readable(client_a, ready_run, monkeypatch):
    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]
    _execute(accepted["job_id"])

    response = client_a.get(f"/api/v1/retrievals/{accepted['resource_id']}")
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["query"]
    assert data["job_id"] == accepted["job_id"]
    assert data["results"] is not None
    assert {"evidence_id", "rank", "score", "reason"} <= set(data["results"][0])


def test_history_listing_returns_own_retrievals(client_a, ready_run, monkeypatch):
    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]
    _execute(accepted["job_id"])

    response = client_a.get(f"/api/v1/runs/{ready_run.id}/retrievals")
    assert response.status_code == 200, response.json()
    items = response.json()["data"]
    assert [item["id"] for item in items] == [accepted["resource_id"]]


def test_foreign_retrieval_is_404(client_a, client_b, ready_run, monkeypatch):
    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]

    assert client_b.get(f"/api/v1/retrievals/{accepted['resource_id']}").status_code == 404
    assert client_b.get(f"/api/v1/runs/{ready_run.id}/retrievals").status_code == 404
    assert (
        client_b.post(
            f"/api/v1/runs/{ready_run.id}/retrievals",
            {"query": "attention"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        ).status_code
        == 404
    )


# ── 8. 内部同步入口 ───────────────────────────────────────────
def test_retrieve_evidence_is_usable_without_a_public_job(client_a, ready_run, monkeypatch):
    """M10 生成阶段复用它：只依赖 Run 快照，不创建公开 Job。"""
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.retrieval import services as retrieval_services

    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]
    context = claim_execution(accepted["job_id"])
    jobs_before = Job.objects.count()

    ranked, expanded, candidates = retrieval_services.retrieve_evidence(
        context,
        _snapshot_for(ready_run),
        "attention 方法",
        {"candidate_limit": 10, "top_k": 3},
    )

    assert Job.objects.count() == jobs_before, "内部入口不得创建新 Job"
    assert ranked and expanded and candidates
    assert all(item["rank"] == index + 1 for index, item in enumerate(ranked))


def test_expired_lease_cannot_publish(client_a, ready_run, monkeypatch):
    """租约过期后不得写入检索结果。"""
    from datetime import timedelta

    from django.utils import timezone

    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution
    from apps.retrieval import services as retrieval_services

    _patch_llm(monkeypatch)
    accepted = _create_retrieval(client_a, ready_run.id).json()["data"]
    context = claim_execution(accepted["job_id"])
    Job.objects.filter(pk=context.job_id).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    with pytest.raises(ApiError):
        retrieval_services.execute_retrieval(context, accepted["resource_id"])

    assert RetrievalResult.objects.get(pk=accepted["resource_id"]).results is None
