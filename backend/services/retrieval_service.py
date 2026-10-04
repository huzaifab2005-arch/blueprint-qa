"""Page retrieval for the drawing assistant.

Drawing text is full of tokens a normal full-text engine mangles (RTU-1, 2X4,
E2.01, 3/M2.02), so this uses a small BM25 ranker with a tokenizer built for
them. A drawing set is tens to a few hundred pages, so scoring in memory per
question is fast and keeps the index DB-agnostic: the "index" is the stored
per-page text and metadata.

Pure functions only; no I/O.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass

from backend.services.drawing_metadata import SHEET_ID_RE, normalise_sheet_key

# 2x4, 2 X 4, 2'x4', 2'-0" x 4'-0" (first pair only) -> 2X4
_DIMENSION_RE = re.compile(r"(\d+)\s*['\"]?\s*[xX×]\s*(\d+)")
_TOKEN_RE = re.compile(r"[A-Z0-9]+(?:[-.][A-Z0-9]+)*")
_SPLIT_RE = re.compile(r"[-.]")

STOPWORDS = {
    "A", "AN", "AND", "ARE", "AS", "AT", "BE", "BY", "DO", "DOES", "FOR", "FROM",
    "HOW", "I", "IN", "IS", "IT", "OF", "ON", "OR", "THE", "THERE", "THIS", "TO",
    "WHAT", "WHERE", "WHICH", "WHO", "WITH", "SHOWN", "SHOW", "SHOWS", "MANY",
    "MUCH", "ALL", "ANY", "CAN", "YOU", "ME", "TELL", "PAGE", "SHEET", "DRAWING",
    "DRAWINGS", "THAT", "THESE", "THOSE", "HAVE", "HAS", "WHEN", "ABOUT", "FIND",
}

# Trade vocabulary: a question rarely uses the same word the drawing does.
# Expansions are low-weight, so a literal match always outranks a synonym.
SYNONYMS: dict[str, list[str]] = {
    "LIGHT": ["LUMINAIRE", "FIXTURE", "LIGHTING", "LED"],
    "LIGHTS": ["LUMINAIRE", "FIXTURE", "LIGHTING", "LED"],
    "LIGHTING": ["LUMINAIRE", "FIXTURE", "LIGHT"],
    "FIXTURE": ["LUMINAIRE", "LIGHTING", "LIGHT"],
    "FIXTURES": ["LUMINAIRE", "LIGHTING", "LIGHT", "FIXTURE"],
    "LUMINAIRE": ["FIXTURE", "LIGHTING", "LIGHT"],
    "DUCT": ["DUCTWORK", "SUPPLY", "RETURN", "AIR"],
    "DUCTS": ["DUCT", "DUCTWORK", "SUPPLY", "RETURN"],
    "DUCTWORK": ["DUCT"],
    "RTU": ["ROOFTOP", "UNIT", "HVAC"],
    "AHU": ["AIR", "HANDLING", "UNIT"],
    "MODEL": ["MFR", "MANUFACTURER", "TYPE", "CATALOG"],
    "MANUFACTURER": ["MFR", "MODEL"],
    "SIZE": ["DIMENSION", "DIM"],
    "HEIGHT": ["AFF", "HT"],
    "HEIGHTS": ["HEIGHT", "AFF"],
    "TALL": ["HEIGHT", "AFF"],
    "CEILING": ["CLG"],
    "DIMENSIONS": ["DIMENSION", "SIZE", "DIM"],
    "DIMENSION": ["SIZE", "DIM"],
    "WIDTH": ["WIDE", "DIMENSION"],
    "DEPTH": ["DEEP", "DIMENSION"],
    "SIZES": ["SIZE", "DIMENSION", "DIM"],
    "PANEL": ["PANELBOARD"],
    "SCHEDULE": ["SCHEDULES"],
    "RESTROOM": ["TOILET", "WC", "LAVATORY"],
    "SPRINKLER": ["FIRE", "PROTECTION", "FP"],
}


# A.F.F., U.N.O., C.W. -> AFF, UNO, CW. Without this "A.F.F." is three one-letter
# tokens and a question about height can never reach it. Only single-letter groups
# are joined, so sheet numbers like E2.01 are untouched.
_DOTTED_ABBR_RE = re.compile(r"\b(?:[A-Z]\.){2,}(?=\s|$|[^A-Z0-9])")


# "BACK OF HOUSE" in a drawing and "BOH" in a question are the same thing. Joining
# them into one token beats expanding BOH into BACK + HOUSE, which matches any page
# that happens to contain either word.
_AREA_PHRASES = (
    (re.compile(r"\bBACK[\s-]+OF[\s-]+(?:THE[\s-]+)?HOUSE\b"), "BOH"),
    (re.compile(r"\bFRONT[\s-]+OF[\s-]+(?:THE[\s-]+)?HOUSE\b"), "FOH"),
)


def normalise_text(text: str) -> str:
    text = _DIMENSION_RE.sub(lambda m: f"{m.group(1)}X{m.group(2)}", text.upper())
    text = _DOTTED_ABBR_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    for pattern, token in _AREA_PHRASES:
        text = pattern.sub(token, text)
    return text


# Words that say what KIND of answer is wanted, not what the topic is. "washroom
# dimensions" is about the washroom; "dimensions" appears all over specification
# prose and must not outrank the sheet that is actually about the washroom.
INTENT_WORDS = {
    "DIMENSION", "DIMENSIONS", "DIM", "SIZE", "SIZES", "HEIGHT", "HEIGHTS", "WIDTH",
    "LENGTH", "DEPTH", "THICKNESS", "TALL", "WIDE", "LONG", "DEEP", "MODEL", "MODELS",
    "MANUFACTURER", "MAKE", "BRAND", "LIST", "ITEMS", "ITEM", "QUANTITY", "QTY",
    "NUMBER", "COUNT", "TOTAL",
}
INTENT_WEIGHT = 0.3


def _expand_token(tok: str) -> list[str]:
    """A compound token is indexed whole and as its parts: RTU-1 -> RTU-1, RTU, 1."""
    parts = [p for p in _SPLIT_RE.split(tok) if p]
    out = [tok]
    if len(parts) > 1:
        out.extend(parts)
    return out


def tokenize(text: str) -> list[str]:
    """Index-side tokenizer: whole compound tokens plus their parts."""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(normalise_text(text)):
        out.extend(_expand_token(tok))
    return out


def query_terms(question: str) -> dict[str, float]:
    """Weighted query terms. Distinctive tokens (RTU-1, 2X4, E2.01) weigh most."""
    weights: dict[str, float] = {}

    def add(term: str, w: float) -> None:
        if term in STOPWORDS or not term:
            return
        weights[term] = max(weights.get(term, 0.0), w)

    for tok in _TOKEN_RE.findall(normalise_text(question)):
        has_digit = any(c.isdigit() for c in tok)
        compound = bool(_SPLIT_RE.search(tok))
        # Whole token: strong when it looks like a tag, sheet id or size.
        add(tok, 2.5 if (has_digit and (compound or re.search(r"[A-Z]", tok))) else 1.0)
        if compound:
            for part in (p for p in _SPLIT_RE.split(tok) if p):
                add(part, 0.8)
        for syn in SYNONYMS.get(tok, []):
            add(syn, 0.4)
        # Crude plural folding so "fixtures" matches "FIXTURE".
        if tok.endswith("S") and len(tok) > 3:
            add(tok[:-1], 0.7)
    # Answer-type words (and anything expanded from them) are weak evidence of topic.
    return {t: min(w, INTENT_WEIGHT) if t in INTENT_WORDS else w for t, w in weights.items()}


def _osa_distance(a: str, b: str) -> int:
    """Edit distance counting an adjacent swap as one edit (hieght -> height = 1)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def correct_typos(terms: dict[str, float], doc_freq: Counter) -> dict[str, float]:
    """Add the document's own spelling for query words the document never uses.

    Only plain alphabetic words of 5+ letters that appear nowhere in the document
    are candidates, and only document words within one edit (two for long words)
    are offered, so tags and part numbers are never "corrected".
    """
    extra: dict[str, float] = {}
    for term, weight in terms.items():
        if len(term) < 5 or not term.isalpha() or doc_freq.get(term):
            continue
        limit = 1 if len(term) < 8 else 2
        best, best_df = None, 0
        for word, df in doc_freq.items():
            if abs(len(word) - len(term)) > limit or not word.isalpha() or len(word) < 4:
                continue
            if df > best_df and _osa_distance(term, word) <= limit:
                best, best_df = word, df
        if best:
            extra[best] = max(extra.get(best, 0.0), weight * 0.9)
    return extra


