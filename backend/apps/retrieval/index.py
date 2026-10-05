"""retrieval.index：TF-IDF 索引的构建、发布与召回。

职责（docs/mvp/01 第 4.5 节、docs/mvp/04 第 3 节）：
- `build_index(run_snapshot) → IndexReference`
- `encode_reference(index_ref) → bytes`（index 阶段发布用）
- `recall(index_ref, queries, limits) → CandidateEvidence[]`

约束：
- 内部索引由服务生成，**不能接收用户上传的 pickle / joblib 对象**；
- 缓存失效键至少含 Run 和索引配置版本；
- 不创建向量字段，不引入 pgvector。

实现选择：索引产物是**纯 JSON**（见 `artifact.py`），只保存词表、IDF 与稀疏权重。
既满足"Run 专属索引文件 + manifest"的契约，也不引入反序列化执行代码的面。
检索时优先加载已发布产物并**按源摘要逐项校验**；产物缺失或不一致时在内存中重建
（首次检索、旧 Run，或解析产物已变更），绝不拿对不上的索引去排序。

本模块不依赖 scikit-learn：TF-IDF 与余弦都由这里实现，因此索引的**每一个数值**
都能被完整地写进产物并逐字段校验，不存在"矩阵与向量器两套状态"的漂移。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.common.http import ApiError, api_error
from apps.parsing.models import EvidenceChunk
from apps.retrieval.artifact import (
    ARTIFACT_FORMAT,
    LoadedIndex,
    cosine_scores,
    decode_index,
    encode_index,
)

logger = logging.getLogger("apps.retrieval.index")

# 索引配置版本：分词 / 权重 / 融合规则改变时必须提升，否则缓存会给出旧排序
INDEX_CONFIG_VERSION = "tfidf-v1"

# 中英文混合的最小 token 长度：1 会让标点与单字母噪声进入词表
MIN_TOKEN_LENGTH = 1


@dataclass(frozen=True)
class IndexReference:
    """索引引用；manifest 含 run_id、corpus_revision、解析产物 ID 与哈希、词表配置、依赖版本。"""

    run_id: Any
    index_asset_id: Any = None
    manifest: dict[str, Any] = field(default_factory=dict)
    cache_key: str = ""
    # 仅供本进程使用的实际索引对象；**不对外暴露，也不序列化**
    engine: Any = None


@dataclass(frozen=True)
class CandidateEvidence:
    evidence_id: Any
    recall_score: float
    matched_queries: list[str] = field(default_factory=list)


@dataclass
class _Engine:
    """内存索引：词表 + IDF + 每片稀疏权重。

    **IDF 属于索引本身**：查询向量必须用同一套 IDF 加权，否则口径不一致、排序会偏。
    因此这里保存 idf，而不是在查询时按"当前"语料重算。
    """

    chunk_ids: list[str]
    vocabulary: dict[str, int]
    idf: list[float]
    rows: list[dict[int, float]]
    norms: list[float]
    token_sets: list[set[str]]
    meta: dict[str, dict[str, Any]]


_cache: dict[str, IndexReference] = {}
_cache_lock = threading.Lock()


def clear_index_cache() -> None:
    """清空进程内索引缓存（测试与配置变更后使用）。"""
    with _cache_lock:
        _cache.clear()


def index_config() -> dict[str, Any]:
    """当前部署的索引配置（仅在**没有冻结配置**时可作为来源）。"""
    return {
        "version": INDEX_CONFIG_VERSION,
        "artifact_format": ARTIFACT_FORMAT,
        "max_features": int(settings.RETRIEVAL_INDEX_MAX_FEATURES),
        "min_df": int(settings.RETRIEVAL_INDEX_MIN_DF),
        "ngram_max": int(settings.RETRIEVAL_INDEX_NGRAM_MAX),
    }


def effective_index_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把部分覆盖补全为完整索引配置；非法范围明确拒绝。

    `overrides` 是 Run 冻结快照里的 `index` 段。只采纳已知键，其余取当前默认值——
    这样"冻结了 max_features 但没冻结 min_df"的历史快照仍然可用。
    **快照里多出来的键（如 artifact_version）直接忽略**：那是快照自己的记账字段，
    不是构建参数，为它报错会拒绝合法快照。
    """
    base = index_config()
    supplied = {key: value for key, value in (overrides or {}).items() if key in base}
    config = {**base, **supplied}
    for name in ("max_features", "min_df", "ngram_max"):
        value = config[name]
        if type(value) is not int or value < 1:
            raise api_error("VALIDATION_ERROR", f"索引配置 {name} 必须是正整数")
    if config["ngram_max"] > 4:
        raise api_error("VALIDATION_ERROR", "索引配置 ngram_max 不得超过 4")
    return config


