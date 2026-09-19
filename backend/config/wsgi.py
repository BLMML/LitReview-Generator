"""正式部署的 WSGI 入口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 让 apps.* 可导入；容器内工作目录可能不是 backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
