"""供应商 HTTP 替身与完整 M04 网关；无任何真实 API 请求。"""

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest
from django.conf import settings as django_settings
from django.utils import timezone

from apps.common.http import ApiError
from apps.jobs import services
from apps.jobs.models import Job
from apps.llm import client
from apps.llm.models import ModelCall, UsageBudget
from apps.llm.pricing import cost, pricing_snapshot
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import (
    ModelTaskRequest,
    ModelUsage,
    normalize_response,
    normalize_usage,
    validate_payload,
)

pytestmark = pytest.mark.django_db(transaction=True)
EVIDENCE = [f"{i * 8}-{i * 4}-4{i * 3}-8{i * 3}-{i * 12}" for i in ("1", "2", "3")]
PAPERS = ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]


@pytest.fixture
def context(project_a, user_a):
    job = Job.objects.create(
        project=project_a,
        requested_by=user_a,
        kind="import_arxiv",
        resource_type="import_batch",
        resource_id=uuid.uuid4(),
        input_hash="0" * 64,
    )
    return services.claim_execution(job.pk)


@pytest.fixture
def request_data():
    payload = {
        "research_question": "attention",
        "current_query": "attention",
        "domain_context": "learning",
        "max_queries": 3,
    }
    prompt = load_prompt("expand_query.md", payload, task_type="query_expand")
    return ModelTaskRequest("query_expand", prompt.version, "1.0", {}, payload)


@pytest.fixture
def real_mode(settings, monkeypatch):
    settings.LLM_MODE = "real"
    settings.DEEPSEEK_API_KEY = "test-only-not-a-real-key"
    settings.LLM_PRICING_VERSION = "test-only"
    settings.LLM_PRICING_CALENDAR_YEAR = timezone.now().year
    settings.LLM_PRICING_OFF_PEAK_DATES_UTC = [f"{timezone.now().year}-01-01"]
    settings.PROJECT_DEFAULT_BUDGET_USD = Decimal("10")
    UsageBudget.objects.update(limit_usd=Decimal("10"))
    settings.LLM_PRICING = {
        band: {
            "input_cache_hit_per_million_usd": Decimal("0.01") * factor,
            "input_cache_miss_per_million_usd": Decimal("0.3") * factor,
            "output_per_million_usd": Decimal("1.2") * factor,
        }
        for band, factor in (("peak", 1), ("off_peak", Decimal("0.5")))
    }
    monkeypatch.setattr(client, "_pause", Mock())


def response(payload=None, *, finish="stop", usage=True):
    result = {
        "id": "provider-test",
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": json.dumps(payload or {"queries": ["attention"]})},
            }
        ],
    }
    if usage:
        result["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 40,
            "prompt_cache_miss_tokens": 60,
            "completion_tokens_details": {"reasoning_tokens": 10},
        }
    return result


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        # 拦截 AsyncClient 创建，测试无法绕开 MockTransport 发出网络请求。
        factory = Mock(
            side_effect=lambda: httpx.AsyncClient(
                base_url="https://api.deepseek.com/",
                transport=httpx.MockTransport(handler),
                headers={"Authorization": "Bearer test-only-not-a-real-key"},
                follow_redirects=False,
            )
        )
        monkeypatch.setattr(client, "_http_client", factory)
        return factory

    return install


def test_usage_normalization_does_not_double_count_reasoning():
    usage = normalize_usage(response()["usage"])
    assert usage.input_tokens == 100 and usage.output_tokens == 20
    assert usage.cache_hit_tokens == 40 and usage.cache_miss_tokens == 60
    assert normalize_usage(None).input_tokens is None


@pytest.mark.parametrize(
    "usage",
    [{"prompt_tokens": -1}, {"prompt_tokens": True}, {"completion_tokens": "12"}, {"prompt_tokens": 1e50}],
)
def test_invalid_usage_is_unknown(usage):
    result = normalize_usage(usage)
    assert result.input_tokens is None and result.output_tokens is None


def test_inconsistent_cache_usage_is_unknown():
    usage = normalize_usage(
        {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 9,
            "prompt_cache_miss_tokens": 9,
            "completion_tokens": 2,
        }
    )
    assert usage.cache_hit_tokens is None and usage.cache_miss_tokens is None


