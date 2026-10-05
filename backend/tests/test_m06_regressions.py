"""Independent M06 boundary checks; no provider requests or development DB writes."""

import json
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone
from tests.conftest import make_pdf_file, make_text_pdf_file

from apps.analysis.models import AnalysisRun, RunPaper
from apps.common.auth import ActorContext
from apps.common.http import ApiError, api_error
from apps.common.models import FileAsset
from apps.common.storage import resolve_storage_key, staging_root, storage_root
from apps.jobs.models import Job
from apps.jobs.services import cancel_job, claim_execution, complete_job
from apps.llm.models import ModelCall
from apps.parsing import services
from apps.parsing.config import effective_config
from apps.parsing.evidence import ChunkDraft, build_chunks
from apps.parsing.models import EvidenceChunk, ParsedDocument

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def paper_factory(client_a, project_a):
    from apps.literature.models import Paper

    def create(texts=None, *, blank_pages=None):
        upload = (
            make_pdf_file(blank_pages)
            if blank_pages
            else make_text_pdf_file(texts or ["A true sentence from the source page."])
        )
        response = client_a.post(
            f"/api/v1/projects/{project_a.pk}/papers/upload",
            {"file": upload},
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert response.status_code == 201, response.json()
        paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])
        return paper

    return create


def snapshot(paper):
    return SimpleNamespace(
        paper_id=paper.pk,
        project_id=paper.project_id,
        title=paper.title,
        file_asset=paper.file_asset,
        file_sha256=paper.file_sha256,
    )


def make_run_paper(project, paper, document=None):
    run = AnalysisRun.objects.create(
        project=project,
        corpus_revision=project.corpus_revision,
        research_question=project.research_question,
        output_language="zh-CN",
    )
    row = RunPaper.objects.create(
        run=run,
        paper=paper,
        title=paper.title,
        authors=paper.authors,
        source=paper.source,
        file_asset=paper.file_asset,
        file_sha256=paper.file_sha256,
        page_count=paper.page_count,
        parsed_document=document,
    )
    return run, row


def context_for(project, user, run_id=None):
    job = Job.objects.create(
        project=project,
        requested_by=user,
        kind="analyze",
        resource_type="analysis_run",
        resource_id=run_id or uuid.uuid4(),
        input_hash="0" * 64,
        frozen_input={"run_id": str(run_id)} if run_id else {},
    )
    return job, claim_execution(job.pk)


def test_true_run_paper_is_supported(paper_factory, project_a):
    paper = paper_factory()
    _, run_paper = make_run_paper(project_a, paper)
    document = services.parse_paper(None, run_paper, {})
    assert document.paper_id == paper.pk
    assert document.project_id == project_a.pk


@pytest.mark.parametrize("invalid", ["cancelled", "expired", "replaced"])
def test_invalid_execution_cannot_publish(paper_factory, project_a, user_a, invalid):
    paper = paper_factory()
    job, context = context_for(project_a, user_a)
    if invalid == "cancelled":
        cancel_job(ActorContext(user_a, "req_m06_review"), job.pk)
    elif invalid == "expired":
        Job.objects.filter(pk=job.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    else:
        Job.objects.filter(pk=job.pk).update(lease_token=context.lease_token + 1, status="failed")
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, snapshot(paper), {})
    assert error.value.code == "INVALID_JOB_STATE"
    assert not ParsedDocument.objects.exists()


def test_context_cannot_publish_into_another_project(paper_factory, project_b, user_b):
    paper = paper_factory()
    _, context = context_for(project_b, user_b)
    with pytest.raises(ApiError):
        services.parse_paper(context, snapshot(paper), {})
    assert not ParsedDocument.objects.exists()


