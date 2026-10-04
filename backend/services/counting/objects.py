"""What the user asked to count: parsing the question and a registry of object classes.

A class says how an object usually shows up in a drawing set: what its tags look
like (RTU-1, EF-2, D101), words that name it, whether each tag is one unit or a
type applied to many, and which sheet types are authoritative for it. Unknown
objects ("sprinklers") get an ad-hoc class built from the question's own words,
so the registry is a head start, not a limit.

Pure functions; no I/O.
"""
import re
from dataclasses import dataclass, field

from backend.services.retrieval_service import normalise_text

_COUNT_INTENT_RE = re.compile(
    r"\b(how\s+many|count\b|number\s+of|total\s+(?:number|count|quantity)|quantity\s+of|how\s+much\s+of)\b",
    re.IGNORECASE,
)


def is_count_question(question: str) -> bool:
    return bool(_COUNT_INTENT_RE.search(question))


@dataclass(frozen=True)
class ObjectClass:
    key: str
    label: str                       # singular, for messages
    keywords: tuple[str, ...]        # words naming it, singular, upper-case
    # How tags look. Matched against single words (upper-case).
    tag_re: str | None = None
    # unit: every distinct tag is one object (RTU-1, RTU-2 -> 2).
    # type: a tag is a kind of object drawn many times (L1 x 14).
    tag_mode: str = "type"
    # Words that make a heading line a legend/schedule entry for this class.
    legend_keywords: tuple[str, ...] = ()
    # Sheet-title words of the drawings that show it, best first. Counting on
    # sheets of different kinds would double count the same objects.
    plan_titles: tuple[str, ...] = ()
    # Words that only name the class ("light", "fixture"). Any other word the user
    # adds ("downlight", "recessed", "pendant") is a restriction and must appear in
    # the schedule row or legend heading that is counted. None = all keywords generic.
    generic: tuple[str, ...] | None = None
    # A reason a tag-based count of this object can be wrong even when it is
    # consistent. Always surfaced, and the count can never be better than
    # needs_verification.
    tag_caveat: str | None = None

    @property
    def generic_words(self) -> set[str]:
        src = self.generic if self.generic is not None else tuple(w for k in self.keywords for w in k.split())
        return {singular(w) for w in src}


