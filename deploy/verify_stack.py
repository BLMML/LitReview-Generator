"""在装有 Docker 的机器上验证整套运行环境。

回答一个 SQLite 回答不了的问题：**PostgreSQL、Redis、worker、beat 真的能用吗。**
本机没有 Docker 时这个脚本跑不了，配置层的静态检查见 .data/tools/validate_deploy.py。

用法（在仓库根目录）：

    python deploy/verify_stack.py              # 构建、启动、逐项验证，最后保留运行中的栈
    python deploy/verify_stack.py --down       # 验证完自动 docker compose down
    python deploy/verify_stack.py --skip-build # 复用已有镜像

脚本只做验证，不改动仓库文件。它会真实创建演示用户与一个测试项目。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO_USERS = (("alice", "demo-pass-123"), ("bob", "demo-pass-123"))

FAILURES: list[str] = []
CHECKS = 0


def say(message: str = "") -> None:
    print(message, flush=True)


def check(label: str, ok: bool, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    suffix = f"  ← {detail}" if not ok and detail else ""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}", flush=True)
    if not ok:
        FAILURES.append(label)
    return ok


def run(args: list[str], *, timeout: int = 900, check_rc: bool = False) -> subprocess.CompletedProcess:
    # 参数全部由本脚本拼出（docker / docker compose / venv python），不含外部输入
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # 超时也返回一个 CompletedProcess，让上层按失败处理而不是崩掉整个脚本
        return subprocess.CompletedProcess(
            args, returncode=124, stdout=exc.stdout or "", stderr=f"命令超时（{timeout}s）"
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr=str(exc))
    if check_rc and proc.returncode != 0:
        say(f"    命令失败：{' '.join(args)}")
        say((proc.stdout or "")[-2000:])
        say((proc.stderr or "")[-2000:])
    return proc


def compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return run(["docker", "compose", *args], **kwargs)


def exec_in(service: str, *command: str, **kwargs) -> subprocess.CompletedProcess:
    return compose("exec", "-T", service, *command, **kwargs)


def section(title: str) -> None:
    say()
    say("=" * 72)
    say(title)
    say("=" * 72)


def wait_for_health(services: tuple[str, ...], timeout_seconds: int = 180) -> dict[str, str]:
    """轮询 docker compose ps，直到指定服务都 healthy 或超时。"""
    deadline = time.time() + timeout_seconds
    last: dict[str, str] = {}
    while time.time() < deadline:
        proc = compose("ps", "--format", "{{.Service}}|{{.State}}|{{.Health}}")
        last = {}
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) == 3:
                last[parts[0]] = f"{parts[1]}/{parts[2]}"
        if all(last.get(name, "").startswith("running/healthy") for name in services):
            return last
        time.sleep(3)
    return last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--down", action="store_true", help="验证结束后 docker compose down")
    parser.add_argument("--skip-build", action="store_true", help="跳过镜像构建")
    args = parser.parse_args()

    section("0. 前置检查")
    if shutil.which("docker") is None:
        say("  找不到 docker 命令。请先安装 Docker Desktop 并确认它在 PATH 上。")
        return 2
    version = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if version.returncode != 0:
        say("  docker 命令存在，但连不上守护进程。请先启动 Docker Desktop。")
        say((version.stderr or "").strip()[:400])
        return 2
    say(f"  Docker 服务端版本：{version.stdout.strip()}")
    if not (ROOT / ".env").is_file():
        say("  缺少 .env。请先复制 .env.example 为 .env 并填写 POSTGRES_PASSWORD。")
        return 2
    say("  .env 存在")

    section("1. Compose 配置校验（docker compose config）")
    proc = compose("config", "--quiet")
    check("compose.yaml 通过 Compose 自身校验", proc.returncode == 0, (proc.stderr or "")[:500])
    if proc.returncode != 0:
        return 1

    # 用 Compose 自己渲染出的最终结构来验证锚点层级：
    # 这是权威结果，比读源文件猜锚点语义可靠。
    # 用 --format json 以避开对 PyYAML 的依赖（它不属于项目运行依赖）。
    rendered = compose("config", "--format", "json")
    rendered_services: dict = {}
    if rendered.returncode != 0:
        say("  docker compose 不支持 --format json（Compose 版本偏旧），跳过结构化校验。")
        say("  建议升级 Docker Desktop；那部分仍可用 .data/tools/validate_deploy.py 覆盖。")
    else:
        try:
            rendered_services = (json.loads(rendered.stdout or "{}") or {}).get("services", {})
        except json.JSONDecodeError as exc:
            say(f"  无法解析 docker compose config 的输出：{exc}")

    for name in ("web", "worker", "beat"):
        if not rendered_services:
            break
        env = (rendered_services.get(name) or {}).get("environment") or {}
        # 只比较键名，绝不打印值：渲染后的 DATABASE_URL 含真实密码
        bad = sorted({"env_file", "environment"} & set(env))
        check(
            f"渲染后 {name}.environment 是扁平键值对",
            isinstance(env, dict) and not bad,
            f"混入了 {bad}",
        )
        check(
            f"渲染后 {name} 注入的是容器内 DATABASE_URL",
            "@db:5432/" in str(env.get("DATABASE_URL", "")),
            "DATABASE_URL 未指向 db 服务",
        )
        check(f"渲染后 {name} 的 APP_ENV=production", env.get("APP_ENV") == "production")

    section("2. 构建镜像")
    if args.skip_build:
        say("  已跳过（--skip-build）")
    else:
        proc = compose("build", timeout=1800)
        check("镜像构建成功（含 Dockerfile 里的 gunicorn 断言）", proc.returncode == 0,
              (proc.stderr or "")[-500:])
        if proc.returncode != 0:
            return 1

    section("3. 启动服务")
    proc = compose("up", "-d", timeout=600)
    check("docker compose up 成功", proc.returncode == 0, (proc.stderr or "")[-500:])
    if proc.returncode != 0:
        return 1

    section("4. 健康检查")
    health = wait_for_health(("db", "redis"))
    for name in ("db", "redis", "web", "worker", "beat"):
        state = health.get(name, "<未出现>")
        check(f"{name} 处于运行状态（{state}）", state.startswith("running"), state)

    section("5. Django 配置与数据库")
    proc = exec_in("web", "python", "manage.py", "check")
    check("manage.py check 通过", proc.returncode == 0, (proc.stderr or "")[-400:])

    proc = exec_in(
        "web",
        "python",
        "manage.py",
        "shell",
        "-c",
        "from django.conf import settings; from django.db import connection; "
        "print('VENDOR=' + connection.vendor); print('DEBUG=' + str(settings.DEBUG))",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    check("实际连接的是 PostgreSQL（不是 SQLite）", "VENDOR=postgresql" in output, output[-300:])
    check("容器内 DEBUG=False", "DEBUG=False" in output, output[-300:])

    proc = exec_in("web", "python", "manage.py", "migrate", "--noinput", timeout=600)
    check("迁移可在 PostgreSQL 上从空库应用", proc.returncode == 0, (proc.stderr or "")[-500:])

    proc = exec_in("web", "python", "manage.py", "makemigrations", "--check", "--dry-run")
    check("模型与迁移文件一致（无未生成的变更）", proc.returncode == 0, (proc.stdout or "")[-300:])

    section("6. Redis 与 Celery")
    proc = exec_in("web", "python", "-c",
                   "import redis, os; r = redis.Redis.from_url(os.environ['REDIS_URL']); "
                   "print('PING=' + str(r.ping()))")
    check("能从 web 容器 ping 通 Redis", "PING=True" in ((proc.stdout or "") + (proc.stderr or "")),
          (proc.stderr or "")[-300:])

    logs = compose("logs", "worker", "--tail", "80").stdout or ""
    check("worker 已连上 broker 并就绪", "ready" in logs.lower(), logs[-400:])
    beat_logs = compose("logs", "beat", "--tail", "80").stdout or ""
    check("beat 调度器已启动", "beat" in beat_logs.lower() and "starting" in beat_logs.lower(),
          beat_logs[-400:])

    # 真正投递一个任务，确认「web 入队 → Redis → worker 执行」这条链路通了
    proc = exec_in("web", "python", "-c",
                   "from apps.jobs.tasks import reconcile_jobs; "
                   "r = reconcile_jobs.delay(); print('TASK_ID=' + str(r.id))")
    task_out = (proc.stdout or "") + (proc.stderr or "")
    check("能从 web 容器投递任务到队列", "TASK_ID=" in task_out, task_out[-300:])
    if "TASK_ID=" in task_out:
        task_id = task_out.split("TASK_ID=")[1].split()[0].strip()
        seen = False
        for _ in range(20):
            time.sleep(1)
            if task_id[:8] in (compose("logs", "worker", "--tail", "120").stdout or ""):
                seen = True
                break
        check("worker 真的执行了该任务（消息链路通）", seen, f"任务 {task_id} 未出现在 worker 日志中")

    section("7. 在真实 PostgreSQL 上跑测试套件")
    say("  这是 SQLite 无法替代的部分：行锁、部分唯一索引、并发受理。")
    proc = exec_in("web", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", timeout=1800)
    output = (proc.stdout or "") + (proc.stderr or "")
    say("  " + "\n  ".join(output.strip().splitlines()[-12:]))
    check("容器内测试套件通过", proc.returncode == 0, output[-600:])
    check("测试确实执行了（不是全部跳过）", "passed" in output, output[-300:])

    section("8. HTTP 冒烟与业务走查（从宿主机发起）")
    for username, password in DEMO_USERS:
        exec_in("web", "python", "manage.py", "shell", "-c",
                "from django.contrib.auth import get_user_model as g; U = g(); "
                f"u, _ = U.objects.get_or_create(username='{username}'); "
                f"u.set_password('{password}'); u.save()")
    say(f"  已创建演示用户：{', '.join(name for name, _ in DEMO_USERS)}")

    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        venv_python = ROOT / ".venv" / "bin" / "python"
    interpreter = str(venv_python) if venv_python.is_file() else sys.executable

    for script, label in (
        ("backend/tests/smoke_http.py", "端到端冒烟（协议层）"),
        ("backend/tests/walkthrough_http.py", "业务闭环走查（登录 → 建项目 → 乐观锁 → 越权）"),
    ):
        proc = run([interpreter, str(ROOT / script)], timeout=300)
        output = (proc.stdout or "") + (proc.stderr or "")
        say()
        say(f"  ── {label} ──")
        say("  " + "\n  ".join(output.strip().splitlines()[-6:]))
        check(label, proc.returncode == 0, output[-400:])

    section("结果")
    say(f"  共 {CHECKS} 项检查，失败 {len(FAILURES)} 项")
    for item in FAILURES:
        say(f"    - {item}")

    if args.down:
        say()
        say("  正在 docker compose down ...")
        compose("down", timeout=300)
    else:
        say()
        say("  栈仍在运行。查看日志：docker compose logs -f worker")
        say("  停止：docker compose down")

    if FAILURES:
        return 1
    say()
    say("  全部通过：PostgreSQL、Redis、worker、beat、HTTP 链路均已在真实容器中验证。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已中断。")
        sys.exit(130)
