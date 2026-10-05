"""核验报告完整性与导出警示；受理、执行和重试共用同一规则。"""

from __future__ import annotations

from typing import Any

from apps.common.http import api_error
from apps.jobs.models import Job
from apps.reviews.schemas import sections_content_hash
from apps.reviews.validation import evidence_snapshot, revision_blocks, snapshot_hash


def require_revision_integrity(revision: Any, expected_hash: str) -> None:
    try:
        actual = sections_content_hash(revision.sections)
    except (AttributeError, TypeError, ValueError) as exc:
        raise api_error("STRUCTURAL_VALIDATION_FAILED", "正文内容已损坏") from exc
    if revision.content_hash != expected_hash or actual != expected_hash:
        raise api_error("STRUCTURAL_VALIDATION_FAILED", "正文哈希与冻结版本不符，请重新保存并核验")


def require_completed_report(revision: Any, report: Any) -> None:
    if report is None or report.revision_id != revision.id or report.content_hash != revision.content_hash:
        raise api_error("VALIDATION_REQUIRED", "需要同一正文版本的完整核验报告")
    job = (
        Job.objects.filter(resource_type="validation_report", resource_id=report.id)
        .order_by("-attempt")
        .first()
    )
    if job is None or job.status != "succeeded" or report.result not in ("passed", "needs_review"):
        raise api_error("VALIDATION_REQUIRED", "核验任务尚未成功完成")
    if report.config_snapshot != job.frozen_input.get("config_snapshot") or snapshot_hash(
        evidence_snapshot(revision)
    ) != report.config_snapshot.get("evidence_hash"):
        raise api_error("VALIDATION_REQUIRED", "报告使用的证据或配置已改变，需要重新核验")
    if report.structure_passed is not True:
        raise api_error("STRUCTURAL_VALIDATION_FAILED")
    expected = [b["block_id"] for b in revision_blocks(revision)]
    actual = [b.get("block_id") for b in (report.blocks or [])]
    if (
        len(actual) != len(expected)
        or set(actual) != set(expected)
        or len(actual) != len(set(actual))
        or snapshot_hash(report_snapshot(report)) != (job.result or {}).get("report_hash")
    ):
        raise api_error("VALIDATION_REQUIRED", "报告内容不完整或已改变，需要重新核验")


def report_snapshot(report: Any) -> dict:
    return {
        "id": str(report.id),
        "revision_id": str(report.revision_id),
        "content_hash": report.content_hash,
        "result": report.result,
        "structure_passed": report.structure_passed,
        "blocks": report.blocks,
    }


def export_warnings(revision: Any, report: Any) -> list[str]:
    warnings = []
    if report.result == "needs_review":
        warnings.append("核验结果为 needs_review，正文存在待审问题")
    if revision.corpus_revision != revision.review.project.corpus_revision:
        warnings.append("Run 语料已过时，导出使用历史语料快照")
    if revision.analysis_revision != revision.run.analysis_revision:
        warnings.append("分析已过时，正文仍基于历史分析版本")
    if revision.review.revisions.filter(revision_number__gt=revision.revision_number).exists():
        warnings.append("导出的正文不是最新版本")
    return warnings


def require_acknowledged(warnings: list[str], acknowledged: bool, *, frozen: list[str] | None = None) -> None:
    if warnings and (not acknowledged or frozen is not None and not set(warnings) <= set(frozen)):
        raise api_error("WARNINGS_NOT_ACKNOWLEDGED", details={"warnings": warnings})
