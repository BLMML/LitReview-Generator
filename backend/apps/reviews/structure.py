"""Source-only manuscript layout; legacy revisions keep their original shape."""

SECTION_KEYS = (
    "background",
    "significance",
    "approach",
    "theoretical_basis",
    "research_status",
    "achievements",
)
SECTION_TITLES = dict(
    zip(
        SECTION_KEYS,
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
MISSING_CONTENT = "提供的素材中没有该内容。"
SOURCE_POLICY = "pdf-source-only-v1"


def is_source_structure(sections):
    return [s.get("section_key") for s in sections] == list(SECTION_KEYS)


def section_heading(key):
    if key == "theoretical_basis":
        return "4.1 理论基础", 2
    if key == "research_status":
        return "4.2 国内外研究现状", 2
    number = {"background": 1, "significance": 2, "approach": 3, "achievements": 5}[key]
    return f"{number} {SECTION_TITLES[key]}", 1
