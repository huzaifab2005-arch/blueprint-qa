"""Phase 5: takeoff lines, totals, honesty rules and the HTTP API."""
from types import SimpleNamespace as NS

import pytest

from backend.services.takeoff import lines
from backend.services.takeoff.lines import TakeoffError

pytest.importorskip("reportlab")
pytest.importorskip("aiosqlite")

from backend.tests.test_assistant_api import (  # noqa: E402,F401  (client is a fixture)
    ROOM, _across, _upload_count_set, _upload_measure_sheet, client,
)


def meas(kind="length", value_in=144.0, sqin=None, unc=1.0, status="verified", page=1, display="12'-0\"", warnings=None):
    return NS(id="m", kind=kind, value_in=value_in, value_sqin=sqin, uncertainty_in=unc, scale_status=status,
              page_number=page, display=display, scale_text="SCALE: 1/8\" = 1'-0\"", warnings=warnings or [])


def item(q=10, unit="EA", status="verified", waste=0.0, cat="", desc="x", kind="count"):
    return NS(quantity=q, unit=unit, status=status, waste_pct=waste, category=cat, description=desc, source_kind=kind,
              confidence=None, uncertainty=None, sources=[], basis="", warnings=[], notes="", computed_quantity=None)


# ── pure rules ──

def test_waste_rounds_whole_items_up_but_not_lengths():
    assert lines.quantity_with_waste(14, 10, "EA") == 16
    assert lines.quantity_with_waste(10, 0, "EA") == 10
    assert lines.quantity_with_waste(100, 5, "LF") == 105.0
    assert lines.quantity_with_waste(3, -5, "EA") == 3                      # negative waste is ignored


def test_units_are_normalised():
    assert [lines.normalise_unit(u) for u in ("each", "ft", "sq ft", "m²", "LF")] == ["EA", "LF", "SF", "M2", "LF"]


def payload(status="cross_checked", qty=8):
    return {"status": status, "quantity": qty, "object": "2x4 light", "primary": "tag_instances",
            "methods": [{"method": "tag_instances", "quantity": qty, "detail": "8 tags", "per_page": {"2": qty}},
                        {"method": "schedule_qty", "quantity": qty, "detail": "", "per_page": {"2": qty}}],
            "blocking": ["Door tags may be reused."]}


def test_count_status_maps_to_line_status_without_upgrading():
    a = lines.line_from_count(payload("cross_checked"), "how many 2x4 lights")
    assert (a.status, a.confidence, a.unit, a.quantity) == ("verified", "high", "EA", 8.0)
    one = lines.line_from_count(payload("single_source"), "q")
    assert (one.status, one.confidence) == ("needs_verification", "medium")      # one reading is not a check
    c = lines.line_from_count(payload("needs_verification"), "q")
    assert c.status == "needs_verification" and any("second, independent" in w for w in c.warnings)
    assert "Door tags may be reused." in a.warnings                           # caveats travel with the line
    with pytest.raises(TakeoffError):
        lines.line_from_count({"status": "not_found", "quantity": None, "object": "x", "methods": []}, "q")


def test_measurements_sum_and_carry_the_weakest_scale():
    ok = lines.line_from_measurements([meas(value_in=144, unc=1), meas(value_in=288, unc=2)], "Wall", {1: "A1"})
    assert ok.unit == "LF" and ok.quantity == 36.0 and ok.status == "verified" and ok.uncertainty == 0.25
    mixed = lines.line_from_measurements([meas(), meas(status="stated")], "Wall", {})
    assert mixed.status == "needs_verification" and mixed.confidence is None and mixed.warnings
    conflict = lines.line_from_measurements([meas(status="conflict")], "Wall", {})
    assert conflict.status == "needs_verification"


def test_area_and_metric_and_mixing_rules():
    a = lines.line_from_measurements([meas("area", None, 144 * 384, display="384.0 sf")], "Slab", {})
    assert a.unit == "SF" and a.quantity == 384.0 and a.uncertainty is None
    m = lines.line_from_measurements([meas(value_in=39.3701 * 5, display="5.000 m")], "Pipe", {})
    assert m.unit == "M" and abs(m.quantity - 5.0) < 0.001
    with pytest.raises(TakeoffError):
        lines.line_from_measurements([meas(), meas("area", None, 144)], "x", {})
    with pytest.raises(TakeoffError):
        lines.line_from_measurements([meas(display="12'-0\""), meas(display="3.000 m")], "x", {})
    with pytest.raises(TakeoffError):
        lines.line_from_measurements([], "x", {})


