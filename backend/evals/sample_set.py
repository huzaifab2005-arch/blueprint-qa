"""A small synthetic drawing set with known ground truth.

It is a regression fixture for the pipeline's plumbing and retrieval, NOT a
benchmark of model quality on real drawings (text-only sheets are much easier
than real CAD output). Bring your own PDF and cases for that; see run_eval.py.
"""
import io

SHEET_SIZE = (36 * 72, 24 * 72)  # ARCH E, landscape

# (sheet number, title, body lines)
SHEETS: list[tuple[str, str, list[str]]] = [
    ("G0.01", "COVER SHEET", [
        "SAMPLE BUILDING - TENANT IMPROVEMENT",
        "DRAWING INDEX",
        "A1.01  FIRST FLOOR PLAN",
        "E2.01  LIGHTING PLAN",
        "E3.01  POWER PLAN",
        "E6.01  LIGHTING SCHEDULE",
        "M2.01  MECHANICAL ROOF PLAN",
        "M2.02  MECHANICAL DETAILS",
        "M6.01  MECHANICAL SCHEDULES",
    ]),
    ("A1.01", "FIRST FLOOR PLAN", [
        "SALES FLOOR  3200 SF", "BOH STORAGE  ROOM 104", "BOH OFFICE  ROOM 105", "RESTROOM  ROOM 106",
        "ENTRY VESTIBULE", "STOCK ROOM", "GENERAL NOTES", "1. VERIFY ALL DIMENSIONS IN FIELD",
        "2. COORDINATE WITH MECHANICAL AND ELECTRICAL", "SCALE: 1/8\" = 1'-0\"",
    ]),
    ("E2.01", "LIGHTING PLAN", [
        "FIRST FLOOR LIGHTING PLAN",
        "L1 L1 L1 L1 L1 L1 L1 L1 SALES FLOOR",
        "L2 L2 L2 BOH STORAGE ROOM 104",
        "L2 L2 BOH OFFICE ROOM 105",
        "L3 RESTROOM ROOM 106",
        "SEE LIGHTING SCHEDULE ON SHEET E6.01",
    ]),
    ("E3.01", "POWER PLAN", [
        "FIRST FLOOR POWER PLAN", "PANEL LP-1  225A  208Y/120V", "RTU-1 DISCONNECT 60A 3P",
        "RTU-2 DISCONNECT 60A 3P", "DEDICATED RECEPTACLE  SALES FLOOR  20A",
    ]),
    ("E6.01", "LIGHTING SCHEDULE", [
        "LIGHTING FIXTURE SCHEDULE",
        "TYPE  DESCRIPTION            MANUFACTURER  CATALOG NUMBER      VOLTS  WATTS",
        "L1    2X4 LED TROFFER        LITHONIA      2BLT4-40L-ADP-LP840 120    32",
        "L2    4FT LED STRIP          METALUX       4SNLED-LD5-40SL-U   120    28",
        "L3    LED DOWNLIGHT 6IN      PRESCOLITE    LBR6-30K-WH         120    12",
    ]),
    ("M2.01", "MECHANICAL ROOF PLAN", [
        "ROOF MECHANICAL PLAN",
        "RTU-1 LOCATED OVER SALES FLOOR  GRID C-4",
        "RTU-2 LOCATED OVER BOH STORAGE  GRID F-2",
        "MAIN SUPPLY DUCT 24x12 FROM RTU-1",
        "BRANCH SUPPLY DUCT 14x10",
        "RETURN DUCT 20x12",
        "SEE DETAIL 3/M2.02 FOR RTU CURB",
    ]),
    ("M2.02", "MECHANICAL DETAILS", [
        "1 DUCT CONNECTION DETAIL", "2 FLEXIBLE CONNECTION DETAIL",
        "3 RTU CURB DETAIL  SHOWS 14\" TALL CURB WITH WOOD NAILER AND COUNTERFLASHING",
        "4 ROOF PENETRATION DETAIL",
    ]),
    ("M6.01", "MECHANICAL SCHEDULES", [
        "ROOFTOP UNIT SCHEDULE",
        "TAG    MANUFACTURER  MODEL     COOLING TONS  CFM   MCA",
        "RTU-1  TRANE         YHC074    7.5           3000  38",
        "RTU-2  TRANE         YHC092    8.5           3400  42",
        "EXHAUST FAN SCHEDULE",
        "EF-1   GREENHECK     CUE-070   CFM 700",
    ]),
]

