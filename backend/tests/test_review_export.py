"""综述生成、编辑、核验与导出测试（docs/mvp/01 第 9 节）。

**必须覆盖的行为**（实现 docs/mvp/05 阶段 D 后逐条打开）：
- 五节结构与顺序、每节至少一个 block；
- 论断必须有证据（claim 至少一个当前 Run 证据）；
- 跨 Run 引用拒绝；
- 编辑版本冲突（base_revision_number 不匹配返回 409 VERSION_CONFLICT）；
- 一份报告只绑定一个 revision，旧报告不能用于新正文；
- 无匹配且成功的报告时禁止导出；
- needs_review、分析过时或非最新正文导出必须 acknowledge_warnings=true；
- 参考文献只来源于冻结元数据。
"""

from __future__ import annotations

import pytest

PENDING = "MVP 骨架阶段：reviews 服务尚未实现，见 docs/mvp/05 阶段 D"

pytestmark = [pytest.mark.django_db, pytest.mark.skip(reason=PENDING)]


def test_sections_must_have_five_ordered_keys():
    raise AssertionError("待实现：background/directions/representative_work/disagreements/trends")


def test_each_section_needs_at_least_one_block():
    raise AssertionError("待实现：空节被结构校验拒绝")


def test_block_limits_are_enforced():
    raise AssertionError("待实现：≤100 block、每块 1—600 字符、每块 ≤20 证据")


def test_block_ids_must_be_unique_across_review():
    raise AssertionError("待实现：重复 block_id 被拒绝")


def test_claim_requires_evidence_from_current_run():
    raise AssertionError("待实现：claim 无证据或引用跨 Run 证据时被拒绝")


def test_html_is_rejected_in_block_text():
    raise AssertionError("待实现：正文含 HTML 被拒绝")


def test_save_revision_conflict_is_409():
    raise AssertionError("待实现：base_revision_number 落后时返回 409 VERSION_CONFLICT")


def test_new_revision_inherits_analysis_revision():
    raise AssertionError("待实现：人工改字不消除分析过时状态")


def test_report_binds_exactly_one_revision():
    raise AssertionError("待实现：已完成报告不能指向新正文继续复用")


def test_export_without_matching_report_is_rejected():
    raise AssertionError("待实现：validation 与 revision 不匹配时拒绝导出")


def test_export_with_unacknowledged_warnings_is_409():
    raise AssertionError("待实现：needs_review / 过时 / 非最新正文需 acknowledge_warnings=true")


def test_bibliography_uses_frozen_metadata_only():
    raise AssertionError("待实现：参考文献来自 RunPaper 快照，不抓取外部指标")


def test_export_file_is_draft_marked():
    raise AssertionError("待实现：所有导出标明 AI 辅助生成草稿")
