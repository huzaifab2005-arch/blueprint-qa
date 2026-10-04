"""Checks that an answer is supported by the sheet text it claims to come from.

The model is asked to quote evidence and stay on the page, but a prompt is a
request, not a guarantee. These checks run on what the model returned:

  * quotes must appear in the page's extracted text (`confirm_quote`);
  * identifiers in the answer (model numbers, tags, sizes) must appear in that
    text or in the user's own question (`ungrounded_identifiers`);
  * a claim that fails both is rejected, and a weakly supported one has its
    confidence capped (`assess_finding`).

Pure functions, no I/O. Limits: this can only check against *extracted text*.
A sheet with no text layer and failed OCR is image-only, so claims about it
cannot be verified this way and are capped to low confidence instead.
"""
import re
from dataclasses import dataclass

from backend.services.retrieval_service import normalise_text

_IDENT_RE = re.compile(r"[A-Z0-9]+(?:[-.][A-Z0-9]+)*")
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# Below this many characters a page is treated as image-only for grounding.
MIN_TEXT_CHARS = 30


def alnum(s: str) -> str:
    """Uppercase, dimension-normalised, letters and digits only. 24"x12" -> 24X12."""
    return re.sub(r"[^A-Z0-9]", "", normalise_text(s))


def confirm_quote(quote: str, page_text: str) -> bool:
    q = alnum(quote)
    return len(q) >= 3 and q in alnum(page_text)


def identifiers(text: str) -> list[str]:
    """Tokens a reader would treat as a specific fact: they mix letters and digits
    (RTU-1, 24LED-4000, E2.01, 24X12). Plain words and plain numbers are ignored."""
    out: list[str] = []
    for tok in _IDENT_RE.findall(normalise_text(text)):
        if len(alnum(tok)) >= 3 and re.search(r"\d", tok) and re.search(r"[A-Z]", tok):
            if tok not in out:
                out.append(tok)
    return out


def ungrounded_identifiers(answer: str, *supporting: str) -> list[str]:
    """Identifiers in `answer` that appear in none of the supporting texts."""
    haystack = " ".join(alnum(s) for s in supporting)
    return [tok for tok in identifiers(answer) if alnum(tok) not in haystack]


def cap_confidence(confidence: str, cap: str | None) -> str:
    if cap is None:
        return confidence
    return min(confidence, cap, key=lambda c: CONFIDENCE_ORDER.get(c, 0))


@dataclass
class Verdict:
    accepted: bool
    confidence_cap: str | None = None
    reason: str = ""


def assess_finding(
    answer: str,
    quotes_confirmed: list[bool],
    page_text: str,
    question: str,
) -> Verdict:
    """Decide whether one page's finding can be trusted.

    quotes_confirmed holds one flag per evidence quote the model gave.
    """
    if len((page_text or "").strip()) < MIN_TEXT_CHARS:
        return Verdict(True, "low", "sheet has no extractable text; answer rests on the image alone")

    missing = ungrounded_identifiers(answer, page_text, question)
    any_confirmed = any(quotes_confirmed)

    if missing and not any_confirmed:
        return Verdict(False, None, "not found in the sheet text: " + ", ".join(missing[:4]))
    if missing:
        return Verdict(True, "low", "partly unsupported by the sheet text: " + ", ".join(missing[:4]))
    if not quotes_confirmed:
        return Verdict(True, "low", "no supporting quote was given")
    if not any_confirmed:
        return Verdict(True, "low", "supporting quotes were not found in the sheet text")
    return Verdict(True)
