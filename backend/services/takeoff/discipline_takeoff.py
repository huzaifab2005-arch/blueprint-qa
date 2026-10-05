"""Build a takeoff for a whole discipline ("the lighting takeoff") from the drawings.

What goes in it is found, not asked for:

  * schedule rows: a line that starts with a tag (L1, RTU-1, WC-1) in a schedule or
    legend table gives an item, with its description, model/manufacturer (read from the
    table's own column headers) and, where there is one, its QTY;
  * legend entries: a symbol with a heading and text block beside it (the way many
    sets draw their lighting legend) gives an item whose model/spec is read from the
    block's labelled lines (MFR:, MODEL:, CODE:).

Each item is then counted with the same evidence and the same rules as a "how many"
question (tag instances on plans, legend symbols matched on plans, schedule QTY), so
an item is only `verified` when two independent readings agree. Nothing is invented:
an item the schedule lists but the plans do not show comes back with quantity 0 and
a warning, not a guess.

Blocking (run in a thread).
"""
import logging
import re
from dataclasses import dataclass, field

from backend.config import get_settings
from backend.services.counting.count_service import (
    CROSS_CHECKED, Evidence, MIN_PLAN_SHAPES, PageMeta, build_methods, classify, collect_evidence,
    _is_schedule_page, _legend_matcher, _mentions_object,
)
from backend.services.counting.objects import ParsedCount, singular, size_variants
from backend.services.counting.pdf_geometry import PageGeometry, load_geometry
from backend.services.counting.symbol_counter import (
    _text_cells, block_starting_at, cell_box, group_lines, split_cells,
)
from backend.services.counting.tag_counter import TAG_RE, DefRow, clean_word, find_tag_hits, tag_key
from backend.services.retrieval_service import normalise_text
from backend.services.takeoff.disciplines import Discipline

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_PAGES = 40

HEADER_WORDS = {
    "TYPE", "TAG", "MARK", "ID", "DESCRIPTION", "MODEL", "MFR", "MFG", "MANUFACTURER", "CATALOG", "CAT", "QTY",
    "QUANTITY", "VOLTS", "VOLTAGE", "LAMP", "WATTS", "WATTAGE", "REMARKS", "NOTES", "SIZE", "MOUNTING", "FINISH",
    "CAPACITY", "CFM", "LOCATION", "SERVES", "PART", "COMMENTS", "FIXTURE", "DRIVER", "LUMENS", "CCT",
}
_MODEL_COLS = ("MODEL", "CATALOG", "CAT", "PART")
_MFR_COLS = ("MANUFACTURER", "MFR", "MFG", "MAKE")
_DESC_COLS = ("DESCRIPTION", "FIXTURE", "TYPE")
_QTY_COLS = ("QTY", "QUANTITY")
_LABELLED = re.compile(r"^\s*(?P<k>[A-Za-z][A-Za-z .#/]{1,22}?)\s*[:：]\s*(?P<v>\S.*)$")
_MODEL_KEYS = re.compile(r"^(?:MODEL(?:\s*(?:NO|NUMBER|#))?|CODE|CAT(?:ALOG)?(?:\s*(?:NO|NUMBER|#))?|PART(?:\s*(?:NO|NUMBER|#))?)\.?$", re.I)
_MFR_KEYS = re.compile(r"^(?:MFR|MFG|MANUFACTURER|MAKE)\.?$", re.I)


@dataclass
class Item:
    key: str
    item: str
    details: str
    model: str
    quantity: int
    status: str                         # verified | needs_verification
    confidence: str | None
    sources: list[dict]
    basis: str
    warnings: list[str] = field(default_factory=list)
    unit: str = "EA"
    defined_on: list[str] = field(default_factory=list)
    entry: dict = field(default_factory=dict)


@dataclass
class DisciplineResult:
    discipline: str
    label: str
    items: list[Item]
    notes: list[str]
    pages_searched: list[int]


# ── Page selection ──────────────────────────────────────────────────────────

