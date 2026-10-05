"""Find a sheet's drawing scale, and check it against the sheet's own dimensions.

A scale is a ratio: real length / paper length (3/16" = 1'-0" is 64; 1:100 is 100).
A measured length is then points/72 inches on paper times the ratio. The danger is
a scale that is wrong for what is on the sheet, which silently scales every
number: a sheet may carry several scales (a plan and its details), may say
"AS NOTED", or may have been printed at a reduced size so its stated scale no
longer holds. So the stated scale is never trusted alone. It is compared with the
scale implied by the sheet's dimension annotations:

    12'-7"   drawn as a line with a tick mark at each end

The distance between the two ticks is the dimension, exactly (the line itself
overshoots the ticks by a fixed amount, so its length is not usable). Every such
dimension gives one sample of the real scale; samples that agree confirm a
scale, whatever the sheet claims.

Pure functions over PageGeometry; no I/O. Limits: only horizontal dimension text
is read (rotated text arrives as single characters), and dimensions drawn without
tick marks give weaker samples that are not used to confirm a scale.
"""
import math
import re
from dataclasses import dataclass, field

from backend.services.counting.pdf_geometry import PageGeometry, Shape, Word
from backend.services.counting.symbol_counter import cell_box, group_lines, split_cells
from backend.services.measure.units import parse_length

PT_PER_IN = 72.0
AGREE = 0.03          # samples within 3% of a ratio support it
MIN_SUPPORT = 3       # dimensions needed to call a scale verified

_NTS_RE = re.compile(r"\b(?:N\.?T\.?S\.?|NOT\s+TO\s+SCALE|AS\s+NOTED|VARIES|VARIOUS)\b", re.I)
_IMPERIAL_SCALE_RE = re.compile(
    r"""(?P<paper>(?:\d+\s+)?\d+\s*/\s*\d+|\d+(?:\.\d+)?)\s*["”″]?\s*=\s*
        (?P<real>\d+(?:\.\d+)?\s*['’′](?:\s*-?\s*\d+(?:\s+\d+/\d+)?\s*["”″]?)?|\d+(?:\.\d+)?\s*["”″])""",
    re.VERBOSE,
)
# 1:50 only. A slash is a fraction (SCALE: 1/4" = 1'-0"), never a ratio.
_METRIC_SCALE_RE = re.compile(r"\bSCALE\s*[:=]?\s*1\s*:\s*(?P<n>\d{1,5})\b|^\s*1\s*:\s*(?P<m>\d{1,5})\s*$", re.I)
# Some CAD fonts have no glyph for "=" and the PDF text comes out as "(cid:32)".
_CID_EQUALS_RE = re.compile(r"""(?<=["”″])\s*\(cid:\d+\)\s*(?=\d+\s*['’′])""")


@dataclass
class StatedScale:
    text: str
    ratio: float | None            # None for NTS / AS NOTED / VARIES
    box: tuple[float, float, float, float]   # pt, top-left origin


@dataclass
class DimSample:
    text: str
    value_in: float
    distance_pt: float             # tick to tick
    x: float
    y: float
    ratio: float                   # implied real/paper
    p1: tuple[float, float] = (0.0, 0.0)   # the two tick marks (pt)
    p2: tuple[float, float] = (0.0, 0.0)


@dataclass
class ScaleInfo:
    ratio: float
    text: str                      # how to show it: 'SCALE: 3/16" = 1'-0"' or '1:98.9 from 12 dimensions'
    source: str                    # stated | measured
    status: str                    # verified | stated | measured | conflict
    support: int = 0               # dimensions agreeing with this ratio
    system: str = "imperial"       # which units to show first: imperial | metric
    # Relative spread of the dimension samples behind this scale (0.005 = 0.5%). Real
    # sheets agree with their own dimensions only to about this much, so it bounds how
    # exactly anything measured with the scale can be known. None = nothing to judge by.
    spread: float | None = None
    anchors: list[tuple[float, float]] = field(default_factory=list)   # pt positions this scale applies near


@dataclass
class SheetScales:
    scales: list[ScaleInfo]
    stated: list[StatedScale]
    samples: int
    notes: list[str]
    primary: int | None            # index into scales, or None

    @property
    def primary_scale(self) -> ScaleInfo | None:
        return self.scales[self.primary] if self.primary is not None else None


# ── Stated scales ───────────────────────────────────────────────────────────

def parse_scale_text(text: str) -> tuple[float | None, bool]:
    """(ratio, recognised). 'AS NOTED' is recognised with no ratio."""
    t = _CID_EQUALS_RE.sub(" = ", " ".join(text.split()))
    m = _IMPERIAL_SCALE_RE.search(t)
    if m:
        paper_txt = m.group("paper").strip()
        paper = parse_length(paper_txt if re.search(r"""["”″]""", paper_txt) else paper_txt + '"')
        real_txt = m.group("real").strip()
        real = parse_length(real_txt if re.search(r"""['’′"”″]""", real_txt) else real_txt + "'")
        if paper and real and paper > 0 and real > 0:
            return real / paper, True
    m = _METRIC_SCALE_RE.search(t)
    if m:
        n = int(m.group("n") or m.group("m"))
        if n > 0:
            return float(n), True
    if _NTS_RE.search(t):
        return None, True
    return None, False


