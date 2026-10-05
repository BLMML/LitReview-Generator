"""M06 解析与证据账本测试（docs/mvp/01 第 4.3 节、docs/mvp/05 T09）。

覆盖四层：

1. `parsing.pdf`：真实文本层提取（1 起始页码、逐页对应）、空白页标记为需图像辅助、
   受控 PNG 渲染（不依赖 Pillow）；
2. `parsing.evidence`：切片长度区间、ordinal 从 1 起、**片段不跨页**、
   `locator` 页级且 bbox 为 null、原文匹配的诚实性；
3. `parsing.services`：全部页成功才发布、失败页不发布任何片段但保留诊断、
   相同配置复用产物、配置变化产生新产物、图像转录记为 `image_unverified`；
4. 跨 Run 引用拒绝：同一解析产物被两个 Run 复用时，只对真正纳入它的 Run 可见。

模型调用一律走 mock 夹具，不产生付费调用。
"""

from __future__ import annotations

import json
import uuid

import pytest
from tests.conftest import make_text_pdf_file

from apps.common.http import ApiError
from apps.common.storage import resolve_storage_key
from apps.parsing import evidence as evidence_module
from apps.parsing.models import (
    VERIFICATION_IMAGE_UNVERIFIED,
    VERIFICATION_TEXT_MATCHED,
    EvidenceChunk,
    ParsedDocument,
)
from apps.parsing.pdf import extract_pages, render_page

pytestmark = pytest.mark.django_db

PAGE_ONE = "Attention Is All You Need\nAshish Vaswani\nAbstract: the dominant sequence model."
PAGE_TWO = "Introduction\nRecurrent models have been the standard approach."
PAGE_THREE = "Method\nWe propose a new simple network architecture."


# ── 夹具 ──────────────────────────────────────────────────────
class _Snapshot:
    """`parse_paper` 需要的论文快照；字段与 RunPaper 对齐。"""

    def __init__(self, paper, asset):
        self.paper_id = paper.id
        self.project_id = paper.project_id
        self.file_asset = asset
        self.file_sha256 = paper.file_sha256
        self.title = paper.title


@pytest.fixture
def text_paper(client_a, project_a):
    """上传一份含文本层的 3 页 PDF，返回 (Paper, source_asset)。"""
    from apps.literature.models import Paper

    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_text_pdf_file([PAGE_ONE, PAGE_TWO, PAGE_THREE], name="text.pdf")},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])
    return paper, paper.file_asset


@pytest.fixture
def blank_paper(client_a, project_a):
    """上传一份**没有文本层**的单页 PDF，用于图像辅助路径。

    本夹具验证单页产物；多页由逐页转录测试单独覆盖。
    """
    from tests.conftest import make_pdf_file

    from apps.literature.models import Paper

    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(1, name="blank.pdf")},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])
    return paper, paper.file_asset