def _is_listing_page(meta: PageMeta) -> bool:
    title = (meta.sheet_title or "").upper()
    return _is_schedule_page(meta) or bool(re.search(r"\b(LEGEND|LEGENDS|SCHEDULE|NOTES)\b", title))


def candidate_pages(metas: list[PageMeta], disc: Discipline) -> list[int]:
    cls = disc.object_class()
    parsed = ParsedCount("", [], cls, [], None)
    words = [w for w in disc.page_words]
    scored = []
    for m in metas:
        norm = normalise_text(m.text or "")
        title = (m.sheet_title or "").upper()
        score = (3 if any(w in title for w in words) else 0) + (2 if _mentions_object(m.text or "", cls, parsed) else 0) \
            + (1 if any(w in norm for w in words) else 0)
        if score:
            scored.append((score, m.page_number))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return sorted(n for _s, n in scored[:MAX_PAGES])


# ── Schedule rows ───────────────────────────────────────────────────────────

@dataclass
class Row:
    tag: str
    page_number: int
    description: str
    model: str
    qty: int | None
    details: str
    first_word: object
    text: str


def _header_columns(lines: list[list], row_index: int) -> list[tuple[float, str]] | None:
    """Column starts and names from the nearest header line above a row."""
    for back in range(1, 14):
        i = row_index - back
        if i < 0:
            return None
        cells = split_cells(lines[i])
        names = [" ".join(w.text for w in c).upper().strip(": ") for c in cells]
        hits = sum(1 for n in names if n.split(" ")[0] in HEADER_WORDS or n in HEADER_WORDS)
        if hits >= 2 and len(cells) >= 3:
            return [(c[0].x0, n) for c, n in zip(cells, names)]
    return None


def _columns_of(line, cols: list[tuple[float, str]]) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for w in line:
        idx = 0
        for j, (x0, _n) in enumerate(cols):
            if w.x0 >= x0 - 4:
                idx = j
        out.setdefault(cols[idx][1], []).append(w.text)
    return {k: " ".join(v) for k, v in out.items()}


def _pick(cols: dict[str, str], names: tuple[str, ...]) -> str:
    for n in names:
        for k, v in cols.items():
            if k == n or k.startswith(n + " ") or k.startswith(n + "."):
                return v.strip()
    return ""


def find_schedule_rows(geom: PageGeometry, disc: Discipline) -> list[Row]:
    lines = group_lines(geom.words)
    tag_pat = disc.tag_pattern()
    kw = {w for k in disc.keywords for w in k.split()}
    rows: list[Row] = []
    for i, line in enumerate(lines):
        first = split_cells(line)[0][0]
        tag = clean_word(first.text)
        if not TAG_RE.match(tag) or len(line) < 3:
            continue
        rest = " ".join(w.text for w in line[1:])
        rest_words = set(re.findall(r"[A-Z][A-Z0-9]*", rest.upper()))
        if not ((tag_pat and tag_pat.match(tag)) or kw & rest_words):
            continue
        cols = _header_columns(lines, i)
        if cols is None:
            continue                       # not in a table: a tag in running text or a plan label
        by_col = _columns_of(line, cols)
        text = " ".join(w.text for w in line)
        qty_text = _pick(by_col, _QTY_COLS)
        qty = int(qty_text) if re.fullmatch(r"\d{1,4}", qty_text.strip()) else None
        model = _pick(by_col, _MODEL_COLS)
        mfr = _pick(by_col, _MFR_COLS)
        desc = _pick(by_col, _DESC_COLS)
        if desc.upper().startswith(tag.upper()):
            desc = desc[len(tag):].strip()
        used = set(by_col.get(c, "") for c in ())
        leftover = [v for k, v in by_col.items()
                    if v and not any(k.startswith(n) for n in _MODEL_COLS + _MFR_COLS + _QTY_COLS)
                    and v.strip().upper() != tag.upper() and v.strip() != desc and not k.startswith(("TYPE", "TAG", "MARK", "ID"))]
        if not desc:
            desc = leftover.pop(0) if leftover else rest
        spec = " ".join(" ".join(leftover).split())
        model_text = " ".join(x for x in (mfr, model) if x).strip()
        rows.append(Row(tag, geom.page_number, " ".join(desc.split()), model_text, qty, spec, first, text))
    return rows


