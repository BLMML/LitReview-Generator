"""PDF text-layer quotes and geometric paragraph/line positions.

Page numbers are physical PDF pages. Paragraphs restart on each page and
follow text reading order; columns, indentation and vertical gaps separate
paragraphs. Lines restart within a paragraph. OCR does not manufacture these
positions. No model-generated positions are accepted.
"""

from __future__ import annotations

import re
from functools import lru_cache
from itertools import groupby
from threading import Lock

from apps.common.storage import resolve_storage_key

LOCATION_REVISION = "pdf-geometric-lines-v1"
_PDF_LOCK = Lock()


def compact(text):
    return "".join(text.split())


@lru_cache(maxsize=64)
def page_lines(storage_key, sha256, page_number):
    with _PDF_LOCK:
        return _read_page_lines(storage_key, page_number)


def _read_page_lines(storage_key, page_number):
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(resolve_storage_key(storage_key)))
    page = textpage = None
    try:
        page = document[page_number - 1]
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        if len(text) != textpage.count_chars():
            raise ValueError("PDF 字符映射无法可靠定位段行，请人工核对材料")
        lines, geometry = [], []
        paragraph, line_number = 0, 0
        previous = None
        paragraph_left = 0
        for match in re.finditer(r"[^\r\n]+", text):
            value = match.group().strip()
            if not value:
                continue
            boxes = []
            for index in range(match.start(), match.end()):
                if not text[index].isspace():
                    boxes.append(textpage.get_charbox(index))
            if not boxes:
                continue
            left = min(b[0] for b in boxes)
            bottom = min(b[1] for b in boxes)
            right = max(b[2] for b in boxes)
            top = max(b[3] for b in boxes)
            height = max(top - bottom, 1)
            new_paragraph = previous is None
            if previous:
                prev_left, prev_bottom, prev_right, prev_top, prev_height = previous
                new_paragraph = (
                    top > prev_top + height * 0.6  # next column / reading-order block
                    or abs(left - prev_left) > page.get_width() * 0.22
                    or prev_bottom - top > max(height, prev_height) * 0.9
                    or abs(height - prev_height) > max(height, prev_height) * 0.45
                    or left - paragraph_left > height * 1.2
                    or prev_right - prev_left < height * 8
                )
            if new_paragraph:
                paragraph += 1
                line_number = 0
                paragraph_left = left
            else:
                paragraph_left = min(paragraph_left, left)
            line_number += 1
            lines.append({"text": value, "paragraph": paragraph, "line": line_number})
            geometry.append((left, height))
            previous = left, bottom, right, top, height
        for _, grouped in groupby(zip(lines, geometry, strict=True), key=lambda pair: pair[0]["paragraph"]):
            entries = list(grouped)
            first, (left, height) = entries[0]
            first["indented"] = left - min(box[0] for _, box in entries) > height * 1.2
        return tuple(lines)
    finally:
        if textpage is not None:
            textpage.close()
        if page is not None:
            page.close()
        document.close()


