"""Paper removal preserves snapshots, enforces ownership and releases capacity."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections, connection, connections, transaction
from django.db.utils import IntegrityError
from django.utils import timezone
from tests.conftest import make_pdf_file

from apps.analysis.models import AnalysisRun
from apps.common.auth import ActorContext
from apps.common.http import ApiError
from apps.common.locking import lock_project
from apps.jobs.models import Job
from apps.literature.models import Paper
from apps.literature.services import delete_paper, update_paper

pytestmark = pytest.mark.django_db(transaction=True)


def upload(client, project, *, pages=2, key=None, title="测试论文"):
    response = client.post(
        f"/api/v1/projects/{project.id}/papers/upload",
        {"file": make_pdf_file(pages), "title": title},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def remove(client, paper, **body):
    return client.delete(
        f"/api/v1/papers/{paper['id']}",
        {"expected_version": paper["version"], **body},
        format="json",
    )


@pytest.mark.parametrize("included", [True, False])
def test_delete_hides_paper_preserves_pdf_and_is_idempotent(client_a, project_a, included):
    paper = upload(client_a, project_a)
    if not included:
        paper = client_a.patch(
            f"/api/v1/papers/{paper['id']}",
            {"expected_version": 1, "included": False},
            format="json",
        ).json()["data"]
    project_a.refresh_from_db()
    before = project_a.corpus_revision
    original_asset = Paper.objects.get(pk=paper["id"]).file_asset_id
    response = remove(client_a, paper)
    assert response.status_code == 200, response.json()
    assert response.json()["data"] == {
        "paper_id": paper["id"],
        "deleted": True,
        "corpus_revision": before + 1,
    }
    row = Paper.objects.get(pk=paper["id"])
    assert row.deleted_at is not None and row.included is False
    assert row.version == paper["version"] + 1
    assert row.file_asset_id == original_asset
    listing = client_a.get(f"/api/v1/projects/{project_a.id}/papers").json()
    assert listing["data"] == [] and listing["meta"]["total"] == 0
    file = client_a.get(paper["file_url"])
    assert file.status_code == 200
    assert b"".join(file.streaming_content).startswith(b"%PDF-")
    file.close()
    again = remove(client_a, paper)
    assert again.json()["data"] == response.json()["data"]
    row.refresh_from_db()
    assert row.version == paper["version"] + 1
    assert (
        client_a.patch(
            f"/api/v1/papers/{paper['id']}",
            {"expected_version": row.version, "included": True},
            format="json",
        ).status_code
        == 404
    )


def test_delete_checks_ownership_and_authentication(client_a, client_b, api_client, project_a):
    paper = upload(client_a, project_a)
    assert remove(client_b, paper).status_code == 404
    assert remove(api_client, paper).status_code == 401
    assert Paper.objects.get(pk=paper["id"]).deleted_at is None


def test_database_rejects_deleted_but_included_paper(client_a, project_a):
    paper = upload(client_a, project_a)
    with pytest.raises(IntegrityError), transaction.atomic():
        Paper.objects.filter(pk=paper["id"]).update(deleted_at=timezone.now(), included=True)


def test_metadata_update_waits_for_project_without_locking_paper(client_a, project_a, user_a):
    """Real PostgreSQL interleaving: deletion commits while an editor waits.

    The editor must not hold the Paper lock before obtaining Project, otherwise
    a deletion holding Project can deadlock against it.
    """
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL locks and independent connections")
    paper = upload(client_a, project_a)
    actor = ActorContext(user=user_a, request_id="req_delete_race")
    project_a.refresh_from_db()
    before = project_a.corpus_revision
    ready = threading.Event()
    backend_pid = []

    def edit():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout TO '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid.append(cursor.fetchone()[0])
            ready.set()
            try:
                update_paper(actor, paper["id"], 1, {"title": "并发修改"})
            except ApiError as exc:
                return exc.code
            return "updated"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            lock_project(project_a.id)
            future = pool.submit(edit)
            assert ready.wait(timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        [backend_pid[0]],
                    )
                    row = cursor.fetchone()
                if row and row[0] == "Lock":
                    break
                time.sleep(0.02)
            else:
                pytest.fail("Concurrent editor did not reach the Project lock")
            # Fail immediately if the waiting editor already holds Paper.
            Paper.objects.select_for_update(nowait=True).get(pk=paper["id"])
            delete_paper(actor, paper["id"], 1)
        assert future.result(timeout=6) == "NOT_FOUND"
    row = Paper.objects.get(pk=paper["id"])
    project_a.refresh_from_db()
    assert row.deleted_at is not None and row.version == 2 and row.title == paper["title"]
    assert project_a.corpus_revision == before + 1


def test_delete_requires_csrf(client_a, csrf_client, user_a, project_a):
    paper = upload(client_a, project_a)
    csrf_client.force_login(user_a)
    assert remove(csrf_client, paper).status_code == 403
    token = csrf_client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
    response = csrf_client.delete(
        f"/api/v1/papers/{paper['id']}",
        {"expected_version": 1},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200, response.json()


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_delete_rejected_while_project_busy(client_a, project_a, user_a, status):
    paper = upload(client_a, project_a)
    Job.objects.create(
        project=project_a,
        requested_by=user_a,
        kind="import_arxiv",
        resource_type="import_batch",
        resource_id=uuid.uuid4(),
        input_hash="0" * 64,
        status=status,
    )
    response = remove(client_a, paper)
    assert response.status_code == 409 and response.json()["error"]["code"] == "PROJECT_BUSY"
    assert Paper.objects.get(pk=paper["id"]).deleted_at is None


def test_stale_version_and_invalid_body_do_not_delete(client_a, project_a):
    paper = upload(client_a, project_a)
    changed = client_a.patch(
        f"/api/v1/papers/{paper['id']}",
        {"expected_version": 1, "title": "编辑后的论文"},
        format="json",
    )
    assert changed.status_code == 200
    conflict = remove(client_a, paper)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    assert client_a.delete(f"/api/v1/papers/{paper['id']}", {}, format="json").status_code == 400
    assert remove(client_a, paper, unknown=True).status_code == 400
    assert Paper.objects.get(pk=paper["id"]).deleted_at is None


def test_delete_releases_capacity_and_reupload_restores_same_identity(client_a, project_a, settings):
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 1}
    paper = upload(client_a, project_a)
    assert remove(client_a, paper).status_code == 200
    restored = upload(client_a, project_a, title="新上传标题不能覆盖原元数据")
    assert restored["id"] == paper["id"] and restored["title"] == paper["title"]
    assert restored["included"] is True and restored["version"] == 3
    assert Paper.objects.count() == 1
    assert Paper.objects.get().deleted_at is None
    # A delayed delete from before restoration cannot remove the restored paper.
    assert remove(client_a, paper).status_code == 409
    assert remove(client_a, restored).status_code == 200
    new_paper = upload(client_a, project_a, pages=3)
    assert new_paper["id"] != paper["id"]
    full = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(2)},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert full.status_code == 422 and full.json()["error"]["code"] == "PAPER_LIMIT_EXCEEDED"
    assert Paper.objects.get(pk=paper["id"]).deleted_at is not None


def test_old_upload_key_does_not_undo_deletion(client_a, project_a):
    key = str(uuid.uuid4())
    paper = upload(client_a, project_a, key=key)
    assert remove(client_a, paper).status_code == 200
    assert upload(client_a, project_a, key=key) == paper
    assert Paper.objects.get(pk=paper["id"]).deleted_at is not None


def test_removed_paper_history_and_evidence_remain_readable(client_a, project_a, monkeypatch):
    from tests.test_m07_analysis import (
        _create_run,
        _extract_payload,
        _patch_extractor,
        _run_pipeline,
        _upload_paper,
    )

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    first = _upload_paper(client_a, project_a, name="first")
    _upload_paper(client_a, project_a, name="second")
    _patch_extractor(monkeypatch, _extract_payload)
    accepted = _create_run(client_a, project_a).json()["data"]
    run = _run_pipeline(accepted["job_id"])
    analysis_path = f"/api/v1/runs/{run.id}/papers/{first['id']}/analysis"
    before = client_a.get(analysis_path).json()["data"]
    assert remove(client_a, first).status_code == 200
    assert client_a.get(analysis_path).json()["data"] == before
    evidence = before["summary"]["evidence_ids"][0]
    assert client_a.get(f"/api/v1/runs/{run.id}/evidence/{evidence}").status_code == 200
    snapshot = client_a.get(f"/api/v1/runs/{run.id}").json()["data"]
    assert snapshot["is_stale"] is True
    assert any(p["paper_id"] == first["id"] for p in snapshot["papers"])
    rejected = _create_run(client_a, project_a)
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "INSUFFICIENT_PAPERS"
    assert AnalysisRun.objects.count() == 1


def test_arxiv_new_import_restores_deleted_paper(client_a, project_a, monkeypatch):
    from tests.test_literature_parsing import _arxiv_metadata, _fake_download

    from apps.jobs.services import claim_execution
    from apps.literature import services

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    monkeypatch.setattr(services.arxiv_client, "fetch_metadata", lambda _: _arxiv_metadata("2401.00001v2"))
    monkeypatch.setattr(services.arxiv_client, "download_pdf", _fake_download())

    def run_import():
        accepted = client_a.post(
            f"/api/v1/projects/{project_a.id}/imports/arxiv",
            {"arxiv_ids": ["2401.00001"]},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        ).json()["data"]
        context = claim_execution(accepted["job_id"])
        services.execute_import_arxiv(context, context.job_id)
        return Job.objects.get(pk=context.job_id)

    assert run_import().status == "succeeded"
    paper = Paper.objects.get()
    assert remove(client_a, {"id": str(paper.id), "version": 1}).status_code == 200
    assert run_import().status == "succeeded"
    paper.refresh_from_db()
    assert paper.deleted_at is None and paper.included is True and paper.version == 3
    assert Paper.objects.count() == 1
