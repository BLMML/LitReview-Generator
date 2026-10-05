"""M11：真实 PostgreSQL、指定版本核验、警示确认、私有 Word 与失败重试。"""

from __future__ import annotations

import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from django.db import connections
from django.utils import timezone
from docx import Document
from tests import test_m10_reviews as m10

from apps.common.auth import ActorContext
from apps.common.http import ApiError, api_error
from apps.common.models import FileAsset
from apps.common.storage import open_asset, resolve_storage_key, staging_root
from apps.jobs.models import Job
from apps.jobs.services import cancel_job, claim_execution, fail_job, retry_job
from apps.jobs.tasks import execute_job, reconcile_jobs
from apps.reviews import services, validation
from apps.reviews.models import Export, ReviewRevision, ValidationReport
from apps.reviews.schemas import sections_content_hash

pytestmark = pytest.mark.django_db(transaction=True)
run_ready = m10.run_ready
generated = m10.generated


def patch_verify(monkeypatch, *, support="supported", structure_ok=True, calls=None, transform=None):
    def call(context, request):
        context.ensure_not_cancelled()
        if calls is not None:
            calls.append(request)
        rows = [
            {
                "block_id": b["block_id"],
                "structure_ok": structure_ok,
                "support": support,
                "issues": [] if support == "supported" else ["原文中的数值或条件需要核对"],
                "suggestion": None if support == "supported" else "请核对引用页中的数字与条件",
            }
            for b in request.payload["blocks_with_evidence"]
        ]
        return SimpleNamespace(payload={"blocks": transform(rows) if transform else rows})

    monkeypatch.setattr(validation, "call_structured", call)


