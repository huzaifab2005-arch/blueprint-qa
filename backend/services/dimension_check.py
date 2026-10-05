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
