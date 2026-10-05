"""Complete 20,000 / 30,000-character papers fit the deployment input limit.

These tests render the production extraction / comparison prompts and Schema.
They do not call a provider or assert academic quality / PDF transcription.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.conf import settings

from apps.common.http import ApiError
from apps.llm import client
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import TASK_PROMPT_FILE, ModelTaskRequest


def _paper(number: int, *, char_count: int = 30000, chunk_size: int = 1000) -> dict:
    # Ordinary Chinese characters are three UTF-8 bytes. Preserve both markers
    # so a truncation cannot silently pass a length-only assertion.
    prefix, suffix = f"论文{number}原文开始", f"论文{number}原文结束"
    middle = ("研究方法验证结论数据局限" * (char_count // 12 + 1))[: char_count - len(prefix) - len(suffix)]
    text = prefix + middle + suffix
    chunks = [
        {
            "evidence_id": str(uuid.uuid4()),
            "page_number": i // chunk_size + 1,
            "section": None,
            "text": text[i : i + chunk_size],
        }
        for i in range(0, len(text), chunk_size)
    ]
    return {"paper_id": str(uuid.uuid4()), "evidence": chunks, "analysis": {}}


def _request(task: str, papers: list[dict]) -> ModelTaskRequest:
    evidence_ids = [chunk["evidence_id"] for paper in papers for chunk in paper["evidence"]]
    paper_ids = [paper["paper_id"] for paper in papers]
    if task == "paper_extract":
        payload = {
            "paper_title": "长文测试论文",
            "paper_metadata": "作者：测试作者；年份：2026；来源：测试",
            "research_question": "研究方法的适用条件是什么？",
            "evidence_chunks": json.dumps(papers[0]["evidence"], ensure_ascii=False),
            "allowed_evidence_ids": json.dumps(evidence_ids),
        }
    else:
        payload = {
            "research_question": "研究方法的适用条件是什么？",
            "topic_name": "研究方法",
            "candidate_papers": json.dumps(papers, ensure_ascii=False),
            "allowed_paper_ids": json.dumps(paper_ids),
            "allowed_evidence_ids": json.dumps(evidence_ids),
            "max_disputes": settings.DISPUTES_MAX,
            "output_language": "zh-CN",
        }
    prompt = load_prompt(TASK_PROMPT_FILE[task], payload, task_type=task)
    return ModelTaskRequest(
        task,
        prompt.version,
        "1.0",
        {},
        payload,
        allowed_evidence_ids=evidence_ids,
        allowed_paper_ids=paper_ids,
    )


@pytest.mark.parametrize("task, count", [("paper_extract", 1), ("disputes", 2)])
@pytest.mark.parametrize("char_count", [20000, 30000])
@pytest.mark.parametrize("chunk_size", [600, 1000])
def test_complete_long_papers_fit_deployment_limit(task, count, char_count, chunk_size):
    papers = [_paper(i + 1, char_count=char_count, chunk_size=chunk_size) for i in range(count)]
    request = _request(task, papers)
    context = SimpleNamespace(project_id=uuid.uuid4(), run_id=uuid.uuid4())
    body, snapshot, _ = client._prepare(context, request)
    user_text = body["messages"][1]["content"]

    for paper in papers:
        assert sum(len(chunk["text"]) for chunk in paper["evidence"]) == char_count
        assert paper["evidence"][0]["text"].startswith(f"论文{papers.index(paper) + 1}原文开始")
        assert paper["evidence"][-1]["text"].endswith(f"论文{papers.index(paper) + 1}原文结束")
        for chunk in paper["evidence"]:
            assert chunk["text"] in user_text
            assert chunk["evidence_id"] in user_text
    assert char_count * 3 < snapshot["input_token_bound"] <= settings.LLM_MAX_INPUT_TOKENS
    assert body["max_tokens"] == settings.LLM_MAX_OUTPUT_TOKENS


def test_lowered_limit_rejects_long_paper_without_truncating(settings):
    papers = [_paper(1)]
    request = _request("paper_extract", papers)
    original_payload = dict(request.payload)
    settings.LLM_MAX_INPUT_TOKENS = 24000
    context = SimpleNamespace(project_id=uuid.uuid4(), run_id=uuid.uuid4())
    with pytest.raises(ApiError) as caught:
        client._prepare(context, request)
    assert caught.value.code == "VALIDATION_ERROR"
    assert request.payload == original_payload


def test_large_combination_still_rejects_without_truncating():
    # An input limit increase is not permission to silently omit later papers.
    papers = [_paper(i + 1) for i in range(8)]
    request = _request("disputes", papers)
    original_payload = dict(request.payload)
    context = SimpleNamespace(project_id=uuid.uuid4(), run_id=uuid.uuid4())
    with pytest.raises(ApiError) as caught:
        client._prepare(context, request)
    assert caught.value.code == "VALIDATION_ERROR"
    assert request.payload == original_payload
