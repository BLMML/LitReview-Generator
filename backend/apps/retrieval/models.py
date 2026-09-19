"""retrieval.models：RetrievalResult。

职责（docs/mvp/01 第 4.5 节、docs/mvp/02 第 4.1 节）：
- 冻结查询、查询扩展、候选与重排结果、Run 关联和生成配置；
- **不创建向量字段，不引入 pgvector**。

公开 results 的 score ∈ [0,1] 只是排序分，不是事实正确概率。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel


class RetrievalResult(TimeStampedModel):
    """一次检索的完整记录；完成前 expanded_queries/candidates/results 为 null。"""

    run = models.ForeignKey(
        "analysis.AnalysisRun",
        on_delete=models.PROTECT,
        related_name="retrievals",
    )
    query = models.TextField()

    analysis_revision = models.PositiveBigIntegerField()
    candidate_limit = models.SmallIntegerField(
        default=settings.MVP_LIMITS["RETRIEVAL_CANDIDATE_LIMIT_DEFAULT"]
    )
    top_k = models.SmallIntegerField(default=settings.MVP_LIMITS["RETRIEVAL_TOP_K_DEFAULT"])

    # 索引版本、扩展 / 重排模板和模型配置
    config_snapshot = models.JSONField(default=dict)

    # 完成前 null；成功一次性发布，成功但无证据时 results=[]
    expanded_queries = models.JSONField(null=True, blank=True)
    # 内部候选：[{evidence_id,recall_score,matched_queries}]
    candidates = models.JSONField(null=True, blank=True)
    # 公开结果：[{evidence_id,rank,score,reason}]，rank 从 1 连续起
    results = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "retrieval_retrieval_result"
        constraints = [
            models.CheckConstraint(
                condition=Q(candidate_limit__gte=5) & Q(candidate_limit__lte=50),
                name="retrieval_candidate_limit_range",
            ),
            models.CheckConstraint(
                condition=Q(top_k__gte=1)
                & Q(top_k__lte=settings.MVP_LIMITS["RETRIEVAL_TOP_K_MAX"]),
                name="retrieval_top_k_range",
            ),
            # top_k 不得超过 candidate_limit
            models.CheckConstraint(
                condition=Q(top_k__lte=models.F("candidate_limit")),
                name="retrieval_top_k_within_candidates",
            ),
            models.CheckConstraint(
                condition=Q(query__isnull=False), name="retrieval_query_present"
            ),
        ]
        indexes = [
            models.Index(fields=["run", "-created_at", "-id"], name="retrieval_run_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"RetrievalResult({self.query[:32]!r})"
