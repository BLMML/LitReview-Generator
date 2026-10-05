"""Default development responses bound to the current request, never academic judgments.

Named fixtures remain unchanged in client.py so deliberate invalid-output tests
still exercise the real schema and reference guards.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from apps.llm.schemas import ModelTaskRequest

DEFAULT_MOCK_VERSION = "request-bound-v2-source-only"
SECTION_TITLES = {
    "background": "研究背景",
    "directions": "主要研究方向",
    "representative_work": "代表性工作",
    "disagreements": "学术争议",
    "trends": "发展趋势",
}


def _items(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _excerpt(request: ModelTaskRequest, field: str) -> tuple[str, list[str]]:
    allowed = {str(i) for i in request.allowed_evidence_ids}
    for chunk in _items(request.payload.get(field)):
        if not isinstance(chunk, Mapping):
            continue
        evidence_id = str(chunk.get("evidence_id", ""))
        text = chunk.get("text")
        if evidence_id in allowed and isinstance(text, str) and text.strip():
            return " ".join(text.split())[:180], [evidence_id]
    return "", []


def default_payload(request: ModelTaskRequest) -> dict:
    """Return schema-shaped placeholders with actual IDs and explicit mock labels."""
    task = request.task_type
    papers = list(dict.fromkeys(str(i) for i in request.allowed_paper_ids))
    evidence = list(dict.fromkeys(str(i) for i in request.allowed_evidence_ids))
    if task == "page_extract":
        # Mock cannot read a page image. Never manufacture text that later
        # becomes apparently genuine PDF evidence.
        return {"elements": []}
    if task == "paper_extract":
        text, ids = _excerpt(request, "evidence_chunks")
        missing = {"value": None, "evidence_ids": []}
        return {
            "research_question": dict(missing),
            "research_object": dict(missing),
            "summary": {"value": f"模拟摘录（未做学术分析）：{text}", "evidence_ids": ids}
            if ids
            else dict(missing),
            "methods": [],
            "datasets": [],
            "metrics": [],
            "conclusions": [],
            "limitations": [],
        }
    if task == "topics":
        return {
            "topics": [
                {
                    "name": "模拟联调主题",
                    "description": "仅按输入论文组建流程测试主题，未判断学术关联。",
                    "paper_ids": papers,
                    "evidence_ids": [],
                }
            ]
            if papers
            else [],
            "unassigned_paper_ids": [],
        }
    if task == "disputes":
        return {"disputes": []}
    if task == "query_expand":
        return {"queries": [request.payload["current_query"]]}
    if task == "rerank":
        maximum = min(int(request.payload.get("max_results", 10)), 50)
        return {
            "results": [
                {
                    "evidence_id": item,
                    "score": round(1 / (position + 1), 6),
                    "reason": "模拟保留候选顺序，未判断学术相关性",
                }
                for position, item in enumerate(evidence[:maximum])
            ]
        }
    if task == "review_section":
        from apps.reviews.structure import MISSING_CONTENT
        from apps.reviews.structure import SECTION_TITLES as source_titles

        key = request.payload["section_key"]
        patterns = {
            "background": r"研究背景|引言|绪论|introduction|background|abstract|摘要",
            "significance": r"研究意义|research significance",
            "approach": r"研究思路|研究方法|方法|method(?:ology|s)?|approach",
            "theoretical_basis": r"理论基础|theoretical (?:basis|foundation)",
            "research_status": r"国内外研究现状|研究现状|文献综述|related work|literature review",
            "achievements": r"已取得研究成果|研究结果|实验结果|results?",
        }
        block = {"kind": "transition", "text": MISSING_CONTENT, "evidence_ids": []}
        if key in patterns:
            for chunk in _items(request.payload.get("evidence_package")):
                if str(chunk.get("evidence_id")) not in evidence:
                    continue
                text = chunk.get("text", "")
                match = re.search(
                    r"(?im)^\s*(?:[\d一二三四五六七八九十]+[.、．]?\s*)?(?:"
                    + patterns[key]
                    + r")\s*[:：]?\s*\n",
                    text,
                )
                if match:
                    remaining = text[match.end() :].strip()
                    stops = list(
                        re.finditer(r"[。！？](?:[”’\"\')]?)|[.!?](?:[”’\"\')]?)(?:\s|$)", remaining[:450])
                    )
                    quote = remaining[: stops[-1].end()].strip() if stops else ""
                    if quote:
                        block = {"kind": "claim", "text": quote, "evidence_ids": [str(chunk["evidence_id"])]}
                        break
        return {
            "section_key": key,
            "title": source_titles.get(key, SECTION_TITLES.get(key, key)),
            "blocks": [block],
        }
    if task == "verify_blocks":
        return {
            "blocks": [
                {
                    "block_id": block["block_id"],
                    "structure_ok": True,
                    "support": "uncertain",
                    "issues": ["模拟核验未判断事实支持，需在真实模型和人工审阅中核对"],
                    "suggestion": None,
                }
                for block in request.payload.get("blocks_with_evidence", [])
            ]
        }
    raise ValueError(f"Unsupported mock task: {task}")