def accept_validation(client, g, *, revision=None, key=None):
    return client.post(
        f"/api/v1/reviews/{g.review.id}/validations",
        {"revision_id": str((revision or g.revision).id)},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def finish_validation(client, g, monkeypatch, **kwargs):
    patch_verify(monkeypatch, **kwargs)
    response = accept_validation(client, g)
    assert response.status_code == 202, response.json()
    data = response.json()["data"]
    report = services.validate_revision(claim_execution(data["job_id"]), data["resource_id"])
    return report, data["job_id"]


@pytest.fixture
def validated(client_a, generated, monkeypatch):
    report, job_id = finish_validation(client_a, generated, monkeypatch)
    return SimpleNamespace(**vars(generated), report=report, validation_job_id=job_id)


def accept_export(client, g, *, revision=None, report=None, ack=False, key=None):
    return client.post(
        f"/api/v1/reviews/{g.review.id}/exports",
        {
            "revision_id": str((revision or g.revision).id),
            "validation_id": str((report or g.report).id),
            "acknowledge_warnings": ack,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def finish_export(client, g, *, ack=False):
    response = accept_export(client, g, ack=ack)
    assert response.status_code == 202, response.json()
    data = response.json()["data"]
    export = services.export_revision(claim_execution(data["job_id"]), data["resource_id"])
    return export, data["job_id"]


def word_text(export):
    with open_asset(export.file_asset) as stream:
        doc = Document(stream)
    return "\n".join(p.text for p in doc.paragraphs) + "\n" + doc.sections[0].header.paragraphs[0].text


def save_edit(client, g):
    sections = copy.deepcopy(g.revision.sections)
    sections[0]["blocks"] = sections[0]["blocks"][:1]
    reply = client.post(
        f"/api/v1/reviews/{g.review.id}/revisions",
        {"base_revision_number": g.revision.revision_number, "sections": sections},
        format="json",
    )
    assert reply.status_code == 201, reply.json()
    return ReviewRevision.objects.get(pk=reply.json()["data"]["id"])


def use_legacy_revision(g):
    """Keep validation regression coverage for existing historical manuscripts."""
    from apps.reviews.models import SECTION_KEYS

    sections = copy.deepcopy(g.revision.sections[:5])
    for section, key in zip(sections, SECTION_KEYS, strict=True):
        section["section_key"] = key
    g.revision.sections = sections
    g.revision.config_snapshot = {}
    g.revision.content_hash = sections_content_hash(sections)
    g.revision.save(update_fields=["sections", "config_snapshot", "content_hash"])


def test_validation_pending_fields_are_null(client_a, generated):
    reply = accept_validation(client_a, generated)
    assert reply.status_code == 202, reply.json()
    report = ValidationReport.objects.get(pk=reply.json()["data"]["resource_id"])
    assert report.revision_id == generated.revision.id
    assert (report.result, report.structure_passed, report.blocks) == (None, None, None)
    dto = client_a.get(f"/api/v1/validations/{report.id}").json()["data"]
    assert (dto["result"], dto["structure_passed"], dto["blocks"]) == (None, None, None)
    assert dto["content_hash"] == generated.revision.content_hash


def test_validation_covers_all_blocks_once(client_a, validated):
    expected = [b["block_id"] for s in validated.revision.sections for b in s["blocks"]]
    assert [b["block_id"] for b in validated.report.blocks] == expected
    assert validated.report.result == "passed" and validated.report.structure_passed is True
    job = Job.objects.get(pk=validated.validation_job_id)
    assert job.status == "succeeded" and job.result["report_hash"]
    assert client_a.get(f"/api/v1/reviews/{validated.review.id}/validations").json()["meta"]["total"] == 1


@pytest.mark.parametrize("support", ["unsupported", "uncertain", "not_applicable"])
def test_semantic_findings_require_review(client_a, generated, monkeypatch, support):
    report, job_id = finish_validation(client_a, generated, monkeypatch, support=support)
    assert report.result == "needs_review" and report.structure_passed is True
    assert Job.objects.get(pk=job_id).status == "succeeded"
    assert all(b["support"] != "not_applicable" for b in report.blocks)


def test_model_structure_error_blocks_export(client_a, generated, monkeypatch):
    report, _ = finish_validation(client_a, generated, monkeypatch, structure_ok=False)
    generated.report = report
    assert report.result == "needs_review" and report.structure_passed is False
    assert (
        accept_export(client_a, generated, ack=True).json()["error"]["code"] == "STRUCTURAL_VALIDATION_FAILED"
    )


@pytest.mark.parametrize("corruption", ["html", "missing_ref", "cross_run", "duplicate_id", "empty_section"])
def test_local_structure_failure_completes_report_without_model(client_a, generated, monkeypatch, corruption):
    sections = copy.deepcopy(generated.revision.sections)
    block = sections[0]["blocks"][0]
    if corruption == "html":
        block["text"] = "<b>bad</b>"
    elif corruption == "missing_ref":
        block["evidence_ids"] = []
    elif corruption == "cross_run":
        block["evidence_ids"] = [str(uuid.uuid4())]
    elif corruption == "duplicate_id":
        sections[1]["blocks"][0]["block_id"] = block["block_id"]
    else:
        sections[0]["blocks"] = []
    ReviewRevision.objects.filter(pk=generated.revision.id).update(
        sections=sections, content_hash=sections_content_hash(sections)
    )
    calls = []
    report, job_id = finish_validation(client_a, generated, monkeypatch, calls=calls)
    assert calls == [] and report.result == "needs_review" and report.structure_passed is False
    assert Job.objects.get(pk=job_id).status == "succeeded"


def test_missing_original_pdf_is_structural_failure(client_a, generated, monkeypatch):
    rp = generated.run.run_papers.first()
    resolve_storage_key(rp.file_asset.storage_key).unlink()
    report, _ = finish_validation(client_a, generated, monkeypatch)
    assert report.structure_passed is False and report.result == "needs_review"
    assert any("原始来源" in issue for b in report.blocks for issue in b["issues"])


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "foreign"])
def test_model_coverage_failure_keeps_report_null(client_a, generated, monkeypatch, invalid):
    def transform(rows):
        if invalid == "missing":
            return rows[1:]
        if invalid == "duplicate":
            return [*rows, rows[0]]
        rows[0]["block_id"] = str(uuid.uuid4())
        return rows

    patch_verify(monkeypatch, transform=transform)
    data = accept_validation(client_a, generated).json()["data"]
    assert execute_job.run(data["job_id"]) == "failed"
    report = ValidationReport.objects.get(pk=data["resource_id"])
    assert (report.result, report.structure_passed, report.blocks) == (None, None, None)
    assert Job.objects.get(pk=data["job_id"]).error["code"] == "MODEL_OUTPUT_INVALID"


def test_failed_model_retry_keeps_revision_and_logical_ids(client_a, generated, monkeypatch, user_a):
    calls = []

    def fail(_context, request):
        calls.append(request)
        raise api_error("MODEL_TIMEOUT")

    monkeypatch.setattr(validation, "call_structured", fail)
    data = accept_validation(client_a, generated).json()["data"]
    execute_job.run(data["job_id"])
    old = Job.objects.get(pk=data["job_id"])
    retried = retry_job(ActorContext(user=user_a, request_id="m11"), old.id)
    patch_verify(monkeypatch, calls=calls)
    report = services.validate_revision(claim_execution(retried.job_id), retried.resource_id)
    assert report.revision_id == generated.revision.id and report.result == "passed"
    assert calls[0].logical_call_id == calls[1].logical_call_id
    old.refresh_from_db()
    assert old.status == "failed" and old.error["code"] == "MODEL_TIMEOUT"
    assert Job.objects.get(pk=retried.job_id).frozen_input == old.frozen_input


def test_validation_frozen_config_ignores_new_defaults(client_a, generated, monkeypatch, settings):
    data = accept_validation(client_a, generated).json()["data"]
    config = ValidationReport.objects.get(pk=data["resource_id"]).config_snapshot
    settings.DEEPSEEK_MODEL = "changed-model"
    settings.LLM_MAX_OUTPUT_TOKENS = 8192
    calls = []
    patch_verify(monkeypatch, calls=calls)
    services.validate_revision(claim_execution(data["job_id"]), data["resource_id"])
    assert all(r.model_config == config["model_config"] for r in calls)
    assert all(len(r.payload["blocks_with_evidence"]) <= 5 for r in calls)


@pytest.mark.parametrize("changed", ["body", "evidence", "template"])
def test_frozen_validation_input_cannot_drift(client_a, generated, monkeypatch, changed):
    from apps.parsing.models import EvidenceChunk

    data = accept_validation(client_a, generated).json()["data"]
    if changed == "body":
        sections = copy.deepcopy(generated.revision.sections)
        sections[0]["blocks"][0]["text"] = "被改写的正文"
        ReviewRevision.objects.filter(pk=generated.revision.id).update(sections=sections)
    elif changed == "evidence":
        eid = generated.revision.sections[0]["blocks"][0]["evidence_ids"][0]
        EvidenceChunk.objects.filter(pk=eid).update(original_text="被改写的证据")
    else:
        monkeypatch.setattr(validation, "validation_template_version", lambda: "changed")
    patch_verify(monkeypatch)
    assert execute_job.run(data["job_id"]) == "failed"
    assert ValidationReport.objects.get(pk=data["resource_id"]).result is None


@pytest.mark.parametrize("pure", [True, False])
def test_transition_cannot_skip_facts(client_a, generated, monkeypatch, pure):
    use_legacy_revision(generated)
    sections = copy.deepcopy(generated.revision.sections)
    block = sections[0]["blocks"][0]
    block.update(
        kind="transition",
        evidence_ids=[],
        text="下面介绍主要研究方向。" if pure else "模型提高了准确率 90%。",
    )
    reply = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/revisions",
        {"base_revision_number": 1, "sections": sections},
        format="json",
    )
    generated.revision = ReviewRevision.objects.get(pk=reply.json()["data"]["id"])
    report, _ = finish_validation(client_a, generated, monkeypatch, support="not_applicable")
    assert report.blocks[0]["support"] == ("not_applicable" if pure else "uncertain")


