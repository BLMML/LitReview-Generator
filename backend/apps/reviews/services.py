"""reviews.services：受理生成 / 核验 / 导出，保存编辑新版本。

职责（docs/mvp/01 第 4.6 节）：
- `create_review(actor, run_id, payload, key) → AcceptedJob`
- `generate_review(context, review_id) → ReviewRevision`
- `save_revision(actor, review_id, base_revision_number, sections) → ReviewRevision`
- `create_validation(...)` / `validate_revision(context, validation_id)`
- `create_export(...)` / `export_revision(context, export_id)`

生成链（docs/mvp/04 第 5.1 节）：
freeze_inputs → retrieve_sections → generate_sections → check_structure → commit_revision。

编辑规则：人工保存锁 Project 与 Review，base_revision_number 必须等于最高版本；
先验证完整正文再插入 N+1；新版本继承 base_revision 的 analysis_revision，
不因改字消除分析过时状态；编辑保存本身不暗中调用模型。

**骨架状态：未实现。**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "reviews.services 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 D）"


def create_review(actor: Any, run_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理生成：新建 Review（无空白 revision 0）+ generate_review Job，返回 202。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def generate_review(context: Any, review_id: Any) -> Any:
    """worker 入口：逐节构造问题与证据包，一次性提交合法五节 revision 1。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def save_revision(
    actor: Any,
    review_id: Any,
    base_revision_number: int,
    sections: Sequence[Any],
) -> Any:
    """保存人工编辑；base_revision_number 不匹配返回 409 VERSION_CONFLICT。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def create_validation(actor: Any, review_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理核验：绑定确切 revision，冻结正文哈希与核验配置。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def validate_revision(context: Any, validation_id: Any) -> Any:
    """worker 入口：结构检查 + 语义支持核验，成功报告覆盖正文所有 block 恰好一次。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def create_export(actor: Any, review_id: Any, payload: Mapping[str, Any], key: Any) -> Any:
    """受理导出：revision 与 validation 必须同版本且 structure_passed=true。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def export_revision(context: Any, export_id: Any) -> Any:
    """worker 入口：生成临时 DOCX → 原子发布文件与 Export.ready。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
