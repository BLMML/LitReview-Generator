"""任务、幂等与预算测试（docs/mvp/01 第 9 节）。

**必须覆盖的行为**（实现 docs/mvp/05 阶段 B/E 后逐条打开）：
- 两个同时提交仅一个任务成功占用项目，另一个 409 PROJECT_BUSY；
- 幂等重放优先于 busy 判断；同键不同体返回 409 IDEMPOTENCY_CONFLICT；
- 协作取消（queued 直接 cancelled；running → cancelling，不立即显示零费用）；
- 重复投递不重复执行；
- 租约过期 / 旧 worker 禁止写入（fencing）；
- 预算预留与结算；失败和未知用量的恢复。

并发结论必须使用真实 PostgreSQL 事务与独立连接验证。
"""

from __future__ import annotations

import pytest

PENDING = "MVP 骨架阶段：任务执行链尚未实现，见 docs/mvp/05 阶段 B/E"

pytestmark = [pytest.mark.django_db, pytest.mark.skip(reason=PENDING)]


def test_second_active_job_per_project_is_rejected():
    raise AssertionError("待实现：部分唯一索引 + PROJECT_BUSY 保证每项目一个活跃任务")


def test_idempotent_replay_precedes_busy_check():
    raise AssertionError("待实现：同键同体在 busy 时仍返回原受理响应")


def test_same_key_different_body_is_conflict():
    raise AssertionError("待实现：不同请求哈希返回 409 IDEMPOTENCY_CONFLICT")


def test_missing_idempotency_key_is_400():
    raise AssertionError("待实现：标记 K 的请求缺少 Idempotency-Key 返回 400")


def test_queued_job_cancels_immediately():
    raise AssertionError("待实现：queued 取消直接进入 cancelled")


def test_running_job_cancel_is_cooperative():
    raise AssertionError("待实现：running 取消进入 cancelling，不退费、不立即终态")


def test_cancel_on_terminal_job_keeps_status():
    raise AssertionError("待实现：已终态取消返回原状态，不倒退")


def test_duplicate_delivery_does_not_reexecute():
    raise AssertionError("待实现：有效租约存在时重复队列消息被丢弃")


def test_stale_worker_cannot_publish():
    raise AssertionError("待实现：lease_token 不匹配时终态提交被拒绝")


def test_retry_only_for_latest_failed_attempt():
    raise AssertionError("待实现：已有更新尝试或成功结果时返回 INVALID_JOB_STATE")


def test_budget_reservation_blocks_over_spend():
    raise AssertionError("待实现：预留超出项目预算时拒绝真实调用")


def test_unsettled_calls_keep_reservation():
    raise AssertionError("待实现：未知用量保留 unsettled_call_count，不静默释放预算")