def test_real_mock_gateway_returns_bound_uncertain_report(client_a, generated, monkeypatch):
    from apps.llm.client import call_structured
    from apps.llm.models import ModelCall

    monkeypatch.setattr(validation, "call_structured", call_structured)
    data = accept_validation(client_a, generated).json()["data"]
    assert execute_job.run(data["job_id"]) == "done"
    report = ValidationReport.objects.get(pk=data["resource_id"])
    assert report.result == "needs_review" and report.structure_passed is True
    calls = ModelCall.objects.filter(job_id=data["job_id"])
    assert calls.count() >= 2 and all(c.estimated_cost_usd == 0 for c in calls)


def test_transition_without_evidence_cannot_be_supported(client_a, generated, monkeypatch):
    use_legacy_revision(generated)
    sections = copy.deepcopy(generated.revision.sections)
    sections[0]["blocks"][0].update(kind="transition", evidence_ids=[], text="准确率提升了 90%。")
    response = client_a.post(
        f"/api/v1/reviews/{generated.review.id}/revisions",
        {"base_revision_number": 1, "sections": sections},
        format="json",
    )
    generated.revision = ReviewRevision.objects.get(pk=response.json()["data"]["id"])
    report, _ = finish_validation(client_a, generated, monkeypatch)
    assert report.blocks[0]["support"] == "uncertain" and report.result == "needs_review"


