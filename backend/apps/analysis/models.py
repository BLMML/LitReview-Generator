"""analysis.models：AnalysisRun、RunPaper、PaperAnalysis、Topic、Dispute。

职责（docs/mvp/01 第 4.4 节、docs/mvp/02 第 3.1 / 3.3 节）：
- 冻结语料、元数据与配置；保留人工复核独立字段；
- Run 不读取后来修改的 Paper 元数据替换快照；
- 一个 Run 中任一 RunPaper 失败则 Run 失败，成功阶段保留。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import CreatedAtModel, TimeStampedModel

# ── AnalysisRun 状态 ─────────────────────────────────────────
RUN_PROCESSING = "processing"
RUN_READY = "ready"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_STATUSES = (RUN_PROCESSING, RUN_READY, RUN_FAILED, RUN_CANCELLED)

# ── RunPaper 状态 ────────────────────────────────────────────
RUN_PAPER_PENDING = "pending"
RUN_PAPER_PROCESSING = "processing"
RUN_PAPER_READY = "ready"
RUN_PAPER_FAILED = "failed"
RUN_PAPER_STATUSES = (
    RUN_PAPER_PENDING,
    RUN_PAPER_PROCESSING,
    RUN_PAPER_READY,
    RUN_PAPER_FAILED,
)

# ── Dispute 分类与人工复核 ───────────────────────────────────
DISPUTE_POSSIBLE_CONFLICT = "possible_conflict"
DISPUTE_DIFFERENT_CONDITIONS = "different_conditions"
DISPUTE_COMPLEMENTARY = "complementary"
DISPUTE_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
DISPUTE_CLASSIFICATIONS = (
    DISPUTE_POSSIBLE_CONFLICT,
    DISPUTE_DIFFERENT_CONDITIONS,
    DISPUTE_COMPLEMENTARY,
    DISPUTE_INSUFFICIENT_EVIDENCE,
)

REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_CONFIRMED = "confirmed"
REVIEW_STATUS_REJECTED = "rejected"
REVIEW_STATUSES = (REVIEW_STATUS_PENDING, REVIEW_STATUS_CONFIRMED, REVIEW_STATUS_REJECTED)


class AnalysisRun(TimeStampedModel):
    """一次冻结研究问题、语料、元数据和配置的分析快照。

    corpus_revision 创建时冻结、不可改；is_stale 由其与 Project 当前值比较得到（计算字段）。
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="runs",
    )
    status = models.CharField(max_length=16, default=RUN_PROCESSING)

    # 冻结当前值，不可改
    corpus_revision = models.PositiveBigIntegerField()
    # 人工调整争议复核后递增
    analysis_revision = models.PositiveBigIntegerField(default=1)

    # 创建时从 Project 复制；后续 Project 改字不改变此快照
    research_question = models.TextField()
    output_language = models.CharField(max_length=16)

    # 模型 / 解析器 / 模板 / schema / 检索配置和依赖锁摘要；不含密钥
    config_snapshot = models.JSONField(default=dict)

    # 完整索引发布后一次性填写
    index_asset = models.ForeignKey(
        "common.FileAsset",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="indexed_runs",
    )
    index_manifest = models.JSONField(null=True, blank=True)

    # 当前尝试最终业务错误；历史错误保留在旧 Job
    error = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "analysis_analysis_run"
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=RUN_STATUSES), name="run_status_known"
            ),
            models.CheckConstraint(
                condition=Q(analysis_revision__gte=1), name="run_analysis_revision_positive"
            ),
            models.CheckConstraint(
                condition=Q(corpus_revision__gte=1), name="run_corpus_revision_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["project", "-created_at", "-id"], name="run_project_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"AnalysisRun({self.status}, corpus={self.corpus_revision})"


class RunPaper(TimeStampedModel):
    """Run 中的文献快照：记录纳入时的标题、作者、年份、文件哈希与解析结果。"""

    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.PROTECT,
        related_name="run_papers",
    )
    paper = models.ForeignKey(
        "literature.Paper",
        on_delete=models.PROTECT,
        related_name="run_papers",
    )

    # 从 Paper 冻结，不跟随原论文编辑
    title = models.CharField(max_length=500)
    authors = models.JSONField(default=list, blank=True)
    year = models.SmallIntegerField(null=True, blank=True)
    source = models.CharField(max_length=16)
    source_id = models.CharField(max_length=64, null=True, blank=True)
    source_url = models.TextField(null=True, blank=True)

    # 与纳入时 PDF 完全一致
    file_asset = models.ForeignKey(
        "common.FileAsset",
        on_delete=models.PROTECT,
        related_name="run_papers",
    )
    file_sha256 = models.CharField(max_length=64)
    page_count = models.SmallIntegerField()

    status = models.CharField(max_length=16, default=RUN_PAPER_PENDING)
    # 完整解析成功后填 FK；失败有错误
    parsed_document = models.ForeignKey(
        "parsing.ParsedDocument",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="run_papers",
    )
    error = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "analysis_run_paper"
        constraints = [
            models.UniqueConstraint(fields=["run", "paper"], name="run_paper_unique"),
            models.CheckConstraint(
                condition=Q(status__in=RUN_PAPER_STATUSES), name="run_paper_status_known"
            ),
            models.CheckConstraint(
                condition=Q(page_count__gte=1), name="run_paper_page_count_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["run", "status"], name="run_paper_run_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"RunPaper({self.title}, {self.status})"


class PaperAnalysis(CreatedAtModel):
    """每篇每 Run 仅一个完整分析。payload 的结构校验见 analysis.schemas。"""

    run_paper = models.OneToOneField(
        RunPaper,
        on_delete=models.PROTECT,
        related_name="analysis",
    )
    # 必含 research_question / research_object / summary 与五个 EvidenceText 数组
    payload = models.JSONField(default=dict)
    # 生成时模型 / 模板 / schema / 证据哈希
    generation_snapshot = models.JSONField(default=dict)
    # 关联已成功完整调用；服务层验证本项目与 Job
    model_call_ids = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "analysis_paper_analysis"

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"PaperAnalysis(run_paper={self.run_paper_id})"


class Topic(CreatedAtModel):
    """模型主题归纳结果；允许一文多主题，成员至少 1 篇且全部属于本 Run。"""

    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.PROTECT,
        related_name="topics",
    )
    name = models.CharField(max_length=200)
    # 可空串，不能用模型补造事实
    description = models.TextField(blank=True, default="")
    paper_ids = models.JSONField(default=list)
    evidence_ids = models.JSONField(default=list)

    class Meta:
        db_table = "analysis_topic"
        indexes = [
            models.Index(fields=["run", "created_at", "id"], name="topic_run_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Topic({self.name})"


class Dispute(TimeStampedModel):
    """争议候选。

    自动 classification / reason / claims 不可被人工 PATCH 覆盖；
    review_status / review_note / version 是独立的人工字段。

    不设置“每论文对只有一条”的唯一约束：一对论文可以在不同论断上有不同结果。
    """

    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.PROTECT,
        related_name="disputes",
    )
    left_paper = models.ForeignKey(
        "literature.Paper",
        on_delete=models.PROTECT,
        related_name="disputes_as_left",
    )
    right_paper = models.ForeignKey(
        "literature.Paper",
        on_delete=models.PROTECT,
        related_name="disputes_as_right",
    )

    # 两侧均为 {value,evidence_ids}；证据分别属于对应论文
    left_claim = models.JSONField(default=dict)
    right_claim = models.JSONField(default=dict)
    # {research_question,left_conditions,right_conditions,comparability_reason}，四项必填
    conditions = models.JSONField(default=dict)

    classification = models.CharField(max_length=32)
    reason = models.TextField()

    # 人工复核字段；乐观锁
    review_status = models.CharField(max_length=16, default=REVIEW_STATUS_PENDING)
    review_note = models.TextField(blank=True, default="")
    version = models.PositiveBigIntegerField(default=1)
    # 人工复核审计；用户 ID 不转换为 UUID
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_disputes",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "analysis_dispute"
        constraints = [
            # 左右论文必须不同
            models.CheckConstraint(
                condition=~Q(left_paper=models.F("right_paper")),
                name="dispute_sides_differ",
            ),
            models.CheckConstraint(
                condition=Q(classification__in=DISPUTE_CLASSIFICATIONS),
                name="dispute_classification_known",
            ),
            models.CheckConstraint(
                condition=Q(review_status__in=REVIEW_STATUSES),
                name="dispute_review_status_known",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="dispute_version_positive"
            ),
        ]
        indexes = [
            models.Index(fields=["run", "created_at", "id"], name="dispute_run_created_idx"),
            models.Index(fields=["run", "review_status"], name="dispute_run_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Dispute({self.classification}, {self.review_status})"