@pytest.mark.parametrize(
    "content,finish",
    [
        ("", "stop"),
        ("{}", "length"),
        ("[]", "stop"),
        ('{"a": NaN}', "stop"),
        ('{"a":1,"a":2}', "stop"),
        ("```json\n{}\n```", "stop"),
    ],
)
def test_invalid_completion_is_rejected(content, finish):
    raw = response()
    raw["choices"][0] = {"finish_reason": finish, "message": {"content": content}}
    with pytest.raises(ApiError) as error:
        normalize_response(raw)
    assert error.value.code == "MODEL_OUTPUT_INVALID"


CASES = json.loads((django_settings.BASE_DIR / "tests/fixtures/llm_cases.json").read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_existing_provider_fixtures_are_actually_validated(case):
    kind = "query_expand" if case["task_type"] == "any" else case["task_type"]
    blocks = case.get("payload", {}).get("blocks", []) if kind == "verify_blocks" else []
    req = ModelTaskRequest(kind, "fixture", "1.0", {}, {"blocks_with_evidence": blocks}, EVIDENCE, PAPERS)
    if case["expected"] == "accepted":
        assert isinstance(client.call_structured_mock(req, case["name"]).payload, dict)
    else:
        with pytest.raises(ApiError) as error:
            client.call_structured_mock(req, case["name"])
        assert error.value.code == case.get("job_error_code", "MODEL_OUTPUT_INVALID")


def test_mock_uses_ledger_and_cache_without_network(context, request_data, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(client, "_http_client", forbidden)
    first = client.call_structured(context, request_data)
    cached = client.call_structured(context, request_data)
    assert first.call_id == cached.call_id
    assert ModelCall.objects.count() == 1 and not forbidden.called
    assert ModelCall.objects.get().pricing_snapshot["mock"] is True
    assert UsageBudget.objects.get().settled_usd == 0


def test_real_gateway_sends_json_and_settles(context, request_data, real_mode, transport):
    def provider(request):
        assert request.url.path == "/chat/completions"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert body["stream"] is False
        return httpx.Response(200, json=response())

    transport(provider)
    result = client.call_structured(context, request_data)
    call = ModelCall.objects.get()
    assert result.payload == {"queries": ["attention"]}
    assert call.outcome == "succeeded" and call.raw_output_asset is not None
    assert call.output_tokens == 20 and call.settled_at is not None
    assert UsageBudget.objects.get().reserved_usd == 0


def test_one_format_repair_counted_as_second_network_attempt(context, request_data, real_mode, transport):
    answers = [response({"unexpected": True}), response()]
    transport(lambda req: httpx.Response(200, json=answers.pop(0)))
    client.call_structured(context, request_data)
    calls = list(ModelCall.objects.order_by("network_attempt"))
    assert [c.outcome for c in calls] == ["failed", "succeeded"]
    assert calls[0].logical_call_id == calls[1].logical_call_id
    assert UsageBudget.objects.get().settled_usd == sum(c.estimated_cost_usd for c in calls)


def test_second_bad_format_fails_instead_of_unlimited_repair(context, request_data, real_mode, transport):
    factory = transport(lambda req: httpx.Response(200, json=response({"unknown": True})))
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert factory.call_count == ModelCall.objects.count() == 2


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "MODEL_AUTH_FAILED"),
        (402, "MODEL_BALANCE_INSUFFICIENT"),
        (400, "MODEL_OUTPUT_INVALID"),
        (422, "MODEL_OUTPUT_INVALID"),
    ],
)
def test_nonretryable_http_errors_not_retried(context, request_data, real_mode, transport, status, code):
    factory = transport(lambda req: httpx.Response(status, text="secret-provider-detail"))
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == code and factory.call_count == 1
    assert "secret-provider-detail" not in json.dumps(ModelCall.objects.get().error)
    assert UsageBudget.objects.get().reserved_usd == 0


def test_rate_limit_honors_retry_after(context, request_data, real_mode, transport):
    answers = [httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200, json=response())]
    transport(lambda req: answers.pop(0))
    client.call_structured(context, request_data)
    assert client._pause.call_args.args[1] >= 4
    assert ModelCall.objects.count() == 2


