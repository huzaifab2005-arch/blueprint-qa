"""Count a drawing symbol by matching its legend swatch against the plan geometry.

A legend entry is a swatch (vector shapes) next to a line of text. The same
symbol drawn on the plan is built from the same shapes, so the count is the number
of places where that constellation of shapes occurs. This is exact geometry, not a
visual guess, but it is only as good as the assumption that the swatch is unique to
that object: look-alikes elsewhere would be counted. Callers therefore always give
the user markers to check, and flag instances whose size differs from the swatch.

Pure functions over PageGeometry; no I/O.
"""
from dataclasses import dataclass, field

from backend.services.counting.pdf_geometry import PageGeometry, Shape, Word

MAX_SYMBOL = 90.0       # pt; larger shapes are walls, grids and borders, not symbols
EDGE_TOL = 0.045        # normalised tolerance when comparing outlines
STRICT_SCALE = 0.07     # instance size within 7% of the swatch counts as the same size


@dataclass
class Template:
    label: str                      # the legend text this swatch belongs to
    members: list[Shape]
    page_number: int
    legend_box: tuple[float, float, float, float]  # x0, top, x1, bottom of swatch

    @property
    def anchor(self) -> Shape:
        """The member to seed matching from: the largest shape with real area, so a
        circle beats the crosshair lines through it."""
        return max(self.members, key=lambda s: (s.w * s.h > 1.0, s.size, s.w * s.h))


@dataclass
class Instance:
    x0: float
    top: float
    x1: float
    bottom: float
    scale: float                    # instance size / swatch size

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class SymbolCount:
    template: Template
    instances: list[Instance] = field(default_factory=list)

    @property
    def median_scale(self) -> float:
        scales = sorted(i.scale for i in self.instances)
        return scales[len(scales) // 2] if scales else 1.0

    @property
    def uniform_scale(self) -> bool:
        """All instances share one scale. A uniform offset from the swatch means the
        legend icon is simply drawn at a different size than the plan symbol; a mix
        of sizes means some matches may be look-alikes."""
        m = self.median_scale
        return all(abs(i.scale / m - 1) <= 0.05 for i in self.instances)

    @property
    def same_size_as_swatch(self) -> bool:
        return bool(self.instances) and abs(self.median_scale - 1) <= STRICT_SCALE


# ── Outline signatures ──────────────────────────────────────────────────────

def _segments(s: Shape) -> list[tuple[float, float, float, float]]:
    """Undirected edges of a shape, normalised to its own bounding box. Order and
    start point of the path do not matter: the plan's instances of a swatch are
    often drawn from a different corner."""
    w, h = max(s.w, 1e-6), max(s.h, 1e-6)
    pts = [((x - s.x0) / w, (y - s.top) / h) for x, y in s.pts]
    if s.kind == "rect" and len(pts) >= 3 and pts[0] != pts[-1]:
        pts.append(pts[0])  # a rectangle's corner list does not repeat the first corner
    segs = []
    for a, b in zip(pts, pts[1:]):
        if a == b:
            continue
        segs.append(tuple(sorted((a, b))))
    flat = {(round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3)) for a, b in segs}
    return sorted(flat)


# The 8 symmetries of a square: a symbol mirrored or turned 90 degrees is the same
# symbol (a fixture's diagonal is "/" in one drawing and "\\" in the next).
_TRANSFORMS = (
    (lambda x, y: (x, y), False),
    (lambda x, y: (1 - x, y), False),
    (lambda x, y: (x, 1 - y), False),
    (lambda x, y: (1 - x, 1 - y), False),
    (lambda x, y: (y, x), True),
    (lambda x, y: (1 - y, x), True),
    (lambda x, y: (y, 1 - x), True),
    (lambda x, y: (1 - y, 1 - x), True),
)


def _transform_segments(segs, fn):
    out = []
    for x1, y1, x2, y2 in segs:
        a, b = fn(x1, y1), fn(x2, y2)
        out.append((a[0], a[1], b[0], b[1]) if a <= b else (b[0], b[1], a[0], a[1]))
    return out


def _segments_match(sa, sb) -> bool:
    if len(sa) != len(sb):
        return False
    unused = list(sb)
    for seg in sa:
        hit = next((o for o in unused if all(abs(x - y) <= EDGE_TOL for x, y in zip(seg, o))), None)
        if hit is None:
            return False
        unused.remove(hit)
    return True