# Each case: question + what a correct answer must contain / cite.
CASES: list[dict] = [
    {"id": "rtu1-model", "question": "What model is RTU-1?", "expected_sheets": ["M6.01"], "must_include": ["YHC074"]},
    {"id": "rtu2-model", "question": "What model is RTU-2?", "expected_sheets": ["M6.01"], "must_include": ["YHC092"]},
    {"id": "rtu1-location", "question": "Where is RTU-1 located?", "expected_sheets": ["M2.01"], "must_include": ["SALES"]},
    {"id": "boh-lights", "question": "What model are the lights in BOH?", "expected_sheets": ["E2.01", "E6.01"], "must_include": ["4SNLED"]},
    {"id": "sales-lights", "question": "What fixture type is used on the sales floor?", "expected_sheets": ["E2.01", "E6.01"], "must_include": ["2BLT4"]},
    {"id": "supply-duct", "question": "What size is the main supply duct?", "expected_sheets": ["M2.01"], "must_include": ["24X12"]},
    {"id": "duct-sizes", "question": "What are the duct sizes?", "expected_sheets": ["M2.01"], "must_include": ["24X12", "14X10", "20X12"]},
    {"id": "lighting-schedule-page", "question": "Which page contains the lighting schedule?", "expected_sheets": ["E6.01"], "must_include": ["E6.01"]},
    {"id": "detail-3", "question": "What does detail 3/M2.02 show?", "expected_sheets": ["M2.02"], "must_include": ["CURB"]},
    {"id": "exhaust-fan", "question": "What model is EF-1?", "expected_sheets": ["M6.01"], "must_include": ["CUE-070"]},
    {"id": "panel", "question": "What size is panel LP-1?", "expected_sheets": ["E3.01"], "must_include": ["225"]},
    {"id": "disconnect", "question": "What is the RTU-1 disconnect rating?", "expected_sheets": ["E3.01"], "must_include": ["60A"]},
    {"id": "curb-sheet", "question": "Which sheet shows the roof curb?", "expected_sheets": ["M2.02"], "must_include": ["M2.02"]},
    {"id": "curb-height", "question": "How tall is the RTU curb?", "expected_sheets": ["M2.02"], "must_include": ["14"]},
    {"id": "cfm", "question": "What is the CFM of RTU-1?", "expected_sheets": ["M6.01"], "must_include": ["3000"]},
    {"id": "fan-maker", "question": "Who makes the exhaust fan?", "expected_sheets": ["M6.01"], "must_include": ["GREENHECK"]},
    {"id": "restroom-lights", "question": "What voltage are the restroom lights?", "expected_sheets": ["E2.01", "E6.01"], "must_include": ["120"]},
    # Multi-hop: the plan says which unit is over BOH storage, the schedule gives its capacity.
    {"id": "multihop-boh-rtu", "question": "What is the cooling capacity of the rooftop unit over BOH storage?", "expected_sheets": ["M2.01", "M6.01"], "must_include": ["8.5"]},
    # Negatives: the drawings do not say. A correct system declines.
    {"id": "neg-chiller", "question": "What model is the chiller?", "expect_unverified": True},
    {"id": "neg-generator", "question": "What size is the emergency generator?", "expect_unverified": True},
    {"id": "neg-rtu9", "question": "What model is RTU-9?", "expect_unverified": True},
    {"id": "neg-offtopic", "question": "What colour is the elephant?", "expect_unverified": True},
]