def test_timeout_has_bounded_attempts_and_keeps_unknown_cost(
    context,
    request_data,
    real_mode,
    transport,
    settings,
):
    settings.LLM_MAX_CONCURRENCY = 3

    def timeout(request):
        raise httpx.ReadTimeout("secret-timeout-context")

    factory = transport(timeout)
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == "MODEL_TIMEOUT"
    assert factory.call_count == ModelCall.objects.count() == 3
    assert set(ModelCall.objects.values_list("outcome", flat=True)) == {"unsettled"}
    assert UsageBudget.objects.get().reserved_usd > 0


def test_wall_clock_timeout_cancels_slow_http(context, request_data, real_mode, transport, settings):
    settings.LLM_REQUEST_TIMEOUT_SECONDS = 0.02
    settings.LLM_MAX_ATTEMPTS = 1

    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json=response())

    transport(slow)
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == "MODEL_TIMEOUT"


def test_provider_failure_never_falls_back_to_mock(
    context,
    request_data,
    real_mode,
    transport,
    monkeypatch,
    settings,
):
    settings.LLM_MAX_CONCURRENCY = 3
    transport(lambda req: httpx.Response(503))
    mock = Mock(side_effect=AssertionError("must not fall back"))
    monkeypatch.setattr(client, "_mock_response", mock)
    with pytest.raises(ApiError):
        client.call_structured(context, request_data)
    assert not mock.called and ModelCall.objects.count() == 3


def test_missing_usage_preserves_reservation(context, request_data, real_mode, transport):
    transport(lambda req: httpx.Response(200, json=response(usage=False)))
    result = client.call_structured(context, request_data)
    assert result.estimated_cost_usd is None and result.usage.input_tokens is None
    assert ModelCall.objects.get().outcome == "unsettled"
    assert UsageBudget.objects.get().reserved_usd > 0


def test_cancellation_during_http_records_cost_without_cache(context, request_data, real_mode, transport):
    def provider(request):
        # MockTransport 的异步线程不能直接运行 ORM；返回前由同步网关替身模拟取消。
        return httpx.Response(200, json=response())

    transport(provider)
    original = client._provider_call

    def cancel(body):
        raw = original(body)
        Job.objects.filter(pk=context.job_id).update(status="cancelling", cancel_requested_at=timezone.now())
        return raw

    from unittest.mock import patch

    with patch.object(client, "_provider_call", cancel), pytest.raises(ApiError):
        client.call_structured(context, request_data)
    call = ModelCall.objects.get()
    assert call.estimated_cost_usd > 0 and call.raw_output_asset is None
    assert not ModelCall.objects.filter(outcome="succeeded").exists()


def test_cache_key_changes_with_input(context, request_data):
    client.call_structured(context, request_data)
    changed = replace(request_data, payload={**request_data.payload, "domain_context": "new domain"})
    client.call_structured(context, changed)
    assert ModelCall.objects.count() == 2


@pytest.mark.parametrize(
    "at,tariff",
    [
        ("2026-09-21T01:00:00+00:00", "peak"),
        ("2026-09-21T04:00:00+00:00", "off_peak"),
        ("2026-09-21T06:00:00+00:00", "peak"),
        ("2026-09-21T10:00:00+00:00", "off_peak"),
        ("2026-09-20T02:00:00+00:00", "off_peak"),
    ],
)
def test_peak_off_peak_boundaries(real_mode, settings, at, tariff):
    settings.LLM_PRICING_CALENDAR_YEAR = 2026
    snapshot = pricing_snapshot(mock=False, at=datetime.fromisoformat(at))
    assert snapshot["tariff"] == tariff
    # 任意档位预留一律峰价，不把输入缓存折扣算进预留。
    assert cost(ModelUsage(1_000_000, 1_000_000), snapshot, reserve=True) == Decimal("1.5")


