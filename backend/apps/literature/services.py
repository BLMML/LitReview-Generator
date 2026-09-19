"""literature.services：上传即时校验与归档、元数据编辑、arXiv 批量导入。

职责（docs/mvp/01 第 4.2 节）：
- `upload_pdf(actor, project_id, uploaded_file, metadata, key) → Paper`
- `update_paper(...) → Paper`
- `import_arxiv(actor, project_id, arxiv_ids, key) → AcceptedJob`
- `execute_import_arxiv(context, job_id) → ItemResults`

**骨架状态：未实现。** 本文件已冻结函数签名与事务边界注释，
实现时按 docs/mvp/01 第 7.2 节的“上传 / 编辑”与“受理后台任务”两行执行。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apps.common.auth import ActorContext
from apps.common.http import api_error

NOT_IMPLEMENTED_MESSAGE = "literature.services 尚未实现（MVP 骨架阶段，见 docs/mvp/05 阶段 B）"


def upload_pdf(
    actor: ActorContext,
    project_id: Any,
    uploaded_file: Any,
    metadata: Mapping[str, Any],
    key: Any,
) -> Any:
    """受理 PDF 上传。

    短事务内：幂等结果查找 → Project 行锁 → 空闲与 30 篇上限校验 →
    关联记录与 corpus_revision 更新。
    事务外：暂存、哈希、PDF 基础检查（格式 / 体积 / 页数 / 加密）；
    提交失败清理可确认无引用的暂存文件。

    相同项目同一 SHA256 重传返回既有 Paper（201 + X-Resource-Reused:true），
    不递增语料版本、不改其 metadata/included。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def update_paper(
    actor: ActorContext,
    paper_id: Any,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Any:
    """编辑元数据 / 纳入状态；实际变化时递增 Paper.version 与 Project.corpus_revision。"""
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def import_arxiv(
    actor: ActorContext,
    project_id: Any,
    arxiv_ids: Sequence[str],
    key: Any,
) -> Any:
    """受理 arXiv 批量导入（1—10 个不同 ID）。

    受理时初始化 Job.result.items：每项
    `{requested_id, resolved_source_id:null, status:"pending", paper_id:null, error:null}`。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)


def execute_import_arxiv(context: Any, job_id: Any) -> Any:
    """worker 入口：逐项解析具体版本 → 先持久化 resolved_source_id → 再下载 → 登记。

    成功项不因后续失败回滚，也不虚报整体成功；
    重试只补 pending/failed，沿用已解析的具体版本。
    """
    raise api_error("SERVICE_UNAVAILABLE", NOT_IMPLEMENTED_MESSAGE, retryable=False)
