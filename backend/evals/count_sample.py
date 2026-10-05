"""Synthetic drawing set with known quantities, for testing object counting.

Ground truth by construction:
  * 8 x 2'x4' troffers (type L1: square-with-diagonal symbol, tagged "L1")
  * 3 x 6" downlights (type L2: circle with crosshair, tagged "L2")
  * 2 rooftop units (RTU-1, RTU-2: tags on the roof plan, rows in the RTU schedule)
It is a plumbing-and-logic fixture. Real drawings are messier; see run_eval.py.
"""
import io

PAGE = (792, 1224)   # portrait points, like a real 11x17 export


def _grid(c, w, h):
    """Background linework so a sheet looks like a drawing, not a text page."""
    c.setStrokeGray(0.75)
    c.setLineWidth(0.4)
    for i in range(60):
        y = 60 + i * (h - 120) / 60
        c.line(30, y, w - 30, y)
    for i in range(60):
        x = 30 + i * (w - 60) / 60
        c.line(x, 60, x, h - 60)
    c.setStrokeGray(0)


def _title_block(c, title, number):
    for text, y, size in ((title, 220, 12), (number, 190, 20), ("DESCRIPTION:", 160, 8),
                          ("SCALE:", 140, 8), ("DRAWN BY:", 120, 8), ("REVISIONS:", 100, 8)):
        c.setFont("Helvetica", size)
        c.drawString(380, y, text)


def _troffer(c, x, y, s=24):
    c.rect(x, y, s, s)
    c.line(x, y, x + s, y + s)            # the diagonal, drawn "\" on the plan


def _troffer_legend(c, x, y, s=24):
    c.rect(x, y, s, s)
    c.line(x, y + s, x + s, y)            # drawn "/" in the legend: same symbol, mirrored


def _downlight(c, x, y, r=6):
    c.circle(x, y, r)
    c.line(x - r - 2, y, x + r + 2, y)
    c.line(x, y - r - 2, x, y + r + 2)


def _pendant(c, x, y):
    """A downlight's circle and crosshair plus an inner circle: a superset of the
    downlight symbol, so a naive matcher counts pendants as downlights."""
    _downlight(c, x, y, r=6)
    c.circle(x, y, 2.5)


