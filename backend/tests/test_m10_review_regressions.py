"""M10 验收问题回归：并发、冻结参数、篇幅、覆盖限制与真实网关重试。"""

import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connections, transaction
from tests import test_m10_reviews as m10_fixtures
from tests.test_m10_reviews import _create_review, _patch_sections, _section_payload

from apps.analysis.models import Dispute
from apps.analysis.services import review_dispute
from apps.common.auth import ActorContext
from apps.common.http import ApiError
from apps.common.locking import lock_project
from apps.jobs.models import Job
from apps.jobs.services import cancel_job, claim_execution, retry_job
from apps.reviews import services
from apps.reviews.models import Review, ReviewRevision
from apps.reviews.schemas import SectionsValidationError
from apps.reviews.structure import SECTION_KEYS

pytestmark = pytest.mark.django_db(transaction=True)
run_ready = m10_fixtures.run_ready
generated = m10_fixtures.generated


@pytest.fixture
def accepted(client_a, run_ready, monkeypatch):
    _patch_sections(monkeypatch)
    response = _create_review(client_a, run_ready)
    assert response.status_code == 202, response.json()
    data = response.json()["data"]
    context = claim_execution(data["job_id"])
    return Review.objects.get(pk=data["resource_id"]), context


def test_control_generation_commits_five_sections_and_job(accepted):
    review, context = accepted
    revision = services.generate_review(context, review.pk)
    assert [section["section_key"] for section in revision.sections] == list(SECTION_KEYS)
    assert review.revisions.count() == 1 and revision.revision_number == 1
    assert Job.objects.get(pk=context.job_id).status == "succeeded"


