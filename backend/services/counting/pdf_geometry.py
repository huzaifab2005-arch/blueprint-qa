"""Words and vector shapes of one PDF page, in displayed-page coordinates.

Counting works from the PDF's own data (text with positions, vector paths)
rather than pixels. pdfplumber (MIT) is used on purpose: it is pure Python and
licence-safe for a commercial product, at the price of ~0.5 s per dense CAD page,
which is fine for the handful of pages a question touches.

pdfplumber reports positions in the page as DISPLAYED (rotation applied), which
is the same space the rendered page image uses; the tests check this on a rotated
page. Coordinates here are PDF points with the origin at the top-left.
"""
from dataclasses import dataclass, field
from functools import lru_cache
import os


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class Shape:
    kind: str  # rect | line | curve
    x0: float
    x1: float
    top: float
    bottom: float
    pts: tuple[tuple[float, float], ...] = ()

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.bottom - self.top

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return max(self.w, self.h)


@dataclass
class PageGeometry:
    page_number: int
    width: float
    height: float
    words: list[Word] = field(default_factory=list)
    shapes: list[Shape] = field(default_factory=list)

    def norm_box(self, x0: float, top: float, x1: float, bottom: float) -> tuple[float, float, float, float]:
        """(x, y, w, h) as fractions of the displayed page, for drawing markers."""
        return (x0 / self.width, top / self.height, (x1 - x0) / self.width, (bottom - top) / self.height)


MAX_SHAPES = 40000  # a page beyond this is a hatch-filled raster-in-disguise; skip shapes


def _shape(kind: str, o: dict) -> Shape:
    pts = tuple((round(float(x), 2), round(float(y), 2)) for x, y in (o.get("pts") or ()))
    return Shape(kind, float(o["x0"]), float(o["x1"]), float(o["top"]), float(o["bottom"]), pts)


def load_geometry(pdf_path: str, page_number: int) -> PageGeometry:
    """Blocking. Words and vector shapes of one page (1-indexed)."""
    return _load_cached(pdf_path, os.path.getmtime(pdf_path), page_number)


@lru_cache(maxsize=48)
def _load_cached(pdf_path: str, _mtime: float, page_number: int) -> PageGeometry:
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number - 1]
        geom = PageGeometry(page_number, float(page.width), float(page.height))
        for w in page.extract_words(keep_blank_chars=False, use_text_flow=False):
            geom.words.append(Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"]), float(w["bottom"])))
        total = len(page.rects) + len(page.lines) + len(page.curves)
        if total <= MAX_SHAPES:
            for kind, objs in (("rect", page.rects), ("line", page.lines), ("curve", page.curves)):
                geom.shapes.extend(_shape(kind, o) for o in objs)
        return geom
