"""Ground truth for the benchmark pages.

Each defect lists the issue types a finding may use and regexes (all must match the
lower-cased description) that tie the finding to THAT defect. A finding that matches
no defect on its page is a false positive; any finding on a clean page is one too.
"""
ANY = None  # any issue_type is accepted

EXPECTED = {
    "clean_1": [],
    "clean_2": [],
    "d01_dim_conflict": [dict(id="D01", label="Dimension contradiction 40'-0\" vs 36'-0\"",
        types={"dimension_mismatch", "inconsistent_annotation"}, all=[r"40'", r"36'"])],
    "d02_untagged_equipment": [dict(id="D02", label="Untagged equipment (AHU-3 box)",
        types={"missing_tag", "unlabeled_element", "inconsistent_annotation"},
        all=[r"ahu-?3|third|untagged|no tag|not tagged|unlabeled|lower|bottom|right|box|rectangle|unit"],
        none=[r"^(?!.*ahu-?3).*\bahu-?[12]\b"])],     # a claim about tagged AHU-1/2 is not this defect
    "d03_missing_detail_ref": [dict(id="D03", label="Reference to non-existent DETAIL 5",
        types=ANY, all=[r"detail\s*5|5\b.*detail"])],
    "d04_missing_section_ref": [dict(id="D04", label="Reference to non-existent SECTION C-C",
        types=ANY, all=[r"c-c|section c"])],
    "d05_conflicting_wall_spec": [dict(id="D05", label="Room 204 walls 8\" CMU vs 6\" GYP STUD",
        types={"inconsistent_annotation", "dimension_mismatch"}, all=[r"cmu", r"gyp|stud"])],
    "d06_duplicate_tag": [dict(id="D06", label="Duplicate tag AHU-2 on two units",
        types=ANY, all=[r"ahu-?2", r"duplicate|twice|two |same|both|more than one|multiple|appears|repeat"])],
    "d07_invalid_sheet_ref": [dict(id="D07", label="Reference to sheet M-901 not in index",
        types=ANY, all=[r"m-?901"])],
    "d08_door_not_in_schedule": [dict(id="D08", label="Door tag D-107 not in door schedule",
        types=ANY, all=[r"d-?107"])],
    "d09_missing_scale": [dict(id="D09", label="No scale given on sheet",
        types={"missing_scale"}, all=[r"."])],
    "d10_room_use_conflict": [dict(id="D10", label="Room 204 is MECH RM on plan, ELECTRICAL ROOM in note",
        types={"inconsistent_annotation", "unlabeled_element", "missing_tag"}, all=[r"204", r"mech|elec"])],
}