def locate_quote(asset, page_number, quote):
    """Return canonical source wording and all covered paragraph/line spans.

    Whitespace is used only for matching. The returned wording is rebuilt
    from the PDF, so a model cannot change word boundaries by changing spaces.
    """
    lines = page_lines(asset.storage_key, asset.sha256, page_number)
    characters, positions = [], []
    for index, line in enumerate(lines):
        for offset, char in enumerate(line["text"]):
            if not char.isspace():
                characters.append(char)
                positions.append((index, offset))
    needle = compact(quote)
    start = "".join(characters).find(needle) if needle else -1
    if start < 0:
        raise ValueError("正文无法匹配 PDF 文本层，不能生成可靠的段行出处")
    first, first_offset = positions[start]
    last, last_offset = positions[start + len(needle) - 1]
    paragraph_start = first_offset == 0 and (
        first == 0 or lines[first - 1]["paragraph"] != lines[first]["paragraph"]
    )
    # An abstract label is metadata at the paragraph start, not a clause.
    paragraph_prefix = (
        "".join(line["text"] for line in lines[:first] if line["paragraph"] == lines[first]["paragraph"])
        + lines[first]["text"][:first_offset]
    )
    if re.fullmatch(r"(?:摘要|abstract)[:：]", compact(paragraph_prefix), flags=re.IGNORECASE):
        paragraph_start = True
    paragraph_end = last_offset == len(lines[last]["text"]) - 1 and (
        last == len(lines) - 1 or lines[last + 1]["paragraph"] != lines[last]["paragraph"]
    )
    prefix = "\n".join([item["text"] for item in lines[:first]] + [lines[first]["text"][:first_offset]])
    prefix = prefix.rstrip().rstrip("”’\"')）】")
    # A superscript footnote following the preceding sentence belongs to that
    # sentence. It must not make the next complete sentence look truncated.
    before_footnotes = prefix
    prefix = prefix.rstrip("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳⁰¹²³⁴⁵⁶⁷⁸⁹").rstrip("”’\"')）】")
    sentence_start = prefix.endswith(tuple("。！？!?")) or (
        prefix.endswith(".")
        and (
            prefix != before_footnotes
            or first_offset == 0
            or lines[first]["text"][first_offset - 1].isspace()
        )
    )
    sentence_end = needle.rstrip("”’\"')）】").endswith(tuple("。！？.!?"))
    if not (paragraph_start or sentence_start) or not (sentence_end or paragraph_end):
        raise ValueError("正文必须保留完整句子或段落，不得截去否定、条件或结论的一部分")
    pieces, spans = [], []
    for index in range(first, last + 1):
        item = lines[index]
        value = item["text"]
        value = value[first_offset:] if index == first else value
        if index == last:
            value = value[: last_offset + 1 - (first_offset if first == last else 0)]
        if (
            pieces
            and pieces[-1][-1:].isascii()
            and (pieces[-1][-1:].isalnum() or pieces[-1][-1:] in ".!?\"')")
            and value[:1].isascii()
            and value[:1].isalnum()
        ):
            pieces.append(" ")
        pieces.append(value)
        if spans and spans[-1]["paragraph"] == item["paragraph"]:
            spans[-1]["line_end"] = item["line"]
        else:
            spans.append(
                {
                    "page": page_number,
                    "paragraph": item["paragraph"],
                    "line_start": item["line"],
                    "line_end": item["line"],
                }
            )
    return "".join(pieces), spans


SOURCE_QUOTE_REVISION = "complete-pdf-passages-v1"


def quotable_passages(asset, page_number, chunk_text, *, maximum=600):
    """Offer only intact source sentences/paragraphs that fit one manuscript block.

    Page/chunk edges may split a sentence. Read the original paragraph geometry,
    then require the complete passage to belong to the selected evidence chunk.
    Never shorten a clause to make it fit the character limit.
    """
    available = compact(chunk_text)
    result, seen = [], set()
    lines = page_lines(asset.storage_key, asset.sha256, page_number)
    first_sentence_paragraph = True
    for _paragraph, grouped in groupby(lines, key=lambda line: line["paragraph"]):
        paragraph_lines = list(grouped)
        text = "\n".join(line["text"] for line in paragraph_lines)
        # Punctuation allows whole sentences; a short complete paragraph is also
        # offered. Final verification below uses the same citation locator.
        stops = list(
            re.finditer(
                r"[。！？!?]+[”’\"')）】]*|(?<!\d)\.(?!\d)[”’\"')）】]*(?=\s|$)",
                text,
            )
        )
        if not stops:
            continue
        pieces, start = [], 0
        for stop in stops:
            pieces.append(text[start : stop.end()].strip())
            start = stop.end()
        ambiguous_start = (
            page_number > 1 and first_sentence_paragraph and not paragraph_lines[0].get("indented", False)
        )
        first_sentence_paragraph = False
        # An unindented first body paragraph may continue the preceding page.
        # Use only later complete sentences instead of assuming its start is intact.
        if ambiguous_start:
            pieces = pieces[1:]
        candidates = (
            [text]
            if not ambiguous_start and stops[-1].end() == len(text.rstrip()) and len(compact(text)) <= maximum
            else []
        )
        candidates.extend(piece for piece in pieces if len(compact(piece)) <= maximum)
        current = ""
        for piece in pieces:
            combined = (current + "\n" + piece).strip() if current else piece
            if current and len(compact(combined)) > maximum:
                candidates.append(current)
                current = ""
            if len(compact(piece)) <= maximum:
                current = (current + "\n" + piece).strip() if current else piece
        if current:
            candidates.append(current)
        for candidate in candidates:
            needle = compact(candidate)
            if not needle or needle not in available or needle in seen:
                continue
            try:
                original, _positions = locate_quote(asset, page_number, candidate)
            except ValueError:
                continue
            if len(original) > maximum:
                continue
            seen.add(needle)
            result.append(original)
    return result
