"""指定不可变正文的结构检查和逐块支持核验；执行失败不产生报告。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.common.http import ApiError, api_error
from apps.common.storage import open_asset
from apps.llm.client import SYSTEM_RULES, call_structured
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import TASK_VERIFY_BLOCKS, ModelTaskRequest, output_schema, validate_payload
from apps.reviews.schemas import SectionsValidationError, validate_sections

VALIDATION_VERSION = "review-validation-v1"
VALIDATE_PROMPT = "validate_claims.md"
# 只有确定的纯承接语允许自动接受 not_applicable，其它情况保守交给人工。
PURE_TRANSITIONS = frozenset(
    {
        "提供的素材中没有该内容。",
        "本轮证据不足以形成判断，本节的覆盖范围有限。",
        "下面介绍主要研究方向。",
        "下面介绍代表性工作。",
        "下面讨论学术争议。",
        "下面讨论发展趋势。",
        "以下按研究方向展开。",
    }
)


@dataclass(frozen=True)
class StructuralFindings:
    passed: bool
    issues: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ClaimFindings:
    blocks: list[dict[str, Any]] = field(default_factory=list)


def revision_blocks(revision: Any) -> list[dict[str, Any]]:
    """也能遍历被损坏的历史 JSON，让结构失败有可定位的报告。"""
    if not isinstance(revision.sections, list):
        return []
    return [
        block
        for section in revision.sections
        if isinstance(section, Mapping)
        for block in (section.get("blocks") if isinstance(section.get("blocks"), list) else [])
        if isinstance(block, Mapping)
    ]


def evidence_snapshot(revision: Any) -> list[dict[str, Any]]:
    """只读取该 Run 的冻结文件与解析产物，绝不读取可编辑 Paper 题录。"""
    from apps.parsing.models import EvidenceChunk

    wanted = {
        str(eid)
        for b in revision_blocks(revision)
        for eid in (b.get("evidence_ids") if isinstance(b.get("evidence_ids"), list) else [])
    }
    wanted = {str(uuid.UUID(eid)) for eid in wanted if _is_uuid(eid)}
    papers = {
        rp.parsed_document_id: rp
        for rp in revision.run.run_papers.filter(parsed_document_id__isnull=False).select_related(
            "parsed_document", "file_asset"
        )
    }
    rows = EvidenceChunk.objects.filter(id__in=wanted, parsed_document_id__in=papers).order_by("id")
    result = []
    for row in rows:
        rp = papers[row.parsed_document_id]
        result.append(
            {
                "evidence_id": str(row.id),
                "paper_id": str(rp.paper_id),
                "parsed_document_id": str(row.parsed_document_id),
                "page_number": row.page_number,
                "section": row.section,
                "element_type": row.element_type,
                "text": row.text,
                "original_text": row.original_text,
                "verification": row.verification,
                "content_hash": row.content_hash,
                "locator": row.locator,
                "normalization_revision": row.normalization_revision,
                "source_image_id": str(row.source_image_id) if row.source_image_id else None,
                "file_asset_id": str(rp.file_asset_id),
                "file_sha256": rp.file_sha256,
            }
        )
    return result


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _readable(asset: Any, project_id: Any, expected_hash: str | None = None) -> bool:
    if asset is None or asset.project_id != project_id or asset.size_bytes <= 0:
        return False
    try:
        digest = hashlib.sha256()
        size = 0
        with open_asset(asset) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if size > asset.size_bytes:
                    return False
        return size == asset.size_bytes and digest.hexdigest() == (expected_hash or asset.sha256)
    except (ApiError, OSError):
        return False


def validate_structure(revision: Any) -> StructuralFindings:
    from apps.analysis.services import _run_scope
    from apps.common.models import FileAsset
    from apps.parsing.evidence import chunk_content_hash

    issues: list[dict[str, Any]] = []
    try:
        validate_sections(revision.sections, run_scope=_run_scope(revision.run))
        from apps.reviews.grounding import ground_sections
        from apps.reviews.structure import is_source_structure

        if is_source_structure(revision.sections):
            ground_sections(revision.run, revision.sections)
    except SectionsValidationError as exc:
        issues.append({"block_id": None, "message": str(exc)})
    evidence = evidence_snapshot(revision)
    papers = {
        str(rp.paper_id): rp for rp in revision.run.run_papers.select_related("file_asset", "parsed_document")
    }
    readable: dict[str, bool] = {}
    for item in evidence:
        rp = papers[item["paper_id"]]
        document = rp.parsed_document
        file_id = item["file_asset_id"]
        if file_id not in readable:
            readable[file_id] = _readable(rp.file_asset, revision.review.project_id, rp.file_sha256)
        valid = (
            readable[file_id]
            and rp.file_asset.purpose == FileAsset.PURPOSE_ORIGINAL_PDF
            and document.project_id == revision.review.project_id
            and document.source_file_id == rp.file_asset_id
            and document.source_sha256 == rp.file_sha256
            and document.page_count == rp.page_count
            and 1 <= item["page_number"] <= rp.page_count
            and bool(item["text"].strip())
            and chunk_content_hash(item["text"]) == item["content_hash"]
        )
        if item["verification"] == "text_matched":
            valid = valid and bool(item["original_text"])
        elif item["verification"] == "image_unverified":
            image = FileAsset.objects.filter(pk=item["source_image_id"]).first()
            valid = (
                valid
                and image is not None
                and image.purpose == FileAsset.PURPOSE_PAGE_IMAGE
                and image.media_type in ("image/png", "image/jpeg")
                and _readable(image, revision.review.project_id)
            )
        if not valid:
            for block in revision_blocks(revision):
                if item["evidence_id"] in (block.get("evidence_ids") or []):
                    issues.append(
                        {
                            "block_id": str(block.get("block_id")),
                            "message": f"证据 {item['evidence_id']} 的原始来源不可读或完整性不符",
                        }
                    )
    return StructuralFindings(not issues, issues, evidence)


def validation_template_version() -> str:
    return load_prompt(
        VALIDATE_PROMPT,
        {
            "revision_number": 1,
            "blocks_with_evidence": "[]",
            "allowed_evidence_ids": "[]",
            "output_language": "zh-CN",
        },
        task_type=TASK_VERIFY_BLOCKS,
    ).version


def validation_schema_config() -> dict[str, Any]:
    schema = output_schema(TASK_VERIFY_BLOCKS)
    # 计入系统提示词、完整 Schema、网关预留和一次格式修复预留。
    return {
        "schema_hash": snapshot_hash(schema),
        "system_input_bytes": len((SYSTEM_RULES + json.dumps(schema, ensure_ascii=False)).encode("utf-8"))
        + 256
        + 128,
    }


def validate_claim_support(
    context: Any, revision: Any, evidence: Sequence[Any], *, config: Mapping[str, Any], validation_id: Any
) -> ClaimFindings:
    """按冻结的批大小核验；逻辑调用 ID 在失败重试中稳定，无正文改写。"""
    by_id = {e["evidence_id"]: e for e in evidence}
    blocks = revision_blocks(revision)
    from apps.reviews.structure import SOURCE_POLICY, is_source_structure

    module_by_block = {
        b["block_id"]: (s["section_key"], s["title"])
        for s in revision.sections
        for b in s["blocks"]
    }
    source_policy = SOURCE_POLICY if is_source_structure(revision.sections) else "legacy"
    prepared = [
        {
            **b,
            "section_key": module_by_block[b["block_id"]][0],
            "section_title": module_by_block[b["block_id"]][1],
            "source_policy": source_policy,
            "evidence": [
                {
                    "evidence_id": eid,
                    "paper_id": by_id[eid]["paper_id"],
                    "page_number": by_id[eid]["page_number"],
                    "text": by_id[eid]["original_text"] or by_id[eid]["text"],
                    "verification": by_id[eid]["verification"],
                }
                for eid in b["evidence_ids"]
            ],
        }
        for b in blocks
    ]
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for block in prepared:
        candidate = [*current, block]
        prompt = _prompt(revision, candidate, config)
        # 网关按 UTF-8 字节数保守预留；不截掉原文来强行缩小请求。
        if current and (
            len(candidate) > config["batch_size"]
            or len(prompt.text.encode("utf-8")) + config["system_input_bytes"] > config["max_input_tokens"]
        ):
            batches.append(current)
            current = [block]
        else:
            current = candidate
    if current:
        batches.append(current)
    findings: dict[str, dict[str, Any]] = {}
    for index, batch in enumerate(batches):
        context.ensure_not_cancelled()
        prompt = _prompt(revision, batch, config)
        if len(prompt.text.encode("utf-8")) + config["system_input_bytes"] > config["max_input_tokens"]:
            raise api_error("VALIDATION_ERROR", "单块完整证据超过核验输入上限，请缩减该块引用后重新核验")
        if prompt.version != config["template_version"]:
            raise api_error("INTERNAL_ERROR", "冻结的核验提示词已改变，请新建核验报告")
        request = ModelTaskRequest(
            task_type=TASK_VERIFY_BLOCKS,
            template_version=prompt.version,
            schema_version=config["schema_version"],
            model_config=config["model_config"],
            payload={
                "revision_number": revision.revision_number,
                "blocks_with_evidence": batch,
                "allowed_evidence_ids": list(dict.fromkeys(eid for b in batch for eid in b["evidence_ids"])),
                "output_language": config["output_language"],
            },
            allowed_evidence_ids=list(dict.fromkeys(eid for b in batch for eid in b["evidence_ids"])),
            logical_call_id=uuid.uuid5(uuid.UUID(str(validation_id)), f"verify_blocks:{index}"),
        )
        result = call_structured(context, request)
        validate_payload(request, result.payload)
        for raw in result.payload["blocks"]:
            finding = dict(raw)
            finding["issues"] = list(raw["issues"])
            block = next(b for b in batch if b["block_id"] == raw["block_id"])
            if finding["support"] == "not_applicable" and (
                block["kind"] != "transition"
                or block["evidence_ids"]
                or block["text"] not in PURE_TRANSITIONS
            ):
                finding["support"] = "uncertain"
                finding["issues"].append("无法确认是纯承接语，不能用 not_applicable 跳过事实核验")
            if not block["evidence_ids"] and finding["support"] == "supported":
                finding["support"] = "uncertain"
                finding["issues"].append("该块没有引用原文，不能认定事实陈述获得证据支持")
            if any(e["verification"] == "image_unverified" for e in block["evidence"]):
                finding["support"] = "uncertain"
                finding["issues"].append("引用包含未经人工核对的图像转录，需回看原始页面")
            findings[raw["block_id"]] = finding
    return ClaimFindings([findings[b["block_id"]] for b in blocks])


def _prompt(revision: Any, blocks: list, config: Mapping) -> Any:
    return load_prompt(
        VALIDATE_PROMPT,
        {
            "revision_number": revision.revision_number,
            "blocks_with_evidence": json.dumps(blocks, ensure_ascii=False, sort_keys=True),
            "allowed_evidence_ids": json.dumps(
                list(dict.fromkeys(eid for b in blocks for eid in b["evidence_ids"])), ensure_ascii=False
            ),
            "output_language": config["output_language"],
        },
        task_type=TASK_VERIFY_BLOCKS,
    )


def build_report(revision: Any, structural: StructuralFindings, claims: ClaimFindings) -> dict[str, Any]:
    if structural.passed:
        blocks = claims.blocks
        passed = all(b["structure_ok"] and b["support"] in ("supported", "not_applicable") for b in blocks)
        structure_passed = all(b["structure_ok"] for b in blocks)
    else:
        blocks = [
            {
                "block_id": str(b.get("block_id")),
                "structure_ok": False,
                "support": "uncertain",
                "issues": [
                    i["message"] for i in structural.issues if i["block_id"] in (None, str(b.get("block_id")))
                ],
                "suggestion": "先修复正文结构或原始来源，再重新核验",
            }
            for b in revision_blocks(revision)
        ]
        passed = structure_passed = False
    return {
        "result": "passed" if passed and structural.passed else "needs_review",
        "structure_passed": structure_passed,
        "blocks": blocks,
    }
