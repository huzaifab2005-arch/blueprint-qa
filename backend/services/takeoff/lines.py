"""Takeoff lines: turning counts and measurements into quantities, and totalling them.

Pure functions; no I/O. The rules that keep a takeoff honest live here:

- A line is `verified` only when its source checked itself (a cross-checked
  count; measurements on verified/measured/calibrated scales).
  Anything else is `needs_verification`; a person's entry or edited number is `manual`.
- Totals are only ever summed within one unit, and always report how much of the
  total is verified.
- Waste is shown beside the quantity, never folded into it.
"""
import csv
import io
import math
import re
from dataclasses import dataclass, field

VERIFIED = "verified"
NEEDS_VERIFICATION = "needs_verification"
MANUAL = "manual"

# Scales a measurement can stand on without a person double-checking it.
TRUSTED_SCALES = ("verified", "measured", "calibrated")

# Only a count confirmed by a second, independent reading is verified. A single
# reading (even a clean one) is shown to the user as "verify", so a takeoff line
# built from it must say the same.
COUNT_STATUS = {"cross_checked": (VERIFIED, "high"), "single_source": (NEEDS_VERIFICATION, "medium"),
                "needs_verification": (NEEDS_VERIFICATION, "low")}

IN_PER_FT = 12.0
IN_PER_M = 1000 / 25.4
SQIN_PER_SF = 144.0
SQIN_PER_SM = IN_PER_M ** 2

DISCLAIMER = ("Quantities are read from the drawings and are not guaranteed. Items marked "
              "needs-verification or manual have not been confirmed by the drawing itself; "
              "check each against the sheets before ordering or pricing.")


class TakeoffError(ValueError):
    pass


@dataclass
class LineDraft:
    description: str
    quantity: float
    unit: str
    source_kind: str
    status: str
    confidence: str | None
    basis: str
    source_ref: dict
    sources: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    uncertainty: float | None = None
    details: str = ""
    model: str = ""


def source_text(sources: list[dict]) -> str:
    """'E2.01-E2.04' for sheets on consecutive pages (three or more), else a list."""
    pages = sorted({(s["page_number"], s.get("label") or f"page {s['page_number']}") for s in sources})
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1][0] == pages[j][0] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{pages[i][1]}-{pages[j][1]}")
        else:
            out.extend(p[1] for p in pages[i:j + 1])
        i = j + 1
    return ", ".join(out)


def normalise_unit(unit: str) -> str:
    u = re.sub(r"\s+", "", unit.strip().upper().replace("²", "2"))
    aliases = {"EACH": "EA", "PC": "EA", "PCS": "EA", "NO": "EA", "FT": "LF", "LNFT": "LF", "SQFT": "SF",
               "SQM": "M2", "SM": "M2", "METER": "M", "METRE": "M"}
    return aliases.get(u, u)[:16]


def line_from_count(payload: dict, question: str) -> LineDraft:
    """A line from a stored/fresh count result (count_answer.to_payload)."""
    status = payload.get("status")
    qty = payload.get("quantity")
    if status == "not_found" or qty is None:
        raise TakeoffError("Nothing could be counted for this, so there is no quantity to add. "
                           "Add it manually if you know it.")
    state, confidence = COUNT_STATUS.get(status, (NEEDS_VERIFICATION, "low"))
    primary = next((m for m in payload.get("methods", []) if m["method"] == payload.get("primary")), None)
    pages = (primary or {}).get("per_page", {})
    sources = [{"page_number": int(p), "label": f"page {p}", "note": f"{n} counted"} for p, n in sorted(pages.items(), key=lambda kv: int(kv[0]))]
    basis = (primary or {}).get("detail", "")
    others = [m for m in payload.get("methods", []) if m is not primary]
    if others:
        basis += " Also: " + "; ".join(f"{m['method'].replace('_', ' ')} {m['quantity']}" for m in others) + "."
    warnings = list(payload.get("blocking", []))
    if state == NEEDS_VERIFICATION:
        warnings.append("This count was not confirmed by a second, independent reading of the drawing; "
                        "check it on the marked sheets.")
    return LineDraft(
        description=str(payload.get("object") or question)[:200], quantity=float(qty), unit="EA",
        source_kind="count", status=state, confidence=confidence, basis=basis.strip(),
        source_ref={"question": question, "object": payload.get("object"), "count_status": status,
                    "primary": payload.get("primary")},
        sources=sources, warnings=warnings,
    )


