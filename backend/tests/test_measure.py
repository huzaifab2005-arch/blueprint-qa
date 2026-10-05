"""Measurement: units, scale detection and verification, and measuring on a sheet.

The synthetic sheets have geometry known by construction (evals/measure_sample.py):
a 24'-0" x 16'-0" room at 1/8" = 1'-0" (9 pt per foot), dimensioned the way CAD does.
"""
import math

import pytest

pytest.importorskip("reportlab")

from backend.evals.measure_sample import ROOM_AREA_SF, ROOM_D_PT, ROOM_W_PT, build_measure_pdf
from backend.services.counting.pdf_geometry import PageGeometry, Word, load_geometry
from backend.services.measure import ops
from backend.services.measure import scale as S
from backend.services.measure.units import (
    format_area, format_imperial, format_metric, parse_length,
)


# ── Units ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,inches", [
    ("12'-7\"", 151), ("12' 7\"", 151), ("5'-0 1/2\"", 60.5), ("4'", 48), ("24\"", 24), ("3 1/2\"", 3.5),
    ("7/8\"", 0.875), ("10'-6 3/4\"", 126.75), ("6'-6\"", 78), ("1200 mm", 1200 / 25.4), ("2.5 m", 2500 / 25.4),
])
def test_parse_length(text, inches):
    assert parse_length(text) == pytest.approx(inches)


@pytest.mark.parametrize("text", ["", "12", "abc", "1.3", "x'-y\""])
def test_not_a_length(text):
    assert parse_length(text) is None                   # a bare number could be anything


def test_format_round_trips_and_rounds_to_a_sixteenth():
    assert format_imperial(151) == "12'-7\"" and format_imperial(150.5) == "12'-6 1/2\""
    assert format_imperial(100.625) == "8'-4 5/8\"" and format_imperial(0) == "0\""
    assert format_imperial(24.03) == "2'-0\""            # 1/16" rounding hides sub-sixteenth noise
    assert format_metric(1200 / 25.4) == "1.200 m" and format_metric(24) == "610 mm"
    assert format_area(144 * 384) == ("384.0 sf", "35.67 m²")


# ── Stated scale text ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,ratio", [
    ('SCALE: 3/16" = 1\'-0"', 64), ('SCALE: 1/8"=1\'-0"', 96), ('SCALE: 1" = 20\'', 240), ('1/4" = 1\'', 48),
    ('SCALE: 1 1/2" = 1\'-0"', 8), ("SCALE: 1:100", 100), ("SCALE 1 : 50", 50),
    ('SCALE: 1/4" (cid:32) 1\'-0"', 48),                # a font with no "=" glyph
])
def test_parse_scale_text(text, ratio):
    assert S.parse_scale_text(text) == (ratio, True)


def test_scale_text_that_is_not_a_scale():
    assert S.parse_scale_text("SCALE: AS NOTED") == (None, True) and S.parse_scale_text("N.T.S.") == (None, True)
    assert S.parse_scale_text("SCALE: 1/4\" ")[0] != 4      # a fraction of an inch is not the ratio 1:4
    assert S.parse_scale_text("ROOM 12:30") == (None, False)


def test_stacked_fractions_are_assembled():
    # 5'-0 [1/2] ": numerator raised, denominator lowered, closing mark, as in a real CAD export
    words = [Word("5'-0", 136.1, 150.7, 56.8, 65.4), Word("1", 150.0, 154.6, 53.8, 62.4),
             Word("2", 155.8, 160.4, 59.9, 68.5), Word('"', 160.1, 163.5, 56.8, 65.4),
             Word("12'-7\"", 257.5, 280.1, 327.0, 335.6), Word("7", 300, 305, 100, 108)]
    got = {(t, round(v, 2)) for t, v, _w, _b in S.assemble_dimensions(words)}
    assert got == {("5'-0 1/2\"", 60.5), ("12'-7\"", 151.0)}


# ── Scale from the sheet's own dimensions ───────────────────────────────────

def _scales(tmp_path, **kw):
    p = tmp_path / "m.pdf"
    p.write_bytes(build_measure_pdf(**kw))
    return load_geometry(str(p), 1), S.analyse_sheet(load_geometry(str(p), 1))


def test_stated_scale_is_verified_by_the_dimensions(tmp_path):
    _g, r = _scales(tmp_path)
    sc = r.primary_scale
    assert sc.ratio == pytest.approx(96, rel=0.005) and sc.status == "verified" and sc.support == 5


def test_a_reduced_print_is_caught_and_the_measured_scale_wins(tmp_path):
    _g, r = _scales(tmp_path, shrink=0.65)
    stated = next(x for x in r.scales if x.source == "stated")
    assert stated.status == "conflict"
    assert r.primary_scale.source == "measured" and r.primary_scale.ratio == pytest.approx(96 / 0.65, rel=0.01)
    assert any("reduced size" in n for n in r.notes)


def test_a_scale_label_alone_is_stated_not_verified(tmp_path):
    _g, r = _scales(tmp_path, with_dims=False)
    assert r.primary_scale.status == "stated" and r.primary_scale.support == 0


def test_two_scales_on_one_sheet_are_each_verified(tmp_path):
    _g, r = _scales(tmp_path, detail=True)
    assert sorted(round(x.ratio) for x in r.scales) == [12, 96] and all(x.status == "verified" for x in r.scales)


def test_as_noted_gives_no_scale_and_says_so(tmp_path):
    _g, r = _scales(tmp_path, with_dims=False, label="SCALE: AS NOTED")
    assert r.scales == [] and r.primary is None and any("AS NOTED" in n for n in r.notes)


# ── Measuring ───────────────────────────────────────────────────────────────

