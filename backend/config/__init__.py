"""暴露 Celery app，作为 Django / worker 共享任务配置的标准加载入口。

本模块只做导入转发：不允许在这里执行网络请求、数据库查询或任何业务逻辑。
"""

from __future__ import annotations

from .celery import app as celery_app

__all__ = ("celery_app",)
