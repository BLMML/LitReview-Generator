"""reviews.models：Review、不可变 ReviewRevision、ValidationReport、Export。

职责（docs/mvp/01 第 4.6 节、docs/mvp/02 第 4.1—4.3 节）：
- 关联确切 Run、analysis_revision、正文版本与核验报告；
- 修改正文新增 revision；已完成报告不能指向新正文继续复用；
- 允许同版本多次核验与多次独立导出，不能对 revision 加一对一约束。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import CreatedAtModel, TimeStampedModel

# 历史五节结构用于兼容旧版本；新稿固定框架见 reviews.structure。
SECTION_KEYS = (
    "background",
    "directions",
    "representative_work",
    "disagreements",
    "trends",
)

BLOCK_KIND_CLAIM = "claim"
BLOCK_KIND_TRANSITION = "transition"
BLOCK_KINDS = (BLOCK_KIND_CLAIM, BLOCK_KIND_TRANSITION)

REVISION_SOURCE_GENERATED = "generated"
REVISION_SOURCE_MANUAL = "manual"
REVISION_SOURCES = (REVISION_SOURCE_GENERATED, REVISION_SOURCE_MANUAL)

VALIDATION_RESULT_PASSED = "passed"
VALIDATION_RESULT_NEEDS_REVIEW = "needs_review"
VALIDATION_RESULTS = (VALIDATION_RESULT_PASSED, VALIDATION_RESULT_NEEDS_REVIEW)

# 逐 block 的支持度
SUPPORT_SUPPORTED = "supported"
SUPPORT_UNSUPPORTED = "unsupported"
SUPPORT_UNCERTAIN = "uncertain"
SUPPORT_NOT_APPLICABLE = "not_applicable"
SUPPORT_VALUES = (SUPPORT_SUPPORTED, SUPPORT_UNSUPPORTED, SUPPORT_UNCERTAIN, SUPPORT_NOT_APPLICABLE)

EXPORT_STATUS_PENDING = "pending"
EXPORT_STATUS_READY = "ready"
EXPORT_STATUS_FAILED = "failed"
EXPORT_STATUS_CANCELLED = "cancelled"
EXPORT_STATUSES = (
    EXPORT_STATUS_PENDING,
    EXPORT_STATUS_READY,
    EXPORT_STATUS_FAILED,
    EXPORT_STATUS_CANCELLED,
)


class Review(CreatedAtModel):
    """一份综述。首次受理只创建 Review，没有空白 revision 0。"""

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="reviews",
    )
    run = models.ForeignKey(
        "analysis.AnalysisRun",
        on_delete=models.PROTECT,
        related_name="reviews",
    )
    title = models.CharField(max_length=settings.MVP_LIMITS["REVIEW_TITLE_MAX"])
    # 冻结生成时的 analysis_revision
    analysis_revision = models.PositiveBigIntegerField()
    target_chars = models.SmallIntegerField(default=settings.MVP_LIMITS["REVIEW_TARGET_CHARS_DEFAULT"])
    style = models.CharField(max_length=32, default=settings.REVIEW_STYLE_DEFAULT)
    # 创建即冻结生成参数；不随环境配置变更
    config_snapshot = models.JSONField(default=dict)

    class Meta:
        db_table = "reviews_review"
        constraints = [
            models.CheckConstraint(
                condition=Q(target_chars__gte=settings.MVP_LIMITS["REVIEW_TARGET_CHARS_MIN"])
                & Q(target_chars__lte=settings.MVP_LIMITS["REVIEW_TARGET_CHARS_MAX"]),
                name="review_target_chars_range",
            ),
            models.CheckConstraint(
                condition=Q(style__in=settings.REVIEW_STYLES_ALLOWED), name="review_style_known"
            ),
        ]
        indexes = [
            models.Index(fields=["project", "-created_at", "-id"], name="review_project_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Review({self.title})"


class ReviewRevision(CreatedAtModel):
    """不可变正文版本；正文不可原位更新，编辑保存创建新版本。"""

    review = models.ForeignKey(
        Review,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    # 与 Review.run 一致，固定证据范围
    run = models.ForeignKey(
        "analysis.AnalysisRun",
        on_delete=models.PROTECT,
        related_name="revisions",
    )

    revision_number = models.PositiveBigIntegerField()
    analysis_revision = models.PositiveBigIntegerField()
    corpus_revision = models.PositiveBigIntegerField()

    # 固定内容槽；每槽 {section_key,title,blocks}，历史五节或新稿六槽。
    sections = models.JSONField(default=list)
    config_snapshot = models.JSONField(default=dict)

    source = models.CharField(max_length=16)
    # 始终为发起操作的用户；generated 另有 source_job
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    # 非空时唯一：阻止同一生成任务重复提交
    source_job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="produced_revisions",
    )
    # revision 1 为 null；手工新版本指向保存时最新版本
    base_revision = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="derived_revisions",
    )
    # 对规范化 sections 计算；用于核验与导出完整性检查
    content_hash = models.CharField(max_length=64)

    class Meta:
        db_table = "reviews_review_revision"
        constraints = [
            models.UniqueConstraint(
                fields=["review", "revision_number"], name="revision_number_unique_per_review"
            ),
            models.UniqueConstraint(
                fields=["source_job"],
                condition=Q(source_job__isnull=False),
                name="revision_source_job_unique",
            ),
            models.CheckConstraint(condition=Q(revision_number__gte=1), name="revision_number_positive"),
            models.CheckConstraint(condition=Q(source__in=REVISION_SOURCES), name="revision_source_known"),
        ]
        indexes = [
            models.Index(fields=["review", "-revision_number"], name="revision_review_number_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ReviewRevision(#{self.revision_number})"


class ValidationReport(TimeStampedModel):
    """对某个正文版本的结构与语义核验；唯一受检正文，不可换绑。

    后台调用失败时保持 result / structure_passed / blocks 为 null，
    不伪装成“已完成核验”。
    """

    revision = models.ForeignKey(
        ReviewRevision,
        on_delete=models.PROTECT,
        related_name="validations",
    )
    # 受理时冻结正文哈希，防止错误关联
    content_hash = models.CharField(max_length=64)
    config_snapshot = models.JSONField(default=dict)

    result = models.CharField(max_length=16, null=True, blank=True)
    structure_passed = models.BooleanField(null=True, blank=True)
    # [{block_id,structure_ok,support,issues,suggestion}]
    blocks = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "reviews_validation_report"
        constraints = [
            models.CheckConstraint(
                condition=Q(result__isnull=True) | Q(result__in=VALIDATION_RESULTS),
                name="validation_result_known",
            ),
        ]
        indexes = [
            models.Index(fields=["revision", "-created_at", "-id"], name="validation_rev_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ValidationReport({self.result or 'pending'})"


class Export(TimeStampedModel):
    """固定正文版本和核验报告的导出记录；所有导出都是草稿。"""

    revision = models.ForeignKey(
        ReviewRevision,
        on_delete=models.PROTECT,
        related_name="exports",
    )
    # 与 revision 必须是同一版本；API 字段为 validation_id
    validation_report = models.ForeignKey(
        ValidationReport,
        on_delete=models.PROTECT,
        related_name="exports",
    )

    status = models.CharField(max_length=16, default=EXPORT_STATUS_PENDING)
    # 受理时冻结过时、历史版本与核验问题警示
    acknowledge_warnings = models.BooleanField(default=False)
    warnings = models.JSONField(default=list, blank=True)

    # 固定正文与 Word 模板版本
    content_hash = models.CharField(max_length=64)
    template_revision = models.CharField(max_length=64)

    # ready 时必须有完整 DOCX；失败有原因
    file_asset = models.ForeignKey(
        "common.FileAsset",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="exports",
    )
    error = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "reviews_export"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=EXPORT_STATUSES), name="export_status_known"),
        ]
        indexes = [
            models.Index(fields=["revision", "-created_at", "-id"], name="export_revision_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Export({self.status})"