@pytest.mark.parametrize("invalid", ["cancelled", "replaced"])
def test_control_cancel_or_replaced_lease_cannot_commit(accepted, monkeypatch, user_a, invalid):
    review, context = accepted
    original = services._commit_revision

    def before_commit(*args, **kwargs):
        if invalid == "cancelled":
            cancel_job(ActorContext(user_a, "req_m10_cancel"), context.job_id)
        else:
            Job.objects.filter(pk=context.job_id).update(lease_token=context.lease_token + 1)
        return original(*args, **kwargs)

    monkeypatch.setattr(services, "_commit_revision", before_commit)
    with pytest.raises(ApiError) as error:
        services.generate_review(context, review.pk)
    assert error.value.code == "INVALID_JOB_STATE"
    assert not ReviewRevision.objects.filter(review=review).exists()


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
def test_manual_save_rejects_project_busy(client_a, generated, status):
    accepted = client_a.post(
        f"/api/v1/runs/{generated.run.pk}/retrievals",
        {"query": "attention accuracy"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert accepted.status_code == 202, accepted.json()
    Job.objects.filter(pk=accepted.json()["data"]["job_id"]).update(status=status)
    response = client_a.post(
        f"/api/v1/reviews/{generated.review.pk}/revisions",
        {"base_revision_number": 1, "sections": copy.deepcopy(generated.revision.sections)},
        format="json",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "PROJECT_BUSY"
    assert generated.review.revisions.count() == 1


def test_review_acceptance_freezes_revision_after_project_lock(client_a, run_ready, monkeypatch, user_a):
    papers = list(run_ready.run_papers.all())
    dispute = Dispute.objects.create(
        run=run_ready,
        left_paper=papers[0].paper,
        right_paper=papers[1].paper,
        left_claim={},
        right_claim={},
        conditions={},
        classification="possible_conflict",
        reason="test",
    )
    original = services.lock_project

    def commit_review_on_other_connection():
        try:
            review_dispute(
                ActorContext(user_a, "req_m10_race"), dispute.pk, 1, {"review_status": "confirmed"}
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def before_project_lock(*args, **kwargs):
            # 当前请求已读过 Run，尚未锁 Project；另一条正常复核请求先成功提交。
            pool.submit(commit_review_on_other_connection).result(timeout=4)
            return original(*args, **kwargs)

        monkeypatch.setattr(services, "lock_project", before_project_lock)
        response = _create_review(client_a, run_ready)
    assert response.status_code == 202, response.json()
    run_ready.refresh_from_db()
    assert run_ready.analysis_revision == 2
    review = Review.objects.get(pk=response.json()["data"]["resource_id"])
    assert review.analysis_revision == run_ready.analysis_revision, (
        "Project 锁前读取的旧版本被保存为新 Review 冻结版本"
    )


def test_target_chars_changes_generation_instruction(client_a, run_ready, monkeypatch):
    calls = []
    _patch_sections(monkeypatch, calls=calls)
    for target in (2000, 4000):
        accepted = _create_review(client_a, run_ready, target_chars=target)
        assert accepted.status_code == 202, accepted.json()
        context = claim_execution(accepted.json()["data"]["job_id"])
        services.generate_review(context, accepted.json()["data"]["resource_id"])
    assert len(calls) == 2 * len(SECTION_KEYS)
    first = [request.payload for request in calls[: len(SECTION_KEYS)]]
    second = [request.payload for request in calls[len(SECTION_KEYS) :]]
    assert first != second, "2000 与 4000 字目标产生完全相同的五节生成指令"


def test_internal_retrieval_uses_review_frozen_model_config(accepted, monkeypatch, settings):
    from apps.retrieval import services as retrieval_services

    review, context = accepted
    original = retrieval_services.call_structured
    calls = []

    def capture(ctx, request):
        calls.append(request)
        return original(ctx, request)

    monkeypatch.setattr(retrieval_services, "call_structured", capture)
    frozen_max = review.config_snapshot["model_config"]["max_tokens"]
    settings.LLM_MAX_OUTPUT_TOKENS = frozen_max + 1
    services.generate_review(context, review.pk)
    assert calls
    assert all(request.model_config["max_tokens"] == frozen_max for request in calls), (
        "生成冻结后，内部检索仍读取修改后的模型配置"
    )


def test_internal_rerank_logical_call_is_stable(accepted, monkeypatch):
    from apps.llm.schemas import TASK_RERANK
    from apps.retrieval import services as retrieval_services

    review, context = accepted
    original = retrieval_services.call_structured
    identifiers = []

    def capture(ctx, request):
        if request.task_type == TASK_RERANK:
            identifiers.append(request.logical_call_id)
        return original(ctx, request)

    monkeypatch.setattr(retrieval_services, "call_structured", capture)
    for _ in range(2):
        package = services._section_evidence(context, review.run, "background", review.config_snapshot)
        assert package["ids"]
    assert len(identifiers) == 2
    assert identifiers[0] == identifiers[1], "同一 Review 同一节重排使用随机逻辑调用 ID"


def test_generation_enforces_frozen_max_blocks(client_a, run_ready, monkeypatch, settings):
    settings.REVIEW_MAX_BLOCKS_PER_SECTION = 1
    _patch_sections(monkeypatch)  # 每节返回 2 块，五节总数仍小于全稿上限。
    response = _create_review(client_a, run_ready)
    assert response.status_code == 202
    review = Review.objects.get(pk=response.json()["data"]["resource_id"])
    assert review.config_snapshot["max_blocks_per_section"] == 1
    context = claim_execution(response.json()["data"]["job_id"])
    try:
        revision = services.generate_review(context, review.pk)
    except (ApiError, SectionsValidationError):
        assert not ReviewRevision.objects.filter(review=review).exists()
        return
    assert all(len(section["blocks"]) <= 1 for section in revision.sections), "单节超过冻结上限仍提交成功"


def test_no_evidence_section_can_publish_coverage_transition(accepted, monkeypatch):
    review, context = accepted
    original = services._section_evidence

    def empty_disagreements(ctx, run, key, config):
        if key == "theoretical_basis":
            return {"chunks": [], "ids": []}
        return original(ctx, run, key, config)

    monkeypatch.setattr(services, "_section_evidence", empty_disagreements)
    revision = services.generate_review(context, review.pk)
    section = next(item for item in revision.sections if item["section_key"] == "theoretical_basis")
    assert section["blocks"]
    assert all(block["kind"] == "transition" and not block["evidence_ids"] for block in section["blocks"])


def test_formal_job_retry_recovers_known_rate_limit_with_gateway(
    client_a, run_ready, monkeypatch, user_a, settings
):
    from apps.jobs.tasks import execute_job
    from apps.llm import client as gateway
    from apps.llm.client import ZERO_USAGE, ProviderFailure
    from apps.llm.models import ModelCall

    _patch_sections(monkeypatch)  # 内部检索仍用替身；生成经过真实网关和账本。
    monkeypatch.setattr(services, "call_structured", gateway.call_structured)
    pending_error = [True]

    def mock_response(request, case_name):
        key = request.payload["section_key"]
        if key == "achievements" and pending_error[0]:
            pending_error[0] = False
            # 已知零用量，Retry-After 超出单次等待上限，只发生一次网络尝试。
            raise ProviderFailure(
                "MODEL_RATE_LIMITED",
                retryable=True,
                usage=ZERO_USAGE,
                retry_after=settings.LLM_REQUEST_TIMEOUT_SECONDS + 1,
            )
        return {
            "id": "mock-m10-independent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            _section_payload(key, list(request.allowed_evidence_ids)), ensure_ascii=False
                        )
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
            },
        }

    monkeypatch.setattr(gateway, "_mock_response", mock_response)
    response = _create_review(client_a, run_ready)
    assert response.status_code == 202
    first_id = response.json()["data"]["job_id"]
    assert execute_job.run(first_id) == "failed"
    first = Job.objects.get(pk=first_id)
    assert (
        first.status == "failed" and first.error["code"] == "MODEL_RATE_LIMITED" and first.error["retryable"]
    )
    review_id = response.json()["data"]["resource_id"]
    assert not ReviewRevision.objects.filter(review_id=review_id).exists()
    logical = uuid.uuid5(uuid.UUID(review_id), "review_section:achievements")
    assert ModelCall.objects.filter(logical_call_id=logical).count() == 1
    retried = retry_job(ActorContext(user_a, "req_m10_retry"), first.pk, uuid.uuid4())
    outcome = execute_job.run(str(retried.job_id))
    second = Job.objects.get(pk=retried.job_id)
    assert outcome == "done" and second.status == "succeeded", second.error
    assert ReviewRevision.objects.filter(review_id=review_id).count() == 1
    assert list(
        ModelCall.objects.filter(logical_call_id=logical)
        .order_by("network_attempt")
        .values_list("network_attempt", flat=True)
    ) == [1, 2]


def test_formal_retry_cannot_reset_three_attempt_limit(client_a, run_ready, monkeypatch, user_a, settings):
    from apps.jobs.tasks import execute_job
    from apps.llm import client as gateway
    from apps.llm.client import ZERO_USAGE, ProviderFailure
    from apps.llm.models import ModelCall

    _patch_sections(monkeypatch)
    monkeypatch.setattr(services, "call_structured", gateway.call_structured)
    network_calls = []

    def limited(request, _case):
        network_calls.append(request.logical_call_id)
        raise ProviderFailure(
            "MODEL_RATE_LIMITED",
            retryable=True,
            usage=ZERO_USAGE,
            retry_after=settings.LLM_REQUEST_TIMEOUT_SECONDS + 1,
        )

    monkeypatch.setattr(gateway, "_mock_response", limited)
    accepted = _create_review(client_a, run_ready).json()["data"]
    job_id = accepted["job_id"]
    for attempt in range(1, 5):
        assert execute_job.run(str(job_id)) == "failed"
        job = Job.objects.get(pk=job_id)
        assert ModelCall.objects.count() == min(attempt, 3)
        if attempt < 4:
            job_id = retry_job(ActorContext(user_a, "req_m10_retry_limit"), job.pk, uuid.uuid4()).job_id
    assert len(network_calls) == 3 and len(set(network_calls)) == 1
    assert job.error["code"] == "INVALID_JOB_STATE" and not job.error["retryable"]
    assert not ReviewRevision.objects.exists()


def test_same_body_replays_when_defaults_change(client_a, run_ready, monkeypatch, settings):
    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    key = str(uuid.uuid4())
    first = _create_review(client_a, run_ready, key=key)
    assert first.status_code == 202
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "REVIEW_TARGET_CHARS_DEFAULT": 3500}
    second = _create_review(client_a, run_ready, key=key)
    assert second.status_code == 202 and second.json()["data"] == first.json()["data"]
    assert Review.objects.get(pk=first.json()["data"]["resource_id"]).target_chars == 3000


def test_retrieval_and_writing_parameters_stay_frozen(accepted, monkeypatch, settings):
    from apps.retrieval import services as retrieval_services

    review, context = accepted
    frozen = review.config_snapshot
    retrieval_calls, writing_calls = [], []
    original_retrieval = retrieval_services.call_structured
    original_writing = services.call_structured

    def capture_retrieval(ctx, request):
        retrieval_calls.append(request)
        return original_retrieval(ctx, request)

    def capture_writing(ctx, request):
        writing_calls.append(request)
        return original_writing(ctx, request)

    monkeypatch.setattr(retrieval_services, "call_structured", capture_retrieval)
    monkeypatch.setattr(services, "call_structured", capture_writing)
    monkeypatch.setattr(services, "SECTION_REQUIREMENTS", dict.fromkeys(SECTION_KEYS, "新部署的要求"))
    settings.RETRIEVAL_MAX_QUERIES = 1
    settings.RETRIEVAL_RERANK_MAX_CANDIDATES = 1
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "RETRIEVAL_QUERY_MAX": 1}
    services.generate_review(context, review.pk)
    assert len(writing_calls) == len(SECTION_KEYS)
    for request in writing_calls:
        key = request.payload["section_key"]
        assert request.payload["section_requirement"] == frozen["section_requirements"][key]
        assert request.payload["section_target_chars"] == frozen["section_target_chars"][key]
    assert sum(frozen["section_target_chars"].values()) == review.target_chars
    assert all(
        request.payload.get("current_query", request.payload.get("query"))
        in frozen["section_queries"].values()
        for request in retrieval_calls
    )
    assert all(
        request.payload["max_queries"] == frozen["retrieval_config"]["expand_query"]["max_queries"]
        for request in retrieval_calls
        if "max_queries" in request.payload
    )


