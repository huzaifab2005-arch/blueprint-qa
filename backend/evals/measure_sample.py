"""Synthetic sheets with known geometry, for testing scale detection and measuring.

Ground truth by construction (at stated scale 1/8" = 1'-0", i.e. 9 pt per foot):
  * a room 24'-0" wide x 16'-0" deep: 216 x 144 pt, area 384 sf
  * dimension lines drawn like CAD does: a line that overshoots a tick mark at each
    end by 6 pt, the value written above it
Variants model what goes wrong on real sheets:
  shrink     the sheet was printed smaller than its stated scale (a reduced set)
  with_dims  False = a plan with a scale label but no dimension annotations
  detail     a second drawing on the sheet at a different scale (1" = 1'-0")
  label      the scale text ("SCALE: AS NOTED" for a sheet with no usable scale)
"""
import io

PAGE = (792, 1224)
PT_PER_FT = 9.0          # 1/8" = 1'-0" on paper


def build_measure_pdf(*, shrink: float = 1.0, with_dims: bool = True, detail: bool = False,
                      label: str = 'SCALE: 1/8" = 1\'-0"') -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=PAGE)
    s = shrink
    ox, oy = 120.0, 700.0                                     # the room's lower-left corner

    # the room, drawn from its own coordinates scaled by `shrink`
    c.setLineWidth(1.5)
    c.rect(ox, oy, 216 * s, 144 * s)
    c.setLineWidth(0.5)
    c.line(ox + 96 * s, oy, ox + 96 * s, oy + 144 * s)       # a partition at 10'-8" from the left wall

    if with_dims:
        # dimensions are specified in unscaled plan units, positioned from the room's corner
        def d(x_a, x_b, y_off, text):
            c.setLineWidth(0.5)
            xa, xb, y = ox + x_a * s, ox + x_b * s, oy + 144 * s + y_off
            c.line(xa - 6, y, xb + 6, y)
            for x in (xa, xb):
                c.line(x - 3, y - 3, x + 3, y + 3)
            c.setFont("Helvetica", 8)
            c.drawCentredString((xa + xb) / 2, y + 3, text)
        d(0, 216, 40, "24'-0\"")
        d(0, 96, 20, "10'-8\"")
        d(96, 216, 20, "13'-4\"")
        d(0, 54, 60, "6'-0\"")
        d(54, 216, 60, "18'-0\"")

    # the scale label, under the plan
    c.setFont("Helvetica", 9)
    c.drawString(ox, oy - 30, "FLOOR PLAN")
    c.drawString(ox, oy - 42, label)

    if detail:
        dx, dy = 450.0, 700.0
        c.setLineWidth(1.0)
        c.rect(dx, dy, 72, 72)                                   # 72 x 72 pt: 1'-0" square at 1" = 1'-0" (1 ft = 72 pt)
        c.setLineWidth(0.5)
        for a, b, off, text in ((0, 72, 18, "1'-0\""), (0, 36, 34, "6\""), (36, 72, 34, "6\"")):
            y = dy + 72 + off
            c.line(dx + a - 6, y, dx + b + 6, y)
            for x in (dx + a, dx + b):
                c.line(x - 3, y - 3, x + 3, y + 3)
            c.setFont("Helvetica", 8)
            c.drawCentredString(dx + (a + b) / 2, y + 3, text)
        c.setFont("Helvetica", 9)
        c.drawString(dx, dy - 30, "WALL DETAIL")
        c.drawString(dx, dy - 42, 'SCALE: 1" = 1\'-0"')
    c.showPage()
    c.save()
    return buf.getvalue()


# ground truth for the default sheet
ROOM_W_FT, ROOM_D_FT = 24.0, 16.0
ROOM_W_PT, ROOM_D_PT = 216.0, 144.0
ROOM_AREA_SF = ROOM_W_FT * ROOM_D_FT
