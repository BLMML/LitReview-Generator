"""Run 与检索测试（docs/mvp/01 第 9 节）。

**必须覆盖的行为**（实现 docs/mvp/05 阶段 C 后逐条打开）：
- Run 冻结与 stale 阻断；
- 任一论文失败使 Run 失败（不得只把成功论文汇总后标 ready）；
- 多主题归属与争议条件对齐；
- 重排不得引入候选外 evidence ID；
- 检索限制在当前 Run 内。

并发相关结论必须用真实 PostgreSQL 事务与独立连接验证，不能用 SQLite 顶替。
"""

from __future__ import annotations

import pytest

PENDING = "MVP 骨架阶段：analysis / retrieval 服务尚未实现，见 docs/mvp/05 阶段 C"

pytestmark = [pytest.mark.django_db, pytest.mark.skip(reason=PENDING)]


def test_run_freezes_research_question_and_corpus():
    raise AssertionError("待实现：Run 冻结 research_question / corpus_revision，不随后续编辑变化")


def test_run_requires_at_least_two_papers():
    raise AssertionError("待实现：少于 2 篇纳入文献返回 422 INSUFFICIENT_PAPERS")


def test_any_run_paper_failure_fails_the_run():
    raise AssertionError("待实现：任一 RunPaper 失败则 Run.failed，成功阶段保留")


def test_stale_run_blocks_new_retrieval():
    raise AssertionError("待实现：语料变化后旧 Run 可读但新检索返回 409 STALE_RUN")


def test_topics_allow_multi_membership():
    raise AssertionError("待实现：一文多主题合法，成员至少 1 篇且都属于本 Run")


def test_dispute_sides_must_differ():
    raise AssertionError("待实现：左右论文相同时被数据库约束拒绝")


def test_dispute_review_bumps_analysis_revision():
    raise AssertionError("待实现：人工复核递增 Dispute.version 与 Run.analysis_revision")


def test_dispute_rejected_requires_note():
    raise AssertionError("待实现：review_status=rejected 必须带非空说明")


def test_rerank_cannot_introduce_new_evidence():
    raise AssertionError("待实现：重排结果包含候选外 ID 时返回 422 INVALID_EVIDENCE")


def test_evidence_reference_across_runs_is_rejected():
    raise AssertionError("待实现：跨 Run 引用证据一律拒绝")


def test_year_counts_use_frozen_snapshot():
    raise AssertionError("待实现：year_counts / unknown_year_count 只读 RunPaper 快照")
