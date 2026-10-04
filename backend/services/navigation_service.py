"""Where things are on a sheet: search highlights and cross-references.

Both read the PDF's word positions (the same data object counting uses), so a hit
can be drawn as a box on the page image and a reference can be a clickable region.
Boxes are fractions (0..1) of the displayed page, which is what the viewer scales.

Pure functions over PageGeometry; no I/O.
"""
import re
from dataclasses import dataclass

from backend.services.counting.pdf_geometry import PageGeometry, Word
from backend.services.counting.symbol_counter import group_lines, split_cells
from backend.services.drawing_metadata import normalise_sheet_key
from backend.services.grounding import alnum
from backend.services.retrieval_service import normalise_text

MAX_BOXES = 400


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float
    text: str = ""


@dataclass
class Reference:
    target_page: int
    target_label: str
    target_title: str | None
    text: str          # the line it appears in, for display
    box: Box


def _token(word: Word) -> str:
    return alnum(normalise_text(word.text))


def _union(words: list[Word]) -> tuple[float, float, float, float]:
    return (min(w.x0 for w in words), min(w.top for w in words),
            max(w.x1 for w in words), max(w.bottom for w in words))


def _box(geom: PageGeometry, words: list[Word], text: str = "") -> Box:
    x, y, w, h = geom.norm_box(*_union(words))
    return Box(round(x, 5), round(y, 5), round(w, 5), round(h, 5), text)


def find_highlights(
    geom: PageGeometry, terms: list[str], phrases: list[str], prefix_last: bool = True,
    patterns: list[re.Pattern] | None = None,
) -> list[Box]:
    """Boxes around every occurrence of the terms (alnum tokens) and phrases (alnum,
    no separators) on the page.

    Terms match whole words (the last may be a prefix, as in search). Phrases are
    searched in each text line with word boundaries removed, so a heading drawn
    letter-by-letter ("S LI M R E C E S S E D") still matches "slim recessed".
    """
    boxes: list[Box] = []
    lines = group_lines(geom.words)

    # Exact-punctuation patterns (dimensions) run over the line as written.
    for pat in patterns or []:
        for line in lines:
            text, spans = "", []
            for w in line:
                if text:
                    text += " "
                spans.append((len(text), len(text) + len(w.text)))
                text += w.text
            for m in pat.finditer(text):
                hit = [w for w, (a, b) in zip(line, spans) if b > m.start() and a < m.end()]
                if hit:
                    boxes.append(_box(geom, hit, m.group(0)))
    if patterns:
        phrases = []     # the pattern is the precise form of the phrase

    for ph in phrases:
        if not ph:
            continue
        for line in lines:
            joined, spans = "", []
            for w in line:
                tok = _token(w)
                spans.append((len(joined), len(joined) + len(tok)))
                joined += tok
            start = joined.find(ph)
            while start != -1:
                end = start + len(ph)
                hit = [w for w, (a, b) in zip(line, spans) if b > start and a < end and b > a]
                if hit:
                    boxes.append(_box(geom, hit, " ".join(w.text for w in hit)))
                start = joined.find(ph, end)

    last = len(terms) - 1
    for i, term in enumerate(terms):
        prefix = prefix_last and i == last and len(term) >= 3
        for w in geom.words:
            tok = _token(w)
            if tok == term or (prefix and tok.startswith(term)):
                boxes.append(_box(geom, [w], w.text))
        # Letter-spaced lines (average word length <= 2.5): match inside the joined line.
        if len(term) >= 4:
            for line in lines:
                if len(line) < 5 or sum(len(w.text) for w in line) / len(line) > 2.5:
                    continue
                joined, spans = "", []
                for w in line:
                    tok = _token(w)
                    spans.append((len(joined), len(joined) + len(tok)))
                    joined += tok
                pos = joined.find(term)
                while pos != -1:
                    hit = [w for w, (a, b) in zip(line, spans) if b > pos and a < pos + len(term) and b > a]
                    if hit:
                        boxes.append(_box(geom, hit, term))
                    pos = joined.find(term, pos + len(term))

    # De-duplicate boxes that cover the same place.
    unique: list[Box] = []
    for b in sorted(boxes, key=lambda b: (b.y, b.x)):
        if not any(abs(b.x - u.x) < 0.002 and abs(b.y - u.y) < 0.002 and abs(b.w - u.w) < 0.004 for u in unique):
            unique.append(b)
    return unique[:MAX_BOXES]


# Words that introduce a reference to another sheet.
_CUES = {"REFER", "SEE", "REF", "REFERENCE", "DRAWING", "DRAWINGS", "DWG", "DWGS", "SHEET", "SHEETS", "DETAIL", "PAGE"}
_NUMERIC_ID = re.compile(r"^\d{1,2}\.\d{1,2}[A-Za-z]?$")


def _clean(text: str) -> str:
    """Display text: drop PDF font artefacts such as '(cid:35)' and collapse spacing."""
    return re.sub(r"\s+", " ", re.sub(r"\(cid:\d+\)", " ", text)).strip()


def find_references(
    geom: PageGeometry,
    sheets: dict[str, tuple[int, str, str | None]],
    own_key: str | None,
) -> list[Reference]:
    """References from this sheet to other sheets in the set.

    `sheets` maps normalised sheet key -> (page_number, label, title).

    A letter-prefixed number (E2.01, M2.02) is a reference wherever it appears. A plain
    numeric one (1.3) is also a dimension, a quantity and a clause number, so it only
    counts after a cue word ("REFER TO 1.3", "SEE DRAWING 1.1b") or as a detail callout
    ("3/1.3"). Self-references and unknown numbers are ignored.
    """
    refs: list[Reference] = []
    seen: set[tuple[int, int]] = set()
    for full_line in group_lines(geom.words):
        # A drawing's text columns share baselines, so a "line" runs across unrelated
        # columns. Cue words and display text come from the column cell a word sits in.
        for cell in split_cells(full_line):
            cell_text = _clean(" ".join(w.text for w in cell))
            for i, w in enumerate(cell):
                raw = w.text.strip(" ,;:()[]")
                parts = raw.split("/") if "/" in raw else [raw]
                for part_no, part in enumerate(parts):
                    key = normalise_sheet_key(part)
                    if not part or key not in sheets or key == own_key:
                        continue
                    has_letters = bool(re.search(r"[A-Za-z]", part))
                    cue = any(re.sub(r"[^A-Z]", "", x.text.upper()) in _CUES for x in cell[max(0, i - 4):i])
                    callout = len(parts) > 1 and part_no == len(parts) - 1
                    if has_letters:
                        # E2.01 / A-101 style needs structure; "A1" alone is a tag.
                        if not (("." in part or "-" in part) or cue or callout):
                            continue
                    elif not (_NUMERIC_ID.match(part) and (cue or callout)):
                        continue
                    target, label, title = sheets[key]
                    spot = (target, round(w.top / 4))
                    if spot in seen:
                        continue
                    seen.add(spot)
                    refs.append(Reference(target, label, title, cell_text[:110], _box(geom, [w], part)))
    return refs
