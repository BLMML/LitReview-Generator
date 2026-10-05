"""reviews.schemas：固定内容槽与 claim / transition 结构校验。

职责（docs/mvp/01 第 4.6 节、docs/mvp/03 第 2.5 节）：
- `validate_sections(sections, run_scope, ...) → 规范化正文`；
- 结构校验是保存和生成提交的**共同门槛**。

硬性限制：
- 新稿固定八个内容槽，兼容历史五节与六槽顺序；述评类型及争议编号随正文冻结；
- 每节至少一个 block；全稿 ≤100 block；每块 1—600 字符、≤20 引用；
- block_id 全文唯一；kind ∈ claim/transition；
- claim 至少一个当前 Run 证据；禁止 HTML、非法引用；
- 新稿保留原文编号；历史结构拒绝自编文献编号。

校验只回答"结构与引用是否合法"，不判断学术正确性：结构与引用通过**不等于**
内容被证据支持，后者是核验阶段（M11）的职责。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from apps.reviews.models import BLOCK_KIND_CLAIM, BLOCK_KINDS, SECTION_KEYS
from apps.reviews.structure import ANALYSIS_ROLES, LEGACY_SOURCE_KEYS
from apps.reviews.structure import SECTION_KEYS as SOURCE_SECTION_KEYS

NOT_IMPLEMENTED_MESSAGE = "reviews.schemas 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 D）"

# 全稿与单块的硬上限（与冻结 OpenAPI 的 Section / Block 定义一致）
MAX_BLOCKS_TOTAL = 100
MAX_EVIDENCE_PER_BLOCK = 20
MIN_BLOCK_CHARS = 1
MAX_BLOCK_CHARS = 600
MAX_TITLE_CHARS = 120

# 生成模板不允许 HTML：正文里出现标签必须被拒，而不是"清理后放行"
_HTML_TAG = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>")
_HTML_ENTITY = re.compile(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);")

# 正文不接受模型给出的文献编号（如 [12] / ［12］）。编号由后端按首次引用生成。
_CITATION_NUMBER = re.compile(r"[\[［]\s*\d+\s*(?:[-,，]\s*\d+\s*)*[\]］]")


class SectionsValidationError(ValueError):
    """正文结构不合法；映射为 422 STRUCTURAL_VALIDATION_FAILED。"""


def validate_sections(
    sections: Sequence[Any],
    *,
    run_scope: Any,
    expected_keys: Sequence[str] | None = None,
    max_blocks_per_section: int | None = None,
    allow_source_numbers: bool = False,
    commentary_layout: bool = False,
) -> list[dict[str, Any]]:
    """校验并规范化正文；返回可直接入库的 `sections`。

    `run_scope` 需提供 `allowed_paper_ids`、`evidence_ids_by_paper`（与
    `analysis.schemas` 的作用域同构），用于确认引用属于当前 Run。
    `expected_keys` 允许调用方把"本次要生成的节"限定为单个，
    但**最终提交时必须传完整结构**。新稿的原文、标题与出处由 grounding 再校验。
    """
    if isinstance(sections, (str, bytes)) or not isinstance(sections, Sequence):
        raise SectionsValidationError("sections 必须是数组")

    source_layout = len(sections) in (len(SOURCE_SECTION_KEYS), len(LEGACY_SOURCE_KEYS))
    commentary_layout = (
        commentary_layout
        or len(sections) == len(SOURCE_SECTION_KEYS)
        or (expected_keys is not None and "limitations" in expected_keys)
    )
    keys = (
        list(expected_keys)
        if expected_keys is not None
        else list(
            SOURCE_SECTION_KEYS
            if len(sections) == len(SOURCE_SECTION_KEYS)
            else LEGACY_SOURCE_KEYS
            if source_layout
            else SECTION_KEYS
        )
    )
    if len(sections) != len(keys):
        raise SectionsValidationError(f"必须提供 {len(keys)} 个章节，实际 {len(sections)} 个")

    allowed_evidence = _allowed_evidence(run_scope)
    seen_block_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    total_blocks = 0

    for position, raw in enumerate(sections):
        name = f"sections[{position}]"
        if not isinstance(raw, Mapping):
            raise SectionsValidationError(f"{name} 必须是对象")
        unknown = sorted(set(raw) - {"section_key", "title", "blocks"})
        if unknown:
            raise SectionsValidationError(f"{name} 含未知字段：{unknown}")
        missing = [key for key in ("section_key", "title", "blocks") if key not in raw]
        if missing:
            raise SectionsValidationError(f"{name} 缺少字段：{missing}")

        # 节序固定：顺序错乱会让成稿的章节顺序不可预期
        if raw["section_key"] != keys[position]:
            raise SectionsValidationError(
                f"{name}.section_key 必须是 {keys[position]!r}，实际 {raw['section_key']!r}"
            )
        key = keys[position]

        title = raw["title"]
        if not isinstance(title, str) or not title.strip():
            raise SectionsValidationError(f"{name}.title 必须是非空字符串")
        title = title.strip()
        if len(title) > MAX_TITLE_CHARS:
            raise SectionsValidationError(f"{name}.title 超过 {MAX_TITLE_CHARS} 字符")

        blocks = raw["blocks"]
        if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
            raise SectionsValidationError(f"{name}.blocks 必须是数组")
        if not blocks:
            # 空节会让成稿出现没有内容的标题，属于结构错误
            raise SectionsValidationError(f"{name} 至少需要一个 block")
        if max_blocks_per_section is not None and len(blocks) > max_blocks_per_section:
            raise SectionsValidationError(f"{name} 超过单节 {max_blocks_per_section} 个 block 的冻结上限")

        normalized_blocks = [
            _block(
                item,
                name=f"{name}.blocks[{index}]",
                allowed=allowed_evidence,
                seen=seen_block_ids,
                reject_numbers=not (source_layout or allow_source_numbers),
                analysis_roles=ANALYSIS_ROLES.get(key) if commentary_layout else None,
            )
            for index, item in enumerate(blocks)
        ]
        total_blocks += len(normalized_blocks)
        normalized.append({"section_key": key, "title": title, "blocks": normalized_blocks})

    if total_blocks > MAX_BLOCKS_TOTAL:
        raise SectionsValidationError(f"全稿 block 数超过 {MAX_BLOCKS_TOTAL}")

    return normalized


def _block(
    raw: Any, *, name: str, allowed: set[str], seen: set[str], reject_numbers=True, analysis_roles=None
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SectionsValidationError(f"{name} 必须是对象")
    unknown = sorted(
        set(raw) - {"block_id", "kind", "text", "evidence_ids", "analysis_role", "controversy_number"}
    )
    if unknown:
        raise SectionsValidationError(f"{name} 含未知字段：{unknown}")
    missing = [key for key in ("block_id", "kind", "text", "evidence_ids") if key not in raw]
    if missing:
        raise SectionsValidationError(f"{name} 缺少字段：{missing}")

    try:
        block_id = str(uuid.UUID(str(raw["block_id"])))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SectionsValidationError(f"{name}.block_id 不是合法 UUID") from exc
    if block_id in seen:
        # 重复 ID 会让核验与导出无法逐块定位
        raise SectionsValidationError(f"{name}.block_id 与其它 block 重复：{block_id}")
    seen.add(block_id)

    kind = raw["kind"]
    if kind not in BLOCK_KINDS:
        raise SectionsValidationError(f"{name}.kind 取值不合法：{kind!r}")

    text = raw["text"]
    if not isinstance(text, str):
        raise SectionsValidationError(f"{name}.text 必须是字符串")
    if _HTML_TAG.search(text) or _HTML_ENTITY.search(text):
        raise SectionsValidationError(f"{name}.text 不得包含 HTML")
    if _CITATION_NUMBER.search(text) and reject_numbers:
        # 文献编号由后端按首次引用生成，正文自带编号会与真实编号冲突
        raise SectionsValidationError(f"{name}.text 不得自带文献编号")
    text = text.strip()
    if not MIN_BLOCK_CHARS <= len(text) <= MAX_BLOCK_CHARS:
        raise SectionsValidationError(f"{name}.text 长度必须在 {MIN_BLOCK_CHARS}—{MAX_BLOCK_CHARS} 之间")

    raw_ids = raw["evidence_ids"]
    if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, Sequence):
        raise SectionsValidationError(f"{name}.evidence_ids 必须是数组")
    if len(raw_ids) > MAX_EVIDENCE_PER_BLOCK:
        raise SectionsValidationError(f"{name} 引用超过 {MAX_EVIDENCE_PER_BLOCK} 条证据")

    evidence_ids: list[str] = []
    for item in raw_ids:
        try:
            identifier = str(uuid.UUID(str(item)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise SectionsValidationError(f"{name}.evidence_ids 含非法 UUID：{item!r}") from exc
        if identifier in evidence_ids:
            continue
        if identifier not in allowed:
            # 伪造 ID 与跨 Run 引用在这里被同一道检查拒绝
            raise SectionsValidationError(f"{name} 引用了不属于本 Run 的证据：{identifier}")
        evidence_ids.append(identifier)

    if kind == BLOCK_KIND_CLAIM and not evidence_ids:
        # claim 是事实性论断，没有证据就不是可核验的正文
        raise SectionsValidationError(f"{name} 是 claim，必须至少引用一条本 Run 证据")

    result = {"block_id": block_id, "kind": kind, "text": text, "evidence_ids": evidence_ids}
    if analysis_roles is not None and kind == BLOCK_KIND_CLAIM:
        role = raw.get("analysis_role")
        if role not in analysis_roles:
            raise SectionsValidationError(f"{name}.analysis_role 不符合述评子模块")
        result["analysis_role"] = role
        if role in ("viewpoint_comparison", "root_cause"):
            number = raw.get("controversy_number")
            if type(number) is not int or not 1 <= number <= 20:
                raise SectionsValidationError(f"{name}.controversy_number 必须是 1—20 的争议编号")
            result["controversy_number"] = number
        elif "controversy_number" in raw:
            raise SectionsValidationError(f"{name} 仅争议对比与成因分析可填写争议编号")
    elif "analysis_role" in raw or "controversy_number" in raw:
        raise SectionsValidationError(f"{name} 原文或缺失说明不能标为自主述评")
    return result


def _allowed_evidence(run_scope: Any) -> set[str]:
    evidence_by_paper = getattr(run_scope, "evidence_ids_by_paper", None)
    if evidence_by_paper is None:
        raise SectionsValidationError("缺少 Run 证据作用域")
    result: set[str] = set()
    for values in evidence_by_paper.values():
        result |= {str(value) for value in values}
    return result


def normalize_sections(sections: Sequence[Any]) -> str:
    """生成 content_hash 用的规范化表示；顺序与空白必须稳定。

    只包含结构字段，不含时间或 ID 之外的易变信息，因此同一份正文
    在任何进程、任何时刻都得到同一字符串。
    """
    import json

    material = [
        {
            "section_key": section.get("section_key"),
            "title": (section.get("title") or "").strip(),
            "blocks": [
                {
                    "block_id": str(block.get("block_id")),
                    "kind": block.get("kind"),
                    "text": (block.get("text") or "").strip(),
                    "evidence_ids": [str(value) for value in (block.get("evidence_ids") or [])],
                    **{key: block[key] for key in ("analysis_role", "controversy_number") if key in block},
                }
                for block in (section.get("blocks") or [])
            ],
        }
        for section in sections
    ]
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sections_content_hash(sections: Sequence[Any]) -> str:
    import hashlib

    return hashlib.sha256(normalize_sections(sections).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_BLOCKS_TOTAL",
    "MAX_BLOCK_CHARS",
    "MAX_EVIDENCE_PER_BLOCK",
    "SECTION_KEYS",
    "SectionsValidationError",
    "normalize_sections",
    "sections_content_hash",
    "validate_sections",
]