def _same_outline(a: Shape, b: Shape) -> bool:
    """True if b is a (scaled, mirrored or quarter-turned) copy of a."""
    if (a.kind == "line") != (b.kind == "line"):
        return False
    ra, rb = (a.w + 1e-6) / (a.h + 1e-6), (b.w + 1e-6) / (b.h + 1e-6)
    sa, sb = _segments(a), _segments(b)
    if len(sa) != len(sb) or not sa:
        return False
    # Aspect is only meaningful for shapes with real extent in both directions; a
    # straight line has ~zero width and its "ratio" is numerical noise.
    check_aspect = min(a.w, a.h) > 0.5 and min(b.w, b.h) > 0.5
    for fn, swaps in _TRANSFORMS:
        target_ratio = 1 / ra if swaps else ra
        if check_aspect and abs(target_ratio - rb) / max(target_ratio, rb) > 0.12:
            continue
        if _segments_match(_transform_segments(sa, fn), sb):
            return True
    return False


# ── Finding a legend swatch ─────────────────────────────────────────────────

def _touching(a: Shape, b: Shape, gap: float = 1.0) -> bool:
    return not (a.x1 + gap < b.x0 or b.x1 + gap < a.x0 or a.bottom + gap < b.top or b.bottom + gap < a.top)


def _clusters(shapes: list[Shape]) -> list[list[Shape]]:
    groups: list[list[Shape]] = []
    for s in shapes:
        merged = [g for g in groups if any(_touching(s, m) for m in g)]
        if not merged:
            groups.append([s])
            continue
        first = merged[0]
        first.append(s)
        for g in merged[1:]:
            first.extend(g)
            groups.remove(g)
    return groups


def _bbox(shapes: list[Shape]) -> tuple[float, float, float, float]:
    return (min(s.x0 for s in shapes), min(s.top for s in shapes),
            max(s.x1 for s in shapes), max(s.bottom for s in shapes))


MIN_SWATCH_EDGES = 5     # a plain circle/square (<=4 edges) cannot be told from anything else
MAX_SWATCH = 60.0


def complexity(shapes: list[Shape]) -> int:
    """Distinct outline edges across all members. A square is 4; a square with a
    diagonal 5; a circle with crosshair more. Low numbers match everything."""
    return sum(len(_segments(s)) for s in shapes)


def _has_text_inside(geom: PageGeometry, shapes: list[Shape]) -> bool:
    """A callout bubble or tag has a number or letter inside it; a legend swatch for
    a fixture or device does not. (Symbols that carry a letter, like a fan marked
    'F', are therefore not matched, which errs on the side of not counting.)"""
    x0, top, x1, bottom = _bbox(shapes)
    return any(x0 - 0.5 <= w.cx <= x1 + 0.5 and top - 0.5 <= w.cy <= bottom + 0.5 for w in geom.words)


def _swatch_clusters(geom: PageGeometry, box: tuple[float, float, float, float]) -> list[list[Shape]]:
    """Qualified symbol clusters left of a text block, within its vertical span."""
    x0, top, _x1, bottom = box
    region = [
        s for s in geom.shapes
        if 4 <= s.size <= MAX_SYMBOL
        and x0 - 100 <= s.cx <= x0 + 2
        and top - 14 <= s.cy <= bottom + 14
    ]
    out = []
    for g in _clusters(region):
        bx = _bbox(g)
        extent = max(bx[2] - bx[0], bx[3] - bx[1])
        if extent < 6 or extent > MAX_SWATCH or len(g) > 8:
            continue  # vector letter outlines (text drawn as paths), or a merged blob
        if _has_text_inside(geom, g) or complexity(g) < MIN_SWATCH_EDGES:
            continue  # a numbered bubble, or a shape too plain to identify by outline
        if min(bx[2] - bx[0], bx[3] - bx[1]) < 3:
            continue  # a stroke, not a two-dimensional symbol
        hairlines = sum(1 for s in g if s.kind == "curve" and min(s.w, s.h) < 0.8 and s.size < 14)
        if hairlines >= 3:
            continue  # stroked glyphs of a number or letter, e.g. a keynote bubble
        out.append(g)
    return out


