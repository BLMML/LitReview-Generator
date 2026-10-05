"""Strict source wording and citation locations for the new manuscript."""

from pypdfium2 import PdfiumError

from apps.parsing.locations import compact, locate_quote
from apps.parsing.models import EvidenceChunk
from apps.reviews.schemas import SectionsValidationError
from apps.reviews.structure import MISSING_CONTENT, SECTION_KEYS, SECTION_TITLES


def ground_sections(run, sections, *, rewrite=False):
    wanted = {str(e) for s in sections for b in s["blocks"] for e in b["evidence_ids"]}
    papers = {rp.parsed_document_id: rp for rp in run.run_papers.select_related("file_asset")}
    rows = {
        str(e.pk): e
        for e in EvidenceChunk.objects.filter(
            pk__in=wanted,
            parsed_document_id__in=papers,
        )
    }
    citations = {}
    for section in sections:
        key = section["section_key"]
        if key not in SECTION_KEYS or section["title"] != SECTION_TITLES[key]:
            raise SectionsValidationError("固定模块标题不得修改")
        for block in section["blocks"]:
            text, ids = block["text"], block["evidence_ids"]
            if text == MISSING_CONTENT and block["kind"] == "transition" and not ids:
                if len(section["blocks"]) != 1:
                    raise SectionsValidationError("缺失说明须独立占据该模块，不可与正文混用")
                citations[block["block_id"]] = []
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
                {
                    "evidence_id": str(chunk.pk),
                    "filename": rp.file_asset.original_filename,
                    **position,
                }
                for position in positions
            ]
    return citations