def test_each_image_page_can_be_processed_in_default_configuration(
    paper_factory, project_a, user_a, monkeypatch
):
    paper = paper_factory(blank_pages=2)
    _, context = context_for(project_a, user_a)
    calls = []

    def transcribe(ctx, request):
        ids = request.model_config["image_asset_ids"]
        calls.append(ids)
        return SimpleNamespace(
            payload={
                "elements": [
                    {
                        "element_type": "text",
                        "text": f"Readable content from invocation {len(calls)}.",
                        "section": None,
                    },
                ]
            }
        )

    monkeypatch.setattr(services, "call_structured", transcribe)
    document = services.parse_paper(context, snapshot(paper), {})
    assert document.page_count == 2
    chunks = list(EvidenceChunk.objects.filter(parsed_document=document))
    assert {chunk.page_number for chunk in chunks} == {1, 2}
    assert all(chunk.verification == "image_unverified" for chunk in chunks)


def test_image_element_types_and_sections_survive(paper_factory, project_a, user_a, monkeypatch):
    paper = paper_factory(blank_pages=1)
    _, context = context_for(project_a, user_a)
    payload = {
        "elements": [
            {
                "element_type": "table",
                "text": "Dataset A score 93 and Dataset B score 81.",
                "section": "Results",
            },
            {"element_type": "formula", "text": "E equals m c squared.", "section": "Method"},
        ]
    }
    monkeypatch.setattr(services, "call_structured", lambda ctx, request: SimpleNamespace(payload=payload))
    document = services.parse_paper(context, snapshot(paper), {})
    actual = {
        (chunk.element_type, chunk.section)
        for chunk in EvidenceChunk.objects.filter(parsed_document=document)
    }
    assert actual == {("table", "Results"), ("formula", "Method")}, actual


def test_frozen_chunk_config_controls_actual_output(paper_factory):
    paper = paper_factory([" ".join(["source"] * 50)])
    frozen = {"EVIDENCE_CHUNK_MIN_CHARS": 10, "EVIDENCE_CHUNK_MAX_CHARS": 40}
    document = services.parse_paper(None, snapshot(paper), frozen)
    lengths = [len(chunk.text) for chunk in EvidenceChunk.objects.filter(parsed_document=document)]
    assert lengths and max(lengths) <= 40, lengths


def test_effective_setting_changes_do_not_reuse_stale_chunks(paper_factory, settings):
    paper = paper_factory([" ".join(["source"] * 50)])
    first = services.parse_paper(None, snapshot(paper), {})
    settings.EVIDENCE_CHUNK_MIN_CHARS = 10
    settings.EVIDENCE_CHUNK_MAX_CHARS = 40
    second = services.parse_paper(None, snapshot(paper), {})
    assert second.pk != first.pk, "Changed effective parser configuration reused the old product"
    assert max(len(chunk.text) for chunk in EvidenceChunk.objects.filter(parsed_document=second)) <= 40


@pytest.mark.parametrize("invalid", ["unmatched-text", "nonexistent-page"])
def test_text_matched_requires_real_same_page_source(paper_factory, invalid):
    paper = paper_factory()
    document = services.parse_paper(None, snapshot(paper), {})
    original = json.loads(resolve_storage_key(document.raw_pages_asset.storage_key).read_text("utf-8"))
    EvidenceChunk.objects.filter(parsed_document=document).delete()
    draft = ChunkDraft(
        page_number=1 if invalid == "unmatched-text" else 999,
        element_type="text",
        text="Invented text absent from the source."
        if invalid == "unmatched-text"
        else original["pages"][0]["text"],
    )
    with pytest.raises(ApiError):
        build_chunks(document, [draft])
    assert not EvidenceChunk.objects.filter(parsed_document=document).exists()


def test_missing_raw_product_is_not_successful_cache(paper_factory):
    paper = paper_factory()
    first = services.parse_paper(None, snapshot(paper), {})
    path = resolve_storage_key(first.raw_pages_asset.storage_key)
    assert path.is_relative_to(staging_root().parent)
    path.unlink()
    try:
        second = services.parse_paper(None, snapshot(paper), {})
    except ApiError:
        return
    assert resolve_storage_key(second.raw_pages_asset.storage_key).is_file(), (
        "Returned a successful cache with missing raw pages"
    )


