"""Count objects in a drawing set and say how far the number can be trusted.

Evidence, most reliable first (every method reads the PDF's own data except the last):

  tag_instances   a tag (L1, RTU-1) located on plan sheets, each occurrence marked
  symbol          a legend swatch matched against the plan's vector shapes
  schedule_qty    the QTY column of schedule rows that define the object
  schedule_rows   the number of distinct units a schedule lists (RTU-1, RTU-2 -> 2)
  vision          a vision-model estimate; only when nothing above exists

Confidence comes from agreement between independent methods, never from a model's
say-so:

  cross_checked        two or more methods agree and nothing is in doubt
  single_source        one method found it; nothing contradicts it
  needs_verification   vision only, methods disagree, or something could not be
                       resolved (an area filter, mixed symbol sizes, overlapping sheets)
  not_found            nothing countable was found

No status means the number is certain. Even "cross_checked" means "two readings of
the drawing agree"; the UI says so and shows every counted object on the sheet.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

from backend.config import get_settings
from backend.services.counting.objects import (
    ObjectClass, ParsedCount, adhoc_class, singular, size_variants,
)
from backend.services.counting.pdf_geometry import PageGeometry, load_geometry
from backend.services.counting.symbol_counter import (
    SymbolCount, count_template, find_legend_templates, looks_like_callout_bubbles, resolve_conflicts,
)
from backend.services.counting.tag_counter import (
    DefRow, TagHit, clean_word, distinct_tags, find_rows, find_tag_hits, scan_class_tags, tag_key,
)
from backend.services.drawing_metadata import page_label
from backend.services.retrieval_service import PageDoc, normalise_text, rank_pages_detailed

logger = logging.getLogger(__name__)
settings = get_settings()

CROSS_CHECKED = "cross_checked"
SINGLE_SOURCE = "single_source"
NEEDS_VERIFICATION = "needs_verification"
NOT_FOUND = "not_found"

# Order in which methods are preferred for the headline number: what is observed on
# the plan beats what a schedule says should be there.
MIN_PLAN_SHAPES = 100   # fewer vector shapes than this: a text page, not a drawing

METHOD_PRIORITY = ("tag_instances", "symbol", "schedule_qty", "schedule_rows", "vision")


@dataclass
class Marker:
    page_number: int
    x: float
    y: float
    w: float
    h: float
    label: str = ""


@dataclass
class MethodResult:
    method: str
    quantity: int
    per_page: dict[int, int]
    breakdown: dict[str, int]
    detail: str
    markers: list[Marker] = field(default_factory=list)
    summed_sheets: bool = False


@dataclass
class PageMeta:
    page_number: int
    sheet_number: str | None
    sheet_title: str | None
    text: str
    image_path: str | None = None

    @property
    def label(self) -> str:
        return page_label(self.page_number, self.sheet_number)


@dataclass
class CountOutcome:
    status: str
    quantity: int | None
    object_label: str
    summary: str
    methods: list[MethodResult] = field(default_factory=list)
    primary: str | None = None
    definitions: list[dict] = field(default_factory=list)   # what was matched, with sheet
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages_searched: list[int] = field(default_factory=list)

    @property
    def confidence(self) -> str | None:
        return {CROSS_CHECKED: "high", SINGLE_SOURCE: "medium", NEEDS_VERIFICATION: "low"}.get(self.status)

    @property
    def verified(self) -> bool:
        return self.status in (CROSS_CHECKED, SINGLE_SOURCE)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _plural(noun: str, n: int) -> str:
    if n == 1:
        return noun
    if noun.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    return noun + "s"


def _is_schedule_page(meta: PageMeta) -> bool:
    title = (meta.sheet_title or "").upper()
    return bool(re.search(r"\b(SCHEDULES?|LISTS?)\b", title))


def _kind_of(meta: PageMeta, cls: ObjectClass) -> str:
    """Which kind of drawing a sheet is (lighting plan, ceiling plan, ...). Sheets of
    different kinds often show the same objects, so they must not be added together."""
    title = (meta.sheet_title or "").upper()
    for i, word in enumerate(cls.plan_titles):
        if word in title:
            return f"{i}:{word}"
    return f"9:{title}"


def _phrase_re(phrase: str) -> re.Pattern:
    """A keyword phrase as a whole-word, plural-tolerant pattern: 'WATER CLOSET' must
    appear as that phrase, not as 'WATER' somewhere and 'CLOSET' somewhere else."""
    parts = [re.escape(singular(w)) + r"(?:E?S)?" for w in phrase.split()]
    return re.compile(r"\b" + r"[\s\-]+".join(parts) + r"\b")


def _mentions_object(text: str, cls: ObjectClass, parsed: ParsedCount) -> bool:
    """Does this page's text name the object at all? (class keywords or the user's words)"""
    norm = normalise_text(text or "")
    phrases = [_phrase_re(k) for k in cls.keywords] + [_phrase_re(w) for w in parsed.words]
    return any(p.search(norm) for p in phrases)


def _legend_matcher(cls: ObjectClass, parsed: ParsedCount):
    phrases = [_phrase_re(k) for k in cls.legend_keywords]
    descriptors = {w for w in parsed.specific_words if not (cls.tag_re and w in cls.tag_re)}
    size_opts = [size_variants(s) for s in parsed.sizes]

    def matches(text: str) -> bool:
        norm = normalise_text(text)
        # A heading names the object in a few words. Long lines are notes, and
        # numbered lines ("8. ALL FLOOR ...") are note lists.
        # Length is counted in letters: a letter-spaced heading ("S LI M R E C E S S E D")
        # is many "words" but a short title.
        compact = re.sub(r"\s+", "", norm)
        if len(compact) > 60 or re.match(r"^\s*\d+[.)]\s", norm) or norm.rstrip().endswith((",", ";")):
            return False
        if not any(p.search(norm) for p in phrases):
            return False
        words = {singular(w) for w in re.findall(r"[A-Z][A-Z0-9]*", norm)}
        compact_norm = re.sub(r"[^A-Z0-9]", "", norm)
        if descriptors and not all(d in words or d in compact_norm for d in descriptors):
            return False
        if size_opts and not all(any(v in norm.replace(" ", "") for v in opts) for opts in size_opts):
            return False
        return True

    return matches


def _merge_sheets(
    per_page: dict[int, int],
    metas: dict[int, PageMeta],
    cls: ObjectClass,
) -> tuple[dict[int, int], bool, list[str], list[str]]:
    """Decide which sheets' counts add up.

    Returns (kept per-page counts, summed_over_several_sheets, blocking, warnings).
    Several sheets of the same kind (FIRST FLOOR / SECOND FLOOR lighting plans) are
    summed; sheets of different kinds (lighting plan vs reflected ceiling plan) are
    treated as alternative views of the same objects and only the preferred kind is
    counted, with the others reported.
    """
    pages = [p for p, c in per_page.items() if c > 0]
    if len(pages) <= 1:
        return {p: per_page[p] for p in pages}, False, [], []
    kinds: dict[str, list[int]] = {}
    for p in pages:
        kinds.setdefault(_kind_of(metas[p], cls), []).append(p)
    if len(kinds) == 1:
        labels = ", ".join(metas[p].label for p in pages)
        return (
            {p: per_page[p] for p in pages}, True, [],
            [f"Counts from {len(pages)} sheets of the same type were added together ({labels}). "
             f"This assumes they show different areas, not the same objects twice."],
        )
    preferred = min(kinds)
    keep = kinds[preferred]
    others = [p for k, ps in kinds.items() if k != preferred for p in ps]
    kept = {p: per_page[p] for p in keep}
    kept_total = sum(kept.values())
    blocking, warnings = [], []
    for p in others:
        if per_page[p] == kept_total:
            warnings.append(f"{metas[p].label} also shows {per_page[p]}, which agrees; it was not added.")
        else:
            blocking.append(
                f"{metas[p].label} shows {per_page[p]} of the same kind of object but {kept_total} were counted on "
                f"{', '.join(metas[q].label for q in keep)}. The sheets disagree."
            )
    return kept, len(keep) > 1, blocking, warnings


def _template_key(t) -> tuple:
    """Identity of a legend entry on its page: where its swatch is."""
    return (t.page_number, round(t.legend_box[0]), round(t.legend_box[1]))


def _marker(geom: PageGeometry, box, label: str) -> Marker:
    x, y, w, h = geom.norm_box(*box)
    return Marker(geom.page_number, round(x, 5), round(y, 5), round(w, 5), round(h, 5), label)


# ── Evidence collection (blocking work; run in a thread) ────────────────────

@dataclass
class Evidence:
    rows: list[DefRow] = field(default_factory=list)
    wrong_size_rows: list[str] = field(default_factory=list)
    tag_hits: list[TagHit] = field(default_factory=list)          # on plan sheets
    symbol_counts: list[SymbolCount] = field(default_factory=list)
    legend_pages: dict[int, list[str]] = field(default_factory=dict)  # page -> legend labels matched
    legend_notes: list[str] = field(default_factory=list)
    geoms: dict[int, PageGeometry] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def collect_evidence(
    pdf_path: str, metas: dict[int, PageMeta], page_numbers: list[int], cls: ObjectClass, parsed: ParsedCount
) -> Evidence:
    ev = Evidence()
    matcher = _legend_matcher(cls, parsed)
    row_words: set[int] = set()
    plan_pages: list[int] = []

    for n in page_numbers:
        try:
            geom = load_geometry(pdf_path, n)
        except Exception as exc:  # a page that cannot be parsed must not sink the answer
            logger.warning("Could not read geometry of page %s: %s", n, exc)
            ev.errors.append(f"{metas[n].label} could not be read.")
            continue
        ev.geoms[n] = geom
        rows, wrong = find_rows(geom, cls, parsed)
        ev.rows.extend(rows)
        ev.wrong_size_rows.extend(f"{metas[n].label} {w}" for w in wrong)
        row_words |= {id(r.first_word) for r in rows}
        # Tags are only counted on drawings. A text-only page (specifications, notes)
        # has almost no vector shapes, and a code like "D882" there is a clause number.
        if not _is_schedule_page(metas[n]) and len(geom.shapes) >= MIN_PLAN_SHAPES:
            plan_pages.append(n)

        templates, notes = find_legend_templates(geom, matcher)
        # Notes about legend-like text with no symbol are only worth showing on a sheet
        # where a legend entry WAS matched: there they mean "other entries exist that
        # were not counted". Elsewhere they are just prose that mentions the object.
        if templates:
            ev.legend_notes.extend(notes)
            # Resolve against EVERY legend entry on the sheet, not just the ones asked
            # about: a potlight symbol is a sub-shape of a pendant, and another entry's
            # own swatch must never count as an instance. Only then keep the asked-for ones.
            # (A legend heading is a phrase; a short label beside a plan symbol, like "B2",
            # is not, or the plan's own fixtures would be taken for legend swatches.)
            everything, _ = find_legend_templates(
                geom, lambda t: len(re.sub(r"[^A-Za-z]", "", t)) >= 8 and not t.rstrip().endswith("."))
            asked = {_template_key(t) for t in templates}
            by_key = {_template_key(t): t for t in everything}
            for t in templates:
                by_key.setdefault(_template_key(t), t)
            all_templates = list(by_key.values())
            exclude = {id(m) for t in all_templates for m in t.members}
            counts = [count_template(geom, t, exclude) for t in all_templates]
            resolve_conflicts(counts)
            counts = [c for c in counts if _template_key(c.template) in asked]
            ev.legend_pages[n] = [t.label for t in templates]
            for c in counts:
                if looks_like_callout_bubbles(geom, c.instances):
                    ev.legend_notes.append(
                        f"'{c.template.label}' looks like a numbered note marker, not a drawing symbol; ignored.")
                    c.instances = []
            ev.symbol_counts.extend(counts)

    # Tag occurrences on plan sheets: the tags the schedule defines, else the class's own form.
    tags = {r.tag for r in ev.rows}
    restricted = bool(parsed.sizes or [w for w in parsed.specific_words if not (cls.tag_re and w in cls.tag_re)])
    for n in plan_pages:
        geom = ev.geoms.get(n)
        if geom is None:
            continue
        if tags:
            ev.tag_hits.extend(find_tag_hits(geom, tags, row_words))
        elif cls.tag_re and not restricted and not ev.wrong_size_rows:
            # No schedule names the object, so fall back to tags of the class's own
            # form (RTU-1, EF-2). Never when the question restricts by size or type:
            # a bare tag cannot honour that, and would count the wrong things.
            ev.tag_hits.extend(scan_class_tags(geom, cls, row_words))
    return ev


# ── Turning evidence into method results ────────────────────────────────────

def build_methods(
    ev: Evidence, metas: dict[int, PageMeta], cls: ObjectClass, parsed: ParsedCount
) -> tuple[list[MethodResult], list[str], list[str]]:
    methods: list[MethodResult] = []
    blocking: list[str] = []
    warnings: list[str] = []

    # schedule rows (deduplicated by tag: a schedule can repeat across sheets)
    rows_by_tag: dict[str, DefRow] = {}
    for r in ev.rows:
        rows_by_tag.setdefault(tag_key(r.tag), r)
    rows = list(rows_by_tag.values())

    # tag instances / distinct tags on plans
    if ev.tag_hits:
        by_tag = distinct_tags(ev.tag_hits)
        if cls.tag_mode == "unit":
            first = [hits[0] for hits in by_tag.values()]
            per_page: dict[int, int] = {}
            for h in first:
                per_page[h.page_number] = per_page.get(h.page_number, 0) + 1
            tags_txt = ", ".join(sorted(hits[0].tag for hits in by_tag.values()))
            methods.append(MethodResult(
                "tag_instances", len(by_tag), per_page,
                {hits[0].tag: 1 for hits in by_tag.values()},
                f"{len(by_tag)} distinct tags on the plans ({tags_txt})",
                [_marker(ev.geoms[h.page_number], (h.word.x0, h.word.top, h.word.x1, h.word.bottom), h.tag)
                 for hits in by_tag.values() for h in hits],
            ))
        else:
            per_page = {}
            for h in ev.tag_hits:
                per_page[h.page_number] = per_page.get(h.page_number, 0) + 1
            kept, summed, blk, wrn = _merge_sheets(per_page, metas, cls)
            blocking += blk
            warnings += wrn
            hits = [h for h in ev.tag_hits if h.page_number in kept]
            breakdown: dict[str, int] = {}
            for h in hits:
                breakdown[h.tag] = breakdown.get(h.tag, 0) + 1
            if hits:
                methods.append(MethodResult(
                    "tag_instances", len(hits), kept, breakdown,
                    "tag labels on the plan: " + ", ".join(f"{t} ×{c}" for t, c in sorted(breakdown.items())),
                    [_marker(ev.geoms[h.page_number], (h.word.x0, h.word.top, h.word.x1, h.word.bottom), h.tag)
                     for h in hits],
                    summed_sheets=summed,
                ))

    # symbols
    sym = [c for c in ev.symbol_counts if c.instances]
    if sym:
        per_page = {}
        for c in sym:
            per_page[c.template.page_number] = per_page.get(c.template.page_number, 0) + len(c.instances)
        kept, summed, blk, wrn = _merge_sheets(per_page, metas, cls)
        blocking += blk
        warnings += wrn
        used = [c for c in sym if c.template.page_number in kept]
        for c in used:
            if not c.uniform_scale:
                blocking.append(
                    f"Symbols matching '{c.template.label}' on {metas[c.template.page_number].label} appear at "
                    f"different sizes, so some may be look-alikes."
                )
            elif not c.same_size_as_swatch:
                warnings.append(
                    f"On {metas[c.template.page_number].label} the plan symbols for '{c.template.label}' are "
                    f"{c.median_scale:.2f}× the size of their legend swatch (consistent across all of them)."
                )
        warnings.append(
            "Only objects whose legend symbol was identified are counted this way; other types of the same "
            "object without a matching legend entry are not included."
        )
        if len(used) > 1 and parsed.sizes:
            warnings.append("More than one legend entry matches; their counts were added together.")
        breakdown = {}
        markers = []
        for c in used:
            breakdown[c.template.label] = breakdown.get(c.template.label, 0) + len(c.instances)
            for inst in c.instances:
                markers.append(_marker(ev.geoms[c.template.page_number],
                                       (inst.x0, inst.top, inst.x1, inst.bottom), c.template.label))
        total = sum(breakdown.values())
        methods.append(MethodResult(
            "symbol", total, {p: kept[p] for p in kept}, breakdown,
            "legend symbols matched on the plan: " + ", ".join(f"{t} ×{n}" for t, n in breakdown.items()),
            markers, summed_sheets=summed,
        ))

    # schedule quantities
    if rows:
        qtys = [r.qty for r in rows]
        if all(q is not None for q in qtys):
            total = sum(q for q in qtys if q is not None)
            methods.append(MethodResult(
                "schedule_qty", total,
                {r.page_number: sum(x.qty or 0 for x in rows if x.page_number == r.page_number) for r in rows},
                {r.tag: r.qty or 0 for r in rows},
                "QTY column of the schedule: " + ", ".join(f"{r.tag} ×{r.qty}" for r in rows),
            ))
        elif any(q is not None for q in qtys):
            warnings.append("Only some schedule rows have a quantity, so the schedule total was not used.")
        if cls.tag_mode == "unit":
            methods.append(MethodResult(
                "schedule_rows", len(rows), {r.page_number: 1 for r in rows}, {r.tag: 1 for r in rows},
                f"{len(rows)} schedule rows ({', '.join(r.tag for r in rows)})",
            ))
    return methods, blocking, warnings


def classify(methods: list[MethodResult], blocking: list[str]) -> tuple[str, MethodResult | None, list[str]]:
    """(status, primary method, notes). Pure; the heart of what 'verified' means."""
    notes: list[str] = []
    if not methods:
        return NOT_FOUND, None, notes
    ordered = sorted(methods, key=lambda m: METHOD_PRIORITY.index(m.method))
    primary = ordered[0]
    if primary.method == "vision":
        return NEEDS_VERIFICATION, primary, notes
    deterministic = [m for m in ordered if m.method != "vision"]
    quantities = {m.quantity for m in deterministic}
    if blocking:
        return NEEDS_VERIFICATION, primary, notes
    if len(deterministic) >= 2:
        if len(quantities) == 1:
            return CROSS_CHECKED, primary, notes
        others = "; ".join(f"{m.detail} = {m.quantity}" for m in deterministic if m.quantity != primary.quantity)
        notes.append(f"The sources disagree. Headline {primary.quantity} is from {primary.detail}; {others}.")
        return NEEDS_VERIFICATION, primary, notes
    return SINGLE_SOURCE, primary, notes


# ── Vision fallback ─────────────────────────────────────────────────────────

VISION_PROMPT = """Count the {what} on this construction drawing sheet ({label}).

