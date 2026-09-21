# 后端镜像：Web / worker / 调度共用同一个镜像
#
# 职责（docs/mvp/01 第 8.2 节）：
# - 按锁文件安装依赖；
# - 非 root 运行；
# - 运行产物写私有卷；
# - 不在镜像中烘焙凭据或论文。

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pypdfium2 / psycopg 运行需要的系统库；不安装编译工具链
RUN apt-get update \
 && apt-get install --no-install-recommends -y libpq5 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层单独缓存：只有锁文件变化才重装。
# requirements.lock 是**部署完整集**，已包含 Linux 专用的 gunicorn
# （对应 pyproject.toml 的 prod extra）。下面的 import 断言让镜像在
# 锁文件缺 gunicorn 时立刻构建失败，而不是等到容器启动才报 command not found。
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
 && python -c "import gunicorn; print('gunicorn', gunicorn.__version__)"

# 源码层
COPY backend/ ./backend/
COPY docs/mvp/contracts/ ./backend/contracts/
# pyproject.toml 带着 pytest 配置（pythonpath / DJANGO_SETTINGS_MODULE）。
# 镜像里要能跑测试套件，才能在真实 PostgreSQL 上验证并发约束，
# 而不是只靠 SQLite 的结论。
COPY pyproject.toml ./

# 私有存储卷挂载点；PRIVATE_STORAGE_ROOT 由编排注入
RUN mkdir -p /var/lib/review-mvp/private \
 && useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app /var/lib/review-mvp
USER appuser

WORKDIR /app/backend
ENV DJANGO_SETTINGS_MODULE=config.settings \
    APP_ENV=production \
    PYTHONPATH=/app/backend

EXPOSE 8000

# Web 的默认命令；worker / beat 在编排里覆盖 command
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