# ── Legend lookup by text ───────────────────────────────────────────────────

def group_lines(words: list[Word], tol: float = 0.35) -> list[list[Word]]:
    """Words grouped into text lines by vertical position, left to right."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.cy, w.x0)):
        for line in lines:
            ref = line[0]
            if abs(w.cy - ref.cy) <= tol * max(ref.bottom - ref.top, 1):
                line.append(w)
                break
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w.x0)
    return lines


def split_cells(line: list[Word], gap: float = 14.0) -> list[list[Word]]:
    """Split a text line into table cells wherever the horizontal gap is large."""
    cells: list[list[Word]] = []
    for w in line:
        if cells and w.x0 - cells[-1][-1].x1 <= gap:
            cells[-1].append(w)
        else:
            cells.append([w])
    return cells


def cell_box(cell: list[Word]) -> tuple[float, float, float, float]:
    return (min(w.x0 for w in cell), min(w.top for w in cell), max(w.x1 for w in cell), max(w.bottom for w in cell))


@dataclass
class TextBlock:
    heading: str
    heading_box: tuple[float, float, float, float]
    box: tuple[float, float, float, float]       # the whole block, heading and description lines


def _text_cells(geom: PageGeometry) -> list[tuple[tuple[float, float, float, float], str]]:
    cells = []
    for line in group_lines(geom.words):
        for cell in split_cells(line):
            cells.append((cell_box(cell), " ".join(w.text for w in cell)))
    return cells


def block_starting_at(cells, index: int) -> TextBlock:
    """The legend entry that starts at cells[index]: that line plus the lines under it
    in the same column, until a gap or the next heading (a line ending in ':').

    Starting AT the matching line, rather than at the top of a column of text, keeps
    a legend's title ("LIGHTING LEGEND") from swallowing its first entry.
    """
    head_box, head_text = cells[index]
    boxes = [head_box]
    column = [c for c in cells if abs(c[0][0] - head_box[0]) <= 4 and c[0][1] > head_box[1] + 0.5]
    column.sort(key=lambda c: c[0][1])
    prev = head_box
    for box, text in column:
        height = max(prev[3] - prev[1], 4)
        if box[1] - prev[3] > 1.6 * height or text.rstrip().endswith(":"):
            break
        boxes.append(box)
        prev = box
    return TextBlock(
        head_text, head_box,
        (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)),
    )


def find_legend_templates(
    geom: PageGeometry,
    matches_heading,
) -> tuple[list[Template], list[str]]:
    """Templates for legend entries whose heading `matches_heading(text)` accepts.

    A legend entry is a text block with its swatch beside it, vertically centred on
    the block. The swatch is assigned to the block it is aligned with, so a swatch
    that is rejected (too plain, contains text) is never handed to the next entry.
    Returns (templates, notes).
    """
    cells = _text_cells(geom)
    candidates: list[tuple[float, TextBlock, list[Shape]]] = []
    unmatched: list[str] = []
    for i, (_box, text) in enumerate(cells):
        if not matches_heading(text) or text.rstrip().endswith("."):
            continue
        blk = block_starting_at(cells, i)
        swatches = _swatch_clusters(geom, blk.box)
        if not swatches:
            unmatched.append(blk.heading)
            continue
        centre = (blk.box[1] + blk.box[3]) / 2
        for sw in swatches:
            bx = _bbox(sw)
            dist = abs((bx[1] + bx[3]) / 2 - centre) + 0.3 * abs(blk.box[0] - bx[2])
            candidates.append((dist, blk, sw))

    candidates.sort(key=lambda c: c[0])
    templates: list[Template] = []
    used_blocks: set[tuple] = set()
    used_swatches: set[tuple] = set()
    for _dist, blk, sw in candidates:
        kb = (round(blk.box[0]), round(blk.box[1]))
        ks = tuple(sorted((round(s.x0), round(s.top)) for s in sw))
        if kb in used_blocks or ks in used_swatches:
            continue
        used_blocks.add(kb)
        used_swatches.add(ks)
        templates.append(Template(blk.heading.strip(" :"), sw, geom.page_number, _bbox(sw)))
    notes = [f"Legend text '{t[:50]}' has no countable symbol beside it." for t in unmatched[:3]]
    return templates, notes


# ── Counting instances ──────────────────────────────────────────────────────

def count_template(
    geom: PageGeometry, template: Template, exclude: set[int] | None = None
) -> SymbolCount:
    """Instances of the template on the page. `exclude` holds ids of shapes that are
    legend swatches (of this or any other entry) and so never count as instances."""
    anchor = template.anchor
    members = [m for m in template.members if m is not anchor]
    own = {id(m) for m in template.members} | (exclude or set())
    result = SymbolCount(template)
    claimed: set[int] = set()

    candidates = sorted(
        (s for s in geom.shapes if id(s) not in own and s.size <= MAX_SYMBOL * 2),
        key=lambda s: (s.top, s.x0),
    )
    found: list[Instance] = []
    for a in candidates:
        if id(a) in claimed or not _same_outline(a, anchor):
            continue
        scale = a.size / max(anchor.size, 1e-6)
        if not 0.6 <= scale <= 1.7:
            continue
        used = [a]
        ok = True
        for m in members:
            ex = a.cx + (m.cx - anchor.cx) * scale
            ey = a.cy + (m.cy - anchor.cy) * scale
            tol = max(1.5, 0.1 * m.size * scale)
            hit = next(
                (s for s in candidates
                 if id(s) not in claimed and id(s) not in {id(u) for u in used}
                 and abs(s.cx - ex) <= tol and abs(s.cy - ey) <= tol
                 and 0.8 <= (s.size / max(m.size, 1e-6)) / scale <= 1.25
                 and _same_outline(s, m)),
                None,
            )
            if hit is None:
                ok = False
                break
            used.append(hit)
        if not ok:
            continue
        for u in used:
            claimed.add(id(u))
        bx = _bbox(used)
        found.append(Instance(bx[0], bx[1], bx[2], bx[3], scale))

    # Identical objects drawn twice on top of each other are one symbol.
    deduped: list[Instance] = []
    for inst in found:
        if not any(abs(inst.cx - d.cx) < 0.3 * max(d.x1 - d.x0, 1) and abs(inst.cy - d.cy) < 0.3 * max(d.bottom - d.top, 1)
                   for d in deduped):
            deduped.append(inst)
    result.instances = deduped
    return result


def looks_like_callout_bubbles(geom: PageGeometry, instances: list[Instance]) -> bool:
    """True when the "symbol" is really a numbered keynote bubble.

    Bubbles sit in a column beside note text, so most of their instances have a line
    of text starting just to their right. A fixture symbol on a plan does not.
    """
    if len(instances) < 3:
        return False
    lines = group_lines(geom.words)
    starts = []
    for line in lines:
        if len(line) >= 3:
            starts.append((line[0].x0, line[0].cy))
    beside = 0
    for inst in instances:
        if any(2 <= x0 - inst.x1 <= 25 and abs(cy - inst.cy) <= 9 for x0, cy in starts):
            beside += 1
    return beside / len(instances) >= 0.5


def resolve_conflicts(counts: list[SymbolCount]) -> None:
    """When two legend entries claim the same spot, keep the more specific one.

    A potlight (circle + crosshair) is a sub-shape of a pendant (circle + crosshair
    + inner circle), so the simpler template also "matches" the pendant. The
    template with more members wins; on a tie, the one whose scale is closer to 1.
    """
    def rank(c: SymbolCount, inst: Instance):
        return (-len(c.template.members), abs(inst.scale - 1))

    claims: list[tuple[SymbolCount, Instance]] = [(c, i) for c in counts for i in c.instances]
    drop: set[int] = set()
    for idx, (c1, i1) in enumerate(claims):
        for c2, i2 in claims[idx + 1:]:
            if c1 is c2:
                continue
            if abs(i1.cx - i2.cx) <= 0.4 * max(i1.x1 - i1.x0, i2.x1 - i2.x0) and \
               abs(i1.cy - i2.cy) <= 0.4 * max(i1.bottom - i1.top, i2.bottom - i2.top):
                loser = i2 if rank(c1, i1) <= rank(c2, i2) else i1
                drop.add(id(loser))
    for c in counts:
        c.instances = [i for i in c.instances if id(i) not in drop]
