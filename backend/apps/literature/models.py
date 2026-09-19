"""literature.models：Paper。

职责（docs/mvp/01 第 4.2 节、docs/mvp/02 第 2.2 节）：
- Paper、不可变 PDF 关联、来源、元数据、included、version；
- 数据库唯一约束支持重复上传和导入重试。

关键点：
- file_sha256 创建后不可改，用于项目内唯一约束；
- 同哈希重复上传返回已有 Paper，不改其 included 或元数据；
- PDF 不可原位替换：新文件形成新 Paper。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel

SOURCE_UPLOAD = "upload"
SOURCE_ARXIV = "arxiv"
PAPER_SOURCES = (SOURCE_UPLOAD, SOURCE_ARXIV)


class Paper(TimeStampedModel):
    """一份不可变 PDF 对应的文献。"""

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="papers",
    )
    # 此 Paper 不可变的原始 PDF
    file_asset = models.OneToOneField(
        "common.FileAsset",
        on_delete=models.PROTECT,
        related_name="paper_original",
    )
    # 上传缺省使用文件名；模型不得自动覆盖
    title = models.CharField(max_length=500)
    # 作者顺序保留
    authors = models.JSONField(default=list, blank=True)
    year = models.SmallIntegerField(null=True, blank=True)

    source = models.CharField(max_length=16, default=SOURCE_UPLOAD)
    # arxiv 保存已解析的具体版本 ID 和可信来源 URL
    source_id = models.CharField(max_length=64, null=True, blank=True)
    source_url = models.TextField(null=True, blank=True)

    included = models.BooleanField(default=True)
    # metadata 或 included 实际变化时递增
    version = models.PositiveBigIntegerField(default=1)

    # 与 FileAsset.sha256 相同；创建后不可改
    file_sha256 = models.CharField(max_length=64)
    # 上传 / 导入时真实检查；MVP 上限 50 页
    page_count = models.SmallIntegerField()

    class Meta:
        db_table = "literature_paper"
        constraints = [
            # 同项目同内容只保留一条
            models.UniqueConstraint(
                fields=["project", "file_sha256"], name="paper_project_file_sha_unique"
            ),
            # 非空 arxiv source_id 在项目内唯一
            models.UniqueConstraint(
                fields=["project", "source", "source_id"],
                condition=Q(source_id__isnull=False),
                name="paper_project_source_id_unique",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="paper_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(source__in=PAPER_SOURCES), name="paper_source_known"
            ),
            models.CheckConstraint(
                condition=Q(year__isnull=True) | Q(year__gte=1000),
                name="paper_year_plausible",
            ),
            models.CheckConstraint(
                condition=Q(page_count__gte=1)
                & Q(page_count__lte=settings.MVP_LIMITS["PDF_MAX_PAGES"]),
                name="paper_page_count_within_mvp_limit",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "-created_at", "-id"], name="paper_project_created_idx"),
            models.Index(fields=["project", "included"], name="paper_project_included_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Paper({self.title})"
