"""jobs.models：Job —— 数据库是任务真实状态的唯一来源。

职责（docs/mvp/01 第 4.7 节、docs/mvp/02 第 5.1 节）：
- Job、输入快照、状态、阶段、进度、结果引用、取消意图、尝试 / 租约与错误；
- Celery task ID 不是公开的业务 Job ID，也不作为状态来源。

关键约束：
- 每 project 仅一条 status ∈ queued/running/cancelling（部分唯一索引）；
- 唯一 `(resource_type, resource_id, attempt)`：重试创建新 attempt，历史不被改写；
- resource_id 是内部限定多态关联，数据库无法建跨表 FK，由统一 accept_job 分发校验。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel

# ── 状态与类型枚举（docs/mvp/00 第 5 节、docs/mvp/02 第 5.1 节）──
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_CANCELLING = "cancelling"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

JOB_STATUSES = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_CANCELLING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)
ACTIVE_JOB_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING, JOB_STATUS_CANCELLING)
TERMINAL_JOB_STATUSES = (JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)

JOB_KIND_ANALYZE = "analyze"
JOB_KIND_RETRIEVE = "retrieve"
JOB_KIND_GENERATE_REVIEW = "generate_review"
JOB_KIND_VALIDATE_REVIEW = "validate_review"
JOB_KIND_EXPORT_DOCX = "export_docx"
JOB_KIND_IMPORT_ARXIV = "import_arxiv"

# kind → resource_type（docs/mvp/02 第 5.1 节固定映射）
RESOURCE_TYPE_ANALYSIS_RUN = "analysis_run"
RESOURCE_TYPE_RETRIEVAL_RESULT = "retrieval_result"
RESOURCE_TYPE_REVIEW = "review"
RESOURCE_TYPE_VALIDATION_REPORT = "validation_report"
RESOURCE_TYPE_EXPORT = "export"
RESOURCE_TYPE_IMPORT_BATCH = "import_batch"

JOB_KIND_RESOURCE_TYPE = {
    JOB_KIND_ANALYZE: RESOURCE_TYPE_ANALYSIS_RUN,
    JOB_KIND_RETRIEVE: RESOURCE_TYPE_RETRIEVAL_RESULT,
    JOB_KIND_GENERATE_REVIEW: RESOURCE_TYPE_REVIEW,
    JOB_KIND_VALIDATE_REVIEW: RESOURCE_TYPE_VALIDATION_REPORT,
    JOB_KIND_EXPORT_DOCX: RESOURCE_TYPE_EXPORT,
    JOB_KIND_IMPORT_ARXIV: RESOURCE_TYPE_IMPORT_BATCH,
}

# 阶段名取自 docs/mvp/04 第 3 节分析流水线与第 5 节生成链；
# 各任务只允许使用自己那条链上的阶段名。
STAGE_ACCEPTED = "accepted"
ANALYSIS_STAGES = ("snapshot", "parse", "extract", "index", "topics", "disputes", "finalize")
REVIEW_STAGES = (
    "freeze_inputs",
    "retrieve_sections",
    "generate_sections",
    "check_structure",
    "commit_revision",
)
RETRIEVAL_STAGES = ("expand_query", "recall", "rerank", "publish")
VALIDATION_STAGES = ("structure", "claim_support", "summarize")
EXPORT_STAGES = ("render", "publish")
IMPORT_STAGES = ("resolve", "download", "register")


def default_progress() -> dict[str, object]:
    """进度默认值；每行独立创建，绝不共享可变默认对象。"""
    return {"completed": 0, "total": None, "unit": "step"}


class Job(TimeStampedModel):
    """一次后台执行记录。lease_token 是单调递增的 fencing 标识，不是客户端字段。"""

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_jobs",
    )
    kind = models.CharField(max_length=32)
    status = models.CharField(max_length=16, default=JOB_STATUS_QUEUED)

    # 内部限定多态关联：由 accept_job 校验目标存在、项目归属与类型
    resource_type = models.CharField(max_length=32)
    resource_id = models.UUIDField()

    # 固定业务输入、版本与配置；重试复用同一份
    frozen_input = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=64)

    stage = models.CharField(max_length=32, default=STAGE_ACCEPTED)
    progress = models.JSONField(default=default_progress)

    result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)

    retry_of_job = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="retries",
    )
    attempt = models.PositiveIntegerField(default=1)

    # token 单调递增；非客户端字段
    lease_token = models.BigIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # 可靠补投信息；不以队列 ACK 推导成功
    dispatch_attempts = models.IntegerField(default=0)
    last_dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jobs_job"
        constraints = [
            # 每项目同时最多一个活跃任务
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(status__in=ACTIVE_JOB_STATUSES),
                name="job_one_active_per_project",
            ),
            models.UniqueConstraint(
                fields=["resource_type", "resource_id", "attempt"],
                name="job_resource_attempt_unique",
            ),
            models.CheckConstraint(condition=Q(attempt__gte=1), name="job_attempt_positive"),
            models.CheckConstraint(
                condition=Q(lease_token__gte=0), name="job_lease_token_non_negative"
            ),
        ]
        indexes = [
            models.Index(fields=["project", "-created_at", "-id"], name="job_project_created_idx"),
            models.Index(fields=["status", "lease_expires_at"], name="job_status_lease_idx"),
            models.Index(
                fields=["resource_type", "resource_id", "-created_at", "-id"],
                name="job_resource_created_idx",
            ),
            models.Index(
                fields=["status", "last_dispatched_at"], name="job_status_dispatched_idx"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Job({self.kind}, {self.status}, attempt={self.attempt})"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES
