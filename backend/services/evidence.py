"""Evidence validation for vision-model findings.

The model is good at noticing that something looks off but poor at grounding the claim:
it invents identifiers, repeats prompt examples, and reports things as unlabeled when
their label is printed on the sheet. These checks compare each model finding with the
drawing's own text and drop the ones the text contradicts or cannot support.

Visual claims that the text neither supports nor contradicts (e.g. "the circle symbol is
not labeled") are kept: they are what the model is for.
"""
import re

from backend.services.dimension_check import _DIM_RE, _to_inches

# A feet/inch dimension never starts mid-identifier: "M-501'" (a closing quote) or "5/M-501" are not 501 feet.
_DIM = re.compile(r"(?<![A-Za-z0-9/\-])(?:" + _DIM_RE.pattern + r")", re.VERBOSE)

_TAG = re.compile(r"\b[A-Z]{1,5}-?\d{1,4}[A-Z]?\b")
_DETAIL_OR_SECTION = re.compile(r"\bDETAIL\s+(\d{1,2})\b|\bSECTION\s+([A-Z]-[A-Z])\b")
# Findings whose truth is decided by text on the sheet; with no citation they can't be checked.
_TEXT_CHECKABLE = {"dimension_mismatch", "inconsistent_annotation", "incomplete_detail"}
_VISUAL_LABEL_TYPES = {"missing_tag", "unlabeled_element"}
_SUBJECT = re.compile(
    r"^(?:the|a|an)?\s*(.{3,60}?)\s+(?:is|are)\s+(?:not\b|missing\b|un(?:label|tag)|without\b)"
    r"|^(?:the|a|an)?\s*(.{3,60}?)\s+(?:lacks?|has\s+no|have\s+no|does\s+not\s+have)\b",
    re.IGNORECASE,
)
_GENERIC = {"symbol", "symbols", "element", "elements", "item", "items", "component", "components",
            "label", "labels", "tag", "tags", "the", "a", "an", "of", "in", "on", "for", "to", "with", "by"}


# A claim that something referenced is "not shown" is decided by the referencing text.
_REFERENCE_CLAIM = re.compile(r"\b(?:referenc|referred|refers)\w*\b.*\bnot\s+(?:shown|included|provided|present|on\s+this)", re.IGNORECASE)
_STOPWORDS = {"there", "is", "are", "was", "be", "an", "a", "the", "in", "on", "of", "at", "to", "and", "or", "not",
              "no", "missing", "unlabeled", "unlabelled", "labeled", "labelled", "tag", "tagged", "untagged",
              "label", "this", "that", "room", "area", "page", "sheet", "drawing", "plan", "some", "any",
              "has", "have", "does", "with", "without", "for", "it", "its", "identified", "identification"}


_NOT_SHOWN = re.compile(r"\bnot\s+(?:shown|included|provided|present|on\s+this)|\bmissing\b", re.IGNORECASE)


class Corpus:
    """Everything the drawing literally says, normalised for comparison."""

    def __init__(self, ocr_text: str, *word_lists: list[dict] | None):
        parts = [ocr_text or ""]
        for words in word_lists:
            parts.extend(w["text"] for w in (words or []))
        text = "\n".join(parts).upper()
        self.text = text
        self.words = re.sub(r"[^A-Z0-9]+", " ", text)
        self.tags = {t.replace("-", "") for t in _TAG.findall(text)}
        self.dims = {round(_to_inches(m), 2) for m in _DIM.finditer(text)}
        self.details = {m.group(1) for m in re.finditer(r"\bDETAIL\s+(\d{1,2})\b", text)}
        self.sections = {m.group(1) for m in re.finditer(r"\bSECTION\s+([A-Z]-[A-Z])\b", text)}
        self.sections |= {f"{a}-{b}" for a, b in re.findall(r"(?<![A-Z0-9-])([A-Z])-([A-Z])(?![A-Z0-9-])", text)}
        # Details/sections that are *defined* here (titled), as opposed to merely referenced.
        ref = r"(?:SEE|REFER\s+TO|PER|AS\s+SHOWN\s+IN)\s+"
        self.defined_details = {m.group(1) for m in re.finditer(r"\bDETAIL\s+(\d{1,2})\b", text)
                                if not re.search(ref + r"$", text[max(0, m.start() - 20):m.start()])}
        self.defined_sections = {m.group(1) for m in re.finditer(r"\bSECTION\s+([A-Z]-[A-Z])\b", text)
                                 if not re.search(ref + r"$", text[max(0, m.start() - 20):m.start()])}

    def has_phrase(self, phrase: str) -> bool:
        needle = re.sub(r"[^A-Z0-9]+", " ", phrase.upper()).strip()
        return bool(needle) and f" {needle} " in f" {self.words} "


