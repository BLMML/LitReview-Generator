"""Preserve literal source modules and locate the evidence behind autonomous commentary."""

from pypdfium2 import PdfiumError

from apps.parsing.locations import compact, locate_quote
from apps.parsing.models import EvidenceChunk
from apps.reviews.schemas import SectionsValidationError
from apps.reviews.structure import (
    COMMENTARY_KEYS,
    LEGACY_SOURCE_TITLES,
    MISSING_CONTENT,
    SECTION_TITLES,
    SOURCE_KEYS,
    is_commentary_structure,
)


def ground_sections(run, sections, *, rewrite=False, source_sections=None, commentary=None):
    if commentary is None:
        commentary = is_commentary_structure(sections) or source_sections is not None
    wanted = {str(e) for s in sections for b in s["blocks"] for e in b["evidence_ids"]}
    papers = {rp.parsed_document_id: rp for rp in run.run_papers.select_related("file_asset")}
    rows = {str(e.pk): e for e in EvidenceChunk.objects.filter(pk__in=wanted, parsed_document_id__in=papers)}
    citations = {}
    basis = {}
    if source_sections is not None:
        basis_citations = ground_sections(run, source_sections, commentary=True)
        for section in source_sections:
            if section["section_key"] in SOURCE_KEYS:
                for block in section["blocks"]:
                    for eid in block["evidence_ids"]:
                        basis.setdefault(eid, []).extend(basis_citations[block["block_id"]])
    for section in sections:
        key = section["section_key"]
        titles = SECTION_TITLES if commentary else LEGACY_SOURCE_TITLES
        if key not in titles or section["title"] != titles[key]:
            raise SectionsValidationError("固定模块标题不得修改")
        for block in section["blocks"]:
            text, ids = block["text"], block["evidence_ids"]
            if "模拟草稿" in text:
                raise SectionsValidationError("综述正文不得包含模拟草稿提示")
            if text == MISSING_CONTENT and block["kind"] == "transition" and not ids:
                if len(section["blocks"]) != 1:
                    raise SectionsValidationError("缺失说明须独立占据该模块，不可与正文混用")
                citations[block["block_id"]] = []
                continue
            if commentary and key in COMMENTARY_KEYS:
                if block["kind"] != "claim" or not ids:
                    raise SectionsValidationError("自主述评必须引用前文已使用的原文依据")
                if any(eid not in basis for eid in ids):
                    raise SectionsValidationError("述评只能基于前文原文模块已引用的证据")
                citations[block["block_id"]] = list(
                    {
                        (p["evidence_id"], p["page"], p["paragraph"], p["line_start"], p["line_end"]): p
                        for eid in ids
                        for p in basis[eid]
                    }.values()
                )
                continue
            if block["kind"] != "claim" or len(ids) != 1:
                raise SectionsValidationError("正文每块必须引用一条原文证据；缺失内容使用规定句子")
            chunk = rows.get(str(ids[0]))
            if chunk is None or chunk.verification != "text_matched":
                raise SectionsValidationError("正文证据必须有可定位的 PDF 文本层")
            if compact(text) not in compact(chunk.text):
                raise SectionsValidationError("正文必须采用所引证据的原文表述，不得添加或改写内容")
            rp = papers[chunk.parsed_document_id]
            try:
                original, positions = locate_quote(rp.file_asset, chunk.page_number, text)
            except PdfiumError as exc:
                raise SectionsValidationError("PDF 文本层无法可靠定位，请核对材料") from exc
            except (ValueError, IndexError, OSError) as exc:
                raise SectionsValidationError(str(exc)) from exc
            if rewrite:
                block["text"] = original
            elif text != original:
                raise SectionsValidationError("正文与 PDF 原文表述不一致，请按原文保存")
            citations[block["block_id"]] = [
                {"evidence_id": str(chunk.pk), "filename": rp.file_asset.original_filename, **position}
                for position in positions
            ]
            for eid in ids:
                basis.setdefault(eid, []).extend(citations[block["block_id"]])
    return citations
