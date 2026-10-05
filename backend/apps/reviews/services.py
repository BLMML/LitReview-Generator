"""reviews.services：受理生成 / 核验 / 导出，保存编辑新版本。

职责（docs/mvp/01 第 4.6 节）：
- `create_review(actor, run_id, payload, key) → AcceptedJob`
- `generate_review(context, review_id) → ReviewRevision`
- `save_revision(actor, review_id, base_revision_number, sections) → ReviewRevision`
- `create_validation(...)` / `validate_revision(context, validation_id)`（M11）
- `create_export(...)` / `export_revision(context, export_id)`（M11）

生成链（docs/mvp/04 第 5.1 节）：
freeze_inputs → retrieve_sections → generate_sections → check_structure → commit_revision。

编辑规则：人工保存锁 Project 与 Review，base_revision_number 必须等于最高版本；
先验证完整正文再插入 N+1；新版本继承 base_revision 的 analysis_revision，
不因改字消除分析过时状态；编辑保存本身不暗中调用模型。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.common.auth import ActorContext
from apps.common.http import api_error
from apps.common.idempotency import ReplayResult, claim_or_replay, request_fingerprint
from apps.common.locking import lock_project, require_project_idle
from apps.common.models import IdempotencyRecord
from apps.jobs.models import JOB_KIND_EXPORT_DOCX, JOB_KIND_GENERATE_REVIEW, JOB_KIND_VALIDATE_REVIEW, Job
from apps.jobs.services import AcceptedJob, ExecutionContext, accept_job, claim_execution, record_progress
from apps.llm.client import call_structured
from apps.llm.prompt_loader import load_prompt
from apps.llm.schemas import TASK_REVIEW_SECTION, ModelTaskRequest
from apps.parsing.locations import LOCATION_REVISION, SOURCE_QUOTE_REVISION, quotable_passages
from apps.reviews.grounding import ground_sections
from apps.reviews.models import (
    REVISION_SOURCE_GENERATED,
    REVISION_SOURCE_MANUAL,
    Export,
    Review,
    ReviewRevision,
    ValidationReport,
)
from apps.reviews.schemas import (
    SectionsValidationError,
    sections_content_hash,
    validate_sections,
)
from apps.reviews.structure import MISSING_CONTENT, SECTION_KEYS, SECTION_TITLES, SOURCE_POLICY

logger = logging.getLogger("apps.reviews.services")

GENERATE_PROMPT = "generate_sections.md"
SCHEMA_VERSION = "1.0"

# 每节选定的证据条数（`04` 第 5.1 节：5—10 条）
EVIDENCE_PER_SECTION_MIN = 5
EVIDENCE_PER_SECTION_MAX = 10

# 生成配置版本：模板或生成参数改变时必须提升
GENERATION_VERSION = "review-gen-v4-source-passages"

SECTION_REQUIREMENTS = {
    "background": "研究背景：仅摘录文献明确交代的研究背景与问题情境。",
    "significance": "研究意义：仅摘录文献明确说明的研究意义，不自行评价研究价值。",
    "approach": "研究思路：仅摘录文献明确说明的研究思路、研究路径与方法安排。",
    "theoretical_basis": "理论基础：仅摘录文献明确采用或阐述的理论基础。",
    "research_status": "国内外研究现状：仅摘录文献对国内外已有研究的陈述，不引入外部文献。",
    "achievements": "已取得研究成果：仅摘录文献明确记载的已取得成果或研究结果，不添加预测。",
}


# ── 受理 ──────────────────────────────────────────────────────
def create_review(actor: ActorContext, run_id: Any, payload: Mapping[str, Any], key: Any) -> AcceptedJob:
    """受理生成：新建 Review（无空白 revision 0）+ generate_review Job，返回 202。"""
    from apps.analysis.models import AnalysisRun, RunPaper
    from apps.projects.models import Project

    run = (
        AnalysisRun.objects.select_related("project")
        .filter(pk=run_id, project__owner=actor.user, project__deleted_at__isnull=True)
        .first()
    )
    if run is None:
        raise api_error("NOT_FOUND")
    project = Project.objects.filter(pk=run.project_id, owner=actor.user, deleted_at__isnull=True).first()
    if project is None:  # pragma: no cover - 与上面的过滤条件等价
        raise api_error("NOT_FOUND")

    # 与创建 Run 同样的处理：指纹只含**客户端请求身份**，不含服务端生成的配置。
    canonical_path = f"/api/v1/runs/{run_id}/reviews"
    client_fingerprint = request_fingerprint(
        {
            "kind": JOB_KIND_GENERATE_REVIEW,
            "title": payload.get("title"),
            "target_chars": payload.get("target_chars"),
            "style": payload.get("style"),
        }
    )

    with transaction.atomic():
        locked = lock_project(project.pk, owner=actor.user)
        claim = claim_or_replay(
            actor,
            "POST",
            canonical_path,
            uuid.UUID(str(key)),
            _compatible_request_fingerprint(actor, run, canonical_path, key, payload, client_fingerprint),
        )
        if isinstance(claim, ReplayResult):
            return AcceptedJob(
                uuid.UUID(claim.body["job_id"]),
                claim.body["resource_type"],
                uuid.UUID(claim.body["resource_id"]),
                replayed=True,
            )

        require_project_idle(locked)
        # 锁前的 Run 只用于定位项目；争议复核可能已改变分析版本。
        run = AnalysisRun.objects.select_for_update().get(pk=run.pk, project=locked)
        _require_generatable(run, locked)

        title = str(payload.get("title") or "").strip() or _default_title(run)
        target_chars = int(payload.get("target_chars") or settings.MVP_LIMITS["REVIEW_TARGET_CHARS_DEFAULT"])
        style = str(payload.get("style") or settings.REVIEW_STYLE_DEFAULT)
        if style not in settings.REVIEW_STYLES_ALLOWED:
            raise api_error("VALIDATION_ERROR", "不支持的综述风格")
        review_id = uuid.uuid4()
        config_snapshot = _build_config_snapshot(run, review_id=review_id, target_chars=target_chars)

        ready_papers = RunPaper.objects.filter(run=run, status="ready").count()
        if ready_papers == 0:
            raise api_error("RESULT_NOT_READY", "Run 还没有任何已完成的单篇分析")

        review = Review.objects.create(
            id=review_id,
            project=locked,
            run=run,
            title=title,
            analysis_revision=run.analysis_revision,
            target_chars=target_chars,
            style=style,
            config_snapshot=config_snapshot,
        )

        return accept_job(
            actor,
            locked,
            JOB_KIND_GENERATE_REVIEW,
            {
                "resource_id": review.id,
                "frozen_input": {
                    "run_id": str(run.id),
                    "review_id": str(review.id),
                    "analysis_revision": run.analysis_revision,
                    "corpus_revision": run.corpus_revision,
                    "config_snapshot": config_snapshot,
                },
            },
            idempotency=claim,
        )


def _compatible_request_fingerprint(
    actor: ActorContext, run: Any, path: str, key: Any, payload: Mapping[str, Any], current: str
) -> str:
    """v1 曾把默认值写入指纹；旧记录按原 Review 参数重放，不回填或改写历史。"""
    existing = (
        IdempotencyRecord.objects.select_related("job")
        .filter(user=actor.user, method="POST", canonical_path=path, key=key)
        .first()
    )
    if existing is None or existing.request_hash == current:
        return current
    if existing.job is None or existing.job.kind != JOB_KIND_GENERATE_REVIEW:
        return current
    legacy = Review.objects.filter(
        pk=existing.job.resource_id,
        project_id=run.project_id,
        run_id=run.pk,
        config_snapshot__version="review-gen-v1",
    ).first()
    if legacy is None:
        return current
    legacy_hash = request_fingerprint(
        {
            "kind": JOB_KIND_GENERATE_REVIEW,
            "title": payload.get("title"),
            "target_chars": payload.get("target_chars") or legacy.target_chars,
            "style": payload.get("style") or legacy.style,
        }
    )
    return legacy_hash if existing.request_hash == legacy_hash else current


def _default_title(run: Any) -> str:
    """未给标题时用冻结的研究问题派生一个可读标题，不编造内容。"""
    question = (run.research_question or "").strip()
    if not question:
        return "综述"
    limit = int(settings.MVP_LIMITS["REVIEW_TITLE_MAX"])
    return question[:limit]


def _require_generatable(run: Any, project: Any) -> None:
    """生成要求 Run ready 且语料未过时（`03` 第 2.4 节）。"""
    from apps.analysis.models import RUN_READY

    if run.status != RUN_READY:
        raise api_error("RUN_NOT_READY", details={"status": run.status})
    if int(run.corpus_revision) != int(project.corpus_revision):
        raise api_error("STALE_RUN", details={"corpus_revision": run.corpus_revision})


def _generation_template_version() -> str:
    """即使某节无证据、不调用模型，也先核对冻结模板版本。"""
    return load_prompt(
        GENERATE_PROMPT,
        {
            "section_key": SECTION_KEYS[0],
            "section_requirement": "",
            "research_question": "",
            "output_language": "",
            "corpus_metadata": "",
            "analysis_summary": "",
            "evidence_package": "",
            "allowed_evidence_ids": "",
            "max_blocks": 1,
            "target_chars": 1,
            "section_target_chars": 1,
        },
        task_type=TASK_REVIEW_SECTION,
    ).version


def _build_config_snapshot(run: Any, *, review_id: uuid.UUID, target_chars: int) -> dict[str, Any]:
    """冻结生成、内部检索和分节篇幅参数；执行不得回退到新部署默认值。"""
    from apps.retrieval.services import freeze_retrieval_config

    retrieval_config = freeze_retrieval_config()
    retrieval_config.update(retrieval_id=str(review_id), run_id=str(run.id))
    # 在 20 个候选的契约上限内尊重更低的部署限制，并记录实际值。
    candidate_limit = min(20, int(retrieval_config["rerank"]["max_candidates"]))
    top_k = min(EVIDENCE_PER_SECTION_MAX, candidate_limit)
    weights = (15, 15, 15, 15, 20, 20)
    section_targets = {
        key: target_chars * weight // 100 for key, weight in zip(SECTION_KEYS, weights, strict=True)
    }
    section_targets[SECTION_KEYS[-1]] += target_chars - sum(section_targets.values())
    return {
        "version": GENERATION_VERSION,
        "source_policy": SOURCE_POLICY,
        "location_revision": LOCATION_REVISION,
        "source_quote_revision": SOURCE_QUOTE_REVISION,
        "template_version": _generation_template_version(),
        "schema_version": SCHEMA_VERSION,
        "model_config": {
            "model": settings.DEEPSEEK_MODEL,
            "thinking": settings.DEEPSEEK_THINKING_MODE,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            "temperature": 0,
        },
        "evidence_per_section_min": EVIDENCE_PER_SECTION_MIN,
        "evidence_per_section_max": EVIDENCE_PER_SECTION_MAX,
        "max_blocks_per_section": int(settings.REVIEW_MAX_BLOCKS_PER_SECTION),
        "section_keys": list(SECTION_KEYS),
        "target_chars": target_chars,
        "section_target_chars": section_targets,
        "retrieval_config": retrieval_config,
        "retrieval_limits": {"candidate_limit": candidate_limit, "top_k": top_k},
        "retrieval_query_max": int(settings.MVP_LIMITS["RETRIEVAL_QUERY_MAX"]),
        "section_queries": {
            key: _section_query(run, key, maximum=int(settings.MVP_LIMITS["RETRIEVAL_QUERY_MAX"]))
            for key in SECTION_KEYS
        },
        "section_requirements": dict(SECTION_REQUIREMENTS),
        # 分析侧配置一并留档：生成结果依赖的主题 / 争议 / 索引口径都在里面
        "analysis_config": dict(run.config_snapshot or {}),
    }


# ── 执行 ──────────────────────────────────────────────────────
def generate_review(context: ExecutionContext, review_id: Any) -> ReviewRevision:
    """worker 入口：逐槽选取原文与证据，一次性提交固定研究框架 revision 1。"""

    review = (
        Review.objects.select_related("run", "run__project")
        .filter(pk=review_id, run__project_id=context.project_id)
        .first()
    )
    if review is None:
        raise api_error("NOT_FOUND")
    run = review.run
    config = _frozen_generation_config(review)

    if _existing_generated_revision(review, context) is not None:
        # 同一 Job 已提交过：不重复生成（源任务唯一约束也会拦住）
        logger.info("生成任务已提交过正文 review=%s", review.id)
        return _existing_generated_revision(review, context)

    sections: list[dict[str, Any]] = []
    record_progress(context, stage="freeze_inputs", completed=0, total=len(SECTION_KEYS), unit="section")
    for position, section_key in enumerate(SECTION_KEYS):
        context.ensure_not_cancelled()
        sections.append(_generate_section(context, review, run, section_key, position, config))
        record_progress(context, completed=position + 1)

    return _commit_revision(context, review, run, sections, config)


def _existing_generated_revision(review: Review, context: ExecutionContext) -> ReviewRevision | None:
    return ReviewRevision.objects.filter(review=review, source_job_id=context.job_id).first()


def _frozen_generation_config(review: Review) -> dict[str, Any]:
    snapshot = review.config_snapshot or {}
    if (
        snapshot.get("version") != GENERATION_VERSION
        or not isinstance(snapshot.get("template_version"), str)
        or any(
            not isinstance(snapshot.get(key), Mapping)
            for key in (
                "model_config",
                "retrieval_config",
                "retrieval_limits",
                "section_target_chars",
                "section_queries",
                "section_requirements",
            )
        )
    ):
        # 缺生成参数时不得回退到当前默认值：那等于用今天的模板完成昨天的 Review
        raise api_error("INTERNAL_ERROR", "Review 缺少冻结生成配置，请重新创建综述")
    _require_frozen_version(_generation_template_version(), snapshot["template_version"], "生成")
    return dict(snapshot)


def _generate_section(
    context: ExecutionContext,
    review: Review,
    run: Any,
    section_key: str,
    position: int,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """单节：选证据 → 渲染提示词 → 调用模型 → 校验该节结构。"""
    from apps.llm.schemas import TASK_REVIEW_SECTION

    record_progress(context, stage="retrieve_sections", completed=position)
    package = _section_evidence(context, run, section_key, config)
    record_progress(context, stage="generate_sections", completed=position)
    if not package["chunks"]:
        context.ensure_not_cancelled()
        return _validated_section(
            {
                "section_key": section_key,
                "title": SECTION_TITLES[section_key],
                "blocks": [
                    {
                        "kind": "transition",
                        "text": MISSING_CONTENT,
                        "evidence_ids": [],
                    }
                ],
            },
            section_key,
            run,
            max_blocks=int(config["max_blocks_per_section"]),
        )

    parameters = {
        "section_key": section_key,
        "section_requirement": str(config["section_requirements"][section_key]),
        "research_question": run.research_question,
        "output_language": run.output_language,
        "corpus_metadata": _json(_corpus_metadata(run)),
        "analysis_summary": _json(_analysis_summary(run)),
        "evidence_package": _json(package["chunks"]),
        "allowed_evidence_ids": _json(package["ids"]),
        "max_blocks": int(config["max_blocks_per_section"]),
        "target_chars": int(config["target_chars"]),
        "section_target_chars": int(config["section_target_chars"][section_key]),
    }
    prompt = load_prompt(GENERATE_PROMPT, parameters, task_type=TASK_REVIEW_SECTION)
    _require_frozen_version(prompt.version, config["template_version"], "生成")

    for attempt in range(2):
        request = ModelTaskRequest(
            task_type=TASK_REVIEW_SECTION,
            template_version=prompt.version,
            schema_version=str(config["schema_version"]),
            model_config=dict(config["model_config"]),
            payload=parameters,
            allowed_evidence_ids=list(package["ids"]),
            logical_call_id=uuid.uuid5(
                review.id,
                f"review_section:{section_key}" + (":source_repair" if attempt else ""),
            ),
        )
        result = call_structured(context, request)
        context.ensure_not_cancelled()
        try:
            return _validated_section(
                result.payload, section_key, run, max_blocks=int(config["max_blocks_per_section"])
            )
        except SectionsValidationError as exc:
            if attempt:
                raise api_error(
                    "MODEL_OUTPUT_INVALID",
                    f"{SECTION_TITLES[section_key]}的原文摘录未通过校验：{exc}",
                    {"section_key": section_key},
                    retryable=False,
                ) from exc
            parameters = {
                **parameters,
                "section_requirement": str(config["section_requirements"][section_key])
                + "\n上次正文未通过原文完整性校验。请重新选择 quotable_passages 中的完整原句或原段，"
                "禁止使用页首、页尾的半句话，不改写或拼接；没有适合本模块的原句时使用规定的缺失说明。",
            }
    raise AssertionError("unreachable")


def _validated_section(payload: Any, section_key: str, run: Any, *, max_blocks: int) -> dict[str, Any]:
    """校验单节输出并补齐后端分配的 block_id。"""
    from apps.analysis.services import _run_scope

    if not isinstance(payload, Mapping):
        raise SectionsValidationError(f"{section_key} 的输出必须是对象")
    if payload.get("section_key") != section_key:
        # 模型改写了内容槽会破坏固定顺序。
        raise SectionsValidationError(
            f"{section_key} 的输出 section_key 必须是本节，实际 {payload.get('section_key')!r}"
        )

    raw_blocks = payload.get("blocks")
    if isinstance(raw_blocks, (str, bytes)) or not isinstance(raw_blocks, Sequence):
        raise SectionsValidationError(f"{section_key}.blocks 必须是数组")

    # block_id 由后端分配：模型不参与编号，因此不接受它给出的 ID
    blocks = [
        {
            "block_id": str(uuid.uuid4()),
            "kind": item.get("kind") if isinstance(item, Mapping) else None,
            "text": item.get("text") if isinstance(item, Mapping) else None,
            "evidence_ids": list(item.get("evidence_ids") or []) if isinstance(item, Mapping) else None,
        }
        for item in raw_blocks
    ]
    candidate = [{"section_key": section_key, "title": SECTION_TITLES[section_key], "blocks": blocks}]
    validated = validate_sections(
        candidate,
        run_scope=_run_scope(run),
        expected_keys=[section_key],
        max_blocks_per_section=max_blocks,
        allow_source_numbers=True,
    )
    ground_sections(run, validated, rewrite=True)
    return validated[0]


def _commit_revision(
    context: ExecutionContext,
    review: Review,
    run: Any,
    sections: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> ReviewRevision:
    """结构检查通过后，在持租约的短事务里一次性提交 revision。"""
    from apps.analysis.services import _run_scope
    from apps.jobs.services import complete_job

    # 全稿复核：单节校验各自通过，不代表拼起来仍合法（block_id 唯一、总量上限）
    record_progress(context, stage="check_structure")
    final_sections = validate_sections(
        list(sections),
        run_scope=_run_scope(run),
        expected_keys=list(SECTION_KEYS),
        max_blocks_per_section=int(config["max_blocks_per_section"]),
    )
    source_citations = ground_sections(run, final_sections)
    content_hash = sections_content_hash(final_sections)
    record_progress(context, stage="commit_revision")
    with transaction.atomic():
        lock_project(context.project_id)
        review = Review.objects.select_for_update().get(pk=review.pk, project_id=context.project_id)
        _lock_review_job(context, review)
        next_number = _next_revision_number(review)
        revision = ReviewRevision.objects.create(
            review=review,
            run=run,
            revision_number=next_number,
            analysis_revision=review.analysis_revision,
            corpus_revision=run.corpus_revision,
            sections=final_sections,
            config_snapshot={**dict(config), "source_citations": source_citations},
            source=REVISION_SOURCE_GENERATED,
            created_by=review.project.owner,
            source_job_id=context.job_id,
            content_hash=content_hash,
        )

        def finalize(job: Job) -> None:
            pass

        complete_job(
            context,
            {
                "review_id": str(review.id),
                "revision_id": str(revision.id),
                "revision_number": revision.revision_number,
            },
            finalize_resource=finalize,
        )
    return revision


def _next_revision_number(review: Review) -> int:
    latest = review.revisions.order_by("-revision_number").first()
    return 1 if latest is None else int(latest.revision_number) + 1


# ── 编辑保存 ──────────────────────────────────────────────────
def save_revision(
    actor: ActorContext,
    review_id: Any,
    base_revision_number: int,
    sections: Sequence[Any],
) -> ReviewRevision:
    """保存人工编辑；base_revision_number 不匹配返回 409 VERSION_CONFLICT。

    同步短事务：锁 Project 与 Review → 比较版本 → 全量校验 → 插入 N+1。
    新版本**继承** base 的 analysis_revision，用户改字不能自动消除来源过时状态。
    """
    from apps.analysis.services import _run_scope

    with transaction.atomic():
        review = (
            Review.objects.select_related("run", "project")
            .filter(pk=review_id, project__owner=actor.user, project__deleted_at__isnull=True)
            .first()
        )
        if review is None:
            raise api_error("NOT_FOUND")

        project = lock_project(review.project_id, owner=actor.user)
        require_project_idle(project)
        review = Review.objects.select_for_update().select_related("run").get(pk=review.pk)

        latest = review.revisions.order_by("-revision_number").first()
        if latest is None:
            raise api_error("RESULT_NOT_READY", "还没有可编辑的正文版本")
        if int(latest.revision_number) != int(base_revision_number):
            # 乐观锁：不匹配说明有人在页面上改过，不能让后者静默覆盖前者
            raise api_error(
                "VERSION_CONFLICT",
                details={
                    "current_revision_number": int(latest.revision_number),
                    "base_revision_number": int(base_revision_number),
                },
            )

        try:
            validated = validate_sections(
                list(sections),
                run_scope=_run_scope(review.run),
                expected_keys=[s["section_key"] for s in latest.sections],
            )
            if latest.config_snapshot.get("source_policy") == SOURCE_POLICY:
                source_citations = ground_sections(review.run, validated)
            else:
                source_citations = {}
        except SectionsValidationError as exc:
            raise api_error("STRUCTURAL_VALIDATION_FAILED", str(exc), {"fields": {}}) from exc

        revision = ReviewRevision.objects.create(
            review=review,
            run=review.run,
            revision_number=int(latest.revision_number) + 1,
            # 继承而非重置：改字不会让过时的分析变新
            analysis_revision=int(latest.analysis_revision),
            corpus_revision=int(latest.corpus_revision),
            sections=validated,
            config_snapshot={**dict(latest.config_snapshot or {}), "source_citations": source_citations},
            source=REVISION_SOURCE_MANUAL,
            created_by=actor.user,
            base_revision=latest,
            content_hash=sections_content_hash(validated),
        )
    return revision


# ── 核验与导出（M11 实现）────────────────────────────────────
def create_validation(actor: Any, review_id: Any, payload: Mapping[str, Any], key: Any) -> AcceptedJob:
    """受理核验：绑定确切 revision，冻结正文哈希与核验配置。"""
    from apps.reviews.snapshots import require_revision_integrity
    from apps.reviews.validation import (
        VALIDATION_VERSION,
        evidence_snapshot,
        snapshot_hash,
        validation_schema_config,
        validation_template_version,
    )

    review = _owned_review(actor, review_id)
    body = {"revision_id": str(payload["revision_id"])}
    with transaction.atomic():
        project = lock_project(review.project_id, owner=actor.user)
        claim = _claim_review_request(actor, review.id, "validations", key, body)
        if isinstance(claim, ReplayResult):
            return _accepted_replay(claim)
        require_project_idle(project)
        revision = _review_revision(review.id, body["revision_id"])
        require_revision_integrity(revision, revision.content_hash)
        config = {
            "version": VALIDATION_VERSION,
            "template_version": validation_template_version(),
            "schema_version": SCHEMA_VERSION,
            "output_language": revision.run.output_language,
            "batch_size": 5,
            "max_input_tokens": int(settings.LLM_MAX_INPUT_TOKENS),
            "model_config": {
                "model": settings.DEEPSEEK_MODEL,
                "thinking": settings.DEEPSEEK_THINKING_MODE,
                "temperature": 0,
                "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            },
            "evidence_hash": snapshot_hash(evidence_snapshot(revision)),
            **validation_schema_config(),
        }
        report = ValidationReport.objects.create(
            revision=revision, content_hash=revision.content_hash, config_snapshot=config
        )
        return accept_job(
            actor,
            project,
            JOB_KIND_VALIDATE_REVIEW,
            {
                "resource_id": report.id,
                "frozen_input": {
                    "run_id": str(revision.run_id),
                    "review_id": str(review.id),
                    "revision_id": str(revision.id),
                    "content_hash": report.content_hash,
                    "config_snapshot": config,
                },
            },
            claim,
        )


def validate_revision(context: Any, validation_id: Any) -> Any:
    """worker 入口：结构检查 + 语义支持核验，成功报告覆盖正文所有 block 恰好一次。"""
    from apps.jobs.services import complete_job
    from apps.reviews.snapshots import report_snapshot, require_revision_integrity
    from apps.reviews.validation import (
        VALIDATION_VERSION,
        ClaimFindings,
        build_report,
        evidence_snapshot,
        snapshot_hash,
        validate_claim_support,
        validate_structure,
        validation_schema_config,
        validation_template_version,
    )

    report = (
        ValidationReport.objects.select_related("revision__review", "revision__run")
        .filter(pk=validation_id, revision__review__project_id=context.project_id)
        .first()
    )
    if report is None:
        raise api_error("NOT_FOUND")
    job = _execution_job(context, JOB_KIND_VALIDATE_REVIEW, report.id)
    if job.status == "succeeded" and report.result is not None:
        return report
    context.ensure_not_cancelled()
    frozen = job.frozen_input
    revision = report.revision
    config = frozen.get("config_snapshot") or {}
    require_revision_integrity(revision, frozen["content_hash"])
    if (
        frozen["revision_id"] != str(revision.id)
        or report.content_hash != frozen["content_hash"]
        or config != report.config_snapshot
        or config.get("version") != VALIDATION_VERSION
        or config.get("template_version") != validation_template_version()
    ):
        raise api_error("INVALID_JOB_STATE", "核验绑定或冻结配置已改变，请新建核验报告")
    if config.get("schema_hash") != validation_schema_config()["schema_hash"]:
        raise api_error("INVALID_JOB_STATE", "冻结的核验 Schema 已改变，请新建核验报告")
    record_progress(context, stage="structure", completed=0, total=3, unit="step")
    structural = validate_structure(revision)
    if snapshot_hash(structural.evidence) != config["evidence_hash"]:
        raise api_error("INVALID_EVIDENCE", "冻结证据已改变，请新建核验报告")
    record_progress(context, stage="claim_support", completed=1)
    claims = (
        validate_claim_support(context, revision, structural.evidence, config=config, validation_id=report.id)
        if structural.passed
        else ClaimFindings()
    )
    record_progress(context, stage="summarize", completed=2)
    findings = build_report(revision, structural, claims)
    with transaction.atomic():
        lock_project(context.project_id)
        _lock_review_job(context, revision.review)
        locked = ValidationReport.objects.select_for_update().get(pk=report.id)
        current = _review_revision(revision.review_id, revision.id)
        require_revision_integrity(current, frozen["content_hash"])
        if locked.config_snapshot != config or locked.content_hash != frozen["content_hash"]:
            raise api_error("INVALID_JOB_STATE", "核验期间报告绑定已改变")
        if snapshot_hash(evidence_snapshot(current)) != config["evidence_hash"]:
            raise api_error("INVALID_EVIDENCE", "核验期间证据已改变")
        for key, value in findings.items():
            setattr(locked, key, value)
        locked.save(update_fields=["result", "structure_passed", "blocks", "updated_at"])
        # 成功报告摘要参与完整性校验；不改变业务请求的冻结输入。
        result_hash = snapshot_hash(report_snapshot(locked))
        complete_job(
            context, {"validation_id": str(locked.id), "result": locked.result, "report_hash": result_hash}
        )
    return locked


def create_export(actor: Any, review_id: Any, payload: Mapping[str, Any], key: Any) -> AcceptedJob:
    """受理导出：revision 与 validation 必须同版本且 structure_passed=true。"""
    from apps.reviews.exporter import TEMPLATE_REVISION, build_bibliography
    from apps.reviews.snapshots import (
        export_warnings,
        report_snapshot,
        require_acknowledged,
        require_completed_report,
        require_revision_integrity,
    )
    from apps.reviews.validation import evidence_snapshot, snapshot_hash, validate_structure

    review = _owned_review(actor, review_id)
    body = {
        "revision_id": str(payload["revision_id"]),
        "validation_id": str(payload["validation_id"]),
        "acknowledge_warnings": bool(payload.get("acknowledge_warnings", False)),
    }
    with transaction.atomic():
        project = lock_project(review.project_id, owner=actor.user)
        claim = _claim_review_request(actor, review.id, "exports", key, body)
        if isinstance(claim, ReplayResult):
            return _accepted_replay(claim)
        require_project_idle(project)
        revision = _review_revision(review.id, body["revision_id"])
        require_revision_integrity(revision, revision.content_hash)
        report = ValidationReport.objects.filter(
            pk=body["validation_id"], revision__review_id=review.id
        ).first()
        require_completed_report(revision, report)
        if not validate_structure(revision).passed:
            raise api_error("STRUCTURAL_VALIDATION_FAILED", "原始来源或正文结构不再有效，需要重新核验")
        warnings = export_warnings(revision, report)
        require_acknowledged(warnings, body["acknowledge_warnings"])
        evidence = evidence_snapshot(revision)
        from apps.reviews.structure import is_source_structure

        if not is_source_structure(revision.sections):
            raise api_error("STRUCTURAL_VALIDATION_FAILED", "此手稿为旧结构，请重新生成以使用固定研究框架")
        snapshot = {
            "title": review.title,
            "revision_id": str(revision.id),
            "revision_number": revision.revision_number,
            "run_id": str(revision.run_id),
            "research_question": revision.run.research_question,
            "analysis_revision": revision.analysis_revision,
            "content_hash": revision.content_hash,
            "sections": revision.sections,
            "source_citations": ground_sections(revision.run, revision.sections),
            "warnings": warnings,
            "papers": _frozen_bibliography_papers(revision),
            "evidence": evidence,
        }
        bibliography = build_bibliography(snapshot)
        validation_snapshot = report_snapshot(report)
        export = Export.objects.create(
            revision=revision,
            validation_report=report,
            acknowledge_warnings=body["acknowledge_warnings"],
            warnings=warnings,
            content_hash=revision.content_hash,
            template_revision=TEMPLATE_REVISION,
        )
        return accept_job(
            actor,
            project,
            JOB_KIND_EXPORT_DOCX,
            {
                "resource_id": export.id,
                "frozen_input": {
                    "run_id": str(revision.run_id),
                    "review_id": str(review.id),
                    "revision_id": str(revision.id),
                    "validation_id": str(report.id),
                    "content_hash": revision.content_hash,
                    "template_revision": TEMPLATE_REVISION,
                    "revision_snapshot": snapshot,
                    "validation_snapshot": validation_snapshot,
                    "bibliography": bibliography,
                    "report_hash": snapshot_hash(validation_snapshot),
                    "evidence_hash": snapshot_hash(evidence),
                },
            },
            claim,
        )


def export_revision(context: Any, export_id: Any) -> Any:
    """worker 入口：生成临时 DOCX → 原子发布文件与 Export.ready。"""
    from apps.common.models import FileAsset
    from apps.common.storage import commit_asset, discard_staged, resolve_storage_key
    from apps.jobs.services import complete_job
    from apps.reviews.exporter import render_docx

    export = (
        Export.objects.select_related("revision__review__project", "revision__run", "validation_report")
        .filter(pk=export_id, revision__review__project_id=context.project_id)
        .first()
    )
    if export is None:
        raise api_error("NOT_FOUND")
    job = _execution_job(context, JOB_KIND_EXPORT_DOCX, export.id)
    if job.status == "succeeded" and export.status == "ready":
        return export
    context.ensure_not_cancelled()
    _check_export_input(export, job.frozen_input)
    frozen = job.frozen_input
    staged = asset = None
    published = False
    try:
        record_progress(context, stage="render", completed=0, total=2, unit="step")
        staged = render_docx(
            frozen["revision_snapshot"],
            frozen["validation_snapshot"],
            frozen["bibliography"],
            frozen["revision_snapshot"]["evidence"],
        )
        record_progress(context, stage="publish", completed=1)
        with transaction.atomic():
            project = lock_project(context.project_id)
            # 所有导出终态统一 Job → Export，取消/失败/租约回收也遵循此顺序。
            _lock_review_job(context, export.revision.review)
            locked = (
                Export.objects.select_for_update(of=("self",))
                .select_related("revision__review__project", "revision__run", "validation_report")
                .get(pk=export.id)
            )
            _check_export_input(locked, frozen)
            asset = commit_asset(project, staged, FileAsset.PURPOSE_EXPORT_DOCX, source_job=job)
            locked.file_asset = asset
            locked.status = "ready"
            locked.error = None
            locked.save(update_fields=["file_asset", "status", "error", "updated_at"])
            complete_job(context, {"export_id": str(locked.id), "file_asset_id": str(asset.id)})
        published = True
        return locked
    finally:
        discard_staged(staged)
        if asset is not None and not published:
            # 仅移除本次已经改名、但被外层事务回滚的文件。
            resolve_storage_key(asset.storage_key).unlink(missing_ok=True)


def _owned_review(actor: Any, review_id: Any) -> Review:
    review = Review.objects.filter(
        pk=review_id, project__owner=actor.user, project__deleted_at__isnull=True
    ).first()
    if review is None:
        raise api_error("NOT_FOUND")
    return review


def _review_revision(review_id: Any, revision_id: Any) -> ReviewRevision:
    revision = (
        ReviewRevision.objects.select_related("run", "review__project")
        .filter(pk=revision_id, review_id=review_id)
        .first()
    )
    if revision is None:
        raise api_error("NOT_FOUND")
    return revision


def _claim_review_request(actor: Any, review_id: Any, action: str, key: Any, body: Mapping) -> Any:
    return claim_or_replay(
        actor, "POST", f"/api/v1/reviews/{review_id}/{action}", uuid.UUID(str(key)), request_fingerprint(body)
    )


def _accepted_replay(claim: ReplayResult) -> AcceptedJob:
    return AcceptedJob(
        uuid.UUID(claim.body["job_id"]),
        claim.body["resource_type"],
        uuid.UUID(claim.body["resource_id"]),
        replayed=True,
    )


def _execution_job(context: ExecutionContext, kind: str, resource_id: Any) -> Job:
    from apps.jobs.services import payload_fingerprint

    job = Job.objects.filter(
        pk=context.job_id,
        project_id=context.project_id,
        kind=kind,
        resource_id=resource_id,
        lease_token=context.lease_token,
    ).first()
    if job is None:
        raise api_error("INVALID_JOB_STATE", "任务与业务资源不匹配")
    if payload_fingerprint(job.frozen_input) != job.input_hash:
        raise api_error("INVALID_JOB_STATE", "冻结任务输入已改变")
    return job


def _frozen_bibliography_papers(revision: Any) -> list[dict[str, Any]]:
    return [
        {
            "paper_id": str(rp.paper_id),
            "title": rp.title,
            "authors": rp.authors,
            "year": rp.year,
            "source": rp.source,
            "source_id": rp.source_id,
            "source_url": rp.source_url,
            "file_sha256": rp.file_sha256,
        }
        for rp in revision.run.run_papers.order_by("created_at", "id")
    ]


def _check_export_input(export: Export, frozen: Mapping) -> None:
    from apps.reviews.exporter import TEMPLATE_REVISION
    from apps.reviews.snapshots import (
        export_warnings,
        report_snapshot,
        require_acknowledged,
        require_completed_report,
        require_revision_integrity,
    )
    from apps.reviews.validation import evidence_snapshot, snapshot_hash, validate_structure

    revision = export.revision
    require_revision_integrity(revision, frozen["content_hash"])
    require_completed_report(revision, export.validation_report)
    if (
        frozen["revision_id"] != str(revision.id)
        or frozen["validation_id"] != str(export.validation_report_id)
        or export.content_hash != frozen["content_hash"]
        or export.template_revision != frozen["template_revision"]
        or frozen["template_revision"] != TEMPLATE_REVISION
        or frozen["revision_snapshot"]["warnings"] != export.warnings
        or snapshot_hash(report_snapshot(export.validation_report)) != frozen["report_hash"]
        or snapshot_hash(evidence_snapshot(revision)) != frozen["evidence_hash"]
    ):
        raise api_error("INVALID_JOB_STATE", "导出绑定或冻结输入已改变，请新建导出")
    if not validate_structure(revision).passed:
        raise api_error("STRUCTURAL_VALIDATION_FAILED", "导出来源已失效")
    require_acknowledged(
        export_warnings(revision, export.validation_report),
        export.acknowledge_warnings,
        frozen=export.warnings,
    )


# ── 共享工具 ──────────────────────────────────────────────────
def _lock_review_job(context: ExecutionContext, review: Review) -> Job | None:
    """锁住本 Job 行并确认仍是当前尝试；阶段发布必须用它。"""
    job = Job.objects.select_for_update().filter(pk=context.job_id, project_id=review.project_id).first()
    if job is None or int(job.lease_token) != int(context.lease_token):
        raise api_error("INVALID_JOB_STATE", "本次尝试已失效")
    from apps.common.locking import check_execution_lease

    check_execution_lease(job, context.lease_token)
    from django.utils import timezone

    if job.lease_expires_at is None or job.lease_expires_at <= timezone.now():
        raise api_error("INVALID_JOB_STATE", "执行租约已过期")
    return job


def _require_frozen_version(current: str, frozen: Any, stage: str) -> None:
    if frozen != current:
        raise api_error(
            "INTERNAL_ERROR",
            f"冻结的{stage}提示词版本与当前模板不一致，请重新创建综述",
            {"frozen": frozen, "current": current},
        )


def _section_evidence(
    context: ExecutionContext, run: Any, section_key: str, config: Mapping[str, Any]
) -> dict[str, Any]:
    """为单节选证据：用该节的问题去检索，取前 N 条作为生成上下文。"""
    from apps.retrieval.services import retrieve_evidence, run_snapshot_for

    retrieval_config = dict(config["retrieval_config"])
    # 每节有稳定的内部检索身份，既不创建嵌套 Job，也不跨 Review 共用尝试计数。
    retrieval_config["retrieval_id"] = str(
        uuid.uuid5(uuid.UUID(retrieval_config["retrieval_id"]), f"section_retrieval:{section_key}")
    )
    query = str(config["section_queries"][section_key])
    ranked, _expanded, candidates = retrieve_evidence(
        context,
        run_snapshot_for(run),
        query,
        dict(config["retrieval_limits"]),
        config=retrieval_config,
    )

    by_id = {str(item["evidence_id"]): item for item in candidates}
    chunks: list[dict[str, Any]] = []
    for hit in ranked:
        evidence_id = str(hit["evidence_id"])
        meta = by_id.get(evidence_id)
        if meta is None:  # pragma: no cover - 结果必来自候选
            continue
        chunks.append(
            {
                "evidence_id": evidence_id,
                "paper_id": meta.get("paper_id"),
                "page_number": meta.get("page_number"),
                "section": meta.get("section"),
                "text": meta.get("text"),
            }
        )
    if config.get("source_quote_revision") == SOURCE_QUOTE_REVISION and chunks:
        papers = {str(p.paper_id): p for p in run.run_papers.select_related("file_asset")}
        usable = []
        for chunk in chunks:
            paper = papers.get(str(chunk["paper_id"]))
            if paper is None:
                continue
            passages = quotable_passages(paper.file_asset, chunk["page_number"], chunk["text"])
            if passages:
                usable.append({**chunk, "quotable_passages": passages})
        if not usable:
            raise api_error("INVALID_EVIDENCE", "本节原文没有可完整定位的句子或段落，请核对 PDF 文本层")
        chunks = usable
    return {"chunks": chunks, "ids": [item["evidence_id"] for item in chunks]}


def _section_query(run: Any, section_key: str, *, maximum: int) -> str:
    """把冻结研究问题与本节要求拼成**聚焦**的检索语句。

    查询必须短：关键词覆盖率是 `命中词数 / 查询词数`，把整段节要求（几十个词）
    拼进去会让覆盖率极低，召回直接落空——文案看起来更"精确"，实际什么都取不到。
    这里只取节要求的第一句，并整体截断到检索接口允许的长度。
    语义排序由重排模型负责，召回只需给出一批候选。
    """
    question = (run.research_question or "").strip()
    first_sentence = SECTION_REQUIREMENTS[section_key].split("。", 1)[0]
    query = f"{question} {first_sentence}".strip()
    return query[:maximum]


def _corpus_metadata(run: Any) -> dict[str, Any]:
    """本 Run 的真实题录与年份统计；只使用冻结快照里的信息。"""
    year_counts: dict[str, int] = {}
    unknown_year = 0
    papers = []
    for run_paper in run.run_papers.order_by("created_at", "id"):
        papers.append(
            {
                "paper_id": str(run_paper.paper_id),
                "title": run_paper.title,
                "authors": list(run_paper.authors or []),
                "year": run_paper.year,
                "source": run_paper.source,
            }
        )
        if run_paper.year is None:
            unknown_year += 1
        else:
            key = str(run_paper.year)
            year_counts[key] = year_counts.get(key, 0) + 1
    return {
        "papers": papers,
        "year_counts": year_counts,
        "unknown_year_count": unknown_year,
    }


def _analysis_summary(run: Any) -> dict[str, Any]:
    """单篇要素、主题归属与争议标签（含人工复核状态）。"""
    from apps.analysis.models import REVIEW_STATUS_REJECTED, PaperAnalysis

    payloads = {
        str(item.run_paper.paper_id): item.payload
        for item in PaperAnalysis.objects.select_related("run_paper").filter(run_paper__run=run)
    }
    topics = [
        {"name": topic.name, "description": topic.description, "paper_ids": list(topic.paper_ids)}
        for topic in run.topics.order_by("created_at", "id")
    ]
    disputes = [
        {
            "left_paper_id": str(item.left_paper_id),
            "right_paper_id": str(item.right_paper_id),
            "classification": item.classification,
            "reason": item.reason,
            "review_status": item.review_status,
            "conditions": item.conditions,
            # rejected 候选不得进入结论：这里明确带出状态，由提示词规则约束写法
            "excluded": item.review_status == REVIEW_STATUS_REJECTED,
        }
        for item in run.disputes.order_by("created_at", "id")
    ]
    return {
        "papers": [{"paper_id": key, "analysis": value} for key, value in payloads.items()],
        "topics": topics,
        "disputes": disputes,
    }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def claim_review_execution(job_id: Any) -> ExecutionContext:
    return claim_execution(job_id)


# 供测试与调用方判断"哪些节由本阶段生成"
__all__ = [
    "SECTION_REQUIREMENTS",
    "create_export",
    "create_review",
    "create_validation",
    "export_revision",
    "generate_review",
    "save_revision",
    "validate_revision",
]