def _lighting_plan(c, w, h, l1_on_plan, pendants=0):
    """E2.01 drawn in a w x h frame (y up). Positions are relative to the frame, so the
    same sheet can be authored upright (portrait) or landscape for a rotated page."""
    base = h - 520
    _grid(c, w, h)
    for i in range(l1_on_plan):
        x, y = 70 + (i % 4) * 110, base + 150 + (i // 4) * 90
        _troffer(c, x, y)
        c.setFont("Helvetica", 9)
        c.drawString(x + 6, y - 11, "L1")
    for i in range(3):
        x, y = 90 + i * 120, base - 30
        _downlight(c, x, y)
        c.setFont("Helvetica", 9)
        c.drawString(x + 10, y - 3, "L2")
    # legend: swatch left of its heading
    _troffer_legend(c, 60, base - 150)
    c.setFont("Helvetica", 11)
    c.drawString(100, base - 142, "2'x4' LED TROFFER:")
    c.setFont("Helvetica", 8)
    c.drawString(100, base - 154, "MFR: LITHONIA")
    _downlight(c, 72, base - 190)
    c.setFont("Helvetica", 11)
    c.drawString(100, base - 192, "6\" DOWNLIGHT:")
    for i in range(pendants):
        _pendant(c, 400 + i * 60, base - 30)
    if pendants:
        _pendant(c, 72, base - 230)
        c.setFont("Helvetica", 11)
        c.drawString(100, base - 232, "PENDANT LIGHT:")
    _title_block(c, "LIGHTING PLAN", "E2.01")


def build_count_pdf(*, schedule_qty_l1: int = 8, l1_on_plan: int = 8, rotate_first: int = 0,
                    pendants: int = 0, plumbing: bool = False) -> bytes:
    """rotate_first=270 stores page 1 as a portrait page with /Rotate 270 whose content
    is authored to display upright in landscape, as CAD exports of landscape sheets do."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=PAGE)

    # E2.01 LIGHTING PLAN
    if rotate_first == 270:
        c.saveState()
        c.transform(0, -1, 1, 0, 0, PAGE[1])      # displayed (x', y') -> raw (y', H - x')
        _lighting_plan(c, PAGE[1], PAGE[0], l1_on_plan, pendants)
        c.restoreState()
    else:
        _lighting_plan(c, PAGE[0], PAGE[1], l1_on_plan, pendants)
    c.showPage()

    # E6.01 LIGHTING SCHEDULE
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, 1100, "TYPE")
    c.drawString(120, 1100, "DESCRIPTION")
    c.drawString(300, 1100, "MANUFACTURER")
    c.drawString(430, 1100, "QTY")
    c.setFont("Helvetica", 9)
    c.drawString(60, 1080, "L1"); c.drawString(120, 1080, "2X4 LED TROFFER"); c.drawString(300, 1080, "LITHONIA 2BLT4")
    c.drawString(430, 1080, str(schedule_qty_l1))
    c.drawString(60, 1060, "L2"); c.drawString(120, 1060, "6IN LED DOWNLIGHT"); c.drawString(300, 1060, "PRESCOLITE LBR6")
    c.drawString(430, 1060, "3")
    _title_block(c, "LIGHTING SCHEDULE", "E6.01")
    c.showPage()

    # M2.01 ROOF PLAN
    _grid(c, PAGE[0], PAGE[1])
    c.setFont("Helvetica", 10)
    c.rect(100, 600, 80, 50); c.drawString(110, 620, "RTU-1")
    c.rect(300, 600, 80, 50); c.drawString(310, 620, "RTU-2")
    c.drawString(100, 560, "MAIN SUPPLY DUCT 24x12")
    _title_block(c, "MECHANICAL ROOF PLAN", "M2.01")
    c.showPage()

    # M6.01 RTU SCHEDULE
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, 1100, "TAG"); c.drawString(120, 1100, "MANUFACTURER"); c.drawString(260, 1100, "MODEL")
    c.setFont("Helvetica", 9)
    c.drawString(60, 1080, "RTU-1"); c.drawString(120, 1080, "TRANE"); c.drawString(260, 1080, "YHC074 ROOFTOP UNIT")
    c.drawString(60, 1060, "RTU-2"); c.drawString(120, 1060, "TRANE"); c.drawString(260, 1060, "YHC092 ROOFTOP UNIT")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(60, 1130, "ROOFTOP UNIT SCHEDULE")
    _title_block(c, "MECHANICAL SCHEDULES", "M6.01")
    c.showPage()

    if plumbing:
        # P2.01 PLUMBING PLAN: 2 x WC-1, 3 x LAV-1, 1 x FD-1 (tags beside simple symbols)
        _grid(c, PAGE[0], PAGE[1])
        c.setFont("Helvetica", 9)
        for i in range(2):
            c.circle(120 + i * 90, 700, 10); c.drawString(135 + i * 90, 696, "WC-1")
        for i in range(3):
            c.rect(120 + i * 90, 600, 18, 12); c.drawString(142 + i * 90, 602, "LAV-1")
        c.circle(120, 500, 6); c.drawString(132, 497, "FD-1")
        _title_block(c, "PLUMBING PLAN", "P2.01")
        c.showPage()
        # P6.01 PLUMBING FIXTURE SCHEDULE (no QTY column: quantity comes from the plan)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(60, 1100, "MARK"); c.drawString(120, 1100, "DESCRIPTION"); c.drawString(300, 1100, "MANUFACTURER")
        c.drawString(430, 1100, "MODEL")
        c.setFont("Helvetica", 9)
        for y, row in ((1080, ("WC-1", "WATER CLOSET, FLOOR MOUNT", "KOHLER", "K-96053")),
                       (1060, ("LAV-1", "LAVATORY, WALL HUNG", "AMERICAN STD", "0356.421")),
                       (1040, ("FD-1", "FLOOR DRAIN, 4 IN", "ZURN", "ZN415"))):
            for x, t in zip((60, 120, 300, 430), row):
                c.drawString(x, y, t)
        _title_block(c, "PLUMBING FIXTURE SCHEDULE", "P6.01")
        c.showPage()
    c.save()
    data = buf.getvalue()
    if rotate_first:
        # Set /Rotate without touching the MediaBox, as a CAD export does (reportlab's
        # own setPageRotation swaps the media box, which is not what real files look like).
        from pypdf import PdfReader, PdfWriter

        reader, writer = PdfReader(io.BytesIO(data)), PdfWriter()
        for i, page in enumerate(reader.pages):
            if i == 0:
                page.rotate(rotate_first)
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        data = out.getvalue()
    return data


# question -> expected (quantity, status). None quantity = nothing countable.
COUNT_CASES = [
    {"id": "c-2x4", "question": "How many 2x4 lights?", "expected_count": 8, "expected_status": ["cross_checked"]},
    {"id": "c-rtu", "question": "How many RTUs?", "expected_count": 2, "expected_status": ["cross_checked"]},
    {"id": "c-downlight", "question": "How many downlights?", "expected_count": 3, "expected_status": ["cross_checked"]},
    {"id": "c-2x2-absent", "question": "How many 2x2 lights?", "expected_count": None, "expected_status": ["not_found"]},
    {"id": "c-diffuser-absent", "question": "How many diffusers?", "expected_count": None, "expected_status": ["not_found"]},
    {"id": "c-area", "question": "How many 2x4 lights in BOH?", "expected_count": 8, "expected_status": ["needs_verification"]},
]
