"""Deterministic QA rules that run on OCR text.

These checks are cheap, repeatable and need no model. Each rule is deliberately
conservative: it only reports when the page itself contains enough evidence to
judge (e.g. a sheet index, or locally titled details), because real drawing sets
reference things that legitimately live on other sheets.

Every rule returns issue dicts shaped like the model's (issue_type, severity,
description, location_hint) plus an `evidence` list of the OCR strings involved.
"""
import re

_SHEET = r"[A-Z]{1,2}-?\d{3}"
_REF_VERB = r"(?:SEE|REFER\s+TO|PER|AS\s+SHOWN\s+IN)"


def _norm(sheet_id: str) -> str:
    return sheet_id.replace("-", "").upper()


_OCR_LOOKALIKE = str.maketrans("OILSBZ", "011582")


def _same_after_ocr_noise(a: str, b: str) -> bool:
    """True if a and b differ only by look-alike characters OCR confuses (O/0, I/1, S/5...).

    A real different digit (M-901 vs M-101) is NOT tolerated: that is the error to catch.
    """
    return a.translate(_OCR_LOOKALIKE) == b.translate(_OCR_LOOKALIKE)


def _issue(issue_type: str, severity: str, description: str, evidence: list[str]) -> dict:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
        "location_hint": "",
        "evidence": evidence,
    }


# ---------------------------------------------------------------- references
def check_references(ocr_text: str) -> list[dict]:
    """Flag references to sheets, details or sections that this sheet cannot resolve."""
    if not ocr_text:
        return []
    text = ocr_text.upper()
    issues: list[dict] = []

    # -- sheet references, judged against the sheet index printed on this page
    index = {
        _norm(m.group(1))
        for m in re.finditer(rf"^\s*({_SHEET})\s+[A-Z][A-Z0-9 &/,.\-()']{{3,}}\s*$", text, re.MULTILINE)
    }
    own = re.search(rf"SHEET\s*(?:NO|NUMBER|#)\.?\s*:?\s*({_SHEET})", text)
    if own:
        index.add(_norm(own.group(1)))
    if len(index) >= 3:
        seen = set()
        for m in re.finditer(rf"\b(?:SHEETS?|DRAWINGS?|DWGS?\.?)\s+({_SHEET})\b", text):
            ref = _norm(m.group(1))
            if ref in index or ref in seen or any(_same_after_ocr_noise(ref, k) for k in index):
                continue
            seen.add(ref)
            issues.append(_issue(
                "incomplete_detail", "high",
                f"Note refers to sheet {m.group(1)}, which is not in this sheet's index.",
                [m.group(0)],
            ))

    # -- details and sections: only judged when this page defines local ones
    ref_spans = []
    detail_refs = []
    for m in re.finditer(rf"\b{_REF_VERB}\s+DETAILS?\s+(\d{{1,2}})(?:\s*/\s*({_SHEET}))?", text):
        ref_spans.append(m.span())
        detail_refs.append((m.group(1), m.group(2), m.group(0)))
    section_refs = []
    for m in re.finditer(rf"\b{_REF_VERB}\s+SECTIONS?\s+([A-Z]-[A-Z])(?:\s*/\s*({_SHEET}))?", text):
        ref_spans.append(m.span())
        section_refs.append((m.group(1), m.group(2), m.group(0)))

    def outside_refs(pattern):
        return [m for m in re.finditer(pattern, text)
                if not any(s <= m.start() < e for s, e in ref_spans)]

    defined_details = {m.group(1) for m in outside_refs(r"\bDETAIL\s+(\d{1,2})\b")}
    defined_sections = {m.group(1).replace(" ", "") for m in outside_refs(r"\bSECTION\s+([A-Z]\s*-\s*[A-Z])\b")}
    defined_sections |= {f"{m.group(1)}-{m.group(2)}"
                         for m in outside_refs(r"(?<![A-Z0-9-])([A-Z])-([A-Z])(?![A-Z0-9-])")}
    if not (defined_details or defined_sections):
        return issues  # no local context, so these references may point to other sheets

    for num, sheet, quote in detail_refs:
        if sheet and _norm(sheet) != _norm(own.group(1) if own else ""):
            continue  # qualified to another sheet
        if num not in defined_details:
            issues.append(_issue(
                "incomplete_detail", "medium",
                f"Note refers to DETAIL {num}, but no DETAIL {num} is shown on this sheet.", [quote]))
    for sec, sheet, quote in section_refs:
        if sheet:
            continue
        if sec not in defined_sections:
            issues.append(_issue(
                "incomplete_detail", "medium",
                f"Note refers to SECTION {sec}, but no SECTION {sec} is shown on this sheet.", [quote]))
    return issues