{legend}Rules:
- Count only symbols you can actually see that match this object. Do not guess or estimate beyond what you can see.
- If the symbol is ambiguous, or the sheet is too small or crowded to count reliably, say so in "note" and lower "confidence".
- If there are none on this sheet, return 0.

Reply with a single JSON object: {{"count": <integer or null if you cannot tell>, "confidence": "high" or "medium" or "low", "note": "<what you counted and any doubt>"}}"""


async def vision_estimate(
    metas: list[PageMeta], cls: ObjectClass, parsed: ParsedCount, legend_hint: str
) -> MethodResult | None:
    from backend.services import assistant_service as A

    what = " ".join(parsed.sizes + parsed.words).lower() or cls.label
    per_page: dict[int, int] = {}
    notes: list[str] = []
    for meta in metas:
        if not meta.image_path:
            continue
        prompt = VISION_PROMPT.format(what=what, label=meta.label,
                                      legend=(f"The legend describes it as: {legend_hint}\n\n" if legend_hint else ""))
        try:
            data = await A.vision_json(prompt, meta.image_path, max_tokens=300)
        except Exception as exc:
            logger.warning("Vision count failed on page %s: %s", meta.page_number, exc)
            continue
        count = (data or {}).get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        if count:
            per_page[meta.page_number] = count
            notes.append(f"{meta.label}: {count}")
    if not per_page:
        return None
    return MethodResult(
        "vision", sum(per_page.values()), per_page, {},
        "a vision-model estimate from the page image (" + ", ".join(notes) + ")",
    )


# ── Entry point ─────────────────────────────────────────────────────────────

def _summary(outcome: CountOutcome, primary: MethodResult | None, parsed: ParsedCount, cls: ObjectClass) -> str:
    noun = " ".join(parsed.sizes).lower() + " " if parsed.sizes else ""
    noun = (noun + cls.label).strip()
    if outcome.status == NOT_FOUND:
        return ("No " + _plural(noun, 2) + " could be counted from the uploaded drawings. "
                "That does not prove there are none: the drawings may show them in a form that could not be read.")
    assert primary is not None
    q = primary.quantity
    label = f"{q} {_plural(noun, q)}"
    if outcome.status == CROSS_CHECKED:
        return (f"{label} counted. Two independent readings of the drawings agree. "
                f"Check the marked sheets before relying on the number.")
    if outcome.status == SINGLE_SOURCE:
        return (f"{label} counted from one source ({primary.detail}). "
                f"Verify against the marked sheet before relying on the number.")
    return (f"About {label}, but this count needs verification. "
            f"See the notes below and check the marked sheets.")


async def count_objects(
    question: str,
    parsed: ParsedCount,
    pages: list[PageMeta],
    pdf_path: str,
) -> CountOutcome:
    cls = parsed.object_class or adhoc_class(parsed)
    metas = {p.page_number: p for p in pages}
    blocking: list[str] = []
    warnings: list[str] = []

    query = " ".join(parsed.words + parsed.sizes + list(cls.keywords[:3]) + ["schedule", "legend"])
    ranked, _overflow = rank_pages_detailed(
        query,
        [PageDoc(p.page_number, p.text, p.sheet_number, p.sheet_title) for p in pages],
        top_k=settings.count_max_pages,
        min_relative_score=0.1,
    )
    candidates = [r.page_number for r in ranked]
    if not candidates:
        outcome = CountOutcome(NOT_FOUND, None, cls.label, "", pages_searched=[])
        outcome.summary = _summary(outcome, None, parsed, cls)
        return outcome

    ev = await asyncio.to_thread(collect_evidence, pdf_path, metas, candidates, cls, parsed)
    warnings += ev.errors + ev.legend_notes

    methods, blk, wrn = build_methods(ev, metas, cls, parsed)
    blocking += blk
    warnings += wrn

    if cls.tag_caveat and any(m.method in ("tag_instances", "schedule_rows", "schedule_qty") for m in methods):
        blocking.append(cls.tag_caveat)

    if parsed.area:
        blocking.append(
            f"You asked about '{parsed.area}'. Counting inside part of a building needs room boundaries, "
            f"which are not read, so this is the count for the whole sheet."
        )

    # Something about this object is on the sheets but nothing could be counted from it.
    saw_object_text = bool(ev.rows or ev.wrong_size_rows or ev.legend_pages or ev.symbol_counts)
    if not methods:
        if ev.wrong_size_rows:
            warnings.append(
                "Entries for this kind of object exist but not at the size you asked about: "
                + "; ".join(ev.wrong_size_rows[:4]) + "."
            )
        if ev.legend_pages and not ev.symbol_counts:
            warnings.append("A legend entry was found but its symbol could not be located on the plan.")
        elif ev.symbol_counts:
            warnings.append(
                "A legend symbol was found, but none of it appears on the plan (0 matches): "
                + ", ".join(c.template.label for c in ev.symbol_counts) + "."
            )
        # A vision model asked to count something that is not on the sheet will often
        # invent a number. So it is only asked when the sheets' text names the object
        # (present, but not identifiable by tag, schedule or legend), or when a sheet has
        # no readable text at all (a scan whose OCR failed), where text cannot tell us.
        mentioned = any(_mentions_object(metas[n].text, cls, parsed) for n in candidates)
        unreadable = any(len((metas[n].text or "").strip()) < 80 for n in candidates)
        if (not saw_object_text) and (mentioned or unreadable) and settings.count_vision_fallback and settings.nvidia_api_key:
            plan_metas = [metas[n] for n in candidates if not _is_schedule_page(metas[n])][:3]
            est = await vision_estimate(plan_metas, cls, parsed, "")
            if est:
                methods.append(est)
                warnings.append("No schedule, tag or legend symbol could be read for this object, so the number is a "
                                "vision-model estimate. Models count unreliably; verify on the sheet.")

    status, primary, notes = classify(methods, blocking)
    warnings += notes

    definitions = [
        {"page_number": r.page_number, "label": metas[r.page_number].label, "text": " ".join(r.text.split())[:160]}
        for r in {tag_key(r.tag): r for r in ev.rows}.values()
    ]
    for p, labels in ev.legend_pages.items():
        for lab in labels:
            definitions.append({"page_number": p, "label": metas[p].label, "text": f"Legend: {lab}"})

    outcome = CountOutcome(
        status=status,
        quantity=primary.quantity if primary else None,
        object_label=cls.label,
        summary="",
        methods=sorted(methods, key=lambda m: METHOD_PRIORITY.index(m.method)),
        primary=primary.method if primary else None,
        definitions=definitions,
        blocking=blocking,
        warnings=warnings,
        pages_searched=candidates,
    )
    outcome.summary = _summary(outcome, primary, parsed, cls)
    return outcome
