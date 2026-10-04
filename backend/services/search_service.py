"""Search across the pages of an indexed drawing set.

This is literal search for finding things, not the ranking used to pick pages for a
question. A user typing "RTU-1", "2x4", "10'-8" or "1.3" expects pages that contain
that, shown with where it appears, not pages that are loosely about it. Rules:

  * every word must be present on a page (AND); quoted text is a phrase. If no page
    has them all, pages with some of them are shown and the answer says so;
  * the last word may be a prefix, so results appear while typing;
  * a sheet number ("1.3", "go to E2.01", "page 7") finds that sheet first;
  * tags and sizes normalise the way drawings write them: RTU-1 = RTU1, 2'x4' = 2x4;
  * a query that matches nothing offers a spelling from the document's own words.

Pure functions over page records; no I/O.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from backend.services.drawing_metadata import normalise_sheet_key, page_label
from backend.services.grounding import alnum
from backend.services.retrieval_service import STOPWORDS, _TOKEN_RE, correct_typos, normalise_text

_NAV_WORDS_RE = re.compile(r"^\s*(?:go\s*to|goto|open|show|find|jump\s*to|sheet|sheets|drawing|dwg)\s+", re.I)
_PAGE_NO_RE = re.compile(r"^\s*(?:go\s*to\s+)?(?:pdf\s+)?page\s+(\d{1,4})\s*$", re.I)


@dataclass
class PageRecord:
    page_number: int
    text: str
    sheet_number: str | None = None
    sheet_title: str | None = None


@dataclass
class Query:
    raw: str
    terms: list[str] = field(default_factory=list)       # alnum tokens, in order
    phrases: list[str] = field(default_factory=list)     # alnum, no separators
    phrase_text: list[str] = field(default_factory=list)  # as typed (for display/highlighting)
    # Dimensions keep their foot/inch marks: 10'-8" must not match "DOOR 108".
    patterns: list[re.Pattern] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.terms and not self.phrases


@dataclass
class Snippet:
    text: str
    spans: list[tuple[int, int]]


@dataclass
class SearchResult:
    page_number: int
    label: str
    sheet_number: str | None
    sheet_title: str | None
    score: float
    match_count: int
    kind: str = "text"            # text | sheet | page
    title_match: bool = False
    snippets: list[Snippet] = field(default_factory=list)


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult]
    mode: str                     # all | partial | none
    suggestion: str | None = None
    total_pages: int = 0


def dimension_pattern(raw: str) -> re.Pattern:
    """Regex for a dimension as typed: the digit groups joined by the marks between them."""
    groups = re.findall(r"\d+", raw)
    gaps = re.findall(r"\d+(\D*?)(?=\d)", raw)
    parts = [re.escape(groups[0])]
    for gap, grp in zip(gaps, groups[1:]):
        if "'" in gap or "’" in gap:
            sep = r"""\s*['’]\s*-?\s*"""
        elif '"' in gap or "”" in gap:
            sep = r"""\s*["”]\s*-?\s*"""
        elif "/" in gap:
            sep = r"\s*/\s*"
        elif "-" in gap:
            sep = r"\s*-\s*"
        else:
            sep = r"\s+"
        parts.append(sep + re.escape(grp))
    return re.compile(r"(?<![A-Za-z0-9])" + "".join(parts) + r"(?![A-Za-z0-9])")


def parse_query(raw: str) -> Query:
    q = Query(raw=raw)
    # A dimension (10'-8", 6'-6", 3/4") is one thing; split into numbers it would match
    # any page that has a 10 and an 8 somewhere.
    if re.fullmatch(r"[\d\s'\"’”\-/.x×]+", raw.strip()) and re.search(r"['\"’”]", raw) and alnum(raw):
        q.phrases.append(alnum(raw))
        q.phrase_text.append(raw.strip())
        q.patterns.append(dimension_pattern(raw))
        return q
    for m in re.finditer(r'"([^"]+)"', raw):
        text = m.group(1).strip()
        if alnum(text):
            q.phrases.append(alnum(text))
            q.phrase_text.append(text)
    rest = re.sub(r'"[^"]*"', " ", raw)
    tokens = [alnum(t) for t in _TOKEN_RE.findall(normalise_text(rest))]
    tokens = [t for t in tokens if t]
    meaningful = [t for t in tokens if t not in {alnum(w) for w in STOPWORDS}]
    q.terms = list(dict.fromkeys(meaningful or tokens))
    return q


def page_tokens(text: str) -> list[str]:
    """The page's words as search sees them: one alnum token per word-like run, so
    'RTU-1' is RTU1 and '2'x4'' is 2X4. Sub-parts are not added; search is literal."""
    return [alnum(t) for t in _TOKEN_RE.findall(normalise_text(text))]


def _term_matches(term: str, tokens: Counter, prefix: bool) -> int:
    n = tokens.get(term, 0)
    if prefix and len(term) >= 3:
        n += sum(c for tok, c in tokens.items() if tok != term and tok.startswith(term))
    return n


def _flex_pattern(term: str, prefix: bool) -> re.Pattern:
    """A regex that finds `term` (alnum) in original text however it was punctuated:
    RTU1 matches 'RTU-1', 2X4 matches "2'x4'"."""
    body = r"[^A-Za-z0-9]{0,2}".join(re.escape(c) for c in term)
    tail = "" if prefix else r"(?![A-Za-z0-9])"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, re.IGNORECASE)


