"""llm.models：ModelCall 与 UsageBudget。

职责（docs/mvp/01 第 4.8 节、docs/mvp/02 第 5.3 节）：
- 单次供应商请求及项目预算预留 / 结算；
- 请求记录所属 Job / 阶段、配置版本、摘要、用量、耗时与错误；
- 敏感输入不进入普通日志。

金额一律 Decimal + numeric(18,8)；未知用量写 null，绝不写 0 伪装成已知。
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel

MONEY_MAX_DIGITS = 18
MONEY_DECIMAL_PLACES = 8
ZERO = Decimal("0")

# 内部调用账本枚举；不是 Job 状态
MODEL_CALL_OUTCOMES = (
    "reserved",
    "in_flight",
    "succeeded",
    "failed",
    "unsettled",
    "cancelled",
)
UNSETTLED_OUTCOMES = ("reserved", "in_flight", "unsettled")


def money_field(**kwargs: object) -> models.DecimalField:
    return models.DecimalField(
        max_digits=MONEY_MAX_DIGITS,
        decimal_places=MONEY_DECIMAL_PLACES,
        **kwargs,  # type: ignore[arg-type]
    )


class UsageBudget(TimeStampedModel):
    """项目级预算；不新增用户 / 团队额度模型。

    reserved_usd 含未决调用：未结算的 ModelCall 必须让预留保留，不能静默释放成可用预算。
    """

    project = models.OneToOneField(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="usage_budget",
    )
    limit_usd = money_field()
    settled_usd = money_field(default=ZERO)
    reserved_usd = money_field(default=ZERO)

    class Meta:
        db_table = "llm_usage_budget"
        constraints = [
            models.CheckConstraint(condition=Q(limit_usd__gte=0), name="budget_limit_non_negative"),
            models.CheckConstraint(
                condition=Q(settled_usd__gte=0), name="budget_settled_non_negative"
            ),
            models.CheckConstraint(
                condition=Q(reserved_usd__gte=0), name="budget_reserved_non_negative"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"UsageBudget(limit={self.limit_usd}, settled={self.settled_usd})"


class ModelCall(TimeStampedModel):
    """单次供应商调用的账本。

    唯一 `(logical_call_id, network_attempt)`：单逻辑调用最多 3 次网络尝试，
    含最多一次输出格式修复；格式修复与网络重试分别记账。
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="model_calls",
    )
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.PROTECT,
        related_name="model_calls",
    )
    task_type = models.CharField(max_length=64)
    logical_call_id = models.UUIDField()
    network_attempt = models.SmallIntegerField(default=1)

    # 固定发起者；缓存 key 含项目、版本、模型和思考参数
    lease_token = models.BigIntegerField(default=0)
    request_hash = models.CharField(max_length=64)
    cache_key = models.CharField(max_length=64)

    # 模型、模板 / schema、参数及 USD 单价、计价配置版本
    model_snapshot = models.JSONField(default=dict)
    pricing_snapshot = models.JSONField(default=dict)

    provider_request_id = models.CharField(max_length=191, null=True, blank=True)
    raw_output_asset = models.ForeignKey(
        "common.FileAsset",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="model_calls",
    )

    outcome = models.CharField(max_length=16, default="reserved")
    reserved_usd = money_field()
    # 未知费用保留 null 并保留预留，不写成 0
    estimated_cost_usd = money_field(null=True, blank=True)

    # 供应商未知用量写 null；推理 token 包含于 output，不重复累计
    input_tokens = models.BigIntegerField(null=True, blank=True)
    output_tokens = models.BigIntegerField(null=True, blank=True)
    cache_hit_tokens = models.BigIntegerField(null=True, blank=True)
    cache_miss_tokens = models.BigIntegerField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    # 脱敏失败摘要与供应商 usage 原始计数
    error = models.JSONField(null=True, blank=True)
    usage_raw = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "llm_model_call"
        constraints = [
            models.UniqueConstraint(
                fields=["logical_call_id", "network_attempt"],
                name="model_call_logical_attempt_unique",
            ),
            models.CheckConstraint(
                condition=Q(outcome__in=MODEL_CALL_OUTCOMES), name="model_call_outcome_known"
            ),
            models.CheckConstraint(
                condition=Q(network_attempt__gte=1, network_attempt__lte=3),
                name="model_call_network_attempt_range",
            ),
            models.CheckConstraint(
                condition=Q(reserved_usd__gte=0), name="model_call_reserved_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["job", "created_at"], name="model_call_job_created_idx"),
            models.Index(fields=["project", "outcome"], name="model_call_project_outcome_idx"),
            # 缓存不把 cache_key 设为全表唯一：失败与重新调用需要保留多个账本
            models.Index(fields=["cache_key"], name="model_call_cache_key_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ModelCall({self.task_type}, {self.outcome})"