REGISTRY: tuple[ObjectClass, ...] = (
    ObjectClass("rtu", "rooftop unit", ("RTU", "ROOFTOP UNIT", "ROOF TOP UNIT"),
                tag_re=r"^RTU-?\d+[A-Z]?$", tag_mode="unit",
                legend_keywords=("ROOFTOP", "RTU"), plan_titles=("MECHANICAL", "ROOF")),
    ObjectClass("ahu", "air handling unit", ("AHU", "AIR HANDLING UNIT", "AIR HANDLER"),
                tag_re=r"^AHU-?\d+[A-Z]?$", tag_mode="unit",
                legend_keywords=("AIR HANDLING",), plan_titles=("MECHANICAL",)),
    ObjectClass("fan", "fan", ("EXHAUST FAN", "FAN", "EF"),
                tag_re=r"^(?:EF|SF|RF|CF)-?\d+[A-Z]?$", tag_mode="unit",
                legend_keywords=("EXHAUST FAN",), plan_titles=("MECHANICAL",)),
    ObjectClass("diffuser", "diffuser", ("DIFFUSER", "GRILLE", "GRILL", "REGISTER", "AIR DEVICE"),
                tag_re=r"^(?:SD|RD|CD|SG|RG|EG|TG)-?\d+[A-Z]?$", tag_mode="type",
                legend_keywords=("DIFFUSER", "GRILLE", "AIR DEVICE"),
                plan_titles=("MECHANICAL", "CEILING", "HVAC")),
    ObjectClass("light", "light fixture", ("LIGHT", "LIGHTING", "FIXTURE", "LUMINAIRE", "TROFFER", "DOWNLIGHT",
                                            "LED", "LAMP", "PENDANT", "TRACK"),
                generic=("LIGHT", "LIGHTING", "FIXTURE", "LUMINAIRE", "LAMP"),
                tag_re=r"^L-?\d{1,2}[A-Z]?$", tag_mode="type",
                legend_keywords=("FIXTURE", "LUMINAIRE", "DOWNLIGHT", "TROFFER", "LIGHT"),
                plan_titles=("LIGHTING", "REFLECTED CEILING", "CEILING", "ELECTRICAL")),
    ObjectClass("door", "door", ("DOOR",),
                tag_caveat="Door tags usually identify door TYPES (D1, D2), not individual doors, so a count "
                           "from tags can be too low. Count the door symbols on the plan to confirm.",
                tag_re=r"^D-?\d{1,3}[A-Z]?$", tag_mode="unit",
                legend_keywords=("DOOR",), plan_titles=("FLOOR PLAN", "PLAN", "CONSTRUCTION")),
    ObjectClass("toilet", "toilet", ("TOILET", "WATER CLOSET", "WC"),
                tag_re=r"^WC-?\d+[A-Z]?$", tag_mode="unit",
                legend_keywords=("TOILET", "WATER CLOSET"), plan_titles=("PLUMBING", "PLAN", "WASHROOM")),
    ObjectClass("sink", "sink", ("SINK", "LAVATORY", "LAV"),
                tag_re=r"^(?:S|LAV|SK)-?\d+[A-Z]?$", tag_mode="type",
                legend_keywords=("SINK", "LAVATORY"), plan_titles=("PLUMBING", "PLAN")),
    ObjectClass("urinal", "urinal", ("URINAL",), tag_re=r"^UR-?\d+[A-Z]?$", tag_mode="type",
                legend_keywords=("URINAL",), plan_titles=("PLUMBING", "PLAN")),
    ObjectClass("panel", "panel", ("PANEL", "PANELBOARD"), tag_re=r"^(?:LP|PP|DP|RP|MDP)-?\d+[A-Z]?$",
                tag_mode="unit", legend_keywords=("PANELBOARD", "PANEL"), plan_titles=("ELECTRICAL", "POWER")),
    ObjectClass("receptacle", "receptacle", ("RECEPTACLE", "OUTLET"), tag_re=None, tag_mode="type",
                legend_keywords=("RECEPTACLE", "OUTLET"), plan_titles=("ELECTRICAL", "POWER")),
    ObjectClass("exit", "exit sign", ("EXIT SIGN", "EXIT"), tag_re=None, tag_mode="type",
                legend_keywords=("EXIT",), plan_titles=("LIGHTING", "CEILING", "ELECTRICAL")),
    ObjectClass("sprinkler", "sprinkler head", ("SPRINKLER", "SPRINKLER HEAD"), tag_re=None, tag_mode="type",
                legend_keywords=("SPRINKLER",), plan_titles=("CEILING", "FIRE", "SPRINKLER")),
    ObjectClass("switch", "switch", ("SWITCH",), tag_re=None, tag_mode="type",
                legend_keywords=("SWITCH",), plan_titles=("LIGHTING", "CEILING", "ELECTRICAL")),
)

_NOISE = {
    "HOW", "MANY", "ARE", "IS", "THERE", "THE", "A", "AN", "OF", "ALL", "SHOWN", "SHOW", "DRAWN", "DO", "WE",
    "HAVE", "TOTAL", "NUMBER", "COUNT", "QUANTITY", "WHAT", "TELL", "ME", "IN", "ON", "THESE", "THOSE", "THIS",
    "DRAWINGS", "DRAWING", "PLANS", "PLAN", "SET", "PROJECT", "NEED", "NEEDED", "REQUIRED", "INCLUDED",
    "PLEASE", "CAN", "YOU", "GIVE", "FOR", "TO", "BE", "ARE", "PROVIDED", "INSTALLED", "WILL", "LISTED",
    "SHEET", "SHEETS", "PAGE", "PAGES",
}
# "...in BOH", "...in the washroom": a region of the building. Counting inside a region
# needs room boundaries, which are not read, so it is reported rather than applied.
_AREA_RE = re.compile(
    r"\b(?:in|inside|within|at|on|for)\s+(?:the\s+)?(?P<area>(?!all\b|total\b|this\b|these\b|the\b|drawings?\b|plans?\b|sheets?\b)[A-Za-z][\w\- ]{1,40}?)\s*[?.!]*$",
    re.IGNORECASE,
)