def test_holiday_is_off_peak_and_calendar_is_frozen(real_mode, settings):
    settings.LLM_PRICING_CALENDAR_YEAR = 2026
    settings.LLM_PRICING_OFF_PEAK_DATES_UTC = ["2026-09-21"]
    snapshot = pricing_snapshot(mock=False, at=datetime(2026, 9, 21, 2, tzinfo=UTC))
    settings.LLM_PRICING_OFF_PEAK_DATES_UTC = []
    assert snapshot["tariff"] == "off_peak"
    assert snapshot["off_peak_dates_utc"] == ["2026-09-21"]


def test_unverified_calendar_prevents_real_reservation(real_mode, settings):
    settings.LLM_PRICING_CALENDAR_YEAR = 2025
    with pytest.raises(ApiError):
        pricing_snapshot(mock=False, at=datetime(2026, 9, 21, tzinfo=UTC))


def test_unknown_evidence_rejected_even_if_uuid_is_valid():
    req = ModelTaskRequest("rerank", "test", "1.0", {}, {}, EVIDENCE)
    with pytest.raises(ApiError):
        validate_payload(req, {"results": [{"evidence_id": str(uuid.uuid4()), "score": 0.9, "reason": "x"}]})


def test_invalid_model_or_prompt_never_reaches_network(context, request_data, monkeypatch):
    network = Mock()
    monkeypatch.setattr(client, "_provider_call", network)
    for req in (
        replace(request_data, template_version="stale"),
        replace(request_data, model_config={"base_url": "https://untrusted.example"}),
    ):
        with pytest.raises(ApiError):
            client.call_structured(context, req)
    assert not network.called and not ModelCall.objects.exists()


def test_ambiguous_timeouts_cannot_bypass_global_limit(context, request_data, real_mode, transport, settings):
    settings.LLM_MAX_CONCURRENCY = 2

    def timeout(request):
        raise httpx.ReadTimeout("timeout")

    factory = transport(timeout)
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == "MODEL_RATE_LIMITED"
    assert factory.call_count == 2
    assert ModelCall.objects.filter(slot_expires_at__gt=timezone.now()).count() == 2


def test_private_image_is_encoded_without_external_url(
    context, request_data, real_mode, transport, project_a
):
    import io

    from apps.common.storage import commit_asset, stage_file

    staged = stage_file(
        io.BytesIO(b"test-image-bytes"), limit_bytes=100, media_type="image/png", suffix=".png"
    )
    asset = commit_asset(project_a, staged, "page_image")

    def provider(request):
        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json=response())

    transport(provider)
    result = client.call_structured(
        context, replace(request_data, model_config={"image_asset_ids": [str(asset.pk)]})
    )
    assert result.payload["queries"] == ["attention"]


def test_foreign_image_is_rejected_before_network(context, request_data, project_b, monkeypatch):
    import io

    from apps.common.storage import commit_asset, stage_file

    asset = commit_asset(
        project_b, stage_file(io.BytesIO(b"image"), limit_bytes=10, media_type="image/png"), "page_image"
    )
    forbidden = Mock()
    monkeypatch.setattr(client, "_provider_call", forbidden)
    with pytest.raises(ApiError) as error:
        client.call_structured(
            context, replace(request_data, model_config={"image_asset_ids": [str(asset.pk)]})
        )
    assert error.value.code == "NOT_FOUND" and not forbidden.called


def test_corrupt_cached_asset_is_not_reused(context, request_data):
    from apps.common.storage import resolve_storage_key

    first = client.call_structured(context, request_data)
    asset = ModelCall.objects.get().raw_output_asset
    resolve_storage_key(asset.storage_key).write_text(
        json.dumps(response({"queries": ["tampered"]})), "utf-8"
    )
    second = client.call_structured(context, request_data)
    assert first.call_id != second.call_id
    assert second.payload == {"queries": ["attention"]}


def test_direct_mock_is_unavailable_in_real_mode(request_data, real_mode):
    with pytest.raises(ApiError):
        client.call_structured_mock(request_data, "query_expand_ok")


