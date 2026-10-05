"""解析任务的有效配置；同一快照用于执行和缓存身份。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from django.conf import settings

from apps.common.http import api_error
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import TASK_PAGE_EXTRACT

SCHEMA_VERSION = "1.0"


def effective_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """首次解析补齐默认值；完整快照可在后续任务中原样传回。"""
    prompt = load_prompt(
        "extract_paper.md",
        {"paper_title": "", "page_number": 1, "page_content": ""},
        task_type=TASK_PAGE_EXTRACT,
    )
    defaults = {
        "PARSER_REVISION": settings.PARSER_REVISION,
        "NORMALIZATION_REVISION": settings.NORMALIZATION_REVISION,
        "EVIDENCE_CHUNK_MIN_CHARS": settings.EVIDENCE_CHUNK_MIN_CHARS,
        "EVIDENCE_CHUNK_MAX_CHARS": settings.EVIDENCE_CHUNK_MAX_CHARS,
        "PAGE_RENDER_SCALE": settings.PAGE_RENDER_SCALE,
        "PAGE_IMAGE_MAX_BYTES": settings.PAGE_IMAGE_MAX_BYTES,
        "PAGE_IMAGES_PER_CALL": settings.PAGE_IMAGES_PER_CALL,
        "PAGE_TEMPLATE_VERSION": prompt.version,
        "SCHEMA_VERSION": SCHEMA_VERSION,
        "MODEL_CONFIG": {
            "model": settings.DEEPSEEK_MODEL,
            "thinking": settings.DEEPSEEK_THINKING_MODE,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            "temperature": 0,
        },
    }
    if overrides is not None and not isinstance(overrides, Mapping):
        raise api_error("VALIDATION_ERROR", "parser_config 必须是对象")
    supplied = dict(overrides or {})
    unknown = supplied.keys() - defaults.keys()
    if unknown:
        raise api_error("VALIDATION_ERROR", "未知解析配置字段", {"fields": sorted(unknown)})
    config = {**defaults, **supplied}
    for name in ("PARSER_REVISION", "NORMALIZATION_REVISION", "PAGE_TEMPLATE_VERSION", "SCHEMA_VERSION"):
        if config[name] != defaults[name]:
            raise api_error("VALIDATION_ERROR", f"当前实现不支持冻结的 {name}，请保留原环境或创建新任务")
    minimum = _integer(config, "EVIDENCE_CHUNK_MIN_CHARS", 1, 12000)
    maximum = _integer(config, "EVIDENCE_CHUNK_MAX_CHARS", 1, 12000)
    if minimum > maximum:
        raise api_error("VALIDATION_ERROR", "切片最小长度不能大于最大长度")
    _integer(config, "PAGE_IMAGE_MAX_BYTES", 1, 5 * 1024 * 1024)
    _integer(config, "PAGE_IMAGES_PER_CALL", 1, 2)
    scale = config["PAGE_RENDER_SCALE"]
    if type(scale) not in (int, float) or not math.isfinite(scale) or not 0 < scale <= 4:
        raise api_error("VALIDATION_ERROR", "PAGE_RENDER_SCALE 必须在 (0, 4] 内")
    config["PAGE_RENDER_SCALE"] = float(scale)
    model = config["MODEL_CONFIG"]
    if not isinstance(model, Mapping) or model.keys() - defaults["MODEL_CONFIG"].keys():
        raise api_error("VALIDATION_ERROR", "MODEL_CONFIG 字段不合法")
    model = {**defaults["MODEL_CONFIG"], **model}
    if model["model"] != settings.DEEPSEEK_MODEL or model["thinking"] not in ("enabled", "disabled"):
        raise api_error("VALIDATION_ERROR", "冻结模型必须符合当前已核验的模型与计价配置")
    _integer(model, "max_tokens", 1, settings.LLM_MAX_OUTPUT_TOKENS)
    temperature = model["temperature"]
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise api_error("VALIDATION_ERROR", "temperature 不合法")
    config["MODEL_CONFIG"] = dict(model)
    return config


def _integer(values: Mapping[str, Any], name: str, minimum: int, maximum: int) -> int:
    value = values[name]
    if type(value) is not int or not minimum <= value <= maximum:
        raise api_error("VALIDATION_ERROR", f"{name} 必须是 {minimum}—{maximum} 的整数")
    return value