_METRIC = re.compile(r"\b(?:mm|cm|m|m²)\b")


def is_metric(display: str) -> bool:
    return bool(_METRIC.search(display or ""))


def line_from_measurements(ms: list, description: str, page_labels: dict[int, str]) -> LineDraft:
    """Sum saved measurements into one line. `ms` are Measurement-like rows.

    All must be lengths (length/polyline) or all areas. The line is verified only
    if every one stood on a trusted scale; uncertainty is the straight sum of the
    parts (the scale error is shared, so it does not average out)."""
    if not ms:
        raise TakeoffError("Choose at least one measurement.")
    areas = [m.kind == "area" for m in ms]
    if any(areas) and not all(areas):
        raise TakeoffError("Lengths and areas cannot be added together; make one line for each.")
    area = areas[0]
    metric = all(is_metric(m.display) for m in ms)
    if not metric and any(is_metric(m.display) for m in ms):
        raise TakeoffError("These measurements mix metric and imperial sheets; make separate lines.")

    if area:
        total_sqin = sum(m.value_sqin or 0.0 for m in ms)
        qty, unit = (total_sqin / SQIN_PER_SM, "M2") if metric else (total_sqin / SQIN_PER_SF, "SF")
        uncertainty = None   # area error is not estimated
    else:
        total_in = sum(m.value_in or 0.0 for m in ms)
        qty, unit = (total_in / IN_PER_M, "M") if metric else (total_in / IN_PER_FT, "LF")
        err_in = sum(m.uncertainty_in or 0.0 for m in ms)
        uncertainty = err_in / IN_PER_M if metric else err_in / IN_PER_FT

    bad = [m for m in ms if m.scale_status not in TRUSTED_SCALES]
    state = NEEDS_VERIFICATION if bad else VERIFIED
    warnings: list[str] = []
    if bad:
        warnings.append(f"{len(bad)} of {len(ms)} measurement(s) used a scale that is only stated on the sheet "
                        f"or disagrees with its dimensions. Verify the scale or calibrate, then re-measure.")
    for m in ms:
        for w in m.warnings or []:
            if w not in warnings and "more than one scale" in w:
                warnings.append(w)
    pages: dict[int, float] = {}
    for m in ms:
        part = (m.value_sqin or 0.0) if area else (m.value_in or 0.0)
        pages[m.page_number] = pages.get(m.page_number, 0.0) + part
    sources = []
    for p in sorted(pages):
        part = pages[p] / (SQIN_PER_SM if metric else SQIN_PER_SF) if area else pages[p] / (IN_PER_M if metric else IN_PER_FT)
        n = sum(1 for m in ms if m.page_number == p)
        sources.append({"page_number": p, "label": page_labels.get(p, f"page {p}"),
                        "note": f"{n} measurement{'s' if n != 1 else ''}, {part:,.1f} {unit}"})
    scales = sorted({f"{m.scale_text} ({m.scale_status})" for m in ms})
    return LineDraft(
        description=description[:200], quantity=round(qty, 3), unit=unit, source_kind="measurement",
        status=state, confidence=None if bad else "medium",
        basis=f"Sum of {len(ms)} measurement{'s' if len(ms) != 1 else ''} on the drawing. Scales: {'; '.join(scales)}.",
        source_ref={"measurement_ids": [str(m.id) for m in ms]}, sources=sources, warnings=warnings,
        uncertainty=round(uncertainty, 3) if uncertainty is not None else None,
    )