def config_fingerprint(config: Mapping[str, Any]) -> str:
    """完整有效配置的指纹，参与缓存身份。"""
    material = json.dumps(dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _tokenize(text: str) -> list[str]:
    """轻量分词：英文按词、中文按字，兼顾两种语料且不引入额外依赖。

    scikit-learn 的默认 token_pattern 会整段丢弃中文，因此这里显式切分：
    连续的 ASCII 字母数字视为一个词；每个 CJK 字符单独成为一个 token
    （中文没有空格，按字切分对 TF-IDF 召回的召回率最稳）。
    """
    import re

    tokens: list[str] = []
    for piece in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text or ""):
        if len(piece) >= MIN_TOKEN_LENGTH:
            tokens.append(piece.lower())
    return tokens


def _ngrams(tokens: Sequence[str], maximum: int) -> list[str]:
    """按 ngram_max 生成 1..n 元组；用 \\x1f 连接以避免与自然文本歧义。"""
    if maximum <= 1:
        return list(tokens)
    result: list[str] = list(tokens)
    for size in range(2, maximum + 1):
        for start in range(len(tokens) - size + 1):
            result.append("\x1f".join(tokens[start : start + size]))
    return result


def _chunk_rows(document_ids: Sequence[Any]) -> list[dict[str, Any]]:
    return list(
        EvidenceChunk.objects.filter(parsed_document_id__in=list(document_ids))
        .order_by("page_number", "ordinal", "id")
        .values(
            "id",
            "page_number",
            "ordinal",
            "element_type",
            "section",
            "text",
            "parsed_document_id",
        )
    )


