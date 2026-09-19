"""llm.client：唯一 DeepSeek 适配器。

职责（docs/mvp/01 第 4.8 节）：
- 文字 / 图片输入、结构化输出、超时、限流、有限重试、取消检查与错误映射；
- 入口：`call_structured(context, request: ModelTaskRequest) → ModelTaskResult`
  （context 为 ExecutionContext）。

硬性边界：
- **Web 进程不得调用 DeepSeek**；解析图像辅助、查询扩展、重排、分析、生成与语义核验
  全部通过本适配器；
- 禁止其他 app 私建供应商客户端，禁止前端直接请求供应商；
- 不接受模型文本指定任意 URL 或文件路径；
- 正式模式（production + LLM_MODE=real）在 API 失败时**不得静默返回模拟结果**。

**骨架状态**：未实现。mock 结果由 backend/tests/fixtures/llm_cases.json 提供。
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.common.http import api_error
from apps.llm.schemas import ModelTaskRequest, ModelTaskResult

NOT_IMPLEMENTED_MESSAGE = "llm.client 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"


def is_mock_mode() -> bool:
    return settings.LLM_MODE == "mock"


def call_structured(context: Any, request: ModelTaskRequest) -> ModelTaskResult:
    """执行一次结构化调用。

    调用前必须完成预算预留与全局并发名额认领；
    网络尝试最多 LLM_MAX_ATTEMPTS 次（含最多一次输出格式修复），分别记账。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def call_structured_mock(request: ModelTaskRequest, case_name: str) -> ModelTaskResult:
    """从固定样例返回结果，仅供测试与 mock 模式；不产生任何网络请求。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
