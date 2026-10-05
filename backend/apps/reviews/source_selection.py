"""Bind model selections to complete PDF passages offered for this section.

The model chooses meaning and placement; only the backend copies source text.
IDs bind the evidence, canonical passage and policy, never mutable block UUIDs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from apps.reviews.schemas import SectionsValidationError
from apps.reviews.structure import SOURCE_KEYS

SOURCE_SELECTION_REVISION = "pdf-passage-selection-v1"


def passage_catalog(chunks):
    catalog = {}
    offered = []
    for chunk in chunks:
        choices = []
        for text in chunk.get("quotable_passages", []):
            if not isinstance(text, str) or not text.strip():
                continue
            identity = json.dumps(
                [SOURCE_SELECTION_REVISION, str(chunk["evidence_id"]), text],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            passage_id = "src_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            catalog[passage_id] = {
                "kind": "claim",
                "text": text,
                "evidence_ids": [str(chunk["evidence_id"])],
            }
            choices.append({"source_passage_id": passage_id, "text": text})
        offered.append({**chunk, "quotable_passages": choices})
    return catalog, offered


def resolve_selections(blocks, section_key, catalog):
    resolved, seen = [], set()
    for block in blocks:
        if not isinstance(block, Mapping) or "source_passage_id" not in block:
            # Historical full-text outputs still undergo strict PDF grounding.
            resolved.append(block)
            continue
        if section_key not in SOURCE_KEYS:
            raise SectionsValidationError("述评必须输出分析正文，不能使用原句选择编号")
        if set(block) != {"kind", "source_passage_id"} or block["kind"] != "claim":
            raise SectionsValidationError("原句选择只允许 kind 和 source_passage_id，不接受改写正文或引用")
        passage_id = block["source_passage_id"]
        if not isinstance(passage_id, str) or passage_id not in catalog:
            raise SectionsValidationError("原句编号不在本节提供的完整原文目录中")
        if passage_id in seen:
            raise SectionsValidationError("同一节不能重复选择同一原句编号")
        seen.add(passage_id)
        source = catalog[passage_id]
        resolved.append({**source, "evidence_ids": list(source["evidence_ids"])})
    return resolved
