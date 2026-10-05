"""Phase 6: cross-sheet set checks."""
import io

import pytest

pytest.importorskip("reportlab")
pytest.importorskip("aiosqlite")

from reportlab.pdfgen import canvas  # noqa: E402

from backend.evals.measure_sample import build_measure_pdf  # noqa: E402
from backend.services.checks import set_checks as sc  # noqa: E402
from backend.services.counting.pdf_geometry import load_geometry  # noqa: E402
from backend.tests.test_assistant_api import client  # noqa: E402,F401


def geom_of(tmp_path, pdf: bytes, name="s.pdf"):
    p = tmp_path / name
    p.write_bytes(pdf)
    return load_geometry(str(p), 1)


def info(n, sheet, title="", text=""):
    return sc.PageInfo(n, sheet, title, text)


def run_one(g, sheet="A1.01", others=()):
    pages = [info(1, sheet, "PLAN")] + [info(i + 2, s) for i, s in enumerate(others)]
    return sc.run_checks(pages, {1: g})


def test_a_clean_sheet_is_flagged_for_nothing(tmp_path):
    assert run_one(geom_of(tmp_path, build_measure_pdf())) == []


def test_reduced_print_is_a_scale_conflict_with_both_numbers(tmp_path):
    fs = run_one(geom_of(tmp_path, build_measure_pdf(shrink=0.65)))
    f = next(x for x in fs if x.check == "scale_conflict")
    assert f.severity == "high" and "1/8" in f.message and "reduced size" in f.message
    assert any(e.startswith("Stated") for e in f.evidence) and any("From dimensions" in e for e in f.evidence)


def test_one_wrong_dimension_is_found_and_boxed(tmp_path):
    fs = run_one(geom_of(tmp_path, build_measure_pdf(wrong_dim=True)))
    assert [f.check for f in fs] == ["dimension_mismatch"]
    f = fs[0]
    assert "21'-0" in f.message and "18'-0" in f.message and f.box and 0 <= f.box[0] <= 1 and f.confidence == "medium"


def test_detail_at_another_scale_is_not_a_dimension_error(tmp_path):
    assert run_one(geom_of(tmp_path, build_measure_pdf(detail=True))) == []


def test_unverified_scale_gives_no_dimension_findings(tmp_path):
    # no dimensions to judge by: nothing to compare, so nothing is claimed
    assert run_one(geom_of(tmp_path, build_measure_pdf(with_dims=False))) == []


def test_reference_to_a_missing_sheet_but_not_to_present_ones_or_plain_numbers(tmp_path):
    notes = ("SEE E9.01 FOR PANEL DETAILS", "REFER TO E2.01 FOR LIGHTING", "SEE NOTE 3.50", "WALL TYPE A2.01 TYPICAL")
    fs = run_one(geom_of(tmp_path, build_measure_pdf(notes=notes)), others=["E2.01", "A2.01"])
    refs = [f for f in fs if f.check == "broken_reference"]
    assert len(refs) == 1 and "E9.01" in refs[0].message and refs[0].box and "SEE E9.01" in refs[0].evidence[0]


def test_duplicate_sheet_numbers():
    fs = sc.check_duplicate_sheets([info(1, "E2.01", "LIGHTING"), info(2, "A1.01"), info(3, "E-2.01", "POWER")])
    assert len(fs) == 1 and fs[0].page_number == 3 and "page 1" in fs[0].message


def cover_pdf(rows):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(792, 612))
    c.setFont("Helvetica", 10)
    c.drawString(60, 560, "PROJECT DATA")
    c.drawString(60, 520, "LIST OF DRAWINGS:")
    y = 500
    for r in rows:
        c.drawString(60, y, r)
        y -= 14
    c.showPage(); c.save()
    return buf.getvalue()


def test_drawing_list_versus_sheets_present(tmp_path):
    g = geom_of(tmp_path, cover_pdf(["GENERAL NOTES   0.0-0.8", "FLOOR PLAN   1.1a", "CEILING PLAN   1.3", "DETAILS   2.1"]))
    text = "LIST OF DRAWINGS"
    pages = [info(1, None, "COVER", text), info(2, "1.1a"), info(3, "0.4"), info(4, "2.1"), info(5, "7.7")]
    fs = sc.check_index(pages, {1: g})
    msgs = [f.message for f in fs]
    assert any("names sheet 1.3" in m for m in msgs)                 # listed, not in the set
    assert any("Sheet 7.7 is in the set but is not on the drawing list" in m for m in msgs)
    assert not any("0.4" in m for m in msgs)                         # inside the 0.0-0.8 range
    assert all(f.severity in ("medium", "low") for f in fs)


def test_no_drawing_list_means_no_index_findings(tmp_path):
    g = geom_of(tmp_path, build_measure_pdf())
    assert sc.check_index([info(1, "A1.01", "PLAN", "FLOOR PLAN")], {1: g}) == []


# ── API ──

@pytest.mark.asyncio
async def test_checks_endpoint_dismissal_and_restore(client):
    r = await client.post("/api/documents/upload", files={"file": ("m.pdf", build_measure_pdf(wrong_dim=True), "application/pdf")})
    doc = r.json()["id"]
    assert (await client.get(f"/api/checks/{doc}")).status_code == 409          # not indexed yet
    await client.post(f"/api/assistant/{doc}/index")
    out = (await client.get(f"/api/checks/{doc}")).json()
    assert out["pages_checked"] == 1 and "dimension_mismatch" in out["checks_run"]
    assert [f["check"] for f in out["findings"]] == ["dimension_mismatch"] and "look at" in out["summary"]
    assert "not verdicts" in out["disclaimer"]
    fid = out["findings"][0]["id"]

    assert (await client.post(f"/api/checks/{doc}/dismissals", json={"finding_id": fid})).status_code == 201
    await client.post(f"/api/checks/{doc}/dismissals", json={"finding_id": fid})          # idempotent
    after = (await client.get(f"/api/checks/{doc}")).json()
    assert after["findings"] == [] and "not a guarantee" in after["summary"]
    shown = (await client.get(f"/api/checks/{doc}?include_dismissed=true")).json()["findings"]
    assert shown[0]["dismissed"] is True
    assert (await client.delete(f"/api/checks/{doc}/dismissals?finding_id={fid}")).status_code == 204
    assert len((await client.get(f"/api/checks/{doc}")).json()["findings"]) == 1
    assert (await client.get("/api/checks/00000000-0000-0000-0000-000000000001")).status_code == 404