def test_validation_replay_before_busy(client_a, generated):
    key = str(uuid.uuid4())
    first = accept_validation(client_a, generated, key=key)
    replay = accept_validation(client_a, generated, key=key)
    assert first.status_code == replay.status_code == 202 and first.json()["data"] == replay.json()["data"]
    busy = accept_validation(client_a, generated)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "PROJECT_BUSY"


def test_report_and_download_are_owner_scoped(client_b, validated):
    assert client_b.get(f"/api/v1/validations/{validated.report.id}").status_code == 404
    assert accept_validation(client_b, validated).status_code == 404
    assert accept_export(client_b, validated).status_code == 404


@pytest.mark.parametrize("stale", ["corpus", "analysis", "historical"])
def test_export_requires_each_warning_ack(client_a, validated, stale):
    if stale == "historical":
        save_edit(client_a, validated)
    elif stale == "analysis":
        from apps.analysis.models import AnalysisRun

        AnalysisRun.objects.filter(pk=validated.run.id).update(
            analysis_revision=validated.run.analysis_revision + 1
        )
    else:
        from apps.projects.models import Project

        Project.objects.filter(pk=validated.review.project_id).update(corpus_revision=999)
    reply = accept_export(client_a, validated)
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "WARNINGS_NOT_ACKNOWLEDGED"
    export, _ = finish_export(client_a, validated, ack=True)
    assert len(export.warnings) == 1 and export.warnings[0] not in word_text(export)


def test_needs_review_export_includes_issues(client_a, generated, monkeypatch):
    report, _ = finish_validation(client_a, generated, monkeypatch, support="unsupported")
    generated.report = report
    assert accept_export(client_a, generated).status_code == 409
    export, _ = finish_export(client_a, generated, ack=True)
    text = word_text(export)
    assert "原文中的数值或条件需要核对" not in text
    assert "needs_review" not in text and generated.report.content_hash not in text
    assert export.validation_report.result == "needs_review" and export.warnings


def test_export_binds_report_and_revision(client_a, validated):
    new = save_edit(client_a, validated)
    reply = accept_export(client_a, validated, revision=new, ack=True)
    assert reply.status_code == 422 and reply.json()["error"]["code"] == "VALIDATION_REQUIRED"
    assert Export.objects.count() == 0


def test_report_pending_cannot_export(client_a, generated):
    data = accept_validation(client_a, generated).json()["data"]
    Job.objects.filter(pk=data["job_id"]).update(status="failed")
    generated.report = ValidationReport.objects.get(pk=data["resource_id"])
    reply = accept_export(client_a, generated, ack=True)
    assert reply.status_code == 422 and reply.json()["error"]["code"] == "VALIDATION_REQUIRED"


def test_changed_completed_report_cannot_export(client_a, validated):
    ValidationReport.objects.filter(pk=validated.report.id).update(blocks=[])
    reply = accept_export(client_a, validated)
    assert reply.status_code == 422 and reply.json()["error"]["code"] == "VALIDATION_REQUIRED"