def test_second_staging_failure_cleans_first_file(paper_factory, monkeypatch):
    paper = paper_factory()
    original = services._stage_json
    calls = []

    def fail_second(payload, suffix):
        calls.append(suffix)
        if len(calls) == 2:
            raise api_error("FILE_TOO_LARGE", "Simulated second staging failure")
        return original(payload, suffix)

    monkeypatch.setattr(services, "_stage_json", fail_second)
    with pytest.raises(ApiError):
        services.parse_paper(None, snapshot(paper), {})
    assert list(staging_root().iterdir()) == [], "Raw staging file was left behind"
    assert not ParsedDocument.objects.exists()


def test_chunk_ordinals_keep_page_reading_order(paper_factory, settings):
    settings.EVIDENCE_CHUNK_MIN_CHARS = 1
    settings.EVIDENCE_CHUNK_MAX_CHARS = 14
    paper = paper_factory(["Zebra first.\n\nAlpha second."])
    document = services.parse_paper(None, snapshot(paper), {})
    chunks = list(EvidenceChunk.objects.filter(parsed_document=document).order_by("ordinal"))
    assert [chunk.text for chunk in chunks] == ["Zebra first.", "Alpha second."]


def test_default_mock_image_path_does_not_fabricate_pdf_text(paper_factory, project_a, user_a):
    paper = paper_factory(blank_pages=1)
    job, context = context_for(project_a, user_a)
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, snapshot(paper), {})
    assert error.value.code == "PDF_PARSE_FAILED"
    assert not EvidenceChunk.objects.filter(parsed_document__paper=paper).exists()
    assert ModelCall.objects.filter(job=job, task_type="page_extract", outcome="succeeded").exists()


def test_evidence_http_and_original_file_authorization(paper_factory, project_a, client_a, client_b):
    paper = paper_factory()
    document = services.parse_paper(None, snapshot(paper), {})
    run, _ = make_run_paper(project_a, paper, document)
    chunk = EvidenceChunk.objects.filter(parsed_document=document).first()
    url = f"/api/v1/runs/{run.pk}/evidence/{chunk.pk}"
    response = client_a.get(url)
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["page_number"] == 1 and data["file_url"] == f"/api/v1/papers/{paper.pk}/file"
    assert client_b.get(url).status_code == 404
    assert client_b.get(data["file_url"]).status_code == 404
    original_file = client_a.get(data["file_url"])
    assert original_file.status_code == 200
    original_file.close()


def _storage_files():
    return {path for path in storage_root().rglob("*") if path.is_file()}


def _pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _wait_for_job_lock(waiting_pid, holder_pid, finished):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [waiting_pid])
            if holder_pid in cursor.fetchone()[0]:
                return
        if finished.wait(0.01):
            pytest.fail("竞争操作在发布事务释放 Job 锁前已完成")
    pytest.fail("未观察到竞争连接等待 Job 行锁")


def test_cancel_waits_for_document_and_chunks_commit(paper_factory, project_a, user_a, monkeypatch):
    assert connection.vendor == "postgresql"
    paper = paper_factory()
    job, context = context_for(project_a, user_a)
    actor = ActorContext(user_a, "req_m06_publish_race")
    holder_pid = _pid()
    started, finished = threading.Event(), threading.Event()
    pids, futures = [], []
    build = services.build_chunks

    def cancel_on_other_connection():
        try:
            pids.append(_pid())
            started.set()
            cancel_job(actor, job.pk)
        finally:
            connections.close_all()
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def publish_chunks_then_race(document, drafts):
            rows = build(document, drafts)
            futures.append(pool.submit(cancel_on_other_connection))
            assert started.wait(5)
            _wait_for_job_lock(pids[0], holder_pid, finished)
            return rows

        monkeypatch.setattr(services, "build_chunks", publish_chunks_then_race)
        document = services.parse_paper(context, snapshot(paper), {})
        futures[0].result(timeout=5)

    assert EvidenceChunk.objects.filter(parsed_document=document).exists()
    assert document.raw_pages_asset.source_job_id == job.pk
    assert document.structured_asset.source_job_id == job.pk
    job.refresh_from_db()
    assert job.status == "cancelling"
    with pytest.raises(ApiError) as error:
        complete_job(context, {"parsed_document_id": str(document.pk)})
    assert error.value.code == "INVALID_JOB_STATE"


