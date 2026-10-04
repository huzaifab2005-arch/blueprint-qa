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
    "BOH": ["BACK", "HOUSE"],
    "FOH": ["FRONT", "HOUSE"],
    "MODEL": ["MFR", "MANUFACTURER", "TYPE", "CATALOG"],
    "MANUFACTURER": ["MFR", "MODEL"],
    "SIZE": ["DIMENSION", "DIM"],
    "SIZES": ["SIZE", "DIMENSION", "DIM"],
    "PANEL": ["PANELBOARD"],
    "SCHEDULE": ["SCHEDULES"],
    "RESTROOM": ["TOILET", "WC", "LAVATORY"],
    "SPRINKLER": ["FIRE", "PROTECTION", "FP"],
}


def normalise_text(text: str) -> str:
    return _DIMENSION_RE.sub(lambda m: f"{m.group(1)}X{m.group(2)}", text.upper())


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
    return weights


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


def sheet_refs_in(question: str, pages: list[PageDoc]) -> list[int]:
    """Page numbers whose sheet number is named in the question ('3/M2.02', 'E2.01')."""
    by_key = {
        normalise_sheet_key(p.sheet_number): p.page_number
        for p in pages
        if p.sheet_number
    }
    found: list[int] = []
    for m in SHEET_ID_RE.finditer(question.upper()):
        page = by_key.get(normalise_sheet_key(m.group(0)))
        if page and page not in found:
            found.append(page)
    return found


def rank_pages(
    question: str,
    pages: list[PageDoc],
    top_k: int = 4,
    min_relative_score: float = 0.2,
) -> list[ScoredPage]:
    """Rank pages for a question. Returns at most top_k pages with a positive score.

    Pages named explicitly by sheet number in the question are always included
    first. The sheet number and title are indexed with extra weight so
    "lighting schedule" finds the sheet titled LIGHTING SCHEDULE.
    """
    if not pages:
        return []

    terms = query_terms(question)
    pinned = sheet_refs_in(question, pages)

    docs: dict[int, Counter] = {}
    lengths: dict[int, int] = {}
    for p in pages:
        toks = tokenize(p.text or "")
        if p.sheet_title:
            toks += tokenize(p.sheet_title) * 3
        if p.sheet_number:
            toks += tokenize(p.sheet_number) * 3
        docs[p.page_number] = Counter(toks)
        lengths[p.page_number] = max(len(toks), 1)

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

    if ranked:
        cutoff = ranked[0][1] * min_relative_score
        for page_number, score in ranked:
            if len(result) >= top_k:
                break
            if page_number in seen or score < cutoff:
                continue
            result.append(ScoredPage(page_number, score, reasons[page_number]))
            seen.add(page_number)

    return result[:top_k]


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
