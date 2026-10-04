"""Count objects from the text on a sheet: schedule rows and tag occurrences.

Two independent kinds of evidence come out of a sheet's words:

  * definition rows: a schedule line "L1  2X4 LED TROFFER ... QTY 14" gives a tag,
    a description (so a "2x4" question can be resolved to L1, not L2) and often a
    quantity;
  * tag occurrences: where "L1" (or "RTU-1") appears on a plan, with its position,
    so each counted instance can be marked on the sheet for the user to check.

Pure functions over PageGeometry; no I/O.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from backend.services.counting.objects import ObjectClass, ParsedCount, singular, size_variants
from backend.services.counting.pdf_geometry import PageGeometry, Word
from backend.services.counting.symbol_counter import cell_box, group_lines, split_cells
from backend.services.retrieval_service import normalise_text

# A tag: 1-4 letters then digits (L1, RTU-1, EF-2, SD-3, D101, LF-2A). Whole words only.
TAG_RE = re.compile(r"^[A-Z]{1,4}-?\d{1,3}[A-Z]?$")
_QTY_HEADERS = {"QTY", "QTY.", "QUANTITY", "QUAN", "QUAN.", "NO.", "COUNT"}
_QTY_INLINE_RE = re.compile(r"\b(?:QTY|QUANTITY)\.?\s*[:=]?\s*(\d{1,4})\b")


def clean_word(text: str) -> str:
    """Upper-case with surrounding punctuation stripped: '(L1),' -> 'L1'."""
    return re.sub(r"^[^A-Z0-9]+|[^A-Z0-9]+$", "", text.upper())


def tag_key(tag: str) -> str:
    """RTU-1, RTU1 and rtu-1 are the same tag."""
    return re.sub(r"[^A-Z0-9]", "", tag.upper())


@dataclass
class DefRow:
    tag: str
    page_number: int
    text: str                       # the whole row, upper-case
    qty: int | None
    box: tuple[float, float, float, float]
    first_word: Word


@dataclass
class TagHit:
    tag: str
    page_number: int
    word: Word


@dataclass
class TextEvidence:
    rows: list[DefRow] = field(default_factory=list)
    hits: list[TagHit] = field(default_factory=list)
    skipped_for_size: list[str] = field(default_factory=list)  # rows of the class that lack the size


def _line_text(words: list[Word]) -> str:
    return normalise_text(" ".join(w.text for w in words))


def _words_of(text: str) -> set[str]:
    return {singular(w) for w in re.findall(r"[A-Z][A-Z0-9]*", text)}


def _qty_for_row(line_words: list[Word], header_cx: float | None, tag_word: Word) -> int | None:
    inline = _QTY_INLINE_RE.search(" ".join(w.text for w in line_words).upper())
    if inline:
        return int(inline.group(1))
    if header_cx is None:
        return None
    best = None
    for w in line_words:
        if w is tag_word or not re.fullmatch(r"\d{1,4}", w.text.strip()):
            continue
        dist = abs(w.cx - header_cx)
        if dist <= 18 and (best is None or dist < best[0]):
            best = (dist, int(w.text))
    return best[1] if best else None


def _qty_headers(lines: list[list[Word]]) -> list[tuple[float, float]]:
    """(top, cx) of each QTY column header on the page."""
    out = []
    for line in lines:
        for w in line:
            if w.text.upper().strip(":") in _QTY_HEADERS and w.text.upper() != "NO.":
                out.append((w.top, w.cx))
    return out


def tag_prefix_word(word: str, cls: ObjectClass) -> bool:
    """True for a user word that is the class's own tag prefix (RTU for RTU-1): the
    row matches by its tag, so the word is not a further restriction."""
    return bool(cls.tag_re) and word in cls.tag_re


def find_rows(
    geom: PageGeometry, cls: ObjectClass, parsed: ParsedCount
) -> tuple[list[DefRow], list[str]]:
    """Schedule rows on this page that define the object being counted.

    A row is a line that starts with a tag, has a description, and either names the
    object (keyword) or carries a tag of the object's own form (RTU-1). If the user
    gave a size ("2x4"), the row must mention it; rows of the right kind that lack
    it are returned separately so the answer can say what was seen instead.
    """
    lines = group_lines(geom.words)
    headers = _qty_headers(lines)
    keywords = {singular(w) for kw in cls.keywords for w in kw.split()} | set(parsed.words)
    # Words the user added beyond the class noun ("downlight", "recessed") must be in the row.
    specific = [w for w in parsed.specific_words if not tag_prefix_word(w, cls)]
    size_opts = [size_variants(s) for s in parsed.sizes]
    tag_form = re.compile(cls.tag_re) if cls.tag_re else None

    rows: list[DefRow] = []
    wrong_size: list[str] = []
    for line in lines:
        first_cell = split_cells(line)[0]
        first = first_cell[0]
        tag = clean_word(first.text)
        if not TAG_RE.match(tag) or len(line) < 3:
            continue
        text = _line_text(line)
        rest_words = _words_of(" ".join(w.text for w in line[1:]).upper())
        compact = re.sub(r"[^A-Z0-9]", "", text)
        if specific:
            # Every restriction must appear (as a word, or run together: "DOWN LIGHT").
            if not all(w in rest_words or w in compact for w in specific):
                continue
        elif not (bool(keywords & rest_words) or bool(tag_form and tag_form.match(tag))):
            continue
        if size_opts and not all(any(v in text.replace(" ", "") or v in text for v in opts) for opts in size_opts):
            wrong_size.append(f"{tag}: {' '.join(w.text for w in line[1:7])}")
            continue
        header_cx = None
        above = [(first.top - t, cx) for t, cx in headers if t < first.top]
        if above:
            header_cx = min(above)[1]
        rows.append(DefRow(
            tag=tag, page_number=geom.page_number, text=text,
            qty=_qty_for_row(line, header_cx, first),
            box=cell_box(line), first_word=first,
        ))
    return rows, wrong_size


def find_tag_hits(
    geom: PageGeometry, tags: set[str], exclude_words: set[int] | None = None
) -> list[TagHit]:
    """Every occurrence of any of `tags` on the page, with its position."""
    keys = {tag_key(t): t for t in tags}
    hits = []
    for w in geom.words:
        if exclude_words and id(w) in exclude_words:
            continue
        k = tag_key(clean_word(w.text))
        if k in keys:
            hits.append(TagHit(keys[k], geom.page_number, w))
    return hits


def scan_class_tags(geom: PageGeometry, cls: ObjectClass, exclude_words: set[int] | None = None) -> list[TagHit]:
    """Words shaped like this class's tags (RTU-1, EF-2) when no schedule names them."""
    if not cls.tag_re:
        return []
    pattern = re.compile(cls.tag_re)
    out = []
    for w in geom.words:
        if exclude_words and id(w) in exclude_words:
            continue
        tag = clean_word(w.text)
        if pattern.match(tag):
            out.append(TagHit(tag, geom.page_number, w))
    return out


def distinct_tags(hits: list[TagHit]) -> dict[str, list[TagHit]]:
    out: dict[str, list[TagHit]] = {}
    for h in hits:
        out.setdefault(tag_key(h.tag), []).append(h)
    return out