def _norm(g, pts):
    return [[x / g.width, y / g.height] for x, y in pts]


def _room_corners(g):
    """The room's rectangle, found as the biggest rect on the sheet."""
    rect = max((s for s in g.shapes if s.kind == "rect"), key=lambda s: s.w * s.h)
    return rect


def test_length_and_area_match_the_ground_truth(tmp_path):
    g, r = _scales(tmp_path)
    room = _room_corners(g)
    assert room.w == pytest.approx(ROOM_W_PT) and room.h == pytest.approx(ROOM_D_PT)
    width = ops.measure("length", [(room.x0, room.top), (room.x1, room.top)], r.primary_scale, [True, True])
    assert width.value_in == pytest.approx(24 * 12, abs=0.5) and width.display == "24'-0\""
    corners = [(room.x0, room.top), (room.x1, room.top), (room.x1, room.bottom), (room.x0, room.bottom)]
    area = ops.measure("area", corners, r.primary_scale, [True] * 4)
    assert area.value_sqin / 144 == pytest.approx(ROOM_AREA_SF, rel=0.005) and area.display == "384.0 sf"
    assert area.perimeter_display == "80'-0\""


def test_a_path_sums_its_segments(tmp_path):
    g, r = _scales(tmp_path)
    room = _room_corners(g)
    path = ops.measure("polyline", [(room.x0, room.top), (room.x1, room.top), (room.x1, room.bottom)], r.primary_scale)
    assert path.value_in == pytest.approx((24 + 16) * 12, abs=0.5) and len(path.segments_in) == 2


def test_measuring_with_the_stated_scale_on_a_reduced_print_would_be_wrong_so_the_measured_one_is_used(tmp_path):
    g, r = _scales(tmp_path, shrink=0.65)
    room = _room_corners(g)
    pts = [(room.x0, room.top), (room.x1, room.top)]
    stated = next(x for x in r.scales if x.source == "stated")
    assert ops.measure("length", pts, stated).value_in / 12 == pytest.approx(24 * 0.65, rel=0.01)   # 15.6 ft: wrong
    used = r.scales[ops.choose_scale(r.scales, pts, r.primary)]
    assert used.source == "measured"
    assert ops.measure("length", pts, used).value_in / 12 == pytest.approx(24, rel=0.01)           # right


def test_the_scale_nearest_the_measurement_is_chosen_on_a_two_scale_sheet(tmp_path):
    g, r = _scales(tmp_path, detail=True)
    room = _room_corners(g)
    plan_pts = [(room.x0, room.top), (room.x1, room.top)]
    detail_rect = min((s for s in g.shapes if s.kind == "rect"), key=lambda s: s.w * s.h)
    detail_pts = [(detail_rect.x0, detail_rect.top), (detail_rect.x1, detail_rect.top)]
    assert r.scales[ops.choose_scale(r.scales, plan_pts, r.primary)].ratio == pytest.approx(96, rel=0.01)
    assert r.scales[ops.choose_scale(r.scales, detail_pts, r.primary)].ratio == pytest.approx(12, rel=0.01)
    assert ops.measure("length", detail_pts, r.scales[ops.choose_scale(r.scales, detail_pts, r.primary)]).display == "1'-0\""
    # an explicit choice always wins
    assert ops.choose_scale(r.scales, plan_pts, r.primary, requested=1) == 1


def test_snap_points_include_the_drawings_vertices(tmp_path):
    g, _r = _scales(tmp_path)
    room = _room_corners(g)
    snaps = ops.snap_points(g)
    for corner in [(room.x0, room.top), (room.x1, room.bottom)]:
        assert any(math.dist(corner, s) < 0.6 for s in snaps)
    assert len(snaps) < 500                                       # a small sheet: no flood of points


def test_bad_input_is_rejected():
    sc = S.ScaleInfo(96, "x", "stated", "stated")
    with pytest.raises(ValueError):
        ops.measure("length", [(0, 0)], sc)
    with pytest.raises(ValueError):
        ops.measure("area", [(0, 0), (1, 1)], sc)
    with pytest.raises(ValueError):
        ops.measure("length", [(0, 0), (1, 1), (2, 2)], sc)       # a length is two points; use a path


def test_snapped_points_carry_less_uncertainty():
    sc = S.ScaleInfo(96, "x", "calibrated", "calibrated")      # exact scale: only the click error is left
    loose = ops.measure("length", [(0, 0), (100, 0)], sc, [False, False])
    tight = ops.measure("length", [(0, 0), (100, 0)], sc, [True, True])
    assert tight.uncertainty_in < loose.uncertainty_in / 4


def test_uncertainty_reflects_how_well_the_scale_is_known():
    pts = [(0.0, 0.0), (1000.0, 0.0)]                                    # ~ 111 ft at 1/8"
    exact = ops.measure("length", pts, S.ScaleInfo(96, "c", "calibrated", "calibrated"), [True, True])
    verified = ops.measure("length", pts, S.ScaleInfo(96, "v", "stated", "verified", spread=0.005), [True, True])
    unchecked = ops.measure("length", pts, S.ScaleInfo(96, "s", "stated", "stated"), [True, True])
    assert exact.uncertainty_in < verified.uncertainty_in < unchecked.uncertainty_in
    assert verified.uncertainty_in == pytest.approx(verified.value_in * 0.005, rel=0.2)   # dominated by the spread
    assert unchecked.uncertainty_in == pytest.approx(unchecked.value_in * 0.02, rel=0.05)


def test_the_spread_of_a_verified_scale_is_reported(tmp_path):
    _g, r = _scales(tmp_path)
    assert 0.002 <= r.primary_scale.spread < 0.02
