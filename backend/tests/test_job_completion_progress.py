"""Completion progress is atomic with success and never invented on failure."""

import uuid

import pytest

from apps.jobs.models import Job
from apps.jobs.services import claim_execution, complete_job, fail_job, record_progress

pytestmark = pytest.mark.django_db(transaction=True)


def execution(project, user):
    job = Job.objects.create(project=project, requested_by=user, kind="import_arxiv",
                             resource_type="import_batch", resource_id=uuid.uuid4(), input_hash="0" * 64)
    context = claim_execution(job.id)
    record_progress(context, completed=1, total=3, stage="download", unit="step")
    return job, context


def test_success_completes_known_progress_with_resource_commit(project_a, user_a):
    job, context = execution(project_a, user_a)
    complete_job(context)
    job.refresh_from_db()
    assert job.status == "succeeded" and job.progress["completed"] == job.progress["total"] == 3


def test_failure_keeps_partial_progress(project_a, user_a):
    job, context = execution(project_a, user_a)
    fail_job(context, {"code": "INTERNAL_ERROR", "message": "test", "retryable": False})
    job.refresh_from_db()
    assert job.status == "failed" and job.progress["completed"] == 1


def test_resource_commit_failure_does_not_publish_complete_progress(project_a, user_a):
    job, context = execution(project_a, user_a)

    def reject(_):
        raise RuntimeError("resource publication failed")

    with pytest.raises(RuntimeError, match="resource publication failed"):
        complete_job(context, finalize_resource=reject)
    job.refresh_from_db()
    assert job.status == "running" and job.progress["completed"] == 1
