"""Celery 应用与 worker 配置。

职责（docs/mvp/01_文件架构与职责.md 第 3.1 节）：
定义 worker 配置、任务发现和定时恢复扫描；配置进程并发与保守预取。
Web 与 worker 必须使用相同任务名、序列化和队列设置。

注意：Redis 只是队列与临时协调，任务真实状态永远以数据库为准。
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("review_mvp")

# 配置全部来自 Django settings 的 CELERY_* 键，避免 worker 与 Web 各写一份
app.config_from_object("django.conf:settings", namespace="CELERY")

# 自动发现各 app 的 tasks.py（目前只有 apps.jobs.tasks）
app.autodiscover_tasks(
    [
        "apps.jobs",
        "apps.literature",
        "apps.analysis",
        "apps.retrieval",
        "apps.reviews",
    ]
)


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:  # pragma: no cover - 仅用于本地验证 worker 连通性
    """确认 worker 已加载配置与任务发现。"""
    return f"ok:{self.request.id}"
