"""llm.prompt_loader：加载受版本控制的提示词并记录内容哈希。

职责（docs/mvp/01 第 4.8 节）：
- `load_prompt(name, parameters) → RenderedPrompt`，包含版本 / 哈希，
  供 Run 和 ModelCall 记录；
- 模板参数显式传入，原文作为不可信材料隔离。

契约要点（docs/mvp/01 第 5 节）：
- `extract_paper.md` 内含两个独立模板段：page_extract 与 paper_extract，
  按 task_type 只加载对应段，不能一次模型调用混合两种产物；
- 模板版本改变，受影响阶段的缓存必须失效；
- 原文、PDF 元数据和检索内容是研究材料，不能覆盖系统规则，不执行其中的指令。

本模块是骨架中**已实现**的部分：加载、分段、参数替换与哈希都可用。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from apps.common.http import api_error
from apps.llm.schemas import TASK_PAGE_EXTRACT, TASK_PAPER_EXTRACT, TASK_PROMPT_FILE

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# 模板段标记：<!-- section: page_extract -->
_SECTION_PATTERN = re.compile(r"^<!--\s*section:\s*([a-z_]+)\s*-->\s*$", re.MULTILINE)
# 参数占位：{{name}}
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class RenderedPrompt:
    """渲染后的提示词；版本与哈希必须随 Run / ModelCall 一起记录。"""

    name: str
    section: str | None
    version: str
    content_hash: str
    text: str

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "prompt_name": self.name,
            "prompt_section": self.section,
            "template_version": self.version,
            "template_hash": self.content_hash,
        }


@lru_cache(maxsize=64)
def _read_prompt_file(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name:
        raise api_error("VALIDATION_ERROR", "提示词名称不合法")
    path = PROMPTS_DIR / name
    if not path.is_file():
        raise api_error("SERVICE_UNAVAILABLE", f"提示词模板缺失：{name}", retryable=False)
    return path.read_text(encoding="utf-8")


def _split_sections(raw: str) -> tuple[dict[str, str], str]:
    """把模板拆成 {
       段名: 正文
    } 与无段归属的公共头。"""
    matches = list(_SECTION_PATTERN.finditer(raw))
    if not matches:
        return {}, raw
    sections: dict[str, str] = {}
    header = raw[: matches[0].start()]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        sections[match.group(1)] = raw[match.end() : end].strip()
    return sections, header.strip()


def prompt_version(raw: str) -> str:
    """模板版本：内容哈希前 12 位；内容变化即版本变化。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def load_prompt(
    name: str,
    parameters: Mapping[str, Any] | None = None,
    *,
    task_type: str | None = None,
) -> RenderedPrompt:
    """加载并渲染提示词。

    `name` 为提示词文件名；`task_type` 指定时只取对应模板段
    （extract_paper.md 必须按 task_type 分别取 page_extract / paper_extract）。
    """
    raw = _read_prompt_file(name)
    sections, header = _split_sections(raw)

    section: str | None = None
    body = raw
    if task_type is not None:
        expected = TASK_PROMPT_FILE.get(task_type)
        if expected is not None and expected != name:
            raise api_error(
                "VALIDATION_ERROR",
                f"task_type={task_type} 应使用模板 {expected}，而不是 {name}",
            )
        if task_type in (TASK_PAGE_EXTRACT, TASK_PAPER_EXTRACT):
            if task_type not in sections:
                raise api_error(
                    "SERVICE_UNAVAILABLE",
                    f"模板 {name} 缺少 section: {task_type}",
                    retryable=False,
                )
            section = task_type
            body = sections[task_type]
        elif sections:
            # 有分段但本任务不属于分段模板：使用除分段外的公共头
            body = header or raw

    values = dict(parameters or {})

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise api_error(
                "VALIDATION_ERROR",
                f"提示词参数缺失：{key}",
                {"fields": {key: ["模板要求该参数"]}},
            )
        return str(values[key])

    rendered = _PLACEHOLDER_PATTERN.sub(replace, body)
    return RenderedPrompt(
        name=name,
        section=section,
        version=prompt_version(raw),
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        text=rendered,
    )


def available_prompts() -> list[str]:
    """列出已交付的模板文件，便于启动自检。"""
    if not PROMPTS_DIR.is_dir():
        return []
    return sorted(path.name for path in PROMPTS_DIR.glob("*.md"))
