"""retrieval.index：TF-IDF 索引构建与召回。

职责（docs/mvp/01 第 4.5 节）：
- `build_index(run_snapshot) → IndexReference`
- `recall(index_ref, queries, limits) → CandidateEvidence[]`

约束：
- 内部索引由服务生成，**不能接收用户上传的 pickle / joblib 对象**；
- 缓存失效键至少含 Run 和索引配置版本；
- 不创建向量字段，不引入 pgvector。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "retrieval.index 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"


@dataclass(frozen=True)
class IndexReference:
    """索引引用；manifest 须含 run_id、corpus_revision、解析产物 ID 与哈希、词表配置、依赖版本。"""

    run_id: Any
    index_asset_id: Any
    manifest: dict[str, Any] = field(default_factory=dict)
    cache_key: str = ""


@dataclass(frozen=True)
class CandidateEvidence:
    evidence_id: Any
    recall_score: float
    matched_queries: list[str] = field(default_factory=list)


def build_index(run_snapshot: Any) -> IndexReference:
    """为明确的 Run / 解析版本建立 TF-IDF 索引并原子发布。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def recall(
    index_ref: IndexReference,
    queries: Sequence[str],
    limits: Any,
) -> list[CandidateEvidence]:
    """关键词 / 词频召回并按稳定规则融合排序。

    索引缓存必须带源文件哈希和配置版本；恢复时校验存在性与完整性，
    不能仅凭文件名复用。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