# ── Legend entries ──────────────────────────────────────────────────────────

def despace(words: list) -> str:
    """Text of a cell. A heading drawn letter by letter ("S LI M  R E C E S S E D") comes out as
    one word per letter; the gaps between its words are wider than between its letters."""
    if len(words) < 5 or sum(len(w.text) for w in words) / len(words) > 2.2:
        return " ".join(w.text for w in words)
    gaps = [b.x0 - a.x1 for a, b in zip(words, words[1:])]
    cut = max(gaps) * 0.5 if gaps and max(gaps) > 0 else 1.0
    out = words[0].text
    for g, w in zip(gaps, words[1:]):
        out += (" " if g > cut else "") + w.text
    return out


def _clean_cells(geom: PageGeometry) -> list[tuple[tuple[float, float, float, float], str]]:
    cells = []
    for line in group_lines(geom.words):
        for cell in split_cells(line):
            text = re.sub(r"\(cid:\d+\)", " ", despace(cell))
            cells.append((cell_box(cell), " ".join(text.split())))
    return cells


def _legend_block(geom: PageGeometry, label: str) -> tuple[str, str, str, str]:
    """(clean heading, description lines, model, manufacturer) for the legend entry whose
    heading is `label`. Letter-spaced text is rejoined before it is read."""
    raw = _text_cells(geom)
    clean = _clean_cells(geom)
    for i, (_box, text) in enumerate(raw):
        if text.strip(" :") == label:
            heading = clean[i][1].strip(" :")
            blk = block_starting_at(raw, i)
            x0, y0, x1, y1 = blk.box
            lines = [t for (b, _r), (_b, t) in zip(raw, clean)
                     if b[0] >= x0 - 1 and b[2] <= x1 + 1 and b[1] >= y0 - 1 and b[3] <= y1 + 1]
            mfr = model = ""
            other = []
            for t in lines:
                if t.strip(" :") == heading:
                    continue
                m = _LABELLED.match(t)
                if m and _MFR_KEYS.match(m.group("k").strip()):
                    mfr = mfr or m.group("v").strip()
                elif m and _MODEL_KEYS.match(m.group("k").strip()):
                    model = model or m.group("v").strip()
                else:
                    other.append(t.strip())
            return heading, " ".join(" ".join(other).split())[:240], model, mfr
    return label, "", "", ""


def discipline_matcher(disc: Discipline, cls, parsed):
    """Legend-heading matcher that also reads letter-spaced headings ("SU S P E N D E D T R A C K
    F I X TURE"), which many CAD exports produce and word matching cannot see through."""
    base = _legend_matcher(cls, parsed)
    compact_phrases = [re.sub(r"[^A-Z]", "", p.upper()) for p in disc.legend_phrases]

    def matches(text: str) -> bool:
        if base(text):
            return True
        toks = text.split()
        if len(toks) < 5 or sum(len(t) for t in toks) / len(toks) > 2.2 or text.rstrip().endswith((".", ",", ";")):
            return False
        compact = re.sub(r"[^A-Z]", "", text.upper())
        return len(compact) <= 60 and any(p in compact for p in compact_phrases)

    return matches


# ── Matching a legend entry to the schedule row that describes the same thing ──

_GENERIC_SIG = {"LED", "FIXTURE", "LIGHT", "LIGHTING", "LUMINAIRE", "TYPE", "THE", "AND", "FOR", "WITH", "OF", "IN", "IN.", "A"}