def find_stated_scales(geom: PageGeometry) -> list[StatedScale]:
    """Every scale statement on the sheet. Only text that says SCALE (or is a bare
    imperial 'a" = b'-c"' statement) counts: a ratio like 1:50 elsewhere is not one."""
    out: list[StatedScale] = []
    for line in group_lines(geom.words):
        for cell in split_cells(line, gap=24.0):
            text = " ".join(w.text for w in cell)
            has_label = bool(re.search(r"\bSCALE\b", text, re.I))
            ratio, ok = parse_scale_text(text)
            if not ok:
                continue
            if ratio is None and not has_label:
                continue
            if ratio is not None and not has_label and not _IMPERIAL_SCALE_RE.search(text):
                continue
            shown = text[re.search(r"\bSCALE\b", text, re.I).start():] if has_label else text
            shown = " ".join(_CID_EQUALS_RE.sub(" = ", shown).split())
            out.append(StatedScale(shown, ratio, cell_box(cell)))
    return out


# ── Dimension strings ───────────────────────────────────────────────────────

_DIM_BASE_RE = re.compile(r"""^\d+['’′](?:-?\d+)?["”″]?$|^\d+["”″]$""")


def assemble_dimensions(words: list[Word]) -> list[tuple[str, float, Word, tuple[float, float, float, float]]]:
    """(text, inches, base word, box) for each horizontal dimension string.

    Fractions are drawn as a raised numerator and a lowered denominator, separate
    words to the right of the whole part: 5'-0 [1][2] ". They are joined here.
    """
    ordered = sorted(words, key=lambda w: (round(w.top), w.x0))
    used: set[int] = set()
    out = []
    for w in ordered:
        if id(w) in used or not _DIM_BASE_RE.match(w.text.strip()):
            continue
        text = w.text.strip()
        right = w.x1
        box = [w.x0, w.top, w.x1, w.bottom]
        height = w.bottom - w.top
        parts = [w]
        # numerator, denominator, closing mark: small words immediately to the right
        cand = sorted(
            (x for x in words if id(x) not in used and x is not w and -1.5 <= x.x0 - right <= 14
             and abs(x.cy - w.cy) <= height * 0.9 and (x.bottom - x.top) <= height * 1.05),
            key=lambda x: x.x0,
        )
        nums = []
        for x in cand:
            t = x.text.strip()
            if re.fullmatch(r"\d{1,2}", t) and len(nums) < 2:
                nums.append(x)
                parts.append(x)
                right = max(right, x.x1)
            elif re.fullmatch(r"\d{1,2}\s*/\s*\d{1,2}", t) and not nums:
                nums.append(x)
                parts.append(x)
                right = max(right, x.x1)
            elif t in ('"', "”", "″") and (nums or not text.endswith('"')):
                parts.append(x)
                right = max(right, x.x1)
                break
        if len(nums) == 2 and nums[0].top < nums[1].top:
            text += f" {nums[0].text}/{nums[1].text}"
        elif len(nums) == 1 and "/" in nums[0].text:
            text += f" {nums[0].text}"
        elif nums:
            parts = [p for p in parts if p not in nums]    # unrelated digits: leave them out
        if not text.endswith(('"', "”", "″")):
            text += '"'
        value = parse_length(text)
        if value is None or value <= 0:
            continue
        for p in parts:
            used.add(id(p))
        box = [min(p.x0 for p in parts), min(p.top for p in parts), max(p.x1 for p in parts), max(p.bottom for p in parts)]
        out.append((text, value, w, tuple(box)))
    return out


# ── Dimension lines and their ticks ─────────────────────────────────────────

def _ticks_near(shapes: list[Shape], line: Shape, end_x: float, side: int) -> Shape | None:
    """A small mark centred on the dimension line, just inside one of its ends."""
    best, best_d = None, 1e9
    for s in shapes:
        if not (3 <= s.size <= 14) or abs(s.cy - line.cy) > 2.2:
            continue
        inside = (s.cx - end_x) * side            # positive = inside the line's span
        if not (-1.0 <= inside <= 12):
            continue
        d = abs(inside - 6)
        if d < best_d:
            best, best_d = s, d
    return best


def find_dimension_samples(geom: PageGeometry) -> list[DimSample]:
    """One scale sample per dimension whose line has a tick at each end."""
    horizontal = [s for s in geom.shapes if s.kind == "line" and s.h <= 0.3 and s.w >= 14]
    small = [s for s in geom.shapes if 3 <= s.size <= 14]
    samples: list[DimSample] = []
    for text, value, base, box in assemble_dimensions(geom.words):
        cx, bottom = (box[0] + box[2]) / 2, box[3]
        best = None
        for ln in horizontal:
            gap = ln.cy - bottom
            if not (-2.0 <= gap <= 16.0) or not (ln.x0 - 1 <= cx <= ln.x1 + 1) or ln.w < (box[2] - box[0]) * 1.1:
                continue
            if best is None or gap < best[0]:
                best = (gap, ln)
        if best is None:
            continue
        ln = best[1]
        t1 = _ticks_near(small, ln, ln.x0, +1)
        t2 = _ticks_near(small, ln, ln.x1, -1)
        if t1 is None or t2 is None or t1 is t2:
            continue
        distance = abs(t2.cx - t1.cx)
        if distance < 8:
            continue
        ratio = value / (distance / PT_PER_IN)
        samples.append(DimSample(text, value, distance, cx, ln.cy, ratio, (t1.cx, t1.cy), (t2.cx, t2.cy)))
    return samples