def test_empty_package_does_not_bypass_template_check(accepted, monkeypatch):
    review, context = accepted
    snapshot = {**review.config_snapshot, "template_version": "wrong-template-version"}
    Review.objects.filter(pk=review.pk).update(config_snapshot=snapshot)
    monkeypatch.setattr(services, "_section_evidence", lambda *args: {"chunks": [], "ids": []})
    with pytest.raises(ApiError) as error:
        services.generate_review(context, review.pk)
    assert error.value.code == "INTERNAL_ERROR"
    assert not ReviewRevision.objects.exists()


def test_real_gateway_rejects_frozen_block_limit(client_a, run_ready, monkeypatch, settings):
    from apps.jobs.tasks import execute_job
    from apps.llm import client as gateway
    from apps.llm.models import ModelCall

    settings.REVIEW_MAX_BLOCKS_PER_SECTION = 1
    _patch_sections(monkeypatch)
    monkeypatch.setattr(services, "call_structured", gateway.call_structured)
    monkeypatch.setattr(gateway, "_pause", lambda *args: None)

    def over_limit(request, _case):
        payload = _section_payload(request.payload["section_key"], list(request.allowed_evidence_ids))
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    monkeypatch.setattr(gateway, "_mock_response", over_limit)
    accepted = _create_review(client_a, run_ready).json()["data"]
    assert execute_job.run(accepted["job_id"]) == "failed"
    assert Job.objects.get(pk=accepted["job_id"]).error["code"] == "MODEL_OUTPUT_INVALID"
    assert ModelCall.objects.count() == 2 and not ModelCall.objects.filter(outcome="succeeded").exists()
    assert not ReviewRevision.objects.exists()