def _snippets(text: str, query: Query, limit: int = 3, width: int = 150) -> list[Snippet]:
    pats = [_flex_pattern(t, prefix=(i == len(query.terms) - 1)) for i, t in enumerate(query.terms)]
    pats += list(query.patterns) or [_flex_pattern(p, prefix=False) for p in query.phrases]
    scored: list[tuple[int, int, str, list[tuple[int, int]]]] = []
    for ln_no, line in enumerate(text.splitlines()):
        line = re.sub(r"\s{2,}", "  ", line.strip())
        if not line:
            continue
        spans = [m.span() for p in pats for m in p.finditer(line)]
        if not spans:
            continue
        covered = sum(1 for p in pats if p.search(line))
        scored.append((covered, -ln_no, line, spans))
    scored.sort(reverse=True)
    out: list[Snippet] = []
    seen: set[str] = set()
    for _covered, _neg, line, spans in scored:
        if len(out) >= limit:
            break
        start = max(min(s for s, _ in spans) - 40, 0)
        end = min(start + width, len(line))
        window = line[start:end]
        key = re.sub(r"\W+", "", window.lower())
        if key in seen:
            continue
        seen.add(key)
        shifted = [(max(s - start, 0), min(e - start, len(window))) for s, e in spans if e > start and s < end]
        prefix = "…" if start else ""
        out.append(Snippet(prefix + window + ("…" if end < len(line) else ""),
                           [(s + len(prefix), e + len(prefix)) for s, e in shifted]))
    return out