def test_export_frozen_metadata_and_first_citation_order(client_a, validated):
    from apps.literature.models import Paper
    from apps.reviews.exporter import build_bibliography

    Paper.objects.filter(project_id=validated.review.project_id).update(title="活跃题录被修改", year=2026)
    export, _ = finish_export(client_a, validated)
    text = word_text(export)
    assert "活跃题录被修改" not in text and "年份未知" not in text
    job = Job.objects.get(resource_type="export", resource_id=export.id)
    frozen = job.frozen_input["revision_snapshot"]
    by_id = {e["evidence_id"]: e for e in frozen["evidence"]}
    expected = list(
        dict.fromkeys(
            by_id[eid]["paper_id"]
            for s in frozen["sections"]
            for b in s["blocks"]
            for eid in b["evidence_ids"]
        )
    )
    bibliography = build_bibliography(frozen)
    assert [p["paper_id"] for p in bibliography] == expected
    assert [p["number"] for p in bibliography] == list(range(1, len(expected) + 1))
    assert "页级证据附录" not in text and "页，第" in text and "段，第" in text


def test_export_download_draft_marked_and_private(client_a, client_b, validated):
    response = accept_export(client_a, validated)
    data = response.json()["data"]
    assert client_a.get(f"/api/v1/exports/{data['resource_id']}/file").status_code == 409
    export = services.export_revision(claim_execution(data["job_id"]), data["resource_id"])
    response = client_a.get(f"/api/v1/exports/{export.id}/file")
    assert response.status_code == 200
    from urllib.parse import unquote

    assert unquote(response["Content-Disposition"].split("filename*=UTF-8''", 1)[1]) == (
        validated.review.project.title + "文献综述.docx"
    )
    assert export.file_asset.original_filename == validated.review.project.title + "文献综述.docx"
    job = Job.objects.get(pk=data["job_id"])
    assert job.frozen_input["revision_snapshot"]["project_title"] == validated.review.project.title
    content = b"".join(response.streaming_content)
    response.close()
    document = Document(BytesIO(content))
    assert document.sections[0].header.paragraphs[0].text == ""
    assert document.styles["Title"].element.xpath("./w:pPr/w:pBdr") == []
    assert client_b.get(f"/api/v1/exports/{export.id}/file").status_code == 404
    assert export.file_asset.purpose == FileAsset.PURPOSE_EXPORT_DOCX

    # Historical files also adopt the current user-defined project name,
    # without rewriting their document bytes or relying on an old asset name.
    from apps.projects.models import Project

    FileAsset.objects.filter(pk=export.file_asset_id).update(original_filename="legacy_综述_v1.docx")
    Project.objects.filter(pk=validated.review.project_id).update(title='新项目/知识体系"研究"')
    renamed = client_a.get(f"/api/v1/exports/{export.id}/file")
    assert unquote(renamed["Content-Disposition"].split("filename*=UTF-8''", 1)[1]) == (
        "新项目_知识体系_研究_文献综述.docx"
    )
    assert b"".join(renamed.streaming_content) == content
    renamed.close()


def test_export_replay_and_body_conflict(client_a, validated):
    key = str(uuid.uuid4())
    first = accept_export(client_a, validated, key=key)
    replay = accept_export(client_a, validated, key=key)
    assert first.json()["data"] == replay.json()["data"] and replay.status_code == 202
    conflict = accept_export(client_a, validated, key=key, ack=True)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("change", ["cancel", "lease", "report", "source", "new_warning"])
def test_export_checks_again_before_publication(client_a, validated, monkeypatch, change, user_a):
    from apps.reviews import exporter

    actor = ActorContext(user=user_a, request_id="m11")
    response = accept_export(client_a, validated)
    data = response.json()["data"]
    original = exporter.render_docx

    def render(*args):
        staged = original(*args)
        if change == "cancel":
            cancel_job(actor, uuid.UUID(data["job_id"]))
        elif change == "lease":
            Job.objects.filter(pk=data["job_id"]).update(lease_token=99)
        elif change == "report":
            ValidationReport.objects.filter(pk=validated.report.id).update(blocks=[])
        elif change == "source":
            rp = validated.run.run_papers.first()
            resolve_storage_key(rp.file_asset.storage_key).unlink()
        else:
            from apps.projects.models import Project

            Project.objects.filter(pk=validated.review.project_id).update(corpus_revision=999)
        return staged

    monkeypatch.setattr(exporter, "render_docx", render)
    context = claim_execution(data["job_id"])
    with pytest.raises(ApiError):
        services.export_revision(context, data["resource_id"])
    export = Export.objects.get(pk=data["resource_id"])
    assert export.file_asset_id is None and export.status != "ready"
    assert not FileAsset.objects.filter(purpose="export_docx").exists()
    assert list(staging_root().iterdir()) == []