def test_legacy_idempotency_replays_after_upgrade(client_a, run_ready, monkeypatch, settings):
    from apps.common.idempotency import request_fingerprint
    from apps.common.models import IdempotencyRecord

    monkeypatch.setattr("apps.jobs.services.dispatch", lambda job: None)
    key = str(uuid.uuid4())
    first = _create_review(client_a, run_ready, key=key)
    assert first.status_code == 202
    review = Review.objects.get(pk=first.json()["data"]["resource_id"])
    Review.objects.filter(pk=review.pk).update(config_snapshot={"version": "review-gen-v1"})
    legacy_hash = request_fingerprint(
        {
            "kind": "generate_review",
            "title": "注意力机制综述",
            "target_chars": 3000,
            "style": "standard",
        }
    )
    IdempotencyRecord.objects.filter(key=key).update(request_hash=legacy_hash)
    settings.MVP_LIMITS = {**settings.MVP_LIMITS, "REVIEW_TARGET_CHARS_DEFAULT": 3500}
    replayed = _create_review(client_a, run_ready, key=key)
    assert replayed.status_code == 202 and replayed.json()["data"] == first.json()["data"]
    for changed in ({"target_chars": 3500}, {"title": "changed title"}):
        conflict = _create_review(client_a, run_ready, key=key, **changed)
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert Review.objects.count() == 1
    assert IdempotencyRecord.objects.get(key=key).request_hash == legacy_hash