@pytest.fixture
def two_blank_pages_paper(client_a, project_a):
    """两页都没有文本层的 PDF，用于验证多页图像批次不会被伪造成多页证据。"""
    from tests.conftest import make_pdf_file

    from apps.literature.models import Paper

    response = client_a.post(
        f"/api/v1/projects/{project_a.id}/papers/upload",
        {"file": make_pdf_file(2, name="blank2.pdf")},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    paper = Paper.objects.select_related("file_asset").get(pk=response.json()["data"]["id"])
    return paper, paper.file_asset


def _execution_context(paper):
    from apps.jobs.models import Job
    from apps.jobs.services import claim_execution

    job = Job.objects.create(
        project_id=paper.project_id,
        requested_by=paper.project.owner,
        kind="analyze",
        resource_type="analysis_run",
        resource_id=uuid.uuid4(),
        input_hash="0" * 64,
    )
    return claim_execution(job.pk)


# ── 1. parsing.pdf ────────────────────────────────────────────
def test_extract_pages_maps_real_text_to_one_based_pages(text_paper):
    """页文本与真实文档逐页对应，页码从 1 起。"""
    _paper, asset = text_paper
    pages = extract_pages(asset)

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert "Attention Is All You Need" in pages[0].text
    assert "Recurrent models" in pages[1].text
    assert "new simple network architecture" in pages[2].text
    assert all(page.is_empty is False for page in pages)


def test_pages_without_text_layer_are_marked_for_image_help(blank_paper):
    """没有文本层的页被标记为空白，交给上层决定图像辅助，而不是当成解析成功。"""
    _paper, asset = blank_paper
    pages = extract_pages(asset)

    assert len(pages) == 1
    assert all(page.is_empty for page in pages)
    assert all(page.text == "" for page in pages)


def test_all_blank_pages_are_marked(two_blank_pages_paper):
    """多页都没有文本层时，每一页都要被标记，不能只标记第一页。"""
    _paper, asset = two_blank_pages_paper
    pages = extract_pages(asset)

    assert [page.page_number for page in pages] == [1, 2]
    assert all(page.is_empty for page in pages)


def test_render_page_returns_png_bytes(text_paper):
    """页面渲染产出真实 PNG；不依赖 Pillow。"""
    _paper, asset = text_paper
    payload = render_page(asset, 1)

    assert payload[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG 签名"
    assert len(payload) > 1000, "渲染结果过小，可能没有真正画出来"
    # 第二页尺寸应与第一页一致（同一文档、同一倍率）
    assert render_page(asset, 2)[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("page_number", [0, -1, 4, 99])
def test_render_page_rejects_out_of_range_page(text_paper, page_number):
    """超出真实页数或非 1 起始的页码必须显式失败。"""
    _paper, asset = text_paper
    with pytest.raises(ApiError):
        render_page(asset, page_number)


# ── 2. parsing.evidence：切片与 locator ───────────────────────
def test_split_page_text_respects_length_bounds(settings):
    """切片落在 600—1,200 字符区间内（`04` 第 6.2 节）。"""
    from apps.parsing.evidence import split_page_text

    settings.EVIDENCE_CHUNK_MIN_CHARS = 60
    settings.EVIDENCE_CHUNK_MAX_CHARS = 120
    body = "\n\n".join(f"Paragraph {index} " + ("word " * 12).strip() for index in range(12))
    pieces = split_page_text(body)

    assert pieces, "没有切出任何片段"
    assert all(len(piece) <= 120 for piece in pieces), [len(p) for p in pieces]
    # 除最后一片外都应达到下限（最后一片受原文长度限制）
    assert all(len(piece) >= 60 for piece in pieces[:-1]), [len(p) for p in pieces]
    assert "".join(pieces).count("Paragraph") == 12, "切分丢内容"


def test_split_page_text_never_loses_content(settings):
    """超长无标点文本按长度硬切，不丢字符。"""
    from apps.parsing.evidence import split_page_text

    settings.EVIDENCE_CHUNK_MIN_CHARS = 10
    settings.EVIDENCE_CHUNK_MAX_CHARS = 50
    body = "x" * 137
    pieces = split_page_text(body)
    assert "".join(pieces) == body


def test_detect_section_does_not_guess():
    """章节只在明确命中标题形态时返回，否则为 None。

    返回的是**原文行**（保留 "3." 这样的编号）——章节名要能与原文核对，
    不做归一化改写。
    """
    from apps.parsing.evidence import detect_section

    assert detect_section("Abstract\nThis paper studies ...") == "Abstract"
    assert detect_section("3. Method\nWe use ...") == "3. Method"
    assert detect_section("随机的一行普通正文，不是标题") is None


# ── 3. parsing.services：发布与失败语义 ───────────────────────
def test_parse_paper_publishes_document_and_page_bound_chunks(text_paper, user_a):
    """全部页成功时发布产物与片段；页码 1 起始、片段不跨页。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    document = parse_paper(None, _Snapshot(paper, asset), {})

    assert document.page_count == 3
    chunks = list(EvidenceChunk.objects.filter(parsed_document=document).order_by("ordinal"))
    assert chunks, "没有生成任何证据片段"
    assert [chunk.ordinal for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(1 <= chunk.page_number <= 3 for chunk in chunks)

    # 片段不跨页：任一页码的片段文本必须是该页原文的子串
    by_page = {page: [] for page in (1, 2, 3)}
    for chunk in chunks:
        by_page[chunk.page_number].append(chunk)
    pages = {page.page_number: page.text for page in extract_pages(asset)}
    for page_number, page_chunks in by_page.items():
        for chunk in page_chunks:
            normalized = " ".join(chunk.text.split())
            normalized_page = " ".join(pages[page_number].split())
            assert normalized in normalized_page, (
                f"第 {page_number} 页的片段不在该页原文中：{normalized[:60]!r}"
            )


def test_parse_paper_chunk_locator_is_page_level(text_paper):
    """MVP 只承诺页级定位：granularity=page 且 bbox 为 null。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    document = parse_paper(None, _Snapshot(paper, asset), {})
    for chunk in EvidenceChunk.objects.filter(parsed_document=document):
        assert chunk.locator["granularity"] == "page"
        assert chunk.locator["bbox"] is None, "不得伪造句子级坐标"


def test_parse_paper_text_chunks_are_text_matched(text_paper):
    """文本层片段记为 text_matched，且保留同页原文用于核对。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    document = parse_paper(None, _Snapshot(paper, asset), {})
    chunks = EvidenceChunk.objects.filter(parsed_document=document)
    assert chunks.exists()
    for chunk in chunks:
        assert chunk.verification == VERIFICATION_TEXT_MATCHED
        assert chunk.original_text, "文本层片段必须保留同页原文"


def test_parse_paper_reuses_result_for_same_config(text_paper):
    """相同项目 + 哈希 + parser_revision + config_hash 复用既有产物。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    first = parse_paper(None, _Snapshot(paper, asset), {"EVIDENCE_CHUNK_MAX_CHARS": 1200})
    second = parse_paper(None, _Snapshot(paper, asset), {"EVIDENCE_CHUNK_MAX_CHARS": 1200})

    assert first.pk == second.pk
    assert ParsedDocument.objects.count() == 1
    assert EvidenceChunk.objects.filter(parsed_document=first).count() == EvidenceChunk.objects.count()


def test_parse_paper_new_config_creates_new_document(text_paper):
    """配置变化必须产生新产物，不能复用旧切片。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    first = parse_paper(
        None, _Snapshot(paper, asset), {"EVIDENCE_CHUNK_MIN_CHARS": 60, "EVIDENCE_CHUNK_MAX_CHARS": 120}
    )
    second = parse_paper(
        None, _Snapshot(paper, asset), {"EVIDENCE_CHUNK_MIN_CHARS": 60, "EVIDENCE_CHUNK_MAX_CHARS": 240}
    )

    assert first.pk != second.pk
    assert ParsedDocument.objects.count() == 2


def test_unrecognized_page_fails_explicitly(blank_paper, user_a):
    """无法识别的复杂页显式失败：不发布产物、不产生片段、不补造内容。

    本用例没有执行上下文，因此不会调用模型；这正是"无法识别"的最保守形态——
    宁可整篇失败，也不把空白页当成解析成功。
    """
    from apps.parsing.services import parse_paper

    paper, asset = blank_paper
    with pytest.raises(ApiError) as error:
        parse_paper(None, _Snapshot(paper, asset), {})

    assert error.value.code == "PDF_PARSE_FAILED"
    assert error.value.retryable is True
    failed_pages = error.value.details["pages"]
    assert [item["page_number"] for item in failed_pages] == [1]
    assert not ParsedDocument.objects.exists(), "失败时不得发布产物"
    assert not EvidenceChunk.objects.exists(), "失败时不得产生片段"


def test_failed_parse_keeps_diagnostics(blank_paper, user_a):
    """失败页保留诊断产物：raw_pages 里能看到每页为何失败。"""
    from apps.common.models import FileAsset
    from apps.parsing.services import parse_paper

    paper, asset = blank_paper
    with pytest.raises(ApiError):
        parse_paper(None, _Snapshot(paper, asset), {})

    raw_asset = FileAsset.objects.filter(
        project=paper.project_id, purpose=FileAsset.PURPOSE_RAW_PAGES
    ).first()
    assert raw_asset is not None, "失败时没有保留诊断产物"
    payload = json.loads(resolve_storage_key(raw_asset.storage_key).read_text(encoding="utf-8"))
    assert payload["pages"][0]["error"]["code"] == "PDF_PARSE_FAILED"
    assert all(page["text"] == "" for page in payload["pages"])


def test_multi_page_images_are_transcribed_separately(two_blank_pages_paper, monkeypatch):
    """每页独立转录，成功页绑定自己的文字和真实图像，不复制整批内容。"""
    from apps.parsing import services as parsing_services

    paper, asset = two_blank_pages_paper
    context = _execution_context(paper)
    calls = []

    def transcribe(ctx, request):
        calls.append(request)
        assert len(request.model_config["image_asset_ids"]) == 1
        return type(
            "Result",
            (),
            {
                "payload": {
                    "elements": [
                        {
                            "element_type": "text",
                            "text": f"Text for page {request.payload['page_number']}",
                            "section": None,
                        }
                    ]
                }
            },
        )()

    monkeypatch.setattr(parsing_services, "call_structured", transcribe)
    document = parsing_services.parse_paper(context, _Snapshot(paper, asset), {})
    chunks = list(EvidenceChunk.objects.filter(parsed_document=document).order_by("ordinal"))
    assert [call.payload["page_number"] for call in calls] == [1, 2]
    assert [(chunk.page_number, chunk.text) for chunk in chunks] == [
        (1, "Text for page 1"),
        (2, "Text for page 2"),
    ]
    assert chunks[0].source_image_id != chunks[1].source_image_id
    assert all(chunk.verification == VERIFICATION_IMAGE_UNVERIFIED for chunk in chunks)


def test_image_transcription_is_unverified_by_default(blank_paper, user_a, monkeypatch):
    """图像辅助得到的片段记为 image_unverified，且绝不自动设为 human_verified。"""
    from apps.parsing import services as parsing_services
    from apps.parsing.models import VERIFICATION_HUMAN_VERIFIED

    paper, asset = blank_paper
    context = _execution_context(paper)

    # 模型替身：返回一段可辨认文字，证明"识别成功"路径也不得声称已匹配原文
    class _FakeResult:
        payload = {"elements": [{"element_type": "text", "text": "Transcribed line", "section": None}]}

    monkeypatch.setattr(parsing_services, "call_structured", lambda ctx, req: _FakeResult())

    document = parsing_services.parse_paper(context, _Snapshot(paper, asset), {})
    chunks = EvidenceChunk.objects.filter(parsed_document=document)
    assert chunks.exists(), "图像辅助路径没有产出片段"

    for chunk in chunks:
        assert chunk.verification == VERIFICATION_IMAGE_UNVERIFIED, chunk.verification
        assert chunk.verification != VERIFICATION_HUMAN_VERIFIED
        assert chunk.original_text is None, "图像转录不得伪称与原文匹配"
        assert chunk.source_image is not None, "图像页必须绑定实际渲染资产"


def test_image_pages_bind_real_rendered_asset(blank_paper, monkeypatch):
    """图像页的 source_image 指向本项目已登记的 PNG 资产。"""
    from apps.common.models import FileAsset
    from apps.parsing import services as parsing_services

    paper, asset = blank_paper
    context = _execution_context(paper)

    class _FakeResult:
        payload = {"elements": [{"element_type": "text", "text": "Transcribed", "section": None}]}

    monkeypatch.setattr(parsing_services, "call_structured", lambda ctx, req: _FakeResult())
    document = parsing_services.parse_paper(context, _Snapshot(paper, asset), {})

    chunk = EvidenceChunk.objects.filter(parsed_document=document).select_related("source_image").first()
    assert chunk is not None and chunk.source_image is not None
    assert chunk.source_image.purpose == FileAsset.PURPOSE_PAGE_IMAGE
    assert chunk.source_image.media_type == "image/png"
    stored = resolve_storage_key(chunk.source_image.storage_key)
    assert stored.is_file() and stored.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ── 4. 跨 Run 引用拒绝 ────────────────────────────────────────
def _make_run_with_parsed_paper(project, paper, document, *, include: bool):
    """建一个 Run；`include` 决定它是否真的纳入该解析产物。"""
    from apps.analysis.models import AnalysisRun, RunPaper

    run = AnalysisRun.objects.create(
        project=project,
        corpus_revision=1,
        research_question="用于测试的研究问题",
        output_language="zh-CN",
    )
    RunPaper.objects.create(
        run=run,
        paper=paper,
        title=paper.title,
        authors=[],
        year=None,
        source=paper.source,
        file_asset=paper.file_asset,
        file_sha256=paper.file_sha256,
        page_count=document.page_count,
        status="ready",
        parsed_document=document if include else None,
    )
    return run


def test_evidence_batch_rejects_cross_run_reference(text_paper, project_a):
    """跨 Run 引用一律拒绝：未纳入该产物的 Run 读不到它的证据。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    document = parse_paper(None, _Snapshot(paper, asset), {})
    chunk = EvidenceChunk.objects.filter(parsed_document=document).first()
    assert chunk is not None

    included_run = _make_run_with_parsed_paper(project_a, paper, document, include=True)
    other_run = _make_run_with_parsed_paper(project_a, paper, document, include=False)

    visible = evidence_module.get_evidence_batch(included_run.pk, [chunk.id])
    assert len(visible) == 1, "纳入该产物的 Run 应当能读到证据"
    assert visible[0]["id"] == str(chunk.id)
    assert visible[0]["run_id"] == str(included_run.pk)
    assert visible[0]["paper_id"] == str(paper.id)
    assert visible[0]["page_number"] == chunk.page_number

    hidden = evidence_module.get_evidence_batch(other_run.pk, [chunk.id])
    assert hidden == [], "未纳入该产物的 Run 不得读到跨 Run 证据"


def test_evidence_batch_denies_evidence_from_another_project(text_paper, project_a, project_b, user_b):
    """另一个项目的 Run 读不到本项目证据（越权与不存在同表现）。"""
    from apps.analysis.models import AnalysisRun
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    document = parse_paper(None, _Snapshot(paper, asset), {})
    chunk = EvidenceChunk.objects.filter(parsed_document=document).first()

    foreign_run = AnalysisRun.objects.create(
        project=project_b,
        corpus_revision=1,
        research_question="另一个项目的研究问题",
        output_language="zh-CN",
    )
    assert evidence_module.get_evidence_batch(foreign_run.pk, [chunk.id]) == []


def test_evidence_dto_shape_matches_contract(text_paper, project_a):
    """EvidenceDTO 字段与 `03` 第 2.3 节一致，且不含磁盘路径。"""
    from apps.parsing.services import parse_paper

    paper, asset = text_paper
    document = parse_paper(None, _Snapshot(paper, asset), {})
    chunk = EvidenceChunk.objects.filter(parsed_document=document).first()
    run = _make_run_with_parsed_paper(project_a, paper, document, include=True)

    dto = evidence_module.get_evidence_batch(run.pk, [chunk.id])[0]
    assert set(dto) == {
        "id",
        "run_id",
        "paper_id",
        "parsed_document_id",
        "page_number",
        "section",
        "element_type",
        "text",
        "locator",
        "verification",
        "file_url",
    }
    assert dto["file_url"] == f"/api/v1/papers/{paper.id}/file"
    assert "storage" not in json.dumps(dto).lower()
