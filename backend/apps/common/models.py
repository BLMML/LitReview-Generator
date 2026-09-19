"""common.models：FileAsset 与幂等请求持久记录。

职责（docs/mvp/01 第 3.3 节、docs/mvp/02 第 2.3 / 5.2 节）：
- 文件只保存内部存储键、哈希、大小和所属关系，API 不返回磁盘绝对路径；
- 幂等记录保留原始受理响应与请求摘要。

本模块不调用任何供应商，也不导入领域服务。
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone


class UUIDModel(models.Model):
    """业务主键：UUID 字符串，由服务端生成。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class CreatedAtModel(UUIDModel):
    """不可变记录：只有 created_at。"""

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(CreatedAtModel):
    """可变记录：created_at + updated_at。"""

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class FileAsset(TimeStampedModel):
    """受权限控制的文件记录。

    只登记已完整关闭且可读取的文件；工作临时文件不注册为可下载的 FileAsset。
    storage_key 由服务端生成，解析后必须仍在受控根目录下。
    不存在“pending”状态：写入完成并通过 sha256 校验后才登记。
    """

    PURPOSE_ORIGINAL_PDF = "original_pdf"
    PURPOSE_PAGE_IMAGE = "page_image"
    PURPOSE_RAW_PAGES = "raw_pages"
    PURPOSE_STRUCTURED_PARSE = "structured_parse"
    PURPOSE_INDEX = "index"
    PURPOSE_MODEL_RAW_OUTPUT = "model_raw_output"
    PURPOSE_EXPORT_DOCX = "export_docx"

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="file_assets",
    )
    # 服务端生成的相对存储键；全局唯一；不允许客户端指定
    storage_key = models.CharField(max_length=512, unique=True)
    # 内部用途标识，不是外部任务状态
    purpose = models.CharField(max_length=64)
    media_type = models.CharField(max_length=128)
    # 仅用于显示：已去掉路径片段与控制字符
    original_filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField()
    source_job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="produced_assets",
    )

    class Meta:
        db_table = "common_file_asset"
        constraints = [
            models.CheckConstraint(
                condition=Q(size_bytes__gte=0), name="file_asset_size_bytes_non_negative"
            ),
            models.CheckConstraint(
                condition=~Q(storage_key__startswith="/"),
                name="file_asset_storage_key_relative",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "purpose"], name="file_asset_project_purpose_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"FileAsset({self.purpose}, {self.original_filename})"


class IdempotencyRecord(CreatedAtModel):
    """幂等请求的持久记录。

    与业务资源、Job 在同一数据库事务提交；不存在“先记成功、后建资源”的中间态。
    权限校验后、PROJECT_BUSY 判断之前查同键。
    """

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="idempotency_records",
    )
    method = models.CharField(max_length=10)
    canonical_path = models.TextField()
    key = models.UUIDField()
    # 规范化 JSON 摘要；上传另含实际文件 SHA256 与元数据
    request_hash = models.CharField(max_length=64)
    response_status = models.SmallIntegerField()
    response_body = models.JSONField(default=dict)
    # 只存必要且安全的响应头，不存 Cookie 或密钥
    response_headers = models.JSONField(default=dict)
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="idempotency_records",
    )
    job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="idempotency_records",
    )
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "common_idempotency_record"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "method", "canonical_path", "key"],
                name="idempotency_unique_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="idempotency_expires_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"IdempotencyRecord({self.method} {self.canonical_path})"
