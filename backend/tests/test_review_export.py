"""综述生成、编辑、核验与导出测试（docs/mvp/01 第 9 节）。

**必须覆盖的行为**：
- 五节结构与顺序、每节至少一个 block（M10，已打开）；
- 论断必须有证据（claim 至少一个当前 Run 证据）（M10，已打开）；
- 跨 Run 引用拒绝（M10，已打开）；
- 编辑版本冲突（base_revision_number 不匹配返回 409 VERSION_CONFLICT）（M10，已打开）；
- 一份报告只绑定一个 revision，旧报告不能用于新正文（M11）；
- 无匹配且成功的报告时禁止导出（M11）；
- needs_review、分析过时或非最新正文导出必须 acknowledge_warnings=true（M11）；
- 参考文献只来源于冻结元数据（M11）。

**M10 的结构与保存断言已在 `tests/test_m10_reviews.py` 完整覆盖**（25 项）。
本文件保留契约层面的对应检查，并覆盖"跨 Run 引用"这一 M10 边界；
核验与导出的 7 条契约断言已在 M11 打开。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from tests import test_m11_reviews as m11

from apps.common.http import ApiError
from apps.jobs.services import claim_execution
from apps.llm.schemas import TASK_EXPAND_QUERY, TASK_REVIEW_SECTION
from apps.reviews.models import Review, ReviewRevision
from apps.reviews.schemas import SectionsValidationError, validate_sections
from apps.reviews.structure import SECTION_KEYS

generated = m11.generated
validated = m11.validated

pytestmark = pytest.mark.django_db(transaction=True)

REVIEW_PAGES = [
    "Abstract\nSelf-attention improves sequence modelling on small corpora.",
    "Introduction\nRecurrent baselines are compared with attention models on small samples.",
    "3. Method\nWe replace the recurrent layer with a multi-head attention block.",
    "4. Results\nAttention improves small-sample accuracy by two points on the benchmark.",
]

REVIEW_QUESTION = "Does attention improve sequence modelling accuracy on small-sample benchmarks?"


@pytest.fixture
def run_ready(client_a, project_a, monkeypatch):
    """生成链需要的 ready Run（产物直接构造，不跑整条流水线）。"""
    from tests.conftest import analysis_extract_payload, make_text_pdf_file

    from apps.analysis.models import (
        RUN_PAPER_READY,
        RUN_READY,
        AnalysisRun,
        PaperAnalysis,
        RunPaper,
        Topic,
    )
    from apps.jobs.models import Job
    from apps.parsing.models import EvidenceChunk
    from apps.parsing.services import parse_paper

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    papers = []
    for name in ("alpha", "beta"):
        reply = client_a.post(
            f"/api/v1/projects/{project_a.pk}/papers/upload",
            {"file": make_text_pdf_file([f"{name} {page}" for page in REVIEW_PAGES], name=f"{name}.pdf")},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert reply.status_code == 201, reply.json()
        papers.append(reply.json()["data"])

    accepted = client_a.post(
        f"/api/v1/projects/{project_a.pk}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    ).json()["data"]
    run = AnalysisRun.objects.get(pk=accepted["resource_id"])
    # 检索用冻结研究问题做查询，语料与查询必须同语言且可被文本层编码
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
        PaperAnalysis.objects.create(run_paper=run_paper, payload=analysis_extract_payload(evidence_ids))

    Topic.objects.create(
        run=run,
        name="attention",
        description="fixture topic",
        paper_ids=[str(paper["id"]) for paper in papers],
        evidence_ids=[],
    )
    AnalysisRun.objects.filter(pk=run.pk).update(status=RUN_READY, error=None)
    Job.objects.filter(pk=accepted["job_id"]).update(status="succeeded", lease_expires_at=None)
    run.refresh_from_db()
    return run


def _patch_models(monkeypatch, *, section_override=None):
    """替身：逐节生成 + 检索链路（扩展 / 重排）。"""
    from apps.retrieval import services as retrieval_services
    from apps.reviews import services as review_services

    def retrieval_call(_context, request):
        if request.task_type == TASK_EXPAND_QUERY:
            return SimpleNamespace(
                payload={"queries": [request.payload["current_query"]]}, call_id="mock:expand"
            )
        allowed = list(request.allowed_evidence_ids)
        return SimpleNamespace(
            payload={
                "results": [
                    {"evidence_id": eid, "score": 0.9 - index * 0.05, "reason": "relevant"}
                    for index, eid in enumerate(allowed[:10])
                ]
            },
            call_id="mock:rerank",
        )

    monkeypatch.setattr(retrieval_services, "call_structured", retrieval_call)

    def review_call(_context, request):
        assert request.task_type == TASK_REVIEW_SECTION
        allowed = list(request.allowed_evidence_ids)
        key = request.payload["section_key"]
        if section_override is not None:
            payload = section_override(key, allowed)
        else:
            payload = {
                "section_key": key,
                "title": f"{key} title",
                "blocks": [
                    {
                        "kind": "claim",
                        "text": __import__("apps.parsing.models", fromlist=["EvidenceChunk"])
                        .EvidenceChunk.objects.get(pk=allowed[i])
                        .text[:250]
                        .strip(),
                        "evidence_ids": [allowed[i]],
                    }
                    for i in range(2)
                ],
            }
        return SimpleNamespace(payload=payload, call_id=f"mock:{key}")

    monkeypatch.setattr(review_services, "call_structured", review_call)


def _generate(client, run, monkeypatch):
    _patch_models(monkeypatch)
    accepted = client.post(
        f"/api/v1/runs/{run.id}/reviews",
        {"title": "review"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    ).json()["data"]
    from apps.reviews import services as review_services

    context = claim_execution(accepted["job_id"])
    revision = review_services.generate_review(context, accepted["resource_id"])
    return Review.objects.get(pk=accepted["resource_id"]), revision


# ── M10：结构与引用 ───────────────────────────────────────────
def test_wrong_section_key_is_rejected_before_commit(client_a, run_ready, monkeypatch):
    """节序错误 → 结构校验拒绝，且不提交正文。"""
    _patch_models(
        monkeypatch,
        section_override=lambda key, allowed: {
            # 每节都自称是 background：节序校验必须拒绝
            "section_key": "background",
            "title": "t",
            "blocks": [
                {
                    "kind": "claim",
                    "text": __import__("apps.parsing.models", fromlist=["EvidenceChunk"])
                    .EvidenceChunk.objects.get(pk=allowed[0])
                    .text.strip(),
                    "evidence_ids": [allowed[0]],
                }
            ],
        },
    )
    accepted = client_a.post(
        f"/api/v1/runs/{run_ready.id}/reviews",
        {"title": "review"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    ).json()["data"]

    from apps.reviews import services as review_services

    context = claim_execution(accepted["job_id"])
    with pytest.raises(ApiError) as error:
        review_services.generate_review(context, accepted["resource_id"])
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert error.value.details["section_key"] == "significance"
    assert not ReviewRevision.objects.exists()


def test_each_section_needs_at_least_one_block(client_a, run_ready, monkeypatch):
    """空节被结构校验拒绝（schema 层，直接验证）。"""
    from apps.analysis.services import _run_scope

    scope = _run_scope(run_ready)
    evidence = sorted(next(iter(scope.evidence_ids_by_paper.values())))[0]
    sections = [
        {
            "section_key": key,
            "title": f"{key} title",
            "blocks": []
            if key == "achievements"
            else [
                {
                    "block_id": str(uuid.uuid4()),
                    "kind": "claim",
                    "text": "claim",
                    "evidence_ids": [evidence],
                }
            ],
        }
        for key in SECTION_KEYS
    ]
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


def test_block_limits_are_enforced(client_a, run_ready):
    """每块 1—600 字符；超长被拒。"""
    from apps.analysis.services import _run_scope

    scope = _run_scope(run_ready)
    evidence = sorted(next(iter(scope.evidence_ids_by_paper.values())))[0]
    sections = [
        {
            "section_key": key,
            "title": f"{key} title",
            "blocks": [
                {
                    "block_id": str(uuid.uuid4()),
                    "kind": "claim",
                    "text": "x" * 601,
                    "evidence_ids": [evidence],
                }
            ],
        }
        for key in SECTION_KEYS
    ]
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


def test_block_ids_must_be_unique_across_review(client_a, run_ready):
    """全稿 block_id 必须唯一。"""
    from apps.analysis.services import _run_scope

    scope = _run_scope(run_ready)
    evidence = sorted(next(iter(scope.evidence_ids_by_paper.values())))[0]
    shared = str(uuid.uuid4())
    sections = [
        {
            "section_key": key,
            "title": f"{key} title",
            "blocks": [
                {
                    "block_id": shared,
                    "kind": "claim",
                    "text": "claim",
                    "evidence_ids": [evidence],
                }
            ],
        }
        for key in SECTION_KEYS
    ]
    with pytest.raises(SectionsValidationError):
        validate_sections(sections, run_scope=scope)


def test_claim_requires_evidence_from_current_run(client_a, run_ready):
    """claim 无证据被拒；引用其它 Run 的证据同样被拒。"""
    from apps.analysis.services import _run_scope

    scope = _run_scope(run_ready)
    evidence = sorted(next(iter(scope.evidence_ids_by_paper.values())))[0]

    def build(block_evidence):
        return [
            {
                "section_key": key,
                "title": f"{key} title",
                "blocks": [
                    {
                        "block_id": str(uuid.uuid4()),
                        "kind": "claim",
                        "text": "claim",
                        "evidence_ids": block_evidence,
                    }
                ],
            }
            for key in SECTION_KEYS
        ]

    with pytest.raises(SectionsValidationError):
        validate_sections(build([]), run_scope=scope)
    # 另一个 Run 的证据不在本 Run 作用域内
    with pytest.raises(SectionsValidationError):
        validate_sections(build([str(uuid.uuid4())]), run_scope=scope)
    assert validate_sections(build([evidence]), run_scope=scope)


# ── M10：编辑版本冲突 ─────────────────────────────────────────
def test_save_revision_conflict_is_409(client_a, run_ready, monkeypatch):
    review, revision = _generate(client_a, run_ready, monkeypatch)
    response = client_a.post(
        f"/api/v1/reviews/{review.id}/revisions",
        {"base_revision_number": 99, "sections": revision.sections},
        format="json",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert review.revisions.count() == 1


# ── M11：核验与导出 ───────────────────────────────────────────
def test_report_binds_exactly_one_revision(client_a, validated):
    new = m11.save_edit(client_a, validated)
    validated.report.refresh_from_db()
    assert validated.report.revision_id == validated.revision.id != new.id
    assert m11.accept_export(client_a, validated, revision=new, ack=True).status_code == 422


def test_export_without_matching_report_is_rejected(client_a, generated):
    response = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/exports",
        {"revision_id": str(generated.revision.id), "validation_id": str(uuid.uuid4())},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_REQUIRED"


def test_export_with_unacknowledged_warnings_is_409(client_a, generated, monkeypatch):
    report, _ = m11.finish_validation(client_a, generated, monkeypatch, support="uncertain")
    generated.report = report
    response = m11.accept_export(client_a, generated)
    assert response.status_code == 409 and response.json()["error"]["code"] == "WARNINGS_NOT_ACKNOWLEDGED"


def test_bibliography_uses_frozen_metadata_only(client_a, validated):
    from apps.literature.models import Paper

    Paper.objects.filter(project_id=validated.review.project_id).update(title="不应出现在旧版本中的新标题")
    export, _ = m11.finish_export(client_a, validated)
    assert "不应出现在旧版本中的新标题" not in m11.word_text(export)


def test_export_file_is_draft_marked(client_a, validated):
    export, _ = m11.finish_export(client_a, validated)
    assert "模拟草稿（需人工审阅）：" not in m11.word_text(export)


def test_html_is_rejected_in_block_text(validated):
    import copy

    from apps.reviews.validation import validate_structure

    revision = validated.revision
    revision.sections = copy.deepcopy(revision.sections)
    revision.sections[0]["blocks"][0]["text"] = "<script>bad</script>"
    findings = validate_structure(revision)
    assert not findings.passed and any("HTML" in i["message"] for i in findings.issues)


def test_new_revision_inherits_analysis_revision(client_a, validated):
    from apps.analysis.models import AnalysisRun

    AnalysisRun.objects.filter(pk=validated.run.id).update(analysis_revision=999)
    new = m11.save_edit(client_a, validated)
    assert new.analysis_revision == validated.revision.analysis_revision != 999
    dto = client_a.get(f"/api/v1/reviews/{validated.review.id}/revisions/{new.id}").json()["data"]
    assert dto["is_stale"] is True
