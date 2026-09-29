"""Django 配置。

职责（docs/mvp/01_文件架构与职责.md 第 3.1 节）：
读取并校验环境配置；注册 app、DRF、会话、CSRF、日志、数据库与任务参数；关闭自动补斜杠。
缺少必需配置时启动失败，且失败信息中不得出现任何密钥。

变量清单与含义见同目录第 8.1 节，模板见仓库根 .env.example。
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
REPO_ROOT = BASE_DIR.parent


class ConfigurationError(RuntimeError):
    """环境配置缺失或非法。消息中只允许出现变量名，不允许出现变量值。"""


# ── 环境变量读取 ──────────────────────────────────────────────
def _read_env_file() -> None:
    """从仓库根 .env 补全环境变量；已存在的进程环境变量优先。"""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


_read_env_file()


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if required:
            raise ConfigurationError(f"缺少必需的环境变量 {name}，请参考 .env.example 填写。")
        return default if default is not None else ""
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"环境变量 {name} 必须是布尔值（true/false）。")


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"环境变量 {name} 必须是整数。") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"环境变量 {name} 不得小于 {minimum}。")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"环境变量 {name} 不得大于 {maximum}。")
    return value


def env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        result = Decimal(raw)
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result
    except InvalidOperation as exc:
        raise ConfigurationError(f"环境变量 {name} 必须是十进制数字；金额统一用十进制定点。") from exc


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# ── 运行模式 ──────────────────────────────────────────────────
APP_ENV = env_str("APP_ENV", "development")
if APP_ENV not in {"development", "test", "production"}:
    raise ConfigurationError("APP_ENV 只能是 development / test / production。")

IS_PRODUCTION = APP_ENV == "production"
IS_TEST = APP_ENV == "test"

# 默认 mock：测试与开发绝不产生付费调用。
# real 必须显式启用；production 下 real 调用失败不得静默退回 mock（由 llm 模块保证）。
LLM_MODE = env_str("LLM_MODE", "mock")
if LLM_MODE not in {"mock", "real"}:
    raise ConfigurationError("LLM_MODE 只能是 mock / real。")
if IS_PRODUCTION and LLM_MODE == "mock":
    pass  # 允许：演示环境可以整体处于模拟模式

# ── 安全 ──────────────────────────────────────────────────────
DEBUG = env_bool("DJANGO_DEBUG", default=not IS_PRODUCTION)

_secret_key = env_str("DJANGO_SECRET_KEY")
if not _secret_key:
    if IS_PRODUCTION:
        raise ConfigurationError("production 必须显式提供 DJANGO_SECRET_KEY。")
    # 仅开发 / 测试允许临时密钥：进程重启后 session 失效，属于预期行为。
    from django.core.management.utils import get_random_secret_key

    _secret_key = get_random_secret_key()
SECRET_KEY = _secret_key
del _secret_key

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
if IS_PRODUCTION and "*" in ALLOWED_HOSTS:
    raise ConfigurationError("production 不允许用 * 作为 DJANGO_ALLOWED_HOSTS。")

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# ── 应用注册 ──────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # 领域 app：直接注册包路径，不在这一步创建空壳 AppConfig
    "apps.common",
    "apps.accounts",
    "apps.projects",
    "apps.literature",
    "apps.parsing",
    "apps.analysis",
    "apps.retrieval",
    "apps.reviews",
    "apps.jobs",
    "apps.llm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # request_id 注入：定义在 common/http.py，供日志与响应 meta 共用
    "apps.common.http.RequestIdMiddleware",
]

# 契约规定路由无尾斜杠，写请求也不靠重定向补齐地址
APPEND_SLASH = False

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ── 数据库 ────────────────────────────────────────────────────
def _postgres_from_url(url: str) -> dict[str, object]:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ConfigurationError("DATABASE_URL 目前只支持 postgresql:// 连接串。")
    options: dict[str, object] = {}
    query = dict(pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair)
    if query.get("sslmode"):
        options["sslmode"] = query["sslmode"]
    # 连接超时：数据库没起来时快速失败并给出明确错误，
    # 而不是让每个请求挂到操作系统的 TCP 超时（实测能到几分钟）。
    options.setdefault("connect_timeout", env_int("DJANGO_DB_CONNECT_TIMEOUT", 5, minimum=1))
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": env_int("DJANGO_DB_CONN_MAX_AGE", 60, minimum=0),
        "OPTIONS": options,
    }


_DATABASE_URL = env_str("DATABASE_URL")
if _DATABASE_URL:
    DATABASES = {"default": _postgres_from_url(_DATABASE_URL)}
elif IS_PRODUCTION:
    raise ConfigurationError("production 必须提供 DATABASE_URL（PostgreSQL）。")
else:
    # 仅本机开发回退：没有 PostgreSQL 时也能 manage.py check / 冒烟。
    # 并发约束、行锁与部分唯一索引必须用真实 PostgreSQL 验证，不能用 SQLite 结论代替。
    _fallback_dir = REPO_ROOT / ".data"
    _fallback_dir.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(_fallback_dir / "db.sqlite3"),
        }
    }
    if "test" not in sys.argv and "check" not in sys.argv:
        print(
            "[config] 未设置 DATABASE_URL，已回退到 SQLite 开发库。"
            "MVP 基线与集成测试要求 PostgreSQL，见 docs/mvp/00 第 2 节。",
            file=sys.stderr,
        )

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── 认证与会话 ────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# 前端从 GET /auth/csrf 读取 token；cookie 仍设为可被同源脚本读取，便于调试
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"

# Secure cookie 默认跟随 APP_ENV：production → true，development / test → false。
# 未接 TLS 的内部试用阶段必须显式关掉：纯 HTTP 下 Secure cookie 不会被客户端回传，
# 登录会直接失败。见 .env.example 与 compose.yaml 的 DJANGO_SECURE_COOKIES。
SECURE_COOKIES = env_bool("DJANGO_SECURE_COOKIES", default=IS_PRODUCTION)
SESSION_COOKIE_SECURE = SECURE_COOKIES
CSRF_COOKIE_SECURE = SECURE_COOKIES

if IS_PRODUCTION:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

# ── DRF ───────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.common.auth.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["apps.common.auth.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "EXCEPTION_HANDLER": "apps.common.http.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "apps.common.http.EnvelopePagination",
    "PAGE_SIZE": 20,
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "DATETIME_FORMAT": "iso-8601",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# ── 国际化与时间 ──────────────────────────────────────────────
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = REPO_ROOT / ".data" / "static"
MEDIA_URL = ""  # 私有文件不通过公开 MEDIA_URL 暴露
MEDIA_ROOT = REPO_ROOT / ".private" / "media-never-served"

# ── 私有存储 ──────────────────────────────────────────────────
_private_root = env_str("PRIVATE_STORAGE_ROOT")
if not _private_root:
    if IS_PRODUCTION:
        raise ConfigurationError("production 必须提供 PRIVATE_STORAGE_ROOT（Web 与 worker 共享卷）。")
    _private_root = str(REPO_ROOT / ".private" / "storage")
PRIVATE_STORAGE_ROOT = Path(_private_root).resolve()

# ── MVP 范围参数（集中定义，页面与 worker 不得各写一份数值）────
MVP_LIMITS = {
    "PDF_MAX_BYTES": 25 * 1024 * 1024,  # 25 MiB
    "PDF_MAX_PAGES": 50,
    "PAPERS_PER_PROJECT_MAX": 30,  # 含取消纳入的
    "PAPERS_PER_PROJECT_MIN": 2,  # 创建 Run 的下限
    "ARXIV_IDS_MIN": 1,
    "ARXIV_IDS_MAX": 10,
    "ARXIV_MAX_REDIRECTS": 3,
    "ARXIV_DOWNLOAD_TIMEOUT_SECONDS": 60,
    "ARXIV_MIN_INTERVAL_SECONDS": 3,  # 请求频率下限
    "PROJECT_TITLE_MAX": 120,
    "RESEARCH_QUESTION_MIN": 5,
    "RESEARCH_QUESTION_MAX": 2000,
    "PAPER_TITLE_MAX": 500,
    "PAPER_YEAR_MIN": 1000,
    "RETRIEVAL_QUERY_MIN": 2,
    "RETRIEVAL_QUERY_MAX": 2000,
    "RETRIEVAL_CANDIDATE_LIMIT_DEFAULT": 20,
    "RETRIEVAL_CANDIDATE_LIMIT_MIN": 5,
    "RETRIEVAL_CANDIDATE_LIMIT_MAX": 50,
    "RETRIEVAL_TOP_K_DEFAULT": 5,
    "RETRIEVAL_TOP_K_MIN": 1,
    "RETRIEVAL_TOP_K_MAX": 10,
    "REVIEW_TARGET_CHARS_DEFAULT": 3000,
    "REVIEW_TARGET_CHARS_MIN": 2000,
    "REVIEW_TARGET_CHARS_MAX": 4000,
    "REVIEW_TITLE_MAX": 120,
    "REVIEW_MAX_BLOCKS": 100,
    "BLOCK_TEXT_MIN": 1,
    "BLOCK_TEXT_MAX": 600,
    "BLOCK_MAX_EVIDENCE_IDS": 20,
    "PAGE_SIZE_DEFAULT": 20,
    "PAGE_SIZE_MAX": 100,
    "IDEMPOTENCY_RETENTION_DAYS": 7,
}

OUTPUT_LANGUAGE_DEFAULT = "zh-CN"
OUTPUT_LANGUAGES_ALLOWED = ("zh-CN",)
REVIEW_STYLE_DEFAULT = "standard"
REVIEW_STYLES_ALLOWED = ("standard",)

# ── 后台任务与租约 ────────────────────────────────────────────
REDIS_URL = env_str("REDIS_URL", "redis://127.0.0.1:6379/0")

JOB_HEARTBEAT_SECONDS = env_int("JOB_HEARTBEAT_SECONDS", 10, minimum=1)
JOB_LEASE_SECONDS = env_int("JOB_LEASE_SECONDS", 60, minimum=2)
if JOB_LEASE_SECONDS <= JOB_HEARTBEAT_SECONDS:
    raise ConfigurationError("JOB_LEASE_SECONDS 必须大于 JOB_HEARTBEAT_SECONDS。")

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None  # 任务真实状态只存数据库，Redis 只做队列
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# 恢复扫描：补投 queued、处理失去执行资格的任务；清理仅针对可确认未被引用的产物
CELERY_BEAT_SCHEDULE = {
    "reconcile-jobs": {
        "task": "apps.jobs.tasks.reconcile_jobs",
        "schedule": 30.0,
    },
    "cleanup-expired-artifacts": {
        "task": "apps.jobs.tasks.cleanup_expired_artifacts",
        "schedule": 3600.0,
    },
}

# ── DeepSeek / 模型调用 ───────────────────────────────────────
DEEPSEEK_API_KEY = env_str("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = env_str("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_THINKING_MODE = env_str("DEEPSEEK_THINKING_MODE", "disabled")

LLM_REQUEST_TIMEOUT_SECONDS = env_int("LLM_REQUEST_TIMEOUT_SECONDS", 120, minimum=1)
LLM_MAX_ATTEMPTS = env_int("LLM_MAX_ATTEMPTS", 3, minimum=1, maximum=3)
LLM_MAX_INPUT_TOKENS = env_int("LLM_MAX_INPUT_TOKENS", 24000, minimum=1)
LLM_MAX_OUTPUT_TOKENS = env_int("LLM_MAX_OUTPUT_TOKENS", 4096, minimum=1)
LLM_MAX_CONCURRENCY = env_int("LLM_MAX_CONCURRENCY", 2, minimum=1)

LLM_PRICING = {
    band: {
        f"{component}_per_million_usd": env_decimal(
            f"LLM_PRICE_{component.upper()}_{band.upper()}_PER_MILLION_USD",
            Decimal("0"),
        )
        for component in ("input_cache_hit", "input_cache_miss", "output")
    }
    for band in ("peak", "off_peak")
}
LLM_PRICING_VERSION = env_str("LLM_PRICING_VERSION")
LLM_PRICING_CALENDAR_YEAR = env_int("LLM_PRICING_CALENDAR_YEAR", 0, minimum=0)
# 运维核验的 UTC 日期（YYYY-MM-DD），这些日期全天为谷时；不在线猜测节假日。
LLM_PRICING_OFF_PEAK_DATES_UTC = env_list("LLM_PRICING_OFF_PEAK_DATES_UTC")
from datetime import date  # noqa: E402

try:
    for _holiday in LLM_PRICING_OFF_PEAK_DATES_UTC:
        if date.fromisoformat(_holiday).year != LLM_PRICING_CALENDAR_YEAR:
            raise ValueError
except ValueError as exc:
    raise ConfigurationError("LLM_PRICING_OFF_PEAK_DATES_UTC 必须是日历年份内的 ISO 日期") from exc

# 项目默认预算。零或未配置不得被解释为无限预算。
PROJECT_DEFAULT_BUDGET_USD = env_decimal("PROJECT_DEFAULT_BUDGET_USD", Decimal("0"))

if LLM_MODE == "real":
    _missing = []
    if not DEEPSEEK_API_KEY:
        _missing.append("DEEPSEEK_API_KEY")
    if not LLM_PRICING_VERSION:
        _missing.append("LLM_PRICING_VERSION")
    if not LLM_PRICING_CALENDAR_YEAR or not LLM_PRICING_OFF_PEAK_DATES_UTC:
        _missing.append("LLM_PRICING_CALENDAR_YEAR / LLM_PRICING_OFF_PEAK_DATES_UTC")
    if PROJECT_DEFAULT_BUDGET_USD <= 0:
        _missing.append("PROJECT_DEFAULT_BUDGET_USD")
    for _component in LLM_PRICING["peak"]:
        if not 0 < LLM_PRICING["off_peak"][_component] <= LLM_PRICING["peak"][_component]:
            _missing.append(f"LLM_PRICE_{_component.upper()} 的峰谷价")
    if _missing:
        raise ConfigurationError("真实调用配置不完整：" + "、".join(_missing))

ARXIV_CONTACT = env_str("ARXIV_CONTACT")

# ── 日志 ──────────────────────────────────────────────────────
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        # 记录 request_id / job_id / stage，并在格式化前过滤认证头、Cookie 与密钥
        "request_context": {"()": "apps.common.http.RequestContextFilter"},
        "redact_secrets": {"()": "apps.common.http.SecretRedactionFilter"},
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s "
            "[request_id=%(request_id)s job_id=%(job_id)s stage=%(stage)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["request_context", "redact_secrets"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "django.security": {"level": "WARNING", "propagate": True},
        "apps": {"level": LOG_LEVEL, "propagate": True},
    },
}
