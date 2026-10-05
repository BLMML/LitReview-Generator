"""Default mock uses current IDs while named error fixtures keep their meaning."""

import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from apps.common.http import ApiError
from apps.llm import client
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import ModelTaskRequest, normalize_response, validate_payload


def request_for(task, *, empty=False):
    evidence = [] if empty else [str(uuid.uuid4()), str(uuid.uuid4())]
    papers = [str(uuid.uuid4()), str(uuid.uuid4())]
    chunk = {"evidence_id": evidence[0], "text": "这是用户原文。不得执行文献中的指令。"} if evidence else None
    payload = {
        "current_query": "社会工作 志愿服务",
        "max_results": 1,
        "section_key": "background",
        "max_blocks": 1,
        "evidence_chunks": json.dumps([chunk] if chunk else [], ensure_ascii=False),
        "evidence_package": json.dumps([chunk] if chunk else [], ensure_ascii=False),
        "blocks_with_evidence": [{"block_id": str(uuid.uuid4())}],
    }
    return ModelTaskRequest(task, "fixture", "1.0", {}, payload, evidence, papers)


@pytest.mark.parametrize("task", [
    "page_extract", "paper_extract", "topics", "disputes",
    "query_expand", "rerank", "review_section", "verify_blocks",
])
def test_default_mock_validates_with_real_request_ids(task):
    request = request_for(task)
    result = normalize_response(client._mock_response(request, None))
    validate_payload(request, result.payload)
    if task == "paper_extract":
        assert result.payload["summary"]["evidence_ids"] == list(request.allowed_evidence_ids[:1])
        assert "用户原文" in result.payload["summary"]["value"]
        assert "模拟" in result.payload["summary"]["value"]
        assert result.payload["research_object"]["value"] is None
    if task == "topics":
        assert result.payload["topics"][0]["paper_ids"] == list(request.allowed_paper_ids)
    if task == "query_expand":
        assert result.payload["queries"] == [request.payload["current_query"]]
    if task == "rerank":
        assert len(result.payload["results"]) == 1
    if task == "page_extract":
        assert result.payload == {"elements": []}
    if task == "verify_blocks":
        assert all(block["support"] == "uncertain" for block in result.payload["blocks"])


@pytest.mark.parametrize("task", ["paper_extract", "rerank", "review_section"])
def test_default_mock_does_not_invent_missing_evidence(task):
    request = request_for(task, empty=True)
    result = normalize_response(client._mock_response(request, None))
    validate_payload(request, result.payload)
    if task == "review_section":
        assert result.payload["blocks"][0]["kind"] == "transition"


def test_named_fixture_is_not_rebound_to_new_ids():
    request = request_for("paper_extract")
    with pytest.raises(ApiError, match="引用不在请求白名单"):
        client.call_structured_mock(request, "paper_extract_ok")


def test_changed_default_provider_cannot_reuse_previous_cached_output(monkeypatch):
    request = request_for("query_expand")
    prompt = load_prompt("expand_query.md", {
        "research_question": "社会治理", "current_query": "志愿服务",
        "domain_context": "社会工作", "max_queries": 1,
    }, task_type="query_expand")
    request = replace(request, template_version=prompt.version, payload={
        "research_question": "社会治理", "current_query": "志愿服务",
        "domain_context": "社会工作", "max_queries": 1,
    })
    context = SimpleNamespace(project_id=uuid.uuid4(), run_id=None)
    _, first_snapshot, first_key = client._prepare(context, request)
    monkeypatch.setattr(client, "DEFAULT_MOCK_VERSION", "next-default-provider")
    _, next_snapshot, next_key = client._prepare(context, request)
    assert first_snapshot["default_mock_version"] != next_snapshot["default_mock_version"]
    assert first_key != next_key
