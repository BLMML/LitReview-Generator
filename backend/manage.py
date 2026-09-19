#!/usr/bin/env python
"""Django 管理入口。

职责见 docs/mvp/01_文件架构与职责.md 第 3.1 节：
数据库迁移、管理员建号与开发检查都从这里进入。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    # backend/ 必须在内，apps.* 才能导入
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - 环境未装依赖时的提示
        raise ImportError(
            "无法导入 Django。请确认已激活虚拟环境并安装依赖（见 README 首次启动）。"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
