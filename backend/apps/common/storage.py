"""common.storage：安全文件名、暂存、哈希、原子归档、授权后读取、临时文件清理。

职责（docs/mvp/01 第 3.3 / 7.3 节）：
- `stage_file(stream, limit_bytes) → StagedFile`
- `commit_asset(project, staged_file, kind) → FileAsset`
- `open_asset(asset) → BinaryStream`（调用者必须先校验资源权限）

约束：
- 内部存储键由程序生成，绝不拼接用户上传名形成磁盘路径；
- 原始 PDF 不覆盖：新文件形成新资产；
- 解析后路径必须仍位于 PRIVATE_STORAGE_ROOT 之内；
- 不通过公开 MEDIA_URL 暴露任何私有文件。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from django.conf import settings
from django.db import transaction

from apps.common.http import api_error

CHUNK_SIZE = 1024 * 1024  # 1 MiB
STAGING_DIRNAME = ".staging"

_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]")


@dataclass(frozen=True)
class StagedFile:
    """已落盘但尚未登记为 FileAsset 的暂存文件。"""

    path: Path
    sha256: str
    size_bytes: int
    original_filename: str
    media_type: str


def storage_root() -> Path:
    root = Path(settings.PRIVATE_STORAGE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def staging_root() -> Path:
    path = storage_root() / STAGING_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_display_filename(name: str | None, *, fallback: str = "untitled") -> str:
    """去掉路径片段与控制字符，只保留可安全显示的基名。"""
    if not name:
        return fallback
    # 同时处理 POSIX 与 Windows 分隔符，避免跨平台目录穿越
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", basename).strip().strip(".")
    return cleaned[:255] or fallback


def resolve_storage_key(storage_key: str) -> Path:
    """把内部存储键解析为绝对路径，并确认它没有逃出受控根目录。"""
    if not storage_key or storage_key.startswith("/") or ".." in Path(storage_key).parts:
        raise api_error("NOT_FOUND")
    root = storage_root()
    candidate = (root / storage_key).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise api_error("NOT_FOUND")
    return candidate


def build_storage_key(project_id: Any, kind: str, sha256: str, suffix: str = "") -> str:
    """生成内部存储键：项目隔离 + 内容寻址分片，不含用户提供的名称。"""
    shard = (sha256 or "0" * 8)[:2]
    return f"{project_id}/{kind}/{shard}/{uuid.uuid4().hex}{suffix}"


def iter_chunks(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        yield chunk


def stage_file(
    stream: BinaryIO,
    *,
    limit_bytes: int,
    original_filename: str = "",
    media_type: str = "application/octet-stream",
    suffix: str = "",
) -> StagedFile:
    """把上传流写入受控暂存区并计算 SHA256。

    - 超过 limit_bytes 立即中止并删除临时文件（413 FILE_TOO_LARGE）；
    - 任何异常都不会留下可被引用的暂存文件。
    """
    digest = hashlib.sha256()
    size = 0
    target = staging_root() / f"{uuid.uuid4().hex}{suffix}"
    try:
        with target.open("wb") as handle:
            for chunk in iter_chunks(stream):
                size += len(chunk)
                if size > limit_bytes:
                    raise api_error(
                        "FILE_TOO_LARGE",
                        details={"limit_bytes": limit_bytes},
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise api_error("VALIDATION_ERROR", "上传内容为空")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return StagedFile(
        path=target,
        sha256=digest.hexdigest(),
        size_bytes=size,
        original_filename=safe_display_filename(original_filename),
        media_type=media_type,
    )


def discard_staged(staged: StagedFile | None) -> None:
    """清理确认未被引用的暂存文件；调用方负责确认没有已提交资源引用它。"""
    if staged is None:
        return
    staged.path.unlink(missing_ok=True)


def commit_asset(
    project: Any,
    staged_file: StagedFile,
    kind: str,
    *,
    source_job: Any | None = None,
) -> Any:
    """把暂存文件原子归档并登记为 FileAsset。

    先写临时位置、再原子改名、最后登记数据库；只有数据库合法引用后才能对外提供下载。
    """
    from apps.common.models import FileAsset  # 延迟导入，避免模块级循环

    suffix = staged_file.path.suffix
    storage_key = build_storage_key(project.id, kind, staged_file.sha256, suffix)
    destination = resolve_storage_key(storage_key)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # 同卷内改名是原子的；失败则不登记任何记录
    os.replace(staged_file.path, destination)

    try:
        with transaction.atomic():
            asset = FileAsset.objects.create(
                project=project,
                storage_key=storage_key,
                purpose=kind,
                media_type=staged_file.media_type,
                original_filename=staged_file.original_filename,
                sha256=staged_file.sha256,
                size_bytes=staged_file.size_bytes,
                source_job=source_job,
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return asset


def open_asset(asset: Any) -> BinaryIO:
    """打开受控文件用于读取。调用者必须先完成业务资源归属校验。"""
    path = resolve_storage_key(asset.storage_key)
    if not path.is_file():
        # 数据库有引用但文件缺失：明确失败，不伪装成空文件
        raise api_error("NOT_FOUND", "文件不存在或不可读")
    return path.open("rb")


def asset_size(asset: Any) -> int:
    path = resolve_storage_key(asset.storage_key)
    try:
        return path.stat().st_size
    except OSError as exc:
        raise api_error("NOT_FOUND", "文件不存在或不可读") from exc


def cleanup_staging(older_than_seconds: int = 24 * 3600) -> int:
    """清理超过保留窗口的暂存文件；已被 FileAsset 引用的文件不在暂存区，天然安全。"""
    import time

    cutoff = time.time() - older_than_seconds
    removed = 0
    for entry in staging_root().iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:  # pragma: no cover - 并发清理时忽略
            continue
    return removed


def remove_storage_root_for_tests() -> None:  # pragma: no cover - 仅测试辅助
    """测试结束后清空私有存储根目录（仅供受控测试环境调用）。"""
    root = Path(settings.PRIVATE_STORAGE_ROOT)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
