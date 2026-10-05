"""Explicit local mock walkthrough through HTTP and a real Celery worker.

Creates a separate project for the specified owner. Session creation is the
only direct database write; all project, job and artifact changes use APIs.
Login/CSRF UI is independently covered by walkthrough_m12_browser.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path

import httpx
from docx import Document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--pdf", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun", action="store_true", help="Create a new Run in the same test project")
    args = parser.parse_args()
    assert len(args.pdf) == 2 and all(path.is_file() for path in args.pdf)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session
    from django.test import Client

    from apps.llm.models import ModelCall

    assert settings.LLM_MODE == "mock", "This walkthrough must never make paid calls."
    owner = get_user_model().objects.get(pk=args.owner_id, is_active=True)
    login = Client()
    login.force_login(owner)
    session_key = login.cookies[settings.SESSION_COOKIE_NAME].value
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"mode": "mock", "owner": owner.username, "jobs": [], "pdfs": []}
    report_path = args.output / "live-result.json"
    if args.resume:
        report = json.loads(report_path.read_text("utf-8"))
        assert report["owner"] == owner.username
    if args.rerun:
        assert args.resume
        report = {key: report[key] for key in ("mode", "owner", "pdfs", "project_id", "project_url")}
        report["jobs"] = []

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(base_url="http://127.0.0.1:8000/api/v1", timeout=60, trust_env=False) as client:
        client.cookies.set(settings.SESSION_COOKIE_NAME, session_key, domain="127.0.0.1", path="/")
        try:
            response = client.get("/auth/csrf")
            response.raise_for_status()
            token = response.json()["data"]["csrf_token"]
            client.headers.update({"X-CSRFToken": token, "Referer": "http://127.0.0.1:8000/"})

            def call(method, path, payload=None, *, expected=200, key=None, files=None):
                headers = {"Idempotency-Key": key or str(uuid.uuid4())}
                response = client.request(method, path, json=payload, files=files, headers=headers)
                if response.status_code != expected:
                    raise AssertionError(f"{method} {path}: {response.status_code} {response.text[:2000]}")
                return response.json()["data"]

            def wait(accepted):
                deadline = time.monotonic() + 180
                observations = []
                while time.monotonic() < deadline:
                    job = call("GET", f"/jobs/{accepted['job_id']}")
                    point = {"status": job["status"], "stage": job["stage"], "progress": job["progress"]}
                    if not observations or observations[-1] != point:
                        observations.append(point)
                    if job["status"] in ("succeeded", "failed", "cancelled"):
                        report["jobs"].append({
                            "id": job["id"], "kind": job["kind"], **point,
                            "error": job["error"], "observations": observations,
                        })
                        save()
                        assert job["status"] == "succeeded", job
                        print(f"{job['kind']}: succeeded ({job['stage']})", flush=True)
                        return job
                    time.sleep(0.5)
                raise TimeoutError(f"Job {accepted['job_id']} did not finish")

            if args.resume:
                project = call("GET", f"/projects/{report['project_id']}")
            else:
                project = call("POST", "/projects", {
                    "title": "社会治理与五社联动 全流程测试 2026-10-03",
                    "research_question": "社会工作与志愿服务在五社联动中如何协同促进基层社会治理？",
                }, expected=201)
            pid = project["id"]
            report["project_id"] = pid
            report["project_url"] = f"http://127.0.0.1:5173/projects/{pid}"
            save()
            for path in args.pdf:
                if args.resume:
                    continue
                content = path.read_bytes()
                paper = call("POST", f"/projects/{pid}/papers/upload", expected=201,
                             files={"file": (path.name, content, "application/pdf")})
                report["pdfs"].append({
                    "filename": path.name, "sha256": hashlib.sha256(content).hexdigest(),
                    "paper_id": paper["id"], "page_count": paper["page_count"],
                })
                save()
            assert len(call("GET", f"/projects/{pid}/papers")) == 2
            if report.get("run_id"):
                rid = report["run_id"]
                job = call("GET", f"/jobs/{report['jobs'][0]['id']}")
            else:
                accepted = call("POST", f"/projects/{pid}/runs", {}, expected=202)
                rid = accepted["resource_id"]
                report["run_id"] = rid
                job = wait(accepted)
            assert job["stage"] == "finalize"
            assert job["progress"] == {"completed": 2, "total": 2, "unit": "paper"}
            run = call("GET", f"/runs/{rid}")
            assert run["status"] == "ready"
            report["run_status"] = run["status"]
            for paper in report["pdfs"]:
                analysis = call("GET", f"/runs/{rid}/papers/{paper['paper_id']}/analysis")
                assert "模拟" in analysis["summary"]["value"]
                evidence_id = analysis["summary"]["evidence_ids"][0]
                evidence = call("GET", f"/runs/{rid}/evidence/{evidence_id}")
                assert evidence["paper_id"] == paper["paper_id"]
                original = client.get(f"/papers/{paper['paper_id']}/file")
                assert hashlib.sha256(original.content).hexdigest() == paper["sha256"]
            previous_retrievals = call("GET", f"/runs/{rid}/retrievals") if args.resume else []
            if previous_retrievals:
                retrieval = previous_retrievals[0]
            else:
                accepted = call("POST", f"/runs/{rid}/retrievals", {
                    "query": "社会工作 志愿服务 社区", "candidate_limit": 20, "top_k": 5,
                }, expected=202)
                wait(accepted)
                retrieval = call("GET", f"/retrievals/{accepted['resource_id']}")
            assert retrieval["results"]
            report["retrieval_results"] = len(retrieval["results"])
            if report.get("review_id"):
                review_id = report["review_id"]
            else:
                accepted = call("POST", f"/runs/{rid}/reviews", {
                    "title": "社会治理与五社联动流程测试草稿", "target_chars": 3000, "style": "standard",
                }, expected=202)
                review_id = accepted["resource_id"]
                report["review_id"] = review_id
                wait(accepted)
            report["review_id"] = review_id
            report["review_url"] = f"http://127.0.0.1:5173/reviews/{review_id}"
            review = call("GET", f"/reviews/{review_id}")
            revision = call("GET", f"/reviews/{review_id}/revisions/{review['latest_revision']['id']}")
            assert len(revision["sections"]) == 5
            if revision["revision_number"] == 1:
                revision["sections"][0]["blocks"][0]["text"] += " 人工编辑流程测试。"
                revision = call("POST", f"/reviews/{review_id}/revisions", {
                    "base_revision_number": 1, "sections": revision["sections"],
                }, expected=201)
            assert revision["revision_number"] == 2
            report["revision_id"] = revision["id"]
            previous_validations = call("GET", f"/reviews/{review_id}/validations")
            if previous_validations:
                validation_id = previous_validations[0]["id"]
            else:
                accepted = call("POST", f"/reviews/{review_id}/validations", {
                    "revision_id": revision["id"],
                }, expected=202)
                wait(accepted)
                validation_id = accepted["resource_id"]
            validation = call("GET", f"/validations/{validation_id}")
            assert validation["structure_passed"] and validation["result"] == "needs_review"
            report["validation"] = {"id": validation_id, "result": validation["result"]}
            export_body = {"revision_id": revision["id"], "validation_id": validation_id,
                           "acknowledge_warnings": False}
            blocked = client.post(f"/reviews/{review_id}/exports", json=export_body,
                                  headers={"Idempotency-Key": str(uuid.uuid4())})
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "WARNINGS_NOT_ACKNOWLEDGED"
            report["warning_guard"] = blocked.json()["error"]["code"]
            export_body["acknowledge_warnings"] = True
            accepted = call("POST", f"/reviews/{review_id}/exports", export_body, expected=202)
            wait(accepted)
            export = call("GET", f"/exports/{accepted['resource_id']}")
            assert export["status"] == "ready"
            downloaded = client.get(f"/exports/{export['id']}/file")
            downloaded.raise_for_status()
            output = args.output / "社会治理与五社联动_全流程测试草稿.docx"
            output.write_bytes(downloaded.content)
            text = "\n".join(p.text for p in Document(BytesIO(downloaded.content)).paragraphs)
            assert "AI 辅助生成草稿，需人工审阅" in text and "人工编辑流程测试" in text
            assert "参考文献" in text and "needs_review" in text
            calls = list(ModelCall.objects.filter(project_id=pid))
            assert calls and all(
                c.outcome == "succeeded" and c.model_snapshot["mode"] == "mock" for c in calls
            )
            report.update({"export_id": export["id"], "docx": str(output), "model_calls": len(calls),
                           "paid_calls": 0, "result": "passed"})
            save()
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        finally:
            # Remove only this walkthrough's temporary login session.
            Session.objects.filter(session_key=session_key).delete()


if __name__ == "__main__":
    main()
