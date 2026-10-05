"""retrieval.artifact：Run 专属索引产物的编码、解码与完整性校验。

职责（docs/mvp/01 第 4.5 节、docs/mvp/04 第 3 节）：
- 索引 manifest 含 run_id、corpus_revision、解析产物 ID 与哈希、词表配置、依赖版本；
- 索引文件只能由应用生成读取，**不能接收用户上传的 pickle / joblib 对象**。

实现选择：产物是**纯 JSON**，只保存词表、IDF 与每片的稀疏词频。加载后由本进程
重建稀疏矩阵。因此：
- 不引入任何反序列化执行代码的面（对比 pickle / joblib）；
- 加载时能逐字段校验版本、哈希与词表自洽性，损坏即显式失败；
- 矩阵是**精确重建**的，不是近似——TF-IDF 的 idf 与词表都在产物里。

代价是加载时要重算稀疏结构。MVP 语料（30 篇）下可忽略；语料显著变大时应重新评估
是否改用更紧凑的二进制格式（届时仍须保留完整性校验）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.common.http import api_error

# 产物内部结构版本；字段含义变化时必须提升
ARTIFACT_FORMAT = "index-json-v1"


@dataclass
class LoadedIndex:
    """从 JSON 产物重建出的索引。"""

    chunk_ids: list[str]
    vocabulary: dict[str, int]
    idf: list[float]
    rows: list[dict[int, float]]  # 每片的稀疏 {列索引: tf-idf 权重}
    norms: list[float]
    meta: dict[str, dict[str, Any]]
    manifest: dict[str, Any] = field(default_factory=dict)


def encode_index(
    *,
    chunk_ids: Sequence[str],
    vocabulary: Mapping[str, int],
    idf: Sequence[float],
    weights: Sequence[Mapping[int, float]],
    meta: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> bytes:
    """把索引编码为 JSON 字节串。

    `weights` 是每片的稀疏 {列: 权重}；不保存零元素，产物大小与命中词数成正比。
    """
    payload = {
        "format": ARTIFACT_FORMAT,
        "manifest": dict(manifest),
        "vocabulary": {str(term): int(column) for term, column in vocabulary.items()},
        "idf": [round(float(value), 8) for value in idf],
        "chunks": [
            {
                "evidence_id": str(chunk_id),
                "terms": {str(column): round(float(weight), 8) for column, weight in row.items()},
                "meta": dict(meta.get(str(chunk_id), {})),
            }
            for chunk_id, row in zip(chunk_ids, weights, strict=True)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_index(raw: bytes, *, expected_manifest: Mapping[str, Any] | None = None) -> LoadedIndex:
    """解码并校验索引产物；任何不一致都显式失败，绝不"尽力而为"地继续。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise api_error("PDF_PARSE_FAILED", "索引产物无法解析") from exc
    if not isinstance(payload, Mapping) or payload.get("format") != ARTIFACT_FORMAT:
        found = payload.get("format") if isinstance(payload, Mapping) else None
        raise api_error(
            "PDF_PARSE_FAILED",
            "索引产物格式版本不匹配",
            {"expected": ARTIFACT_FORMAT, "found": found},
        )

    manifest = payload.get("manifest")
    vocabulary = payload.get("vocabulary")
    idf = payload.get("idf")
    chunks = payload.get("chunks")
    if not isinstance(manifest, Mapping) or not isinstance(vocabulary, Mapping):
        raise api_error("PDF_PARSE_FAILED", "索引产物缺少 manifest 或词表")
    if not isinstance(idf, Sequence) or isinstance(idf, (str, bytes)):
        raise api_error("PDF_PARSE_FAILED", "索引产物缺少 IDF 向量")
    if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
        raise api_error("PDF_PARSE_FAILED", "索引产物缺少片段列表")

    for name, expected in (expected_manifest or {}).items():
        if manifest.get(name) != expected:
            # 索引与当前 Run / 解析产物不一致：必须失败，不能拿旧索引给出排序
            raise api_error(
                "PDF_PARSE_FAILED",
                f"索引产物与当前 Run 不一致：{name}",
                {"expected": expected, "found": manifest.get(name)},
            )

    columns = len(idf)
    idf_values: list[float] = []
    for value in idf:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise api_error("PDF_PARSE_FAILED", "索引产物的 IDF 含非法数值")
        idf_values.append(float(value))

    chunk_ids: list[str] = []
    rows: list[dict[int, float]] = []
    norms: list[float] = []
    meta: dict[str, dict[str, Any]] = {}
    for item in chunks:
        if not isinstance(item, Mapping):
            raise api_error("PDF_PARSE_FAILED", "索引产物的片段项结构不合法")
        evidence_id = item.get("evidence_id")
        terms = item.get("terms")
        if not isinstance(evidence_id, str) or not isinstance(terms, Mapping):
            raise api_error("PDF_PARSE_FAILED", "索引产物的片段缺少 evidence_id 或 terms")
        row: dict[int, float] = {}
        total = 0.0
        for column, weight in terms.items():
            try:
                index = int(column)
            except (TypeError, ValueError) as exc:
                raise api_error("PDF_PARSE_FAILED", "索引产物的列索引不合法") from exc
            if not 0 <= index < columns:
                # 词表与向量长度必须自洽，否则余弦相似度会算到别的词上
                raise api_error("PDF_PARSE_FAILED", "索引产物的列索引超出词表范围")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                raise api_error("PDF_PARSE_FAILED", "索引产物的权重不是数值")
            value = float(weight)
            if not math.isfinite(value) or value == 0:
                continue
            row[index] = value
            total += value * value
        chunk_ids.append(evidence_id)
        rows.append(row)
        norms.append(math.sqrt(total))
        item_meta = item.get("meta")
        meta[evidence_id] = dict(item_meta) if isinstance(item_meta, Mapping) else {}

    return LoadedIndex(
        chunk_ids=chunk_ids,
        vocabulary={str(term): int(column) for term, column in vocabulary.items()},
        idf=idf_values,
        rows=rows,
        norms=norms,
        meta=meta,
        manifest=dict(manifest),
    )


def cosine_scores(
    index: LoadedIndex, query_terms: Sequence[str], idf_lookup: Mapping[str, float]
) -> list[float]:
    """查询与每片的余弦相似度。

    查询向量按产物里的 IDF 加权——**必须用产物携带的 IDF**，不能用当前配置重算，
    否则索引与查询的口径不一致，排序会偏。
    """
    query_weights: dict[int, float] = {}
    total = 0.0
    for term in query_terms:
        column = index.vocabulary.get(term)
        if column is None:
            continue
        weight = float(idf_lookup.get(term, 0.0))
        if weight <= 0:
            continue
        query_weights[column] = query_weights.get(column, 0.0) + weight
        total += weight * weight
    if not query_weights or total <= 0:
        return [0.0] * len(index.chunk_ids)

    query_norm = math.sqrt(total)
    scores: list[float] = []
    for row, norm in zip(index.rows, index.norms, strict=True):
        if norm <= 0:
            scores.append(0.0)
            continue
        dot = 0.0
        # 遍历较短的一侧：查询通常远小于片段
        smaller, larger = (query_weights, row) if len(query_weights) <= len(row) else (row, query_weights)
        for column, value in smaller.items():
            other = larger.get(column)
            if other:
                dot += value * other
        scores.append(dot / (query_norm * norm))
    return scores