def search_pages(pages: list[PageRecord], raw_query: str, limit: int = 40) -> SearchResponse:
    raw = raw_query.strip()
    resp = SearchResponse(query=raw, results=[], mode="none", total_pages=len(pages))
    if not raw:
        return resp

    results: dict[int, SearchResult] = {}

    # "page 7": a PDF page number.
    m = _PAGE_NO_RE.match(raw)
    if m and 1 <= int(m.group(1)) <= max((p.page_number for p in pages), default=0):
        n = int(m.group(1))
        rec = next((p for p in pages if p.page_number == n), None)
        if rec:
            results[n] = SearchResult(n, page_label(n, rec.sheet_number), rec.sheet_number, rec.sheet_title,
                                      1000.0, 0, kind="page")

    # A sheet number, optionally after "go to" / "sheet". ("page 17" is a PDF page, never
    # sheet 1.7.) A purely numeric sheet number must be typed with its dot: its key
    # ("17") is otherwise indistinguishable from a page number or a plain number.
    stripped = _NAV_WORDS_RE.sub("", raw)
    key = normalise_sheet_key(stripped)
    if key and not _PAGE_NO_RE.match(raw):
        for rec in pages:
            numeric_sheet = bool(rec.sheet_number) and not re.search(r"[A-Za-z]", rec.sheet_number.rstrip("abcdefghijklmnopqrstuvwxyz"))
            if numeric_sheet and "." not in stripped:
                continue
            if rec.sheet_number and normalise_sheet_key(rec.sheet_number) == key:
                results[rec.page_number] = SearchResult(
                    rec.page_number, page_label(rec.page_number, rec.sheet_number), rec.sheet_number,
                    rec.sheet_title, 1000.0, 0, kind="sheet")

    query = parse_query(raw)
    if query.empty:
        resp.results = sorted(results.values(), key=lambda r: -r.score)[:limit]
        resp.mode = "all" if resp.results else "none"
        return resp

    # Per-page token counts.
    counts: dict[int, Counter] = {p.page_number: Counter(page_tokens(p.text)) for p in pages}
    title_counts: dict[int, Counter] = {
        p.page_number: Counter(page_tokens(" ".join(filter(None, [p.sheet_title, p.sheet_number])))) for p in pages
    }
    phrase_pats = list(query.patterns) or [_flex_pattern(ph, prefix=False) for ph in query.phrases]
    n_pages = len(pages)
    last = len(query.terms) - 1

    idf = {}
    for i, t in enumerate(query.terms):
        d = sum(1 for pn in counts if _term_matches(t, counts[pn], i == last) or title_counts[pn].get(t))
        idf[t] = math.log(1 + (n_pages - d + 0.5) / (d + 0.5))

    partial: dict[int, SearchResult] = {}
    for p in pages:
        pn = p.page_number
        per_term = []
        for i, t in enumerate(query.terms):
            body = _term_matches(t, counts[pn], i == last)
            title = title_counts[pn].get(t, 0)
            per_term.append((t, body, title))
        phrase_hits = [len(pat.findall(p.text)) for pat in phrase_pats]
        matched_terms = [t for t, body, title in per_term if body or title]
        has_all = len(matched_terms) == len(query.terms) and all(phrase_hits)
        any_hit = bool(matched_terms) or any(phrase_hits)
        if not any_hit:
            continue
        score = sum(idf[t] * (1 + math.log(1 + body) + 3 * (1 if title else 0)) for t, body, title in per_term if body or title)
        score += sum(5 * (1 + math.log(1 + h)) for h in phrase_hits if h)
        total = sum(body for _t, body, _ti in per_term) + sum(phrase_hits)
        rec = SearchResult(pn, page_label(pn, p.sheet_number), p.sheet_number, p.sheet_title, score, total,
                           title_match=any(title for _t, _b, title in per_term))
        (results if has_all else partial).setdefault(pn, rec)

    text_results = [r for r in results.values() if r.kind == "text"]
    if text_results or any(r.kind in ("sheet", "page") for r in results.values()):
        resp.mode = "all"
    elif partial:
        results = {**partial, **results}
        resp.mode = "partial"

    ordered = sorted(results.values(), key=lambda r: (-r.score, r.page_number))[:limit]
    by_no = {p.page_number: p for p in pages}
    for r in ordered:
        if r.kind == "text":
            r.snippets = _snippets(by_no[r.page_number].text, query)
    resp.results = ordered
    if not ordered:
        resp.mode = "none"
        resp.suggestion = _suggest(raw, query, counts)
    return resp


def _suggest(raw: str, query: Query, counts: dict[int, Counter]) -> str | None:
    """'Did you mean': replace words found nowhere in the set by the closest word that is."""
    from backend.services.retrieval_service import _osa_distance

    vocab: Counter = Counter()
    for c in counts.values():
        vocab.update(c.keys())
    out = raw
    for term in query.terms:
        if len(term) < 4 or not term.isalpha() or vocab.get(term):
            continue
        budget = 1 if len(term) <= 5 else 2
        best, best_key = None, None
        for word, n in vocab.items():
            if not word.isalpha() or len(word) < 4 or abs(len(word) - len(term)) > budget:
                continue
            d = _osa_distance(term, word)
            if d <= budget and (best_key is None or (d, -n) < best_key):
                best, best_key = word, (d, -n)
        if best:
            out = re.sub(re.escape(term), best.lower(), out, flags=re.IGNORECASE)
    return out if out.lower() != raw.lower() else None