def signature(text: str) -> set[str]:
    """Comparable features of a name: its specific words, and its sizes in every
    spelling (2X4 = 2'x4' = 24X48; 6IN = 6" = D6)."""
    t = normalise_text(text).replace("\u2032", "'").replace("\u2033", '"')
    sig: set[str] = set()
    for a, b in re.findall(r"(\d+(?:\.\d+)?)\s*['\"]?\s*X\s*(\d+(?:\.\d+)?)", t):
        sig |= {f"S{v}" for v in size_variants(f"{a}X{b}".upper())}
    for d in re.findall(r"(?<![\dX.])(\d+(?:\.\d+)?)\s*(?:IN\b|INCH\b|\"|\u201d)", t):
        sig.add(f"D{d}")
    for w in re.findall(r"[A-Z][A-Z]+", t):
        w = singular(w)
        if w not in _GENERIC_SIG and len(w) > 2:
            sig.add(w)
    return sig


def best_row_for(label: str, rows: dict[str, "Row"]) -> str | None:
    sig = signature(label)
    best, best_n = None, 0
    for k, r in rows.items():
        n = len(sig & signature(r.description))
        if n > best_n:
            best, best_n = k, n
    return best if best_n >= 1 else None


# ── Counting ────────────────────────────────────────────────────────────────

def _source_list(per_page: dict[int, int], metas: dict[int, PageMeta]) -> list[dict]:
    return [{"page_number": p, "label": metas[p].label, "note": f"{n} counted"}
            for p, n in sorted(per_page.items()) if n]


def _item_from_methods(key, item, details, model, methods, blocking, warnings, metas, defined_on, entry, note="") -> Item:
    status, primary, notes = classify(methods, blocking)
    warnings = [w for w in warnings if not w.startswith("Only objects whose legend symbol was identified")] + notes
    sources = _source_list(primary.per_page, metas) if primary else []
    if primary is None:
        warnings.append("Listed in the drawings, but no instance could be found on the plans. Check the plans, or enter the quantity manually.")
    state = "verified" if status == CROSS_CHECKED else "needs_verification"
    if status != CROSS_CHECKED and primary is not None:
        warnings.append("Only one reading of the drawing produced this quantity, so it is not cross-checked." if status == "single_source"
                        else "The readings of the drawing disagree or are uncertain; check it on the sheets.")
    conf = {"cross_checked": "high", "single_source": "medium"}.get(status, "low" if primary else None)
    basis = " ".join(x for x in (primary.detail if primary else "", note) if x)
    others = [m for m in methods if m is not primary]
    if others:
        basis += " Also: " + "; ".join(f"{m.method.replace('_', ' ')} {m.quantity}" for m in others) + "."
    return Item(key, item, details, model, primary.quantity if primary else 0, state, conf, sources, basis.strip(),
                warnings, defined_on=defined_on, entry=entry)


