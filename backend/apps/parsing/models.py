"""parsing.models：ParsedDocument 与 EvidenceChunk。

职责（docs/mvp/01 第 4.3 节、docs/mvp/02 第 3.2 节）：
- 记录解析配置 / 版本、页码和原文件关联；片段稳定绑定解析结果；
- 只有完整成功的产物才登记 / 复用。

注意：
- 文本提取成功不等于语义识别正确；
- image_unverified 表示视觉转录待核对，不能伪称原文已匹配；
- human_verified 是保留枚举，本期没有修改证据认证状态的接口，应用不得自动设置。
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from apps.common.models import CreatedAtModel

ELEMENT_TEXT = "text"
ELEMENT_TABLE = "table"
ELEMENT_FORMULA = "formula"
ELEMENT_FIGURE_CAPTION = "figure_caption"
ELEMENT_TYPES = (ELEMENT_TEXT, ELEMENT_TABLE, ELEMENT_FORMULA, ELEMENT_FIGURE_CAPTION)

VERIFICATION_TEXT_MATCHED = "text_matched"
VERIFICATION_IMAGE_UNVERIFIED = "image_unverified"
VERIFICATION_HUMAN_VERIFIED = "human_verified"
VERIFICATION_VALUES = (
    VERIFICATION_TEXT_MATCHED,
    VERIFICATION_IMAGE_UNVERIFIED,
    VERIFICATION_HUMAN_VERIFIED,
)

LOCATOR_GRANULARITY_PAGE = "page"
LOCATOR_GRANULARITY_REGION = "region"


class ParsedDocument(CreatedAtModel):
    """一份完整、可复用的解析产物。

    唯一 `(project_id, source_sha256, parser_revision, config_hash)`：
    相同来源文件 + 相同解析配置才允许复用。
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="parsed_documents",
    )
    paper = models.ForeignKey(
        "literature.Paper",
        on_delete=models.PROTECT,
        related_name="parsed_documents",
    )
    source_file = models.ForeignKey(
        "common.FileAsset",
        on_delete=models.PROTECT,
        related_name="parsed_documents",
    )

    # 三者共同确定解析缓存版本
    parser_revision = models.CharField(max_length=64)
    config_hash = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)

    page_count = models.SmallIntegerField()
    content_hash = models.CharField(max_length=64)

    # 原始按页文本与结构化完整产物；图像页在结构化产物内关联页图资产
    raw_pages_asset = models.ForeignKey(
        "common.FileAsset",
        on_delete=models.PROTECT,
        related_name="parsed_raw_pages",
    )
    structured_asset = models.ForeignKey(
        "common.FileAsset",
        on_delete=models.PROTECT,
        related_name="parsed_structured",
    )

    class Meta:
        db_table = "parsing_parsed_document"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "source_sha256", "parser_revision", "config_hash"],
                name="parsed_document_cache_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(page_count__gte=1), name="parsed_document_page_count_positive"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"ParsedDocument(paper={self.paper_id}, pages={self.page_count})"


class EvidenceChunk(CreatedAtModel):
    """可追溯的证据片段；ID 稳定绑定某版解析产物，不能移到其他解析产物。"""

    parsed_document = models.ForeignKey(
        ParsedDocument,
        on_delete=models.PROTECT,
        related_name="chunks",
    )
    # ordinal 从 1 起；page_number 为真实 1 起始页码
    ordinal = models.IntegerField()
    page_number = models.SmallIntegerField()

    element_type = models.CharField(max_length=32)
    # 未知章节为 null
    section = models.TextField(null=True, blank=True)

    # text 是提供给模型 / 检索的文本；原始抽取文本独立保留
    text = models.TextField()
    original_text = models.TextField(null=True, blank=True)

    # {granularity:"page"|"region", bbox:null|[x0,y0,x1,y1]}，坐标为左上原点、0—1 归一化
    locator = models.JSONField(default=dict)
    verification = models.CharField(max_length=32)

    # 图像页绑定实际渲染资产；归一化规则可追溯
    source_image = models.ForeignKey(
        "common.FileAsset",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="evidence_chunks",
    )
    normalization_revision = models.CharField(max_length=64)
    content_hash = models.CharField(max_length=64)

    class Meta:
        db_table = "parsing_evidence_chunk"
        constraints = [
            models.UniqueConstraint(
                fields=["parsed_document", "ordinal"], name="evidence_chunk_ordinal_unique"
            ),
            models.CheckConstraint(
                condition=Q(ordinal__gte=1), name="evidence_chunk_ordinal_positive"
            ),
            models.CheckConstraint(
                condition=Q(page_number__gte=1), name="evidence_chunk_page_number_positive"
            ),
            models.CheckConstraint(
                condition=Q(element_type__in=ELEMENT_TYPES), name="evidence_chunk_element_type_known"
            ),
            models.CheckConstraint(
                condition=Q(verification__in=VERIFICATION_VALUES),
                name="evidence_chunk_verification_known",
            ),
        ]
        indexes = [
            models.Index(
                fields=["parsed_document", "page_number"], name="evidence_chunk_page_idx"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"EvidenceChunk(p{self.page_number}#{self.ordinal})"
