"""Whole-project removal, history preservation and access/locking boundaries."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections, connection, connections, transaction
from tests.conftest import make_text_pdf_file

from apps.common.auth import ActorContext
from apps.common.http import ApiError
from apps.common.locking import lock_project
from apps.common.models import FileAsset
from apps.jobs.models import Job
from apps.jobs.tasks import execute_job
from apps.llm.models import ModelCall, UsageBudget
from apps.projects.models import Project
from apps.projects.services import delete_project, update_project
from apps.reviews.models import Export, ReviewRevision

pytestmark = pytest.mark.django_db(transaction=True)


def remove(client, project, **changes):
    body = {"expected_version": project.version, "expected_corpus_revision": project.corpus_revision}
    return client.delete(f"/api/v1/projects/{project.id}", {**body, **changes}, format="json")


def test_delete_project_hides_listing_and_is_idempotent(client_a, project_a, project_b):
    response = remove(client_a, project_a)
    assert response.status_code == 200
    assert response.json()["data"] == {"project_id": str(project_a.id), "deleted": True}
    project_a.refresh_from_db()
    deleted_at = project_a.deleted_at
    assert deleted_at is not None and project_a.version == 2
    listing = client_a.get("/api/v1/projects").json()
    assert listing["data"] == [] and listing["meta"]["total"] == 0
    assert remove(client_a, project_a, expected_version=1).json()["data"] == response.json()["data"]
    project_a.refresh_from_db()
    assert project_a.version == 2 and project_a.deleted_at == deleted_at
    assert Project.objects.count() == UsageBudget.objects.count() == 2
    assert Project.objects.get(pk=project_b.id).deleted_at is None
    with transaction.atomic():
        assert lock_project(project_a.id).pk == project_a.id  # Internal ledger settlement is retained.


def test_delete_project_ownership_and_session(client_a, client_b, api_client, project_a):
    assert remove(client_b, project_a).status_code == 404
    assert remove(api_client, project_a).status_code == 401
    assert remove(client_a, Project(id=uuid.uuid4(), version=1, corpus_revision=1)).status_code == 404
    project_a.refresh_from_db()
    assert project_a.deleted_at is None


def test_delete_project_requires_csrf(csrf_client, user_a, project_a):
    csrf_client.force_login(user_a)
    response = remove(csrf_client, project_a)
    assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_FAILED"
    token = csrf_client.get("/api/v1/auth/csrf").json()["data"]["csrf_token"]
    response = csrf_client.delete(
        f"/api/v1/projects/{project_a.id}",
        {"expected_version": 1, "expected_corpus_revision": 1},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expected_version": 1},
        {"expected_version": 0, "expected_corpus_revision": 1},
        {"expected_version": 1, "expected_corpus_revision": 0},
        {"expected_version": 1, "expected_corpus_revision": 1, "owner": "someone"},
    ],
)
def test_delete_rejects_invalid_contract(client_a, project_a, body):
    assert client_a.delete(f"/api/v1/projects/{project_a.id}", body, format="json").status_code == 400
    project_a.refresh_from_db()
    assert project_a.deleted_at is None


@pytest.mark.parametrize("field", ["expected_version", "expected_corpus_revision"])
def test_delete_checks_project_and_material_versions(client_a, project_a, field):
    response = remove(client_a, project_a, **{field: 2})
    assert response.status_code == 409 and response.json()["error"]["code"] == "VERSION_CONFLICT"
    project_a.refresh_from_db()
    assert project_a.deleted_at is None and project_a.version == 1


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_active_task_blocks_project_deletion(client_a, project_a, user_a, status):
    job = Job.objects.create(
        project=project_a,
        requested_by=user_a,
        kind="analyze",
        resource_type="analysis_run",
        resource_id=uuid.uuid4(),
        status=status,
        input_hash="test",
    )
    response = remove(client_a, project_a)
    assert response.status_code == 409 and response.json()["error"]["code"] == "PROJECT_BUSY"
    assert response.json()["error"]["details"]["active_job_id"] == str(job.id)
    project_a.refresh_from_db()
    assert project_a.deleted_at is None


def test_deleted_project_closes_all_resource_and_file_links(client_a, project_a, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    assert (
        client_a.patch(
            f"/api/v1/projects/{project_a.id}",
            {"expected_version": 1, "research_question": "Does Attention improve small sample accuracy?"},
            format="json",
        ).status_code
        == 200
    )
    papers = []
    for index in range(2):
        response = client_a.post(
            f"/api/v1/projects/{project_a.id}/papers/upload",
            {
                "file": make_text_pdf_file(
                    ["Introduction\n" + f"Attention model {index} improves small sample accuracy. " * 30],
                    name=f"source-{index}.pdf",
                )
            },
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert response.status_code == 201
        papers.append(response.json()["data"])

    def run_task(path, body):
        response = client_a.post(
            f"/api/v1{path}", body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        assert response.status_code == 202, response.json()
        accepted = response.json()["data"]
        execute_job(accepted["job_id"])
        job = Job.objects.get(pk=accepted["job_id"])
        assert job.status == "succeeded", job.error
        return str(accepted["resource_id"]), str(job.id)

    run, job = run_task(f"/projects/{project_a.id}/runs", {})
    retrieval, _ = run_task(f"/runs/{run}/retrievals", {"query": "Attention accuracy", "top_k": 5})
    review, _ = run_task(f"/runs/{run}/reviews", {"title": "Project removal", "target_chars": 2000})
    revision = ReviewRevision.objects.get(review_id=review)
    validation, _ = run_task(f"/reviews/{review}/validations", {"revision_id": str(revision.id)})
    export, _ = run_task(
        f"/reviews/{review}/exports",
        {
            "revision_id": str(revision.id),
            "validation_id": validation,
            "acknowledge_warnings": True,
        },
    )
    evidence = revision.sections[0]["blocks"][0]["evidence_ids"][0]
    word = Export.objects.select_related("file_asset").get(pk=export)
    project_a.refresh_from_db()
    asset_count, call_count = FileAsset.objects.count(), ModelCall.objects.count()
    assert call_count > 0
    assert remove(client_a, project_a).status_code == 200

    paths = [
        f"/projects/{project_a.id}",
        *[f"/projects/{project_a.id}/{suffix}" for suffix in ("papers", "runs", "reviews", "jobs")],
        f"/papers/{papers[0]['id']}",
        papers[0]["file_url"].removeprefix("/api/v1"),
        f"/runs/{run}",
        f"/runs/{run}/topics",
        f"/runs/{run}/disputes",
        f"/runs/{run}/papers/{papers[0]['id']}/analysis",
        f"/runs/{run}/papers/{papers[0]['id']}/evidence",
        f"/runs/{run}/evidence/{evidence}",
        f"/runs/{run}/retrievals",
        f"/retrievals/{retrieval}",
        f"/reviews/{review}",
        f"/reviews/{review}/revisions",
        f"/reviews/{review}/revisions/{revision.id}",
        f"/reviews/{review}/validations",
        f"/reviews/{review}/exports",
        f"/validations/{validation}",
        f"/exports/{export}",
        f"/exports/{export}/file",
        f"/jobs/{job}",
    ]
    for path in paths:
        response = client_a.get(f"/api/v1{path}")
        assert response.status_code == 404, (path, response.json())
    writes = [
        ("patch", f"/projects/{project_a.id}", {"expected_version": 2, "title": "changed"}),
        ("patch", f"/papers/{papers[0]['id']}", {"expected_version": 1, "title": "changed"}),
        ("delete", f"/papers/{papers[0]['id']}", {"expected_version": 1}),
        ("post", f"/projects/{project_a.id}/runs", {}),
        ("post", f"/projects/{project_a.id}/imports/arxiv", {"arxiv_ids": ["2401.12345"]}),
        ("post", f"/runs/{run}/retrievals", {"query": "Attention accuracy"}),
        ("post", f"/runs/{run}/reviews", {"title": "new", "target_chars": 2000}),
        ("post", f"/reviews/{review}/revisions", {"base_revision_number": 1, "sections": revision.sections}),
        ("post", f"/reviews/{review}/validations", {"revision_id": str(revision.id)}),
        (
            "post",
            f"/reviews/{review}/exports",
            {"revision_id": str(revision.id), "validation_id": validation, "acknowledge_warnings": True},
        ),
        ("post", f"/jobs/{job}/cancel", {}),
        ("post", f"/jobs/{job}/retry", {}),
    ]
    for method, path, body in writes:
        response = getattr(client_a, method)(
            f"/api/v1{path}", body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        assert response.status_code == 404, (path, response.json())
    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_text_pdf_file(["no resurrection"])},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 404
    assert FileAsset.objects.count() == asset_count and ModelCall.objects.count() == call_count
    assert Export.objects.get(pk=word.pk).file_asset_id == word.file_asset_id
    assert ReviewRevision.objects.get(pk=revision.id).sections == revision.sections


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires real PostgreSQL row locks")
def test_delete_serializes_with_edit_and_blocks_the_waiting_writer(project_a, user_a):
    waiting = threading.Event()
    actor = ActorContext(user=user_a, request_id="delete-lock-test")

    def writer():
        close_old_connections()

        def observe(execute, sql, params, many, context):
            if "FOR UPDATE" in sql:
                waiting.set()
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(observe):
                update_project(actor, project_a.id, 1, {"title": "must not be written"})
        except ApiError as exc:
            return exc.code
        finally:
            connections.close_all()
        return "updated"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            lock_project(project_a.id, owner=user_a)
            future = pool.submit(writer)
            assert waiting.wait(timeout=5)
            assert not future.done()
            delete_project(actor, project_a.id, 1, 1)
        assert future.result(timeout=5) == "NOT_FOUND"
    project_a.refresh_from_db()
    assert project_a.deleted_at is not None and project_a.title == "项目 A"