def test_cancel_committed_first_rejects_waiting_publisher(paper_factory, project_a, user_a, monkeypatch):
    assert connection.vendor == "postgresql"
    paper = paper_factory()
    before = _storage_files()
    job, context = context_for(project_a, user_a)
    actor = ActorContext(user_a, "req_m06_cancel_first")
    holder_pid = _pid()
    staged_ready, allow_publish, finished = threading.Event(), threading.Event(), threading.Event()
    pids = []
    stage = services._stage_json

    def stage_then_signal(payload, suffix):
        result = stage(payload, suffix)
        if suffix == ".structured.json":
            staged_ready.set()
            assert allow_publish.wait(5)
        return result

    def publish_on_other_connection():
        try:
            pids.append(_pid())
            return services.parse_paper(context, snapshot(paper), {})
        finally:
            connections.close_all()
            finished.set()

    monkeypatch.setattr(services, "_stage_json", stage_then_signal)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish_on_other_connection)
        try:
            assert staged_ready.wait(5)
            with transaction.atomic():
                cancel_job(actor, job.pk)
                allow_publish.set()
                _wait_for_job_lock(pids[0], holder_pid, finished)
            with pytest.raises(ApiError) as error:
                future.result(timeout=5)
            assert error.value.code == "INVALID_JOB_STATE"
        finally:
            allow_publish.set()
    assert not ParsedDocument.objects.exists()
    assert not EvidenceChunk.objects.exists()
    assert _storage_files() == before


def test_expiry_during_publication_rolls_back_rows_and_files(paper_factory, project_a, user_a, monkeypatch):
    paper = paper_factory()
    before = _storage_files()
    job, context = context_for(project_a, user_a)
    job.refresh_from_db()
    build = services.build_chunks

    def build_then_expire(document, drafts):
        rows = build(document, drafts)
        monkeypatch.setattr(timezone, "now", lambda: job.lease_expires_at + timedelta(microseconds=1))
        return rows

    monkeypatch.setattr(services, "build_chunks", build_then_expire)
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, snapshot(paper), {})
    assert error.value.code == "INVALID_JOB_STATE"
    assert not ParsedDocument.objects.exists()
    assert not EvidenceChunk.objects.exists()
    assert not FileAsset.objects.filter(source_job=job).exists()
    assert _storage_files() == before


def test_concurrent_publications_converge_without_orphan_files(paper_factory, monkeypatch):
    assert connection.vendor == "postgresql"
    paper = paper_factory()
    inspection, pages = services.inspect_pdf(paper.file_asset), services.extract_pages(paper.file_asset)
    # 只竞争数据库发布；PDFium 的原生提取不参与线程并发。
    monkeypatch.setattr(services, "inspect_pdf", lambda asset: inspection)
    monkeypatch.setattr(services, "extract_pages", lambda asset: pages)
    barrier = threading.Barrier(2)
    stage = services._stage_json

    def stage_then_race(payload, suffix):
        result = stage(payload, suffix)
        if suffix == ".structured.json":
            barrier.wait(timeout=5)
        return result

    def publish():
        try:
            document = services.parse_paper(None, snapshot(paper), {})
            return document.pk, list(document.chunks.order_by("ordinal").values_list("pk", flat=True))
        finally:
            connections.close_all()

    monkeypatch.setattr(services, "_stage_json", stage_then_race)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(publish), pool.submit(publish)
        assert first.result(timeout=10) == second.result(timeout=10)
    assert ParsedDocument.objects.count() == 1
    assert FileAsset.objects.count() == 3  # 原 PDF + 两个完整产物
    assert _storage_files() == {resolve_storage_key(asset.storage_key) for asset in FileAsset.objects.all()}