def _cluster(samples: list[DimSample]) -> list[list[DimSample]]:
    """Group samples whose ratios agree within AGREE, largest group first."""
    remaining = sorted(samples, key=lambda s: s.ratio)
    clusters: list[list[DimSample]] = []
    while remaining:
        best: list[DimSample] = []
        for i, s in enumerate(remaining):
            group = [t for t in remaining[i:] if t.ratio <= s.ratio * (1 + 2 * AGREE)]
            if len(group) > len(best):
                best = group
        clusters.append(best)
        remaining = [s for s in remaining if s not in best]
    clusters.sort(key=len, reverse=True)
    return clusters


def _spread(ratios: list[float]) -> float | None:
    """Relative standard deviation, floored: a handful of samples never prove perfection."""
    if len(ratios) < 2:
        return None
    mean = sum(ratios) / len(ratios)
    var = sum((r - mean) ** 2 for r in ratios) / (len(ratios) - 1)
    return max(math.sqrt(var) / mean, 0.002)


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _fmt_ratio(r: float) -> str:
    return f"1:{r:.0f}" if abs(r - round(r)) < 0.05 * max(r, 1) / 10 or r >= 100 else f"1:{r:.1f}"


def analyse_sheet(geom: PageGeometry) -> SheetScales:
    """Stated scales, dimension samples, and the verdict on each scale."""
    stated = find_stated_scales(geom)
    samples = find_dimension_samples(geom)
    notes: list[str] = []
    scales: list[ScaleInfo] = []

    def support_for(ratio: float) -> list[DimSample]:
        return [s for s in samples if abs(s.ratio / ratio - 1) <= AGREE]

    seen: list[float] = []
    for st in stated:
        if st.ratio is None:
            notes.append(f"The sheet says “{st.text.strip()}”: some drawings on it are not to scale, so "
                         f"measure only against a dimension you can check.")
            continue
        if any(abs(st.ratio / r - 1) < 0.001 for r in seen):
            # the same scale stated again (several details): add its position
            for sc in scales:
                if abs(sc.ratio / st.ratio - 1) < 0.001:
                    sc.anchors.append(((st.box[0] + st.box[2]) / 2, (st.box[1] + st.box[3]) / 2))
            continue
        seen.append(st.ratio)
        sup = support_for(st.ratio)
        scales.append(ScaleInfo(
            ratio=st.ratio, text=" ".join(st.text.split()), source="stated",
            system="metric" if re.search(r"\b1\s*:\s*\d", st.text) and not re.search(r"['’′\"”″]", st.text) else "imperial",
            status="verified" if len(sup) >= MIN_SUPPORT else "stated", support=len(sup),
            spread=_spread([x.ratio for x in sup]) if len(sup) >= MIN_SUPPORT else None,
            anchors=[((st.box[0] + st.box[2]) / 2, (st.box[1] + st.box[3]) / 2)] + [(s.x, s.y) for s in sup],
        ))

    # Scales the dimensions agree on that no label explains (a reduced print, or no label).
    for group in _cluster(samples):
        if len(group) < MIN_SUPPORT:
            break
        ratio = _median([s.ratio for s in group])
        if any(abs(sc.ratio / ratio - 1) <= AGREE for sc in scales):
            continue
        scales.append(ScaleInfo(
            ratio=ratio, text=f"{_fmt_ratio(ratio)} (from {len(group)} dimensions)", source="measured",
            status="measured", support=len(group), anchors=[(s.x, s.y) for s in group],
            spread=_spread([s.ratio for s in group]),
        ))

    # A stated scale that nothing supports, while the dimensions clearly agree on another.
    measured = [sc for sc in scales if sc.source == "measured"]
    if measured:
        for sc in scales:
            if sc.source == "stated" and sc.status == "stated":
                sc.status = "conflict"
                notes.append(
                    f"Stated scale {sc.text} does not match the sheet's own dimensions, which agree on "
                    f"{measured[0].text}. The sheet may have been printed at a reduced size. Measure "
                    f"with the measured scale, or calibrate against a known dimension.")
    if not scales:
        notes.append("No scale could be found or confirmed on this sheet. Calibrate against a known dimension "
                     "before measuring.")

    def rank(i: int) -> tuple:
        sc = scales[i]
        order = {"verified": 0, "measured": 1, "stated": 2, "conflict": 3}[sc.status]
        return (order, -sc.support)

    primary = min(range(len(scales)), key=rank) if scales else None
    # A conflicting stated scale is never primary when a measured one exists.
    return SheetScales(scales=scales, stated=stated, samples=len(samples), notes=notes, primary=primary)
