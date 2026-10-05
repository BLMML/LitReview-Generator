"""Fixed article framework; historical source-only revisions remain immutable."""

LEGACY_SOURCE_KEYS = (
    "background",
    "significance",
    "approach",
    "theoretical_basis",
    "research_status",
    "achievements",
)
LEGACY_SOURCE_TITLES = dict(
    zip(
        LEGACY_SOURCE_KEYS,
        (
            "研究背景",
            "研究意义",
            "研究思路",
            "理论基础",
            "国内外研究现状",
            "已取得研究成果",
        ),
        strict=True,
    )
)
SECTION_KEYS = (
    "background",
    "approach",
    "significance",
    "theoretical_basis",
    "research_status",
    "achievements",
    "limitations",
    "innovation",
)
SECTION_TITLES = dict(
    zip(
        SECTION_KEYS,
        (
            "研究背景",
            "研究思路",
            "研究意义",
            "理论基础",
            "国内外研究现状",
            "已取得的成果",
            "文献的不足之处",
            "本文创新点",
        ),
        strict=True,
    )
)
SOURCE_KEYS = SECTION_KEYS[:5]
COMMENTARY_KEYS = SECTION_KEYS[5:]
ANALYSIS_ROLES = {
    "achievements": ("achievement",),
    "limitations": ("viewpoint_comparison", "root_cause"),
    "innovation": ("research_gap", "future_direction", "innovation"),
}
MISSING_CONTENT = "提供的素材中没有该内容。"
SOURCE_POLICY = "pdf-source-only-v1"
COMMENTARY_POLICY = "pdf-source-with-commentary-v2"
INSUFFICIENT_COMMENTARY = (
    "争议证据不足：当前素材及述评尚未形成至少两个有依据的学术争议、"
    "各观点合理性与局限性对比及成因分析。本篇按述评草稿导出，需补充材料与核对。"
)


def is_commentary_structure(sections):
    return [s.get("section_key") for s in sections] == list(SECTION_KEYS)


def is_source_structure(sections):
    return is_commentary_structure(sections) or [s.get("section_key") for s in sections] == list(
        LEGACY_SOURCE_KEYS
    )


def commentary_notices(sections, findings=None):
    """Coverage checks are mechanical; academic support still requires semantic validation."""
    if not is_commentary_structure(sections):
        return []
    by_key = {s["section_key"]: s["blocks"] for s in sections}
    supported = (
        None
        if findings is None
        else {
            b["block_id"]
            for b in findings
            if b.get("support") == "supported" and b.get("structure_ok") is True and not b.get("issues")
        }
    )

    def usable(block):
        return block.get("kind") == "claim" and (supported is None or block["block_id"] in supported)

    comparisons = {
        b.get("controversy_number")
        for b in by_key["limitations"]
        if b.get("analysis_role") == "viewpoint_comparison" and usable(b)
    }
    causes = {
        b.get("controversy_number")
        for b in by_key["limitations"]
        if b.get("analysis_role") == "root_cause" and usable(b)
    }
    notices = []
    if len((comparisons & causes) - {None}) < 2:
        notices.append(INSUFFICIENT_COMMENTARY)
    roles = {b.get("analysis_role") for key in COMMENTARY_KEYS for b in by_key[key] if usable(b)}
    labels = {
        "achievement": "已取得成果",
        "research_gap": "研究空白",
        "future_direction": "未来突破方向",
        "innovation": "本文拟开展的创新点",
    }
    missing = [label for role, label in labels.items() if role not in roles]
    if missing:
        notices.append("述评内容不足：" + "、".join(missing) + "尚无充分的分析内容，请补充材料与核对。")
    return notices


def section_heading(key, *, legacy=False):
    if key == "theoretical_basis":
        return "4.1 理论基础", 2
    if key == "research_status":
        return "4.2 国内外研究现状", 2
    if not legacy and key in COMMENTARY_KEYS:
        return f"5.{COMMENTARY_KEYS.index(key) + 1} {SECTION_TITLES[key]}", 2
    titles = LEGACY_SOURCE_TITLES if legacy else SECTION_TITLES
    numbers = {
        "background": 1,
        "significance": 2 if legacy else 3,
        "approach": 3 if legacy else 2,
        "achievements": 5,
    }
    return f"{numbers[key]} {titles[key]}", 1