@pytest.mark.parametrize("failure", ["second-commit", "chunk-insert"])
def test_publication_failure_removes_rolled_back_asset_files(paper_factory, monkeypatch, failure):
    paper = paper_factory()
    before = _storage_files()
    if failure == "second-commit":
        commit = services.commit_asset

        def fail_structured(project, staged, purpose, **kwargs):
            if purpose == FileAsset.PURPOSE_STRUCTURED_PARSE:
                raise OSError("Simulated filesystem commit failure")
            return commit(project, staged, purpose, **kwargs)

        monkeypatch.setattr(services, "commit_asset", fail_structured)
    else:

        def fail_chunks(document, drafts):
            raise api_error("PDF_PARSE_FAILED", "Simulated chunk insertion failure")

        monkeypatch.setattr(services, "build_chunks", fail_chunks)
    with pytest.raises((ApiError, OSError)):
        services.parse_paper(None, snapshot(paper), {})
    assert FileAsset.objects.count() == 1
    assert not ParsedDocument.objects.exists()
    assert not EvidenceChunk.objects.exists()
    assert _storage_files() == before


@pytest.mark.parametrize("part", ["raw", "structured", "chunk", "missing-chunk"])
def test_corrupt_or_incomplete_cache_is_rejected_without_rewriting(paper_factory, part):
    paper = paper_factory()
    document = services.parse_paper(None, snapshot(paper), {})
    if part == "chunk":
        document.chunks.update(text="Tampered unrelated evidence")
    elif part == "missing-chunk":
        document.chunks.all().delete()
    else:
        asset = document.raw_pages_asset if part == "raw" else document.structured_asset
        resolve_storage_key(asset.storage_key).write_bytes(b"{}")
    with pytest.raises(ApiError) as error:
        services.parse_paper(None, snapshot(paper), {})
    assert error.value.code == "PDF_PARSE_FAILED"
    assert ParsedDocument.objects.get().pk == document.pk


@pytest.fixture
def explicit_image_response(monkeypatch):
    """These cache/parameter tests supply a known transcription, not default OCR."""
    from apps.llm import client

    original = client._mock_response

    def provider(request, case_name):
        if request.task_type == "page_extract" and case_name is None:
            fixture_path = client.settings.BASE_DIR / "tests/fixtures/llm_cases.json"
            cases = json.loads(fixture_path.read_text("utf-8"))
            case_name = next(
                case["name"] for case in cases["cases"]
                if case["task_type"] == "page_extract" and case["expected"] == "accepted"
            )
        return original(request, case_name)

    monkeypatch.setattr(client, "_mock_response", provider)


def test_missing_image_cache_is_rejected(paper_factory, project_a, user_a, explicit_image_response):
    paper = paper_factory(blank_pages=1)
    _, context = context_for(project_a, user_a)
    document = services.parse_paper(context, snapshot(paper), {})
    image = document.chunks.first().source_image
    resolve_storage_key(image.storage_key).unlink()
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, snapshot(paper), {})
    assert error.value.code == "PDF_PARSE_FAILED"
    assert ParsedDocument.objects.get().pk == document.pk


def test_rebuilding_same_evidence_preserves_ids_and_rejects_changes(paper_factory):
    paper = paper_factory()
    document = services.parse_paper(None, snapshot(paper), {})
    original = list(document.chunks.order_by("ordinal"))
    drafts = [ChunkDraft(row.page_number, row.element_type, row.text, row.section) for row in original]
    assert [row.pk for row in build_chunks(document, drafts)] == [row.pk for row in original]
    changed = [ChunkDraft(1, "text", original[0].text, section="Changed section")]
    with pytest.raises(ApiError) as error:
        build_chunks(document, changed)
    assert error.value.code == "INVALID_JOB_STATE"
    assert list(document.chunks.values_list("pk", flat=True)) == [row.pk for row in original]


def test_full_frozen_config_survives_default_changes(paper_factory, settings):
    paper = paper_factory([" ".join(["source"] * 50)])
    frozen = effective_config({"EVIDENCE_CHUNK_MIN_CHARS": 10, "EVIDENCE_CHUNK_MAX_CHARS": 40})
    settings.EVIDENCE_CHUNK_MAX_CHARS = 100
    document = services.parse_paper(None, snapshot(paper), frozen)
    manifest = json.loads(resolve_storage_key(document.structured_asset.storage_key).read_text("utf-8"))
    assert manifest["config"] == frozen
    assert max(len(row.text) for row in document.chunks.all()) <= 40