def build_sample_pdf() -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=SHEET_SIZE)
    for number, title, lines in SHEETS:
        y = SHEET_SIZE[1] - 120
        c.setFont("Courier", 26)
        c.drawString(150, y + 40, title)
        for ln in lines:
            c.setFont("Courier", 22)
            c.drawString(150, y, ln)
            y -= 44
        c.setFont("Helvetica", 24)
        c.drawString(SHEET_SIZE[0] - 700, 150, "SAMPLE BUILDING")
        c.drawString(SHEET_SIZE[0] - 700, 110, title)
        c.setFont("Helvetica-Bold", 30)
        c.drawString(SHEET_SIZE[0] - 700, 60, f"SHEET NO: {number}")
        c.showPage()
    c.save()
    return buf.getvalue()


# A second, differently-labelled set: plain numeric sheet numbers (1.3, 4.0) in a
# title block read as TITLE / NUMBER / "DESCRIPTION:" - the format that defeated the
# letter-prefix detector on a real set. Same ground-truth idea as SHEETS above.
NUMERIC_SHEETS: list[tuple[str, str, list[str]]] = [
    ("1.1a", "EQUIPMENT PLAN", ["BOH PREP AREA", "MOUNT AT 18\" A.F.F. SEE 1.6", "1 MOP SINK  FIAT MSB-2424",
                                "REFER TO 1.3 FOR CEILING HEIGHT", "TOILET 1.3 GPF"]),
    ("1.3", "REFLECTED CEILING PLAN", [
        "NEW SUSPENDED 2'x2' GRID CEILING", "10'-8\" A.F.F. TYPICAL THROUGHOUT EXCEPT", "WASHROOM  8'-6\" A.F.F.",
        "2'x2' RECESSED FIXTURE  MFR: SYLVANIA  CODE: PANELF-1A-032-UNV-D-8-40",
    ]),
    ("4.0", "WASHROOM DETAILS", ["WASHROOM DETAIL PLAN", "10'-3 1/2\"", "6'-6\"", "BARRIER FREE VANITY"]),
    ("0.1", "GENERAL REQUIREMENTS & SPECIFICATIONS", [
        "VERIFY ALL DIMENSIONS ON SITE. REPORT DIMENSIONS THAT DIFFER.",
        "THE CONTRACTOR SHALL CONFIRM DIMENSIONS BEFORE FABRICATION.",
    ]),
]

NUMERIC_CASES: list[dict] = [
    {"id": "n-ceiling-typo", "question": "what is the ceiling hieght in BOH?", "expected_sheets": ["1.3"], "must_include": ["10'-8"]},
    {"id": "n-washroom-dims", "question": "what are the dimensions for the washroom", "expected_sheets": ["4.0"], "must_include": ["10'-3", "6'-6"]},
    {"id": "n-sheet-ref", "question": "What is shown on sheet 1.3?", "expected_sheets": ["1.3"]},
    {"id": "n-light-model", "question": "What model are the lights?", "expected_sheets": ["1.3"], "must_include": ["PANELF"]},
]


def build_numeric_titleblock_pdf() -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(792, 1224))
    for number, title, lines in NUMERIC_SHEETS:
        y = 1100
        for ln in lines:
            c.setFont("Helvetica", 14)
            c.drawString(60, y, ln)
            y -= 30
        # Title block, drawn in the order a CAD export emits it.
        for text, ypos, size in ((title, 220, 12), (number, 190, 20), ("DESCRIPTION:", 160, 8),
                                 ("SCALE:", 140, 8), ("DRAWN BY:", 120, 8), ("REVISIONS:", 100, 8)):
            c.setFont("Helvetica", size)
            c.drawString(380, ypos, text)
        c.showPage()
    c.save()
    return buf.getvalue()