def _citations(description: str) -> dict[str, set]:
    d = description.upper()
    return {
        "tags": {t.replace("-", "") for t in _TAG.findall(d)},
        "dims": {round(_to_inches(m), 2) for m in _DIM.finditer(d)},
        "details": {m.group(1) for m in re.finditer(r"\bDETAIL\s+(\d{1,2})\b", d)},
        "sections": {m.group(1) for m in re.finditer(r"\bSECTION\s+([A-Z]-[A-Z])\b", d)},
    }


def cited_dimensions(description: str) -> set:
    """Dimension values (inches) quoted in a finding's description."""
    return _citations(description)["dims"]


def reject_reason(issue: dict, corpus: Corpus) -> str | None:
    """Why this model finding should be dropped, or None to keep it."""
    desc = issue["description"]
    cites = _citations(desc)

    # 1. Hallucinated evidence: it names something that appears nowhere on the sheet.
    for kind, have in (("tags", corpus.tags), ("dims", corpus.dims),
                       ("details", corpus.details), ("sections", corpus.sections)):
        missing = cites[kind] - have
        if missing:
            return f"cites {sorted(missing)} which does not appear in the drawing text"

    # 2. "X is not labeled" where X is literally printed as text on the sheet.
    if issue["issue_type"] in _VISUAL_LABEL_TYPES:
        m = _SUBJECT.match(desc.strip())
        subject = (m.group(1) or m.group(2)) if m else None
        if subject:
            parts = [" ".join(w for w in re.findall(r"[A-Za-z0-9]+", part) if w.lower() not in _GENERIC)
                     for part in re.split(r"\s+and\s+|,", subject)]
            parts = [p for p in parts if p]
            if parts and all(corpus.has_phrase(p) for p in parts):
                return f"'{' / '.join(parts)}' is printed on the sheet, so it is labeled"

    # 2b. "DETAIL n / SECTION X-X is not shown" where this sheet titles it.
    if _NOT_SHOWN.search(desc):
        if (cites["details"] and cites["details"] <= corpus.defined_details) or \
           (cites["sections"] and cites["sections"] <= corpus.defined_sections):
            return "says a detail/section is not shown, but this sheet titles it"

    # 3. A text-checkable claim that points at no specific text cannot be verified.
    text_checkable = issue["issue_type"] in _TEXT_CHECKABLE or _REFERENCE_CLAIM.search(desc)
    if text_checkable and not any(cites.values()):
        return "makes a text-checkable claim without citing any tag, dimension, detail or section"

    # 3b. A dimension mismatch needs two different dimensions to compare.
    if issue["issue_type"] == "dimension_mismatch":
        if len(cites["dims"]) < 2:
            return "claims a dimension mismatch without citing two different dimensions"
        if re.search(r"\bscale\b", desc, re.IGNORECASE):
            return "compares dimensions with the drawing scale, which cannot be verified from text"

    # 4. A label/tag finding that does not say which element it is about cannot be acted on.
    if issue["issue_type"] in _VISUAL_LABEL_TYPES:
        content = [w for w in re.findall(r"[a-z0-9]+", desc.lower()) if w not in _STOPWORDS | _GENERIC]
        if not content:
            return "does not say which element it is about"
    return None


def validate_findings(issues: list[dict], corpus: Corpus) -> tuple[list[dict], list[tuple[dict, str]]]:
    kept, dropped = [], []
    for issue in issues:
        reason = reject_reason(issue, corpus)
        (dropped.append((issue, reason)) if reason else kept.append(issue))
    return kept, dropped
