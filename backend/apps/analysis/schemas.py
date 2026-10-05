"""analysis.schemas：模型输出的内部结构校验。

职责（docs/mvp/01 第 4.4 节）：
- `validate_paper_analysis`、`validate_topics`、`validate_disputes`；
- 校验未知字段、证据范围与缺失值；输出验证后的结构。

EvidenceText 统一为 `{value: string|null, evidence_ids: UUID[]}`：
缺失时 value=null、evidence_ids=[]；有事实 value 时至少一个合法证据。

本模块只做**结构与归属**校验，不判断学术正确性：结构通过只表示格式与引用合法，
事实支持仍需证据核验与人工评审。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from apps.analysis.models import DISPUTE_CLASSIFICATIONS

# 单篇分析 payload 的固定字段
EVIDENCE_TEXT_SINGLE_FIELDS = ("research_question", "research_object", "summary")
EVIDENCE_TEXT_LIST_FIELDS = ("methods", "datasets", "metrics", "conclusions", "limitations")
PAPER_ANALYSIS_FIELDS = EVIDENCE_TEXT_SINGLE_FIELDS + EVIDENCE_TEXT_LIST_FIELDS

DISPUTE_CONDITION_FIELDS = (
    "research_question",
    "left_conditions",
    "right_conditions",
    "comparability_reason",
)

NOT_IMPLEMENTED_MESSAGE = "analysis.schemas 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 C）"

# value 为 None 时不得带证据；有事实内容时至少一个证据
EVIDENCE_TEXT_KEYS = {"value", "evidence_ids"}


class SchemaValidationError(ValueError):
    """结构不合法；由调用方映射为 Job.error 的 MODEL_OUTPUT_INVALID。"""


def allowed_evidence_ids_for(run_paper: Any) -> set[str]:
    """该 RunPaper 允许引用的证据 ID 集合。

    只包含**本篇**已发布解析产物的片段：跨篇、跨 Run 的 ID 一律不在集合内，
    因此调用方无需再自行过滤。未解析成功时返回空集合。
    """
    from apps.parsing.models import EvidenceChunk

    if run_paper is None or run_paper.parsed_document_id is None:
        return set()
    return {
        str(value)
        for value in EvidenceChunk.objects.filter(
            parsed_document_id=run_paper.parsed_document_id
        ).values_list("id", flat=True)
    }


def validate_paper_analysis(payload: Mapping[str, Any], *, run_scope: Any) -> dict[str, Any]:
    """校验单篇要素与摘要：未知字段、缺失值形态、证据属于同一 RunPaper。

    `run_scope` 是本次分析对应的 `RunPaper`。单篇分析只能引用**本篇**片段；
    其他论文或别的 Run 的 ID 一律拒绝（`04` 第 4 节第 4、5 条）。
    """
    if run_scope is None:
        raise SchemaValidationError("缺少 RunPaper 作用域")
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("单篇分析输出必须是 JSON 对象")

    unknown = sorted(set(payload) - set(PAPER_ANALYSIS_FIELDS))
    if unknown:
        raise SchemaValidationError(f"单篇分析包含未知字段：{unknown}")
    missing = [name for name in PAPER_ANALYSIS_FIELDS if name not in payload]
    if missing:
        raise SchemaValidationError(f"单篇分析缺少必填字段：{missing}")

    allowed = allowed_evidence_ids_for(run_scope)
    normalized: dict[str, Any] = {}
    for name in EVIDENCE_TEXT_SINGLE_FIELDS:
        normalized[name] = _evidence_text(payload[name], name=name, allowed=allowed)
    for name in EVIDENCE_TEXT_LIST_FIELDS:
        normalized[name] = _evidence_text_list(payload[name], name=name, allowed=allowed)
    return normalized


def _evidence_text(raw: Any, *, name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SchemaValidationError(f"{name} 必须是 {{value, evidence_ids}} 对象")
    if set(raw) != EVIDENCE_TEXT_KEYS:
        raise SchemaValidationError(f"{name} 只允许 value 与 evidence_ids 两个字段")

    value = raw["value"]
    if value is not None and not isinstance(value, str):
        raise SchemaValidationError(f"{name}.value 必须是字符串或 null")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            # "" / "未知" 之类的占位文本表示"缺失"，必须写成 null
            raise SchemaValidationError(f"{name}.value 不得用空字符串代替 null")

    evidence_ids = _evidence_ids(raw["evidence_ids"], name=name, allowed=allowed)
    if value is None and evidence_ids:
        raise SchemaValidationError(f"{name} 缺失信息时不得附带 evidence_ids")
    if value is not None and not evidence_ids:
        # 有事实性内容必须附本篇证据（`04` 第 4 节第 4 条）
        raise SchemaValidationError(f"{name} 有事实内容时必须提供至少一个本篇 evidence_id")
    return {"value": value, "evidence_ids": evidence_ids}


def _evidence_text_list(raw: Any, *, name: str, allowed: set[str]) -> list[dict[str, Any]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaValidationError(f"{name} 必须是数组")
    return [
        _evidence_text(item, name=f"{name}[{index}]", allowed=allowed)
        for index, item in enumerate(raw)
    ]


def _evidence_ids(raw: Any, *, name: str, allowed: set[str]) -> list[str]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaValidationError(f"{name}.evidence_ids 必须是数组")

    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        try:
            identifier = str(uuid.UUID(str(item)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise SchemaValidationError(f"{name}.evidence_ids 含非法 UUID：{item!r}") from exc
        if identifier in seen:
            # 同一证据重复引用没有意义，去重而不是报错（顺序保持首次出现）
            continue
        if identifier not in allowed:
            # 伪造 ID、跨篇引用与跨 Run 引用在这里被同一道检查拒绝
            raise SchemaValidationError(f"{name} 引用了不属于本篇的证据：{identifier}")
        seen.add(identifier)
        result.append(identifier)
    return result


def validate_topics(payload: Mapping[str, Any], *, run_scope: Any) -> dict[str, Any]:
    """校验主题归纳输出：成员属于本 Run，成员至少 1 篇，允许一文多主题。

    `run_scope` 需要提供 `allowed_paper_ids`（本 Run 纳入的 Paper.id）与
    `evidence_ids_by_paper`（每篇允许引用的证据集合）。返回归一化后的
    `{"topics": [...], "unassigned_paper_ids": [...]}`。

    证据必须来自**该主题成员之内**：跨成员引用等于让主题为不属于它的文献作证。
    """
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("主题输出必须是 JSON 对象")
    unknown = sorted(set(payload) - {"topics", "unassigned_paper_ids"})
    if unknown:
        raise SchemaValidationError(f"主题输出包含未知字段：{unknown}")
    missing = [name for name in ("topics", "unassigned_paper_ids") if name not in payload]
    if missing:
        raise SchemaValidationError(f"主题输出缺少必填字段：{missing}")

    allowed_papers = {str(value) for value in (getattr(run_scope, "allowed_paper_ids", None) or [])}
    evidence_by_paper = getattr(run_scope, "evidence_ids_by_paper", None) or {}

    raw_topics = payload["topics"]
    if isinstance(raw_topics, (str, bytes)) or not isinstance(raw_topics, Sequence):
        raise SchemaValidationError("topics 必须是数组")
    if len(raw_topics) > int(getattr(run_scope, "max_topics", 20)):
        raise SchemaValidationError("主题数量超过上限")

    topics: list[dict[str, Any]] = []
    for index, item in enumerate(raw_topics):
        name = f"topics[{index}]"
        if not isinstance(item, Mapping):
            raise SchemaValidationError(f"{name} 必须是对象")
        if set(item) != {"name", "description", "paper_ids", "evidence_ids"}:
            raise SchemaValidationError(f"{name} 只允许 name / description / paper_ids / evidence_ids")

        title = item["name"]
        if not isinstance(title, str) or not title.strip():
            raise SchemaValidationError(f"{name}.name 必须是非空字符串")
        description = item["description"]
        if not isinstance(description, str):
            # 描述可以为空串，但不能用 null 冒充：那会让前端渲染出 undefined
            raise SchemaValidationError(f"{name}.description 必须是字符串（可为空串）")

        paper_ids = _uuid_list(item["paper_ids"], name=f"{name}.paper_ids")
        if not paper_ids:
            # 成员至少 1 篇：空主题没有意义，只会让下游以为"归纳出了东西"
            raise SchemaValidationError(f"{name} 的 paper_ids 不能为空")
        outsiders = [value for value in paper_ids if value not in allowed_papers]
        if outsiders:
            raise SchemaValidationError(f"{name} 引用了不属于本 Run 的论文：{outsiders[:3]}")

        allowed_evidence: set[str] = set()
        for paper_id in paper_ids:
            allowed_evidence |= {str(item_id) for item_id in evidence_by_paper.get(paper_id, set())}
        evidence_ids = _evidence_ids(item["evidence_ids"], name=name, allowed=allowed_evidence)

        topics.append(
            {
                "name": title.strip(),
                "description": description.strip(),
                "paper_ids": paper_ids,
                "evidence_ids": evidence_ids,
            }
        )

    unassigned = _uuid_list(payload["unassigned_paper_ids"], name="unassigned_paper_ids")
    outsiders = [value for value in unassigned if value not in allowed_papers]
    if outsiders:
        raise SchemaValidationError(f"unassigned_paper_ids 含不属于本 Run 的论文：{outsiders[:3]}")
    assigned = {paper_id for topic in topics for paper_id in topic["paper_ids"]}
    overlap = set(unassigned) & assigned
    if overlap:
        # 同时"已归类"又"未归类"是自相矛盾的，会让年份统计与覆盖检查失准
        raise SchemaValidationError(f"论文不能同时属于主题又未归类：{sorted(overlap)[:3]}")

    # **完整性**：成员并集 ∪ 显式未归类 = 本 Run 纳入的全部论文。
    # 不检查的话，模型漏掉一篇（且 unassigned 为空）会静默通过，
    # Run 仍然 ready——那篇文献在主题结果里凭空消失，后续生成把不完整分析当成成功输入。
    covered = assigned | set(unassigned)
    missing = sorted(allowed_papers - covered)
    if missing:
        raise SchemaValidationError(
            f"主题结果遗漏了 {len(missing)} 篇纳入文献，必须归入某主题或列入 unassigned_paper_ids："
            f"{missing[:3]}"
        )

    return {"topics": topics, "unassigned_paper_ids": unassigned}


def validate_disputes(payload: Mapping[str, Any], *, run_scope: Any) -> dict[str, Any]:
    """校验争议候选：左右论文不同且都在 Run 内，两侧证据分别属于对应论文。

    `run_scope` 需要提供 `allowed_paper_ids` 与 `evidence_ids_by_paper`。
    返回归一化后的 `{"disputes": [...]}`。
    """
    if not isinstance(payload, Mapping):
        raise SchemaValidationError("争议输出必须是 JSON 对象")
    unknown = sorted(set(payload) - {"disputes"})
    if unknown:
        raise SchemaValidationError(f"争议输出包含未知字段：{unknown}")
    if "disputes" not in payload:
        raise SchemaValidationError("争议输出缺少必填字段：disputes")

    allowed_papers = {str(value) for value in (getattr(run_scope, "allowed_paper_ids", None) or [])}
    evidence_by_paper = getattr(run_scope, "evidence_ids_by_paper", None) or {}

    raw_disputes = payload["disputes"]
    if isinstance(raw_disputes, (str, bytes)) or not isinstance(raw_disputes, Sequence):
        raise SchemaValidationError("disputes 必须是数组")
    if len(raw_disputes) > int(getattr(run_scope, "max_disputes", 50)):
        raise SchemaValidationError("争议数量超过上限")

    disputes: list[dict[str, Any]] = []
    for index, item in enumerate(raw_disputes):
        name = f"disputes[{index}]"
        if not isinstance(item, Mapping):
            raise SchemaValidationError(f"{name} 必须是对象")
        expected = {
            "left_paper_id",
            "right_paper_id",
            "left_claim",
            "right_claim",
            "conditions",
            "classification",
            "reason",
        }
        if set(item) != expected:
            raise SchemaValidationError(f"{name} 字段集合不合法：{sorted(set(item) ^ expected)}")

        left = _paper_id(item["left_paper_id"], name=f"{name}.left_paper_id", allowed=allowed_papers)
        right = _paper_id(
            item["right_paper_id"], name=f"{name}.right_paper_id", allowed=allowed_papers
        )
        if left == right:
            # 数据库有同名约束；这里先拒绝，避免把必然失败的写入推迟到事务里
            raise SchemaValidationError(f"{name} 的左右论文必须不同")

        left_claim = _evidence_text(
            item["left_claim"],
            name=f"{name}.left_claim",
            allowed={str(value) for value in evidence_by_paper.get(left, set())},
        )
        right_claim = _evidence_text(
            item["right_claim"],
            name=f"{name}.right_claim",
            allowed={str(value) for value in evidence_by_paper.get(right, set())},
        )

        conditions = item["conditions"]
        if not isinstance(conditions, Mapping) or set(conditions) != set(DISPUTE_CONDITION_FIELDS):
            raise SchemaValidationError(
                f"{name}.conditions 必须且只能包含 {list(DISPUTE_CONDITION_FIELDS)}"
            )
        normalized_conditions: dict[str, str] = {}
        for field in DISPUTE_CONDITION_FIELDS:
            value = conditions[field]
            if not isinstance(value, str) or not value.strip():
                # 条件对齐的四个字段是判断"是否可比"的依据，不允许留空
                raise SchemaValidationError(f"{name}.conditions.{field} 必须是非空字符串")
            normalized_conditions[field] = value.strip()

        classification = item["classification"]
        if classification not in DISPUTE_CLASSIFICATIONS:
            raise SchemaValidationError(f"{name}.classification 取值不合法：{classification!r}")
        reason = item["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaValidationError(f"{name}.reason 必须是非空字符串")

        disputes.append(
            {
                "left_paper_id": left,
                "right_paper_id": right,
                "left_claim": left_claim,
                "right_claim": right_claim,
                "conditions": normalized_conditions,
                "classification": classification,
                "reason": reason.strip(),
            }
        )

    return {"disputes": disputes}


def _uuid_list(raw: Any, *, name: str) -> list[str]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaValidationError(f"{name} 必须是数组")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        try:
            identifier = str(uuid.UUID(str(item)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise SchemaValidationError(f"{name} 含非法 UUID：{item!r}") from exc
        if identifier in seen:
            continue
        seen.add(identifier)
        result.append(identifier)
    return result


def _paper_id(raw: Any, *, name: str, allowed: set[str]) -> str:
    try:
        identifier = str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SchemaValidationError(f"{name} 不是合法 UUID：{raw!r}") from exc
    if identifier not in allowed:
        raise SchemaValidationError(f"{name} 不属于本 Run：{identifier}")
    return identifier


__all__ = [
    "DISPUTE_CONDITION_FIELDS",
    "EVIDENCE_TEXT_LIST_FIELDS",
    "EVIDENCE_TEXT_SINGLE_FIELDS",
    "PAPER_ANALYSIS_FIELDS",
    "SchemaValidationError",
    "allowed_evidence_ids_for",
    "validate_disputes",
    "validate_paper_analysis",
    "validate_topics",
]
