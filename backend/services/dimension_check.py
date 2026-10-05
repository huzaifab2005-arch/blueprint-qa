import re
from collections import defaultdict

# Feet-and-inches ("12'-6\"", "12'-6 1/2\"", "12'") or bare inches ("8\"").
_DIM_RE = re.compile(
    r"""(?:(?P<ft>\d+)\s*'\s*(?:-?\s*(?P<in>\d+)(?:\s+(?P<num>\d+)/(?P<den>\d+))?\s*")?)
        |(?:(?P<bin>\d+)(?:\s+(?P<bnum>\d+)/(?P<bden>\d+))?\s*")""",
    re.VERBOSE,
)
_IGNORED_LABELS = {"TYP", "SIM", "NOTE", "NOTES", "MIN", "MAX", "NTS", "EQ", "TYP SIM"}


def _to_inches(m: re.Match) -> float:
    if m.group("ft") is not None:
        total = int(m.group("ft")) * 12.0
        if m.group("in") is not None:
            total += int(m.group("in"))
            if m.group("num"):
                total += int(m.group("num")) / int(m.group("den"))
        return total
    total = float(m.group("bin"))
    if m.group("bnum"):
        total += int(m.group("bnum")) / int(m.group("bden"))
    return total


def _fmt(m: re.Match) -> str:
    return re.sub(r"\s+", " ", m.group(0)).strip()


def find_dimension_conflicts(ocr_text: str) -> list[dict]:
    """Flag the same labelled dimension given two different values.

    Works on OCR text only, which has no geometry, so it can only relate two
    dimensions when they carry the same text label (e.g. "CORRIDOR WIDTH 6'-0\"
    ... CORRIDOR WIDTH 5'-0\""). Unlabelled dimensions are never compared:
    without positions there is no way to tell whether two numbers describe the
    same element.
    """
    if not ocr_text:
        return []

    by_label: dict[str, dict[float, str]] = defaultdict(dict)
    for line in ocr_text.splitlines():
        matches = list(_DIM_RE.finditer(line))
        if len(matches) != 1:
            continue  # no dimension, or several (e.g. "12'-0\" x 10'-0\"") - ambiguous
        m = matches[0]
        label = re.sub(r"[^A-Z0-9 ]", " ", (line[: m.start()] + " " + line[m.end():]).upper())
        label = re.sub(r"\s+", " ", label).strip()
        if len(label) < 3 or not re.search(r"[A-Z]{2}", label) or label in _IGNORED_LABELS:
            continue
        by_label[label].setdefault(_to_inches(m), _fmt(m))

    issues = []
    for label, values in by_label.items():
        if len(values) < 2:
            continue
        shown = " vs ".join(values[v] for v in sorted(values))
        issues.append({
            "issue_type": "dimension_mismatch",
            "severity": "high",
            "description": f'"{label.title()}" is dimensioned inconsistently: {shown}.',
            "location_hint": "",
        })
    return issues


_FEET_RE = re.compile(
    r"""^(?P<ft>\d+)\s*'\s*(?:-?\s*(?P<in>\d+)(?:\s+(?P<num>\d+)/(?P<den>\d+))?\s*")?$"""
)


def _row_key(word: dict):
    return (word["block"], word["par"], word["line"])


def _position_hint(cx: float, cy: float, width: int, height: int) -> str:
    vert = "top" if cy < height / 3 else "bottom" if cy > 2 * height / 3 else "middle"
    horiz = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "center"
    return f"{vert}-{horiz}" if vert != "middle" or horiz != "center" else "center"


def find_aligned_dimension_conflicts(
    words: list[dict], page_width: int, page_height: int, max_issues: int = 3
) -> list[dict]:
    """Flag horizontal dimension strings stacked over the same span with different values.

    `words` are OCR words with left/top/width/height and block/par/line ids. Two
    feet-and-inches strings whose text is centred on the same x position but
    sits on different rows are normally dimensioning the same span (e.g. an
    overall dimension at the top of a wall and again at the bottom), so
    different values are a likely contradiction.

    Limitations: only horizontal text is considered (rotated vertical
    dimensions are not read reliably by OCR), only feet-and-inches strings are
    used (bare inches are member sizes), and dimension lines are not seen, so
    this is a heuristic reported at medium severity.
    """
    if not words or page_width <= 0:
        return []

    # Rows containing "=" or "scale" are scale notes such as 1/4" = 1'-0".
    skip_rows = {
        _row_key(w)
        for w in words
        if w["text"] == "=" or "SCALE" in w["text"].upper()
    }

    dims = []
    for w in words:
        text = w["text"].strip()
        m = _FEET_RE.match(text)
        if not m or _row_key(w) in skip_rows:
            continue
        if w["width"] <= w["height"]:
            continue  # not horizontal text
        value = int(m.group("ft")) * 12.0
        if m.group("in") is not None:
            value += int(m.group("in"))
            if m.group("num"):
                value += int(m.group("num")) / int(m.group("den"))
        dims.append({
            "text": text,
            "value": value,
            "cx": w["left"] + w["width"] / 2,
            "cy": w["top"] + w["height"] / 2,
            "h": w["height"],
            "row": _row_key(w),
        })

    tol_x = max(15.0, 0.02 * page_width)
    issues = []
    for i, a in enumerate(dims):
        for b in dims[i + 1:]:
            if a["row"] == b["row"] or abs(a["cx"] - b["cx"]) > tol_x:
                continue
            if abs(a["cy"] - b["cy"]) < 3 * max(a["h"], b["h"]):
                continue  # adjacent text, likely a chain rather than a repeated span
            if abs(a["value"] - b["value"]) < 0.5:
                continue
            first, second = sorted((a, b), key=lambda d: d["cy"])
            issues.append({
                "issue_type": "dimension_mismatch",
                "severity": "medium",
                "description": (
                    f"Dimensions {first['text']} and {second['text']} are stacked over the "
                    "same span but give different values; one wall or run appears to be "
                    "dimensioned two ways."
                ),
                "location_hint": _position_hint(
                    first["cx"], (first["cy"] + second["cy"]) / 2, page_width, page_height
                ),
            })
            if len(issues) >= max_issues:
                return issues
    return issues