def test_totals_never_mix_units_and_always_report_verification():
    s = lines.summarise([item(14, "EA", "verified", 10, "Lighting"), item(6, "EA", "needs_verification", 0, "Lighting"),
                         item(100, "LF", "manual", 5, "Conduit")])
    assert s["lines"] == 3 and s["verified_lines"] == 1 and s["needs_verification_lines"] == 1 and s["manual_lines"] == 1
    ea = s["by_unit"]["EA"]
    assert ea["quantity"] == 20 and ea["verified_quantity"] == 14 and ea["with_waste"] == 22     # 16 + 6
    assert "LF" in s["by_unit"] and s["by_unit"]["LF"]["verified_quantity"] == 0
    assert s["by_category"]["Lighting"]["EA"]["quantity"] == 20 and "EA" not in s["by_category"]["Conduit"]
    assert "not guaranteed" in s["disclaimer"]


def test_csv_is_formula_safe_and_states_status():
    it = item(5, "EA", "needs_verification", 0, "=cmd|' /C calc'!A0", "+SUM(A1)")
    out = lines.to_csv([it])
    assert "'=cmd" in out and "'+SUM" in out and "Needs verification" in out and "not guaranteed" in out


# ── API ──

async def test_count_to_takeoff_line_end_to_end(client):
    doc = await _upload_count_set(client)
    r = await client.post(f"/api/takeoff/{doc}/items/from-count", json={"question": "2x4 lights", "category": "Lighting"})
    assert r.status_code == 201, r.text
    it = r.json()
    assert it["quantity"] == 8 and it["unit"] == "EA" and it["status"] == "verified" and it["confidence"] == "high"
    assert it["sources"][0]["label"] == "E2.01" and it["category"] == "Lighting"
    assert not client.llm.calls

    t = (await client.get(f"/api/takeoff/{doc}")).json()
    assert t["lines"] == 1 and t["verified_lines"] == 1 and t["by_unit"]["EA"]["quantity"] == 8

    # waste is separate from the quantity; whole items round up
    p = (await client.patch(f"/api/takeoff/{doc}/items/{it['id']}", json={"waste_pct": 10})).json()
    assert p["quantity"] == 8 and p["order_quantity"] == 9 and p["status"] == "verified"

    # editing the number makes it a person's number, and says what the drawing gave
    e = (await client.patch(f"/api/takeoff/{doc}/items/{it['id']}", json={"quantity": 10})).json()
    assert e["status"] == "manual" and e["confidence"] is None and e["computed_quantity"] == 8
    back = (await client.patch(f"/api/takeoff/{doc}/items/{it['id']}", json={"quantity": 8})).json()
    assert back["status"] == "verified" and back["confidence"] == "high"          # restored only when it matches again

    # the unit of a drawing-derived line cannot be re-labelled
    assert (await client.patch(f"/api/takeoff/{doc}/items/{it['id']}", json={"unit": "LF"})).status_code == 422

    # refresh recomputes from the sheets and keeps a person's override
    await client.patch(f"/api/takeoff/{doc}/items/{it['id']}", json={"quantity": 12})
    rf = (await client.post(f"/api/takeoff/{doc}/items/{it['id']}/refresh")).json()
    assert rf["quantity"] == 12 and rf["status"] == "manual" and rf["computed_quantity"] == 8
    assert any("edited by a person" in w for w in rf["warnings"])

    assert (await client.delete(f"/api/takeoff/{doc}/items/{it['id']}")).status_code == 204
    assert (await client.get(f"/api/takeoff/{doc}")).json()["lines"] == 0


async def test_line_from_a_count_the_assistant_already_answered(client):
    doc = await _upload_count_set(client)
    ans = (await client.post(f"/api/assistant/{doc}/ask", json={"question": "How many 2x4 lights?"})).json()["answer"]
    r = await client.post(f"/api/takeoff/{doc}/items/from-count", json={"message_id": ans["id"]})
    assert r.status_code == 201 and r.json()["quantity"] == 8
    assert r.json()["description"] and r.json()["status"] == "verified"
    # a non-count message is refused; question and message_id together are ambiguous
    msgs = (await client.get(f"/api/assistant/{doc}/messages")).json()
    user_id = next(m["id"] for m in msgs if m["role"] == "user")
    assert (await client.post(f"/api/takeoff/{doc}/items/from-count", json={"message_id": user_id})).status_code == 404
    assert (await client.post(f"/api/takeoff/{doc}/items/from-count",
                              json={"message_id": ans["id"], "question": "lights"})).status_code == 422
    assert (await client.post(f"/api/takeoff/{doc}/items/from-count", json={})).status_code == 422


