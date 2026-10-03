"""literature.services：上传即时校验与归档、元数据编辑、arXiv 批量导入。

职责（docs/mvp/01 第 4.2 节）：
- `upload_pdf(actor, project_id, uploaded_file, metadata, key) → Paper`
- `update_paper(actor, paper_id, expected_version, changes) → Paper`
- `import_arxiv(actor, project_id, arxiv_ids, key) → AcceptedJob`
- `execute_import_arxiv(context, job_id) → ItemResults`

事务边界（docs/mvp/01 第 7.2 节）：
- 「上传 / 编辑」：幂等结果查找、Project 行锁、空闲与版本校验、关联记录与语料版本更新
  在同一短事务内；暂存、哈希、PDF 基础检查在事务外。
- 「受理后台任务」：先幂等查找，再锁 Project，冻结输入、创建 Job 与资源占位；
  提交后投递队列（由 `jobs.services.accept_job` 负责，本模块**不再重复投递**）。

哈希与 idempotency 顺序说明：上传接口的幂等摘要必须包含文件 SHA256，而 SHA256 只有
读到内容才知道，因此本实现先暂存取哈希、再查幂等记录。这样也顺带避免了为一次「同键重放」
重复做 PDF 解析；重放命中时本次暂存文件立即丢弃。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.auth import ActorContext
from apps.common.http import ApiError, api_error
from apps.common.idempotency import (
    Claim,
    ReplayResult,
    claim_or_replay,
    record_claim,
    request_fingerprint,
)
from apps.common.locking import lock_project, require_project_idle
from apps.common.models import FileAsset
from apps.common.storage import commit_asset, discard_staged
from apps.jobs.models import JOB_KIND_IMPORT_ARXIV, Job
from apps.jobs.services import accept_job, complete_job, fail_job, mark_cancelled, record_progress
from apps.literature import arxiv as arxiv_client
from apps.literature.models import SOURCE_ARXIV, SOURCE_UPLOAD, Paper
from apps.parsing.pdf import inspect_pdf_file

logger = logging.getLogger("apps.literature.services")

REUSED_HEADER = "X-Resource-Reused"
UNKNOWN_TITLE = "未命名文献"

# 上传内容的存储与下载类型固定为 PDF：类型由内容检查确认，不由上传者声明
PDF_MEDIA_TYPE = "application/pdf"

# 导入账本中不可再被重试填充的终态
ITEM_STATUS_PENDING = "pending"
ITEM_STATUS_IMPORTED = "imported"
ITEM_STATUS_DUPLICATE = "duplicate"
ITEM_STATUS_FAILED = "failed"
ITEM_RETRYABLE_STATUSES = (ITEM_STATUS_PENDING, ITEM_STATUS_FAILED)

# retry_of 链的向上查找上限：正常链条很短，限步只用于防范脏数据成环
MAX_RETRY_CHAIN = 20


# ── 基础工具 ──────────────────────────────────────────────────
def _limits() -> Mapping[str, Any]:
    return settings.MVP_LIMITS


def _require_owned_project(actor: ActorContext, project_id: Any) -> Any:
    """越权与不存在统一 404，不泄露资源归属。"""
    from apps.projects.models import Project

    project = Project.objects.filter(pk=project_id, owner=actor.user).first()
    if project is None:
        raise api_error("NOT_FOUND")
    return project


def _require_owned_paper(actor: ActorContext, paper_id: Any, *, lock: bool = False) -> Paper:
    queryset = Paper.objects.select_for_update() if lock else Paper.objects
    paper = queryset.select_related("project", "file_asset").filter(
        pk=paper_id, project__owner=actor.user
    ).first()
    if paper is None:
        raise api_error("NOT_FOUND")
    return paper


def _project_pk(project: Any) -> Any:
    """接受 Project 实例或裸 project_id。

    导入路径手上只有 `job.project_id`，上传路径手上是 Project 实例；
    两种调用都要能用，避免再出现「传错对象导致计数永远通过」这类静默错误。
    """
    return getattr(project, "pk", project)


def _paper_capacity_left(project: Any, *, pending_new: int = 0) -> int:
    """项目还可新增多少篇（含已取消纳入的）。超限返回 0。"""
    existing = Paper.objects.filter(project_id=_project_pk(project)).count()
    return max(0, int(_limits()["PAPERS_PER_PROJECT_MAX"]) - existing - pending_new)


def _clean_title(raw: Any, fallback: str) -> str:
    if raw is None:
        return fallback
    text = str(raw).replace("\x00", "").strip()
    if not text:
        raise api_error(
            "VALIDATION_ERROR", "title 不能为空", {"fields": {"title": ["不能为空"]}}
        )
    max_length = int(_limits()["PAPER_TITLE_MAX"])
    if len(text) > max_length:
        raise api_error(
            "VALIDATION_ERROR",
            f"title 最长 {max_length} 个字符",
            {"fields": {"title": [f"最长 {max_length} 个字符"]}},
        )
    return text


def _title_from_filename(filename: str) -> str:
    stem = (filename or "").rsplit(".", 1)[0].strip()
    return (stem or UNKNOWN_TITLE)[: int(_limits()["PAPER_TITLE_MAX"])]


# ── 上传 ──────────────────────────────────────────────────────
def _upload_claim(
    actor: ActorContext, project_id: Any, key: Any, request_hash: str
) -> ReplayResult | Claim:
    if isinstance(key, Claim):
        return claim_or_replay(actor, key.method, key.canonical_path, key.key, key.request_hash)
    return claim_or_replay(
        actor, "POST", f"/api/v1/projects/{project_id}/papers/upload", uuid.UUID(str(key)), request_hash
    )


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
    if uploaded_file is None:
        raise api_error("VALIDATION_ERROR", "缺少 file 字段", {"fields": {"file": ["必填"]}})

    # 归属校验放在做任何文件工作之前；越权直接 404，不留下暂存垃圾
    project = _require_owned_project(actor, project_id)

    original_name = getattr(uploaded_file, "name", "") or "upload.pdf"

    staged = None
    asset = None
    try:
        # 体积上限在暂存过程中即时生效，不会先把超大文件落满磁盘
        staged = _stage_upload(uploaded_file, original_name)

        # 事务外：真实内容检查（格式 / 页数 / 加密 / 损坏）
        inspection = inspect_pdf_file(staged.path)

        title = _clean_title(metadata.get("title"), _title_from_filename(staged.original_filename))
        request_hash = request_fingerprint(
            {"title": title, "filename": staged.original_filename}, file_sha256=staged.sha256
        )

        with transaction.atomic():
            locked = lock_project(project.pk, owner=actor.user)

            # 幂等重放优先于忙碌判断（docs/mvp/00 第 5.2 节：相同幂等键的重放是例外）
            replay_or_claim = _upload_claim(actor, project_id, key, request_hash)
            if isinstance(replay_or_claim, ReplayResult):
                # 同键同体重放：不重复落盘、不重复建文献；本次暂存文件立即丢弃
                discard_staged(staged)
                staged = None
                return _replay_response(replay_or_claim)

            # 活跃任务期间不接受新的语料写入
            require_project_idle(locked)

            existing = Paper.objects.select_related("file_asset").filter(
                project=locked, file_sha256=staged.sha256
            ).first()
            if _paper_capacity_left(locked) <= 0 and existing is None:
                raise api_error(
                    "PAPER_LIMIT_EXCEEDED",
                    details={"limit": _limits()["PAPERS_PER_PROJECT_MAX"]},
                )

            if existing is not None:
                # 同项目同内容：返回既有文献，成功码仍是 201，不改其元数据 / 纳入状态。
                # 本次上传的文件没有任何引用，立即清理，不留给日后清扫兜底。
                discard_staged(staged)
                staged = None
                reused = True
                paper = existing
            else:
                asset = commit_asset(locked, staged, FileAsset.PURPOSE_ORIGINAL_PDF)
                staged = None  # 已归档，不能再当临时文件清理
                paper = Paper.objects.create(
                    project=locked,
                    file_asset=asset,
                    title=title,
                    authors=[],
                    year=None,
                    source=SOURCE_UPLOAD,
                    included=True,
                    file_sha256=asset.sha256,
                    page_count=inspection.page_count,
                )
                locked.corpus_revision += 1
                locked.save(update_fields=["corpus_revision", "updated_at"])
                reused = False

            dto = _paper_dto(paper)
            if isinstance(replay_or_claim, Claim):
                record_claim(
                    replay_or_claim,
                    status_code=201,
                    # 保存**原受理响应快照**：重放必须原样返回第一次的结果，
                    # 而不是拿当前 Paper 重新序列化（之后可能已被编辑）
                    body={"paper": dto, "reused": reused},
                    headers={REUSED_HEADER: "true"} if reused else None,
                    project=locked,
                )
        return _upload_result(dto, reused=reused)
    except BaseException:
        # 失败清理：只删本次确未被任何已提交资源引用的文件
        discard_staged(staged)
        _discard_asset(asset)
        raise


def _stage_upload(uploaded_file: Any, original_name: str) -> Any:
    """暂存上传内容。

    **不信任上传者声明的 Content-Type**：`text/html` 之类的值会被原样存下来，
    再通过授权下载返回给浏览器，等于让上传者决定响应类型。文件类型一律由
    后续的 PDF 结构检查（只认内容）确认，暂存类型固定为 application/pdf。
    """
    from apps.common.storage import stage_file

    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    return stage_file(
        uploaded_file,
        limit_bytes=int(_limits()["PDF_MAX_BYTES"]),
        original_filename=original_name,
        media_type=PDF_MEDIA_TYPE,
        suffix=".pdf",
    )


def _paper_dto(paper: Paper) -> dict[str, Any]:
    """PaperDTO 的原始字典（受理响应与幂等快照共用同一份序列化）。

    用 DRF 的 `DateTimeField` 渲染时间，而不是自己拼 ISO 字符串：这样幂等快照与
    正常出站序列化**逐字节一致**，重放才能真正等于原响应。
    """

    asset = paper.file_asset
    return {
        "id": str(paper.id),
        "project_id": str(paper.project_id),
        "title": paper.title,
        "authors": list(paper.authors or []),
        "year": paper.year,
        "source": paper.source,
        "source_id": paper.source_id,
        "source_url": paper.source_url,
        "included": paper.included,
        "version": paper.version,
        "file_sha256": paper.file_sha256,
        "file_size": asset.size_bytes,
        "page_count": paper.page_count,
        "original_filename": asset.original_filename,
        "file_url": f"/api/v1/papers/{paper.id}/file",
        "created_at": _render_datetime(paper.created_at),
        "updated_at": _render_datetime(paper.updated_at),
    }


_datetime_field = None


def _render_datetime(moment: Any) -> Any:
    global _datetime_field
    if _datetime_field is None:
        from rest_framework import serializers

        _datetime_field = serializers.DateTimeField()
    return _datetime_field.to_representation(moment)


def _discard_asset(asset: Any) -> None:
    """回滚本次已归档但未被引用的文件；已被 Paper 引用的资产绝不删除。

    只在事务已回滚、或确认没有 Paper 指向它时才动手，避免 ProtectedError 掩盖真实错误。
    """
    if asset is None:
        return
    try:
        if Paper.objects.filter(file_asset_id=asset.pk).exists():
            return
        from apps.common.storage import resolve_storage_key

        resolve_storage_key(asset.storage_key).unlink(missing_ok=True)
        asset.delete()
    except Exception:  # pragma: no cover - 清理失败不得掩盖原始错误
        logger.warning("清理未引用文件失败：%s", getattr(asset, "pk", None))


class UploadedPaper:
    """上传结果：出站 DTO + 是否为复用既有文献。

    刻意携带**已序列化的原始响应体**而不是 Paper 实例：幂等重放必须原样重发
    第一次受理的响应，若在重放时重新序列化当前 Paper，用户编辑过的标题与
    递增后的 version 会污染原响应（复核问题 P2 第 5 项）。
    """

    __slots__ = ("data", "reused")

    def __init__(self, data: Mapping[str, Any], reused: bool) -> None:
        self.data = dict(data)
        self.reused = reused

    @property
    def paper_id(self) -> str:
        return str(self.data["id"])


def _upload_result(data: Mapping[str, Any], *, reused: bool) -> UploadedPaper:
    return UploadedPaper(data, reused)


def _replay_response(replay: ReplayResult) -> UploadedPaper:
    """幂等重放：直接返回**第一次受理时保存的响应快照**。

    不重新查询也不重新序列化当前 Paper——之后发生的编辑不应改变原响应。
    """
    body = dict(replay.body or {})
    data = body.get("paper")
    if not isinstance(data, Mapping):  # pragma: no cover - 旧格式记录
        raise api_error("IDEMPOTENCY_CONFLICT", "该幂等键的历史响应不可重放")
    return UploadedPaper(data, bool(body.get("reused")))


# ── 元数据编辑 ────────────────────────────────────────────────
def update_paper(
    actor: ActorContext,
    paper_id: Any,
    expected_version: int,
    changes: Mapping[str, Any],
) -> Any:
    """编辑元数据 / 纳入状态；实际变化时递增 Paper.version 与 Project.corpus_revision。"""
    editable = ("title", "authors", "year", "included")
    if not any(name in changes for name in editable):
        raise api_error(
            "VALIDATION_ERROR",
            "至少提供 title / authors / year / included 之一",
            {"fields": {name: ["未提供可修改字段"] for name in editable}},
        )

    with transaction.atomic():
        paper = _require_owned_paper(actor, paper_id, lock=True)
        project = lock_project(paper.project_id, owner=actor.user)
        # 活跃任务期间不接受元数据 / 纳入状态修改（契约：409 PROJECT_BUSY）
        require_project_idle(project)

        if int(paper.version) != int(expected_version):
            raise api_error("VERSION_CONFLICT", details={"current_version": paper.version})

        dirty: list[str] = []

        if "title" in changes:
            new_title = _clean_title(changes["title"], paper.title)
            if new_title != paper.title:
                paper.title = new_title
                dirty.append("title")

        if "authors" in changes:
            new_authors = _clean_authors(changes["authors"])
            if new_authors != list(paper.authors or []):
                paper.authors = new_authors
                dirty.append("authors")

        if "year" in changes:
            new_year = _clean_year(changes["year"])
            if new_year != paper.year:
                paper.year = new_year
                dirty.append("year")

        if "included" in changes:
            new_included = bool(changes["included"])
            if new_included != paper.included:
                paper.included = new_included
                dirty.append("included")

        if not dirty:
            # 提交内容与现值相同：不递增版本，避免把无变化保存伪装成编辑
            return paper

        paper.version += 1
        paper.save(update_fields=[*dirty, "version", "updated_at"])
        # 元数据与纳入集合都是语料快照的一部分：变化让既有 Run 过时
        project.corpus_revision += 1
        project.save(update_fields=["corpus_revision", "updated_at"])
        return paper


def _clean_authors(raw: Any) -> list[str]:
    if raw is None:
        raise api_error(
            "VALIDATION_ERROR", "authors 不能为 null", {"fields": {"authors": ["必须是数组"]}}
        )
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise api_error(
            "VALIDATION_ERROR", "authors 必须是数组", {"fields": {"authors": ["必须是数组"]}}
        )
    cleaned: list[str] = []
    for item in raw:
        text = str(item).replace("\x00", "").strip()
        if not text:
            raise api_error(
                "VALIDATION_ERROR",
                "authors 不能包含空字符串",
                {"fields": {"authors": ["不能包含空字符串"]}},
            )
        if len(text) > 200:
            raise api_error(
                "VALIDATION_ERROR",
                "单个作者名最长 200 个字符",
                {"fields": {"authors": ["单个作者名最长 200 个字符"]}},
            )
        cleaned.append(text)
    if len(cleaned) != len(set(cleaned)):
        raise api_error(
            "VALIDATION_ERROR", "authors 存在重复项", {"fields": {"authors": ["存在重复项"]}}
        )
    return cleaned


def _clean_year(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise api_error(
                "VALIDATION_ERROR", "year 必须是整数", {"fields": {"year": ["必须是整数"]}}
            ) from exc
    else:
        value = raw
    minimum = int(_limits()["PAPER_YEAR_MIN"])
    if value < minimum or value > _current_year():
        raise api_error(
            "VALIDATION_ERROR",
            f"year 必须在 {minimum}—{_current_year()} 之间",
            {"fields": {"year": [f"必须在 {minimum}—{_current_year()} 之间"]}},
        )
    return value


def _current_year() -> int:
    from django.utils import timezone

    return timezone.now().year


# ── arXiv 导入：受理 ──────────────────────────────────────────
def import_arxiv(
    actor: ActorContext,
    project_id: Any,
    arxiv_ids: Sequence[str],
    key: Any,
) -> Any:
    """受理 arXiv 批量导入（1—10 个不同 ID）。

    受理时初始化 Job.result.items：每项
    `{requested_id, resolved_source_id:null, status:"pending", paper_id:null, error:null}`。

    幂等记录、Project 锁、空闲校验与提交后投递都由 `jobs.services.accept_job` 完成，
    本模块**不重复记录幂等响应，也不重复 dispatch**。
    """
    requested = arxiv_client.validate_arxiv_ids(arxiv_ids)
    project = _require_owned_project(actor, project_id)

    items = [
        {
            "requested_id": arxiv_id,
            "resolved_source_id": None,
            "status": ITEM_STATUS_PENDING,
            "paper_id": None,
            "error": None,
        }
        for arxiv_id in requested
    ]
    return accept_job(
        actor,
        project,
        JOB_KIND_IMPORT_ARXIV,
        {"frozen_input": {"arxiv_ids": requested, "items": items}},
        idempotency=key,
        # 契约（03 第 2.2 节）要求受理时 `Job.result.items` 就存在，
        # 不能等 worker 执行后才出现；否则 202 之后立刻查询会拿到 null。
        result={"items": items},
    )


# ── arXiv 导入：执行 ──────────────────────────────────────────
def _load_items(job: Job) -> list[dict[str, Any]]:
    """账本来源：**优先本次尝试已持久化的账本**，否则用冻结输入叠加祖先进度。

    不能只看直接前一次尝试，也不能让中间尝试的初始 `frozen_input` 截断更早的真实进度：
    「排队期间被取消、从未执行」的尝试会把初始 pending 项写进 `frozen_input`，
    若把它当成有效进度，成功项与已冻结的具体版本都会被重置。
    """
    current = _result_items(job)
    if _has_progress(current):
        return current

    items = [
        dict(item) for item in (job.frozen_input or {}).get("items", []) if isinstance(item, Mapping)
    ]
    ancestor_items = _nearest_ancestor_items(job)
    if not ancestor_items:
        return items

    for entry in items:
        old = ancestor_items.get(str(entry.get("requested_id")))
        if not old:
            continue
        if old.get("status") in (ITEM_STATUS_IMPORTED, ITEM_STATUS_DUPLICATE):
            # 成功项沿用旧账本；重试只补未完成项
            entry.update(
                status=old.get("status"),
                resolved_source_id=old.get("resolved_source_id"),
                paper_id=old.get("paper_id"),
                error=old.get("error"),
            )
        elif old.get("resolved_source_id"):
            # 已解析出的具体版本必须沿用，重试不漂移到新版本
            entry["resolved_source_id"] = old["resolved_source_id"]
    return items


def _result_items(job: Job) -> list[dict[str, Any]]:
    entries = (job.result or {}).get("items")
    return [dict(item) for item in entries] if isinstance(entries, list) else []


def _has_progress(items: list[dict[str, Any]]) -> bool:
    """账本是否已经超出了「初始全 pending」状态。

    只含 pending 且没有 resolved_source_id 的账本等于没跑过，
    不能拿它当作有效进度去覆盖更早尝试的真实结果。
    """
    return any(
        item.get("status") != ITEM_STATUS_PENDING or item.get("resolved_source_id")
        for item in items
    )


def _nearest_ancestor_items(job: Job) -> dict[str, dict[str, Any]]:
    """沿 retry_of 链向上取最近一个**含真实进度**的账本。

    中间尝试的初始 `frozen_input` 不算进度；链条有环或过长时限步，
    避免脏数据把 worker 卡死。
    """
    seen: set[Any] = {job.pk}
    current = job
    for _ in range(MAX_RETRY_CHAIN):
        if current.retry_of_job_id is None:
            return {}
        parent = Job.objects.filter(pk=current.retry_of_job_id).first()
        if parent is None or parent.pk in seen:
            return {}
        seen.add(parent.pk)
        for entries in (_result_items(parent), _frozen_items(parent)):
            if _has_progress(entries):
                return {
                    str(item["requested_id"]): item
                    for item in entries
                    if item.get("requested_id")
                }
        current = parent
    return {}


def _frozen_items(job: Job) -> list[dict[str, Any]]:
    entries = (job.frozen_input or {}).get("items")
    return [dict(item) for item in entries] if isinstance(entries, list) else []


def _save_items(
    context: Any, job: Job, items: list[dict[str, Any]], *, allow_cancelling: bool = False
) -> bool:
    """把账本写回当前作业；租约已失效时返回 False，绝不改动任务记录。

    只比较 `lease_token` 是不够的：租约**过期**时恢复扫描可能还没递增 token，
    此时旧 worker 若继续写账本，就会在已经判死的任务上留下新的进度
    （文献写入已被发布检查拦住，但任务记录仍会被污染）。
    因此写入前必须在短事务内确认：token 未变、状态允许写入、租约未过期、
    且没有"早于本次写入"的取消意图。
    """
    if str(job.pk) != str(context.job_id) or str(job.project_id) != str(context.project_id):
        return False
    # 外层事务持有 _lease_writable 获取的 Job 行锁直到 UPDATE 提交。
    # 若仅在 helper 内开事务，取消请求可在校验成功和写账本之间先提交。
    with transaction.atomic():
        if not _lease_writable(context, allow_cancelling=allow_cancelling):
            return False
        now = timezone.now()
        writable = Job.objects.filter(
            pk=context.job_id,
            project_id=context.project_id,
            lease_token=context.lease_token,
            lease_expires_at__gt=now,
            status__in=("running", "cancelling") if allow_cancelling else ("running",),
        )
        if not allow_cancelling:
            writable = writable.filter(cancel_requested_at__isnull=True)
        # 同时防住校验后、真正 UPDATE 前租约刚好到期的边界。
        return bool(writable.update(result={"items": items}, updated_at=now))


def _lease_writable(context: Any, *, allow_cancelling: bool) -> bool:
    """短事务内判定本次尝试是否仍有权写入任务记录。"""
    from apps.common.locking import check_execution_lease

    with transaction.atomic():
        current = (
            Job.objects.select_for_update()
            .filter(pk=context.job_id, project_id=context.project_id)
            .first()
        )
        if current is None:
            return False
        if current.cancel_requested_at is not None and not allow_cancelling:
            return False
        try:
            check_execution_lease(current, context.lease_token, allow_cancelling=allow_cancelling)
        except ApiError:
            return False
        # 租约过期后不得再写入：恢复扫描可能尚未接管，但这次尝试已经失去资格
        if current.lease_expires_at is None or current.lease_expires_at <= timezone.now():
            return False
    return True


def _job_error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"code": code, "message": message, "details": {}, "retryable": retryable}


def _find_existing_paper(
    project_id: Any, *, source_id: str | None = None, file_sha256: str | None = None
) -> Paper | None:
    """按来源版本或文件内容找项目内已有文献。

    去重是「同项目同内容」的规则，与来源无关：同一份 PDF 无论是上传还是导入得到，
    都应复用同一篇。只按 `source=arxiv` 查会漏掉以「上传」来源存在的同内容文献。
    """
    queryset = Paper.objects.filter(project_id=_project_pk(project_id))
    if file_sha256 is not None:
        return queryset.filter(file_sha256=file_sha256).first()
    if source_id is not None:
        return queryset.filter(source=SOURCE_ARXIV, source_id=source_id).first()
    return None


def _mark_duplicate(item: dict[str, Any], existing: Paper) -> None:
    """把账本项记为复用既有文献。"""
    item["status"] = ITEM_STATUS_DUPLICATE
    item["paper_id"] = str(existing.id)
    item["error"] = None


def execute_import_arxiv(context: Any, job_id: Any) -> list[dict[str, Any]]:
    """worker 入口：逐项解析具体版本 → 先持久化 resolved_source_id → 再下载 → 登记。

    成功项不因后续失败回滚，也不虚报整体成功；
    重试只补 pending/failed，沿用已解析的具体版本。

    `job_id` 是批次资源 ID（受理时等于首次 Job ID）。**当前作业必须用 `context.job_id`
    读取**：重试产生的新 Job 有自己的账本，改 `job_id` 指向的旧 Job 会污染历史。
    出网与文件处理都在数据库事务外，租约检查靠 fencing 写入保证。
    """
    job = Job.objects.filter(pk=context.job_id, project_id=context.project_id).first()
    if job is None:
        raise api_error("NOT_FOUND")
    if uuid.UUID(str(job.resource_id)) != uuid.UUID(str(job_id)):
        # 受理时 resource_id 固定等于首次 Job ID，重试沿用；不一致说明上下文被串了
        logger.warning("导入批次资源 ID 与上下文不一致：%s / %s", job.resource_id, job_id)

    items = _load_items(job)
    todo = [item for item in items if item.get("status") in ITEM_RETRYABLE_STATUSES]
    total = len(items)
    done = total - len(todo)

    _save_items(context, job, items)
    record_progress(context, completed=done, total=total, unit="paper", stage="resolve")

    cancelled = False
    for item in todo:
        if context.cancel_checker() or not _lease_alive(context):
            cancelled = True
            break

        if not _import_one_item(context, job, items, item):
            # 本项未完成（取消已提交 / 租约被取代），账本保持原状供重试继续
            cancelled = True
            _save_items(context, job, items)
            break

        done += 1
        if not _save_items(context, job, items):
            # 账本已无法写入：本次尝试失效，交给恢复扫描处理
            raise api_error("INVALID_JOB_STATE", "执行租约已失效，无法提交导入账本")

        if not _report_progress(context, job, completed=done, total=total):
            # 本项产物合法，但取消已提交或租约已失效：进度属于被禁止的写入，
            # 直接收尾为取消，剩余项留给重试（账本与已导入文献都已落地）
            cancelled = True
            break

    _save_items(context, job, items)

    if cancelled:
        mark_cancelled(context, job)
        return items

    failures = [item for item in items if item.get("status") == ITEM_STATUS_FAILED]
    if failures:
        # 任一失败即整批失败，但成功项保留（docs/mvp/00 第 9 节）
        fail_job(
            context,
            _job_error(
                failures[0].get("error", {}).get("code", "ARXIV_FETCH_FAILED"),
                f"{len(failures)} 项导入失败，成功项已保留；重试只补未完成项",
                retryable=True,
            ),
            result={"items": items},
        )
        return items

    # 仍有 pending 项（例如上一轮被取消）时不能宣布成功
    if any(item.get("status") == ITEM_STATUS_PENDING for item in items):
        fail_job(
            context,
            _job_error("ARXIV_FETCH_FAILED", "仍有未完成的导入项，请重试", retryable=True),
            result={"items": items},
        )
        return items

    complete_job(context, {"items": items})
    return items


def _lease_alive(context: Any, *, allow_cancelling: bool = False) -> bool:
    from apps.common.locking import check_execution_lease

    job = Job.objects.filter(pk=context.job_id).first()
    if job is None:
        return False
    try:
        check_execution_lease(job, context.lease_token, allow_cancelling=allow_cancelling)
    except ApiError:
        return False
    return True


def _report_progress(context: Any, job: Job, *, completed: int, total: int) -> bool:
    """提交进度；取消已提交或租约失效时返回 False，而不是抛出中断整批。

    进度写入本身受 fencing 保护（`record_progress` 会再校验一次租约），
    这里把「被 fencing 拒绝」当作正常的协作取消信号处理。
    """
    try:
        record_progress(context, completed=completed, total=total, stage="register")
    except ApiError as exc:
        if exc.code == "INVALID_JOB_STATE":
            return False
        raise
    return True


def _assert_publishable(context: Any, locked_project: Any) -> None:
    """发布业务产物前的执行资格检查；**必须在写 Paper 的同一事务内调用**。

    只检查租约时间是不够的：下载期间恢复扫描可能已经把这次尝试判死并递增
    `lease_token`，此时旧 worker 若继续写库，就会留下「任务失败但文献已入库」
    的不一致状态。事后拒绝写账本无法撤销已提交的文献。

    取消时间必须从**锁定的当前行**读取：调用方手里的 Job 是下载前的快照，
    下载期间到达的取消不会体现在它上面。

    允许 `cancelling`：那是「本项已经开始、取消随后到达」的正常收尾路径，
    产物合法且会被如实记为已导入；但取消意图若早于本次发布，就必须拒绝。
    """
    from apps.common.locking import check_execution_lease

    current = (
        Job.objects.select_for_update()
        .filter(pk=context.job_id, project_id=_project_pk(locked_project))
        .first()
    )
    if current is None:
        raise api_error("INVALID_JOB_STATE", "执行上下文已失效")
    if current.cancel_requested_at is not None:
        raise api_error("INVALID_JOB_STATE", "任务已被请求取消")
    check_execution_lease(current, context.lease_token, allow_cancelling=True)


def _record_item_error(item: dict[str, Any], exc: Exception) -> None:
    item["status"] = ITEM_STATUS_FAILED
    if isinstance(exc, ApiError):
        item["error"] = exc.as_error_body()
    else:
        item["error"] = _job_error("INTERNAL_ERROR", "该项导入失败", retryable=True)


def _import_one_item(
    context: Any, job: Job, items: list[dict[str, Any]], item: dict[str, Any]
) -> bool:
    """处理单个导入项，返回是否真正处理完成（未完成 = 取消 / 租约失效）。

    `item` 必须是 `items` 里的同一个对象：中途持久化时整份内存账本一起落库，
    避免出现「本地进度」和「已存账本」两份互相覆盖的副本。

    除「本次尝试已失效」以外的异常，一律记入该项台账后返回 True——
    单项失败不能让整批停止，也不能丢账。
    """
    requested = str(item.get("requested_id"))

    try:
        # 1) 解析具体版本（重试时沿用已冻结的版本），解析后立即持久化再下载，
        #    否则中断重试会漂移到接口当前的最新版本
        if not item.get("resolved_source_id"):
            metadata = arxiv_client.fetch_metadata(requested)
            item["resolved_source_id"] = metadata.source_id
            if not _save_items(context, job, items):
                raise api_error("INVALID_JOB_STATE", "执行租约已失效，无法提交解析结果")
        else:
            metadata = arxiv_client.fetch_metadata(str(item["resolved_source_id"]))

        # 2) 容量上限：满容量时先看有没有**已有的同一篇**可以复用，再决定是否拒绝。
        #    只按 (source=arxiv, source_id) 判断是不够的：同一份 PDF 可能早就以
        #    「上传」来源存在于项目里，那样它应当按内容复用（duplicate），
        #    而不是被判成容量超限。真正需要新增时才拒绝。
        if _paper_capacity_left(job.project_id) <= 0:
            existing = _find_existing_paper(job.project_id, source_id=metadata.source_id)
            if existing is not None:
                _mark_duplicate(item, existing)
                return True
            # 来源不同但内容可能相同：需要拿到哈希才能判定，此时只能先下载。
            # 这是满容量下唯一不可避免的下载，数量受项目容量上限约束。
            staged_probe = None
            try:
                staged_probe = arxiv_client.download_pdf(metadata)
                existing = _find_existing_paper(
                    job.project_id, file_sha256=staged_probe.sha256
                )
            finally:
                discard_staged(staged_probe)
            if existing is not None:
                _mark_duplicate(item, existing)
                return True
            raise api_error(
                "PAPER_LIMIT_EXCEEDED",
                details={"limit": _limits()["PAPERS_PER_PROJECT_MAX"]},
            )

        # 3) 下载 + 与上传同一套 PDF 校验
        staged = None
        asset = None
        try:
            staged = arxiv_client.download_pdf(metadata)
            inspection = inspect_pdf_file(staged.path)

            with transaction.atomic():
                locked = lock_project(job.project_id)

                # 发布前必须重新确认执行资格：下载期间租约可能已被恢复扫描替换，
                # 或用户已提交取消。事后拒绝写账本无法撤销已提交的文献，
                # 因此这项检查必须与 Paper / 语料版本的提交同处一个短事务。
                _assert_publishable(context, locked)

                existing = _find_existing_paper(locked.pk, file_sha256=staged.sha256)
                if existing is None:
                    existing = _find_existing_paper(locked.pk, source_id=metadata.source_id)

                if existing is not None:
                    _mark_duplicate(item, existing)
                    discard_staged(staged)
                    staged = None
                    return True

                if _paper_capacity_left(locked.pk) <= 0:
                    raise api_error(
                        "PAPER_LIMIT_EXCEEDED",
                        details={"limit": _limits()["PAPERS_PER_PROJECT_MAX"]},
                    )

                asset = commit_asset(
                    locked, staged, FileAsset.PURPOSE_ORIGINAL_PDF, source_job=job
                )
                staged = None
                paper = Paper.objects.create(
                    project=locked,
                    file_asset=asset,
                    title=metadata.title[: int(_limits()["PAPER_TITLE_MAX"])],
                    authors=[name[:200] for name in metadata.authors],
                    year=metadata.year,
                    source=SOURCE_ARXIV,
                    source_id=metadata.source_id,
                    source_url=metadata.source_url,
                    included=True,
                    file_sha256=asset.sha256,
                    page_count=inspection.page_count,
                )
                locked.corpus_revision += 1
                locked.save(update_fields=["corpus_revision", "updated_at"])

            item["status"] = ITEM_STATUS_IMPORTED
            item["paper_id"] = str(paper.id)
            item["error"] = None
            return True
        except BaseException:
            discard_staged(staged)
            _discard_asset(asset)
            raise
    except ApiError as exc:
        if exc.code == "INVALID_JOB_STATE":
            # 取消已提交或租约被取代：本项未完成，账本保持原状供重试继续，
            # 由批次层停止后续项，不能把它记成 failed（那不是它的错，也不该被跳过）
            return False
        _record_item_error(item, exc)
        return True
    except Exception as exc:  # noqa: BLE001 - 单项失败不拖垮整批记账
        # exception 而不是 warning：单项失败必须留下可诊断的堆栈，
        # 只记类型名会让「为什么失败」无从查起
        logger.exception("导入单项失败 job=%s id=%s", job.pk, requested)
        _record_item_error(item, exc)
        return True
