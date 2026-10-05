"""Lengths: parsing what a drawing writes, and formatting what a person reads.

Internally every length is in inches (a float), whatever the drawing used. A
drawing writes 12'-7", 5'-0 1/2", 24", 3 1/2", 1200 mm or 2.5 m; a person wants the
answer back in the same system, and usually the other one as well.

Pure functions; no I/O.
"""
import re
from fractions import Fraction

MM_PER_IN = 25.4

_FRAC = r"(?:\d+\s+)?\d+\s*/\s*\d+"
_NUM = r"\d+(?:\.\d+)?"
# feet and/or inches: 12'-7", 5'-0 1/2", 4', 24", 3 1/2", 10'-6 3/4"
_IMPERIAL_RE = re.compile(
    rf"""^\s*
        (?:(?P<ft>{_NUM})\s*['’′]\s*-?\s*)?
        (?:(?P<in_whole>{_NUM})(?:\s+(?P<in_frac>\d+\s*/\s*\d+))?\s*(?:["”″]|in\b|inch(?:es)?\b)?)?
        \s*$""",
    re.VERBOSE | re.IGNORECASE,
)
_FRAC_ONLY_RE = re.compile(r"^\s*(?P<n>\d+)\s*/\s*(?P<d>\d+)\s*[\"”″]?\s*$")
_METRIC_RE = re.compile(r"^\s*(?P<v>\d+(?:[.,]\d+)?)\s*(?P<u>mm|cm|m)\b\.?\s*$", re.IGNORECASE)


def _frac(text: str) -> float:
    n, d = (int(x) for x in re.split(r"\s*/\s*", text.strip()))
    return n / d if d else 0.0


def parse_length(text: str) -> float | None:
    """A length as written on a drawing, in inches, or None if it is not one.

    Understood: 12'-7", 12' 7", 5'-0 1/2", 4', 24", 3 1/2", 7/8", 1200 mm, 120 cm, 2.5 m.
    A bare number is not a length (it could be anything), except when it carries a unit.
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _METRIC_RE.match(t)
    if m:
        v = float(m.group("v").replace(",", "."))
        mm = {"mm": v, "cm": v * 10, "m": v * 1000}[m.group("u").lower()]
        return mm / MM_PER_IN
    m = _FRAC_ONLY_RE.match(t)
    if m:
        return int(m.group("n")) / int(m.group("d")) if int(m.group("d")) else None
    m = _IMPERIAL_RE.match(t)
    if not m or not (m.group("ft") or m.group("in_whole")):
        return None
    has_marks = ("'" in t or "’" in t or "′" in t or '"' in t or "”" in t or "″" in t or re.search(r"\bin", t, re.I))
    if not has_marks:
        return None
    inches = float(m.group("ft") or 0) * 12
    if m.group("in_whole"):
        inches += float(m.group("in_whole"))
    if m.group("in_frac"):
        inches += _frac(m.group("in_frac"))
    return inches


def format_imperial(inches: float, denominator: int = 16) -> str:
    """12.583 ft -> 12'-7", 150.5 -> 12'-6 1/2". Rounded to 1/denominator inch."""
    if inches < 0:
        return "-" + format_imperial(-inches, denominator)
    total = Fraction(round(inches * denominator), denominator)
    feet = int(total // 12)
    rest = total - feet * 12
    whole = int(rest)
    frac = rest - whole
    inch_txt = str(whole) if frac == 0 else (f"{whole} {frac.numerator}/{frac.denominator}" if whole else f"{frac.numerator}/{frac.denominator}")
    return f"{feet}'-{inch_txt}\"" if feet else f"{inch_txt}\""


def format_metric(inches: float) -> str:
    mm = inches * MM_PER_IN
    if abs(mm) >= 10000:
        return f"{mm / 1000:.2f} m"
    if abs(mm) >= 1000:
        return f"{mm / 1000:.3f} m"
    return f"{mm:.0f} mm"


def format_area(sq_inches: float) -> tuple[str, str]:
    """(imperial, metric) for an area given in square inches."""
    sf = sq_inches / 144
    sm = sq_inches * (MM_PER_IN / 1000) ** 2
    return f"{sf:,.1f} sf", f"{sm:,.2f} m²"
