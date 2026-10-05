"""30-paper capacity: real PDF/DB/gateway mock, complete aggregation and resumable failures."""

import json
import uuid
from collections import Counter
from itertools import combinations
from types import SimpleNamespace

import pytest
from tests.conftest import make_text_pdf_file

from apps.analysis import services
from apps.analysis.batching import comparison_batches, paper_batches
from apps.analysis.models import AnalysisRun, PaperAnalysis, Topic
from apps.common.auth import ActorContext
from apps.common.http import ApiError, api_error
from apps.jobs.models import Job
from apps.jobs.services import claim_execution, retry_job
from apps.llm.models import ModelCall
from apps.llm.schemas import TASK_ASSIGN_TOPICS, TASK_COMPARE_DISPUTES, TASK_PAPER_EXTRACT

pytestmark = pytest.mark.django_db(transaction=True)


def _upload(client, project, number):
    return client.post(
        f"/api/v1/projects/{project.pk}/papers/upload",
        {
            "file": make_text_pdf_file(
                [f"Paper {number}. Attention improves accuracy on a benchmark. We compare two methods."],
                name=f"paper-{number}.pdf",
            )
        },
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )


@pytest.fixture
def thirty(client_a, project_a, user_a, monkeypatch):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    ids = []
    for number in range(30):
        response = _upload(client_a, project_a, number)
        assert response.status_code == 201, response.json()
        ids.append(response.json()["data"]["id"])
    return SimpleNamespace(client=client_a, project=project_a, user=user_a, ids=ids)