def test_file_publication_rollback_cleans_archived_file(client_a, validated, monkeypatch):
    monkeypatch.setattr(
        "apps.jobs.services.complete_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
    )
    data = accept_export(client_a, validated).json()["data"]
    assert execute_job.run(data["job_id"]) == "failed"
    export = Export.objects.get(pk=data["resource_id"])
    assert export.status == "failed" and export.file_asset_id is None
    assert export.error["code"] == "INTERNAL_ERROR"
    assert not FileAsset.objects.filter(purpose="export_docx").exists()
    assert not list(staging_root().iterdir())
    assert not list(resolve_storage_key(str(validated.review.project_id)).glob("export_docx/**/*.docx"))


def test_export_retry_reuses_resource_and_snapshot(client_a, validated, monkeypatch, user_a):
    from apps.reviews import exporter

    original = exporter.render_docx

    def fail(*args):
        raise api_error("INTERNAL_ERROR")

    monkeypatch.setattr(exporter, "render_docx", fail)
    data = accept_export(client_a, validated).json()["data"]
    assert execute_job.run(data["job_id"]) == "failed"
    old = Job.objects.get(pk=data["job_id"])
    assert Export.objects.get(pk=data["resource_id"]).status == "failed"
    retried = retry_job(ActorContext(user=user_a, request_id="m11"), old.id)
    assert retried.resource_id == old.resource_id
    assert Export.objects.get(pk=data["resource_id"]).status == "pending"
    monkeypatch.setattr(exporter, "render_docx", original)
    assert execute_job.run(str(retried.job_id)) == "done"
    old.refresh_from_db()
    assert old.status == "failed"
    assert Export.objects.get(pk=data["resource_id"]).status == "ready"
    assert Job.objects.get(pk=retried.job_id).frozen_input == old.frozen_input


