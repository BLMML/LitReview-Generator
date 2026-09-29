"""显式启用的真实 Redis + Celery worker + PostgreSQL + mock 网关闭环。"""

import os
import time
import uuid
from dataclasses import asdict

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from django.db import connections

from apps.jobs.models import Job
from apps.jobs.services import claim_execution, complete_job
from apps.llm.client import call_structured
from apps.llm.models import ModelCall
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import ModelTaskRequest

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("RUN_M04_REDIS_TEST") != "1",
        reason="设置 RUN_M04_REDIS_TEST=1 执行独立 Redis 队列验证",
    ),
]


def test_worker_executes_mock_gateway_and_persists_result(project_a, user_a, settings):
    queue = f"m04_test_{uuid.uuid4().hex}"
    app = Celery("m04_test", broker=settings.CELERY_BROKER_URL)
    app.conf.update(
        task_default_queue=queue, task_ignore_result=True, broker_connection_retry_on_startup=False
    )
    job = Job.objects.create(
        project=project_a,
        requested_by=user_a,
        kind="import_arxiv",
        resource_type="import_batch",
        resource_id=uuid.uuid4(),
        input_hash="0" * 64,
    )
    payload = {
        "research_question": "attention",
        "current_query": "attention",
        "domain_context": "learning",
        "max_queries": 3,
    }
    prompt = load_prompt("expand_query.md", payload, task_type="query_expand")
    request = ModelTaskRequest("query_expand", prompt.version, "1.0", {}, payload)

    @app.task(name="m04_test.gateway", ignore_result=True)
    def probe(job_id, request_dict):
        try:
            context = claim_execution(uuid.UUID(job_id))
            result = call_structured(context, ModelTaskRequest(**request_dict))
            complete_job(context, {"payload": result.payload, "call_id": result.call_id})
        finally:
            connections.close_all()

    try:
        with start_worker(
            app,
            pool="solo",
            concurrency=1,
            queues=[queue],
            perform_ping_check=False,
            shutdown_timeout=10,
            loglevel="WARNING",
        ):
            probe.apply_async(args=[str(job.pk), asdict(request)], queue=queue)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                job.refresh_from_db()
                if job.status in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.1)
            assert job.status == "succeeded"
            assert job.result["payload"] == {"queries": ["attention"]}
            call = ModelCall.objects.get(pk=job.result["call_id"])
            assert call.job_id == job.pk and call.outcome == "succeeded"
            assert call.estimated_cost_usd == 0
    finally:
        with app.connection_for_write() as conn:
            conn.default_channel.queue_delete(queue)
        app.close()
