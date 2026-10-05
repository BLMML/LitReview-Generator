"""M07–M09 独立复核问题的回归测试。

对应 `docs/mvp/M07_M09_复核问题清单_2026-10-01.md` 的 10 类问题。
这些用例原本是外部独立检查（21 项），现纳入项目正式测试以守住修复。

覆盖：
1. 有成功单篇产物时的分析重试（反向 OneToOne 关系，不存在 `analysis_id` 字段）；
2. 阶段发布必须锁 Job 行：取消不能在"校验通过"与"写入"之间提交；
3. 失败 / 取消收尾必须校验写入资格，且 Run 与 Job 不得分裂终态；
4. 争议复核要求 Run ready、非 stale、项目空闲；
5. 各阶段使用自己的**冻结**配置；索引缓存身份含完整有效配置；
6. 主题覆盖完整性：成员并集 ∪ 未归类 = 纳入集合；
7. 索引发布事务回滚后不得留下未登记文件；
8. 部署改配置后，同键同请求仍能重放；
9. 单篇分析与检索的 HTTP 响应符合冻结 OpenAPI；
10. 重试不得重复追加已提交的争议候选。

并发相关结论依赖真实 PostgreSQL 行锁，不能用 SQLite 顶替。
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from tests.conftest import analysis_extract_payload, make_text_pdf_file

from apps.analysis import services
from apps.analysis.models import AnalysisRun, Dispute, PaperAnalysis, RunPaper, Topic
from apps.common.auth import ActorContext
from apps.common.http import ApiError
from apps.common.models import FileAsset
from apps.common.storage import storage_root
from apps.jobs.models import Job
from apps.jobs.services import cancel_job, claim_execution, retry_job
from apps.llm.schemas import TASK_ASSIGN_TOPICS, TASK_COMPARE_DISPUTES, TASK_PAPER_EXTRACT
from apps.parsing.services import parse_paper
from apps.retrieval import index

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def bundle(client_a, project_a, user_a, monkeypatch):
    """两篇文献 + 已受理的 Run + 已认领的执行上下文。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    index.clear_index_cache()
    for name in ("alpha", "beta"):
        reply = client_a.post(
            f"/api/v1/projects/{project_a.pk}/papers/upload",
            {"file": make_text_pdf_file([f"{name} attention accuracy method benchmark."])},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert reply.status_code == 201, reply.json()
    key = str(uuid.uuid4())
    reply = client_a.post(
        f"/api/v1/projects/{project_a.pk}/runs", {}, format="json", HTTP_IDEMPOTENCY_KEY=key
    )
    assert reply.status_code == 202, reply.json()
    data = reply.json()["data"]
    run = AnalysisRun.objects.get(pk=data["resource_id"])
    context = claim_execution(data["job_id"])
    actor = ActorContext(user_a, "req_m0709_regression")
    return SimpleNamespace(
        run=run, ctx=context, actor=actor, key=key, client=client_a, project=project_a
    )


def _patch_model(monkeypatch, *, fail_topics=False, omit_paper=False, requests=None):
    """替身：单篇抽取固定合法；主题按参数构造；争议默认无候选。"""

    def call(context, request):
        if requests is not None:
            requests.append(request)
        if request.task_type == TASK_PAPER_EXTRACT:
            payload = analysis_extract_payload(list(request.allowed_evidence_ids))
        elif request.task_type == TASK_ASSIGN_TOPICS:
            if fail_topics:
                from apps.common.http import api_error

                raise api_error("MODEL_TIMEOUT", "注入的主题阶段失败", retryable=True)
            ids = list(request.allowed_paper_ids)
            payload = {
                "topics": [
                    {
                        "name": "attention",
                        "description": "",
                        "paper_ids": ids[:1] if omit_paper else ids,
                        "evidence_ids": [],
                    }
                ],
                # omit_paper 时既不归类也不声明未归类 —— 覆盖检查必须拦住
                "unassigned_paper_ids": [] if not omit_paper else [],
            }
        else:
            assert request.task_type == TASK_COMPARE_DISPUTES
            payload = {"disputes": []}
        return SimpleNamespace(payload=payload, call_id=uuid.uuid4())

    monkeypatch.setattr(services, "call_structured", call)


def _attach_pages(bundle):
    rows = list(
        bundle.run.run_papers.select_related("file_asset", "run").order_by("created_at", "id")
    )
    for row in rows:
        row.parsed_document = parse_paper(
            bundle.ctx, row, bundle.run.config_snapshot["parsing"]
        )
        row.save(update_fields=["parsed_document"])
    return rows


def _execute(bundle):
    result = services.execute_run(bundle.ctx, bundle.run.pk)
    result.refresh_from_db()
    return result


# ── 1. 重试：反向关系，不存在 analysis_id ─────────────────────
def test_retry_keeps_ready_papers_and_can_finish(bundle, monkeypatch):
    """主题阶段失败后重试：已成功的单篇产物原样保留，只补未完成阶段。

    `RunPaper` 与 `PaperAnalysis` 是反向 OneToOne，**没有 `analysis_id` 字段**；
    直接取它会抛 AttributeError，把最需要复用结果的重试路径打死。
    """
    _patch_model(monkeypatch, fail_topics=True)
    first = _execute(bundle)
    assert first.status == "failed"
    originals = list(PaperAnalysis.objects.values_list("pk", flat=True))
    assert len(originals) == 2

    accepted = retry_job(bundle.actor, bundle.ctx.job_id, uuid.uuid4())
    bundle.ctx = claim_execution(accepted.job_id)
    _patch_model(monkeypatch)

    second = _execute(bundle)
    assert second.status == "ready", second.error
    assert list(PaperAnalysis.objects.values_list("pk", flat=True)) == originals, (
        "重试不得重新生成已完整的单篇产物"
    )


# ── 2. 阶段发布必须持 Job 行锁 ────────────────────────────────
def test_cancellation_cannot_commit_between_lease_check_and_analysis_insert(bundle, monkeypatch):
    """取消不能在"资�格校验通过"与"写入产物"之间提交。

    只校验不加锁时，另一个连接可以在窗口内提交取消，而本次写入照样落库。
    这里在真实 PostgreSQL 上用第二条连接复现该窗口。
    """
    from django.db import connection, connections

    assert connection.vendor == "postgresql", "该结论依赖真实行锁语义"
    paper = _attach_pages(bundle)[0]
    initial = services._locked_job
    cancel_finished = threading.Event()
    checked_windows: list[bool] = []

    def cancel_other_connection():
        try:
            cancel_job(bundle.actor, bundle.ctx.job_id)
        finally:
            connections.close_all()
            cancel_finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def checked(context, run):
            current = initial(context, run)
            pool.submit(cancel_other_connection)
            # 正确的行锁应当让取消**阻塞**在事务外；这里等一小段时间观察它是否先提交
            checked_windows.append(cancel_finished.wait(0.3))
            return current

        monkeypatch.setattr(services, "_locked_job", checked)
        try:
            services._save_analysis(
                bundle.ctx,
                bundle.run,
                paper,
                analysis_extract_payload(list(services._evidence_pack(paper)["ids"])),
                [str(uuid.uuid4())],
                bundle.run.config_snapshot,
            )
        except ApiError as exc:
            assert exc.code == "INVALID_JOB_STATE"

    assert not checked_windows[0] or not PaperAnalysis.objects.filter(run_paper=paper).exists(), (
        "取消已先于阶段写入提交，阶段仍发布了 PaperAnalysis"
    )


# ── 3. 旧尝试不得覆写状态；终态不得分裂 ───────────────────────
@pytest.mark.parametrize("operation", ["release", "paper-failed", "run-failed", "run-cancelled"])
def test_old_attempt_cannot_overwrite_new_attempt_state(bundle, operation):
    """已被取代的旧尝试不得改动论文状态或 Run 终态。"""
    paper = bundle.run.run_papers.first()
    paper.status = "processing"
    paper.save(update_fields=["status"])
    terminal = operation in ("run-failed", "run-cancelled")
    current_run_status = "ready" if terminal else "processing"
    # 模拟"恢复扫描已接管 / 新尝试已开始"：token 前进且 Job 已终态
    Job.objects.filter(pk=bundle.ctx.job_id).update(
        status="succeeded" if terminal else "running",
        lease_token=bundle.ctx.lease_token + 1,
    )
    AnalysisRun.objects.filter(pk=bundle.run.pk).update(status=current_run_status)

    if operation == "release":
        services._release_processing_paper(bundle.ctx, paper)
    elif operation == "paper-failed":
        services._mark_paper_failed(bundle.ctx, paper, {"code": "MODEL_TIMEOUT"})
    elif operation == "run-cancelled":
        services._finish_cancelled(bundle.ctx, bundle.run)
    else:
        try:
            services._finish_stage_failed(
                bundle.ctx,
                bundle.run,
                {"code": "MODEL_TIMEOUT", "message": "old", "details": {}, "retryable": True},
            )
        except ApiError:
            pass

    paper.refresh_from_db()
    bundle.run.refresh_from_db()
    assert (paper.status, bundle.run.status) == ("processing", current_run_status)


def test_cancelled_job_and_failed_run_are_not_split_terminal_states(bundle):
    """Job 已取消时，Run 必须也是 cancelled —— 不得出现 Job.cancelled / Run.failed。"""
    cancel_job(bundle.actor, bundle.ctx.job_id)
    services._finish_stage_failed(
        bundle.ctx,
        bundle.run,
        {"code": "MODEL_TIMEOUT", "message": "failed", "details": {}, "retryable": True},
    )
    bundle.run.refresh_from_db()
    assert (bundle.run.status, Job.objects.get(pk=bundle.ctx.job_id).status) == (
        "cancelled",
        "cancelled",
    )


# ── 4. 争议复核的前置约束 ─────────────────────────────────────
@pytest.mark.parametrize("invalid", ["not-ready", "stale", "busy"])
def test_dispute_patch_requires_ready_nonstale_idle_run(bundle, monkeypatch, invalid):
    """未 ready / 已过时 / 项目忙时，复核必须 409 且不改任何记录。"""
    _patch_model(monkeypatch)
    run = _execute(bundle)
    papers = list(run.run_papers.all())
    dispute = Dispute.objects.create(
        run=run,
        left_paper=papers[0].paper,
        right_paper=papers[1].paper,
        left_claim={},
        right_claim={},
        conditions={},
        classification="possible_conflict",
        reason="test",
    )
    if invalid == "not-ready":
        AnalysisRun.objects.filter(pk=run.pk).update(status="failed")
        expected = "RESULT_NOT_READY"
    elif invalid == "stale":
        from apps.projects.models import Project

        Project.objects.filter(pk=bundle.project.pk).update(
            corpus_revision=run.corpus_revision + 1
        )
        expected = "STALE_RUN"
    else:
        Job.objects.create(
            project=bundle.project,
            requested_by=bundle.actor.user,
            kind="retrieve",
            resource_type="retrieval_result",
            resource_id=uuid.uuid4(),
            input_hash="0" * 64,
        )
        expected = "PROJECT_BUSY"

    response = bundle.client.patch(
        f"/api/v1/disputes/{dispute.pk}",
        {"expected_version": 1, "review_status": "confirmed"},
        format="json",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == expected
    dispute.refresh_from_db()
    assert dispute.version == 1 and dispute.review_status == "pending"


# ── 5. 冻结配置必须真的生效 ───────────────────────────────────
def test_frozen_index_config_controls_output(bundle, settings):
    """冻结的 `index.max_features` 必须控制实际构建，而不是读当前全局默认值。"""
    _attach_pages(bundle)
    frozen = copy.deepcopy(bundle.run.config_snapshot)
    assert frozen["index"]["max_features"] > 1
    settings.RETRIEVAL_INDEX_MAX_FEATURES = 1

    reference = services._stage_index(bundle.ctx, bundle.run, frozen)
    assert reference.manifest["index_config"]["max_features"] == frozen["index"]["max_features"]
    assert len(reference.engine.vocabulary) > 1


def test_index_cache_identity_includes_effective_config(bundle, settings):
    """缓存身份必须含**完整有效配置**：只含版本号时改参数会命中旧缓存。"""
    _attach_pages(bundle)
    document_ids = list(
        bundle.run.run_papers.values_list("parsed_document_id", flat=True)
    )
    snap = SimpleNamespace(
        run_id=bundle.run.pk,
        analysis_revision=1,
        corpus_revision=bundle.run.corpus_revision,
        parsed_document_ids=document_ids,
    )
    first = index.build_index(snap)
    settings.RETRIEVAL_INDEX_MAX_FEATURES = 1
    second = index.build_index(snap)

    assert second.manifest["index_config"]["max_features"] == 1
    assert second.cache_key != first.cache_key


def test_topics_uses_own_frozen_model_config(bundle, monkeypatch):
    """主题归纳使用自己的冻结模型配置，不得顺手拿 extraction 的那份。"""
    for row in _attach_pages(bundle):
        row.status = "ready"
        row.save(update_fields=["status"])
        PaperAnalysis.objects.create(
            run_paper=row, payload=analysis_extract_payload(services._evidence_pack(row)["ids"])
        )
    config = copy.deepcopy(bundle.run.config_snapshot)
    config["topics"]["model_config"]["temperature"] = 0.75
    requests: list = []
    _patch_model(monkeypatch, requests=requests)

    services._stage_topics(bundle.ctx, bundle.run, config)
    assert requests[0].model_config["temperature"] == 0.75


def test_dispute_member_limit_uses_frozen_config(bundle, monkeypatch, settings):
    """每主题成员上限取自冻结配置；改全局默认值不得改变历史 Run 的比较范围。"""
    rows = _attach_pages(bundle)
    config = copy.deepcopy(bundle.run.config_snapshot)
    topic = Topic.objects.create(
        run=bundle.run,
        name="same",
        description="",
        paper_ids=[str(row.paper_id) for row in rows],
        evidence_ids=[],
    )
    config["disputes"]["max_papers_per_topic"] = 2
    settings.DISPUTES_MAX_PAPERS_PER_TOPIC = 1
    calls: list = []
    _patch_model(monkeypatch, requests=calls)

    services._stage_disputes(bundle.ctx, bundle.run, [topic], config)
    assert len(calls) == 1 and len(calls[0].allowed_paper_ids) == 2


# ── 6. 主题覆盖完整性 ─────────────────────────────────────────
def test_ready_run_does_not_silently_omit_paper_coverage(bundle, monkeypatch):
    """Run.ready 时，不得有论文既未归类也未显式列为未归类。"""
    _patch_model(monkeypatch, omit_paper=True)
    run = _execute(bundle)
    if run.status != "ready":
        return
    included = {str(row.paper_id) for row in run.run_papers.all()}
    assigned = {str(pk) for topic in run.topics.all() for pk in topic.paper_ids}
    unassigned = set(run.config_snapshot.get("unassigned_paper_ids", []))
    assert included == assigned | unassigned, "Run.ready 却有论文凭空消失"


# ── 7. 索引回滚清理 ───────────────────────────────────────────
def test_index_publication_rollback_removes_committed_file(bundle, monkeypatch):
    """索引产物已 move 到最终位置但登记回滚时，文件必须一并清理。"""
    _attach_pages(bundle)
    before = {p for p in storage_root().rglob("*") if p.is_file()}
    initial = AnalysisRun.objects.filter

    def query(**kwargs):
        queryset = initial(**kwargs)
        if kwargs.get("pk") == bundle.run.pk:
            update = queryset.update

            def fail(**values):
                if "index_asset" in values:
                    raise RuntimeError("注入：Run 索引挂接失败")
                return update(**values)

            queryset.update = fail
        return queryset

    monkeypatch.setattr(AnalysisRun.objects, "filter", query)
    with pytest.raises(RuntimeError):
        services._stage_index(bundle.ctx, bundle.run, bundle.run.config_snapshot)

    assert not FileAsset.objects.filter(purpose=FileAsset.PURPOSE_INDEX).exists()
    after = {p for p in storage_root().rglob("*") if p.is_file()}
    assert after == before, "索引事务回滚后仍有未登记文件"


# ── 8. 幂等重放不受部署配置影响 ───────────────────────────────
def test_same_run_request_replays_after_deployment_config_change(bundle, settings):
    """部署改了索引配置后，同一客户端请求仍须重放原受理结果。

    请求指纹若包含服务端生成的 `config_snapshot`，改配置就会被误判成
    "同键不同请求体"，返回 409 IDEMPOTENCY_CONFLICT。
    """
    settings.RETRIEVAL_INDEX_MAX_FEATURES += 1
    response = bundle.client.post(
        f"/api/v1/projects/{bundle.project.pk}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=bundle.key,
    )
    assert response.status_code == 202, response.json()
    assert response.json()["data"]["resource_id"] == str(bundle.run.pk)


# ── 9. HTTP 响应必须符合冻结 OpenAPI ──────────────────────────
def _assert_dto(name: str, payload: dict) -> None:
    contract = json.loads(
        (
            Path(__file__).resolve().parent.parent.parent
            / "docs/mvp/contracts/openapi.json"
        ).read_text("utf-8")
    )
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": contract["components"]}
    )
    errors = list(validator.iter_errors(payload))
    assert not errors, " | ".join(
        f"{list(error.path)}: {error.message}" for error in errors
    )


def test_single_analysis_http_response_matches_frozen_openapi(bundle, monkeypatch):
    """单篇分析必须把八个字段**展平**，且不得有额外字段。"""
    _patch_model(monkeypatch)
    run = _execute(bundle)
    paper = run.run_papers.first()
    reply = bundle.client.get(f"/api/v1/runs/{run.pk}/papers/{paper.paper_id}/analysis")
    assert reply.status_code == 200, reply.json()
    _assert_dto("PaperAnalysis", reply.json()["data"])


def test_retrieval_http_response_matches_frozen_openapi(bundle, monkeypatch):
    """检索 DTO 与结果项都必须符合契约，不得透传内部字段。"""
    from apps.llm.schemas import TASK_EXPAND_QUERY
    from apps.retrieval import services as retrieval_services

    _patch_model(monkeypatch)
    run = _execute(bundle)
    reply = bundle.client.post(
        f"/api/v1/runs/{run.pk}/retrievals",
        {"query": "attention"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert reply.status_code == 202, reply.json()
    accepted = reply.json()["data"]

    def model(context, request):
        payload = (
            {"queries": ["attention"]}
            if request.task_type == TASK_EXPAND_QUERY
            else {
                "results": [
                    {
                        "evidence_id": request.allowed_evidence_ids[0],
                        "score": 0.7,
                        "reason": "match",
                    }
                ]
            }
        )
        return SimpleNamespace(payload=payload, call_id=uuid.uuid4())

    monkeypatch.setattr(retrieval_services, "call_structured", model)
    retrieval_services.execute_retrieval(
        claim_execution(accepted["job_id"]), accepted["resource_id"]
    )

    reply = bundle.client.get(f"/api/v1/retrievals/{accepted['resource_id']}")
    assert reply.status_code == 200, reply.json()
    assert reply.json()["data"]["results"], "本次检索应当有结果"
    _assert_dto("Retrieval", reply.json()["data"])


# ── 10. 重试不得重复追加争议 ──────────────────────────────────
def test_retry_does_not_duplicate_completed_dispute_candidates(bundle, monkeypatch):
    """争议阶段已提交后中断再重试，同一逻辑候选不得变成两条。"""
    _patch_model(monkeypatch)
    basic = services.call_structured

    def model(context, request):
        if request.task_type != TASK_COMPARE_DISPUTES:
            return basic(context, request)
        scope = services._run_scope(bundle.run)
        left, right = request.allowed_paper_ids[:2]
        evidence = scope.evidence_ids_by_paper
        payload = {
            "disputes": [
                {
                    "left_paper_id": left,
                    "right_paper_id": right,
                    "left_claim": {
                        "value": "left result",
                        "evidence_ids": [next(iter(evidence[left]))],
                    },
                    "right_claim": {
                        "value": "right result",
                        "evidence_ids": [next(iter(evidence[right]))],
                    },
                    "conditions": {
                        "research_question": "same",
                        "left_conditions": "reported",
                        "right_conditions": "reported",
                        "comparability_reason": "comparable",
                    },
                    "classification": "possible_conflict",
                    "reason": "candidate comparison",
                }
            ]
        }
        return SimpleNamespace(payload=payload, call_id=uuid.uuid4())

    monkeypatch.setattr(services, "call_structured", model)
    disputes_stage = services._stage_disputes
    pending = [True]

    def save_then_interrupt(*args, **kwargs):
        result = disputes_stage(*args, **kwargs)
        if pending[0]:
            # 争议已提交，随后中断：模拟"阶段完成但整体失败"
            pending[0] = False
            from apps.common.http import api_error

            raise api_error("MODEL_TIMEOUT", "注入：争议提交后中断", retryable=True)
        return result

    monkeypatch.setattr(services, "_stage_disputes", save_then_interrupt)

    first = _execute(bundle)
    assert first.status == "failed" and Dispute.objects.filter(run=first).count() == 1

    accepted = retry_job(bundle.actor, bundle.ctx.job_id, uuid.uuid4())
    bundle.ctx = claim_execution(accepted.job_id)
    second = _execute(bundle)

    assert second.status == "ready", second.error
    assert Dispute.objects.filter(run=second).count() == 1, "重试重复追加了同一争议候选"


# ══════════════════════════════════════════════════════════════
# 第二轮复验补充：TOCTOU、配置透传、提交失败、产物保留
# 对应 docs/mvp/M07_M09_第二轮复验_2026-10-01.md
# ══════════════════════════════════════════════════════════════


# ── 11. 接管发生在"检查之后、写入之前" ────────────────────────
@pytest.mark.parametrize("operation", ["release", "paper-failed", "run-cancelled"])
def test_takeover_between_cleanup_check_and_write_cannot_be_overwritten(
    bundle, monkeypatch, operation
):
    """接管发生在检查之后、写入之前时，旧 worker 仍不得改动当前状态。

    上一轮只覆盖了"调用前就失效"的情形。真正危险的是 TOCTOU：
    检查通过 → 另一个连接提交新 lease_token → 旧 worker 继续写入。
    因此判定必须在**写入所在的那个事务**里、持 Job 行锁完成。
    """
    from django.db import connection, connections

    assert connection.vendor == "postgresql", "该结论依赖真实行锁语义"
    paper = bundle.run.run_papers.first()
    RunPaper.objects.filter(pk=paper.pk).update(status="processing")
    paper.status = "processing"
    if operation == "run-cancelled":
        cancel_job(bundle.actor, bundle.ctx.job_id)

    initial = services._attempt_is_current
    finished = threading.Event()
    windows: list[bool] = []

    def takeover():
        try:
            Job.objects.filter(pk=bundle.ctx.job_id).update(
                lease_token=bundle.ctx.lease_token + 1
            )
            AnalysisRun.objects.filter(pk=bundle.run.pk).update(status="processing")
            RunPaper.objects.filter(pk=paper.pk).update(status="processing")
        finally:
            connections.close_all()
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def checked(context, run):
            current = initial(context, run)
            pool.submit(takeover)
            # 正确实现下，接管会被行锁挡在事务外，因此这里等不到它先提交
            windows.append(finished.wait(0.3))
            return current

        monkeypatch.setattr(services, "_attempt_is_current", checked)
        if operation == "release":
            services._release_processing_paper(bundle.ctx, paper)
        elif operation == "paper-failed":
            services._mark_paper_failed(bundle.ctx, paper, {"code": "MODEL_TIMEOUT"})
        else:
            services._finish_cancelled(bundle.ctx, bundle.run)

    paper.refresh_from_db()
    bundle.run.refresh_from_db()
    assert not windows or not windows[0] or (paper.status, bundle.run.status) == (
        "processing",
        "processing",
    ), "新租约已先提交，旧 worker 仍修改当前状态"


def test_old_worker_cannot_mark_current_ready_paper_processing(bundle):
    """持旧 token 的 worker 不得把已 ready 的论文改回 processing。

    单篇入口原先把 `status` 改成 processing **之后**才由解析服务校验租约，
    于是状态已经被改坏；现在改为先锁 Job、校验通过再改。
    """
    paper = bundle.run.run_papers.first()
    Job.objects.filter(pk=bundle.ctx.job_id).update(
        lease_token=bundle.ctx.lease_token + 1
    )
    RunPaper.objects.filter(pk=paper.pk).update(status="ready")

    with pytest.raises(ApiError) as caught:
        services._execute_run_paper(
            bundle.ctx, bundle.run, paper, bundle.run.config_snapshot
        )
    assert caught.value.code == "INVALID_JOB_STATE"

    paper.refresh_from_db()
    assert paper.status == "ready", "执行入口在租约检查之前修改了论文状态"


# ── 12. 冻结配置必须贯穿正式调用链 ────────────────────────────
def test_full_pipeline_preserves_frozen_index_config(bundle, monkeypatch, settings):
    """**正式 execute_run** 必须把冻结的索引配置传到 index 阶段。

    只让底层 `build_index(config=...)` 支持冻结参数是不够的：
    `_frozen_config` 漏掉 index 段时，阶段收到的是"没有 index 的配置"，
    于是静默回退到当前默认值——测试直接调 `_stage_index` 时看不出来。
    """
    _patch_model(monkeypatch)
    frozen = copy.deepcopy(bundle.run.config_snapshot["index"])
    settings.RETRIEVAL_INDEX_MAX_FEATURES = 1

    run = _execute(bundle)
    assert run.status == "ready", run.error
    assert run.index_manifest["index_config"]["max_features"] == frozen["max_features"]


def test_retrieval_resolution_preserves_run_index_config(bundle, monkeypatch, settings):
    """检索加载 / 重建索引时必须透传该 Run 冻结的配置。

    不透传时，部署改配置会让历史 Run 的产物被判为不一致并按新参数重建，
    历史检索输出随部署漂移。
    """
    from apps.retrieval import index as index_module
    from apps.retrieval.services import _resolve_index, run_snapshot_for

    _patch_model(monkeypatch)
    run = _execute(bundle)
    assert run.status == "ready", run.error
    frozen = copy.deepcopy(run.config_snapshot["index"])

    index_module.clear_index_cache()
    settings.RETRIEVAL_INDEX_MAX_FEATURES = 1
    reference = _resolve_index(run_snapshot_for(run))
    assert reference.manifest["index_config"]["max_features"] == frozen["max_features"]


def test_query_vector_uses_same_config_as_frozen_index(bundle, settings):
    """查询向量化必须用索引自己的 ngram 参数，而不是当前 settings。

    同一份固定索引、同一问题，仅改全局 ngram_max 就让召回分数变化，
    等于相同输入产生不同输出。
    """
    from apps.retrieval import index as index_module
    from apps.retrieval.services import run_snapshot_for

    _attach_pages(bundle)
    frozen = copy.deepcopy(bundle.run.config_snapshot["index"])
    frozen["ngram_max"] = 2
    settings.RETRIEVAL_INDEX_NGRAM_MAX = 2
    reference = index_module.build_index(run_snapshot_for(bundle.run), config=frozen)

    first = index_module.recall(reference, ["attention accuracy"], {"candidate_limit": 10})
    assert first, "本次查询应当有召回结果"
    settings.RETRIEVAL_INDEX_NGRAM_MAX = 1
    second = index_module.recall(reference, ["attention accuracy"], {"candidate_limit": 10})

    assert [(str(item.evidence_id), item.recall_score) for item in second] == [
        (str(item.evidence_id), item.recall_score) for item in first
    ], "索引与输入都没变，召回分数却随全局配置漂移"


# ── 13. 主题 / 争议数量上限必须用冻结值 ───────────────────────
@pytest.mark.parametrize("change", ["global-lower", "global-higher"])
def test_topics_schema_limit_is_frozen(bundle, monkeypatch, settings, change):
    """主题数量上限取自冻结配置：全局调小不得判坏合法输出，调大不得放行越限输出。"""
    from apps.analysis.models import PaperAnalysis
    from apps.analysis.schemas import SchemaValidationError

    _attach_pages(bundle)
    config = copy.deepcopy(bundle.run.config_snapshot)
    config["topics"]["max_topics"] = 2 if change == "global-lower" else 1
    bundle.run.config_snapshot = config
    bundle.run.save(update_fields=["config_snapshot"])
    settings.TOPICS_MAX = 1 if change == "global-lower" else 2

    paper_ids = [
        str(pk) for pk in bundle.run.run_papers.values_list("paper_id", flat=True)
    ]

    def call(context, request):
        assert request.task_type == TASK_ASSIGN_TOPICS
        return SimpleNamespace(
            payload={
                "topics": [
                    {
                        "name": f"topic-{position}",
                        "description": "",
                        "paper_ids": [paper_id],
                        "evidence_ids": [],
                    }
                    for position, paper_id in enumerate(paper_ids)
                ],
                "unassigned_paper_ids": [],
            },
            call_id=uuid.uuid4(),
        )

    monkeypatch.setattr(services, "call_structured", call)
    for row in bundle.run.run_papers.all():
        RunPaper.objects.filter(pk=row.pk).update(status="ready")
        PaperAnalysis.objects.create(
            run_paper=row, payload=analysis_extract_payload(services._evidence_pack(row)["ids"])
        )

    if change == "global-lower":
        # 冻结允许 2 个主题，全局降到 1 —— 两个主题都必须被接受
        assert len(services._stage_topics(bundle.ctx, bundle.run, config)) == 2
    else:
        # 冻结只允许 1 个主题，全局升到 2 —— 两个主题必须被拒绝
        with pytest.raises(SchemaValidationError):
            services._stage_topics(bundle.ctx, bundle.run, config)


# ── 14. COMMIT 失败也必须清理索引孤儿 ─────────────────────────
def test_index_commit_failure_removes_moved_file(bundle, monkeypatch):
    """事务 COMMIT 本身失败时，已 move 的索引文件同样不得留下。

    `with transaction.atomic()` 退出只代表"准备提交"；提前放弃清理责任会让
    提交阶段故障变成无人清理的孤儿文件。
    """
    from django.db import DatabaseError, connection

    _attach_pages(bundle)
    before = {p for p in storage_root().rglob("*") if p.is_file()}
    original = connection.commit
    state = {"failed": False}

    def fail_once():
        if not state["failed"]:
            state["failed"] = True
            raise DatabaseError("注入：索引事务提交失败")
        return original()

    with monkeypatch.context() as patch:
        patch.setattr(connection, "commit", fail_once)
        with pytest.raises(DatabaseError):
            services._stage_index(bundle.ctx, bundle.run, bundle.run.config_snapshot)

    assert not FileAsset.objects.filter(purpose=FileAsset.PURPOSE_INDEX).exists()
    after = {p for p in storage_root().rglob("*") if p.is_file()}
    assert after == before, "COMMIT 失败回滚后留下未登记索引"


# ── 15. 重试再次失败必须保留已提交的争议阶段 ───────────────────
def test_retry_failure_retains_previously_committed_dispute_stage(bundle, monkeypatch):
    """争议阶段已完整提交后重试又失败，原有候选必须原样保留。

    原实现在阶段开头先删旧候选、随后逐主题追加：第一个主题提交后第二个主题失败，
    旧集合已被删除，于是"重试失败"把先前完整提交的成功产物弄丢。
    现在改为事务外算完全部候选、再一次性替换。
    """
    _patch_model(monkeypatch)
    basic = services.call_structured

    def candidate(context, request):
        if request.task_type != TASK_COMPARE_DISPUTES:
            return basic(context, request)
        left, right = request.allowed_paper_ids[:2]
        evidence = services._run_scope(bundle.run).evidence_ids_by_paper
        return SimpleNamespace(
            payload={
                "disputes": [
                    {
                        "left_paper_id": left,
                        "right_paper_id": right,
                        "left_claim": {
                            "value": "left",
                            "evidence_ids": [next(iter(evidence[left]))],
                        },
                        "right_claim": {
                            "value": "right",
                            "evidence_ids": [next(iter(evidence[right]))],
                        },
                        "conditions": {
                            "research_question": "same",
                            "left_conditions": "reported",
                            "right_conditions": "reported",
                            "comparability_reason": "comparable",
                        },
                        "classification": "possible_conflict",
                        "reason": "candidate",
                    }
                ]
            },
            call_id=uuid.uuid4(),
        )

    monkeypatch.setattr(services, "call_structured", candidate)
    original_stage = services._stage_disputes
    interrupted = [False]

    def interrupt_after_commit(*args, **kwargs):
        result = original_stage(*args, **kwargs)
        if not interrupted[0]:
            interrupted[0] = True
            from apps.common.http import api_error

            raise api_error("MODEL_TIMEOUT", "注入：争议阶段完成后中断", retryable=True)
        return result

    monkeypatch.setattr(services, "_stage_disputes", interrupt_after_commit)

    first = _execute(bundle)
    assert first.status == "failed"
    original_ids = set(Dispute.objects.filter(run=first).values_list("pk", flat=True))
    assert len(original_ids) == 1

    accepted = retry_job(bundle.actor, bundle.ctx.job_id, uuid.uuid4())
    bundle.ctx = claim_execution(accepted.job_id)

    def fail_disputes(context, request):
        if request.task_type == TASK_COMPARE_DISPUTES:
            from apps.common.http import api_error

            raise api_error("MODEL_TIMEOUT", "注入：重试时模型超时", retryable=True)
        return basic(context, request)

    monkeypatch.setattr(services, "call_structured", fail_disputes)
    second = _execute(bundle)

    current_ids = set(Dispute.objects.filter(run=second).values_list("pk", flat=True))
    if second.status == "failed":
        assert current_ids == original_ids, "重试失败导致已完整提交的争议候选丢失"
    else:
        assert len(current_ids) == 1