def build_discipline_takeoff(pdf_path: str, metas_list: list[PageMeta], disc: Discipline) -> DisciplineResult:
    metas = {m.page_number: m for m in metas_list}
    pages = candidate_pages(metas_list, disc)
    notes: list[str] = []
    if not pages:
        return DisciplineResult(disc.key, disc.label, [], [f"No sheets that mention {disc.label.lower()} were found in this set, so nothing was assumed."], [])

    geoms: dict[int, PageGeometry] = {}
    for n in pages:
        try:
            geoms[n] = load_geometry(pdf_path, n)
        except Exception as exc:
            logger.warning("Could not read geometry of page %s: %s", n, exc)
            notes.append(f"{metas[n].label} could not be read.")

    plan_pages = [n for n in geoms if not _is_listing_page(metas[n]) and len(geoms[n].shapes) >= MIN_PLAN_SHAPES]
    items: list[Item] = []

    # 1. Schedule rows (found first, so legend entries can be matched to them)
    rows: dict[str, Row] = {}
    defined: dict[str, list[str]] = {}
    for n in sorted(geoms):
        for r in find_schedule_rows(geoms[n], disc):
            k = tag_key(r.tag)
            defined.setdefault(k, []).append(metas[n].label)
            rows.setdefault(k, r)
    row_word_ids = {id(r.first_word) for r in rows.values()}

    # 2. Legend entries (symbol + text block), counted by matching the symbol on the plans
    syn = disc.object_class()
    parsed = ParsedCount("", [], syn, [], None)
    ev = collect_evidence(pdf_path, metas, [n for n in pages if n in geoms], syn, parsed,
                          matcher=discipline_matcher(disc, syn, parsed))
    linked: dict[str, list] = {}          # row key -> symbol counts that describe the same item
    standalone = []
    seen_labels: set[tuple[int, str]] = set()
    for c in ev.symbol_counts:
        geom0 = geoms.get(c.template.page_number) or ev.geoms.get(c.template.page_number)
        heading0 = _legend_block(geom0, c.template.label)[0] if geom0 else c.template.label
        if set(re.findall(r"[A-Z]+", heading0.upper())) & set(disc.exclude_words):
            continue
        key = (c.template.page_number, re.sub(r"\W+", "", c.template.label.upper()))
        if key in seen_labels:
            continue
        seen_labels.add(key)
        row_key = best_row_for(c.template.label, rows)
        if row_key:
            linked.setdefault(row_key, []).append(c)
        else:
            standalone.append(c)

    for k, r in rows.items():
        cls = disc.class_for_tag(r.tag)
        hits = []
        for n in plan_pages:
            hits.extend(find_tag_hits(geoms[n], {r.tag}, row_word_ids))
        syms = linked.get(k, [])
        row_ev = Evidence(
            rows=[DefRow(r.tag, r.page_number, r.text.upper(), r.qty, cell_box([r.first_word]), r.first_word)],
            tag_hits=hits, symbol_counts=syms, geoms={**geoms, **ev.geoms},
        )
        methods, blocking, warnings = build_methods(row_ev, metas, cls, ParsedCount(r.tag.lower(), [r.tag], cls, [], None))
        model = r.model
        if syms and not model:
            g = geoms.get(syms[0].template.page_number)
            _h, _d, m_, f_ = _legend_block(g, syms[0].template.label) if g else ("", "", "", "")
            model = " ".join(x for x in (f_, m_) if x)
        defined_on = sorted(set(defined[k]) | {metas[c.template.page_number].label for c in syms})
        items.append(_item_from_methods(
            f"row:{k}", r.description or r.tag, f"Type {r.tag}" + (f" · {r.details}" if r.details else ""), model,
            methods, blocking, warnings, metas, defined_on, {"kind": "schedule_row", "tag": r.tag},
            f"Defined on {', '.join(defined_on)}."))

    for c in standalone:
        label = c.template.label
        geom = geoms.get(c.template.page_number) or ev.geoms.get(c.template.page_number)
        heading, desc, model, mfr = _legend_block(geom, label) if geom else (label, "", "", "")
        model_text = " ".join(x for x in (mfr, model) if x).strip()
        hm = _LABELLED.match(heading)
        if hm and _MODEL_KEYS.match(hm.group("k").strip()):         # the entry's heading line IS its model line
            model_text = model_text or hm.group("v").strip()
            heading = hm.group("v").strip()
        methods, blocking, warnings = build_methods(Evidence(symbol_counts=[c], geoms=ev.geoms), metas, syn, parsed)
        lk = re.sub(r"\W+", "", label.upper())
        items.append(_item_from_methods(
            f"legend:{c.template.page_number}:{lk}", heading, desc, model_text,
            methods, blocking, warnings, metas, [metas[c.template.page_number].label],
            {"kind": "legend_symbol", "label": label, "page_number": c.template.page_number},
            f"Legend entry on {metas[c.template.page_number].label}."))
    if standalone:
        notes.append("Legend items are counted by matching their symbol on the plan; a type with no legend symbol is not included.")

    if not items:
        notes.append(f"No {disc.label.lower()} schedule rows or legend entries could be read from the {len(pages)} sheet(s) "
                     f"searched. The set may not include {disc.label.lower()} sheets, or they are drawn in a way that "
                     f"cannot be read; nothing was assumed.")
    return DisciplineResult(disc.key, disc.label, items, notes, pages)
