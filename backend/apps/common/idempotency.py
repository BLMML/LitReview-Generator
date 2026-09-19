"""common.idempotency：规范化作用域、请求摘要比较、原受理结果重放、并发收敛与过期清理。

职责（docs/mvp/01 第 3.3 节、docs/mvp/02 第 5.2 节）：
- `claim_or_replay(actor, method, path, key, request_hash)`；
- 返回“重放结果”或“本次写入权”；
- 必须先于 PROJECT_BUSY 判断；
- 上传摘要包含 SHA256；
- 不同请求哈希 → 409 IDEMPOTENCY_CONFLICT；
- 幂等记录与业务资源、Job 在同一事务提交。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.auth import ActorContext
from apps.common.http import api_error
from apps.common.models import IdempotencyRecord

# 只保留成功受理 / 创建响应所必需的安全响应头，绝不存 Cookie 或密钥
SAFE_RESPONSE_HEADERS = ("Location", "X-Resource-Reused")


@dataclass(frozen=True)
class ReplayResult:
    """同键同体重放：原样返回先前的响应，不再执行任何副作用。"""

    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_replay(self) -> bool:
        return True


@dataclass(frozen=True)
class Claim:
    """本次请求获得写入权；调用方须在同一事务内调用 `record_claim`。"""

    user: Any
    method: str
    canonical_path: str
    key: uuid.UUID
    request_hash: str

    @property
    def is_replay(self) -> bool:
        return False


def normalize_json(payload: Any) -> str:
    """规范化 JSON：键排序、紧凑分隔、非 ASCII 原样保留。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_fingerprint(payload: Any = None, *, file_sha256: str | None = None) -> str:
    """请求摘要。上传请求必须把实际文件 SHA256 与元数据一并纳入。"""
    material = normalize_json(payload if payload is not None else {})
    if file_sha256:
        material = f"{material}|sha256:{file_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_path_of(request: Any) -> str:
    """规范化路径：不含查询串、不含尾斜杠，保证同键作用域稳定。"""
    path = request.path or "/"
    return path.rstrip("/") or "/"


def parse_idempotency_key(raw: str | None) -> uuid.UUID:
    """标记 K 的写请求必须携带合法 UUID 的 Idempotency-Key。"""
    if not raw:
        raise api_error(
            "VALIDATION_ERROR",
            "缺少 Idempotency-Key 请求头",
            {"header": {"Idempotency-Key": ["该请求必须携带 UUID 幂等键"]}},
        )
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise api_error(
            "VALIDATION_ERROR",
            "Idempotency-Key 必须是 UUID",
            {"header": {"Idempotency-Key": ["格式不合法"]}},
        ) from exc


def _lookup(claim: Claim) -> IdempotencyRecord | None:
    return IdempotencyRecord.objects.filter(
        user=claim.user,
        method=claim.method,
        canonical_path=claim.canonical_path,
        key=claim.key,
    ).first()


def claim_or_replay(
    actor: ActorContext,
    method: str,
    canonical_path: str,
    key: uuid.UUID,
    request_hash: str,
) -> ReplayResult | Claim:
    """同键查询：命中同体则重放，命中异体则 409，未命中则交出写入权。

    必须在 Project 行锁与 PROJECT_BUSY 判断之前调用。
    """
    claim = Claim(
        user=actor.user,
        method=method.upper(),
        canonical_path=canonical_path,
        key=key,
        request_hash=request_hash,
    )
    existing = _lookup(claim)
    if existing is None:
        return claim
    if existing.request_hash != request_hash:
        raise api_error("IDEMPOTENCY_CONFLICT")
    # 同请求直接返回原状态码和资源，即使 Job 已完成也不再发任务
    return ReplayResult(
        status_code=existing.response_status,
        body=existing.response_body,
        headers={
            name: existing.response_headers[name]
            for name in SAFE_RESPONSE_HEADERS
            if name in (existing.response_headers or {})
        },
    )


def record_claim(
    claim: Claim,
    *,
    status_code: int,
    body: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    project: Any | None = None,
    job: Any | None = None,
) -> IdempotencyRecord:
    """在受理事务内落库幂等记录。

    并发首次请求由唯一约束收敛：冲突后重新读取已提交记录，不换 key 自动重试。
    """
    retention_days = int(settings.MVP_LIMITS["IDEMPOTENCY_RETENTION_DAYS"])
    safe_headers = {
        name: value
        for name, value in (headers or {}).items()
        if name in SAFE_RESPONSE_HEADERS
    }
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(
                user=claim.user,
                method=claim.method,
                canonical_path=claim.canonical_path,
                key=claim.key,
                request_hash=claim.request_hash,
                response_status=status_code,
                response_body=json.loads(normalize_json(body)),
                response_headers=safe_headers,
                project=project,
                job=job,
                expires_at=timezone.now() + timedelta(days=retention_days),
            )
    except IntegrityError as exc:
        # 另一个并发请求已经写入：由调用方按 ReplayResult 重新读取
        raise api_error("IDEMPOTENCY_CONFLICT", "该幂等键正在被并发处理") from exc


def purge_expired(*, now: Any | None = None) -> int:
    """清理过期幂等记录。

    活跃 Job 关联的记录不可过期清理；这里只删除 `expires_at` 已过且没有未终态 Job 的行。
    """
    moment = now or timezone.now()
    active_job_statuses = ("queued", "running", "cancelling")
    candidates = IdempotencyRecord.objects.filter(expires_at__lt=moment)
    blocked = candidates.filter(job__status__in=active_job_statuses).values_list("id", flat=True)
    deleted, _ = candidates.exclude(id__in=list(blocked)).delete()
    return deleted