async def test_nothing_counted_means_no_line(client):
    doc = await _upload_count_set(client)
    r = await client.post(f"/api/takeoff/{doc}/items/from-count", json={"question": "zorblax widgets"})
    assert r.status_code == 422 and "Add it manually" in r.json()["detail"]
    assert (await client.get(f"/api/takeoff/{doc}")).json()["lines"] == 0


async def test_measurements_become_a_line_and_follow_deletions(client):
    doc = await _upload_measure_sheet(client)
    depth = [[ROOM["x0"], ROOM["top"]], [ROOM["x0"], ROOM["bottom"]]]
    m1 = (await client.post(f"/api/measure/{doc}/pages/1/measurements", json={"kind": "length", "points": _across()})).json()
    m2 = (await client.post(f"/api/measure/{doc}/pages/1/measurements", json={"kind": "length", "points": depth})).json()
    r = await client.post(f"/api/takeoff/{doc}/items/from-measurements",
                          json={"measurement_ids": [m1["id"], m2["id"]], "description": "Wall base", "category": "Finishes"})
    assert r.status_code == 201, r.text
    it = r.json()
    assert it["unit"] == "LF" and abs(it["quantity"] - 40.0) < 0.2 and it["status"] == "verified"
    assert it["uncertainty"] > 0 and it["sources"][0]["note"].startswith("2 measurements")

    await client.delete(f"/api/measure/{doc}/measurements/{m2['id']}")
    rf = (await client.post(f"/api/takeoff/{doc}/items/{it['id']}/refresh")).json()
    assert abs(rf["quantity"] - 24.0) < 0.2 and any("deleted" in w for w in rf["warnings"])
    await client.delete(f"/api/measure/{doc}/measurements/{m1['id']}")
    assert (await client.post(f"/api/takeoff/{doc}/items/{it['id']}/refresh")).status_code == 409

    assert (await client.post(f"/api/takeoff/{doc}/items/from-measurements",
                              json={"measurement_ids": ["00000000-0000-0000-0000-000000000001"], "description": "x"})).status_code == 404


async def test_an_unchecked_scale_makes_the_line_unverified(client):
    doc = await _upload_measure_sheet(client, with_dims=False)           # scale only stated
    m = (await client.post(f"/api/measure/{doc}/pages/1/measurements", json={"kind": "length", "points": _across()})).json()
    assert m["scale_status"] == "stated"
    it = (await client.post(f"/api/takeoff/{doc}/items/from-measurements",
                            json={"measurement_ids": [m["id"]], "description": "Run"})).json()
    assert it["status"] == "needs_verification" and any("only stated" in w for w in it["warnings"])
    t = (await client.get(f"/api/takeoff/{doc}")).json()
    assert t["verified_lines"] == 0 and t["by_unit"]["LF"]["verified_quantity"] == 0


async def test_manual_lines_totals_and_csv_export(client):
    doc = await _upload_count_set(client)
    await client.post(f"/api/takeoff/{doc}/items/from-count", json={"question": "2x4 lights", "category": "Lighting"})
    r = await client.post(f"/api/takeoff/{doc}/items", json={"description": "Allowance, patching", "quantity": 4,
                                                             "unit": "each", "category": "General", "waste_pct": 0})
    assert r.status_code == 201 and r.json()["status"] == "manual" and r.json()["unit"] == "EA"
    assert (await client.post(f"/api/takeoff/{doc}/items", json={"description": "x", "quantity": -1, "unit": "EA"})).status_code == 422
    t = (await client.get(f"/api/takeoff/{doc}")).json()
    assert t["by_unit"]["EA"]["quantity"] == 12 and t["by_unit"]["EA"]["verified_quantity"] == 8
    assert t["manual_lines"] == 1

    csv = await client.get(f"/api/takeoff/{doc}/export.csv")
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    assert "Verified" in csv.text and "Manual entry" in csv.text and "E2.01" in csv.text and "not guaranteed" in csv.text

    assert (await client.get("/api/takeoff/00000000-0000-0000-0000-000000000001")).status_code == 404
