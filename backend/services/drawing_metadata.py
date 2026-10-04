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
    """Canonical display form: no inner space, 'E-2.01' -> 'E2.01'.

    Letter-prefixed numbers are upper-cased; purely numeric ones keep their case
    so '1.1a' is shown as the drawing shows it.
    """
    v = re.sub(r"\s+", "", value)
    if re.match(r"^\d", v):
        return v
    v = v.upper()
    # Drop a hyphen that sits between the discipline letters and a dotted number.
    return re.sub(r"^([A-Z]{1,3})-(?=\d+\.\d)", r"\1", v)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# A whole line that is a sheet number: E2.01, A-101, or a plain numeric 1.3 / 1.1a / 4.0.
# Plain numbers are only trusted next to a title-block label (see detect_title_block).
SHEET_LINE_RE = re.compile(
    r"^(?:[A-Z]{1,3}[-\s]?\d{1,3}(?:\.\d{1,3})?[A-Z]?|\d{1,2}\.\d{1,2}[A-Za-z]?)$"
)
# Labels that sit next to the sheet number in a title block.
TITLE_BLOCK_LABEL_RE = re.compile(
    r"^(?:DESCRIPTION|DRAWING\s*(?:TITLE|NO\.?|NUMBER)|SHEET\s*(?:TITLE|NO\.?|NUMBER|#)?|DWG\.?\s*(?:NO\.?|#)?)\s*:?$",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", re.I)


def looks_like_title(line: str) -> bool:
    """A drawing title: short, uppercase-ish words, not a sentence, date or number."""
    ln = line.strip()
    if not 4 <= len(ln) <= 60 or ln.endswith(".") or ":" in ln or _DATE_RE.search(ln):
        return False
    if SHEET_LINE_RE.match(ln.upper()) or len(ln.split()) > 8:
        return False
    letters = [c for c in ln if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) >= 0.8


def detect_title_block(raw_text: str) -> tuple[str | None, str | None]:
    """(sheet_number, title) read from a title block, anchored on its labels.

    Works on text in content-stream/reading order (pdftotext -raw), where a title
    block comes out as 'TITLE / SHEET NO / DESCRIPTION: / SCALE: ...' or the label
    followed by its value. Anchoring on the label is what makes plain numeric
    sheet numbers (1.3, 4.0, 0.1) safe to accept: the same string elsewhere on a
    sheet is a dimension or a note number.
    """
    if not raw_text:
        return None, None
    lines = _lines(raw_text)
    found: tuple[str | None, str | None] = (None, None)
    for i, ln in enumerate(lines):
        if not TITLE_BLOCK_LABEL_RE.match(ln):
            continue
        # Nearest sheet-number line within three lines, before the label first.
        number_at = None
        for off in (-1, -2, -3, 1, 2, 3):
            j = i + off
            if 0 <= j < len(lines) and SHEET_LINE_RE.match(lines[j].upper()) and not TITLE_BLOCK_LABEL_RE.match(lines[j]):
                number_at = j
                break
        if number_at is None:
            continue
        title = None
        for j in (number_at - 1, number_at + 1):
            if 0 <= j < len(lines) and not TITLE_BLOCK_LABEL_RE.match(lines[j]) and looks_like_title(lines[j]):
                title = re.sub(r"\s{2,}", " ", lines[j])
                # A wrapped title continues on the line below its first part:
                # "GENERAL REQUIREMENTS" / "& SPECIFICATIONS".
                if j == number_at - 1 and re.match(r"^(&|AND\b|/)", title) and j - 1 >= 0 \
                        and looks_like_title(lines[j - 1]):
                    title = f"{lines[j - 1]} {title}"
                break
        found = (format_sheet_number(lines[number_at]), title)
    return found


def detect_sheet_metadata(raw_text: str, text: str) -> tuple[str | None, str | None]:
    """(sheet_number, sheet_title) for a page. `raw_text` is reading-order text
    (title blocks anchor on labels there); `text` is the layout text used for the
    heuristics that need no anchor. Either may be empty."""
    number, title = detect_title_block(raw_text or text)
    if number is None:
        number = detect_sheet_number(text)
    # Without a sheet number there is no title block to read; a guessed title from
    # body text (a cover sheet's drawing index, say) is worse than none.
    if title is None and number is not None:
        title = detect_sheet_title(text, number)
    return number, title


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
    # The candidate must be a whole line: a sheet number sitting inside a sentence
    # ("MOUNT AT 18 A.F.F. 1.6") is a reference, not this sheet's number.
    best: str | None = None
    for ln in _lines(text.upper())[-30:]:
        if SHEET_ID_RE.fullmatch(ln) and ("." in ln or re.match(r"^[A-Z]{1,2}-\d{2,3}$", ln)):
            if ln.split("-")[0].split(".")[0] not in _NOT_SHEETS:
                best = ln
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
        if not looks_like_title(ln):
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
