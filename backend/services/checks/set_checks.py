"""Cross-sheet checks on a drawing set (Phase 6).

Each check looks for something a reviewer would want to look at, and says exactly
what it saw and where. A finding is a prompt to look, never a verdict: drawings are
messy, so every finding carries a confidence and the evidence behind it, and the
checks are deliberately conservative (a missed problem is better than a wall of
false alarms nobody reads).

Checks:
  duplicate_sheet     two pages claim the same sheet number
  index_mismatch      the sheet list on the cover disagrees with the sheets present
  broken_reference    "SEE E9.01" where no such sheet is in the set
  scale_conflict      the stated scale disagrees with the sheet's own dimensions
  dimension_mismatch  one dimension's text disagrees with its drawn length

Pure functions over page metadata and PageGeometry; no I/O.
"""
import re
from dataclasses import dataclass, field

from backend.services.counting.pdf_geometry import PageGeometry
from backend.services.counting.symbol_counter import group_lines, split_cells
from backend.services.drawing_metadata import normalise_sheet_key, page_label
from backend.services.measure.scale import AGREE, PT_PER_IN, SheetScales, analyse_sheet
from backend.services.measure.units import format_imperial, format_metric

HIGH, MEDIUM, LOW = "high", "medium", "low"
SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

# A dimension must be at least this far (relatively, and in inches) from what is drawn.
DIM_REL_TOLERANCE = 0.10
DIM_MIN_VALUE_IN = 24.0
DIM_MIN_ABS_IN = 6.0


@dataclass
class PageInfo:
    page_number: int
    sheet_number: str | None
    sheet_title: str | None
    text: str = ""

    @property
    def label(self) -> str:
        return page_label(self.page_number, self.sheet_number)


@dataclass
class Finding:
    check: str
    severity: str
    page_number: int
    label: str
    message: str
    evidence: list[str] = field(default_factory=list)
    box: tuple[float, float, float, float] | None = None       # fractions of the page
    confidence: str = "medium"
    key: str = ""                                               # stable id within the check

    @property
    def id(self) -> str:
        return f"{self.check}:{self.page_number}:{self.key}"


# ── Sheet numbering ─────────────────────────────────────────────────────────

def check_duplicate_sheets(pages: list[PageInfo]) -> list[Finding]:
    by_key: dict[str, list[PageInfo]] = {}
    for p in pages:
        if p.sheet_number:
            by_key.setdefault(normalise_sheet_key(p.sheet_number), []).append(p)
    out = []
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        for p in group[1:]:
            first = group[0]
            out.append(Finding(
                "duplicate_sheet", MEDIUM, p.page_number, p.label,
                f"Sheet number {p.sheet_number} is also on PDF page {first.page_number}"
                f"{' (' + first.sheet_title + ')' if first.sheet_title else ''}. Two sheets should not share a number "
                f"(or one was read wrongly).",
                [f"PDF page {q.page_number}: {q.sheet_title or 'untitled'}" for q in group], None, "medium", key,
            ))
    return out


_INDEX_HEADING = re.compile(r"\b(?:LIST\s+OF\s+DRAWINGS|DRAWING\s+(?:LIST|INDEX)|SHEET\s+(?:LIST|INDEX)|INDEX\s+OF\s+DRAWINGS)\b", re.I)
_RANGE = re.compile(r"(?<![\w.])(\d{1,2}\.\d{1,2})\s*[-–]\s*(\d{1,2}\.\d{1,2})(?![\w.])")
_NUMERIC_ID = re.compile(r"(?<![\w.\-])\d{1,2}\.\d{1,2}[a-zA-Z]?(?![\w.])")
_LETTER_ID = re.compile(r"(?<![\w.\-])[A-Z]{1,3}-?\d{1,3}\.\d{1,3}[A-Z]?(?![\w.])")


