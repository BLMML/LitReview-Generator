"""M11 手工 HTTP 走查：已保存正文 → 核验 → 警示确认 → 私有 Word 下载。

须启动 Django 和 Celery worker；用 --review 指定已有 Review，--revision 可选。
--acknowledge-warnings 表示确认本次报告和历史版本的警示。默认不自动确认。
复用 Session 登录与 CSRF；DEMO_USER、DEMO_PASSWORD、DEMO_BASE 可覆盖。
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path

from docx import Document
from walkthrough_review_http import BASE, USER, Session, login, poll_job


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=uuid.UUID, required=True)
    parser.add_argument("--revision", type=uuid.UUID)
    parser.add_argument("--acknowledge-warnings", action="store_true")
    args = parser.parse_args()
    session = Session()
    if not login(session, USER):
        print("登录失败，请检查 DEMO_USER、DEMO_PASSWORD 和 Django 服务。")
        return 1
    status, body = session.call(f"/reviews/{args.review}")
    if status != 200:
        print("找不到该账号拥有的 Review。")
        return 1
    latest = body["data"]["latest_revision"]
    revision_id = str(args.revision) if args.revision else (latest or {}).get("id")
    if revision_id is None:
        print("该 Review 尚无成功生成或保存的正文，请先完成 M10 生成。")
        return 1
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    status, body = session.call(f"/reviews/{args.review}/validations", "POST",
                                {"revision_id": revision_id}, extra_headers=headers)
    if status != 202:
        print("核验受理失败：", body)
        return 1
    accepted = body["data"]
    report_id = accepted["resource_id"]
    job = poll_job(session, accepted["job_id"])
    if job.get("status") != "succeeded":
        print("核验未成功完成：", job.get("status"), job.get("error"))
        return 1
    status, body = session.call(f"/validations/{report_id}")
    if status != 200:
        print("读取核验报告失败。")
        return 1
    report = body["data"]
    print(f"核验报告 {report_id}：{report['result']}；绑定正文 {report['revision_id']}")
    if report["revision_id"] != revision_id or report["structure_passed"] is not True:
        print("正文结构或来源检查未通过，请修复并重新保存、核验。")
        return 1
    payload = {"revision_id": revision_id, "validation_id": report_id,
               "acknowledge_warnings": args.acknowledge_warnings}
    status, body = session.call(f"/reviews/{args.review}/exports", "POST", payload,
                                extra_headers={"Idempotency-Key": str(uuid.uuid4())})
    if status != 202:
        print("导出受理失败：", body)
        return 1
    accepted = body["data"]
    job = poll_job(session, accepted["job_id"])
    if job.get("status") != "succeeded":
        print("导出未成功完成：", job.get("status"), job.get("error"))
        return 1
    export_id = accepted["resource_id"]
    request = urllib.request.Request(f"{BASE}/exports/{export_id}/file")
    status, content = session._send(request)
    if status != 200:
        print("授权下载失败：", status)
        return 1
    doc = Document(BytesIO(content))
    banner = "AI 辅助生成草稿，需人工审阅"
    if banner not in "\n".join(p.text for p in doc.paragraphs):
        print("Word 缺少草稿标记。")
        return 1
    output = Path(__file__).resolve().parents[2] / ".data" / "m11-exports" / f"{export_id}.docx"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    print("Word 已通过打开检查，保存到：", output)
    print("核验、导出历史可以分别从 Review 的 validations、exports 列表查询。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