def test_old_ack_cannot_cover_new_warning_on_retry(client_a, validated, user_a):
    actor = ActorContext(user=user_a, request_id="m11")
    # 即使最初显式给 true，也只确认当时的警示集合。
    data = accept_export(client_a, validated, ack=True).json()["data"]
    fail_job(claim_execution(data["job_id"]), api_error("INTERNAL_ERROR").as_error_body())
    save_edit(client_a, validated)
    response = client_a.post(
        f"/api/v1/jobs/{data['job_id']}/retry", {}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "WARNINGS_NOT_ACKNOWLEDGED"
    assert Job.objects.filter(resource_id=data["resource_id"]).count() == 1
    new = accept_export(client_a, validated, ack=True)
    assert new.status_code == 202 and new.json()["data"]["resource_id"] != data["resource_id"]
    cancel_job(actor, uuid.UUID(new.json()["data"]["job_id"]))


def test_queued_cancel_and_lost_worker_sync_export_status(client_a, validated, user_a):
    actor = ActorContext(user=user_a, request_id="m11")
    data = accept_export(client_a, validated).json()["data"]
    cancel_job(actor, uuid.UUID(data["job_id"]))
    assert Export.objects.get(pk=data["resource_id"]).status == "cancelled"
    data = accept_export(client_a, validated).json()["data"]
    claim_execution(data["job_id"])
    Job.objects.filter(pk=data["job_id"]).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert reconcile_jobs.run()["expired"] == 1
    export = Export.objects.get(pk=data["resource_id"])
    assert export.status == "failed" and export.error["code"] == "WORKER_LOST"


def test_export_does_not_call_model(client_a, validated, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Word 导出不能调用模型")

    monkeypatch.setattr(validation, "call_structured", forbidden)
    monkeypatch.setattr(services, "call_structured", forbidden)
    export, _ = finish_export(client_a, validated)
    assert export.status == "ready"


@pytest.mark.parametrize("change", ["cancel", "lease"])
def test_validation_cancel_or_fence_cannot_publish(client_a, generated, monkeypatch, user_a, change):
    from apps.common.locking import check_execution_lease

    data = accept_validation(client_a, generated).json()["data"]
    context = claim_execution(data["job_id"])
    calls = []
    patch_verify(monkeypatch, calls=calls)
    original = validation.call_structured

    def verify(ctx, request):
        result = original(ctx, request)
        if change == "cancel":
            cancel_job(ActorContext(user=user_a, request_id="m11"), context.job_id)
        else:
            Job.objects.filter(pk=context.job_id).update(lease_token=99)
        return result

    monkeypatch.setattr(validation, "call_structured", verify)
    with pytest.raises(ApiError):
        services.validate_revision(context, data["resource_id"])
    report = ValidationReport.objects.get(pk=data["resource_id"])
    assert (report.result, report.structure_passed, report.blocks) == (None, None, None)
    with pytest.raises(ApiError):
        check_execution_lease(Job.objects.get(pk=context.job_id), context.lease_token)


@pytest.mark.parametrize("route", ["validations", "exports"])
def test_history_revision_filter_is_validated(client_a, validated, route):
    base = f"/api/v1/reviews/{validated.review.id}/{route}"
    assert client_a.get(base, {"revision_id": "not-uuid"}).status_code == 400
    assert client_a.get(base, {"revision_id": str(uuid.uuid4())}).status_code == 404
    response = client_a.get(base, {"revision_id": str(validated.revision.id)})
    assert response.status_code == 200
    assert len(response.json()["data"]) == (1 if route == "validations" else 0)


def test_evidence_changed_after_report_requires_new_validation(client_a, validated):
    from apps.parsing.evidence import chunk_content_hash
    from apps.parsing.models import EvidenceChunk

    eid = validated.revision.sections[0]["blocks"][0]["evidence_ids"][0]
    EvidenceChunk.objects.filter(pk=eid).update(
        text="Different supported result.",
        original_text="Different supported result.",
        content_hash=chunk_content_hash("Different supported result."),
    )
    response = accept_export(client_a, validated)
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_REQUIRED"


def test_historical_stale_revision_can_be_validated(client_a, validated, monkeypatch):
    from apps.analysis.models import AnalysisRun
    from apps.projects.models import Project

    save_edit(client_a, validated)
    Project.objects.filter(pk=validated.review.project_id).update(corpus_revision=999)
    AnalysisRun.objects.filter(pk=validated.run.id).update(analysis_revision=999)
    report, _ = finish_validation(client_a, validated, monkeypatch)
    assert report.revision_id == validated.revision.id and report.result == "passed"
    assert validated.report.id != report.id


def test_duplicate_execution_does_not_create_second_asset(client_a, validated):
    data = accept_export(client_a, validated).json()["data"]
    context = claim_execution(data["job_id"])
    first = services.export_revision(context, data["resource_id"])
    second = services.export_revision(context, data["resource_id"])
    assert first.file_asset_id == second.file_asset_id
    assert FileAsset.objects.filter(purpose="export_docx").count() == 1


def test_unverified_image_requires_human_review(client_a, generated, monkeypatch):
    from apps.common.storage import commit_asset, stage_file
    from apps.parsing.models import EvidenceChunk

    staged = stage_file(BytesIO(b"test image asset"), limit_bytes=100, suffix=".png", media_type="image/png")
    image = commit_asset(generated.review.project, staged, FileAsset.PURPOSE_PAGE_IMAGE)
    eid = generated.revision.sections[0]["blocks"][0]["evidence_ids"][0]
    EvidenceChunk.objects.filter(pk=eid).update(
        verification="image_unverified", source_image=image, original_text=None
    )
    report, _ = finish_validation(client_a, generated, monkeypatch)
    assert report.structure_passed is False and report.result == "needs_review"
    assert any("文本层" in issue for b in report.blocks for issue in b["issues"])


def test_input_bound_batches_full_evidence_without_truncation(client_a, generated, monkeypatch, settings):
    use_legacy_revision(generated)
    from apps.parsing.evidence import chunk_content_hash
    from apps.parsing.models import EvidenceChunk

    settings.LLM_MAX_INPUT_TOKENS = validation.validation_schema_config()["system_input_bytes"] + 13000
    eids = {eid for s in generated.revision.sections for b in s["blocks"] for eid in b["evidence_ids"]}
    text = "original evidence " * 180
    EvidenceChunk.objects.filter(pk__in=eids).update(
        text=text, original_text=text, content_hash=chunk_content_hash(text)
    )
    calls = []
    report, _ = finish_validation(client_a, generated, monkeypatch, calls=calls)
    assert report.result == "passed" and len(calls) > 2
    assert all(
        e["text"] == text for r in calls for b in r.payload["blocks_with_evidence"] for e in b["evidence"]
    )


def test_export_freezes_confirmed_warning_set(client_a, generated, monkeypatch, user_a):
    report, _ = finish_validation(client_a, generated, monkeypatch, support="uncertain")
    generated.report = report
    data = accept_export(client_a, generated, ack=True).json()["data"]
    context = claim_execution(data["job_id"])
    fail_job(context, api_error("INTERNAL_ERROR").as_error_body())
    from apps.analysis.models import AnalysisRun

    AnalysisRun.objects.filter(pk=generated.run.id).update(analysis_revision=999)
    with pytest.raises(ApiError) as error:
        retry_job(ActorContext(user=user_a, request_id="m11"), context.job_id)
    assert error.value.code == "WARNINGS_NOT_ACKNOWLEDGED"


def test_cancel_is_available_while_export_holds_project(client_a, validated, monkeypatch, user_a):
    data = accept_export(client_a, validated).json()["data"]
    context = claim_execution(data["job_id"])
    original_lock = services._lock_review_job

    def cancel_on_other_connection():
        try:
            return cancel_job(ActorContext(user=user_a, request_id="m11"), context.job_id).status
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def before_job_lock(*args):
            assert pool.submit(cancel_on_other_connection).result(timeout=4) == "cancelling"
            return original_lock(*args)

        monkeypatch.setattr(services, "_lock_review_job", before_job_lock)
        with pytest.raises(ApiError) as error:
            services.export_revision(context, data["resource_id"])
    assert error.value.code == "INVALID_JOB_STATE"
    assert Export.objects.get(pk=data["resource_id"]).file_asset_id is None
    assert not FileAsset.objects.filter(purpose="export_docx").exists()
    assert not list(staging_root().iterdir())


def test_live_wsgi_validation_and_private_word_download(live_server, generated, monkeypatch, user_a):
    """真实 TCP/WSGI 验证 CSRF、Session、JSON 任务受理与二进制响应头。"""
    import httpx
    from tests.conftest import PASSWORD

    patch_verify(monkeypatch)
    with httpx.Client(base_url=live_server.url, trust_env=False, timeout=15) as client:
        csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        login = client.post(
            "/api/v1/auth/login",
            json={"username": user_a.username, "password": PASSWORD},
            headers={"X-CSRFToken": csrf},
        )
        assert login.status_code == 200, login.text
        csrf = client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
        response = client.post(
            f"/api/v1/reviews/{generated.review.id}/validations",
            json={"revision_id": str(generated.revision.id)},
            headers={"X-CSRFToken": csrf, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert execute_job.run(accepted["job_id"]) == "done"
        report = client.get(f"/api/v1/validations/{accepted['resource_id']}").json()["data"]
        assert report["result"] == "passed" and report["revision_id"] == str(generated.revision.id)
        response = client.post(
            f"/api/v1/reviews/{generated.review.id}/exports",
            json={"revision_id": report["revision_id"], "validation_id": report["id"]},
            headers={"X-CSRFToken": csrf, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 202, response.text
        accepted = response.json()["data"]
        assert execute_job.run(accepted["job_id"]) == "done"
        response = client.get(f"/api/v1/exports/{accepted['resource_id']}/file")
        assert response.status_code == 200
        assert (
            response.headers["content-type"]
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert "%" in response.headers["content-disposition"]
        assert len(response.content) == int(response.headers["content-length"])
        assert "模拟草稿（需人工审阅）：" not in "\n".join(
            p.text for p in Document(BytesIO(response.content)).paragraphs
        )


def test_export_file_does_not_serve_asset_of_wrong_purpose(client_a, validated):
    export, _ = finish_export(client_a, validated)
    Export.objects.filter(pk=export.id).update(file_asset=validated.run.run_papers.first().file_asset)
    assert client_a.get(f"/api/v1/exports/{export.id}/file").status_code == 404
