"""Heuristics that pull sheet metadata (sheet number, title) out of a page's text.

Pure functions, no I/O, so they are cheap to unit test. These are heuristics:
title blocks vary a lot between firms, so a miss falls back to "Page N" rather
than guessing.
"""
import re

# A1.01, E2.03, M2.02, A-101, E-2.01, FP1.1, S101 ...
SHEET_ID = r"[A-Z]{1,3}[-\s]?\d{1,3}(?:\.\d{1,3})?(?:[A-Z])?"
SHEET_ID_RE = re.compile(rf"(?<![A-Z0-9.\-]){SHEET_ID}(?![A-Z0-9\-]|\.\d)")

# "SHEET NO: E2.01", "SHEET NUMBER" on one line and the id on the next, etc.
LABELLED_RE = re.compile(
    rf"SHEET\s*(?:NO\.?|NUMBER|#|NUM\.?)?\s*[:\-]?\s*\n?\s*({SHEET_ID})(?![A-Z0-9])",
    re.IGNORECASE,
)

TITLE_KEYWORDS = (
    "PLAN", "SCHEDULE", "SCHEDULES", "DETAIL", "DETAILS", "SECTION", "SECTIONS",
    "ELEVATION", "ELEVATIONS", "LEGEND", "NOTES", "DIAGRAM", "RISER", "SITE",
    "COVER", "INDEX", "LAYOUT", "DEMOLITION", "REFLECTED",
)

# Words that look like a sheet id but are not one.
_NOT_SHEETS = {"NO", "NO.", "SCALE", "DATE", "REV", "DWG", "SHEET"}


def normalise_sheet_key(value: str) -> str:
    """'E-2.01', 'e2.01' and 'E2.01' all map to 'E201'."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def format_sheet_number(value: str) -> str:
    """Canonical display form: uppercase, no inner space, 'E-2.01' -> 'E2.01'."""
    v = re.sub(r"\s+", "", value.upper())
    # Drop a hyphen that sits between the discipline letters and a dotted number.
    return re.sub(r"^([A-Z]{1,3})-(?=\d+\.\d)", r"\1", v)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def detect_sheet_number(text: str) -> str | None:
    """Best-effort sheet number for one page of text, or None."""
    if not text:
        return None

    labelled = LABELLED_RE.findall(text.upper())
    if labelled:
        return format_sheet_number(labelled[-1])

    # Title blocks sit at the bottom/right, which in reading order is the end of
    # the text. Only trust candidates with a dot (E2.01) or a letter-hyphen-number
    # form (A-101); bare tokens like "RTU1" are far too noisy in equipment text.
    tail = "\n".join(_lines(text.upper())[-30:])
    best: str | None = None
    for m in SHEET_ID_RE.finditer(tail):
        cand = m.group(0).strip()
        if "." in cand or re.match(r"^[A-Z]{1,2}-\d{2,3}$", cand):
            if cand.split("-")[0].split(".")[0] in _NOT_SHEETS:
                continue
            best = cand
    return format_sheet_number(best) if best else None


def detect_sheet_title(text: str, sheet_number: str | None = None) -> str | None:
    """Pick the title-block line that looks most like a drawing title."""
    if not text:
        return None
    candidates = _lines(text)[-40:]
    key = normalise_sheet_key(sheet_number) if sheet_number else None
    best: str | None = None
    for ln in candidates:
        up = ln.upper()
        if len(ln) < 6 or len(ln) > 80:
            continue
        if key and normalise_sheet_key(ln) == key:
            continue
        # Mostly-uppercase lines containing a drawing-type keyword.
        letters = [c for c in ln if c.isalpha()]
        if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
            continue
        words = set(re.findall(r"[A-Z]+", up))
        if words & set(TITLE_KEYWORDS):
            best = re.sub(r"\s{2,}", " ", ln)
    return best


def page_label(page_number: int, sheet_number: str | None) -> str:
    return sheet_number or f"Page {page_number}"