def _source_digest(rows: Sequence[Any]) -> str:
    """解析产物集合的摘要：片段 ID + 页号 + 顺序 + 文本。

    只比"索引存在与否"或文件名是不够的——解析产物可能已重建，
    那时旧索引的排序会指向已不存在的内容。
    """
    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row["id"]).encode())
        digest.update(b"\x1f")
        digest.update(str(row["page_number"]).encode())
        digest.update(b"\x1f")
        digest.update(str(row["ordinal"]).encode())
        digest.update(b"\x1f")
        digest.update(str(row["text"] or "").encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def build_index(run_snapshot: Any, *, config: Mapping[str, Any] | None = None) -> IndexReference:
    """为明确的 Run / 解析版本建立 TF-IDF 索引（内存态，不含产物登记）。

    `config` 是**冻结的**索引配置（Run 快照里的 `index` 段）。不传时读取当前默认值——
    只有独立调用入口（测试、工具）才应这样用。
    缓存键包含**完整有效配置的指纹**：只含版本号时，改 `max_features` 之类参数
    会命中旧缓存，返回与当前配置不符的索引。
    """
    run_id = getattr(run_snapshot, "run_id", None) or getattr(run_snapshot, "id", None)
    if run_id is None:
        raise api_error("VALIDATION_ERROR", "索引快照缺少 run_id")

    revision = int(getattr(run_snapshot, "analysis_revision", 1) or 1)
    document_ids = _document_ids(run_snapshot)
    resolved = effective_index_config(dict(config) if config else None)
    rows = _chunk_rows(document_ids)
    source_digest = _source_digest(rows)
    cache_key = (
        f"{run_id}:{revision}:{resolved['version']}:{config_fingerprint(resolved)}:"
        f"{source_digest[:16]}"
    )

    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    engine = _fit_engine(rows, resolved)
    manifest = {
        "run_id": str(run_id),
        "analysis_revision": revision,
        "corpus_revision": getattr(run_snapshot, "corpus_revision", None),
        "parsed_document_ids": sorted(str(value) for value in document_ids),
        "source_digest": source_digest,
        "chunk_count": len(engine.chunk_ids),
        "index_config": resolved,
    }
    reference = IndexReference(
        run_id=run_id,
        manifest=manifest,
        cache_key=cache_key,
        engine=engine,
    )
    _cache_put(cache_key, reference)
    return reference


def encode_reference(reference: IndexReference) -> bytes:
    """把索引编码为可发布的 JSON 产物。"""
    engine = reference.engine
    if engine is None:  # pragma: no cover - 调用方保证已构建
        raise api_error("INTERNAL_ERROR", "索引尚未构建")
    return encode_index(
        chunk_ids=engine.chunk_ids,
        vocabulary=engine.vocabulary,
        idf=engine.idf,
        weights=engine.rows,
        meta=engine.meta,
        manifest=reference.manifest,
    )


def resolve_index(run: Any, snapshot: Any, *, config: Mapping[str, Any] | None = None) -> IndexReference:
    """检索入口：优先用已发布产物，否则内存重建。"""
    published = load_published(run, snapshot, config=config)
    if published is not None:
        return published
    return build_index(snapshot, config=config)


def load_published(
    run: Any, snapshot: Any, *, config: Mapping[str, Any] | None = None
) -> IndexReference | None:
    """加载 Run 已发布的索引产物；缺失、损坏或与当前源不一致时返回 None。

    返回 None 表示"应当重建"，不是错误——首次检索时本来就没有产物。
    但**一致性不符不会被静默忽略**：会记 warning 后重建，绝不拿旧索引排序。

    `config` 为冻结索引配置；同时校验产物里的 `index_config` 与它一致，
    避免"用今天的配置去读昨天参数的索引"。
    """
    if getattr(run, "index_asset_id", None) is None:
        return None

    resolved = effective_index_config(dict(config) if config else None)
    rows = _chunk_rows(_document_ids(snapshot))
    expected = {
        "run_id": str(run.id),
        "analysis_revision": int(run.analysis_revision),
        "source_digest": _source_digest(rows),
        "index_config": resolved,
    }

    from apps.common.storage import resolve_storage_key

    asset = run.index_asset
    try:
        with resolve_storage_key(asset.storage_key).open("rb") as handle:
            raw = handle.read(int(settings.INDEX_ARTIFACT_MAX_BYTES) + 1)
    except OSError:
        logger.warning("索引产物文件缺失，改为内存重建 run=%s", run.id)
        return None
    if len(raw) != int(asset.size_bytes) or hashlib.sha256(raw).hexdigest() != asset.sha256:
        logger.warning("索引产物完整性校验失败，改为内存重建 run=%s", run.id)
        return None

    try:
        loaded = decode_index(raw, expected_manifest=expected)
    except ApiError as exc:
        logger.warning("索引产物与当前 Run 不一致（%s），改为内存重建 run=%s", exc.code, run.id)
        return None

    engine = _engine_from_loaded(loaded)
    # 缓存键必须与 `build_index` 完全同构，否则同一份索引会有两个缓存条目
    cache_key = (
        f"{run.id}:{run.analysis_revision}:{resolved['version']}:"
        f"{config_fingerprint(resolved)}:{expected['source_digest'][:16]}"
    )
    reference = IndexReference(
        run_id=run.id,
        index_asset_id=asset.pk,
        manifest=dict(loaded.manifest),
        cache_key=cache_key,
        engine=engine,
    )
    _cache_put(cache_key, reference)
    return reference


def recall(
    index_ref: IndexReference,
    queries: Sequence[str],
    limits: Any,
) -> list[CandidateEvidence]:
    """关键词 / 词频召回并按稳定规则融合排序。

    融合规则：取各查询上 TF-IDF 余弦的最大值作为主分，关键词覆盖率作为次分。
    排序键为 `(score 降序, matched_queries 数降序, evidence_id 升序)`——
    同一输入必须给出同一顺序。
    """
    engine = index_ref.engine
    limit = _candidate_limit(limits)
    if engine is None or not engine.chunk_ids or not queries:
        return []

    idf_lookup = {term: engine.idf[column] for term, column in engine.vocabulary.items()}
    loaded = LoadedIndex(
        chunk_ids=engine.chunk_ids,
        vocabulary=engine.vocabulary,
        idf=engine.idf,
        rows=engine.rows,
        norms=engine.norms,
        meta=engine.meta,
    )
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    # 查询向量必须用**索引自己的** ngrm 参数：读当前 settings 会让同一份固定索引
    # 在不同部署下给出不同召回分数（实测 ngram_max 2→1 时分数从 1.023 掉到 0.927），
    # 而索引和输入都没变。
    maximum = int((index_ref.manifest.get("index_config") or {}).get("ngram_max") or 1)

    for query in queries:
        tokens = _ngrams(_tokenize(query), maximum)
        if not tokens:
            continue
        similarities = cosine_scores(loaded, tokens, idf_lookup)
        for position, value in enumerate(similarities):
            if value <= 0:
                continue
            chunk_id = engine.chunk_ids[position]
            scores[chunk_id] = max(scores.get(chunk_id, 0.0), float(value))
            matched.setdefault(chunk_id, []).append(query)

    # 关键词重叠：TF-IDF 对短查询或词表外术语会给 0 分，这里补一条召回路径
    for query in queries:
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            continue
        for position, chunk_id in enumerate(engine.chunk_ids):
            chunk_tokens = engine.token_sets[position]
            if not chunk_tokens:
                continue
            overlap = len(query_tokens & chunk_tokens)
            if not overlap:
                continue
            coverage = overlap / len(query_tokens)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + coverage * 0.5
            matched.setdefault(chunk_id, []).append(query)

    candidates = [
        CandidateEvidence(
            evidence_id=chunk_id,
            recall_score=round(score, 6),
            matched_queries=sorted(set(matched.get(chunk_id, []))),
        )
        for chunk_id, score in scores.items()
    ]
    candidates.sort(
        key=lambda item: (
            -item.recall_score,
            -len(item.matched_queries),
            str(item.evidence_id),
        )
    )
    return candidates[:limit]


def candidate_metadata(
    index_ref: IndexReference, evidence_ids: Sequence[Any]
) -> list[dict[str, Any]]:
    """按给定顺序返回候选的展示元数据（不含正文以外的新信息）。"""
    engine = index_ref.engine
    if engine is None:
        return []
    result = []
    for evidence_id in evidence_ids:
        item = engine.meta.get(str(evidence_id))
        if item is not None:
            result.append(dict(item))
    return result


# ── 内部工具 ──────────────────────────────────────────────────
def _document_ids(run_snapshot: Any) -> list[Any]:
    documents = getattr(run_snapshot, "parsed_document_ids", None)
    if documents is None:
        raise api_error("VALIDATION_ERROR", "索引快照缺少已解析产物列表")
    return list(documents)


def _fit_engine(rows: Sequence[Any], config: dict[str, Any]) -> _Engine:
    """在已发布的证据片段上拟合 TF-IDF。"""
    chunk_ids: list[str] = []
    token_sets: list[set[str]] = []
    term_counts: list[dict[str, int]] = []
    document_frequency: dict[str, int] = {}
    maximum = int(config["ngram_max"])

    for row in rows:
        text = str(row["text"] or "")
        counts: dict[str, int] = {}
        for token in _ngrams(_tokenize(text), maximum):
            counts[token] = counts.get(token, 0) + 1
        chunk_ids.append(str(row["id"]))
        token_sets.append(set(_tokenize(text)))
        term_counts.append(counts)
        for term in counts:
            document_frequency[term] = document_frequency.get(term, 0) + 1

    total_documents = max(1, len(chunk_ids))
    minimum_df = int(config["min_df"])
    # 词表按列固定：产物必须可复现，不能依赖 dict 的插入顺序
    terms = sorted(
        term
        for term, frequency in document_frequency.items()
        if total_documents < minimum_df or frequency >= minimum_df
    )
    maximum_features = int(config["max_features"])
    if len(terms) > maximum_features:
        # 超限时按文档频率降序保留，再按词序回排，保证同一输入同一词表
        terms = sorted(
            sorted(terms, key=lambda term: (-document_frequency[term], term))[:maximum_features]
        )
    vocabulary = {term: column for column, term in enumerate(terms)}

    idf: list[float] = []
    for term in terms:
        frequency = document_frequency[term]
        # 平滑 IDF（与 sklearn 的 smooth_idf 同形），避免除零
        idf.append(math.log((1 + total_documents) / (1 + frequency)) + 1.0)

    rows_weights: list[dict[int, float]] = []
    norms: list[float] = []
    for counts in term_counts:
        row: dict[int, float] = {}
        total = 0.0
        for term, count in counts.items():
            column = vocabulary.get(term)
            if column is None:
                continue
            # sublinear tf：1 + log(count)，抑制高频词
            weight = (1.0 + math.log(count)) * idf[column]
            row[column] = weight
            total += weight * weight
        rows_weights.append(row)
        norms.append(math.sqrt(total))

    meta = {
        str(row["id"]): {
            "evidence_id": str(row["id"]),
            "paper_id": None,  # 由调用方按 parsed_document 归属回填
            "parsed_document_id": str(row["parsed_document_id"]),
            "page_number": int(row["page_number"]),
            "element_type": row["element_type"],
            "section": row["section"],
            "text": str(row["text"] or ""),
        }
        for row in rows
    }
    return _Engine(
        chunk_ids=chunk_ids,
        vocabulary=vocabulary,
        idf=idf,
        rows=rows_weights,
        norms=norms,
        token_sets=token_sets,
        meta=meta,
    )


def _engine_from_loaded(loaded: LoadedIndex) -> _Engine:
    token_sets = [
        set(_tokenize(str(loaded.meta.get(chunk_id, {}).get("text", ""))))
        for chunk_id in loaded.chunk_ids
    ]
    return _Engine(
        chunk_ids=list(loaded.chunk_ids),
        vocabulary=dict(loaded.vocabulary),
        idf=list(loaded.idf),
        rows=[dict(row) for row in loaded.rows],
        norms=list(loaded.norms),
        token_sets=token_sets,
        meta={key: dict(value) for key, value in loaded.meta.items()},
    )


def _cache_get(cache_key: str) -> IndexReference | None:
    with _cache_lock:
        return _cache.get(cache_key)


def _cache_put(cache_key: str, reference: IndexReference) -> None:
    with _cache_lock:
        _cache[cache_key] = reference


def _candidate_limit(limits: Any) -> int:
    if isinstance(limits, int):
        return limits
    value = getattr(limits, "candidate_limit", None)
    if isinstance(value, int):
        return value
    if isinstance(limits, dict) and isinstance(limits.get("candidate_limit"), int):
        return int(limits["candidate_limit"])
    return int(settings.MVP_LIMITS["RETRIEVAL_CANDIDATE_LIMIT_DEFAULT"])


def manifest_json(reference: IndexReference) -> str:
    """manifest 的稳定序列化，供 `AnalysisRun.index_manifest` 快速比对。"""
    return json.dumps(reference.manifest, ensure_ascii=False, sort_keys=True)
