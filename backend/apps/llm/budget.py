"""llm.budget：调用前预留、并发控制与调用后结算。

职责（docs/mvp/01 第 4.8 节、docs/mvp/02 第 5.3 节）：
- `reserve_call(context, maximum_usage) → Reservation`
- `settle_call(reservation, usage)`
- `check_budget(context)`

规则：
- 按输入 / 输出上界在调用前预留预算，缓存命中与未命中分开计价；
- 推理 token 已包含在输出用量时不得重复收费；
- 实际用量缺失时保守记账；未知结算状态不自动视为零成本；
- 项目预算行与 ModelCall 预留 / 结算在同一短事务内锁定；
- 部署级最大并发（LLM_MAX_CONCURRENCY）必须全局共享，不能每 worker 各自限流；
- 认领名额与预算的短事务绝不持锁等待网络。

**骨架状态**：未实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.common.http import api_error
from apps.llm.schemas import ModelUsage

NOT_IMPLEMENTED_MESSAGE = "llm.budget 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"


@dataclass(frozen=True)
class Reservation:
    """一次调用的预算预留凭据；结算前必须保持占用。"""

    call_id: str
    reserved_usd: Decimal
    model_call_pk: Any = None


def check_budget(context: Any) -> None:
    """预算或额度不足时抛 BUDGET_EXCEEDED；零 / 未配置不得被解释为无限预算。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def reserve_call(context: Any, maximum_usage: ModelUsage) -> Reservation:
    """在独立短事务内认领全局并发名额并预留预算；失败即拒绝本次真实调用。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def settle_call(reservation: Reservation, usage: ModelUsage) -> None:
    """结算：settled 增加实际可估金额，reserved 减少对应预留；重复结算不得二次变动。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