def test_reentry_keeps_logical_attempt_limit(context, request_data, real_mode, transport, settings):
    request_data = replace(request_data, logical_call_id=uuid.uuid4())
    factory = transport(
        lambda req: httpx.Response(
            429,
            headers={"Retry-After": str(settings.LLM_REQUEST_TIMEOUT_SECONDS + 1)},
        )
    )
    for attempt in range(1, 4):
        with pytest.raises(ApiError) as error:
            client.call_structured(context, request_data)
        assert error.value.code == "MODEL_RATE_LIMITED"
        assert ModelCall.objects.count() == attempt
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == "INVALID_JOB_STATE" and not error.value.retryable
    assert factory.call_count == 3
    assert list(ModelCall.objects.order_by("network_attempt").values_list("network_attempt", flat=True)) == [
        1,
        2,
        3,
    ]


def test_resume_keeps_unknown_cost_and_waits_for_slot(context, request_data, real_mode, transport, settings):
    from datetime import timedelta

    settings.LLM_MAX_CONCURRENCY = 1
    request_data = replace(request_data, logical_call_id=uuid.uuid4())

    def timeout(_request):
        raise httpx.ReadTimeout("mock timeout")

    first_factory = transport(timeout)
    with pytest.raises(ApiError):
        client.call_structured(context, request_data)
    original = ModelCall.objects.get()
    reserved = UsageBudget.objects.get().reserved_usd
    assert original.outcome == "unsettled" and reserved > 0
    with pytest.raises(ApiError) as error:
        client.call_structured(context, request_data)
    assert error.value.code == "MODEL_RATE_LIMITED" and first_factory.call_count == 1
    assert ModelCall.objects.count() == 1 and UsageBudget.objects.get().reserved_usd == reserved
    ModelCall.objects.filter(pk=original.pk).update(slot_expires_at=timezone.now() - timedelta(seconds=1))
    resumed_factory = transport(lambda req: httpx.Response(200, json=response()))
    result = client.call_structured(context, request_data)
    original.refresh_from_db()
    assert ModelCall.objects.get(pk=result.call_id).network_attempt == 2 and resumed_factory.call_count == 1
    assert original.outcome == "unsettled" and original.estimated_cost_usd is None
    assert UsageBudget.objects.get().reserved_usd == reserved


def test_logical_identity_cannot_change_request(context, request_data, real_mode, transport, settings):
    request_data = replace(request_data, logical_call_id=uuid.uuid4())
    factory = transport(
        lambda req: httpx.Response(
            429,
            headers={"Retry-After": str(settings.LLM_REQUEST_TIMEOUT_SECONDS + 1)},
        )
    )
    with pytest.raises(ApiError):
        client.call_structured(context, request_data)
    changed = replace(request_data, payload={**request_data.payload, "current_query": "different query"})
    with pytest.raises(ApiError) as error:
        client.call_structured(context, changed)
    assert error.value.code == "INVALID_JOB_STATE"
    assert factory.call_count == ModelCall.objects.count() == 1


@pytest.mark.parametrize("valid_repair", [True, False])
def test_format_repair_resumes_once_after_interruption(
    context,
    request_data,
    real_mode,
    transport,
    monkeypatch,
    valid_repair,
):
    request_data = replace(request_data, logical_call_id=uuid.uuid4())
    bodies = []
    answers = [response({"unexpected": True}), response() if valid_repair else response({"unexpected": True})]

    def provider(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=answers.pop(0))

    transport(provider)
    monkeypatch.setattr(client, "_pause", Mock(side_effect=ApiError("INVALID_JOB_STATE", "模拟执行中断")))
    with pytest.raises(ApiError):
        client.call_structured(context, request_data)
    assert ModelCall.objects.count() == 1
    monkeypatch.setattr(client, "_pause", Mock())
    if valid_repair:
        result = client.call_structured(context, request_data)
        assert result.payload == {"queries": ["attention"]}
        assert client.call_structured(context, request_data).call_id == result.call_id
    else:
        for _ in range(2):
            with pytest.raises(ApiError) as error:
                client.call_structured(context, request_data)
            assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert len(bodies) == 2 and ModelCall.objects.count() == 2
    assert bodies[-1]["messages"][-1]["content"] == client.FORMAT_REPAIR_MESSAGE
    assert list(
        ModelCall.objects.order_by("network_attempt").values_list("model_snapshot__format_repair", flat=True)
    ) == [False, True]