@dataclass
class ParsedCount:
    phrase: str                      # the object as the user said it, normalised
    words: list[str]                 # singularised object words (upper-case)
    object_class: ObjectClass | None
    sizes: list[str] = field(default_factory=list)   # e.g. ["2X4"]
    area: str | None = None          # e.g. "BOH" (not applied; reported)

    @property
    def specific_words(self) -> list[str]:
        """Words that restrict the class (see ObjectClass.generic)."""
        generic = self.object_class.generic_words if self.object_class else set()
        return [w for w in self.words if w not in generic]

    @property
    def label(self) -> str:
        return self.object_class.label if self.object_class else (" ".join(self.words).lower() or "object")


def singular(word: str) -> str:
    w = word.upper()
    if w.endswith("IES") and len(w) > 4:
        return w[:-3] + "Y"
    if w.endswith(("SES", "XES", "ZES", "CHES", "SHES")) and len(w) > 4:
        return w[:-2]
    if w.endswith("S") and not w.endswith("SS") and len(w) > 3:
        return w[:-1]
    return w


def size_variants(size: str) -> set[str]:
    """2X4 is also 4X2, and 24X48 inches. Drawings write fixtures either way."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)", size)
    if not m:
        return {size}
    a, b = m.group(1), m.group(2)
    out = {f"{a}X{b}", f"{b}X{a}"}
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return out
    for x, y in ((fa, fb), (fb, fa)):
        if x < 12 and y < 12:                     # feet -> inches
            out.add(f"{int(x * 12)}X{int(y * 12)}")
        elif x % 12 == 0 and y % 12 == 0:          # inches -> feet
            out.add(f"{int(x // 12)}X{int(y // 12)}")
    return out


def _match_class(words: list[str], phrase_upper: str) -> ObjectClass | None:
    wordset = set(words)
    best, best_len = None, 0
    for cls in REGISTRY:
        for kw in cls.keywords:
            if all(singular(w) in wordset for w in kw.split()):
                if len(kw) > best_len:
                    best, best_len = cls, len(kw)
    return best


def parse_count_question(question: str) -> ParsedCount | None:
    """Object, size qualifiers and area from a 'how many ...' question, or None if
    no object can be made out."""
    q = question.strip()
    area = None
    m = _AREA_RE.search(q)
    if m:
        cand = m.group("area").strip()
        # "in total" / "in the drawings" are not areas; real areas are short names.
        if cand and not re.fullmatch(r"(?:total|all|the drawings?|the plans?|the set|the project)", cand, re.I):
            area = cand
            q = q[: m.start()]
    text = normalise_text(q)
    sizes = list(dict.fromkeys(re.findall(r"\b\d+(?:\.\d+)?X\d+(?:\.\d+)?\b", text)))
    text_nosize = re.sub(r"\b\d+(?:\.\d+)?X\d+(?:\.\d+)?\b", " ", text)
    tokens = [t for t in re.findall(r"[A-Z][A-Z0-9\-]*", text_nosize) if t not in _NOISE]
    words = [singular(t) if not re.search(r"\d", t) else t for t in tokens]
    if not words:
        return None
    cls = _match_class(words, " ".join(words))
    return ParsedCount(" ".join(tokens).lower(), words, cls, sizes, area)


def adhoc_class(parsed: ParsedCount) -> ObjectClass:
    """A class for an object the registry does not know, from the question's words."""
    kws = tuple(dict.fromkeys(parsed.words))
    return ObjectClass("adhoc", " ".join(parsed.words).lower(), kws, tag_re=None, tag_mode="type",
                       legend_keywords=kws)
