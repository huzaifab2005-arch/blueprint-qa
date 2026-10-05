"""The assistant's reply to "give me the lighting takeoff": a plain-text table plus sources.

Pure formatting over a GenerateResult-like object (items with the fields the API returns).
"""
from backend.services.assistant_service import Answer
from backend.services.takeoff.lines import DISCLAIMER

STATUS_WORDS = {"verified": "Verified (cross-checked)", "needs_verification": "Needs verification",
                "manual": "Manual entry"}


def _why(item) -> str:
    if item.status == "verified":
        return STATUS_WORDS["verified"]
    if item.quantity == 0:
        return "Needs verification (not found on the plans)"
    if item.confidence == "medium":
        return "Needs verification (one reading only)"
    return STATUS_WORDS["needs_verification"]


def format_takeoff(result) -> str:
    items = result.items
    verified = sum(1 for i in items if i.status == "verified")
    head = f"{result.label} takeoff: {len(items)} item{'s' if len(items) != 1 else ''}"
    if items:
        head += f" ({verified} verified, {len(items) - verified} to check)"
    out = [head + "."]
    for n, it in enumerate(items, 1):
        out.append("")
        out.append(f"{n}. Item: {it.description}")
        if it.details:
            out.append(f"   Description: {it.details if len(it.details) <= 160 else it.details[:157].rstrip() + '...'}")
        out.append(f"   Model: {it.model or 'not stated on the drawings'}")
        out.append(f"   Quantity: {it.quantity:g}")
        out.append(f"   Unit: {it.unit}")
        out.append(f"   Source: {it.source or 'not found on the plans'}")
        out.append(f"   Status: {_why(it)}")
    if not items:
        out.append("")
        out.extend(result.notes or ["Nothing could be found."])
    else:
        out.append("")
        out.append("These lines are saved in the Takeoff tab, where you can edit quantities, add waste and export CSV.")
    return "\n".join(out)


def build_answer(result, metas: dict[int, object]) -> Answer:
    items = result.items
    pages: dict[int, int] = {}
    for it in items:
        for s in it.sources:
            pages[s.page_number] = pages.get(s.page_number, 0) + 1
    sources = []
    for p, n in sorted(pages.items()):
        m = metas.get(p)
        sources.append({
            "page_number": p, "sheet_number": getattr(m, "sheet_number", None), "sheet_title": getattr(m, "sheet_title", None),
            "label": getattr(m, "label", f"Page {p}"), "note": f"{n} item{'s' if n != 1 else ''} counted here", "evidence": [],
        })
    warnings = list(result.notes)
    for it in items:
        if it.status != "verified" and it.warnings:
            warnings.append(f"{it.description}: {it.warnings[-1]}")
    warnings.append(DISCLAIMER)
    all_verified = bool(items) and all(i.status == "verified" for i in items)
    return Answer(
        answer=format_takeoff(result), verified=all_verified, confidence="high" if all_verified else ("low" if items else None),
        sources=sources, pages_searched=sorted(pages), warnings=warnings,
    )


MENU = ("I can build these takeoffs from the drawings: lighting, HVAC and plumbing fixtures. "
        "Ask for one, for example “Give me the lighting takeoff.”")
