"""文献与解析测试（docs/mvp/01 第 9 节）。

**必须覆盖的行为**（实现 docs/mvp/05 阶段 B 后逐条打开）：
- PDF 大小 / 页数 / 加密 / 损坏；
- 同文件去重（同项目同 SHA256 只产生一个 Paper，且不改 metadata/included、
  不递增 corpus_revision）；
- arXiv 部分成功与重试（失败项保留账本，成功项不回滚，重试只补 pending/failed）；
- 页码与稳定证据（page_number 1 起始、ordinal 稳定、跨页不漂移）；
- 复杂页失败不补造（无法识别的页显式失败，不得凭模型补内容）。

骨架阶段这些服务尚未实现（见对应 services 的 SERVICE_UNAVAILABLE），
因此保留为显式 skip，避免把“没实现”伪装成“已通过”。
"""

from __future__ import annotations

import pytest

PENDING = "MVP 骨架阶段：literature / parsing 服务尚未实现，见 docs/mvp/05 阶段 B"

pytestmark = [pytest.mark.django_db, pytest.mark.skip(reason=PENDING)]


def test_pdf_size_limit_is_413():
    raise AssertionError("待实现：超过 25 MiB 返回 413 FILE_TOO_LARGE")


def test_pdf_page_limit_is_422():
    raise AssertionError("待实现：超过 50 页返回 422 PAGE_LIMIT_EXCEEDED")


def test_encrypted_pdf_is_422():
    raise AssertionError("待实现：加密 PDF 返回 422 UNSUPPORTED_PDF")


def test_broken_pdf_is_422():
    raise AssertionError("待实现：损坏 PDF 返回 422 UNSUPPORTED_PDF")


def test_same_file_upload_dedupes_within_project():
    raise AssertionError("待实现：同哈希重复上传返回既有 Paper，不新增、不递增语料版本")


def test_paper_limit_is_422():
    raise AssertionError("待实现：第 31 篇返回 422 PAPER_LIMIT_EXCEEDED")


def test_arxiv_partial_failure_keeps_successful_items():
    raise AssertionError("待实现：部分失败时 Job failed，成功项保留在 Job.result.items")


def test_arxiv_retry_only_fills_pending_and_failed():
    raise AssertionError("待实现：重试沿用已解析的具体版本，只补 pending/failed")


def test_evidence_page_numbers_start_at_one():
    raise AssertionError("待实现：页级证据 page_number 从 1 起且与原文一致")


def test_unrecognized_page_fails_explicitly():
    raise AssertionError("待实现：无法识别的复杂页显式失败，不补造内容")


def test_image_transcription_is_unverified_by_default():
    raise AssertionError("待实现：视觉转录 verification=image_unverified，不自动设为 human_verified")