_DIMENSION_QUESTION_RE = re.compile(
    r"\b(dimensions?|sizes?|heights?|widths?|lengths?|depths?|clearances?|tall|wide|long|deep|"
    r"far|distance|thick(?:ness)?|how big|how large)\b",
    re.IGNORECASE,
)
_DIMENSION_TOKEN_RE = re.compile(r"\d+\s*'\s*-\s*\d|\d+\s*\"")


def dimension_density(text: str) -> float:
    """0..1: how dimension-heavy a page is (feet-inch and inch marks). Plans and
    details score high, specification prose near zero."""
    return min(1.0, len(_DIMENSION_TOKEN_RE.findall(text)) / 30)


@dataclass
class PageDoc:
    page_number: int
    text: str
    sheet_number: str | None = None
    sheet_title: str | None = None


@dataclass
class ScoredPage:
    page_number: int
    score: float
    reasons: list[str]


# Plain numeric sheet numbers (1.3, 1.1a) look like any decimal, so in a question
# they only count after a cue word or a detail-reference slash: "sheet 1.3", "on 1.3",
# "detail 1/1.3".
_NUMERIC_REF_RE = re.compile(
    r"(?:\b(?:sheets?|drawings?|dwgs?|pages?|on|in|see|refer to)\s+|/\s*)(\d{1,2}\.\d{1,2}[a-z]?)\b",
    re.IGNORECASE,
)