@pytest.mark.parametrize("part", ["PARSER_REVISION", "NORMALIZATION_REVISION", "PAGE_TEMPLATE_VERSION"])
def test_unsupported_frozen_revision_is_rejected(paper_factory, part):
    paper = paper_factory()
    with pytest.raises(ApiError) as error:
        services.parse_paper(None, snapshot(paper), {part: "unsupported-revision"})
    assert error.value.code == "VALIDATION_ERROR"
    assert not ParsedDocument.objects.exists()


def test_real_run_context_must_match_snapshot(paper_factory, project_a, user_a):
    paper = paper_factory()
    run, run_paper = make_run_paper(project_a, paper)
    _, context = context_for(project_a, user_a, run.pk)
    document = services.parse_paper(context, run_paper, {})
    assert document.paper_id == paper.pk
    other_run, other_snapshot = make_run_paper(project_a, paper)
    assert other_run.pk != run.pk
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, other_snapshot, {})
    assert error.value.code == "INVALID_JOB_STATE"


def test_failed_page_diagnostics_are_linked_to_job(paper_factory, project_a, user_a, monkeypatch):
    paper = paper_factory(blank_pages=1)
    job, context = context_for(project_a, user_a)
    monkeypatch.setattr(
        services, "call_structured", lambda ctx, request: SimpleNamespace(payload={"elements": []})
    )
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, snapshot(paper), {})
    assert error.value.code == "PDF_PARSE_FAILED"
    ids = error.value.details["diagnostics"]
    assert set(ids) == {"raw_pages_asset_id", "structured_asset_id"}
    assert FileAsset.objects.filter(pk__in=list(ids.values()), source_job=job).count() == 2
    assert not ParsedDocument.objects.exists()
    assert not EvidenceChunk.objects.exists()


def test_frozen_image_and_model_parameters_control_actual_outputs(
    paper_factory, project_a, user_a, settings, explicit_image_response
):
    paper = paper_factory(blank_pages=1)
    job, context = context_for(project_a, user_a)
    frozen = effective_config(
        {
            "PAGE_RENDER_SCALE": 0.5,
            "MODEL_CONFIG": {"max_tokens": 1000, "thinking": "disabled", "temperature": 0.25},
        }
    )
    settings.PAGE_RENDER_SCALE = 2
    settings.DEEPSEEK_THINKING_MODE = "enabled"
    document = services.parse_paper(context, snapshot(paper), frozen)
    image = document.chunks.first().source_image
    png = resolve_storage_key(image.storage_key).read_bytes()
    assert struct.unpack(">II", png[16:24]) == (100, 100)  # 200 点页面 × 冻结倍率 0.5
    actual = ModelCall.objects.get(job=job, outcome="succeeded").model_snapshot
    assert (actual["max_tokens"], actual["thinking"], actual["temperature"]) == (1000, "disabled", 0.25)


def test_frozen_image_byte_limit_is_enforced(paper_factory, project_a, user_a):
    paper = paper_factory(blank_pages=1)
    _, context = context_for(project_a, user_a)
    with pytest.raises(ApiError) as error:
        services.parse_paper(context, snapshot(paper), {"PAGE_IMAGE_MAX_BYTES": 1})
    assert error.value.code == "PDF_PARSE_FAILED"
    assert error.value.details["pages"][0]["error"]["details"]["limit_bytes"] == 1
    assert not FileAsset.objects.filter(purpose=FileAsset.PURPOSE_PAGE_IMAGE).exists()
    assert not ParsedDocument.objects.exists()


def test_failed_diagnostic_staging_cleans_first_file(paper_factory, monkeypatch):
    paper = paper_factory(blank_pages=1)
    before = _storage_files()
    stage = services._stage_json

    def fail_structured(payload, suffix):
        if suffix == ".structured.json":
            raise api_error("FILE_TOO_LARGE", "Simulated diagnostic staging failure")
        return stage(payload, suffix)

    monkeypatch.setattr(services, "_stage_json", fail_structured)
    with pytest.raises(ApiError) as error:
        services.parse_paper(None, snapshot(paper), {})
    assert error.value.code == "PDF_PARSE_FAILED"
    assert error.value.details["diagnostics"] == {}
    assert FileAsset.objects.count() == 1
    assert _storage_files() == before
