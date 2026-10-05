"""M10 综述生成与编辑保存测试（docs/mvp/01 第 4.6 节、docs/mvp/04 第 5.1 / 5.2 节）。

覆盖 M10 的两条链路：

- **生成**：`freeze_inputs → retrieve_sections → generate_sections → check_structure →
  commit_revision`，五节齐全才一次性提交 revision 1；
- **编辑保存**：锁 Project 与 Review、比较 base_revision_number、全量校验后插入 N+1，
  新版本继承 base 的 analysis_revision。

以及结构校验的硬性限制：节序、每节至少一个 block、全稿 ≤100 block、每块 1—600 字符、
每块 ≤20 引用、block_id 全文唯一、claim 必须有本 Run 证据、禁止 HTML 与自带文献编号。

核验与导出属 M11，相关断言已在 `test_review_export.py` 与 `test_m11_reviews.py` 打开。

模型调用一律用替身，不产生付费调用。真实 PostgreSQL。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.common.http import ApiError
from apps.jobs.models import Job
from apps.jobs.services import claim_execution
from apps.llm.schemas import TASK_REVIEW_SECTION
from apps.parsing.models import EvidenceChunk
from apps.reviews.models import Review, ReviewRevision
from apps.reviews.schemas import SectionsValidationError, validate_sections
from apps.reviews.structure import SECTION_KEYS

pytestmark = pytest.mark.django_db(transaction=True)

# 综述生成的检索用**冻结研究问题**做查询，因此夹具语料必须与查询同语言、且可被
# 文本层编码。`make_text_pdf_file` 用 latin-1 写内容流，中文会整体变成 `?`——
# 那样词表里只剩文件名，召回必然落空（第一版夹具就踩了这个坑）。
# 这里统一用英文语料 + 英文研究问题，与 M07/M08 的语料口径一致。
REVIEW_PAGES = [
    "Abstract\nSelf-attention improves sequence modelling on small corpora.",
    "Introduction\nRecurrent baselines are compared with attention models on small samples.",
    "3. Method\nWe replace the recurrent layer with a multi-head attention block.",
    "4. Results\nAttention improves small-sample accuracy by two points on the benchmark.",
    "5. Limitations\nThe evaluation covers a single language and dataset.",
]

REVIEW_QUESTION = "Does attention improve sequence modelling accuracy on small-sample benchmarks?"


# ── 已就绪的 Run ──────────────────────────────────────────────
# 用本文件的夹具而不是 conftest 的 `run_ready`：后者建立在测试事务内，
# 而生成链要在**事务外**做模型调用（`llm.client` 显式拒绝事务内的网络调用）。
# 本文件的用例都需要真实提交，因此统一 `transaction=True`。
@pytest.fixture
def run_ready(client_a, project_a, monkeypatch):
    """含两篇已解析、已抽取论文的 `ready` Run。

    刻意**不跑整条 M07/M09 流水线**：M10 需要的只是它产出的那套状态
    （解析产物 → 证据片段、RunPaper.ready、PaperAnalysis、Topic）。
    直接构造这些产物既更快，也避免夹具自身的模型调用把测试事务搅进来。
    """
    import uuid as _uuid

    from tests.conftest import analysis_extract_payload, make_text_pdf_file

    from apps.analysis.models import (
        RUN_PAPER_READY,
        RUN_READY,
        AnalysisRun,
        PaperAnalysis,
        RunPaper,
        Topic,
    )
    from apps.parsing.models import EvidenceChunk
    from apps.parsing.services import parse_paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    papers = []
    for name in ("alpha", "beta"):
        reply = client_a.post(
            f"/api/v1/projects/{project_a.pk}/papers/upload",
            {"file": make_text_pdf_file([f"{name} {page}" for page in REVIEW_PAGES], name=f"{name}.pdf")},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(_uuid.uuid4()),
        )
        assert reply.status_code == 201, reply.json()
        papers.append(reply.json()["data"])

    accepted = client_a.post(
        f"/api/v1/projects/{project_a.pk}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(_uuid.uuid4()),
    ).json()["data"]
    run = AnalysisRun.objects.get(pk=accepted["resource_id"])

    # 研究问题改为与语料同语言：生成链用它做检索查询，语言不一致时召回在
    # 关键词层面就无从命中（那是语料限制，不是实现缺陷，但会让夹具测不出东西）。
    AnalysisRun.objects.filter(pk=run.pk).update(research_question=REVIEW_QUESTION)
    run.refresh_from_db()
    claimed = claim_execution(accepted["job_id"])

    for run_paper in run.run_papers.order_by("created_at", "id"):
        document = parse_paper(claimed, run_paper, run.config_snapshot["parsing"])
        RunPaper.objects.filter(pk=run_paper.pk).update(parsed_document=document, status=RUN_PAPER_READY)
        evidence_ids = [
            str(value)
            for value in EvidenceChunk.objects.filter(parsed_document=document)
            .order_by("page_number", "ordinal")
            .values_list("id", flat=True)
        ]
        assert evidence_ids, "夹具必须产出真实证据片段"
        PaperAnalysis.objects.create(run_paper=run_paper, payload=analysis_extract_payload(evidence_ids))

    Topic.objects.create(
        run=run,
        name="注意力机制",
        description="夹具主题。",
        paper_ids=[str(paper["id"]) for paper in papers],
        evidence_ids=[],
    )
    AnalysisRun.objects.filter(pk=run.pk).update(status=RUN_READY, error=None)

    # 夹具只是"借"这个 Job 的执行资格来跑解析；产物构造完后必须把它收尾，
    # 否则项目一直显示有活跃任务，受理综述会被 PROJECT_BUSY 挡住。
    Job.objects.filter(pk=accepted["job_id"]).update(status="succeeded", lease_expires_at=None)
    run.refresh_from_db()
    assert run.status == "ready"
    return run


# ── 替身与工具 ────────────────────────────────────────────────
def _section_payload(section_key: str, evidence_ids: list[str], *, blocks: int = 2) -> dict:
    """构造单节合法输出：block_id 由后端分配，这里刻意**不**提供。

    每个 block 分到**不同**的证据 ID：M04 网关会拒绝同一请求内的重复引用
    （重复引用既无意义，也让"哪条证据支持哪句"无法追溯）。
    """
    assert len(evidence_ids) >= blocks, "证据包必须够每块分到一条不同证据"
    return {
        "section_key": section_key,
        "title": f"{section_key} 标题",
        "blocks": [
            {
                "kind": "claim",
                "text": EvidenceChunk.objects.get(pk=evidence_ids[index]).text[:250].strip(),
                "evidence_ids": [evidence_ids[index]],
            }
            for index in range(blocks)
        ],
    }


def _patch_sections(monkeypatch, *, calls=None, override=None):
    """替身：综述逐节生成 + 其内部检索链路的重排。

    选证据要经过 `retrieve_evidence`（M08），而它内部会调用重排模型。
    两处都必须替身：只替身综述那一层时，检索会拿 mock 夹具的占位 UUID
    去撞 M04 网关的白名单，失败点看起来像"综述生成坏了"，其实是检索没被替身。
    """
    from apps.llm.schemas import TASK_EXPAND_QUERY, TASK_RERANK
    from apps.retrieval import services as retrieval_services
    from apps.reviews import services as review_services

    def retrieval_call(_context, request):
        if request.task_type == TASK_EXPAND_QUERY:
            return SimpleNamespace(
                payload={"queries": [request.payload["current_query"]]}, call_id="mock:expand"
            )
        if request.task_type == TASK_RERANK:
            allowed = list(request.allowed_evidence_ids)
            return SimpleNamespace(
                payload={
                    "results": [
                        {
                            "evidence_id": evidence_id,
                            "score": round(1.0 - position * 0.05, 4),
                            "reason": "与本节问题相关",
                        }
                        for position, evidence_id in enumerate(allowed[:10])
                    ]
                },
                call_id="mock:rerank",
            )
        raise AssertionError(f"检索替身收到意外任务：{request.task_type}")

    monkeypatch.setattr(retrieval_services, "call_structured", retrieval_call)

    def fake_call(context, request):
        assert request.task_type == TASK_REVIEW_SECTION
        if calls is not None:
            calls.append(request)
        allowed = list(request.allowed_evidence_ids)
        assert allowed, "每节都必须收到非空证据包"
        if override is not None:
            payload = override(request, allowed)
        else:
            payload = _section_payload(request.payload["section_key"], allowed)
        return SimpleNamespace(payload=payload, call_id=f"mock:{request.payload['section_key']}")

    monkeypatch.setattr(review_services, "call_structured", fake_call)


def _create_review(client, run, *, key=None, **extra):
    return client.post(
        f"/api/v1/runs/{run.id}/reviews",
        {"title": "注意力机制综述", **extra},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def _generate(client, review_id, job_id):
    from apps.reviews import services as review_services

    context = claim_execution(job_id)
    revision = review_services.generate_review(context, review_id)
    return Review.objects.get(pk=review_id), revision


@pytest.fixture
def generated(client_a, run_ready, monkeypatch):
    """已生成 revision 1 的 Review。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    _patch_sections(monkeypatch)
    accepted = _create_review(client_a, run_ready).json()["data"]
    review, revision = _generate(client_a, accepted["resource_id"], accepted["job_id"])
    return SimpleNamespace(run=run_ready, review=review, revision=revision, job_id=accepted["job_id"])


