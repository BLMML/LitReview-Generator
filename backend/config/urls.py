"""集中挂载 docs/mvp/00 与 docs/mvp/03 定义的全部路由。

约定：
- 根前缀 `/api/v1`，所有路由无尾斜杠（settings.APPEND_SLASH = False）；
- 只引用各 app 的 API 入口，不在这里写视图逻辑；
- 不额外开放通用模型 CRUD。

操作数应与 docs/mvp/contracts/openapi.json 的 45 个 operation 一一对应。
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

API_V1 = "api/v1/"

# 路由未命中与未捕获异常也走统一错误 envelope，绝不返回 HTML 错误页
handler404 = "apps.common.http.json_404"
handler500 = "apps.common.http.json_500"

urlpatterns = [
    path("admin/", admin.site.urls),
    # 账号与项目
    path(API_V1, include("apps.accounts.api")),
    path(API_V1, include("apps.projects.api")),
    # 文献与导入
    path(API_V1, include("apps.literature.api")),
    # 分析、主题、争议、证据
    path(API_V1, include("apps.analysis.api")),
    # 检索
    path(API_V1, include("apps.retrieval.api")),
    # 综述生成、版本、核验、导出
    path(API_V1, include("apps.reviews.api")),
    # 任务控制
    path(API_V1, include("apps.jobs.api")),
]