# ------------------------------------------------------------ spec conflicts
_ELEMENT = re.compile(r"\b(WALLS?|PARTITIONS?|FLOORS?|CEILINGS?|SLABS?|ROOFS?)\b")
_SCOPE = re.compile(r"\b(?:ROOM|RM|AREA|ZONE|LEVEL|SUITE)\s+([A-Z]?\d+[A-Z]?)\b")
_SPEC = re.compile(
    r"\b(?:ARE|IS|TO\s+BE|SHALL\s+BE)\s+(?:(\d+(?:\s+\d+/\d+)?)\s*\"\s*)?"
    r"(CMU|CONCRETE\s+BLOCK|GYP(?:SUM)?(?:\s+(?:BD|BOARD|STUD))?|CONCRETE|BRICK|STEEL(?:\s+STUD)?|WOOD(?:\s+STUD)?)\b"
)
_FAMILY = (("CMU", "CMU"), ("CONCRETE BLOCK", "CMU"), ("GYP", "GYP"), ("CONCRETE", "CONC"),
           ("BRICK", "BRICK"), ("STEEL", "STEEL"), ("WOOD", "WOOD"))


def _inches(raw: str | None) -> float | None:
    if not raw:
        return None
    parts = raw.split()
    total = float(parts[0])
    if len(parts) > 1:
        n, d = parts[1].split("/")
        total += int(n) / int(d)
    return total


def check_spec_conflicts(ocr_text: str) -> list[dict]:
    """Flag the same element in the same scope specified with different materials/thicknesses."""
    if not ocr_text:
        return []
    found: dict[tuple, dict] = {}   # (element, scope) -> {(family, thickness): quoted line}
    for raw in ocr_text.upper().splitlines():
        line = re.sub(r"^\s*\W*\d*\.?\s*", "", raw).strip()
        spec = _SPEC.search(line)
        element = _ELEMENT.search(line)
        if not spec or not element:
            continue
        scope = _SCOPE.search(line)
        if scope:
            scope_key = scope.group(0).replace("RM", "ROOM")
        elif re.search(r"\bALL\b", line[:element.start() + 1]):
            scope_key = "ALL"
        else:
            continue  # unscoped statements may describe different walls
        family = next(fam for key, fam in _FAMILY if spec.group(2).startswith(key))
        key = (element.group(1).rstrip("S"), scope_key)
        found.setdefault(key, {}).setdefault((family, _inches(spec.group(1))), (spec, line))

    issues = []
    for (element, scope), specs in found.items():
        families = {f for f, _ in specs}
        thicknesses = {t for f, t in specs if t is not None}
        if len(families) < 2 and len(thicknesses) < 2:
            continue
        shown = " vs ".join(
            (f'{spec.group(1)}" ' if spec.group(1) else "") + spec.group(2) for spec, _ in specs.values())
        where = "" if scope == "ALL" else f" ({scope})"
        issues.append(_issue(
            "inconsistent_annotation", "high",
            f"Conflicting specifications for {element}S{where}: {shown}.",
            [line for _, line in specs.values()]))
    return issues


def run_rules(ocr_text: str) -> list[dict]:
    return check_references(ocr_text) + check_spec_conflicts(ocr_text)