# ── 1. 受理 ───────────────────────────────────────────────────
def test_create_review_accepts_and_returns_202(client_a, run_ready, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    response = _create_review(client_a, run_ready, target_chars=2500)

    assert response.status_code == 202, response.json()
    data = response.json()["data"]
    assert data["resource_type"] == "review"
    review = Review.objects.get(pk=data["resource_id"])
    assert review.target_chars == 2500
    assert review.analysis_revision == run_ready.analysis_revision
    assert review.config_snapshot["template_version"], "必须冻结生成模板版本"
    assert not review.revisions.exists(), "首次受理不得创建空白 revision 0"


def test_create_review_requires_run_ready(client_a, run_ready, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    run_ready.status = "failed"
    run_ready.save(update_fields=["status", "updated_at"])

    response = _create_review(client_a, run_ready)
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "RUN_NOT_READY"
    assert not Review.objects.exists()


def test_create_review_same_key_replays(client_a, run_ready, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    key = str(uuid.uuid4())
    first = _create_review(client_a, run_ready, key=key)
    second = _create_review(client_a, run_ready, key=key)

    assert first.status_code == second.status_code == 202
    assert first.json()["data"] == second.json()["data"]
    assert Review.objects.count() == 1


def test_create_review_rejects_unknown_fields(client_a, run_ready, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    response = client_a.post(
        f"/api/v1/runs/{run_ready.id}/reviews",
        {"title": "x", "unknown": 1},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 400, response.json()


# ── 2. 生成 ───────────────────────────────────────────────────
def test_generation_produces_five_ordered_sections(client_a, run_ready, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    calls: list = []
    _patch_sections(monkeypatch, calls=calls)
    accepted = _create_review(client_a, run_ready).json()["data"]

    review, revision = _generate(client_a, accepted["resource_id"], accepted["job_id"])

    assert revision.revision_number == 1
    assert revision.source == "generated"
    assert [section["section_key"] for section in revision.sections] == list(SECTION_KEYS)
    assert revision.content_hash
    assert revision.analysis_revision == review.analysis_revision
    assert len(calls) == len(SECTION_KEYS), "每节应各调用一次模型"
    assert [call.payload["section_key"] for call in calls] == list(SECTION_KEYS)

    job = Job.objects.get(pk=accepted["job_id"])
    assert job.status == "succeeded", job.error
    assert job.result["revision_number"] == 1


def test_block_ids_are_assigned_by_backend(client_a, run_ready, monkeypatch):
    """block_id 由后端分配：模型给出的 ID 不被采纳，且全稿唯一。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)

    def with_fake_ids(request, allowed):
        payload = _section_payload(request.payload["section_key"], allowed)
        # 模型试图自带（并且是同一个）block_id
        for block in payload["blocks"]:
            block["block_id"] = "11111111-1111-4111-8111-111111111111"
        return payload

    _patch_sections(monkeypatch, override=with_fake_ids)
    accepted = _create_review(client_a, run_ready).json()["data"]
    _review, revision = _generate(client_a, accepted["resource_id"], accepted["job_id"])

    ids = [block["block_id"] for section in revision.sections for block in section["blocks"]]
    assert len(ids) == len(set(ids)), "block_id 必须全稿唯一"
    assert "11111111-1111-4111-8111-111111111111" not in ids, "不得采纳模型给出的 block_id"


def test_generation_fails_when_a_section_is_invalid(client_a, run_ready, monkeypatch):
    """任一节结构不合法 → 不提交任何正文，Job 失败。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)

    def broken(request, allowed):
        if request.payload["section_key"] != "theoretical_basis":
            return _section_payload(request.payload["section_key"], allowed)
        # claim 不带证据：结构校验必须拒绝
        return {
            "section_key": "theoretical_basis",
            "title": "争议",
            "blocks": [{"kind": "claim", "text": "没有证据的论断", "evidence_ids": []}],
        }

    _patch_sections(monkeypatch, override=broken)
    accepted = _create_review(client_a, run_ready).json()["data"]

    from apps.reviews import services as review_services

    context = claim_execution(accepted["job_id"])
    with pytest.raises((SectionsValidationError, ApiError)):
        review_services.generate_review(context, accepted["resource_id"])

    assert not ReviewRevision.objects.exists(), "失败时不得留下部分正文"


def test_generation_uses_frozen_template_version(client_a, run_ready, monkeypatch):
    """冻结模板与当前模板不一致时显式失败，不用新模板冒充旧配置。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    _patch_sections(monkeypatch)
    accepted = _create_review(client_a, run_ready).json()["data"]

    review = Review.objects.get(pk=accepted["resource_id"])
    snapshot = dict(review.config_snapshot)
    snapshot["template_version"] = "deadbeef00000000"
    Review.objects.filter(pk=review.pk).update(config_snapshot=snapshot)

    from apps.reviews import services as review_services

    context = claim_execution(accepted["job_id"])
    with pytest.raises(ApiError) as error:
        review_services.generate_review(context, accepted["resource_id"])
    assert error.value.code == "INTERNAL_ERROR"
    assert not ReviewRevision.objects.exists()


def test_generation_is_idempotent_for_same_job(client_a, run_ready, monkeypatch):
    """同一 Job 再执行一次不得产生第二个版本。"""
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    _patch_sections(monkeypatch)
    accepted = _create_review(client_a, run_ready).json()["data"]

    from apps.reviews import services as review_services

    context = claim_execution(accepted["job_id"])
    first = review_services.generate_review(context, accepted["resource_id"])
    second = review_services.generate_review(context, accepted["resource_id"])

    assert first.pk == second.pk
    assert ReviewRevision.objects.filter(review_id=accepted["resource_id"]).count() == 1


# ── 3. 结构校验 ───────────────────────────────────────────────
def _scope(*, papers: int = 1):
    evidence = {f"paper-{index}": {str(uuid.uuid4())} for index in range(papers)}
    return SimpleNamespace(
        allowed_paper_ids=set(evidence),
        evidence_ids_by_paper=evidence,
        max_topics=20,
        max_disputes=50,
    )


def _sections(scope, *, keys=None, blocks_per_section=1, evidence: str | None = None):
    keys = keys or SECTION_KEYS
    eid = evidence or next(iter(next(iter(scope.evidence_ids_by_paper.values()))))
    return [
        {
            "section_key": key,
            "title": f"{key} 标题",
            "blocks": [
                {
                    "block_id": str(uuid.uuid4()),
                    "kind": "claim",
                    "text": f"{key} 的论断 {index}",
                    "evidence_ids": [eid],
                }
                for index in range(blocks_per_section)
            ],
        }
        for key in keys
    ]


def test_sections_must_have_five_ordered_keys():
    scope = _scope()
    with pytest.raises(SectionsValidationError):
        validate_sections(_sections(scope)[:4], run_scope=scope)

    reordered = list(reversed(_sections(scope)))
    with pytest.raises(SectionsValidationError):
        validate_sections(reordered, run_scope=scope)


def test_each_section_needs_at_least_one_block():
    scope = _scope()
    sections = _sections(scope)
    sections[2]["blocks"] = []
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


def test_block_limits_are_enforced():
    scope = _scope()
    sections = _sections(scope)

    # 单块超过 600 字符
    too_long = [dict(block) for block in sections]
    too_long[0] = {**sections[0], "blocks": [{**sections[0]["blocks"][0], "text": "字" * 601}]}
    with pytest.raises(SectionsValidationError):
        validate_sections(too_long, run_scope=scope)

    # 单块超过 20 条引用
    eid = sections[0]["blocks"][0]["evidence_ids"][0]
    too_many = [dict(block) for block in sections]
    too_many[0] = {
        **sections[0],
        "blocks": [{**sections[0]["blocks"][0], "evidence_ids": [eid] * 21}],
    }
    with pytest.raises(SectionsValidationError):
        validate_sections(too_many, run_scope=scope)


def test_block_ids_must_be_unique_across_review():
    scope = _scope()
    sections = _sections(scope, blocks_per_section=2)
    shared = sections[0]["blocks"][0]["block_id"]
    sections[1]["blocks"][0]["block_id"] = shared
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


def test_claim_requires_evidence_from_current_run():
    scope = _scope()
    sections = _sections(scope)

    no_evidence = [dict(item) for item in sections]
    no_evidence[0] = {
        **sections[0],
        "blocks": [{**sections[0]["blocks"][0], "evidence_ids": []}],
    }
    with pytest.raises(SectionsValidationError):
        validate_sections(no_evidence, run_scope=scope)

    stranger = [dict(item) for item in sections]
    stranger[0] = {
        **sections[0],
        "blocks": [{**sections[0]["blocks"][0], "evidence_ids": [str(uuid.uuid4())]}],
    }
    with pytest.raises(SectionsValidationError):
        validate_sections(stranger, run_scope=scope)


def test_transition_may_have_no_evidence():
    """transition 是覆盖限制说明，允许没有引用。"""
    scope = _scope()
    sections = _sections(scope)
    sections[3] = {
        **sections[3],
        "blocks": [
            {"block_id": str(uuid.uuid4()), "kind": "transition", "text": "证据不足。", "evidence_ids": []}
        ],
    }
    validated = validate_sections(sections, run_scope=scope)
    assert validated[3]["blocks"][0]["kind"] == "transition"


def test_html_is_rejected_in_block_text():
    scope = _scope()
    sections = _sections(scope)
    sections[0] = {
        **sections[0],
        "blocks": [{**sections[0]["blocks"][0], "text": "<b>加粗</b>的论断"}],
    }
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


def test_model_supplied_citation_numbers_are_rejected():
    """正文自带文献编号会与后端生成的编号冲突。"""
    scope = _scope()
    from apps.reviews.models import SECTION_KEYS as LEGACY_KEYS

    sections = _sections(scope, keys=LEGACY_KEYS)
    sections[0] = {
        **sections[0],
        "blocks": [{**sections[0]["blocks"][0], "text": "已有研究证明了这一点[1]。"}],
    }
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


# ── 4. 编辑保存 ───────────────────────────────────────────────
def _manual_payload(revision):
    """基于现有版本构造一份全量正文（改一处文字）。"""
    sections = [
        {
            "section_key": section["section_key"],
            "title": section["title"],
            "blocks": [dict(block) for block in section["blocks"]],
        }
        for section in revision.sections
    ]
    sections[0]["blocks"] = sections[0]["blocks"][:1]
    return sections


def test_manual_save_creates_next_revision(client_a, generated):
    response = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/revisions",
        {"base_revision_number": 1, "sections": _manual_payload(generated.revision)},
        format="json",
    )
    assert response.status_code == 201, response.json()
    body = response.json()["data"]
    assert body["revision_number"] == 2
    assert body["source"] == "manual"

    latest = generated.review.revisions.order_by("-revision_number").first()
    assert latest.revision_number == 2
    assert latest.base_revision_id == generated.revision.id


def test_save_revision_conflict_is_409(client_a, generated):
    response = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/revisions",
        {"base_revision_number": 99, "sections": _manual_payload(generated.revision)},
        format="json",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert response.json()["error"]["details"]["current_revision_number"] == 1
    assert generated.review.revisions.count() == 1


def test_new_revision_inherits_analysis_revision(client_a, generated):
    """人工改字不消除分析过时状态。"""
    from apps.analysis.models import AnalysisRun

    AnalysisRun.objects.filter(pk=generated.run.pk).update(
        analysis_revision=generated.run.analysis_revision + 1
    )

    response = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/revisions",
        {"base_revision_number": 1, "sections": _manual_payload(generated.revision)},
        format="json",
    )
    assert response.status_code == 201, response.json()
    latest = generated.review.revisions.order_by("-revision_number").first()
    assert latest.analysis_revision == generated.revision.analysis_revision, (
        "新版本必须继承 base 的 analysis_revision"
    )

    detail = client_a.get(f"/api/v1/reviews/{generated.review.id}").json()["data"]
    assert detail["latest_revision"]["is_stale"] is True


def test_manual_save_rejects_invalid_sections(client_a, generated):
    payload = _manual_payload(generated.revision)
    payload[0]["blocks"][0]["evidence_ids"] = []
    response = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/revisions",
        {"base_revision_number": 1, "sections": payload},
        format="json",
    )
    assert response.status_code == 422, response.json()
    assert response.json()["error"]["code"] == "STRUCTURAL_VALIDATION_FAILED"
    assert generated.review.revisions.count() == 1


# ── 5. HTTP 契约 ──────────────────────────────────────────────
def _assert_dto(name: str, payload: dict) -> None:
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    contract = json.loads(
        (Path(__file__).resolve().parent.parent.parent / "docs/mvp/contracts/openapi.json").read_text("utf-8")
    )
    validator = Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": contract["components"]}
    )
    errors = list(validator.iter_errors(payload))
    assert not errors, " | ".join(f"{list(error.path)}: {error.message}" for error in errors)


def test_review_dto_matches_frozen_openapi(client_a, generated):
    detail = client_a.get(f"/api/v1/reviews/{generated.review.id}")
    assert detail.status_code == 200, detail.json()
    _assert_dto("Review", detail.json()["data"])


def test_revision_dto_matches_frozen_openapi(client_a, generated):
    detail = client_a.get(f"/api/v1/reviews/{generated.review.id}")
    revision = detail.json()["data"]["latest_revision"]
    assert revision is not None
    _assert_dto("ReviewRevision", revision)


def test_review_before_generation_has_null_latest_revision(client_a, run_ready, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    accepted = _create_review(client_a, run_ready).json()["data"]

    detail = client_a.get(f"/api/v1/reviews/{accepted['resource_id']}")
    assert detail.status_code == 200, detail.json()
    body = detail.json()["data"]
    assert body["latest_revision"] is None, "生成未完成时不得展示空白正文"
    assert body["generation_job_id"] == accepted["job_id"]
    _assert_dto("Review", body)


def test_foreign_review_is_404(client_a, client_b, generated):
    assert client_b.get(f"/api/v1/reviews/{generated.review.id}").status_code == 404
    assert (
        client_b.post(
            f"/api/v1/reviews/{generated.review.id}/revisions",
            {"base_revision_number": 1, "sections": _manual_payload(generated.revision)},
            format="json",
        ).status_code
        == 404
    )