def test_cancel_under_project_lock_precedes_publication(accepted, monkeypatch, user_a):
    review, context = accepted
    project_locked, job_locked = Event(), Event()
    original_commit = services._commit_revision
    original_lock = services._lock_review_job
    futures = []

    def cancel_holding_project():
        try:
            with transaction.atomic():
                lock_project(review.project_id)
                project_locked.set()
                # 取消已取得项目写锁；发布按契约先等待 Project，不能先抢 Job 提交成功。
                job_locked.wait(timeout=0.2)
                return cancel_job(ActorContext(user_a, "req_m10_cancel_race"), context.job_id).status
        finally:
            connections.close_all()

    def notify_job_lock(*args):
        job = original_lock(*args)
        job_locked.set()
        return job

    with ThreadPoolExecutor(max_workers=1) as pool:

        def competing_commit(*args):
            futures.append(pool.submit(cancel_holding_project))
            assert project_locked.wait(timeout=4)
            return original_commit(*args)

        monkeypatch.setattr(services, "_commit_revision", competing_commit)
        monkeypatch.setattr(services, "_lock_review_job", notify_job_lock)
        error = None
        try:
            services.generate_review(context, review.pk)
        except ApiError as exc:
            error = exc
        assert futures[0].result(timeout=4) == "cancelling"
        assert error is not None and error.code == "INVALID_JOB_STATE"
    assert not ReviewRevision.objects.exists()


def test_cancel_remains_available_while_publication_holds_project(accepted, monkeypatch, user_a):
    review, context = accepted
    original_lock = services._lock_review_job

    def cancel_on_other_connection():
        try:
            return cancel_job(ActorContext(user_a, "req_m10_cancel_join"), context.job_id).status
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def cancel_before_job_lock(*args):
            # 发布已有 Project / Review 锁；取消只需 Job 锁，鉴权 JOIN 不应锁住 Project。
            assert pool.submit(cancel_on_other_connection).result(timeout=4) == "cancelling"
            return original_lock(*args)

        monkeypatch.setattr(services, "_lock_review_job", cancel_before_job_lock)
        with pytest.raises(ApiError) as error:
            services.generate_review(context, review.pk)
        assert error.value.code == "INVALID_JOB_STATE"
    assert not ReviewRevision.objects.exists()


def test_incomplete_source_quote_gets_one_bounded_repair_before_commit(accepted, monkeypatch):
    review, context = accepted
    initial = services.call_structured
    calls = []

    def incomplete_once(ctx, request):
        result = initial(ctx, request)
        calls.append(request)
        if request.payload["section_key"] == "background" and len(calls) == 1:
            payload = copy.deepcopy(result.payload)
            payload["blocks"][0]["text"] = payload["blocks"][0]["text"][1:]
            return type(result)(payload=payload, call_id=result.call_id)
        return result

    monkeypatch.setattr(services, "call_structured", incomplete_once)
    revision = services.generate_review(context, review.pk)
    background_calls = [r for r in calls if r.payload["section_key"] == "background"]
    assert len(background_calls) == 2
    assert background_calls[0].logical_call_id != background_calls[1].logical_call_id
    assert background_calls[1].logical_call_id == uuid.uuid5(
        review.id, "review_section:background:source_repair"
    )
    assert "quotable_passages" in background_calls[1].payload["section_requirement"]
    assert revision.revision_number == 1
    assert Job.objects.get(pk=context.job_id).status == "succeeded"


def test_repeated_source_failure_is_model_output_invalid_with_no_blank_revision(accepted, monkeypatch):
    review, context = accepted
    initial = services.call_structured
    calls = []

    def incomplete_always(ctx, request):
        result = initial(ctx, request)
        calls.append(request)
        payload = copy.deepcopy(result.payload)
        payload["blocks"][0]["text"] = payload["blocks"][0]["text"][1:]
        return type(result)(payload=payload, call_id=result.call_id)

    monkeypatch.setattr(services, "call_structured", incomplete_always)
    with pytest.raises(ApiError) as error:
        services.generate_review(context, review.pk)
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert len(calls) == 2
    assert not ReviewRevision.objects.filter(review=review).exists()