def sheet_refs_in(question: str, pages: list[PageDoc]) -> list[int]:
    """Page numbers whose sheet number is named in the question ('3/M2.02', 'E2.01',
    'sheet 1.3')."""
    by_key = {
        normalise_sheet_key(p.sheet_number): p.page_number
        for p in pages
        if p.sheet_number
    }
    found: list[int] = []
    candidates = [m.group(0) for m in SHEET_ID_RE.finditer(question.upper())]
    candidates += [m.group(1) for m in _NUMERIC_REF_RE.finditer(question)]
    for cand in candidates:
        page = by_key.get(normalise_sheet_key(cand))
        if page and page not in found:
            found.append(page)
    return found


def rank_pages(
    question: str,
    pages: list[PageDoc],
    top_k: int = 4,
    min_relative_score: float = 0.2,
) -> list[ScoredPage]:
    """Rank pages for a question. Returns at most top_k pages with a positive score."""
    return rank_pages_detailed(question, pages, top_k, min_relative_score)[0]


def rank_pages_detailed(
    question: str,
    pages: list[PageDoc],
    top_k: int = 4,
    min_relative_score: float = 0.2,
) -> tuple[list[ScoredPage], list[ScoredPage]]:
    """Like rank_pages, plus the pages that matched well enough but did not fit in
    top_k. Callers use the overflow to say the answer may be incomplete.

    Pages named explicitly by sheet number in the question are always included
    first. The sheet number and title are indexed with extra weight so
    "lighting schedule" finds the sheet titled LIGHTING SCHEDULE.
    """
    if not pages:
        return [], []

    terms = query_terms(question)
    pinned = sheet_refs_in(question, pages)
    asks_dimensions = bool(_DIMENSION_QUESTION_RE.search(question))

    docs: dict[int, Counter] = {}
    lengths: dict[int, int] = {}
    page_text = {p.page_number: p.text or "" for p in pages}
    for p in pages:
        toks = tokenize(p.text or "")
        if p.sheet_title:
            toks += tokenize(p.sheet_title) * 3
        if p.sheet_number:
            toks += tokenize(p.sheet_number) * 3
        docs[p.page_number] = Counter(toks)
        lengths[p.page_number] = max(len(toks), 1)

    # Spell-correct against the words this document actually uses.
    vocab: Counter = Counter()
    for counts in docs.values():
        vocab.update(counts.keys())
    for term, weight in correct_typos(terms, vocab).items():
        if term in INTENT_WORDS:
            weight = min(weight, INTENT_WEIGHT)
        terms[term] = max(terms.get(term, 0.0), weight)

    n = len(pages)
    avg_len = sum(lengths.values()) / n
    k1, b = 1.5, 0.75

    df: Counter = Counter()
    for counts in docs.values():
        for term in terms:
            if term in counts:
                df[term] += 1

    scores: dict[int, float] = {}
    reasons: dict[int, list[str]] = {}
    for page_number, counts in docs.items():
        score = 0.0
        hit: list[str] = []
        for term, weight in terms.items():
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[page_number] / avg_len))
            score += weight * idf * norm
            hit.append(term)
        if score > 0:
            if asks_dimensions:
                # A dimension question is answered on plans and details, not in prose.
                score *= 1 + 0.4 * dimension_density(page_text[page_number])
            scores[page_number] = score
            reasons[page_number] = hit

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    result: list[ScoredPage] = []
    seen: set[int] = set()

    for page_number in pinned:
        result.append(
            ScoredPage(page_number, scores.get(page_number, 0.0), ["sheet named in question"] + reasons.get(page_number, []))
        )
        seen.add(page_number)

    overflow: list[ScoredPage] = []
    if ranked:
        cutoff = ranked[0][1] * min_relative_score
        for page_number, score in ranked:
            if page_number in seen or score < cutoff:
                continue
            scored = ScoredPage(page_number, score, reasons[page_number])
            if len(result) >= top_k:
                overflow.append(scored)
            else:
                result.append(scored)
                seen.add(page_number)

    return result[:top_k], overflow


def select_relevant_text(text: str, question: str, max_chars: int) -> str:
    """Fit page text into a prompt budget, keeping the lines that matter first.

    Lines containing a query term (plus one line of context either side, which
    keeps schedule rows readable) are kept ahead of everything else. Original
    line order is preserved.
    """
    if len(text) <= max_chars:
        return text

    terms = {t for t, w in query_terms(question).items() if w >= 0.7}
    lines = text.splitlines()
    keep: set[int] = set()
    budget = max_chars

    def take(i: int) -> None:
        nonlocal budget
        if 0 <= i < len(lines) and i not in keep and budget > 0:
            keep.add(i)
            budget -= len(lines[i]) + 1

    for i, line in enumerate(lines):
        if terms & set(tokenize(line)):
            for j in (i - 1, i, i + 1):
                take(j)
    for i in range(len(lines)):
        if budget <= 0:
            break
        take(i)

    return "\n".join(lines[i] for i in sorted(keep))
