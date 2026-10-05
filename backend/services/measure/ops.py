"""Measuring on a sheet: length, path length, area, snapping, and which scale applies.

Points arrive as fractions (0..1) of the displayed page and are converted to PDF
points here. A length in points is points / 72 paper inches; times the scale ratio
it is real inches. Everything returned carries the scale it used and how that scale
was checked, so a number is never separated from the assumption behind it.

Pure functions; no I/O.
"""
import math
from dataclasses import dataclass, field

from backend.services.counting.pdf_geometry import PageGeometry
from backend.services.measure.scale import PT_PER_IN, ScaleInfo
from backend.services.measure.units import format_area, format_imperial, format_metric

CLICK_PT_UNSNAPPED = 1.0     # how closely a person can place a point by eye on the image
CLICK_PT_SNAPPED = 0.15      # a snapped point is a vertex of the drawing itself
# When nothing on the sheet confirms the scale, assume the label could be a couple of
# percent off rather than pretend it is exact. A calibrated scale is exact by definition.
UNCHECKED_SPREAD = 0.02


def to_pt(geom: PageGeometry, points: list[list[float]]) -> list[tuple[float, float]]:
    return [(x * geom.width, y * geom.height) for x, y in points]


def path_length_pt(pts: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def polygon_area_pt2(pts: list[tuple[float, float]]) -> float:
    """Shoelace area of a polygon (closed implicitly)."""
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def real_inches(length_pt: float, ratio: float) -> float:
    return length_pt / PT_PER_IN * ratio


@dataclass
class Measured:
    kind: str                         # length | polyline | area
    value_in: float | None            # length / perimeter, in inches
    value_sqin: float | None          # area, in square inches
    display: str                      # the headline, in the scale's own system
    display_other: str                # the same value in the other system
    segments_in: list[float] = field(default_factory=list)
    perimeter_display: str | None = None
    uncertainty_in: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _spread_of(scale: ScaleInfo) -> float:
    if scale.status == "calibrated":
        return 0.0
    return scale.spread if scale.spread is not None else UNCHECKED_SPREAD


def _show(inches: float, system: str) -> tuple[str, str]:
    imp, met = format_imperial(inches), format_metric(inches)
    return (imp, met) if system == "imperial" else (met, imp)


def measure(kind: str, pts: list[tuple[float, float]], scale: ScaleInfo, snapped: list[bool] | None = None) -> Measured:
    """The real-world length or area of a drawn path under `scale`."""
    snapped = snapped or [False] * len(pts)
    click = sum(CLICK_PT_SNAPPED if sn else CLICK_PT_UNSNAPPED for sn in snapped) / max(len(snapped), 1)
    warnings: list[str] = []

    if kind in ("length", "polyline"):
        need = 2
        if len(pts) < need:
            raise ValueError("A length needs at least two points.")
        if kind == "length" and len(pts) != 2:
            raise ValueError("A length is exactly two points; use a path for more.")
        seg_pt = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
        seg_in = [real_inches(x, scale.ratio) for x in seg_pt]
        total = sum(seg_in)
        # Each point carries the click error; the scale carries its own spread, which
        # grows with length (a 0.5% spread is 1" on a 16' run).
        point_err = math.sqrt(len(pts)) * click / PT_PER_IN * scale.ratio
        unc = math.hypot(point_err, total * _spread_of(scale))
        shown, other = _show(total, scale.system)
        return Measured(kind, total, None, shown, other, seg_in, None, unc, warnings)

    if kind == "area":
        if len(pts) < 3:
            raise ValueError("An area needs at least three points.")
        sq_in = polygon_area_pt2(pts) / (PT_PER_IN ** 2) * scale.ratio ** 2
        perimeter = real_inches(path_length_pt(pts + pts[:1]), scale.ratio)
        imp, met = format_area(sq_in)
        shown, other = (imp, met) if scale.system == "imperial" else (met, imp)
        p_shown, _ = _show(perimeter, scale.system)
        # Area error ~ perimeter x click error; the scale error is carried by the perimeter figure.
        unc = math.hypot(perimeter * (click / PT_PER_IN * scale.ratio) * 0.5, perimeter * _spread_of(scale))
        return Measured(kind, perimeter, sq_in, shown, other, [], p_shown, unc, warnings)

    raise ValueError(f"Unknown measurement kind: {kind}")


# ── Which scale applies where ───────────────────────────────────────────────

def choose_scale(
    scales: list[ScaleInfo],
    pts: list[tuple[float, float]],
    primary: int | None,
    requested: int | None = None,
) -> int | None:
    """Index of the scale to use for a measurement.

    A sheet can carry several (a plan and its details). An explicit choice wins; else
    a calibration the user made near the points; else the checked scale (verified or
    measured) with the nearest supporting dimension or label; else the sheet's primary.
    """
    if not scales:
        return None
    if requested is not None and 0 <= requested < len(scales):
        return requested
    cx = sum(p[0] for p in pts) / max(len(pts), 1)
    cy = sum(p[1] for p in pts) / max(len(pts), 1)

    def nearest(sc: ScaleInfo) -> float:
        return min((math.hypot(cx - ax, cy - ay) for ax, ay in sc.anchors), default=1e9)

    trusted = {"calibrated": 0, "verified": 1, "measured": 1}
    ranked = sorted(
        (i for i, sc in enumerate(scales) if sc.status in trusted and sc.status != "conflict"),
        key=lambda i: (nearest(scales[i]), trusted[scales[i].status]),
    )
    if ranked and len(ranked) > 1:
        return ranked[0]
    if ranked:
        return ranked[0]
    return primary if primary is not None else 0


# ── Snapping ────────────────────────────────────────────────────────────────

def snap_points(geom: PageGeometry, cap: int = 24000) -> list[tuple[float, float]]:
    """Vertices of the drawing a click can snap to: line ends and corners, so a measured
    length starts and ends exactly on the drawn geometry rather than where the pointer
    happened to land. Bezier control points are not on the curve, so curves with many
    points (circles, arcs) contribute nothing."""
    seen: set[tuple[int, int]] = set()
    out: list[tuple[float, float]] = []
    for s in geom.shapes:
        if s.kind == "curve" and len(s.pts) > 8:
            continue
        for x, y in s.pts:
            key = (round(x * 2), round(y * 2))        # 0.5 pt grid
            if key in seen:
                continue
            seen.add(key)
            out.append((x, y))
            if len(out) >= cap:
                return out
    return out