def _numeric_value(sheet_number: str) -> tuple[int, int] | None:
    """(2, 10) for '2.10'; None unless it is a plain dotted number like 0.1 or 1.1a."""
    m = re.match(r"^(\d{1,2})\.(\d{1,2})[A-Za-z]?$", sheet_number.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_index(pages: list[PageInfo], geoms: dict[int, PageGeometry]) -> list[Finding]:
    """Compare a 'LIST OF DRAWINGS' with the sheets that exist.

    Only the lines under the heading, in the same text column, are read. A range
    ('0.0-0.8') covers every numeric sheet between its ends."""
    index_page = next((p for p in pages if p.page_number in geoms and _INDEX_HEADING.search(p.text or "")), None)
    if index_page is None:
        return []
    geom = geoms[index_page.page_number]
    heading = None
    for line in group_lines(geom.words):
        for cell in split_cells(line):
            if _INDEX_HEADING.search(" ".join(w.text for w in cell)):
                heading = (cell[0].x0, cell[0].bottom)
    if heading is None:
        return []
    x0, y0 = heading
    column = [w for w in geom.words if w.top >= y0 - 1 and x0 - 4 <= w.x0 <= x0 + 330]
    lines = group_lines(column)
    listed: dict[str, str] = {}
    ranges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    seen_any = False
    for line in lines:
        text = " ".join(w.text for w in line)
        if seen_any and not _NUMERIC_ID.search(text) and not _LETTER_ID.search(text) and len(text) > 40:
            break
        for a, b in _RANGE.findall(text):
            ra, rb = _numeric_value(a), _numeric_value(b)
            if ra and rb:
                ranges.append((ra, rb)); seen_any = True
        text_no_ranges = _RANGE.sub(" ", text)
        for m in list(_LETTER_ID.finditer(text_no_ranges)) + list(_NUMERIC_ID.finditer(text_no_ranges)):
            listed[normalise_sheet_key(m.group(0))] = m.group(0)
            seen_any = True
    if len(listed) + len(ranges) < 3:
        return []          # too little read to judge a set by

    present = {normalise_sheet_key(p.sheet_number): p for p in pages if p.sheet_number}
    out: list[Finding] = []
    for key, shown in listed.items():
        if key not in present:
            out.append(Finding(
                "index_mismatch", MEDIUM, index_page.page_number, index_page.label,
                f"The drawing list names sheet {shown}, but no sheet with that number is in this set.",
                [f"Listed on {index_page.label}: {shown}"], None, "medium", key))

    def covered(key: str, number: str) -> bool:
        v = _numeric_value(number)
        return key in listed or (v is not None and any(a <= v <= b for a, b in ranges))

    for key, p in present.items():
        if p is index_page or covered(key, p.sheet_number):
            continue
        out.append(Finding(
            "index_mismatch", LOW, p.page_number, p.label,
            f"Sheet {p.sheet_number} is in the set but is not on the drawing list on {index_page.label}.",
            [f"Not found in the list on {index_page.label}"], None, "low", key))
    return out


# ── References ──────────────────────────────────────────────────────────────

_CUES = {"REFER", "SEE", "REF", "REFERENCE", "DRAWING", "DRAWINGS", "DWG", "DWGS", "SHEET", "SHEETS"}
# Structured, letter-prefixed numbers only: E2.01, A-101.1. Plain numbers are too
# often dimensions, quantities or clause numbers to call missing.
_STRICT_ID = re.compile(r"^[A-Z]{1,2}-?\d{1,2}\.\d{2}[A-Z]?$")


def check_references(page: PageInfo, geom: PageGeometry, sheet_keys: set[str], own_key: str | None) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for full in group_lines(geom.words):
        for cell in split_cells(full):
            for i, w in enumerate(cell):
                raw = w.text.strip(" ,;:()[]")
                for part in raw.split("/") if "/" in raw else [raw]:
                    if not _STRICT_ID.match(part):
                        continue
                    key = normalise_sheet_key(part)
                    if key in sheet_keys or key == own_key or key in seen:
                        continue
                    cue = any(re.sub(r"[^A-Z]", "", x.text.upper()) in _CUES for x in cell[max(0, i - 3):i])
                    if not cue:
                        continue
                    seen.add(key)
                    line = " ".join(re.sub(r"\(cid:\d+\)", " ", x.text) for x in cell)[:110]
                    x, y, ww, hh = geom.norm_box(w.x0, w.top, w.x1, w.bottom)
                    out.append(Finding(
                        "broken_reference", MEDIUM, page.page_number, page.label,
                        f"Refers to sheet {part}, which is not in this set (the set may be incomplete, or the "
                        f"reference is out of date).",
                        [f"“{' '.join(line.split())}”"], (x, y, ww, hh), "medium", key))
    return out


# ── Scale and dimensions ────────────────────────────────────────────────────

def check_scale(page: PageInfo, sheet: SheetScales) -> list[Finding]:
    out = []
    measured = [s for s in sheet.scales if s.source == "measured"]
    for sc in sheet.scales:
        if sc.status != "conflict" or not measured:
            continue
        m = measured[0]
        out.append(Finding(
            "scale_conflict", HIGH, page.page_number, page.label,
            f"The sheet states {sc.text}, but its own dimensions agree on {m.text}. The stated scale is wrong or "
            f"the sheet was printed at a reduced size, so anything scaled off it will be wrong.",
            [f"Stated: {sc.text}", f"From dimensions: {m.text} ({m.support} agree)"], None,
            "high" if m.support >= 4 else "medium", f"{sc.ratio:.0f}"))
    return out


def check_dimensions(page: PageInfo, geom: PageGeometry, sheet: SheetScales, samples) -> list[Finding]:
    """A dimension whose text differs from the length drawn between its ticks, on a
    sheet whose other dimensions agree with each other.

    Only large dimensions are judged (small ones are usually notes like 4"), and one
    that matches any scale on the sheet is a detail at a different scale, not an error."""
    trusted = [s for s in sheet.scales if s.status in ("verified", "measured") and s.support >= 3]
    if not trusted:
        return []
    base = max(trusted, key=lambda s: s.support)
    out = []
    for s in samples:
        if s.value_in < DIM_MIN_VALUE_IN:
            continue
        if any(abs(s.ratio / sc.ratio - 1) <= AGREE * 2 for sc in sheet.scales if sc.status != "conflict"):
            continue
        drawn_in = s.distance_pt / PT_PER_IN * base.ratio
        diff = drawn_in - s.value_in
        if abs(diff) / s.value_in < DIM_REL_TOLERANCE or abs(diff) < DIM_MIN_ABS_IN:
            continue
        x0 = min(s.p1[0], s.p2[0]); x1 = max(s.p1[0], s.p2[0])
        y = (s.p1[1] + s.p2[1]) / 2
        x, yy, w, h = geom.norm_box(x0, y - 10, x1, y + 6)
        metric = base.system == "metric"
        fmt = (lambda v: format_metric(v)) if metric else (lambda v: format_imperial(v, 2))
        out.append(Finding(
            "dimension_mismatch", MEDIUM if abs(diff) / s.value_in > 0.2 else LOW, page.page_number, page.label,
            f"A dimension reads {fmt(s.value_in)}, but at this sheet's scale ({base.text}, confirmed by "
            f"{base.support} other dimensions) the length drawn between its ends is about {fmt(drawn_in)}.",
            [f"Dimension text: {s.text.strip()}", f"Drawn: ≈ {fmt(drawn_in)}"], (x, yy, w, h),
            "medium" if base.support >= 4 else "low", f"{s.x:.0f},{s.y:.0f}"))
    return out


def run_checks(pages: list[PageInfo], geoms: dict[int, PageGeometry]) -> list[Finding]:
    """All checks over the pages whose geometry is available."""
    from backend.services.measure.scale import find_dimension_samples

    findings = check_duplicate_sheets(pages) + check_index(pages, geoms)
    sheet_keys = {normalise_sheet_key(p.sheet_number) for p in pages if p.sheet_number}
    for p in pages:
        geom = geoms.get(p.page_number)
        if geom is None:
            continue
        own = normalise_sheet_key(p.sheet_number) if p.sheet_number else None
        findings += check_references(p, geom, sheet_keys, own)
        sheet = analyse_sheet(geom)
        findings += check_scale(p, sheet)
        findings += check_dimensions(p, geom, sheet, find_dimension_samples(geom))
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.page_number, f.check, f.key))
    return findings
