"""projects.models：Project。

职责（docs/mvp/01 第 4.1 节、docs/mvp/02 第 2.1 节）：
- Project、version、corpus_revision 和配置；所有者唯一归属；
- 不增加成员 / 共享模型；
- latest_run_id、active_job_id 是计算字段，不建列、不维护重复指针。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel


class Project(TimeStampedModel):
    """research_question / 纳入集合 / 文献元数据变化都会影响 corpus_revision；
    title 之类的表单编辑只增加 version。"""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="projects",
    )
    title = models.CharField(max_length=120)
    research_question = models.TextField()
    # MVP 仅 zh-CN（docs/mvp/00 第 1 节）
    output_language = models.CharField(max_length=16, default="zh-CN")
    # 可编辑项目字段的乐观锁，从 1 起
    version = models.PositiveBigIntegerField(default=1)
    # 研究问题、纳入集合或文献元数据变化时递增；同文件重复上传不递增
    corpus_revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "projects_project"
        indexes = [
            models.Index(fields=["owner", "-created_at", "-id"], name="project_owner_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(version__gte=1), name="project_version_positive"),
            models.CheckConstraint(
                condition=Q(corpus_revision__gte=1), name="project_corpus_revision_positive"
            ),
            models.CheckConstraint(
                condition=Q(output_language="zh-CN"), name="project_output_language_zh_cn"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Project({self.title})"