def quantity_with_waste(quantity: float, waste_pct: float, unit: str) -> float:
    """Order quantity: waste added on top; whole items round up (you cannot buy 0.2 of a fixture)."""
    q = quantity * (1 + max(waste_pct, 0.0) / 100)
    return float(math.ceil(q - 1e-9)) if unit == "EA" else round(q, 3)


def summarise(items: list) -> dict:
    """Totals by unit and category, and how much of it is verified."""
    by_unit: dict[str, dict] = {}
    for it in items:
        order = quantity_with_waste(it.quantity, it.waste_pct, it.unit)
        row = by_unit.setdefault(it.unit, {"lines": 0, "verified_lines": 0, "needs_verification": 0, "manual": 0,
                                           "quantity": 0.0, "with_waste": 0.0, "verified_quantity": 0.0})
        row["lines"] += 1
        row["quantity"] += it.quantity
        row["with_waste"] += order
        if it.status == VERIFIED:
            row["verified_lines"] += 1
            row["verified_quantity"] += it.quantity
        elif it.status == MANUAL:
            row["manual"] += 1
        else:
            row["needs_verification"] += 1
    # A category can mix units, so its quantities are only meaningful per unit.
    cats = {}
    for it in items:
        c = cats.setdefault(it.category or "Uncategorised", {})
        u = c.setdefault(it.unit, {"quantity": 0.0, "with_waste": 0.0, "lines": 0})
        u["quantity"] += it.quantity
        u["with_waste"] += quantity_with_waste(it.quantity, it.waste_pct, it.unit)
        u["lines"] += 1
    n = len(items)
    verified = sum(1 for i in items if i.status == VERIFIED)
    return {
        "lines": n,
        "verified_lines": verified,
        "needs_verification_lines": sum(1 for i in items if i.status == NEEDS_VERIFICATION),
        "manual_lines": sum(1 for i in items if i.status == MANUAL),
        "by_unit": {u: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()} for u, row in by_unit.items()},
        "by_category": {c: {u: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}
                            for u, row in units.items()} for c, units in cats.items()},
        "disclaimer": DISCLAIMER,
    }


STATUS_TEXT = {VERIFIED: "Verified", NEEDS_VERIFICATION: "Needs verification", MANUAL: "Manual entry"}
CSV_HEADER = ["Category", "Item", "Description", "Model / specification", "Quantity", "Unit", "Waste %",
              "Order quantity", "Drawing source", "Status", "Confidence", "Uncertainty (±)", "Origin", "Basis",
              "Warnings", "Notes"]


def _safe(cell) -> str:
    """Spreadsheet-safe text: a leading = + - @ would be run as a formula when opened."""
    s = "" if cell is None else str(cell)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def to_csv(items: list) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_HEADER)
    for it in sorted(items, key=lambda i: ((i.category or "~"), i.description.lower())):
        status = STATUS_TEXT.get(it.status, it.status)
        if it.source_kind != "manual" and it.status == MANUAL:
            status = "Manual (edited from computed " + f"{it.computed_quantity:g})" if it.computed_quantity is not None else "Manual (edited)"
        w.writerow([_safe(it.category), _safe(it.description), _safe(getattr(it, "details", "")),
                    _safe(getattr(it, "model", "")), f"{it.quantity:g}", it.unit, f"{it.waste_pct:g}",
                    f"{quantity_with_waste(it.quantity, it.waste_pct, it.unit):g}", _safe(source_text(it.sources or [])),
                    status, it.confidence or "", f"{it.uncertainty:g}" if it.uncertainty is not None else "",
                    it.source_kind, _safe(it.basis), _safe(" | ".join(it.warnings or [])), _safe(it.notes)])
    w.writerow([])
    w.writerow([DISCLAIMER])
    return out.getvalue()
