"""The takeoffs a user can ask for by name ("the lighting takeoff").

A discipline says what belongs in it: the object classes counting already knows, any
extra tag forms, and the words that make a sheet relevant. Pure data plus the request
parser; no I/O.
"""
import re
from dataclasses import dataclass, replace

from backend.services.counting.objects import REGISTRY, ObjectClass


def _cls(key: str) -> ObjectClass:
    return next(c for c in REGISTRY if c.key == key)


@dataclass(frozen=True)
class Discipline:
    key: str
    label: str
    class_keys: tuple[str, ...]
    keywords: tuple[str, ...]            # what a line or heading in this discipline names
    page_words: tuple[str, ...]          # sheet-title/text words of its sheets
    tag_re: str | None = None            # extra tag forms (besides the classes' own)
    extra_unit_tags: bool = False        # extra tags are single units (VAV-1) rather than types
    tag_mode: str = "type"
    # Headings that start a legend entry for this discipline. Narrower than `keywords`:
    # a heading must name the thing itself, not merely mention it ("... WITH ROOM LIGHTS").
    legend_phrases: tuple[str, ...] = ()
    # A heading containing any of these is something else (a switch, a note), not an item.
    exclude_words: tuple[str, ...] = ("SWITCH", "SENSOR", "SUPPLIED", "INSTALLED", "NOTE", "NOTES", "CONTROL", "CONTROLS")

    @property
    def classes(self) -> list[ObjectClass]:
        return [_cls(k) for k in self.class_keys]

    def class_for_tag(self, tag: str) -> ObjectClass:
        cls = next((c for c in self.classes if c.tag_re and re.match(c.tag_re, tag)), None) or self.object_class()
        # On a fixture schedule a mark (WC-1) is a TYPE drawn wherever it is used, not one unit.
        return replace(cls, tag_mode="type") if self.key == "plumbing" else cls

    def tag_pattern(self) -> re.Pattern | None:
        parts = [c.tag_re for c in self.classes if c.tag_re] + ([self.tag_re] if self.tag_re else [])
        return re.compile("|".join(f"(?:{p})" for p in parts)) if parts else None

    def object_class(self) -> ObjectClass:
        """A class standing for the whole discipline, for the counting code that wants one."""
        classes = self.classes
        legend = self.legend_phrases
        return ObjectClass(
            key=self.key, label=self.label.lower(), keywords=self.keywords,
            tag_re=None, tag_mode=self.tag_mode, legend_keywords=tuple(dict.fromkeys(legend)),
            plan_titles=tuple(dict.fromkeys(w for c in classes for w in c.plan_titles)),
            generic=self.keywords,
        )


DISCIPLINES: dict[str, Discipline] = {
    "lighting": Discipline(
        "lighting", "Lighting", ("light", "exit"),
        keywords=("LIGHT", "LIGHTING", "FIXTURE", "LUMINAIRE", "TROFFER", "DOWNLIGHT", "LED", "PENDANT", "EXIT SIGN"),
        page_words=("LIGHTING", "REFLECTED CEILING", "CEILING", "ELECTRICAL"),
        tag_re=r"^(?:LF|EX|X)-?\d{1,2}[A-Z]?$",
        legend_phrases=("FIXTURE", "LUMINAIRE", "DOWNLIGHT", "TROFFER", "PENDANT", "EXIT SIGN", "STRIP LIGHT"),
    ),
    "hvac": Discipline(
        "hvac", "HVAC", ("rtu", "ahu", "fan", "diffuser"),
        keywords=("ROOFTOP UNIT", "AIR HANDLING", "FAN", "DIFFUSER", "GRILLE", "REGISTER", "VAV", "FCU", "HEAT PUMP",
                  "UNIT HEATER", "FURNACE", "THERMOSTAT", "DUCT"),
        page_words=("MECHANICAL", "HVAC", "ROOF", "DUCT", "VENTILATION"),
        tag_re=r"^(?:VAV|FCU|UH|HP|CU|HRV|ERV|MAU|AC|FC)-?\d+[A-Z]?$", extra_unit_tags=True, tag_mode="unit",
        legend_phrases=("ROOFTOP UNIT", "AIR HANDLING", "EXHAUST FAN", "DIFFUSER", "GRILLE", "REGISTER", "UNIT HEATER",
                        "HEAT PUMP", "FAN COIL"),
    ),
    "plumbing": Discipline(
        "plumbing", "Plumbing fixtures", ("toilet", "sink", "urinal"),
        keywords=("WATER CLOSET", "TOILET", "LAVATORY", "SINK", "URINAL", "FLOOR DRAIN", "SHOWER", "DRINKING FOUNTAIN",
                  "FAUCET", "HOSE BIB", "MOP SINK", "FIXTURE"),
        page_words=("PLUMBING", "WASHROOM", "SANITARY", "DOMESTIC WATER"),
        tag_re=r"^(?:FD|HB|WH|DF|EWC|SH|MS|P)-?\d+[A-Z]?$",
        legend_phrases=("WATER CLOSET", "TOILET", "LAVATORY", "SINK", "URINAL", "FLOOR DRAIN", "SHOWER",
                        "DRINKING FOUNTAIN", "HOSE BIB", "MOP SINK"),
    ),
}

_TAKEOFF_RE = re.compile(r"\btake[\s\-]?offs?\b", re.I)
_ALIASES = (
    ("lighting", re.compile(r"\b(?:light(?:s|ing)?|luminaires?|electrical\s+fixtures?)\b", re.I)),
    ("hvac", re.compile(r"\b(?:hvac|mechanical|heating|ventilation|air[\s\-]?conditioning|ductwork)\b", re.I)),
    ("plumbing", re.compile(r"\b(?:plumbing|sanitary|water\s+closets?|toilets?|lavator(?:y|ies)|washroom)\b", re.I)),
)


def is_takeoff_request(question: str) -> bool:
    return bool(_TAKEOFF_RE.search(question))


def parse_takeoff_request(question: str) -> Discipline | None:
    """The discipline named in 'give me the lighting takeoff', or None."""
    if not is_takeoff_request(question):
        return None
    for key, pattern in _ALIASES:
        if pattern.search(question):
            return DISCIPLINES[key]
    return None
