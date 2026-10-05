"""Build the synthetic benchmark drawings.

Every page is a small, realistic-looking sheet (title block, sheet index, notes,
equipment/door schedules, section and detail titles). Each defective page is the
same base sheet with exactly ONE planted defect; the two clean pages carry none.
All references on the clean sheets are valid, so any finding on a clean page, or
any finding that is not the planted defect, counts as a false positive.
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 2400, 1600
OUT = os.path.join(os.path.dirname(__file__), "pages")
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(size=24):
    try:
        return ImageFont.truetype(_FONT, size)
    except OSError:
        return ImageFont.load_default()


BASE = dict(
    sheet_no="A-101",
    title="MECHANICAL ROOM PLAN",
    scale_text='SCALE: 1/4" = 1\'-0"',          # None removes the scale entirely
    index=["A-101 FLOOR PLAN", "A-102 ENLARGED PLANS", "A-501 DETAILS", "M-101 HVAC PLAN", "E-101 POWER PLAN"],
    dim_top="40'-0\"", dim_bottom="40'-0\"", dim_right="25'-0\"",
    room_label="ROOM 204 - MECH RM",
    units=[("AHU-1", 480, 450), ("AHU-2", 800, 450)],   # drawn and tagged on plan
    extra_boxes=[],                                      # (x, y, tag-or-None)
    schedule=["AHU-1 AIR HANDLING UNIT 2000", "AHU-2 AIR HANDLING UNIT 1500"],
    door_tags=["D-101", "D-102"],
    door_schedule=["D-101 3'-0\" x 7'-0\"", "D-102 3'-0\" x 7'-0\""],
    notes=[
        "ALL DIMENSIONS TO BE VERIFIED ON SITE.",
        "WALLS TO ROOM 204 ARE 8\" CMU.",
        "SEE SECTION A-A FOR WALL ASSEMBLY.",
        "SEE DETAIL 1 FOR CURB CONDITION.",
        "REFER TO SHEET M-101 FOR HVAC LAYOUT.",
    ],
    sections=["A-A", "B-B"],
    details=["1 - CURB", "2 - PIPE SUPPORT"],
)


def render(spec, path):
    s = {**BASE, **spec}
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f, fs = font(24), font(22)
    d.rectangle([20, 20, W - 20, H - 20], outline="black", width=4)

    # sheet index (top-left)
    d.text((50, 50), "SHEET INDEX:", fill="black", font=f)
    for i, line in enumerate(s["index"]):
        d.text((50, 90 + 34 * i), line, fill="black", font=fs)

    # room, dimensions
    d.rectangle([400, 350, 1300, 900], outline="black", width=5)
    d.text((430, 365), s["room_label"], fill="black", font=f)
    d.line([400, 300, 1300, 300], fill="black", width=2)
    d.text((810, 262), s["dim_top"], fill="black", font=f)
    d.line([400, 960, 1300, 960], fill="black", width=2)
    d.text((810, 972), s["dim_bottom"], fill="black", font=f)
    d.text((1325, 610), s["dim_right"], fill="black", font=f)

    # equipment
    for tag, x, y in s["units"]:
        d.rectangle([x, y, x + 120, y + 90], outline="black", width=3)
        d.text((x + 8, y + 100), tag, fill="black", font=fs)
    for x, y, tag in s["extra_boxes"]:
        d.rectangle([x, y, x + 120, y + 90], outline="black", width=3)
        if tag:
            d.text((x + 8, y + 100), tag, fill="black", font=fs)

    # door tags on the plan
    for i, tag in enumerate(s["door_tags"]):
        d.text((410 if i == 0 else 1215, 640 + 60 * i), tag, fill="black", font=fs)

    # section markers on the plan + section views
    for i, sec in enumerate(s["sections"]):
        d.text((375 if i == 0 else 1315, 905), sec, fill="black", font=fs)
        x0 = 60 + 420 * i
        d.rectangle([x0, 1100, x0 + 340, 1400], outline="black", width=3)
        d.text((x0 + 60, 1410), f"SECTION {sec}", fill="black", font=fs)

    # details
    for i, det in enumerate(s["details"]):
        x0 = 900 + 350 * i
        d.rectangle([x0, 1100, x0 + 300, 1400], outline="black", width=3)
        d.text((x0 + 10, 1410), f"DETAIL {det}", fill="black", font=fs)

    # schedules and notes (right column)
    d.text((1450, 350), "EQUIPMENT SCHEDULE", fill="black", font=f)
    for i, line in enumerate(s["schedule"]):
        d.text((1450, 392 + 34 * i), line, fill="black", font=fs)
    d.text((1450, 520), "DOOR SCHEDULE", fill="black", font=f)
    for i, line in enumerate(s["door_schedule"]):
        d.text((1450, 562 + 34 * i), line, fill="black", font=fs)
    d.text((1450, 700), "GENERAL NOTES:", fill="black", font=f)
    for i, line in enumerate(s["notes"]):
        d.text((1450, 742 + 36 * i), f"{i + 1}. {line}", fill="black", font=fs)

    # title block
    d.rectangle([1700, 1300, 2370, 1570], outline="black", width=3)
    d.text((1720, 1315), "PROJECT: RETAIL FIT-OUT", fill="black", font=fs)
    d.text((1720, 1355), f"SHEET TITLE: {s['title']}", fill="black", font=fs)
    d.text((1720, 1395), f"SHEET NO: {s['sheet_no']}", fill="black", font=fs)
    if s["scale_text"]:
        d.text((1720, 1435), s["scale_text"], fill="black", font=fs)
    im.save(path)


# page id -> spec overrides
PAGES = {
    "clean_1": {},
    "clean_2": dict(
        sheet_no="A-102", title="ENLARGED PLAN", room_label="ROOM 301 - STORAGE",
        units=[("AHU-3", 480, 450), ("AHU-4", 800, 450)],
        schedule=["AHU-3 AIR HANDLING UNIT 1800", "AHU-4 AIR HANDLING UNIT 1200"],
        door_tags=["D-201", "D-202"], door_schedule=["D-201 3'-0\" x 7'-0\"", "D-202 3'-0\" x 7'-0\""],
        dim_top="30'-0\"", dim_bottom="30'-0\"", dim_right="20'-0\"",
        notes=["ALL DIMENSIONS TO BE VERIFIED ON SITE.", "WALLS TO ROOM 301 ARE 6\" GYP STUD.",
               "SEE SECTION B-B FOR WALL ASSEMBLY.", "SEE DETAIL 2 FOR SUPPORT.",
               "REFER TO SHEET E-101 FOR POWER."]),
    "d01_dim_conflict": dict(dim_bottom="36'-0\""),
    "d02_untagged_equipment": dict(
        extra_boxes=[(980, 650, None)],
        schedule=["AHU-1 AIR HANDLING UNIT 2000", "AHU-2 AIR HANDLING UNIT 1500", "AHU-3 AIR HANDLING UNIT 1000"]),
    "d03_missing_detail_ref": dict(notes=[
        "ALL DIMENSIONS TO BE VERIFIED ON SITE.", "WALLS TO ROOM 204 ARE 8\" CMU.",
        "SEE SECTION A-A FOR WALL ASSEMBLY.", "SEE DETAIL 5 FOR CURB CONDITION.",
        "REFER TO SHEET M-101 FOR HVAC LAYOUT."]),
    "d04_missing_section_ref": dict(notes=[
        "ALL DIMENSIONS TO BE VERIFIED ON SITE.", "WALLS TO ROOM 204 ARE 8\" CMU.",
        "SEE SECTION C-C FOR WALL ASSEMBLY.", "SEE DETAIL 1 FOR CURB CONDITION.",
        "REFER TO SHEET M-101 FOR HVAC LAYOUT."]),
    "d05_conflicting_wall_spec": dict(notes=[
        "ALL DIMENSIONS TO BE VERIFIED ON SITE.", "WALLS TO ROOM 204 ARE 8\" CMU.",
        "SEE SECTION A-A FOR WALL ASSEMBLY.", "SEE DETAIL 1 FOR CURB CONDITION.",
        "REFER TO SHEET M-101 FOR HVAC LAYOUT.", "WALLS TO ROOM 204 ARE 6\" GYP STUD."]),
    "d06_duplicate_tag": dict(extra_boxes=[(980, 650, "AHU-2")]),
    "d07_invalid_sheet_ref": dict(notes=[
        "ALL DIMENSIONS TO BE VERIFIED ON SITE.", "WALLS TO ROOM 204 ARE 8\" CMU.",
        "SEE SECTION A-A FOR WALL ASSEMBLY.", "SEE DETAIL 1 FOR CURB CONDITION.",
        "REFER TO SHEET M-901 FOR HVAC LAYOUT."]),
    "d08_door_not_in_schedule": dict(door_tags=["D-101", "D-107"]),
    "d09_missing_scale": dict(scale_text=None),
    "d10_room_use_conflict": dict(notes=[
        "ALL DIMENSIONS TO BE VERIFIED ON SITE.", "WALLS TO ROOM 204 ARE 8\" CMU.",
        "SEE SECTION A-A FOR WALL ASSEMBLY.", "SEE DETAIL 1 FOR CURB CONDITION.",
        "REFER TO SHEET M-101 FOR HVAC LAYOUT.", "ROOM 204 IS THE ELECTRICAL ROOM."]),
}


def build():
    os.makedirs(OUT, exist_ok=True)
    for name, spec in PAGES.items():
        render(spec, os.path.join(OUT, f"{name}.png"))
    return OUT


if __name__ == "__main__":
    print("wrote", len(PAGES), "pages to", build())