def _start(bundle):
    response = bundle.client.post(
        f"/api/v1/projects/{bundle.project.pk}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 202, response.json()
    data = response.json()["data"]
    return AnalysisRun.objects.get(pk=data["resource_id"]), claim_execution(data["job_id"])


def _record(monkeypatch, transform=None):
    actual = services.call_structured
    requests = []

    def record(context, request):
        requests.append(request)
        if transform:
            response = transform(context, request)
            if response is not None:
                return response
        return actual(context, request)

    monkeypatch.setattr(services, "call_structured", record)
    return requests


def test_thirty_real_pdfs_complete_every_stage_and_cross_batch_comparison(thirty, monkeypatch):
    requests = _record(monkeypatch)
    run, context = _start(thirty)
    result = services.execute_run(context, run.pk)
    result.refresh_from_db()
    assert result.status == "ready", result.error
    assert PaperAnalysis.objects.filter(run_paper__run=run, run_paper__status="ready").count() == 30
    job = Job.objects.get(pk=context.job_id)
    assert job.status == "succeeded"
    assert job.progress["completed"] == job.progress["total"] == 30
    detail = thirty.client.get(f"/api/v1/runs/{run.pk}").json()["data"]
    assert len(detail["papers"]) == 30
    assert detail["stats"]["ready_count"] == detail["stats"]["included_count"] == 30
    assert {str(pk) for topic in Topic.objects.filter(run=run) for pk in topic.paper_ids} == set(thirty.ids)
    topics = [req for req in requests if req.task_type == TASK_ASSIGN_TOPICS]
    assert len(topics) == 5  # 8 + 8 + 8 + 6 papers, then a complete merge.
    assert [len(req.allowed_paper_ids) for req in topics[:-1]] == [8, 8, 8, 6]
    assert set(topics[-1].allowed_paper_ids) == set(thirty.ids)
    assert len(json.loads(topics[-1].payload["paper_analyses"])["batch_results"]) == 4
    comparisons = [req for req in requests if req.task_type == TASK_COMPARE_DISPUTES]
    assert all(2 <= len(req.allowed_paper_ids) <= 8 for req in comparisons)
    covered = {pair for req in comparisons for pair in combinations(sorted(req.allowed_paper_ids), 2)}
    assert covered == set(combinations(sorted(thirty.ids), 2))  # All 435 within-topic pairs.
    assert len({req.logical_call_id for req in comparisons}) == len(comparisons)
    assert ModelCall.objects.filter(project=thirty.project).exists()
    assert not ModelCall.objects.filter(project=thirty.project).exclude(estimated_cost_usd=0).exists()
    first = thirty.client.get(f"/api/v1/projects/{thirty.project.pk}/papers").json()["data"]
    second = thirty.client.get(f"/api/v1/projects/{thirty.project.pk}/papers?page=2").json()["data"]
    assert len(first) == 20 and len(second) == 10
    last = thirty.client.get(f"/api/v1/runs/{run.pk}/papers/{thirty.ids[-1]}/analysis")
    assert last.status_code == 200


def test_thirty_limit_rejects_new_file_but_reuses_existing_file(thirty):
    response = _upload(thirty.client, thirty.project, 30)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PAPER_LIMIT_EXCEEDED"
    repeat = _upload(thirty.client, thirty.project, 29)
    assert repeat.status_code in (200, 201)
    assert repeat.json()["data"]["id"] == thirty.ids[-1]
    assert thirty.project.papers.filter(deleted_at__isnull=True).count() == 30


def test_run_acceptance_checks_upper_bound_even_if_project_already_over_limit(thirty, settings):
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "PAPERS_PER_PROJECT_MAX": 29}
    response = thirty.client.post(
        f"/api/v1/projects/{thirty.project.pk}/runs",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PAPER_LIMIT_EXCEEDED"
    assert not AnalysisRun.objects.filter(project=thirty.project).exists()


def test_thirty_retry_preserves_twenty_nine_completed_analyses(thirty, monkeypatch):
    failed_id = thirty.ids[-2]
    failed_once = False

    def fail_one(context, request):
        nonlocal failed_once
        if (
            request.task_type == TASK_PAPER_EXTRACT
            and request.allowed_paper_ids == [failed_id]
            and not failed_once
        ):
            failed_once = True
            raise api_error("MODEL_TIMEOUT", "Injected one-paper timeout", retryable=True)

    requests = _record(monkeypatch, fail_one)
    run, context = _start(thirty)
    first = services.execute_run(context, run.pk)
    assert first.status == "failed", first.error
    old_ids = set(PaperAnalysis.objects.filter(run_paper__run=run).values_list("pk", flat=True))
    assert len(old_ids) == 29
    accepted = retry_job(ActorContext(thirty.user, "capacity-retry"), context.job_id, uuid.uuid4())
    second = services.execute_run(claim_execution(accepted.job_id), run.pk)
    assert second.status == "ready", second.error
    new_ids = set(PaperAnalysis.objects.filter(run_paper__run=run).values_list("pk", flat=True))
    assert len(new_ids) == 30 and old_ids < new_ids
    counts = Counter(req.allowed_paper_ids[0] for req in requests if req.task_type == TASK_PAPER_EXTRACT)
    assert counts[failed_id] == 2
    assert all(counts[pk] == 1 for pk in thirty.ids if pk != failed_id)


def test_batch_topic_omission_fails_without_publishing_partial_topics(thirty, monkeypatch):
    def omit_paper(context, request):
        if request.task_type == TASK_ASSIGN_TOPICS:
            return SimpleNamespace(
                payload={
                    "topics": [
                        {
                            "name": "Incomplete",
                            "description": "",
                            "paper_ids": request.allowed_paper_ids[:-1],
                            "evidence_ids": [],
                        }
                    ],
                    "unassigned_paper_ids": [],
                }
            )

    _record(monkeypatch, omit_paper)
    run, context = _start(thirty)
    result = services.execute_run(context, run.pk)
    assert result.status == "failed"
    assert result.error["code"] == "MODEL_OUTPUT_INVALID"
    assert PaperAnalysis.objects.filter(run_paper__run=run).count() == 30
    assert not Topic.objects.filter(run=run).exists()


@pytest.mark.parametrize("capacity", [2, 3, 8, 30])
def test_pair_batches_cover_all_thirty_papers_and_pairs_deterministically(capacity):
    members = list(range(30))
    batches = comparison_batches(members, 30, lambda ids: len(ids) <= capacity)
    assert batches == comparison_batches(members, 30, lambda ids: len(ids) <= capacity)
    assert all(2 <= len(batch) <= capacity for batch in batches)
    assert {pair for batch in batches for pair in combinations(batch, 2)} == set(combinations(members, 2))


def test_oversized_pair_is_rejected_instead_of_silently_omitted():
    with pytest.raises(ApiError, match="不能省略文献"):
        comparison_batches(["a", "b", "c"], 8, lambda ids: not {"b", "c"}.issubset(ids))


def test_topic_batches_shrink_to_fit_without_truncating_or_dropping_items():
    items = ["a" * 4, "b" * 3, "c" * 6, "d" * 2]
    batches = paper_batches(items, 8, lambda batch: sum(map(len, batch)) <= 9)
    assert [item for batch in batches for item in batch] == items
    assert [len(batch) for batch in batches] == [2, 2]
    with pytest.raises(ApiError, match="不能截断"):
        paper_batches(items, 8, lambda batch: sum(map(len, batch)) <= 5)


def test_thirty_dispute_retry_keeps_batch_call_identities_and_successful_analyses(thirty, monkeypatch):
    count = 0
    failed_once = False

    def fail_comparison(context, request):
        nonlocal count, failed_once
        if request.task_type == TASK_COMPARE_DISPUTES:
            count += 1
            if count == 3 and not failed_once:
                failed_once = True
                raise api_error("MODEL_TIMEOUT", "Injected comparison timeout", retryable=True)

    requests = _record(monkeypatch, fail_comparison)
    run, context = _start(thirty)
    first = services.execute_run(context, run.pk)
    assert first.status == "failed", first.error
    original_ids = set(PaperAnalysis.objects.filter(run_paper__run=run).values_list("pk", flat=True))
    first_calls = [req for req in requests if req.task_type == TASK_COMPARE_DISPUTES]
    assert len(first_calls) == 3
    requests.clear()
    accepted = retry_job(ActorContext(thirty.user, "capacity-compare-retry"), context.job_id, uuid.uuid4())
    result = services.execute_run(claim_execution(accepted.job_id), run.pk)
    assert result.status == "ready", result.error
    second_calls = [req for req in requests if req.task_type == TASK_COMPARE_DISPUTES]
    assert [req.logical_call_id for req in second_calls[:3]] == [req.logical_call_id for req in first_calls]
    assert original_ids == set(PaperAnalysis.objects.filter(run_paper__run=run).values_list("pk", flat=True))
    assert not [req for req in requests if req.task_type == TASK_PAPER_EXTRACT]
